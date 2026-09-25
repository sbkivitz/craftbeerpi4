# -*- coding: utf-8 -*-
import asyncio
import base64
import logging
import os
try:
    import pwd
    module_pwd = True
except:
    module_pwd = False
import shutil
import random
import time
from logging.handlers import RotatingFileHandler
from unittest.mock import MagicMock, patch

import urllib3
from cbpi.api import *
from cbpi.api.config import ConfigType

logger = logging.getLogger(__name__)


class BatchingRotatingFileHandler(RotatingFileHandler):
    """A rotating log that writes in batches instead of once per reading.

    Sensor readings arrive about once a second per sensor. The stock handler
    writes and flushes each one, so three sensors produce roughly ten thousand
    tiny writes an hour - around fifty thousand over a brew day, each about
    twenty-five bytes.

    On an SD card the cost of a write is not the bytes, it is the erase block.
    A twenty-five byte append can force a page program and, eventually, an
    erase of a block tens or hundreds of times larger. That write amplification
    is what wears out cards in controllers that otherwise sit idle, and losing
    the card mid-brew loses the rig.

    Batching a minute of readings into one write removes about 98% of those
    operations without losing a single sample from the chart. The cost is
    bounded and stated: up to `flush_seconds` of readings may be lost if power
    is cut, which is acceptable for a chart and is why nothing that matters for
    safety or recovery is written through here.

    The batch is also written in one call, which matters more than SD wear once
    the logs live on a network share: see the executor note in log_data_to_CSV.
    """

    def __init__(self, *args, flush_seconds=60, max_records=600, **kwargs):
        super().__init__(*args, **kwargs)
        self._pending = []
        self._last_flush = time.monotonic()
        self.flush_seconds = max(0.0, float(flush_seconds))
        self.max_records = max(1, int(max_records))

    def emit(self, record):
        try:
            self._pending.append(self.format(record))
        except Exception:  # noqa: BLE001 - never let logging break a brew day
            self.handleError(record)
            return
        due = time.monotonic() - self._last_flush >= self.flush_seconds
        if self.flush_seconds <= 0 or due or len(self._pending) >= self.max_records:
            self.flush()

    def flush(self):
        """Write everything buffered as a single append."""
        self.acquire()
        try:
            if not self._pending:
                super().flush()
                return
            batch, self._pending = self._pending, []
            self._last_flush = time.monotonic()
            payload = "".join(line + self.terminator for line in batch)
            try:
                if self.stream is None:
                    self.stream = self._open()
                # Roll over on the whole batch rather than per line, so one
                # flush is never split across two files. This is what
                # shouldRollover does, done directly because that method sizes
                # a LogRecord and there is no record here, only text.
                if self.maxBytes > 0:
                    self.stream.seek(0, 2)
                    if self.stream.tell() + len(payload) >= self.maxBytes:
                        self.doRollover()
                        if self.stream is None:
                            self.stream = self._open()
                self.stream.write(payload)
                super().flush()
            except Exception:  # noqa: BLE001
                # A failed write must not take the controller with it, and must
                # not spin: the batch is already dropped.
                logger.warning("Could not write sensor log batch", exc_info=True)
        finally:
            self.release()

    def close(self):
        try:
            self.flush()
        finally:
            super().close()


