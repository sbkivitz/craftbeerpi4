# -*- coding: utf-8 -*-
import asyncio
import logging
import os
import threading
import time
from subprocess import call

from aiohttp import web
from cbpi.api import *
from cbpi.api.dataclasses import NotificationAction, NotificationType


def getSensors():
    try:
        arr = []
        for dirname in os.listdir("/sys/bus/w1/devices"):
            if dirname.startswith("28") or dirname.startswith("10"):
                arr.append(dirname)
        return arr
    except:
        return []


class ReadThread(threading.Thread):

    value = 0

    def __init__(self, sensor_name):
        threading.Thread.__init__(self)
        self.value = 0
        self.sensor_name = sensor_name
        self.runnig = True
        # When the probe last produced a reading that passed its CRC check. None
        # until the first one. Distinct from "when we last published a number":
        # this loop keeps its previous value when a read fails, so without this
        # there is nothing to tell a live probe from a disconnected one.
        self.last_success = None
        self.failing = False

    def shutdown(self):
        pass

    def stop(self):
        self.runnig = False

    def run(self):

        while self.runnig:
            try:
                if self.sensor_name is None:
                    return
                with open(
                    "/sys/bus/w1/devices/%s/w1_slave" % self.sensor_name, "r"
                ) as content_file:
                    content = content_file.read()
                    if content.split("\n")[0].split(" ")[11] == "YES":
                        temp = float(content.split("=")[-1]) / 1000  # temp in Celcius
                        self.value = temp
                        self.last_success = time.time()
                        if self.failing:
                            self.failing = False
                            logging.info(
                                "OneWire %s: reads recovered", self.sensor_name
                            )
                    else:
                        # CRC failed. Keeping the previous value is fine for a single
                        # bad sample, but last_success is deliberately not advanced,
                        # so a probe that only ever returns bad CRCs stops looking
                        # healthy instead of reporting its last good reading forever.
                        self._note_failure("CRC check failed")
            except Exception as e:
                # Was a bare `except: pass`. The failure itself is expected during a
                # brief bus glitch, but swallowing it silently meant a permanently
                # disconnected probe was indistinguishable from a working one.
                self._note_failure(e)

            time.sleep(1)

    def _note_failure(self, reason):
        """Log the first failure in a run of them, then stay quiet.

        A disconnected probe fails every second; logging each one would bury the
        rest of the log on a long brew.
        """
        if not self.failing:
            self.failing = True
            logging.warning(
                "OneWire %s: read failed (%s). Further failures will not be logged "
                "until it recovers.",
                self.sensor_name,
                reason,
            )


