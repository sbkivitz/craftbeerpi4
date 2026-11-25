import json
import logging

from aiohttp import web
from cbpi.api import *
from cbpi.api import presence
from cbpi.api.dataclasses import Props, Step
from cbpi.controller.step_controller import (ActionRefused, BookRefused, OrderRefused, StartRefused, StepController,
                                             StepRunning)
from cbpi.http_endpoints.http_strike import StrikeHttpEndpoints


class StepHttpEndpoints(StrikeHttpEndpoints):
    """The /step2 endpoints: the mash profile's, and the strike water helper's (StrikeHttpEndpoints)."""

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
        description: |
          Add a new step to the active brewing recipe: last, or at index (0 is first; the number of steps is last).
          index missing or null: last. An index that isn't a whole number from 0 to the number of steps answers 400
          with error bad_body. One at or above a step that has begun - done, running, stopped or failed - answers 409
          with error begun: {"error": "begun", "detail": "<name> has begun, so it can't move, and no step can go
          above it.", "step": "<name>"}, naming the last step that has begun. Either way nothing is added.
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
              index:
                type: integer
            example:
                name: "Mashstep"
                type: "MashStep"
                index: 0
                props:
                    Timer: "30"
                    Temp: "73"
                    Sensor: "2Sn46Fc6jRGvES9MuQXHdG"
                    AutoMode: "Yes"
                    Kettle: "PkVY88NrEX4VYg4H2tY4Ae"
        responses:
            "200":
                description: the step as saved
            "400":
                description: index isn't a whole number from 0 to the number of steps - error bad_body
            "409":
                description: index is at or above a step that has begun - error begun
        """

        data = await request.json()
        step = Step(
            name=data.get("name"),
            props=Props(data.get("props", {})),
            type=data.get("type"),
        )
        try:
            response_data = await self.controller.add(step, index=data.get("index"))
        except OrderRefused as refused:
            return web.json_response(status=refused.status, data=refused.body())
        return web.json_response(data=response_data.to_dict())

    @request_mapping(path="/order", method="PUT", auth_required=False)
    async def http_order(self, request):
        """
        ---
        description: |
          Put the brew steps in a new order, in one change: every loaded step's id, in the new order. Saved once and
          sent to every page once, on step_update. The same order answers 200 and changes nothing.
          Refused, with nothing changed:
          - 400 {"error": "bad_body", "detail"}: not a JSON object, or ids not a list of strings;
          - 409 {"error": "steps_changed", "steps": [the loaded ids, in order]}: the ids aren't exactly the loaded
            steps' - one missing, extra or repeated;
          - 409 {"error": "begun", "detail": "<name> has begun, so it can't move, and no step can go above it.",
            "step": "<name>"}: a step that has begun - done, running, stopped or failed - would move, or a step would
            go above one. The steps up to the last one that has begun keep their places; the steps after it can be
            put in any order, while a step runs too.
        tags:
        - Step
        parameters:
        - in: body
          name: body
          description: every loaded step's id, in the new order
          required: true
          schema:
            type: object
            properties:
              ids:
                type: array
                items:
                  type: string
            example:
                ids: ["PkVY88NrEX4VYg4H2tY4Ae", "RAuQR8UgWLueQtfXrWxShb", "2Sn46Fc6jRGvES9MuQXHdG"]
        responses:
            "200":
                description: '{"changed": true or false, "steps": [the ids, in the order now]}'
            "400":
                description: not a JSON object, or ids not a list of strings - error bad_body
            "409":
                description: ids not exactly the loaded steps' (steps_changed), or a step that has begun moved or
                  passed (begun)
        """
        try:
            data = await request.json()
        except ValueError:  # not JSON, or not UTF-8
            data = None
        if not isinstance(data, dict):
            return web.json_response(status=400, data=dict(
                error="bad_body", detail='Send {"ids": [...]}: every step\'s id, in the new order.'))
        try:
            changed = await self.controller.reorder(data.get("ids"))
        except OrderRefused as refused:
            return web.json_response(status=refused.status, data=refused.body())
        return web.json_response(data=dict(changed=changed, steps=[step.id for step in self.controller.profile]))

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

        # The brewer's NEXT: the one press that moves a step holding the water on (cbpi.api.presence).
        with presence.press("next"):
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
            "400":
                description: >-
                    the action didn't run. error "action refused": the step said no, and detail is its own reason,
                    in the brewer's language (4.7.7.post61). error "action not run": no step has that id, it doesn't
                    declare that action, or the action failed.
        """
        data = await request.json()

        id = request.match_info["id"]
        action = data.get("action", data.get("name"))
        try:
            # A person's press: a step that holds the water may move on from its own button (cbpi.api.presence).
            with presence.press("step action"):
                ok = await self.controller.run_action(
                    id, action, data.get("parameter", [])
                )
        except ActionRefused as refused:
            # The step's own words for why not, so a page can show them. A page tells this core by the error: an
            # older one answers every 400 here as "action not run".
            return web.json_response({"error": "action refused", "detail": refused.reason}, status=400)
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
        description: |
          Keep the brew steps in the Recipe Book, and answer {"file": the book file's id}.
          - {"file": id}: in place of that book recipe, named as the steps are;
          - {"name": "..."}: as a new book recipe of that name, which the steps are then called too;
          - no body, or {}: as a new book recipe named as the steps are, as before.
          With file or name the brew steps then know the book recipe: their basic data's book_file, and
          book_modified, the file's modified time as GET /recipe/ gives it. With no body they don't.
          What is written is the recipe, not this brew: a BrewStart's day given as a date goes in as Tomorrow at
          the same time, the strike water as it was before the first USE, and BrewStart's grain temperature blank,
          while the brew steps keep all three.
          Refused, with nothing changed:
          - 400 {"error": "bad_body", "detail"}: not a JSON object, a key other than file and name, both, a file that
            can't be a book recipe's id, or a name that is blank or not text;
          - 404 {"error": "not_in_book", "detail", "file"}: the book has no recipe with that file.
        tags:
        - Step
        parameters:
        - in: body
          name: body
          description: the book recipe to replace, or the name of a new one; none for a new one named as the steps
          required: false
          schema:
            type: object
            properties:
              file:
                type: string
              name:
                type: string
            example:
                file: "YhweWUoDCyYKB887DJ9EXd"
        responses:
            "200":
                description: '{"file": the book file''s id}'
            "400":
                description: the body can't be used - error bad_body
            "404":
                description: the book has no recipe with that file - error not_in_book
        """
        data = {}
        raw = await request.read() if request.body_exists else b""
        if raw.strip():
            try:
                data = json.loads(raw)
            except ValueError:  # not JSON, or not UTF-8
                data = None
            if not isinstance(data, dict) or set(data) - {"file", "name"}:
                return web.json_response(status=400, data=dict(
                    error="bad_body", detail='Send {"file": id} to replace a book recipe, {"name": "..."} to save '
                                             'the steps as a new one, or nothing.'))
        try:
            book_id = await self.controller.savetobook(file=data.get("file"), name=data.get("name"))
        except BookRefused as refused:
            return web.json_response(status=refused.status, data=refused.body())
        return web.json_response(data=dict(file=book_id))
