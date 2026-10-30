import asyncio
import base64
import datetime
import glob
import io
import logging
import math
import os
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime
from time import localtime, strftime

import numpy as np
import pandas as pd
import shortuuid
from cbpi.api import *
from cbpi.api import clock as sim_clock
from cbpi.api.base import CBPiBase
from cbpi.api.config import ConfigType
from urllib3 import PoolManager, Timeout


class LogController:

    def __init__(self, cbpi):
        """

        :param cbpi: craftbeerpi object
        """
        self.cbpi = cbpi
        self.logger = logging.getLogger(__name__)
        self.configuration = False
        self.datalogger = {}
        self.logsFolderPath = self.cbpi.config_folder.logsFolderPath
        self.logger.info("Log folder path  : " + self.logsFolderPath)
        self.sensor_data_listeners = {}

    def add_sensor_data_listener(self, method):
        listener_id = shortuuid.uuid()
        self.sensor_data_listeners[listener_id] = method
        return listener_id

    def remove_sensor_data_listener(self, listener_id):
        try:
            del self.sensor_data_listener[listener_id]
        except:
            self.logger.error("Failed to remove listener {}".format(listener_id))

    async def _call_sensor_data_listeners(self, sensor_id, value, formatted_time, name):
        for listener_id, method in self.sensor_data_listeners.items():
            asyncio.create_task(
                method(self.cbpi, sensor_id, value, formatted_time, name)
            )

    def log_data(self, id: str, value: str, timestamp=None) -> None:
        # all plugin targets:
        if self.sensor_data_listeners:  # true if there are listners
            try:
                sensor = self.cbpi.sensor.find_by_id(id)
                if sensor is not None:
                    name = sensor.name.replace(" ", "_")
                    self._emit(id, value, name, timestamp)
            except Exception as e:
                logging.error("sensor logging listener exception: {}".format(e))

    def log_control_data(self, series_id: str, value, name: str = None) -> None:
        """Log a series that is not a sensor - a duty, a setpoint, a PID term.

        log_data() resolves the id to a registered sensor to get a display
        name, and silently returns when it cannot. That is correct for sensors
        and fatal for anything else: a kettle logic writing "<kettle>.duty" is
        not a sensor, so every write was dropped without a word.

        Found by running it: PID_LOGGING on, the cascade running, and not one
        series file on disk. The unit test had passed throughout because it
        stubbed cbpi.log.log_data and so never met the real controller - a fake
        more permissive than the thing it stood in for.

        This takes the name instead of looking it up. Everything downstream is
        unchanged: the same listeners, the same millisecond timestamps, the
        same batching, rotation and retention, and the same chart endpoint.
        """
        if not self.sensor_data_listeners:
            return
        try:
            self._emit(series_id, value, (name or series_id).replace(" ", "_"))
        except Exception as e:  # noqa: BLE001 - telemetry never breaks control
            logging.error("control logging listener exception: {}".format(e))

    def _emit(self, id, value, name, timestamp=None):
        """Hand a reading to every log target, stamped to the millisecond.

        Timestamps were "%Y-%m-%d %H:%M:%S", which is fine when readings arrive
        once a second and destructive when they arrive faster. A simulated
        vessel at SIM_TIME_SCALE=60 logs one sample per simulated second, so
        about forty rows landed in every real second carrying an identical
        timestamp - measured on the rig, 400 rows collapsing to 13 distinct
        timestamps.

        Two costs. The chart cannot resolve finer than one real second, which
        at 60x is a whole simulated minute per point, and the bucket is reduced
        with .max() so a ramp comes out as a staircase and a dip vanishes. And
        the duplicate rows are pure waste: sixty times the disk for no extra
        information.

        The stamp is SIMULATED time: cbpi.api.clock, which on real hardware is
        time.time() itself, so nothing changes there. Stamped with real time,
        an accelerated run squeezed its x-axis by the time scale - a simulated
        hour of mash charted across one real minute. `timestamp`, in the same
        clock, is when the reading was taken, if that was not now.

        Readers accept both formats, so existing logs still load.
        """
        when = None
        if timestamp is not None:
            try:
                when = float(timestamp)
            except (TypeError, ValueError):
                when = None
            if when is not None and not math.isfinite(when):
                when = None
        if when is None:
            when = sim_clock.now()
        formatted_time = datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        asyncio.create_task(
            self._call_sensor_data_listeners(id, value, formatted_time, name)
        )

    async def _flush_datalogger(self, name):
        """Push any buffered readings to disk before something reads the files.

        Sensor logging batches writes - see BatchingRotatingFileHandler - so up
        to SENSOR_LOG_FLUSH_SECONDS of readings normally sit in memory. That is
        the point, and it is invisible to anything that goes through the live
        push updates.

        It is very visible to anything that reads the log files directly, which
        is what this controller does. Without this flush the chart a brewer
        pulls up is missing its most recent minute - the minute they are most
        likely to be looking for, because it is the one happening now. It also
        broke tests/test_logger.py, which writes five readings and reads them
        back immediately.

        The flush runs in an executor for the same reason the writes do: on a
        network share it can block, and this is called from the request path.
        """
        data_logger = self.datalogger.get(name)
        if data_logger is None:
            return
        try:
            loop = asyncio.get_running_loop()
            for handler in list(data_logger.handlers):
                await loop.run_in_executor(None, handler.flush)
        except (RuntimeError, OSError) as e:  # noqa: BLE001
            # Never fail a chart request over this; stale is better than broken.
            logging.warning("Could not flush sensor log for %s: %s", name, e)

    # Points to aim for in a chart. The row cap below is 500, so this keeps
    # the resample and the cap from fighting each other.
    TARGET_POINTS = 500

    def _sample_rate_for(self, index):
        """Choose a bucket size from how much time the data actually covers.

        This was hardcoded at 60s, which quietly decides that one minute of
        wall-clock is the finest thing worth seeing. That is reasonable for a
        six hour brew day and wrong for everything shorter.

        It was badly wrong for an accelerated simulation while log rows were
        stamped with real time: at SIM_TIME_SCALE=60 an hour-long mash
        happened in one real minute and landed in a single 60s bucket - the
        whole step became one point. Rows now carry simulated time (_emit), so
        that hour spans an hour; the span still decides the bucket, because
        the bucket is reduced with .max(), and a bucket too coarse for its
        span makes the dip when grain goes in not merely coarse but invisible.
        A chart that cannot show a dough-in dip or a stalled ramp is not worth
        reading, and those are the two things it is read for.

        Deriving the rate from the span needs no plugin, no configuration and
        no knowledge of the time scale here.
        """
        try:
            if len(index) < 2:
                return None
            span = (index.max() - index.min()).total_seconds()
        except Exception:  # noqa: BLE001 - charting must not fail on odd data
            return "60s"
        if span <= 0:
            return None
        # One bucket per target point. Timestamps carry milliseconds, so this
        # is allowed below a second: an accelerated simulation writes dozens of
        # distinct readings per real second, and flooring at 1s would throw
        # them away again - which is the whole defect this is here to avoid.
        # Floored at 100ms, which is finer than anything worth charting.
        seconds = span / self.TARGET_POINTS
        if seconds >= 1:
            return "{}s".format(int(seconds))
        return "{}ms".format(max(100, int(seconds * 1000)))

    # A step back in a log's timestamps bigger than this is the clock being
    # changed, not two readings racing each other.
    CLOCK_BACK_TOLERANCE_S = 2.0

    # How long a clock that went back may run before it is put right, measured
    # on that clock. NTP set the rig's clock 2 min 9 s after it booted from the
    # 25 Oct power cut (DEFECTS C17).
    CLOCK_FIX_WITHIN_S = 900.0

    def _log_files_oldest_first(self, name):
        """A sensor's log files in the order they were written.

        The rotating handler renames sensor_X.log to .log.1, .log.1 to .log.2
        and so on, so the highest number is the oldest. glob returns them in
        whatever order the file system likes. That did not matter while the
        rows were simply sorted by time; it does now, because a clock that went
        back can only be recognised in the order the rows were written - and a
        power cut can put the step back in one file and the correction in the
        next.
        """
        prefix = "sensor_{}.log".format(name)
        files = []
        for path in glob.glob(os.path.join(self.logsFolderPath, glob.escape(prefix) + "*")):
            suffix = os.path.basename(path)[len(prefix):]
            if suffix == "":
                files.append((0, path))
            elif suffix.startswith(".") and suffix[1:].isdigit():
                files.append((int(suffix[1:]), path))
        return [path for _, path in sorted(files, reverse=True)]

    @staticmethod
    def _read_log_file(path, column):
        """One log file's rows, in the order written: DateTime and the value.

        Survives what a power cut leaves in a file (DEFECTS C17). NUL bytes
        where the file had grown but the data never reached the card - 372 of
        them on 25 Oct, in front of the first row written after the restart -
        are removed, so that row reads again; before, its time was unreadable.
        A line that is not two fields is skipped rather than failing the whole
        chart. A row whose time or value cannot be read is kept as NaT / NaN for
        the caller to drop: its time still says something about the clock.
        """
        with open(path, "rb") as f:
            raw = f.read().replace(b"\x00", b"")
        if not raw.strip():
            return pd.DataFrame({"DateTime": pd.Series(dtype="datetime64[ns]"),
                                 column: pd.Series(dtype="float64")})
        df = pd.read_csv(io.BytesIO(raw), names=["DateTime", column], header=None,
                         dtype=str, on_bad_lines="skip", skip_blank_lines=True)
        # format="mixed" so logs written before timestamps gained milliseconds
        # still parse alongside new ones.
        df["DateTime"] = pd.to_datetime(df["DateTime"], format="mixed", errors="coerce")
        df[column] = pd.to_numeric(df[column], errors="coerce")
        return df

    def _misstamped(self, times):
        """Which rows were stamped by a clock that went back and was then put right.

        `times` are the rows' DateTimes in the order they were written. Returns
        a boolean array, True for the rows to leave out.

        A Raspberry Pi has no clock that runs without power. After a power cut
        it boots with the time it last saved, the server starts and logs, and a
        minute or two later NTP sets the real time. On 25 Oct the rig's HLT log
        ran 10:02:34, then 10:01:01 ... 10:03:09 for 129 rows, then 11:42:14:
        readings taken at about 11:40-11:42 were stamped 10:01-10:03, and the
        chart drew the HLT at 114 F at 10:02:56, where it had been about 120.

        So a step back of more than CLOCK_BACK_TOLERANCE_S, followed within
        CLOCK_FIX_WITHIN_S on the slow clock by a step forward that makes up at
        least as much as was lost and lands past every earlier row, marks the
        rows between as misstamped. The forward step is never smaller than the
        backward one: the slow clock is behind by the outage as well. A step is
        judged after taking off the sensor's usual interval between readings,
        so a once-a-minute probe's ordinary 60 s does not pass for the fix.
        When no such step comes - daylight saving ending, or NTP pulling a fast
        clock back - the clock that went back was right, and every row is kept.

        Left out rather than restamped: their true time is known only to about
        a reading interval, and a chart missing two minutes is honest where one
        with two guessed minutes is not.
        """
        n = len(times)
        drop = np.zeros(n, dtype=bool)
        if n < 2:
            return drop
        s = (pd.to_datetime(times).to_numpy(dtype="datetime64[ns]")
             .astype("int64") / 1e9)
        tol = self.CLOCK_BACK_TOLERANCE_S

        def next_step_back(start, latest):
            if start >= n:
                return None
            run = np.maximum.accumulate(np.concatenate(([latest], s[start:])))
            behind = np.nonzero(run[:-1] - s[start:] > tol)[0]
            if behind.size == 0:
                return None
            k = int(behind[0])
            return start + k, float(run[k])

        found = next_step_back(0, -np.inf)
        if found is None:
            return drop  # nearly every log: the clock never went back
        steps = np.diff(s)
        positive = steps[steps > 0]
        usual = float(np.median(positive)) if positive.size else 0.0

        while found is not None:
            i, latest = found
            lost = latest - s[i]
            fixed_at = None
            for k in range(i + 1, n):
                if (s[k] - s[k - 1]) - usual >= lost - tol and s[k] > latest:
                    fixed_at = k
                    break
                if s[k] - s[i] > self.CLOCK_FIX_WITHIN_S:
                    break
            if fixed_at is None:
                # The clock that went back was right: keep its rows, and judge
                # what follows by it.
                found = next_step_back(i + 1, s[i])
            else:
                drop[i:fixed_at] = True
                found = next_step_back(fixed_at, latest)
        return drop

    def _read_sensor_log(self, name, column):
        """Every reading in a sensor's logs, indexed by time and sorted, with
        the rows a power cut misstamped left out (see _misstamped)."""
        frames = [self._read_log_file(path, column)
                  for path in self._log_files_oldest_first(name)]
        frames = [f for f in frames if not f.empty]
        if not frames:
            return pd.DataFrame({column: pd.Series(dtype="float64")},
                                index=pd.DatetimeIndex([], name="DateTime"))
        df = pd.concat(frames, ignore_index=True)
        df = df.dropna(subset=["DateTime"])
        misstamped = self._misstamped(df["DateTime"])
        if misstamped.any():
            # Debug, not warning: a dashboard chart asks every ten seconds.
            self.logger.debug(
                "Log %s: left out %d rows stamped by a clock that was later put right",
                name, int(misstamped.sum()))
            df = df[~misstamped]
        return df.set_index("DateTime").sort_index()

    async def get_data(self, names, sample_rate=None):
        logging.info("Start Log for {}".format(names))
        """
        :param names: name as string or list of names as string
        :param sample_rate: rate for resampling the data. None picks one from
                            the span of the data, which is almost always what
                            you want - see _sample_rate_for.
        :return:
        """
        # make string to array
        if isinstance(names, list) is False:
            names = [names]

        # remove duplicates
        names = set(names)
        timestamp_format = "%Y-%m-%d %H:%M:%S"

        result = None

        for name in names:
            # Buffered readings are not on disk yet, and this reads the files.
            await self._flush_datalogger(name)
            series = self._read_sensor_log(name, name)[name]
            logging.info("Read all files for {}".format(names))

            # resample if rate provided
            rate = sample_rate
            if rate is None:
                rate = self._sample_rate_for(series.index)
            if rate is not None:
                series = series.resample(rate).max()
            logging.info("Sampled now for {}".format(names))
            series = series.dropna()
            # take every nth row so that total number of rows does not exceed max_rows * 2
            max_rows = 500
            total_rows = series.shape[0]
            if (total_rows > 0) and (total_rows > max_rows):
                nth = int(total_rows / max_rows)
                if nth > 1:
                    series = series.iloc[::nth]

            frame = series.to_frame(name)
            if result is None:
                result = frame
            else:
                result = pd.merge(
                    result, frame, how="outer", left_index=True, right_index=True
                )

        # The merged index: with several sensors, the last sensor's own index
        # was sent as the times of every series, so the lists did not line up.
        data = {"time": result.index.tolist()}

        if len(names) > 1:
            for name in names:
                data[name] = (
                    result[name].interpolate(limit_direction="both", limit=10).tolist()
                )
        else:
            data[name] = result[name].interpolate().tolist()

        logging.info("Send Log for {}".format(names))

        return data

    async def get_data2(self, ids) -> dict:
        result = dict()
        for id in ids:
            try:
                df = self._read_sensor_log(id, "Values")
                if df.empty:
                    continue  # no log: left out, as before
                df = df.resample("60s").max()
                df = df.dropna()
                result[id] = {
                    "time": df.index.astype(str).tolist(),
                    "value": df.Values.tolist(),
                }
            except:
                pass
        return result

    def get_logfile_names(self, name: str) -> list:
        """
        Get all log file names
        :param name: log name as string. pattern /logs/sensor_%s.log*
        :return: list of log file names
        """

        return [
            os.path.basename(x)
            for x in glob.glob(os.path.join(self.logsFolderPath, f"sensor_{name}.log*"))
        ]

    def clear_log(self, name: str) -> str:
        all_filenames = glob.glob(
            os.path.join(self.logsFolderPath, f"sensor_{name}.log*")
        )

        logging.info(f"Deleting logfiles for sensor {name}.")

        if name in self.datalogger:
            # Close, not just remove. removeHandler() detaches the handler from
            # the logger but leaves its file open, and on Windows an open handle
            # makes both the rotation rename and the os.remove below fail - so
            # "clear log" would leave the files in place and the next rollover
            # would start throwing.
            #
            # Every handler, not handlers[0]. A logger is a process-global
            # singleton, so a previous leak can have left more than one attached
            # to the same file, and taking only the first leaves the rest
            # holding it open.
            for handler in list(self.datalogger[name].handlers):
                try:
                    self.datalogger[name].removeHandler(handler)
                    handler.close()
                except Exception as e:
                    logging.warning("Could not close log handler for %s: %s", name, e)
            del self.datalogger[name]

        for f in all_filenames:
            try:
                os.remove(f)
            except Exception as e:
                logging.warning(e)

    def get_all_zip_file_names(self, name: str) -> list:
        """
        Return a list of all zip file names
        :param name:
        :return:
        """

        return [
            os.path.basename(x)
            for x in glob.glob(
                os.path.join(self.logsFolderPath, f"*-sensor-{name}.zip")
            )
        ]

    def clear_zip(self, name: str) -> None:
        """
        clear all zip files for a sensor
        :param name: sensor name
        :return: None
        """

        all_filenames = glob.glob(
            os.path.join(self.logsFolderPath, f"*-sensor-{name}.zip")
        )
        for f in all_filenames:
            os.remove(f)

    def zip_log_data(self, name: str) -> str:
        """
        :param name: sensor name
        :return: zip_file_name
        """

        formatted_time = strftime("%Y-%m-%d-%H_%M_%S", localtime())
        file_name = os.path.join(
            self.logsFolderPath, f"{formatted_time}-sensor-{name}.zip"
        )
        zip = zipfile.ZipFile(file_name, "w", zipfile.ZIP_DEFLATED)
        all_filenames = glob.glob(
            os.path.join(self.logsFolderPath, f"sensor_{name}.log*")
        )
        for f in all_filenames:
            zip.write(os.path.join(f))
        zip.close()
        return os.path.basename(file_name)
