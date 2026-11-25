import json

from aiohttp import web
from cbpi.api import request_mapping
from cbpi.controller.strike_controller import StrikeRefused
from cbpi.utils import json_dumps


class StrikeHttpEndpoints:
    """The strike water helper's endpoints (StrikeController), under /step2 with the steps' own: one prefix holds one
    set of routes, so StepHttpEndpoints takes these on.

    GET /step2/strike is the state, which every screen also hears on the websocket topic "strike". The commands answer
    200 with what was applied and the state; a refusal changes nothing and answers 400, 409 or 503 with the reason in
    "error", and every 409 but recipe_changed carries the state too. recipe_changed is START's refusal exactly:
    {"error", "name", "steps"}."""

    @staticmethod
    def _strike_answer(data, status=200):
        return web.json_response(data=data, status=status, dumps=json_dumps)

    @staticmethod
    async def _strike_body(request):
        """The request's JSON object, or None when it isn't one."""
        try:
            raw = await request.read()
            data = json.loads(raw) if raw.strip() else None
        except ValueError:  # not JSON, or not UTF-8
            return None
        return data if isinstance(data, dict) else None

    async def _strike_do(self, command, *args):
        try:
            return self._strike_answer(await command(*args))
        except StrikeRefused as refused:
            return self._strike_answer(refused.body(), status=refused.status)

    @request_mapping(path="/strike", method="GET", auth_required=False)
    async def http_strike_state(self, request):
        """
        ---
        description: The strike water helper - USE for the loaded recipe, the numbers it is worked out from and where each comes from, the forecasts for the strikes the brew-day screen shows, and whether CraftBeerPi is working USE out again. The same object as the websocket topic strike. See the strike water API contract for every field.
        tags:
        - Step
        responses:
            "200":
                description: the state
        """
        return self._strike_answer(self.cbpi.strike.get_state())

    @request_mapping(path="/strike", method="POST", auth_required=False)
    async def http_strike_use(self, request):
        """
        ---
        description: USE or BACK TO. USE sets the strike water step's Temp, BrewStart's Ready_Temp while it hasn't handed on, and the HLT's target while the strike water step heats it - by hand, the HLT's target - and keeps the strike water before the first USE for BACK TO, which puts it back. A Ready by BrewStart that heats the HLT itself (BrewStart 1.3.0 on) is the strike water step when the recipe has no other, its Ready_Temp the strike water's temperature.
        tags:
        - Step
        parameters:
        - in: body
          name: body
          required: true
          schema:
            type: object
            properties:
              do:
                type: string
              temp:
                type: number
              plan:
                type: string
              steps:
                type: array
                items:
                  type: string
        responses:
            "200":
                description: what was applied, and the state
            "400":
                description: bad_body
            "409":
                description: recipe_changed, off, moved, missing, plan_changed or back_changed - nothing changed
        """
        body = await self._strike_body(request)
        return await self._strike_do(self.cbpi.strike.strike, body)

    @request_mapping(path="/strike/water", method="POST", auth_required=False)
    async def http_strike_water(self, request):
        """
        ---
        description: All the water in the HLT, strike and sparge - BrewStart's Volume, in its Volume_Unit, or by hand the brew day's figure. At least the strike water and, once it has gone, the least that covers the HLT's element and coil (the state's inputs.least).
        tags:
        - Step
        parameters:
        - in: body
          name: body
          required: true
          schema:
            type: object
            properties:
              vol:
                type: number
              steps:
                type: array
                items:
                  type: string
        responses:
            "200":
                description: what was applied, with the Campden tablets BrewStart gives for it, and the state
            "400":
                description: bad_body, or too_little (with min, the least HLT water; mash, the mash water; left, what this would leave in the HLT; least and least_from)
            "409":
                description: recipe_changed, or off with nothing loaded
        """
        body = await self._strike_body(request)
        return await self._strike_do(self.cbpi.strike.set_water, body)

    @request_mapping(path="/strike/grain", method="POST", auth_required=False)
    async def http_strike_grain(self, request):
        """
        ---
        description: The grain's temperature - BrewStart's Grain_Temp, or by hand the brew day's figure. null blanks it, and the boil kettle's probe stands for it again. place keeps Brew Day's answer to where the grain spent the night for this brew, as inputs.grain_place shows it - a figure is indoors, and null with place garage the garage; null without place answers nothing, an earlier garage staying and an earlier indoors going.
        tags:
        - Step
        parameters:
        - in: body
          name: body
          required: true
          schema:
            type: object
            properties:
              grain:
                type: number
              place:
                type: string
                enum: [garage, indoors]
              steps:
                type: array
                items:
                  type: string
        responses:
            "200":
                description: what was applied, and the state
            "400":
                description: bad_body (a place that isn't garage or indoors, garage with a figure, indoors without one) or out_of_range
            "409":
                description: recipe_changed, or off with nothing loaded
        """
        body = await self._strike_body(request)
        return await self._strike_do(self.cbpi.strike.set_grain, body)

    @request_mapping(path="/strike/recipe", method="POST", auth_required=False)
    async def http_strike_recipe(self, request):
        """
        ---
        description: The recipe's figures, given on Brew Day where BeerSmith's are missing or changed in the recipe editor - the grain weight in the state's mass_unit, and the strike water to the mash tun (the mash water) in its vol_unit, either or both. Kept with the loaded recipe, in its mash_numbers, named in its typed and taken out of its lacks, so they're never taken for BeerSmith's. A figure BeerSmith gave is refused, unless edit is true - then BeerSmith's own is kept for back, which puts it back; a figure given the same as BeerSmith's own is BeerSmith's again. A MashInStep still to come has its own copy of a figure given cleared, so it reads the recipe's.
        tags:
        - Step
        parameters:
        - in: body
          name: body
          required: true
          schema:
            type: object
            properties:
              grain:
                type: number
              strike_vol:
                type: number
              edit:
                type: boolean
              back:
                type: array
                items:
                  type: string
              steps:
                type: array
                items:
                  type: string
        responses:
            "200":
                description: what was applied (with back, the figures put back to BeerSmith's own), and the state
            "400":
                description: bad_body, out_of_range (with what, low and high), or too_much - mash water that would leave the HLT less than the least that covers its element and coil (with what, hlt, left, least, least_from and most, the most mash water the HLT's water allows)
            "409":
                description: recipe_changed, off with nothing loaded, beersmith - the recipe has BeerSmith's own figure for it - or no_beersmith - there's no BeerSmith figure to go back to
        """
        body = await self._strike_body(request)
        return await self._strike_do(self.cbpi.strike.set_recipe, body)

    @request_mapping(path="/strike/suggest", method="GET", auth_required=False)
    async def http_strike_suggest(self, request):
        """
        ---
        description: A suggestion for the recipe's grain weight and mash water where it has none of its own, from BeerSmith's data - the recipe's kept BeerSmith file, else another Recipe Book recipe of the same name with a grain bill within 2%, else (the mash water) the kept file's water-to-grain ratio times the grain weight. Nothing is saved. Without file, the loaded recipe; with file, that Recipe Book recipe.
        tags:
        - Step
        parameters:
        - name: file
          in: query
          required: false
          type: string
        responses:
            "200":
                description: mass_unit, vol_unit, and grain and strike_vol, each null or {value, from (file, book or ratio), says}
            "404":
                description: not_found - no such Recipe Book recipe
        """
        return await self._strike_do(self.cbpi.strike.suggest, request.query.get("file"))

    @request_mapping(path="/strike/room", method="POST", auth_required=False)
    async def http_strike_room(self, request):
        """
        ---
        description: The room's temperature given on Brew Day for this brew, in the brewery's unit - the room the forecast takes while the boil kettle's probe gives no reading a room can have; once the probe reads within that range again, its reading is the room. null takes it back.
        tags:
        - Step
        parameters:
        - in: body
          name: body
          required: true
          schema:
            type: object
            properties:
              room:
                type: number
              steps:
                type: array
                items:
                  type: string
        responses:
            "200":
                description: what was applied, whether it is the room the forecast takes now (used), and the state
            "400":
                description: bad_body or out_of_range
            "409":
                description: recipe_changed, or off with nothing loaded
        """
        body = await self._strike_body(request)
        return await self._strike_do(self.cbpi.strike.set_room, body)

    @request_mapping(path="/strike/forecast", method="GET", auth_required=False)
    async def http_strike_forecast(self, request):
        """
        ---
        description: A forecast for any strike, without a search of its own - for the last answer's numbers, from the cases it found.
        tags:
        - Step
        parameters:
        - name: temp
          in: query
          required: true
          type: number
        responses:
            "200":
                description: the plan's id and the forecast
            "400":
                description: bad_temp
            "409":
                description: off, missing or working
            "503":
                description: failed - the worker gave no forecast
        """
        return await self._strike_do(self.cbpi.strike.forecast, request.query.get("temp"))
