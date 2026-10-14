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
        # An open page is a request in progress for as long as it stays open,
        # and aiohttp waits for requests in progress - up to a minute - before
        # it runs on_cleanup, where the actors are switched off. Pages
        # reconnect by themselves once the server is back.
        self.cbpi.app.on_shutdown.append(self.close_all)

        # if self.cbpi.config.static.get("ws_push_all", False):
        self.cbpi.bus.register("#", self.listen)

    # Seconds to close every open page, all together. Closing a page whose
    # last command is still being handled waits up to ten seconds for its
    # reply; one page after another, that alone outlasts the stop systemd
    # allows.
    CLOSE_BUDGET = 2

    async def close_all(self, app=None):
        """Close every open page as the server stops, so the shutdown is not held up."""
        closing = [
            asyncio.ensure_future(
                ws.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b"Server shutdown")
            )
            for ws in list(self._clients)
        ]
        if not closing:
            return
        done, pending = await asyncio.wait(closing, timeout=self.CLOSE_BUDGET)
        for task in pending:
            task.cancel()
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                self.logger.debug(
                    "Closing a websocket on shutdown failed: %s", task.exception()
                )

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
                    self.logger.error("Error with client %s: %s" % (ws, str(e)))

            self.cbpi.app.loop.create_task(send_data(ws, data))

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
