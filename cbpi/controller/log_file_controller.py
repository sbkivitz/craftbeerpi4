import asyncio
import base64
import datetime
import glob
import io
import logging
import math
import os
import re
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
from cbpi.api import logclock
from cbpi.api.base import CBPiBase
from cbpi.api.config import ConfigType
from urllib3 import PoolManager, Timeout

#: The column _read_log_file adds, for a log with a row saying CraftBeerPi's clock moved: each row's move, in seconds,
#: NaN for a reading.
MOVED_COLUMN = "(clock moved)"


class LogStamp(str):
    """A reading's time as the logs write it, handed to every log target (_emit), with the brewing clock's seconds it
    was taken at (`when`) and how many times the clock had moved by then (`moves`, LogController.clock_moved), so a
    reading written after a move can be put on the moved clock. Any other target takes it as the text it is."""

    def __new__(cls, text, when, moves):
        stamp = super().__new__(cls, text)
        stamp.when = when
        stamp.moves = moves
        return stamp


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
        # How far CraftBeerPi's clock was moved, in seconds, each time since the server started (clock_moved).
        self.clock_moves = []

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
        not a sensor, so every write would be dropped without a word.

        This takes the name instead of looking it up. Everything downstream is
        the same: the same listeners, the same millisecond timestamps, the
        same batching, rotation and retention, and the same chart endpoint.
        """
        if not self.sensor_data_listeners:
            return
        try:
            self._emit(series_id, value, (name or series_id).replace(" ", "_"))
        except Exception as e:  # noqa: BLE001 - telemetry never breaks control
            logging.error("control logging listener exception: {}".format(e))

    def log_change(self, series_id: str, value) -> None:
        """Log a value only when it differs from the last one logged for its series.

        For what the brew-day screen's graphs draw besides the temperature:
        each kettle's target and whether its AUTO
        is on, and each switch's level, 0 when off. They change a few times a
        brew day, or every control cycle at most while a PID moves a level, so
        writing them only on a change costs less than any temperature log.
        Without them a graph could show the target and the element's power
        only for as long as the page had been open: PID_LOGGING is off by
        default, BoilPower logs nothing, and nothing is logged by hand.
        """
        try:
            last = getattr(self, "_last_change", None)
            if last is None:
                last = self._last_change = {}
            if series_id in last and last[series_id] == value:
                return
            if not self.sensor_data_listeners:
                return  # nothing to write to yet: the next change is logged
            last[series_id] = value
            self.log_control_data(series_id, value)
        except Exception as e:  # noqa: BLE001 - telemetry never breaks control
            logging.error("change logging exception: {}".format(e))

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
        an accelerated run would squeeze its x-axis by the time scale - a
        simulated hour of mash charted across one real minute. `timestamp`,
        in the same clock, is when the reading was taken, if that was not now.

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
        formatted_time = LogStamp(datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], when,
                                  len(self.clock_moves))
        asyncio.create_task(
            self._call_sensor_data_listeners(id, value, formatted_time, name)
        )

    @staticmethod
    def stamp(when):
        """A time on the brewing clock as the logs write it: "2026-11-24 22:10:00.123" (_emit)."""
        return datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    #: Seconds: a move of the clock shorter than this leaves the logs as they are. A change of speed while the clock
    #: is where the computer's is, at 1x, moves it by a few milliseconds.
    CLOCK_MOVE_MIN_S = 1.0

    def clock_moved(self, seconds):
        """CraftBeerPi's clock has just jumped by `seconds` (CraftBeerPi.brewing_clock: a simulator's change of speed,
        back to the computer's time). Each sensor log gets a row saying so, after the rows written so far, and
        whatever reads the logs moves those rows by as much (cbpi.api.logclock): the graphs keep the readings already
        taken, where they were in the brew, and the readings from now on carry on from them as one line. A reading
        taken before the jump and written after it is moved as it is written (SensorLogTarget_CSV). The files keep
        every row as written; nothing is moved or deleted.

        Called with nothing else running between the jump and here, so no reading on the moved clock is written yet.
        Returns the names of the logs marked."""
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            return []
        if not math.isfinite(seconds) or abs(seconds) < self.CLOCK_MOVE_MIN_S:
            return []
        self.clock_moves.append(seconds)
        row = logclock.moved_row(self.stamp(sim_clock.now()), seconds)
        folder = self.logsFolderPath
        names = set(self.datalogger)
        if folder and os.path.isdir(folder):
            for path in glob.glob(os.path.join(glob.escape(folder), "sensor_*.log*")):
                found = re.fullmatch(r"sensor_(.+)\.log(\.\d+)?", os.path.basename(path))
                if found:
                    names.add(found.group(1))
        marked = []
        for name in sorted(names):
            try:
                if self._mark_clock_moved(name, row):
                    marked.append(name)
            except Exception as e:  # noqa: BLE001 - one log never keeps the others from being marked
                self.logger.warning("Could not mark the clock's move in the log %s: %s", name, e)
        return marked

    def _mark_clock_moved(self, name, row):
        """The clock's move `row` written to `name`'s log, after its rows so far: through its open handler, which
        writes what it holds first, or else at the end of its newest file. Whether it was written."""
        data_logger = self.datalogger.get(name)
        handlers = list(data_logger.handlers) if data_logger is not None else []
        told = [h for h in handlers if callable(getattr(h, "clock_moved", None))]
        if told:
            for handler in told:
                handler.clock_moved(row, len(self.clock_moves))
            return True
        for handler in handlers:
            handler.flush()
        path = os.path.join(self.logsFolderPath, "sensor_{}.log".format(name))
        if not os.path.exists(path) and not self._log_files_oldest_first(name):
            return False
        with open(path, "a", encoding="utf-8") as f:
            f.write(row + "\n")
        return True

    def restamped(self, line, when, since, upto=None):
        """A log row of a reading taken at `when`, after `since` moves of the clock, put on the clock as it was after
        `upto` moves (all of them, by default): "time,value" with its time moved by the moves between."""
        try:
            moved = sum(self.clock_moves[int(since):None if upto is None else int(upto)])
            if not moved:
                return line
            return "{},{}".format(self.stamp(float(when) + moved), line.split(",", 1)[1])
        except Exception:  # noqa: BLE001 - a row is never lost over its time
            return line

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

        A fixed 60s would quietly decide that one minute of wall-clock is the
        finest thing worth seeing. That is reasonable for a six hour brew day
        and wrong for everything shorter.

        Rows carry simulated time (_emit), so a simulated hour spans an hour;
        the span decides the bucket, because the bucket is reduced with
        .max(), and a bucket too coarse for its span makes the dip when grain
        goes in not merely coarse but invisible. A chart that cannot show a
        dough-in dip or a stalled ramp is not worth reading, and those are the
        two things it is read for.

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
        # them away.
        # Floored at 100ms, which is finer than anything worth charting.
        seconds = span / self.TARGET_POINTS
        if seconds >= 1:
            return "{}s".format(int(seconds))
        return "{}ms".format(max(100, int(seconds * 1000)))

    # A step back in a log's timestamps bigger than this is the clock being
    # changed, not two readings racing each other (cbpi.api.logclock).
    CLOCK_BACK_TOLERANCE_S = logclock.BACK_TOLERANCE_S

    # How long a clock that went back may run before it is put right, measured
    # on that clock. After a power cut, NTP sets the rig's clock about two
    # minutes after it boots.
    CLOCK_FIX_WITHIN_S = logclock.FIX_WITHIN_S

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

        Survives what a power cut leaves in a file. NUL bytes where the file
        had grown but the data never reached the card - often hundreds, in
        front of the first row written after the restart - are removed, so
        that row's time can be read.
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
        if logclock.MOVED_BYTES in raw:
            # A row saying CraftBeerPi's clock moved there (clock_moved): how far, for _read_sensor_log.
            df[MOVED_COLUMN] = pd.to_numeric(df[column].map(logclock.moved_by), errors="coerce")
        df[column] = pd.to_numeric(df[column], errors="coerce")
        return df

    @staticmethod
    def _on_clock_now(df):
        """A log's rows, in the order written, each moved by the clock's moves written after it (clock_moved), so
        every row is on the clock as it is now; the rows saying the clock moved left out."""
        moves = df.pop(MOVED_COLUMN).to_numpy(dtype=float)
        shift = logclock.moved_after(moves)
        moved = df["DateTime"].notna().to_numpy() & (shift != 0)
        if moved.any():
            written = df.loc[moved, "DateTime"].to_numpy(dtype="datetime64[ns]").astype("int64") / 1e9
            now = np.round(logclock.shift_naive(written, shift[moved]) * 1000).astype("int64") * 1_000_000
            df.loc[moved, "DateTime"] = now.astype("datetime64[ns]")
        return df[np.isnan(moves)]

    def _misstamped(self, times):
        """Which rows were stamped by a clock that went back and was then put right.

        `times` are the rows' DateTimes in the order they were written. Returns
        a boolean array, True for the rows to leave out.

        A Raspberry Pi has no clock that runs without power. After a power cut
        it boots with the time it last saved, the server starts and logs, and a
        minute or two later NTP sets the real time. The log then runs, say,
        10:02:34, then 10:01:01 ... 10:03:09 for 129 rows, then 11:42:14:
        readings taken at about 11:40-11:42 are stamped 10:01-10:03, and a
        chart would draw them two hours early, among the older readings.

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

        The rule is cbpi.api.logclock's, which Figures reads the logs by too.
        """
        if len(times) < 2:
            return np.zeros(len(times), dtype=bool)
        s = (pd.to_datetime(times).to_numpy(dtype="datetime64[ns]")
             .astype("int64") / 1e9)
        return logclock.misstamped(s, self.CLOCK_BACK_TOLERANCE_S, self.CLOCK_FIX_WITHIN_S)

    def _read_sensor_log(self, name, column):
        """Every reading in a sensor's logs, on the clock as it is now (_on_clock_now), indexed by time and sorted,
        with the rows a power cut misstamped left out (see _misstamped)."""
        frames = [self._read_log_file(path, column)
                  for path in self._log_files_oldest_first(name)]
        frames = [f for f in frames if not f.empty]
        if not frames:
            return pd.DataFrame({column: pd.Series(dtype="float64")},
                                index=pd.DatetimeIndex([], name="DateTime"))
        df = pd.concat(frames, ignore_index=True)
        if MOVED_COLUMN in df:
            df = self._on_clock_now(df)
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

    # Points per series a graph asks for when it does not say, and the most it
    # may ask for. The brew-day screen's graphs are a few hundred pixels wide.
    WINDOW_POINTS = 600
    WINDOW_POINTS_MAX = 2000

    async def get_window(self, names, since=None, points=None) -> dict:
        """Each named log from `since` on, thinned to about `points` readings, with its last reading before `since`.

        For the brew-day screen's temperature graphs: the last 10 or 30
        minutes, or the brew day. get_data() reads a
        whole log and thins it to about 500 points, a point every 43 s over six
        hours: too coarse for a 10-minute graph, and mostly older than the graph
        shows. This keeps only what is newer than `since`, and thins each series
        the way get_data() does, by the highest reading in each slice of time.

        Times are seconds on the brewing clock (cbpi.api.clock), the clock the
        logs are stamped in (_emit): on the rig, the wall clock; in a simulation
        it runs SIM_TIME_SCALE times as fast. `now` and `scale` say where that
        clock is, so a page can line up the readings it is sent live with the
        log. A name with no log comes back empty, never as an error.
        """
        if isinstance(names, str):
            names = [names]
        names = list(dict.fromkeys(n for n in (names or []) if isinstance(n, str) and n))
        try:
            points = int(points) if points is not None else self.WINDOW_POINTS
        except (TypeError, ValueError):
            points = self.WINDOW_POINTS
        points = max(10, min(self.WINDOW_POINTS_MAX, points))
        try:
            since = float(since) if since is not None else None
        except (TypeError, ValueError):
            since = None
        if since is not None and not math.isfinite(since):
            since = None
        # As _emit wrote it: the brewing clock's seconds as local time.
        start = datetime.fromtimestamp(since) if since is not None else None

        series = {}
        for name in names:
            # Buffered readings are not on disk yet, and this reads the files.
            await self._flush_datalogger(name)
            try:
                s = self._read_sensor_log(name, "v")["v"].dropna()
            except Exception as e:  # noqa: BLE001 - one bad log never blanks the others
                self.logger.warning("Could not read the log %s for a graph: %s", name, e)
                s = pd.Series(dtype="float64", index=pd.DatetimeIndex([]))
            earlier = s.iloc[0:0]
            if start is not None:
                earlier = s[s.index <= start]
                s = s[s.index > start]
            s = self._thin(s, points)
            # A log written only when its value changes - a kettle's target or AUTO, a switch's level - has nothing
            # in the window until its next change, so its last reading before `since` comes too, at its own time:
            # where the window's line starts. Added after the thinning, which it would otherwise stretch.
            if len(earlier):
                s = pd.concat([earlier.iloc[-1:], s])
            series[name] = {
                # A naive datetime's timestamp() reads it as local time, as
                # fromtimestamp() wrote it, daylight saving and all.
                "t": [round(t.to_pydatetime().timestamp(), 3) for t in s.index],
                "v": [round(float(v), 3) for v in s.tolist()],
            }
        return {"now": sim_clock.now(), "scale": sim_clock.scale(), "series": series}

    @staticmethod
    def _thin(s, points):
        """At most about `points` readings: the highest in each slice of time,
        and always the latest reading, so a line reaches the present."""
        if len(s) <= points:
            return s
        span = (s.index[-1] - s.index[0]).total_seconds()
        if span <= 0:
            return s.iloc[-points:]
        slice_ms = max(1, int(span * 1000 / points))
        # Slices from the first reading, so there are `points` of them, give or take one.
        thinned = s.resample("{}ms".format(slice_ms), origin="start").max().dropna()
        if len(thinned) and thinned.index[-1] < s.index[-1]:
            thinned = pd.concat([thinned.iloc[:-1], s.iloc[-1:]])
        return thinned

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
            self._close_datalogger(name)

        for f in all_filenames:
            try:
                os.remove(f)
            except Exception as e:
                logging.warning(e)

    def _close_datalogger(self, name):
        """Close every file handler of a sensor's log, its batch written first, and forget the logger, so its files
        can be removed or moved. The next reading opens the log afresh (SensorLogTarget_CSV)."""
        data_logger = self.datalogger.pop(name, None)
        if data_logger is None:
            return
        # Close, not just remove. removeHandler() detaches the handler from
        # the logger but leaves its file open, and on Windows an open handle
        # makes both the rotation rename and a remove or move of the file fail.
        #
        # Every handler, not handlers[0]. A logger is a process-global
        # singleton, so a previous leak can have left more than one attached
        # to the same file, and taking only the first leaves the rest
        # holding it open.
        for handler in list(data_logger.handlers):
            try:
                data_logger.removeHandler(handler)
                handler.close()
            except Exception as e:
                logging.warning("Could not close log handler for %s: %s", name, e)

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
