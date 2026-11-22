import json
import logging

from aiohttp import web
from cbpi.api import *
from cbpi.api.dataclasses import Props, Step
from cbpi.controller.step_controller import StartRefused, StepController, StepRunning


class StepHttpEndpoints:

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self.controller: StepController = cbpi.step
        self.cbpi.register(self, "/step2")

    # Check if this is still needed
    def create_dict(self, data):
        return dict(
            name=data["name"],
            id=data["id"],
            type=data.get("type"),
            status=data["status"],
            props=data["props"],
            state_text=data["instance"].get_state(),
        )

    @request_mapping(path="/", auth_required=False)
    async def http_get_all(self, request):
        """
        ---
        description: Get all steps from active brewing recipe
        tags:
        - Step
        responses:
            "200":
                description: successful operation
        """
        return web.json_response(data=self.controller.get_state())

    @request_mapping(path="/", method="POST", auth_required=False)
    async def http_add(self, request):
        """

        ---
        description: Add new step to the end of the active brewing recipe
        tags:
        - Step
        parameters:
        - in: body
          name: body
          description: Create a new step
          required: true
          schema:
            type: object
            properties:
              name:
                type: string
              type:
                type: string
              props:
                type: object
            example:
                name: "Mashstep"
                type: "MashStep"
                props:
                    Timer: "30"
                    Temp: "73"
                    Sensor: "2Sn46Fc6jRGvES9MuQXHdG"
                    AutoMode: "Yes"
                    Kettle: "PkVY88NrEX4VYg4H2tY4Ae"
        responses:
            "200":
                description: successful operation
        """

        data = await request.json()
        step = Step(
            name=data.get("name"),
            props=Props(data.get("props", {})),
            type=data.get("type"),
        )
        response_data = await self.controller.add(step)
        return web.json_response(data=response_data.to_dict())

    @request_mapping(path="/{id}", method="PUT", auth_required=False)
    async def http_update(self, request):
        """
        ---
        description: Update existing MashStep with given id
        tags:
        - Step
        parameters:
        - name: "id"
          in: "path"
          description: "Existing Step id"
          required: true
          type: "string"
        - in: body
          name: body
          description: Update Mashstep with existing id
          required: true
          schema:
            type: object
            properties:
              name:
                type: string
              type:
                type: string
              props:
                type: object
            example:
                name: "Mashstep"
                type: "MashStep"
                props:
                    Timer: "30"
                    Temp: "73"
                    Sensor: "2Sn46Fc6jRGvES9MuQXHdG"
                    AutoMode: "Yes"
                    Kettle: "PkVY88NrEX4VYg4H2tY4Ae"
        responses:
            "200":
                description: successful operation
            "409":
                description: the step is running; stop the profile first
        """

        data = await request.json()
        id = request.match_info["id"]
        step = Step(
            id, data.get("name"), Props(data.get("props", {})), data.get("type")
        )
        saved = await self.controller.update(step)
        if saved is None:
            # Refused: the step is running. The form stays open with their
            # changes and shows why: these words. The other pages are told in
            # a warning, which older pages show for 5 s.
            existing = self.controller.find_by_id(id)
            return web.json_response(
                status=409,
                data=dict(error=self.controller.EDIT_WHILE_RUNNING.format(
                    existing.name if existing is not None else step.name)),
            )
        return web.json_response(saved.to_dict())

    @request_mapping(path="/{id}", method="DELETE", auth_required=False)
    async def http_delete(self, request):
        """

        ---
        description: Delete existing MashStep with given id
        tags:
        - Step
        parameters:
        - name: "id"
          in: "path"
          description: "Existing Step id"
          required: true
          type: "string"
        responses:
            "200":
                description: successful operation
        """
        id = request.match_info["id"]
        await self.controller.delete(id)
        return web.Response(status=200)

    @request_mapping(path="/next", method="POST", auth_required=False)
    async def http_next(self, request):
        """

        ---
        description: Move to the next brewing step
        tags:
        - Step
        responses:
            "200":
                description: successful operation
        """

        await self.controller.next()
        return web.Response(status=200)

    @request_mapping(path="/precheck", auth_required=False)
    async def http_precheck(self, request):
        """
        ---
        description: |
          The pre-brew check, which the brew-day screen makes when START is held on a recipe not begun
          (PrecheckController). It changes nothing. ok is true when all three parts pass; each part has its own ok,
          and one that can't be worked out says why in error, with ok false.

          probes - whether each kettle's probe gives a reading (kettles only), the HLT, mash tun and boil kettle
          first. A reading is a number, published, no older than the probe's own limit (max_age), and a temperature
          a kettle can have (range, in the TEMP_UNIT setting's unit). For each kettle - kettle_id, kettle (its name),
          role (hlt, mt, bk or null), sensor_id, probe (the probe's name), ok, reason, value, age, max_age and
          no_reading_for. reason when ok is false - no_probe (none set; it fails), missing, unreadable, never,
          stale, not_a_number or out_of_range. no_reading_for is how long it hasn't given a reading, in seconds on
          the monotonic real clock sensor ages use, or null when that isn't known.

          clock - whether the system clock counts as set, by the rule the steps and the brew-day timers use
          (cbpi.api.system_clock), asked in a thread and kept 5 s. source - network (a time server, as timedatectl
          reports) or system (the computer's own clock, where there is no timedatectl). reason - synchronized,
          not_synchronized, unconfirmed (timedatectl failed, timed out, couldn't run or gave an odd answer; detail
          says which), no_timedatectl, or no_answer_yet. age - real seconds since it was asked.

          reminders - additions counts the boil additions of the loaded recipe - the reminders set in every boil
          step and the ones its import couldn't give a reminder - and without_reminder lists those, with name and
          minutes before the end of the boil. boil_steps is how many boil steps the profile has, and per_boil_step
          how many reminders each holds.
        tags:
        - Step
        responses:
            "200":
                description: the check's answer
                examples:
                    application/json:
                        ok: false
                        probes:
                            ok: true
                            unit: F
                            range: [-4.0, 230.0]
                            kettles:
                            - kettle_id: npqm66B5iuYbDt2KrvuDS5
                              kettle: HLT
                              role: hlt
                              sensor_id: afft72Vaoj33KfkXVqyiD8
                              probe: HLT Temp
                              ok: true
                              reason: null
                              value: 68.2
                              age: 0.4
                              max_age: 30.0
                              no_reading_for: null
                        clock:
                            ok: false
                            source: network
                            reason: not_synchronized
                            detail: null
                            age: 0.0
                        reminders:
                            ok: false
                            additions: 8
                            without_reminder:
                            - name: 1.50 oz Citra
                              minutes: 5.0
                            - name: 2.00 oz Citra
                              minutes: 0.0
                            boil_steps: 1
                            per_boil_step: 6
        """
        return web.json_response(data=await self.cbpi.precheck.check())

    @request_mapping(path="/move", method="PUT", auth_required=False)
    async def http_move(self, request):
        """
        ---
        description: Move step with given id in given direction (1 for down, -1 for up)
        tags:
        - Step
        parameters:
        - in: body
          name: body
          description: Move step with given id in given direction (1 for down, -1 for up)
          required: true
          schema:
            type: object
            properties:
              id:
                type: string
              direction:
                type: "integer"
                format: "int64"
            example:
                id: "RAuQR8UgWLueQtfXrWxShb"
                direction: -1
        responses:
            "200":
                description: successful operation
        """
        data = await request.json()
        await self.controller.move(data["id"], data["direction"])
        return web.Response(status=200)

    @request_mapping(path="/start", method="POST", auth_required=False)
    async def http_start(self, request):
        """
        ---
        description: |
          Start the brewing process - the next step, resuming a stopped one first.

          START on the brew-day screen says which recipe it was pressed on: the ids of every step of that recipe, in
          order, as {"steps": ["<id>", ...]}. So it never starts another recipe, one that another screen loaded a
          moment before. Given steps, nothing starts, and the answer is 409, when
          - they aren't exactly the loaded profile's ids, in order (error recipe_changed), or
          - they are, but a step of that recipe runs already: another screen started it a moment before (error
            already_running).
          The ids are checked first. The 409's body says what is loaded:
          {"error": "recipe_changed" or "already_running", "name": the loaded recipe's name, or null when nothing
          is loaded or it has no name, "steps": the loaded profile's ids, in order}.

          With no body, an empty one, or a JSON object without steps - as older screens and the other pages send -
          whatever is loaded starts, as it always has, and the answer is 200 even when a step runs already.

          A body that is neither empty nor a JSON object starts nothing, and the answer is 400 with error bad_body.
          The page meant something by it, and CraftBeerPi can't tell whether that was the recipe loaded now.
          Steps given as anything but that recipe's ids - null, say - are recipe_changed.
        tags:
        - Step
        parameters:
        - in: body
          name: body
          description: the ids of every step of the recipe START was pressed on, in order
          required: false
          schema:
            type: object
            properties:
              steps:
                type: array
                items:
                  type: string
            example:
                steps: ["RAuQR8UgWLueQtfXrWxShb", "PkVY88NrEX4VYg4H2tY4Ae", "2Sn46Fc6jRGvES9MuQXHdG"]
        responses:
            "200":
                description: started, or nothing to start
            "400":
                description: a body neither empty nor a JSON object - error bad_body - and nothing started
            "409":
                description: steps given, and nothing started - error recipe_changed, or already_running
        """
        raw = await request.read()
        given = {}
        if raw.strip():
            try:
                body = json.loads(raw)
            except ValueError:  # not JSON, or not UTF-8
                body = None
            if not isinstance(body, dict):
                logging.warning("Nothing started: START sent a body that isn't a JSON object")
                return web.json_response(status=400, data=dict(
                    error="bad_body",
                    detail="Send no body, or a JSON object such as {\"steps\": [...]}. Nothing was started."))
            if "steps" in body:
                given = dict(steps=body["steps"])
        try:
            await self.controller.start(**given)
        except StartRefused as refused:
            return web.json_response(status=409, data=dict(error=refused.reason, name=refused.name,
                                                           steps=refused.steps))
        return web.Response(status=200)

    @request_mapping(path="/stop", method="POST", auth_required=False)
    async def http_stop(self, request):
        """

        ---
        description: Stop the brewing process / step
        tags:
        - Step
        responses:
            "200":
                description: successful operation
        """

        await self.controller.stop()
        return web.Response(status=200)

    @request_mapping(path="/reset", method="POST", auth_required=False)
    async def http_reset(self, request):
        """

        ---
        description: Reset the brewing process / steps
        tags:
        - Step
        responses:
            "200":
                description: successful operation
            "409":
                description: a step is running; stop it first
        """

        if await self.controller.reset_all() is False:
            # Refused, and nothing changed: a page that asked can say so rather than wait for a reset that never comes.
            return web.json_response(status=409, data=dict(error=self.controller.RESET_WHILE_RUNNING))
        return web.Response(status=200)

    @request_mapping(path="/action/{id}", method="POST", auth_required=False)
    async def http_call_action(self, request):
        """
        ---
        description: Call action
        tags:
        - Step
        parameters:
        - name: "id"
          in: "path"
          description: "Step id"
          required: true
          type: "integer"
          format: "int64"
        - in: body
          name: body
          description: call action
          required: false
          schema:
            type: object
            properties:
              action:
                type: string
              parameter:
                type: "array"
                items:
                    type: string
        responses:
            "200":
                description: successful operation
        """
        data = await request.json()

        id = request.match_info["id"]
        action = data.get("action", data.get("name"))
        ok = await self.controller.call_action(
            id, action, data.get("parameter", [])
        )
        if ok is False:
            return web.json_response(
                {
                    "error": "action not run",
                    "detail": (
                        "'{}' did not run. Either the step is not running, or "
                        "that is not a declared action.".format(action)
                    ),
                },
                status=400,
            )
        return web.Response(status=200)

    @request_mapping(path="/clear", method="POST", auth_required=False)
    async def http_clear(self, request):
        """

        ---
        description: Clear All Steps from active brewing recipe
        tags:
        - Step
        responses:
            "200":
                description: successful operation
            "409":
                description: a step is running, and nothing changed - error step_running, detail, step
        """

        try:
            await self.controller.clear()
        except StepRunning as refused:
            return web.json_response(status=409, data=dict(error="step_running", detail=refused.detail,
                                                           step=refused.step))
        return web.Response(status=200)

    @request_mapping(path="/savetobook", method="POST", auth_required=False)
    async def http_savetobook(self, request):
        """

        ---
        description: Save Active Recipe to Recipe Book
        tags:
        - Step
        responses:
            "200":
                description: successful operation
        """
        await self.controller.savetobook()
        return web.Response(status=200)
