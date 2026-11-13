import asyncio
import logging
import os
import time
from collections import OrderedDict
from datetime import datetime
from email import message

import shortuuid
from cbpi.api import *
from cbpi.api.dataclasses import NotificationType


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
                            f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}: Restore Error | {line}'
                        ],
                    )
            os.remove(self.restore_error)
        except Exception as e:
            pass

    # Seconds before the same log message raises a notification again. With
    # NOTIFY_ON_ERROR on, a fault that logs every control pass would otherwise
    # send a notification every second - to the phone, through any listener -
    # for as long as it lasted.
    LOG_NOTIFY_REPEAT_EVERY = 3600
    LOG_NOTIFY_REMEMBERED = 200

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
                if self._log_notification_due(record.levelno, message):
                    # on log events higher then INFO we want to notify all clients
                    type = NotificationType.WARNING
                    if record.levelno > 30:
                        type = NotificationType.ERROR
                    self.cbpi.notify(
                        title=f"{record.levelname}", message=message, type=type
                    )
            except Exception as e:
                pass
        return True

    def _log_notification_due(self, level, message):
        sent = self.__dict__.setdefault("_log_notified", OrderedDict())
        key = (level, message)
        now = time.monotonic()
        last = sent.get(key)
        if last is not None and now - last < self.LOG_NOTIFY_REPEAT_EVERY:
            return False
        sent[key] = now
        sent.move_to_end(key)
        while len(sent) > self.LOG_NOTIFY_REMEMBERED:
            sent.popitem(last=False)
        return True

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

    async def _call_listener(self, title, message, type, action):
        background_tasks = set()
        for id, method in self.listener.items():
            task = asyncio.create_task(method(self.cbpi, title, message, type, action))
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)

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
        :return: the notification's id
        """
        notifcation_id = shortuuid.uuid()
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
        if keep and action:
            self._open()[notifcation_id] = dict(shown)
        self.cbpi.ws.send(shown)
        data = dict(
            type=type.value,
            title=title,
            message=message,
            action=actions,
            timeout=timeout,
        )
        self.cbpi.push_update(topic="cbpi/notification", data=data)
        self.notifications.insert(
            0, [f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}: {title} | {message}']
        )
        if len(self.notifications) > 100:
            self.notifications = self.notifications[:100]
        self.cbpi.ws.send(
            dict(topic=self.update_key, data=self.notifications), self.sorting
        )
        task = asyncio.create_task(self._call_listener(title, message, type, action))
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return notifcation_id

    def retire(self, notification_id, reason="withdrawn") -> bool:
        """Take a prompt down: it has been answered, or no longer applies.

        From every page showing it, and from those shown to pages that open
        later. Its buttons stop working too: pressed on a page that missed
        this, they would act on a profile that has moved on - "Skip this step"
        on a "Resume brew?" left over skips whatever step runs by then.

        `reason` goes to the pages with it: "answered" when a button was
        pressed (notify_callback), else "withdrawn", so a page that did not
        answer it can say which.

        True if it was still waiting for an answer.
        """
        waiting = self.callback_cache.pop(notification_id, None) is not None
        kept = self._open().pop(notification_id, None) is not None
        if waiting or kept:
            self.cbpi.ws.send(dict(topic=self.RETIRED_TOPIC, id=notification_id, reason=reason))
        return waiting or kept

    def kept_prompts(self):
        """The prompts kept for every page, as the messages that showed them."""
        return [dict(shown) for shown in self._open().values()]

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
                task = asyncio.create_task(action.method())
                background_tasks.add(task)
                task.add_done_callback(background_tasks.discard)
            # Answered: gone from every page, not just the one it was
            # answered on, or a phone would keep showing it with a button
            # that no longer does anything.
            self.retire(notification_id, reason="answered")
            return True
        except Exception as e:
            self.logger.error("Failed to call notification callback")
            return False    

