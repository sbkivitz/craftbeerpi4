import json
from urllib.parse import quote

from aiohttp import web
from cbpi.api import *
from cbpi.api.dataclasses import Kettle, Props
from cbpi.controller import kettle_file
from cbpi.controller.kettle_controller import KettleController

auth = False


def _attachment(filename):
    """A Content-Disposition naming the download: plain ASCII for any browser,
    and the name as it is (filename*) for one that reads it."""
    plain = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in filename)
    return "attachment; filename=\"{}\"; filename*=UTF-8''{}".format(plain, quote(filename, safe=""))


def _no_kettle(id):
    return web.json_response({"error": "no such kettle", "detail": "There's no kettle with the id {}.".format(id)},
                             status=404)


def _cannot_load(e):
    return web.json_response({"error": "cannot load this file", "detail": str(e)}, status=400)


def _fill(request):
    """The fill query parameter: True for fill=1, False for fill=0 or none, None for anything else.
    true/false, yes/no and on/off do too."""
    raw = request.query.get("fill")
    if raw is None:
        return False
    text = raw.strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return None


def _bad_fill(request):
    return web.json_response(
        {"error": "bad fill", "detail": "fill={} isn't 1, to fill in the logic's own values, or 0, to leave those "
                                        "settings empty.".format(request.query.get("fill"))},
        status=400,
    )