@parameters(
    [
        Property.Select(label="Sensor", options=getSensors()),
        Property.Number(
            label="offset",
            configurable=True,
            default_value=0,
            description="Sensor Offset (Default is 0)",
        ),
        Property.Select(
            label="Interval",
            options=[1, 5, 10, 30, 60],
            default_value=1,
            description="Interval in Seconds",
        ),
        Property.Kettle(
            label="Kettle",
            description="Reduced logging if Kettle is inactive / range warning in dashboard(only Kettle or Fermenter to be selected)",
        ),
        Property.Fermenter(
            label="Fermenter",
            description="Reduced logging in seconds if Fermenter is inactive / range warning in dashboard (only Kettle or Fermenter to be selected)",
        ),
        Property.Number(
            label="ReducedLogging",
            configurable=True,
            default_value=60,
            description="Reduced logging frequency in seconds if selected Kettle or Fermenter is inactive (default: 60 sec | disabled: 0)",
        ),
        Property.Number(
            label="TempRange",
            configurable=True,
            default_value=0,
            unit="degree",
            description="Temp range in degree between reading and target temp of fermenter/kettle. Larger difference shows different color in dashboard (default:0 | deactivated: 0)",
        ),
    ]
)
class OneWire(CBPiSensor):

    def __init__(self, cbpi, id, props):
        super(OneWire, self).__init__(cbpi, id, props)
        # Nothing has been read from the bus yet. This used to be 200, which is
        # not a temperature this probe can produce and not one any consumer
        # should act on. A placeholder that looks like a plausible reading is
        # worse than no reading, because nothing downstream can tell them
        # apart.
        self.value = None

    async def start(self):
        await super().start()
        self.name = self.props.get("Sensor")
        self.interval = int(self.props.get("Interval", 60))
        self.offset = float(self.props.get("offset", 0))

        self.reducedfrequency = float(self.props.get("ReducedLogging", 60))
        if self.reducedfrequency < 0:
            self.reducedfrequency = 0
        self.lastlog = 0
        self.temprange = float(self.props.get("TempRange", 0))
        self.sensor = self.get_sensor(self.id)
        self.kettleid = self.props.get("Kettle", None)
        self.fermenterid = self.props.get("Fermenter", None)
        self.reducedlogging = True if self.kettleid or self.fermenterid else False

        if self.kettleid is not None and self.fermenterid is not None:
            self.reducedlogging = False
            self.cbpi.notify(
                "OneWire Sensor",
                "Sensor '"
                + str(self.sensor.name)
                + "' cant't have Fermenter and Kettle defined for reduced logging / range warning.",
                NotificationType.WARNING,
                action=[NotificationAction("OK", self.Confirm)],
            )
        if (self.reducedfrequency != 0) and (self.interval >= self.reducedfrequency):
            self.reducedlogging = False
            self.cbpi.notify(
                "OneWire Sensor",
                "Sensor '"
                + str(self.sensor.name)
                + "' has shorter or equal 'reduced logging' compared to regular interval.",
                NotificationType.WARNING,
                action=[NotificationAction("OK", self.Confirm)],
            )

        self.t = ReadThread(self.name)
        self.t.daemon = True

        def shutdown():
            shutdown.cb.shutdown()

        shutdown.cb = self.t
        self.t.start()

    async def Confirm(self, **kwargs):
        pass

    # A probe is polled once a second by the reader thread. Allowing several
    # missed reads rides out a bus glitch without declaring a healthy sensor dead.
    MAX_READ_AGE = 15

    def _reads_are_current(self):
        """True when the probe has recently produced a CRC-valid reading.

        Falls back to True when the reader thread predates this check or exposes
        no last_success, so a subclass or older thread keeps publishing rather
        than going silent - failing towards the previous behaviour, not towards
        a sensor that never reports.
        """
        last_success = getattr(self.t, "last_success", "missing")
        if last_success == "missing":
            return True
        if last_success is None:
            # Thread started but has never managed a good read.
            return False
        age = time.time() - last_success
        if age > max(self.MAX_READ_AGE, self.interval * 3):
            if not getattr(self, "_stale_logged", False):
                self._stale_logged = True
                logging.warning(
                    "OneWire %s: no valid reading for %.0fs - no longer publishing, "
                    "so consumers can see it go stale",
                    self.sensor.name if self.sensor else self.id,
                    age,
                )
            return False
        if getattr(self, "_stale_logged", False):
            self._stale_logged = False
            logging.info(
                "OneWire %s: publishing again",
                self.sensor.name if self.sensor else self.id,
            )
        return True

    async def stop(self):
        try:
            self.t.stop()
            self.running = False
        except:
            pass

    async def run(self):

        self.kettle = (
            self.get_kettle(self.kettleid) if self.kettleid is not None else None
        )
        self.fermenter = (
            self.get_fermenter(self.fermenterid)
            if self.fermenterid is not None
            else None
        )

        while self.running == True:
            self.TEMP_UNIT = self.get_config_value("TEMP_UNIT", "C")
            # Only compute a value once the bus has actually produced one.
            #
            # The reader thread starts at 0 and keeps its last value on a failed
            # read, so computing unconditionally turned "nothing read yet" into
            # 0 C, or 32 F on a Fahrenheit rig. That number then sat in
            # get_state() with age None - never published, so never aged - and
            # any consumer willing to act on an ageless reading would call for
            # full heat against it.
            #
            # last_success is None until the first CRC-checked read, which is
            # exactly the condition "this probe has never told us anything".
            if self.t.last_success is not None:
                if (
                    self.TEMP_UNIT == "C"
                ):  # Report temp in C if nothing else is selected in settings
                    self.value = round((self.t.value + self.offset), 2)
                else:  # Report temp in F if unit selected in settings
                    self.value = round((9.0 / 5.0 * self.t.value + 32 + self.offset), 2)

            # Only publish a reading the probe actually produced. The reader thread
            # keeps its previous value when a read fails, so publishing
            # unconditionally meant a disconnected probe reported a plausible
            # temperature forever - and kept refreshing its own freshness while
            # doing it, defeating any staleness check downstream.
            #
            # Staying quiet is what makes the reading age, which is what lets
            # kettle logic notice and switch the heater off. Logging is skipped for
            # the same reason: charting a value the probe never produced turns a
            # dead sensor into a convincing flat line, where a gap is honest.
            if self._reads_are_current():
                self.push_update(self.value)

                if self.reducedlogging:
                    await self.logvalue()
                else:
                    logging.info(
                        "OneWire {} regular logging".format(self.sensor.name)
                    )
                    self.log_data(self.value)
                    self.lastlog = time.time()

            await asyncio.sleep(self.interval)

    async def logvalue(self):
        now = time.time()
        logging.info("OneWire {} logging subroutine".format(self.sensor.name))
        if self.kettle is not None:
            try:
                kettlestatus = self.kettle.instance.state
            except:
                kettlestatus = False
            if kettlestatus:
                self.log_data(self.value)
                logging.info("OneWire {} Kettle Active".format(self.sensor.name))
                self.lastlog = time.time()
            else:
                logging.info("OneWire {} Kettle Inactive".format(self.sensor.name))
                if self.reducedfrequency != 0:
                    if now >= self.lastlog + self.reducedfrequency:
                        self.log_data(self.value)
                        self.lastlog = time.time()
                        logging.info("Logged with reduced freqency")
                        pass

        if self.fermenter is not None:
            try:
                fermenterstatus = self.fermenter.instance.state
            except:
                fermenterstatus = False
            if fermenterstatus:
                self.log_data(self.value)
                logging.info("OneWire {} Fermenter Active".format(self.sensor.name))
                self.lastlog = time.time()
            else:
                logging.info("OneWire {} Fermenter Inactive".format(self.sensor.name))
                if self.reducedfrequency != 0:
                    if now >= self.lastlog + self.reducedfrequency:
                        self.log_data(self.value)
                        self.lastlog = time.time()
                        logging.info("Logged with reduced freqency")
                        pass

    def get_state(self):
        return dict(value=self.value)


def setup(cbpi):
    cbpi.plugin.register("OneWire", OneWire)
    try:
        # Global Init
        call(["sudo", "modprobe", "w1-gpio"])
        call(["sudo", "modprobe", "w1-therm"])
    except Exception as e:
        pass
