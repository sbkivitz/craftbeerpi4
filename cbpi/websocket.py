import asyncio
import logging
import weakref
from collections import defaultdict

import aiohttp
from aiohttp import web
from cbpi.utils import json_dumps
from voluptuous import Schema


class CBPiWebSocket:
    def __init__(self, cbpi) -> None:
        self.cbpi = cbpi
        self._callbacks = defaultdict(set)
        self._clients = weakref.WeakSet()
        self.logger = logging.getLogger(__name__)
        self.cbpi.app.add_routes([web.get("/ws", self.websocket_handler)])
        self.cbpi.bus.register_object(self)
        # An open websocket is a request in progress for as long as the page
        # is open, and aiohttp waits for requests in progress before running
        # its cleanup - the minute it waited was longer than systemd allows a
        # stop, so cleanup never ran (DEFECTS F22). Pages reconnect on their
        # own when the server is back.
        self.cbpi.app.on_shutdown.append(self.close_all)

        # if self.cbpi.config.static.get("ws_push_all", False):
        self.cbpi.bus.register("#", self.listen)

    # Seconds to say goodbye to every open page, all together. aiohttp waits up
    # to ten seconds per page for its reply, one page after another - two
    # unresponsive browsers outlasted the whole stop systemd allows (DEFECTS
    # F33). A page that has not answered by then is simply disconnected.
    CLOSE_BUDGET = 2

    async def close_all(self, app=None):
        closing = [asyncio.ensure_future(
            ws.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b"Server shutdown"))
            for ws in list(self._clients)]
        if not closing:
            return
        done, pending = await asyncio.wait(closing, timeout=self.CLOSE_BUDGET)
        for task in pending:
            task.cancel()
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                self.logger.debug("closing a websocket on shutdown failed: %s", task.exception())

    async def listen(self, topic, **kwargs):
        data = dict(topic=topic, data=dict(**kwargs))
        self.logger.debug("PUSH %s " % data)
        self.send(data)

    def send(self, data, sorting=False):
        self.logger.debug("broadcast to ws clients. Data: %s" % data)
        for ws in self._clients:

            async def send_data(ws, data):
                try:
                    if sorting:
                        try:
                            data["data"].sort(key=lambda x: x.get("name").upper())
                        except:
                            pass
                    await ws.send_json(data=data, dumps=json_dumps)
                except Exception as e:
                    # A client going away is routine - a page reload does it -
                    # and every broadcast already queued for that socket fails
                    # at once, so one refresh produced two dozen ERROR lines.
                    # That buries the errors that matter during a fault, and on
                    # a Pi it is pointless SD card wear.
                    #
                    # Dropped either way: a socket that failed a send is not
                    # usable again. discard() is idempotent, so the handler's
                    # own cleanup still works.
                    if getattr(ws, "closed", False):
                        self.logger.debug(
                            "dropping update for a closed client: %s", e
                        )
                    else:
                        self.logger.error(
                            "Error with client %s: %s" % (ws, str(e))
                        )
                    self._clients.discard(ws)

            self.cbpi.app.loop.create_task(send_data(ws, data))

    def _snapshot(self):
        """Everything a page shows, as the messages a change would broadcast.

        A page loads its state once, from GET /system/, and afterwards hears
        only changes. Reconnecting after the server went away fetched nothing
        but the actors (the interface asks /actor/ws_update), so a page kept
        whatever it had loaded with: after a restart the dashboard showed a
        kettle still on automatic while the restarted server had every logic
        off. Seen 15 Oct on the 8000 sim - a boil kettle on "auto" sat unheated
        for 37 minutes, its heater correctly shown off beside it.

        So a page is told the current state the moment it connects, in the
        topics and shapes of the broadcasts that follow, and needs nothing new
        to understand it. Each part is built on its own: a controller that
        cannot answer costs that part, never the connection.
        """
        messages = []

        def add(topic, build):
            try:
                messages.append(dict(topic=topic, data=build()))
            except Exception as e:  # noqa: BLE001
                self.logger.debug("snapshot: no %s - %s", topic, e)

        def items(name):
            def build():
                controller = getattr(self.cbpi, name)
                data = [item.to_dict() for item in controller.data]
                if getattr(controller, "sorting", False):
                    data.sort(key=lambda x: str(x.get("name") or "").upper())
                return data
            return build

        add("configupdate", lambda: self.cbpi.config.get_state())
        add("kettleupdate", items("kettle"))
        add("fermenterupdate", items("fermenter"))
        add("fermenterstepupdate", lambda: self.cbpi.fermenter.get_fermenter_steps())
        add("actorupdate", items("actor"))
        add("sensorupdate", items("sensor"))
        add("mash_profile_update", lambda: self.cbpi.step.get_state())
        add("notificationupdate", lambda: self.cbpi.notification.get_state())
        return messages

    async def _send_snapshot(self, ws):
        for message in self._snapshot():
            try:
                await ws.send_json(data=message, dumps=json_dumps)
            except Exception as e:  # noqa: BLE001 - the page has gone; the loop below sees it
                self.logger.debug("snapshot to a new page failed: %s", e)
                return

    async def websocket_handler(self, request):

        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.add(ws)
        try:
            peername = request.transport.get_extra_info("peername")
            if peername is not None:

                host = peername[0]
                port = peername[1]
            else:
                host, port = "Unknowen"
            self.logger.info(
                "Client Connected - Host: %s Port: %s  - client count: %s "
                % (host, port, len(self._clients))
            )
        except Exception as e:
            pass

        try:
            await ws.send_json(data=dict(topic="connection/success"))
            await self._send_snapshot(ws)
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:

                    msg_obj = msg.json()
                    schema = Schema({"topic": str, "data": dict})
                    schema(msg_obj)

                    topic = msg_obj.get("topic")
                    data = msg_obj.get("data")
                    if topic == "close":
                        await ws.close()
                    else:
                        if data is not None:
                            await self.cbpi.bus.fire(topic=topic, **data)
                        else:
                            await self.cbpi.bus.fire(topic=topic)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    self.logger.error(
                        "ws connection closed with exception %s" % ws.exception()
                    )

        except Exception as e:
            self.logger.error("%s - Received Data %s" % (str(e), msg.data))

        finally:
            self._clients.discard(ws)

        self.logger.info("Web Socket Close")

        return ws