class KettleHttpEndpoints:

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self.controller: KettleController = cbpi.kettle
        self.cbpi.register(self, "/kettle")

    @request_mapping(path="/", auth_required=False)
    async def http_get_all(self, request):
        """

        ---
        description: Every kettle, and the kettle logics - each one's settings, its actions, and its blank_defaults, what it uses for each setting left empty, in the configured temperature unit
        tags:
        - Kettle
        responses:
            "200":
                description: "{data: the kettles, types: {logic: {name, properties, actions, blank_defaults: {setting: value}}}}"
        """
        return web.json_response(data=self.controller.get_state())

    @request_mapping(path="/", method="POST", auth_required=False)
    async def http_add(self, request):
        """
        ---
        description: add one Actor
        tags:
        - Kettle
        parameters:
        - in: body
          name: body
          description: Created an actor
          required: true

          schema:
            type: object

            properties:
              name:
                type: string
              sensor:
                type: "integer"
                format: "int64"
              heater:
                type: "integer"
                format: "int64"
              agitator:
                type: "integer"
                format: "int64"
              target_temp:
                type: "integer"
                format: "int64"
              type:
                type: string
              props:
                type: object
            example:
              name: "Kettle 1"
              type: "CustomKettleLogic"
              props: {}

        responses:
            "204":
                description: successful operation
        """
        data = await request.json()

        kettle = Kettle(
            name=data.get("name"),
            sensor=data.get("sensor"),
            heater=data.get("heater"),
            agitator=data.get("agitator"),
            props=Props(data.get("props", {})),
            type=data.get("type"),
        )
        response_data = await self.controller.add(kettle)
        return web.json_response(data=response_data.to_dict())

    @request_mapping(path="/{id}", method="PUT", auth_required=False)
    async def http_update(self, request):
        """
        ---
        description: Update an actor
        tags:
        - Kettle
        parameters:
        - name: "id"
          in: "path"
          description: "Actor ID"
          required: true
          type: "integer"
          format: "int64"
        - in: body
          name: body
          description: Update an actor
          required: false
          schema:
            type: object
            properties:
              name:
                type: string
              type:
                type: string
              config:
                props: object
        responses:
            "200":
                description: successful operation
        """
        id = request.match_info["id"]
        data = await request.json()
        kettle = Kettle(
            id=id,
            name=data.get("name"),
            sensor=data.get("sensor"),
            heater=data.get("heater"),
            agitator=data.get("agitator"),
            props=Props(data.get("props", {})),
            type=data.get("type"),
        )
        return web.json_response(data=(await self.controller.update(kettle)).to_dict())

    @request_mapping(path="/{id}", method="DELETE", auth_required=False)
    async def http_delete_one(self, request):
        """
        ---
        description: Delete an actor
        tags:
        - Kettle
        parameters:
        - name: "id"
          in: "path"
          description: "Actor ID"
          required: true
          type: "string"
        responses:
            "204":
                description: successful operation
        """
        id = request.match_info["id"]
        await self.controller.delete(id)
        return web.Response(status=204)

    @request_mapping(path="/{id}/on", method="POST", auth_required=False)
    async def http_on(self, request) -> web.Response:
        """

        ---
        description: Switch actor on
        tags:
        - Kettle
        parameters:
        - name: "id"
          in: "path"
          description: "Actor ID"
          required: true
          type: "string"

        responses:
            "204":
                description: successful operation
            "405":
                description: invalid HTTP Met
        """
        id = request.match_info["id"]
        await self.controller.start(id)
        return web.Response(status=204)

    @request_mapping(path="/{id}/off", method="POST", auth_required=False)
    async def http_off(self, request) -> web.Response:
        """

        ---
        description: Switch actor on
        tags:
        - Kettle

        parameters:
        - name: "id"
          in: "path"
          description: "Actor ID"
          required: true
          type: "string"

        responses:
            "204":
                description: successful operation
            "405":
                description: invalid HTTP Met
        """
        id = request.match_info["id"]
        # stop(), not off(). KettleController has start, stop and toggle - there
        # is no off, so this route answered every request with a 500 and the
        # text "'KettleController' object has no attribute 'off'". The interface
        # uses /toggle, which is why it survived; anything driving the API
        # directly did not.
        await self.controller.stop(id)
        return web.Response(status=204)

    @request_mapping(path="/{id}/toggle", method="POST", auth_required=False)
    async def http_toggle(self, request) -> web.Response:
        """

        ---
        description: Switch actor on
        tags:
        - Kettle

        parameters:
        - name: "id"
          in: "path"
          description: "Kettle ID"
          required: true
          type: "string"

        responses:
            "204":
                description: successful operation
            "405":
                description: invalid HTTP Met
        """
        id = request.match_info["id"]
        await self.controller.toggle(id)
        return web.Response(status=204)

    @request_mapping(path="/{id}/action", method="POST", auth_required=auth)
    async def http_action(self, request) -> web.Response:
        """

        ---
        description: Toogle an actor on or off
        tags:
        - Kettle
        parameters:
        - name: "id"
          in: "path"
          description: "Actor ID"
          required: true
          type: "integer"
          format: "int64"
        - in: body
          name: body
          description: Update an actor
          required: false
          schema:
            type: object
            properties:
              name:
                type: string
              parameter:
                type: object
        responses:
            "204":
                description: successful operation
        """
        actor_id = request.match_info["id"]
        data = await request.json()
        # Every other endpoint sends "action"; "name" is accepted too, for
        # whatever sends that key.
        action = data.get("action", data.get("name"))
        ok = await self.controller.call_action(
            actor_id, action, data.get("parameter")
        )

        # Report failure to the caller.
        #
        # Answering 204 whatever happened would acknowledge an action sent to
        # a stopped logic, or under a name that is not an action, as applied:
        # the dashboard would close its dialog, the brewer would believe the
        # value was in force, and nothing would have run. A notification is
        # not a substitute for the result of the request that asked for it.
        if ok is False:
            return web.json_response(
                {
                    "error": "action not run",
                    "detail": (
                        "'{}' did not run. Either the kettle's logic is not "
                        "running, or that is not a declared action.".format(action)
                    ),
                },
                status=400,
            )

        return web.Response(status=204)

    @request_mapping(path="/{id}/target_temp", method="POST", auth_required=auth)
    async def http_target(self, request) -> web.Response:
        """

        ---
        description: Toogle an actor on or off
        tags:
        - Kettle
        parameters:
        - name: "id"
          in: "path"
          description: "Actor ID"
          required: true
          type: "integer"
          format: "int64"
        - in: body
          name: body
          description: Update Temp
          required: true
          schema:
            type: object
            properties:
              temp:
                type: integer
        responses:
            "204":
                description: successful operation
        """
        id = request.match_info["id"]
        data = await request.json()
        await self.controller.set_target_temp(id, data.get("temp"))
        return web.Response(status=204)

    @request_mapping(path="/{id}/file", method="GET", auth_required=False)
    async def http_settings_file(self, request) -> web.Response:
        """

        ---
        description: One kettle's settings as a file - its logic, the logic's settings, and its sensor, heater and agitator, each by id and by name - named after the kettle
        tags:
        - Kettle
        parameters:
        - name: "id"
          in: "path"
          description: "Kettle ID"
          required: true
          type: "string"
        responses:
            "200":
                description: the kettle settings file, as an attachment
            "404":
                description: no such kettle
        """
        id = request.match_info["id"]
        try:
            filename, data = self.controller.settings_file(id)
        except kettle_file.NoSuchKettle:
            return _no_kettle(id)
        return web.Response(
            text=json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            content_type="application/json",
            headers={"Content-Disposition": _attachment(filename)},
        )

    @request_mapping(path="/{id}/file/preview", method="POST", auth_required=False)
    async def http_preview_file(self, request) -> web.Response:
        """

        ---
        description: What loading a kettle settings file into this kettle would change - its logic, each setting and each link, old and new, and what would be left out and why. Its blank lists each setting loading would leave empty, with the value the logic uses for it then (null where it gives none). Changes nothing. Its token is what loading it takes.
        tags:
        - Kettle
        parameters:
        - name: "id"
          in: "path"
          description: "Kettle ID"
          required: true
          type: "string"
        - name: "fill"
          in: "query"
          description: "1: fill each setting loading would leave empty with the value the logic uses for it, so it shows as a change and loading saves it. 0, or none: leave them empty. A setting the logic gives no value for stays empty."
          required: false
          type: "string"
        - in: body
          name: body
          description: the file, as it was saved
          required: true
          schema:
            type: object
        responses:
            "200":
                description: what loading the file would change
            "400":
                description: a file that can't be loaded, and why, or a fill that isn't 1 or 0
            "404":
                description: no such kettle
        """
        return await self._preview(request, request.match_info["id"])

    @request_mapping(path="/file/preview", method="POST", auth_required=False)
    async def http_preview_new(self, request) -> web.Response:
        """

        ---
        description: What a kettle settings file would make a new kettle - for the kettle form to fill itself in from. Its blank lists each setting the new kettle would have empty, with the value the logic uses for it then (null where it gives none). Changes nothing.
        tags:
        - Kettle
        parameters:
        - name: "fill"
          in: "query"
          description: "1: fill each setting the new kettle would have empty with the value the logic uses for it. 0, or none: leave them empty. A setting the logic gives no value for stays empty."
          required: false
          type: "string"
        - in: body
          name: body
          description: the file, as it was saved
          required: true
          schema:
            type: object
        responses:
            "200":
                description: the new kettle as the file would make it
            "400":
                description: a file that can't be loaded, and why, or a fill that isn't 1 or 0
        """
        return await self._preview(request, None)

    async def _preview(self, request, id):
        fill = _fill(request)
        if fill is None:
            return _bad_fill(request)
        try:
            return web.json_response(self.controller.preview_file(id, await request.text(), fill))
        except kettle_file.NoSuchKettle:
            return _no_kettle(id)
        except kettle_file.KettleFileError as e:
            return _cannot_load(e)

    @request_mapping(path="/{id}/file", method="POST", auth_required=False)
    async def http_load_file(self, request) -> web.Response:
        """

        ---
        description: Load a kettle settings file into this kettle, as its preview showed. Refused while the kettle's AUTO is running, and if the kettle has changed since the preview.
        tags:
        - Kettle
        parameters:
        - name: "id"
          in: "path"
          description: "Kettle ID"
          required: true
          type: "string"
        - name: "token"
          in: "query"
          description: "the token of the preview being confirmed"
          required: true
          type: "string"
        - name: "fill"
          in: "query"
          description: "as for the preview being confirmed: 1 if it filled in the logic's own values, else 0 or none"
          required: false
          type: "string"
        - in: body
          name: body
          description: the file, as it was saved - the one the preview was of
          required: true
          schema:
            type: object
        responses:
            "200":
                description: loaded - the kettle as it is now, and what changed
            "400":
                description: a file that can't be loaded, and why, no token, or a fill that isn't the preview's
            "404":
                description: no such kettle
            "409":
                description: not loaded - the kettle's AUTO is running, or the kettle has changed since the preview
        """
        id = request.match_info["id"]
        token = request.query.get("token", "")
        fill = _fill(request)
        if fill is None:
            return _bad_fill(request)
        text = await request.text()
        if not token:
            return web.json_response(
                {
                    "error": "no preview",
                    "detail": "Look at what the file would change first (POST /kettle/{}/file/preview), "
                              "then load it with that preview's token.".format(id),
                },
                status=400,
            )
        try:
            kettle, result = await self.controller.load_file(id, text, token, fill)
        except kettle_file.NoSuchKettle:
            return _no_kettle(id)
        except kettle_file.KettleFileError as e:
            return _cannot_load(e)
        except kettle_file.KettleFileRefused as e:
            return web.json_response({"error": "not loaded", "reason": e.reason, "detail": str(e)}, status=409)
        return web.json_response({"kettle": kettle.to_dict(), "changes": result["changes"], "plan": result})