class SensorLogTargetCSV(CBPiExtension):

    def __init__(self, cbpi):  # called from cbpi on start
        self.cbpi = cbpi
        self.logfiles = self.cbpi.config.get("CSVLOGFILES", "Yes")
        if self.logfiles == "No":
            return  # never run()
        self._task = asyncio.create_task(self.run())  # one time run() only

    async def run(self):  # called by __init__ once on start if CSV is enabled
        self.listener_ID = self.cbpi.log.add_sensor_data_listener(self.log_data_to_CSV)
        logger.info("CSV sensor log target listener ID: {}".format(self.listener_ID))

    async def log_data_to_CSV(
        self, cbpi, id: str, value: str, formatted_time, name
    ):  # called by log_data() hook from the log file controller
        self.logfiles = self.cbpi.config.get("CSVLOGFILES", "Yes")
        if self.logfiles == "No":
            # We intentionally do not unsubscribe the listener here because then we had no way of resubscribing him without a restart of cbpi
            # as long as cbpi was STARTED with CSVLOGFILES set to Yes this function is still subscribed, so changes can be made on the fly.
            # but after initially enabling this logging target a restart is required.
            return
        if id not in self.cbpi.log.datalogger:
            max_bytes = int(self.cbpi.config.get("SENSOR_LOG_MAX_BYTES", 100000))
            backup_count = int(self.cbpi.config.get("SENSOR_LOG_BACKUP_COUNT", 3))
            # How long a reading may sit in memory before it reaches the disk.
            # Configurable rather than fixed because the right answer depends
            # on the storage: 60 s suits an SD card, 0 restores the original
            # write-every-reading behaviour for anyone who wants it.
            flush_seconds = float(self.cbpi.config.get("SENSOR_LOG_FLUSH_SECONDS", 60))

            data_logger = logging.getLogger("cbpi.sensor.%s" % id)
            data_logger.propagate = False
            data_logger.setLevel(logging.DEBUG)
            try:
                handler = BatchingRotatingFileHandler(
                    os.path.join(self.cbpi.log.logsFolderPath, f"sensor_{id}.log"),
                    maxBytes=max_bytes,
                    backupCount=backup_count,
                    flush_seconds=flush_seconds,
                )
            except Exception as e:
                logger.error("Error creating log file handler: %s", e)
                try:
                    logger.warning(
                        "Trying to set rights for cbpi user on the log folder and file"
                        )
                    user = pwd.getpwuid(os.getuid()).pw_name
                    file= os.path.join(self.cbpi.log.logsFolderPath, f"sensor_{id}.log")
                    shutil.os.system(f'sudo chown {user}:{user} {file}')

                    handler = BatchingRotatingFileHandler(
                        os.path.join(self.cbpi.log.logsFolderPath, f"sensor_{id}.log"),
                        maxBytes=max_bytes,
                        backupCount=backup_count,
                        flush_seconds=flush_seconds,
                    )
                except Exception as e:
                    logger.error("Error creating log file handler after trying to set rights: %s", e)
                    return

            # Exactly one handler per sensor logger.
            #
            # logging.getLogger() returns a process-global singleton, and this
            # block is re-entered whenever the datalogger dict has lost the id -
            # after clear_log, or a sensor edit. Each pass used to attach ANOTHER
            # RotatingFileHandler to the same file, so the file ended up with
            # several open handles. On Windows the rollover then fails outright
            # (os.rename cannot move a file another handle holds) and sensor
            # logging stops; on Linux it survives but writes every reading two
            # or three times. Either way the chart is wrong, which is how this
            # was noticed.
            for existing in list(data_logger.handlers):
                try:
                    data_logger.removeHandler(existing)
                    existing.close()
                except Exception as e:
                    logger.warning("Could not close old log handler: %s", e)

            data_logger.addHandler(handler)
            self.cbpi.log.datalogger[id] = data_logger

        # Hand the write to a worker thread.
        #
        # This coroutine runs on the same event loop as every PID loop and
        # every actor command, and logging is a blocking filesystem call. On an
        # SD card that is microseconds. On a network share - which is where
        # these logs want to live, to spare the card - an unreachable server
        # blocks the write until the mount times out, and that stalls the loop
        # that is supposed to be modulating a five kilowatt element and the one
        # that would switch it off.
        #
        # Buffering makes this rare; running it off the loop makes it harmless.
        entry = "%s,%s" % (formatted_time, str(value))
        target = self.cbpi.log.datalogger[id]
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, target.info, entry)
        except RuntimeError:
            target.info(entry)


def setup(cbpi):
    cbpi.plugin.register("SensorLogTargetCSV", SensorLogTargetCSV)
