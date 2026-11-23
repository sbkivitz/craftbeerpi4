import json

from aiohttp import web
from cbpi.api import request_mapping
from cbpi.controller.figures_controller import FiguresRefused
from cbpi.utils import json_dumps


class FiguresHttpEndpoints:
    """Figures from past brews (FiguresController), under /figures: GET / is the brews and their figures, GET
    /brews/{id}?figure= one brew's graph for one figure, POST /{figure}/use, /back and /out change what a figure goes
    to. A refusal changes nothing and answers 400, 404, 409 or 503 with the reason in "error"; every 409 carries the
    figures as they are now. Every screen hears the websocket topic "figures" after each change."""

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self.controller = cbpi.figures
        self.cbpi.register(self, url_prefix="/figures")

    @staticmethod
    def _answer(data, status=200):
        return web.json_response(data=data, status=status, dumps=json_dumps)

    def _port(self, request):
        """The port this request came in on: where this CraftBeerPi's own BrewScribe answers."""
        try:
            name = request.transport.get_extra_info("sockname")
            if name and len(name) >= 2 and isinstance(name[1], int):
                return name[1]
        except Exception:  # noqa: BLE001 - the configured port below
            pass
        return int(self.cbpi.static_config.get("port", 8000))

    @staticmethod
    async def _body(request):
        try:
            raw = await request.read()
            data = json.loads(raw) if raw.strip() else None
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    async def _do(self, command, *args):
        try:
            return self._answer(await command(*args))
        except FiguresRefused as refused:
            return self._answer(refused.body(), status=refused.status)

    @request_mapping(path="/", method="GET", auth_required=False)
    async def http_figures(self, request):
        """
        ---
        description: Every past brew in the logs and BrewScribe, newest first, each with its figures (HLT heating, strike water, mash rests, grain absorption, boil kettle), and each figure worked out from the brews beside the setting it would change. See the Figures API contract for every field.
        tags:
        - Figures
        responses:
            "200":
                description: the brews and their figures
            "503":
                description: failed - the logs could not be read
        """
        return await self._do(self.controller.state, self._port(request))

    @request_mapping(path="/brews/{id}", method="GET", auth_required=False)
    async def http_brew(self, request):
        """
        ---
        description: One brew's graph for one figure - its vessel's probe, target, element and step marks over the stretch the figure comes from, with BrewScribe's volume readings.
        tags:
        - Figures
        parameters:
        - name: id
          in: path
          required: true
          type: string
        - name: figure
          in: query
          required: true
          type: string
          enum: [hlt, strike, mash, grain, boil]
        responses:
            "200":
                description: the graph
            "404":
                description: no_brew or no_figure
        """
        return await self._do(self.controller.graph, request.match_info["id"], request.query.get("figure"),
                              self._port(request))

    @request_mapping(path="/{figure}/use", method="POST", auth_required=False)
    async def http_use(self, request):
        """
        ---
        description: USE - sets the figure's setting to what the brews measured (hlt BREWSTART_EFFICIENCY, strike the strike water box's tun_share, grain GRAIN_ABSORPTION), with the value and brews the page showed.
        tags:
        - Figures
        parameters:
        - in: body
          name: body
          required: true
          schema:
            type: object
            properties:
              value:
                type: number
              brews:
                type: array
                items:
                  type: string
        responses:
            "200":
                description: what was applied, and the figures
            "400":
                description: bad_body
            "404":
                description: no_figure
            "409":
                description: brews_changed, not_offered or brewstart_old - nothing changed
        """
        body = await self._body(request)
        return await self._do(self.controller.use, request.match_info["figure"], body, self._port(request))

    @request_mapping(path="/{figure}/back", method="POST", auth_required=False)
    async def http_back(self, request):
        """
        ---
        description: BACK TO - puts back what the setting held before the first USE, with the value in use the page showed.
        tags:
        - Figures
        parameters:
        - in: body
          name: body
          required: true
          schema:
            type: object
            properties:
              value:
                type: number
        responses:
            "200":
                description: what was applied, and the figures
            "400":
                description: bad_body
            "404":
                description: no_figure
            "409":
                description: not_in_use or back_changed - nothing changed
        """
        body = await self._body(request)
        return await self._do(self.controller.back, request.match_info["figure"], body, self._port(request))

    @request_mapping(path="/{figure}/out", method="POST", auth_required=False)
    async def http_out(self, request):
        """
        ---
        description: LEAVE OUT (out true) or PUT BACK (out false) one brew from one figure.
        tags:
        - Figures
        parameters:
        - in: body
          name: body
          required: true
          schema:
            type: object
            properties:
              brew:
                type: string
              out:
                type: boolean
        responses:
            "200":
                description: the figures
            "400":
                description: bad_body
            "404":
                description: no_brew or no_figure
        """
        body = await self._body(request)
        return await self._do(self.controller.out, request.match_info["figure"], body, self._port(request))
