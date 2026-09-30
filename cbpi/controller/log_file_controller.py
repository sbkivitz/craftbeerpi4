import asyncio
import base64
import datetime
import glob
import logging
import os
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import localtime, strftime

import pandas as pd
import shortuuid
from cbpi.api import *
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

    def log_data(self, id: str, value: str) -> None:
        # all plugin targets:
        if self.sensor_data_listeners:  # true if there are listners
            try:
                sensor = self.cbpi.sensor.find_by_id(id)
                if sensor is not None:
                    name = sensor.name.replace(" ", "_")
                    formatted_time = strftime("%Y-%m-%d %H:%M:%S", localtime())
                    asyncio.create_task(
                        self._call_sensor_data_listeners(
                            id, value, formatted_time, name
                        )
                    )
            except Exception as e:
                logging.error("sensor logging listener exception: {}".format(e))

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

        It is badly wrong for an accelerated simulation. Log rows are stamped
        with real time, so at SIM_TIME_SCALE=60 an hour-long mash happens in
        one real minute and lands in a single 60s bucket - the whole step
        becomes one point. Worse, the bucket is reduced with .max(), so the
        dip when grain goes in is not merely coarse, it is invisible: the peak
        wins. A chart that cannot show a dough-in dip or a stalled ramp is not
        worth reading, and those are the two things it is read for.

        Deriving the rate from the span fixes both, and needs no plugin, no
        configuration and no knowledge of the time scale here.
        """
        try:
            if len(index) < 2:
                return None
            span = (index.max() - index.min()).total_seconds()
        except Exception:  # noqa: BLE001 - charting must not fail on odd data
            return "60s"
        if span <= 0:
            return None
        # One bucket per target point, never finer than a second - the logger
        # writes at most one row a second, so anything finer is empty buckets.
        seconds = max(1, int(span / self.TARGET_POINTS))
        return "{}s".format(seconds)

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
            # get all log names
            all_filenames = glob.glob(
                os.path.join(self.logsFolderPath, f"sensor_{name}.log*")
            )
            # concat all logs
            df = pd.concat(
                [
                    pd.read_csv(
                        f, parse_dates=True, names=["DateTime", name], header=None
                    )
                    for f in all_filenames
                ]
            )
            logging.info("Read all files for {}".format(names))
            df["DateTime"] = pd.to_datetime(df["DateTime"], format=timestamp_format)
            df.set_index("DateTime", inplace=True)

            # resample if rate provided
            rate = sample_rate
            if rate is None:
                rate = self._sample_rate_for(df.index)
            if rate is not None:
                df = df[name].resample(rate).max()
            logging.info("Sampled now for {}".format(names))
            df = df.dropna()
            # take every nth row so that total number of rows does not exceed max_rows * 2
            max_rows = 500
            total_rows = df.shape[0]
            if (total_rows > 0) and (total_rows > max_rows):
                nth = int(total_rows / max_rows)
                if nth > 1:
                    df = df.iloc[::nth]

            if result is None:
                result = df
            else:
                result = pd.merge(
                    result, df, how="outer", left_index=True, right_index=True
                )

        data = {"time": df.index.tolist()}

        if len(names) > 1:
            for name in names:
                data[name] = (
                    result[name].interpolate(limit_direction="both", limit=10).tolist()
                )
        else:
            data[name] = result.interpolate().tolist()

        logging.info("Send Log for {}".format(names))

        return data

    async def get_data2(self, ids) -> dict:
        timestamp_format = "%Y-%m-%d %H:%M:%S"
        result = dict()
        for id in ids:
            try:
                all_filenames = glob.glob(
                    os.path.join(self.logsFolderPath, f"sensor_{id}.log*")
                )
                df = pd.concat(
                    [
                        pd.read_csv(
                            f,
                            parse_dates=["DateTime"],
                            names=["DateTime", "Values"],
                            header=None,
                        )
                        for f in all_filenames
                    ]
                )
                df["DateTime"] = pd.to_datetime(df["DateTime"], format=timestamp_format)
                df.set_index("DateTime", inplace=True)
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
