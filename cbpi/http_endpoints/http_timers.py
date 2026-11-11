from aiohttp import web
from cbpi.api import request_mapping
from cbpi.controller.timers_controller import TimerRefused
from cbpi.utils import json_dumps


class TimersHttpEndpoints:
    """The brew-day screen's timers (TimersController): up to four, kept by the
    server, so the monitor and the phone count down the same ones.

    Every command answers with the timers as they now are, as GET does, and the
    same goes to every screen on the websocket topic "timers". 400: the request
    is malformed; 404: no such timer; 409: the timer's state doesn't allow it,
    with the reason in "error"."""

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self.controller = cbpi.timers
        self.cbpi.register(self, url_prefix="/timers")

    @staticmethod
    def _state(state):
        return web.json_response(data=state, dumps=json_dumps)

    @staticmethod
    def _error(e, status):
        return web.json_response(data=dict(error=str(e)), status=status)

    @staticmethod
    async def _body(request):
        """The request's JSON object: {} with no body, None if it isn't an object."""
        if not getattr(request, "body_exists", True):
            return {}
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001 - not JSON
            return None
        return data if isinstance(data, dict) else None

    async def _do(self, command, *args, **kwargs):
        try:
            return self._state(await command(*args, **kwargs))
        except LookupError as e:
            return self._error(e, 404)
        except TimerRefused as e:
            return self._error(e, 409)
        except ValueError as e:
            return self._error(e, 400)

    @request_mapping(path="/", method="GET", auth_required=False)
    async def http_get(self, request):
        """
        ---
        description: The timers - each one's id, name, own time (set), this countdown's length (run), time left, state (stopped, running, paused or done), when it ran out (at) and its prompt's id - with the most there can be, the names to offer (the BREWDAY_TIMER_NAMES setting), the brewing clock's time and its scale
        tags:
        - Timers
        responses:
            "200":
                description: successful operation
        """
        return self._state(self.controller.get_state())

    @request_mapping(path="/", method="POST", auth_required=False)
    async def http_add(self, request):
        """
        ---
        description: Add a timer. With no name it is "Timer N"; with no seconds, 10 minutes. start true counts it down at once.
        tags:
        - Timers
        parameters:
        - in: body
          name: body
          required: false
          schema:
            type: object
            properties:
              name:
                type: string
              seconds:
                type: number
              start:
                type: boolean
        responses:
            "200":
                description: the timers, the new one last
            "400":
                description: the name is too long, or seconds is not 1 to 300 minutes
            "409":
                description: there are four already
        """
        data = await self._body(request)
        if data is None:
            return self._error("send an object: {\"name\": ..., \"seconds\": ..., \"start\": ...}", 400)
        return await self._do(self.controller.add, data.get("name"), data.get("seconds"), data.get("start") is True)

    @request_mapping(path="/{id}", method="PUT", auth_required=False)
    async def http_change(self, request):
        """
        ---
        description: Rename a timer, or set a stopped one's own time (a finished one's alarm is answered by it)
        tags:
        - Timers
        parameters:
        - name: "id"
          in: "path"
          required: true
          type: "integer"
        - in: body
          name: body
          required: true
          schema:
            type: object
            properties:
              name:
                type: string
              seconds:
                type: number
        responses:
            "200":
                description: the timers
            "404":
                description: no such timer
            "409":
                description: it is counting down or paused; change its time left with /add
        """
        data = await self._body(request)
        if data is None:
            return self._error("send an object: {\"name\": ..., \"seconds\": ...}", 400)
        return await self._do(self.controller.change, request.match_info["id"], data.get("name"), data.get("seconds"))

    @request_mapping(path="/{id}", method="DELETE", auth_required=False)
    async def http_remove(self, request):
        """
        ---
        description: Remove a timer, and its alarm's prompt from every screen
        tags:
        - Timers
        parameters:
        - name: "id"
          in: "path"
          required: true
          type: "integer"
        responses:
            "200":
                description: the timers
            "404":
                description: no such timer
        """
        return await self._do(self.controller.remove, request.match_info["id"])

    @request_mapping(path="/{id}/start", method="POST", auth_required=False)
    async def http_start(self, request):
        """
        ---
        description: Count down - a stopped timer from its own time, a paused one from where it is
        tags:
        - Timers
        parameters:
        - name: "id"
          in: "path"
          required: true
          type: "integer"
        responses:
            "200":
                description: the timers
            "404":
                description: no such timer
            "409":
                description: it has run out; answer it with /ok or /plus5
        """
        return await self._do(self.controller.start, request.match_info["id"])

    @request_mapping(path="/{id}/pause", method="POST", auth_required=False)
    async def http_pause(self, request):
        """
        ---
        description: Stop counting down, keeping the time left
        tags:
        - Timers
        parameters:
        - name: "id"
          in: "path"
          required: true
          type: "integer"
        responses:
            "200":
                description: the timers
            "404":
                description: no such timer
        """
        return await self._do(self.controller.pause, request.match_info["id"])

    @request_mapping(path="/{id}/add", method="POST", auth_required=False)
    async def http_add_time(self, request):
        """
        ---
        description: Add time to a timer that is counting down or paused, or take it away with a negative number, never leaving less than 10 seconds
        tags:
        - Timers
        parameters:
        - name: "id"
          in: "path"
          required: true
          type: "integer"
        - in: body
          name: body
          description: seconds to add, e.g. 60, 300 or -60
          required: true
          schema:
            type: object
            properties:
              seconds:
                type: number
        responses:
            "200":
                description: the timers
            "400":
                description: seconds is missing or not a number
            "404":
                description: no such timer
            "409":
                description: it isn't counting down or paused
        """
        data = await self._body(request)
        if not data or "seconds" not in data:
            return self._error("send {\"seconds\": n}", 400)
        return await self._do(self.controller.add_time, request.match_info["id"], data["seconds"])

    @request_mapping(path="/{id}/ok", method="POST", auth_required=False)
    async def http_ok(self, request):
        """
        ---
        description: Answer a timer that has run out - back to its own time, stopped - and take its prompt down on every screen
        tags:
        - Timers
        parameters:
        - name: "id"
          in: "path"
          required: true
          type: "integer"
        responses:
            "200":
                description: the timers
            "404":
                description: no such timer
        """
        return await self._do(self.controller.ok, request.match_info["id"])

    @request_mapping(path="/{id}/plus5", method="POST", auth_required=False)
    async def http_plus5(self, request):
        """
        ---
        description: Answer a timer that has run out with five minutes more, and take its prompt down on every screen
        tags:
        - Timers
        parameters:
        - name: "id"
          in: "path"
          required: true
          type: "integer"
        responses:
            "200":
                description: the timers
            "404":
                description: no such timer
        """
        return await self._do(self.controller.plus5, request.match_info["id"])
