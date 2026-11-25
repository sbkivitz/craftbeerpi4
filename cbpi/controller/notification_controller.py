import asyncio
import contextvars
import inspect
import logging
import os
from collections import OrderedDict
from datetime import datetime
from email import message

import shortuuid
from cbpi.api import *
from cbpi.api import clock, presence, timefmt
from cbpi.api.dataclasses import NotificationType

# What notify_log_event says of the log line it is notifying, for notify to pass on to the listeners: per thread and
# per task, as logging can come from any.
_LOG_LINE = contextvars.ContextVar("log_line", default=None)


class NotificationController:

    # Callbacks are only ever removed when the brewer clicks the action
    # (notify_callback), so anything never clicked stayed forever. Every
    # notification was cached, including the ones with no actions at all -
    # and a toast with action=[] auto-dismisses, so it can never be clicked
    # and its entry could never be removed by any code path.
    #
    # self.notifications is already capped at 100 for exactly this reason;
    # this dict was not capped at all. On a long brew with NOTIFY_ON_ERROR
    # enabled a fault logs a warning every control pass, and each one leaked
    # an entry for the lifetime of the process.
    #
    # A class attribute so it can be read and overridden without constructing
    # a controller.
    MAX_CALLBACK_CACHE = 100

    # Sent to every page when a prompt is taken down - answered on one of
    # them, or no longer applying (retire). With the prompt's id.
    RETIRED_TOPIC = "notification_retired"

    # A plain warning or error - one with no buttons - is remembered, so an OK
    # on one page - the brew-day screen, say - can take it down on the others,
    # BrewScribe's too (seen). Bounded as the callbacks are.
    MAX_PLAIN = 100
    # The ids seen, newest last, sent to each page as it connects: a page
    # asleep in a background tab misses the broadcast.
    MAX_SEEN = 100
    SEEN_TOPIC = "notification_seen"

    def __init__(self, cbpi):
        """
        :param cbpi: craftbeerpi object
        """
        self.cbpi = cbpi
        self.logger = logging.getLogger(__name__)
        logging.root.addFilter(self.notify_log_event)
        self.callback_cache = {}
        # Prompts kept until they are answered or retired (notify(keep=True)):
        # id -> the message that showed it, shown again to every page that
        # connects meanwhile. Only ever prompts still in callback_cache, so
        # bounded with it.
        self.open_prompts = {}
        self.listener = {}
        self.notifications = []
        self.update_key = "notificationupdate"
        self.sorting = False
        self.check_startup_message()

    def check_startup_message(self):
        self.restore_error = self.cbpi.config_folder.get_file_path("restore_error.log")
        try:
            with open(self.restore_error) as f:
                for line in f:
                    self.notifications.insert(
                        0,
                        [
                            f'{datetime.fromtimestamp(clock.now()).strftime("%Y-%m-%d %H:%M:%S")}: Restore Error | {line}'
                        ],
                    )
            os.remove(self.restore_error)
        except Exception as e:
            pass

    # With NOTIFY_ON_ERROR on, a fault that logs every control pass would send
    # a notification every second - to the phone, through any listener - for
    # as long as it lasted. So a line logged again and again is notified once
    # every LOG_NOTIFY_REPEAT_EVERY seconds. A line that has not been logged
    # for LOG_NOTIFY_QUIET seconds has cleared, and is notified at once when
    # it comes back: the window cbpi.api.log_repeats holds a repeating line
    # back for. The lines are remembered LOG_NOTIFY_REMEMBERED at most, the
    # one logged longest ago forgotten first.
    LOG_NOTIFY_REPEAT_EVERY = 3600
    LOG_NOTIFY_QUIET = 300
    LOG_NOTIFY_REMEMBERED = 200
    # Why a log line is notified, as a listener that takes log_line is told:
    # LOG_LINE_NEW the first time, or back after LOG_NOTIFY_QUIET unlogged -
    # its fault cleared and came back; LOG_LINE_STILL once an hour while it
    # keeps being logged - a fault that never cleared. A listener that holds
    # back repeats of the same words, as phone alerts do, holds back only the
    # second kind.
    LOG_LINE_NEW = "new"
    LOG_LINE_STILL = "still"

    def notify_log_event(self, record):
        NOTIFY_ON_ERROR = self.cbpi.config.get("NOTIFY_ON_ERROR", "No")
        if NOTIFY_ON_ERROR == "Yes" and record.levelno > 20:
            try:
                # The message, not its template: str(record.msg) sent
                # "Actor %s reports still ON..." with the actor left out.
                message = record.getMessage()
            except Exception:  # noqa: BLE001
                message = str(record.msg)
            try:
                due = self._log_notification_due(record.levelno, message)
                if due:
                    # on log events higher then INFO we want to notify all clients
                    type = NotificationType.WARNING
                    if record.levelno > 30:
                        type = NotificationType.ERROR
                    told = _LOG_LINE.set(due)
                    try:
                        self.cbpi.notify(
                            title=f"{record.levelname}", message=message, type=type
                        )
                    finally:
                        _LOG_LINE.reset(told)
            except Exception as e:
                pass
        return True

    def _log_notification_due(self, level, message):
        """Whether this log line raises a notification, and why: the first
        time it is logged, and when it comes back after LOG_NOTIFY_QUIET
        seconds unlogged, LOG_LINE_NEW; once every LOG_NOTIFY_REPEAT_EVERY
        seconds while it keeps being logged, LOG_LINE_STILL. None when it
        doesn't.

        Timed on CraftBeerPi's clock, so a simulator at 60x notifies as a rig
        would, 60 times as fast. A gap that comes out negative - the clock
        replaced by one that is behind - counts as the line coming back: it is
        notified once, and timed from then.
        """
        lines = self.__dict__.setdefault("_log_lines", OrderedDict())
        key = (level, message)
        now = clock.monotonic()
        times = lines.get(key)  # [when last notified, when last logged]
        new = (
            times is None
            or not 0 <= now - times[1] < self.LOG_NOTIFY_QUIET
        )
        due = (
            new
            or not 0 <= now - times[0] < self.LOG_NOTIFY_REPEAT_EVERY
        )
        if due:
            lines[key] = [now, now]
        else:
            times[1] = now
        lines.move_to_end(key)
        while len(lines) > self.LOG_NOTIFY_REMEMBERED:
            lines.popitem(last=False)
        if not due:
            return None
        return self.LOG_LINE_NEW if new else self.LOG_LINE_STILL

    def get_state(self):
        result = self.notifications
        return result

    def add_listener(self, method):
        listener_id = shortuuid.uuid()
        self.listener[listener_id] = method
        return listener_id

    def remove_listener(self, listener_id):
        try:
            del self.listener[listener_id]
        except:
            self.logger.error("Failed to remove listener {}".format(listener_id))

    async def _call_listener(self, title, message, type, action, log_line=None):
        # The listeners' tasks copy this task's context: cleared first, so a
        # notification a listener sends itself isn't taken for this log line's.
        _LOG_LINE.set(None)
        background_tasks = set()
        for id, method in self.listener.items():
            # log_line to a listener that takes it; any other is called as it
            # always was.
            more = dict(log_line=log_line) if self._takes_log_line(method) else {}
            task = asyncio.create_task(method(self.cbpi, title, message, type, action, **more))
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)

    @staticmethod
    def _takes_log_line(method):
        try:
            parameters = inspect.signature(method).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(p.name == "log_line" or p.kind is p.VAR_KEYWORD for p in parameters)

    def _open(self):
        """open_prompts, made on first use. A controller assembled without
        __init__ - test_notification_cache_leak.py builds one with __new__ -
        has none, and a class-level {} would be one dict shared by every
        controller."""
        return self.__dict__.setdefault("open_prompts", {})

    def notify(
        self,
        title,
        message: str,
        type: NotificationType = NotificationType.INFO,
        action=[],
        timeout: int = 5000,
        keep: bool = False,
        kind: str = None,
    ):
        """
        This is a convinience method to send notification to the client

        :param key: notification key
        :param message: notification message
        :param type: notification type (info,warning,danger,successs)
        :param keep: for a prompt - one with buttons - keep it until a button
            is pressed or retire() takes it down, and show it to every page
            that opens meanwhile. Otherwise a prompt reaches only the pages
            open when it is sent: one sent while the server starts reaches
            none.
        :param kind: a word a page can tell the notification by, whatever its
            words say, sent with it as "kind": "condenser_on" for the boil's
            lid reminder where a steam condenser is fitted, "condenser_off" for
            the reminder to turn its mister off after the boil. None sends none.
        :return: the notification's id

        A listener (add_listener) that takes a log_line keyword is told, for a
        notification notify_log_event makes from a log line, why it is sent:
        LOG_LINE_NEW or LOG_LINE_STILL. None for any other notification.
        """
        notifcation_id = shortuuid.uuid()
        log_line = _LOG_LINE.get()
        background_tasks = set()

        def prepare_action(item):
            item.id = shortuuid.uuid()
            return item.to_dict()

        actions = list(map(lambda item: prepare_action(item), action))
        # Only cache what can actually be called back. A notification with no
        # actions has nothing to dispatch to, and nothing that would ever
        # remove its entry.
        if action:
            self.callback_cache[notifcation_id] = action
            # Bound it. Unclicked alerts are normal - the brewer sees the hop
            # go in and carries on - so eviction has to be by age rather than
            # by waiting for a click that may never come. dict preserves
            # insertion order, so the oldest pending callback goes first.
            while len(self.callback_cache) > self.MAX_CALLBACK_CACHE:
                self.retire(next(iter(self.callback_cache)))
        shown = dict(
            id=notifcation_id,
            topic="notifiaction",
            type=type.value,
            title=title,
            message=message,
            action=actions,
            timeout=timeout,
        )
        if kind is not None:
            shown["kind"] = str(kind)
        if keep and action:
            self._open()[notifcation_id] = dict(shown)
        if not action and type in (NotificationType.WARNING, NotificationType.ERROR):
            plain = self._plain()
            plain[notifcation_id] = (type.value, title, message)
            while len(plain) > self.MAX_PLAIN:
                plain.popitem(last=False)
        self.cbpi.ws.send(shown)
        data = dict(
            type=type.value,
            title=title,
            message=message,
            action=actions,
            timeout=timeout,
        )
        self.cbpi.push_update(topic="cbpi/notification", data=data)
        # Stamped on CraftBeerPi's clock (cbpi.api.clock), the one every screen shows: on a simulator it runs
        # SIM_TIME_SCALE times as fast, and the computer's would put the list at another time than Brew Day's LOG.
        self.notifications.insert(
            0, [f'{timefmt.day_and_time(clock.now(), self.cbpi, day="%Y-%m-%d", seconds=True)}: '
                f'{title} | {message}']
        )
        if len(self.notifications) > 100:
            self.notifications = self.notifications[:100]
        self.cbpi.ws.send(
            dict(topic=self.update_key, data=self.notifications), self.sorting
        )
        task = asyncio.create_task(self._call_listener(title, message, type, action, log_line))
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return notifcation_id

    def retire(self, notification_id, reason="withdrawn", answer=None) -> bool:
        """Take a prompt down: it has been answered, or no longer applies.

        From every page showing it, and from those shown to pages that open
        later. Its buttons stop working too: pressed on a page that missed
        this, they would act on a profile that has moved on - "Skip this step"
        on a "Resume brew?" left over skips whatever step runs by then.

        `reason` goes to the pages with it: "answered" when a button was
        pressed (notify_callback), else "withdrawn", so a page that did not
        answer it can say which.

        `answer`, when a button was pressed: its label, sent with the reason,
        so a page that did not press it can say which was - the brew-day
        screen's line for an answer from another screen, "Resume brew?
        answered: Skip this step", as an answer given there reads.

        True if it was still waiting for an answer.
        """
        waiting = self.callback_cache.pop(notification_id, None) is not None
        kept = self._open().pop(notification_id, None) is not None
        if waiting or kept:
            gone = dict(topic=self.RETIRED_TOPIC, id=notification_id, reason=reason)
            if answer is not None:
                gone["answer"] = str(answer)
            self.cbpi.ws.send(gone)
        return waiting or kept

    def kept_prompts(self):
        """The prompts kept for every page, as the messages that showed them."""
        return [dict(shown) for shown in self._open().values()]

    def _plain(self):
        """The plain warnings and errors not yet seen: id -> (type, title,
        message), oldest first. Made on first use, as open_prompts is."""
        return self.__dict__.setdefault("_plain_shown", OrderedDict())

    def _seen(self):
        return self.__dict__.setdefault("_seen_ids", OrderedDict())

    def seen(self, notification_id):
        """A plain warning or error OK'd on one page: take it down from every
        page, with each remembered copy in the same words - the screens show a
        repeat once, so one OK answers them all. Each goes to the pages as
        notification_retired, reason "seen".

        -> the ids taken down, or None if it isn't one remembered: a question
        (its buttons answer it), one already seen, one more than MAX_PLAIN
        ago, or one sent before a restart.
        """
        plain = self._plain()
        shown = plain.pop(notification_id, None)
        if shown is None:
            return None
        ids = [notification_id] + [other for other, words in plain.items() if words == shown]
        seen = self._seen()
        for gone in ids:
            plain.pop(gone, None)
            seen[gone] = True
            seen.move_to_end(gone)
            self.cbpi.ws.send(dict(topic=self.RETIRED_TOPIC, id=gone, reason="seen"))
        while len(seen) > self.MAX_SEEN:
            seen.popitem(last=False)
        return ids

    def seen_ids(self):
        """The warnings and errors seen since the server started, the last
        MAX_SEEN, oldest first: for a page that connects."""
        return list(self._seen())

    def waiting_prompts(self):
        """The ids of the prompts whose buttons still do something."""
        return list(self.callback_cache)

    def delete_all_notifications(self):
        self.notifications = []
        self.cbpi.ws.send(
            dict(topic=self.update_key, data=self.notifications), self.sorting
        )

    def notify_callback(self, notification_id, action_id) -> None:
        try:
            pending = self.callback_cache.get(notification_id)
            if pending is None:
                # Evicted, already handled, or never had actions. Clicking a
                # stale alert is a normal thing for a brewer to do and must not
                # look like a fault, but it should be diagnosable rather than
                # silent: logged, not a KeyError raised into the handler below.
                self.logger.info(
                    "Notification %s is no longer pending; ignoring action %s",
                    notification_id,
                    action_id,
                )
                return False
            action = next(
                (item for item in pending if item.id == action_id),
                None,
            )
            if action is not None and action.method is not None:
                background_tasks = set()
                # The brewer's answer: a person's press in the task that runs it, as a step's question needs to
                # move on (cbpi.api.presence).
                task = asyncio.create_task(presence.press_in_task("answer", action.method))
                background_tasks.add(task)
                task.add_done_callback(background_tasks.discard)
            # Answered: gone from every page, not just the one it was
            # answered on, or a phone would keep showing it with a button
            # that no longer does anything. With the button's label, for the
            # pages that did not press it.
            self.retire(notification_id, reason="answered",
                        answer=getattr(action, "label", None))
            return True
        except Exception as e:
            self.logger.error("Failed to call notification callback")
            return False    

