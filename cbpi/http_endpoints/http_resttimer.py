from aiohttp import web
from cbpi.api import request_mapping
from cbpi.utils import json_dumps


class RestTimerHttpEndpoints:
    """The brew-day screen's rest timer (RestTimerController): one timer, kept by
    the server, so the monitor and the phone count down the same rest."""

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self.controller = cbpi.resttimer
        self.cbpi.register(self, url_prefix="/resttimer")

    @staticmethod
    def _state(state):
        return web.json_response(data=state, dumps=json_dumps)

    @request_mapping(path="/", method="GET", auth_required=False)
    async def http_get(self, request):
        """
        ---
        description: The rest timer - seconds to go on the brewing clock, whether it is counting down, and the clock's scale (simulated seconds per real second)
        tags:
        - RestTimer
        responses:
            "200":
                description: successful operation
        """
        return self._state(self.controller.get_state())

    @request_mapping(path="/start", method="POST", auth_required=False)
    async def http_start(self, request):
        """
        ---
        description: Start counting down. At zero CraftBeerPi raises a notification with an OK button.
        tags:
        - RestTimer
        responses:
            "200":
                description: counting down
            "409":
                description: the timer is at zero; add time first
        """
        try:
            return self._state(await self.controller.start())
        except ValueError as e:
            return web.json_response(data=dict(error=str(e)), status=409)

    @request_mapping(path="/pause", method="POST", auth_required=False)
    async def http_pause(self, request):
        """
        ---
        description: Stop counting down, keeping the time left
        tags:
        - RestTimer
        responses:
            "200":
                description: paused
        """
        return self._state(await self.controller.pause())

    @request_mapping(path="/add", method="POST", auth_required=False)
    async def http_add(self, request):
        """
        ---
        description: Add time, or take it away with a negative number. The timer stays between one minute and five hours.
        tags:
        - RestTimer
        parameters:
        - in: body
          name: body
          description: seconds to add, e.g. 300 or -300
          required: true
          schema:
            type: object
            properties:
              seconds:
                type: number
        responses:
            "200":
                description: the new time left
            "400":
                description: seconds is missing or not a number
        """
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001 - no body, or not JSON
            data = None
        if not isinstance(data, dict) or "seconds" not in data or isinstance(data.get("seconds"), bool):
            return web.json_response(data=dict(error="send {\"seconds\": n}"), status=400)
        try:
            return self._state(await self.controller.add(data["seconds"]))
        except ValueError as e:
            return web.json_response(data=dict(error=str(e)), status=400)
