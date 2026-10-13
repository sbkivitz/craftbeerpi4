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

The watchdog is ARMED from here, after start-up has finished, by sending
WATCHDOG_USEC= (systemd's own service.c turns that into the timeout, with or
without WatchdogSec= in the unit). The unit only needs NotifyAccess=main.
That order fails safe both ways: a slow start is never killed for not having
started beating yet, and a server that does not run this never gets a
watchdog that would kill it for silence. WatchdogSec= in the unit, if
present, wins.
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

# Beats per watchdog period. Three leaves room for two missed beats.
BEATS_PER_PERIOD = 3


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
    """READY=1 once started, then WATCHDOG=1 for as long as the event loop turns."""

    def __init__(self, send=notify, environ=None, watchdog_sec=DEFAULT_WATCHDOG_SEC,
                 clock=time.monotonic):
        self._send = send
        self._environ = os.environ if environ is None else environ
        self.watchdog_sec = watchdog_sec
        self._clock = clock
        self.task = None
        self.beats = 0

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

    def start(self):
        """Begin, if systemd is listening. Returns the beating task, or None."""
        if not self._environ.get("NOTIFY_SOCKET"):
            logger.info("No systemd notify socket - running without a watchdog")
            return None
        period = self._systemd_period()
        armed_here = period is None
        if armed_here:
            period = float(self.watchdog_sec)
        self._send("READY=1\nSTATUS=CraftBeerPi running")
        if armed_here:
            self._send("WATCHDOG_USEC={}".format(int(period * 1e6)))
        self._send("WATCHDOG=1")
        logger.info("systemd watchdog: restarted after %.0fs without a heartbeat", period)
        self.task = asyncio.create_task(self._beat(period / BEATS_PER_PERIOD))
        return self.task

    async def _beat(self, interval):
        due = self._clock() + interval
        while True:
            # A one-second cadence, checked against a deadline: the beat is
            # sent from the event loop itself, so it stops when the loop does.
            await asyncio.sleep(1)
            if self._clock() >= due:
                self._send("WATCHDOG=1")
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
