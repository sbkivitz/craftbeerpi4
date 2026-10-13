"""Tell systemd the server is alive, so it can restart one that is not.

A fermenter runs for weeks unattended, and the server can stop serving it in
ways no code inside it can recover from: the process killed, or its event
loop wedged by something blocking it. systemd's Restart= handles the first.
For the second, systemd needs to hear, regularly, that the loop is still
turning - the watchdog - and restarts the service when it stops hearing.

This sends those messages over the sd_notify protocol: one datagram per
message to the unix socket named by $NOTIFY_SOCKET (a leading "@" names an
abstract socket). With no $NOTIFY_SOCKET - Windows, a terminal, a unit
without NotifyAccess= - it does nothing at all.

The watchdog is ARMED from here rather than by WatchdogSec= in the unit, by
sending WATCHDOG_USEC= (systemd's own service.c turns that into the timeout).
The unit only needs NotifyAccess=main. In two phases:

  start-up  WATCHDOG_USEC=<STARTUP_BUDGET_SEC> and no beats, so a start-up
            that never finishes is killed and restarted;
  running   READY=1, then WATCHDOG=1 with the running period restated on
            every beat, so one lost datagram cannot leave it unarmed.

A server that does not run this never gets a watchdog that would kill it for
silence. WatchdogSec= in the unit, if present, sets the running period.
"""

import asyncio
import logging
import os
import socket
import time

logger = logging.getLogger(__name__)

# Seconds of silence before systemd restarts the server. Generous on purpose:
# this is for a loop that has stopped, not one that is busy - a Pi writing to
# a slow SD card can stall for seconds at a time.
DEFAULT_WATCHDOG_SEC = 120

# Seconds start-up may take before systemd restarts the server. Without it, a
# start-up that never finished - an initializer waiting forever - left a server
# that never came up and a watchdog that was never armed (DEFECTS F26). A Pi
# starts this in well under a minute.
STARTUP_BUDGET_SEC = 300

# Beats per watchdog period. Three leaves room for two missed beats.
BEATS_PER_PERIOD = 3

# Attempts to arm the start-up deadline, a second apart, until one is sent.
# Each delivery restarts systemd's timer, so retrying for long would stretch
# the very budget it sets; one attempt alone could leave start-up unwatched
# for good if that datagram was lost (DEFECTS F36).
STARTUP_ARM_ATTEMPTS = 10


def socket_address(path):
    """The address to send to: a leading '@' is the abstract namespace."""
    return "\0" + path[1:] if path.startswith("@") else path


def notify(message, environ=None):
    """Send one sd_notify message. False when there is no systemd listening.

    Never blocks and never raises: a full socket buffer or a missing socket is
    a lost heartbeat, which is exactly what the watchdog is there to judge.
    """
    environ = os.environ if environ is None else environ
    path = environ.get("NOTIFY_SOCKET")
    if not path or not hasattr(socket, "AF_UNIX"):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            sock.sendto(message.encode("utf-8"), socket_address(path))
        return True
    except OSError as e:
        logger.debug("sd_notify %r failed: %s", message, e)
        return False


class Heartbeat:
    """A start-up deadline, then READY=1 and WATCHDOG=1 for as long as the event loop turns."""

    def __init__(self, send=notify, environ=None, watchdog_sec=DEFAULT_WATCHDOG_SEC,
                 clock=time.monotonic, startup_sec=STARTUP_BUDGET_SEC):
        self._send = send
        self._environ = os.environ if environ is None else environ
        self.watchdog_sec = watchdog_sec
        self.startup_sec = startup_sec
        self._clock = clock
        self.task = None
        self.beats = 0
        self.period = None
        self._arming = None

    def _systemd_period(self):
        """WatchdogSec= from the unit, if systemd set one for this process."""
        usec = self._environ.get("WATCHDOG_USEC")
        pid = self._environ.get("WATCHDOG_PID")
        if not usec or (pid and pid != str(os.getpid())):
            return None
        try:
            return int(usec) / 1e6
        except ValueError:
            return None

    def begin_startup(self):
        """Arm the watchdog for start-up, without beating: a start-up that hangs is restarted.

        True once the arming message is sent. If it cannot be, it is retried a
        second apart, a few times, from a task.
        """
        if not self._environ.get("NOTIFY_SOCKET"):
            return False
        message = "STATUS=CraftBeerPi starting\nWATCHDOG_USEC={}".format(
            int(self.startup_sec * 1e6))
        if self._send(message):
            return True
        logger.warning("systemd: could not arm the start-up deadline - retrying")
        try:
            self._arming = asyncio.get_running_loop().create_task(self._arm_until_sent(message))
        except RuntimeError:  # no running loop to retry from
            self._arming = None
        return False

    async def _arm_until_sent(self, message):
        for _ in range(STARTUP_ARM_ATTEMPTS - 1):
            await asyncio.sleep(1)
            if self._send(message):
                return True
        logger.error("systemd: the start-up deadline could not be armed - a start-up "
                     "that hangs will not be restarted")
        return False

    def start(self):
        """Begin, if systemd is listening. Returns the beating task, or None."""
        if self._arming is not None and not self._arming.done():
            self._arming.cancel()
        if not self._environ.get("NOTIFY_SOCKET"):
            logger.info("No systemd notify socket - running without a watchdog")
            return None
        # The unit's own WatchdogSec= wins. Restated either way, because
        # begin_startup() replaced it with the start-up budget.
        self.period = self._systemd_period() or float(self.watchdog_sec)
        self._send("READY=1\nSTATUS=CraftBeerPi running")
        self._send(self._beat_message())
        logger.info("systemd watchdog: restarted after %.0fs without a heartbeat", self.period)
        self.task = asyncio.create_task(self._beat(self.period / BEATS_PER_PERIOD))
        return self.task

    async def on_startup(self, app=None):
        """aiohttp on_startup hook: ready only once every other start-up hook has run."""
        self.start()

    def _beat_message(self):
        # The watchdog's timeout rides along with every beat. Sent once, a
        # lost datagram left the watchdog unarmed for good, beats and all
        # (DEFECTS F26); restating it costs nothing.
        return "WATCHDOG=1\nWATCHDOG_USEC={}".format(int(self.period * 1e6))

    async def _beat(self, interval):
        due = self._clock() + interval
        while True:
            # A one-second cadence, checked against a deadline: the beat is
            # sent from the event loop itself, so it stops when the loop does.
            await asyncio.sleep(1)
            if self._clock() >= due:
                self._send(self._beat_message())
                self.beats += 1
                due = self._clock() + interval

    async def stop(self, app=None):
        """Say the stop is deliberate, so a slow shutdown is not mistaken for a hang."""
        if self.task is None:
            return
        self._send("STOPPING=1")
        self.task.cancel()
        await asyncio.wait({self.task})
        self.task = None
