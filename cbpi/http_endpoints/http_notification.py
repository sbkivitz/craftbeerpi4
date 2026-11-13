from aiohttp import web
from cbpi.api import request_mapping
from cbpi.utils import json_dumps


class NotificationHttpEndpoints:

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self.cbpi.register(self, url_prefix="/notification")

    @request_mapping(
        path="/{id}/action/{action_id}", method="POST", auth_required=False
    )
    async def action(self, request):
        """
        ---
        description: Update an actor
        tags:
        - Notification
        parameters:
        - name: "id"
          in: "path"
          description: "Notification Id"
          required: true
          type: "string"
        - name: "action_id"
          in: "path"
          description: "Action Id"
          required: true
          type: "string"

        responses:
            "200":
                description: successful operation
            "400":
                description: failed operation
        """

        notification_id = request.match_info["id"]
        action_id = request.match_info["action_id"]
        # print(notification_id, action_id)
        success = self.cbpi.notification.notify_callback(notification_id, action_id)
        return web.Response(status=200 if success else 400)

    @request_mapping(path="/{id}/seen", method="POST", auth_required=False)
    async def seen(self, request):
        """
        ---
        description: A warning or error with no buttons was OK'd on one page. Take it down from every page, with each remembered copy in the same words.
        tags:
        - Notification
        parameters:
        - name: "id"
          in: "path"
          description: "Notification Id"
          required: true
          type: "string"
        responses:
            "200":
                description: taken down; the body's retired lists the ids, the OK'd one first
            "404":
                description: not a warning or error remembered - a question, one already seen, one more than 100 ago, or one sent before a restart
        """
        retired = self.cbpi.notification.seen(request.match_info["id"])
        if retired is None:
            return web.Response(status=404)
        return web.json_response({"retired": retired}, dumps=json_dumps)

    @request_mapping("/delete", method="POST", auth_required=False)
    async def restart(self, request):
        """
        ---
        description: DeleteNotifications
        tags:
        - Notification
        responses:
            "200":
                description: successful operation
        """
        self.cbpi.notification.delete_all_notifications()
        return web.Response(status=200)
