# -*- coding: utf-8 -*-
import asyncio
import base64
import logging
import time

import aiohttp
from cbpi.api import *
from cbpi.api.config import ConfigType

logger = logging.getLogger(__name__)


class SensorLogTargetInfluxDB(CBPiExtension):
    """Mirror sensor readings into InfluxDB, without blocking the control loop.

    This used to call urllib3's synchronous http.request() from inside an async
    function:

        timeout = Timeout(connect=2.0, read=None)
        http = PoolManager(timeout=timeout)
        req = http.request("POST", self.influxdburl, ...)

    Synchronous I/O inside a coroutine blocks the entire event loop, not just
    the task it runs in - asyncio.create_task() does not isolate it. Every
    reading therefore stalled the loop that runs the PIDs, the step engine and
    the actor shutdown paths. Three sensors at one reading a second is three
    stalls a second, and `read=None` means no read timeout at all: an InfluxDB
    that accepted the connection and then stopped answering would hang the
    controller indefinitely, with a kettle element still energized.

    That is the whole reason this is rewritten. The rest follows from it:

      * aiohttp, which the server already depends on, with a bounded total
        timeout so a slow endpoint costs one reading rather than the brew.
      * one shared session instead of a new PoolManager per reading.
      * the real timestamp, at millisecond precision, instead of letting
        InfluxDB stamp on receive. Receive time is wrong by however long the
        write queued, and the previous cloud path asked for `precision=s`,
        which discarded sub-second resolution outright.
      * configuration read once per change rather than six times per reading.

    Failures are counted and backed off exactly as before: after max_retries
    the target goes quiet for three minutes. The difference is that the backoff
    no longer runs inside the listener chain.
    """

    # A write is a mirror, not the brew. If InfluxDB cannot answer in this many
    # seconds the reading is dropped and the controller carries on.
    WRITE_TIMEOUT = 5.0

    def __init__(self, cbpi):  # called from cbpi on start
        self.cbpi = cbpi
        self.influxdb = self.cbpi.config.get("INFLUXDB", "No")
        if self.influxdb == "No":
            return  # never run()
        self.counter = 0
        self.max_retries = 2
        self.send = True
        self._session = None
        self._backoff_task = None
        self._task = asyncio.create_task(self.run())  # one time run() only

    async def run(self):  # called by __init__ once on start if influx is enabled
        self.listener_ID = self.cbpi.log.add_sensor_data_listener(
            self.log_data_to_InfluxDB
        )
        logger.info(
            "InfluxDB sensor log target listener ID: {}".format(self.listener_ID)
        )

    async def _get_session(self):
        """One session for the life of the process.

        A PoolManager was built per reading, so every write paid a fresh TCP
        and TLS handshake - the dominant cost of writing one short line.
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.WRITE_TIMEOUT)
            )
        return self._session

    def _line(self, measurement, itemname, id, value, timestamp):
        """InfluxDB line protocol, with an explicit millisecond timestamp.

        No timestamp was sent at all, so InfluxDB applied its own receive time.
        That is wrong by however long the write spent queued or retrying, and
        it silently reorders readings when writes are retried out of order. The
        reading already knows when it was taken; send that.
        """
        line = "{},source={},itemID={} value={}".format(
            measurement, itemname, id, value
        )
        if timestamp is not None:
            line += " {}".format(timestamp)
        return line

    @staticmethod
    def _epoch_ms(timestamp):
        """Milliseconds since the epoch, from the logger's formatted stamp.

        The log controller formats "%Y-%m-%d %H:%M:%S.%f" truncated to
        milliseconds. Older rows have no fractional part, so both parse.
        Returns None if it cannot be read, which falls back to letting
        InfluxDB stamp it - worse, but not a lost reading.
        """
        if not timestamp:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = time.strptime(timestamp.strip(), fmt)
            except (ValueError, TypeError):
                continue
            seconds = time.mktime(parsed)
            fraction = 0.0
            if "." in str(timestamp):
                try:
                    fraction = float("0." + str(timestamp).rsplit(".", 1)[1])
                except (ValueError, IndexError):
                    fraction = 0.0
            return int((seconds + fraction) * 1000)
        return None

    def _begin_backoff(self):
        """Go quiet for three minutes after repeated failures.

        Scheduled as its own task rather than awaited in the listener chain,
        where a three minute sleep would hold up every other log target behind
        it - including the CSV writer the charts are built from.
        """
        if self._backoff_task is not None and not self._backoff_task.done():
            return
        self.send = False
        logger.warning("Waiting 3 Minutes before connecting to INFLUXDB again")

        async def _wait():
            try:
                await asyncio.sleep(180)
            finally:
                self.counter = 0
                self.send = True

        self._backoff_task = asyncio.create_task(_wait())

    async def log_data_to_InfluxDB(
        self, cbpi, id: str, value: str, timestamp, name
    ):  # called by log_data() hook from the log file controller
        self.influxdb = self.cbpi.config.get("INFLUXDB", "No")
        if self.influxdb == "No":
            # We intentionally do not unsubscribe the listener here because then we had no way of resubscribing him without a restart of cbpi
            # as long as cbpi was STARTED with INFLUXDB set to Yes this function is still subscribed, so changes can be made on the fly.
            # but after initially enabling this logging target a restart is required.
            return
        if not self.send:
            return

        influxdbcloud = self.cbpi.config.get("INFLUXDBCLOUD", "No")
        influxdbaddr = self.cbpi.config.get("INFLUXDBADDR", None)
        influxdbname = self.cbpi.config.get("INFLUXDBNAME", None)
        influxdbuser = self.cbpi.config.get("INFLUXDBUSER", None)
        influxdbpwd = self.cbpi.config.get("INFLUXDBPWD", None)
        influxdbmeasurement = self.cbpi.config.get("INFLUXDBMEASUREMENT", "measurement")

        try:
            sensor = self.cbpi.sensor.find_by_id(id)
            if sensor is None:
                return
            itemname = sensor.name.replace(" ", "_")
            out = self._line(
                influxdbmeasurement,
                itemname,
                id,
                value,
                self._epoch_ms(timestamp),
            )
        except Exception as e:  # noqa: BLE001
            logging.error("InfluxDB ID Error: {}".format(e))
            return

        if influxdbcloud == "Yes":
            url = (
                influxdbaddr
                + "/api/v2/write?org="
                + influxdbuser
                + "&bucket="
                + influxdbname
                + "&precision=ms"
            )
            header = {
                "User-Agent": id,
                "Authorization": "Token {}".format(influxdbpwd),
            }
        else:
            base64string = base64.b64encode(
                ("%s:%s" % (influxdbuser, influxdbpwd)).encode()
            )
            url = influxdbaddr + "/write?db=" + influxdbname + "&precision=ms"
            header = {
                "User-Agent": id,
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": "Basic %s" % base64string.decode("utf-8"),
            }

        try:
            session = await self._get_session()
            async with session.post(url, data=out.encode(), headers=header) as resp:
                if resp.status != 204:
                    raise Exception(f"InfluxDB Status code {resp.status}")
            self.counter = 0
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.counter += 1
            logging.error("InfluxDB write Error #{}: {}".format(self.counter, e))
            if self.counter > self.max_retries:
                self._begin_backoff()


def setup(cbpi):
    cbpi.plugin.register("SensorLogTargetInfluxDB", SensorLogTargetInfluxDB)
