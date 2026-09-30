import asyncio
import logging
import os
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

    def __init__(self, cbpi):
        """
        :param cbpi: craftbeerpi object
        """
        self.cbpi = cbpi
        self.logger = logging.getLogger(__name__)
        logging.root.addFilter(self.notify_log_event)
        self.callback_cache = {}
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

    def notify_log_event(self, record):
        NOTIFY_ON_ERROR = self.cbpi.config.get("NOTIFY_ON_ERROR", "No")
        if NOTIFY_ON_ERROR == "Yes":
            try:
                message = str(record.msg)
            except:
                message = record.msg
            try:
                if record.levelno > 20:
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

    def notify(
        self,
        title,
        message: str,
        type: NotificationType = NotificationType.INFO,
        action=[],
        timeout: int = 5000,
    ) -> None:
        """
        This is a convinience method to send notification to the client

        :param key: notification key
        :param message: notification message
        :param type: notification type (info,warning,danger,successs)
        :return:
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
                self.callback_cache.pop(next(iter(self.callback_cache)))
        self.cbpi.ws.send(
            dict(
                id=notifcation_id,
                topic="notifiaction",
                type=type.value,
                title=title,
                message=message,
                action=actions,
                timeout=timeout,
            )
        )
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
                # silent - this used to raise KeyError into the handler below
                # and be logged as an error.
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
            del self.callback_cache[notification_id]
            return True
        except Exception as e:
            self.logger.error("Failed to call notification callback")
            return False    

