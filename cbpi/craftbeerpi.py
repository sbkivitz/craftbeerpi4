
import asyncio
import socket
import sys

try:
    from asyncio import WindowsSelectorEventLoopPolicy, set_event_loop_policy
except ImportError:
    pass
import json
import logging
import os
from os import urandom

import shortuuid
from aiohttp import web
from aiohttp_auth import auth
from aiohttp_session import session_middleware
from aiohttp_session.cookie_storage import EncryptedCookieStorage
from aiohttp_swagger import setup_swagger
from cbpi import __codename__, __version__
from cbpi.api.dataclasses import NotificationType
from cbpi.api.exceptions import CBPiException
from cbpi.api.sd_notify import Heartbeat
from cbpi.controller.actor_controller import ActorController
from cbpi.controller.config_controller import ConfigController
from cbpi.controller.dashboard_controller import DashboardController
from cbpi.controller.fermentation_controller import FermentationController
from cbpi.controller.fermenter_recipe_controller import \
    FermenterRecipeController
from cbpi.controller.job_controller import JobController
from cbpi.controller.kettle_controller import KettleController
from cbpi.controller.log_file_controller import LogController
from cbpi.controller.notification_controller import NotificationController
from cbpi.controller.plugin_controller import PluginController
from cbpi.controller.recipe_controller import RecipeController
from cbpi.controller.satellite_controller import SatelliteController
from cbpi.controller.sensor_controller import SensorController
from cbpi.controller.step_controller import StepController
from cbpi.controller.system_controller import SystemController
from cbpi.controller.upload_controller import UploadController
from cbpi.eventbus import CBPiEventBus
from cbpi.http_endpoints.http_actor import ActorHttpEndpoints
from cbpi.http_endpoints.http_config import ConfigHttpEndpoints
from cbpi.http_endpoints.http_dashboard import DashBoardHttpEndpoints
from cbpi.http_endpoints.http_fermentation import FermentationHttpEndpoints
from cbpi.http_endpoints.http_fermenterrecipe import \
    FermenterRecipeHttpEndpoints
from cbpi.http_endpoints.http_kettle import KettleHttpEndpoints
from cbpi.http_endpoints.http_log import LogHttpEndpoints
from cbpi.http_endpoints.http_login import Login
from cbpi.http_endpoints.http_notification import NotificationHttpEndpoints
from cbpi.http_endpoints.http_plugin import PluginHttpEndpoints
from cbpi.http_endpoints.http_recipe import RecipeHttpEndpoints
from cbpi.http_endpoints.http_sensor import SensorHttpEndpoints
from cbpi.http_endpoints.http_step import StepHttpEndpoints
from cbpi.http_endpoints.http_system import SystemHttpEndpoints
from cbpi.http_endpoints.http_upload import UploadHttpEndpoints
from cbpi.utils import *
from cbpi.websocket import CBPiWebSocket
from voluptuous import MultipleInvalid
from voluptuous.schema_builder import message

logger = logging.getLogger(__name__)

@web.middleware
async def error_middleware(request, handler):
    try:
        response = await handler(request)
        if response.status != 404:
            return response
        # A handler that deliberately returned a 404 with its own body has already
        # answered; only aiohttp's bare not-found response carries .message.
        message = getattr(response, "message", None)
        if message is None:
            return response
    except web.HTTPException as ex:
        if ex.status != 404:
            raise
        message = ex.reason
    except CBPiException as ex:
        message = str(ex)
        return web.json_response(status=500, data={'error': message})
    except MultipleInvalid as ex:
        return web.json_response(status=500, data={'error': str(ex)})
    except Exception as ex:
        return web.json_response(status=500, data={'error': str(ex)})
    except web.GracefulExit:
        return web.json_response(status=500, data={'error': "GracefulExit"})
    except asyncio.exceptions.CancelledError:
        return web.json_response(status=500, data={'error': "CancelledError"})

    # Not found stays not found. This previously returned 500, which made every
    # missing route or file look like a server fault and left handlers unable to
    # report "no such thing" at all.
    return web.json_response(status=404, data={'error': message})


@web.middleware
async def _ui_cache_headers(request, handler):
    """Tell the browser how long it may trust each interface file.

    aiohttp's web.static sends ETag and Last-Modified but no Cache-Control.
    With no directive, browsers fall back to heuristic caching and may reuse
    index.html without asking the server at all.

    That is unsafe here specifically because index.html names the hashed bundle
    to load, and upgrading the interface deletes the old bundle. A browser
    holding a stale index.html therefore asks for a file that no longer exists:
    the shell renders, the script 404s, nothing mounts, and the page is black.
    It stays black until someone knows to hard-refresh - which on a brewery
    tablet, after an upgrade, looks exactly like the controller having died.

    So the entry point is always revalidated, while the content-addressed
    assets beneath it are cached hard: their names change whenever their
    contents do, so they can never go stale.
    """
    response = await handler(request)
    try:
        if "Cache-Control" not in response.headers:
            hashed = any(
                seg in request.path
                for seg in ("/static/js/", "/static/css/", "/static/media/")
            )
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable" if hashed else "no-cache"
            )
    except (AttributeError, TypeError):
        # Streamed or already-sent responses have no mutable headers. Serving
        # the file matters more than labelling it, so never fail the request.
        pass
    return response


def _send_static_files_in_fresh_chunks():
    """Where the OS has no sendfile, serve files in chunks that cannot change.

    aiohttp's FileResponse hands a file to loop.sendfile(). Without
    os.sendfile - on Windows - asyncio falls back to reading each 16 KB block
    into ONE reused bytearray and passing it to transport.write(). Since
    Python 3.12 the selector transport queues what it cannot send at once by
    reference, not as a copy (python/cpython#160321), so the next block
    overwrites data still waiting to go. A browser that reads slower than the
    server writes - one busy parsing - received the 6 MB interface bundle at
    the right length with stretches of it from further on in the file,
    failed to parse it ("Unexpected token"), and showed a black page until
    reloaded (DEFECTS O1). Six slow readers at once: 18 of 18 downloads
    damaged. aiohttp's own fallback, which this switches on, reads a new bytes
    object per chunk; 0 of 36 damaged.

    Linux - the Pi - has os.sendfile: the kernel sends the file with no buffer
    in between, so nothing changes there.
    """
    if hasattr(os, "sendfile"):
        return
    try:
        from aiohttp import web_fileresponse
    except ImportError:
        return
    if hasattr(web_fileresponse, "NOSENDFILE"):
        web_fileresponse.NOSENDFILE = True
    else:
        logging.getLogger(__name__).warning(
            "This aiohttp has no NOSENDFILE switch: interface files may reach "
            "browsers damaged on this platform (DEFECTS O1)")


class CraftBeerPi:

    def __init__(self, configFolder):

        operationsystem= sys.platform
        if operationsystem.startswith('win'):
            set_event_loop_policy(WindowsSelectorEventLoopPolicy())
        _send_static_files_in_fresh_chunks()

        self.path = os.sep.join(os.path.abspath(__file__).split(os.sep)[:-1])  # The path to the package dir
        
        self.version = __version__
        self.codename = __codename__

        self.config_folder = configFolder
        self.static_config = load_config(configFolder.get_file_path("config.yaml"))
        
        logger.info("Init CraftBeerPI")

        policy = auth.SessionTktAuthentication(urandom(32), 60, include_ip=True)
        middlewares = [web.normalize_path_middleware(), session_middleware(EncryptedCookieStorage(urandom(32))),
                       auth.auth_middleware(policy), error_middleware]
        # max upload size increased to 5 Mb. Default is 1 Mb -> config and svg upload
        self.app = web.Application(middlewares=middlewares, client_max_size=5*1024*1024)
        self.app["cbpi"] = self

        self._setup_shutdownhook()
        self.initializer = []

        self.bus = CBPiEventBus(self.app.loop, self)
        self.job = JobController(self)
        self.config = ConfigController(self)
        self.ws = CBPiWebSocket(self)
        self.actor = ActorController(self)
        self.sensor = SensorController(self)
        self.plugin = PluginController(self)
        self.log = LogController(self)
        self.system = SystemController(self)
        self.kettle = KettleController(self)
        self.fermenter : FermentationController = FermentationController(self)
        self.step : StepController = StepController(self)
        self.recipe : RecipeController = RecipeController(self)
        self.fermenterrecipe : FermenterRecipeController = FermenterRecipeController(self)
        self.upload : UploadController = UploadController(self)
        self.notification : NotificationController = NotificationController(self)
        self.satellite = None
        if str(self.static_config.get("mqtt", False)).lower() == "true":
            self.satellite: SatelliteController = SatelliteController(self)
        self.dashboard = DashboardController(self)

        self.http_step = StepHttpEndpoints(self)
        self.http_recipe = RecipeHttpEndpoints(self)
        self.http_fermenterrecipe = FermenterRecipeHttpEndpoints(self)
        self.http_sensor = SensorHttpEndpoints(self)
        self.http_config = ConfigHttpEndpoints(self)
        self.http_actor = ActorHttpEndpoints(self)
        self.http_kettle = KettleHttpEndpoints(self)
        self.http_dashboard = DashBoardHttpEndpoints(self)
        self.http_plugin = PluginHttpEndpoints(self)
        self.http_system = SystemHttpEndpoints(self)
        self.http_log = LogHttpEndpoints(self)
        self.http_notification = NotificationHttpEndpoints(self)
        self.http_upload = UploadHttpEndpoints(self)
        self.http_fermenter = FermentationHttpEndpoints(self)

        self.login = Login(self)

    def _setup_shutdownhook(self):
        self.shutdown = False

        async def on_cleanup(app):
            self.shutdown = True

        self.app.on_cleanup.append(on_cleanup)

    def register_on_startup(self, obj):

        for method in [getattr(obj, f) for f in dir(obj) if
                       callable(getattr(obj, f)) and hasattr(getattr(obj, f), "on_startup")]:
            name = method.__getattribute__("name")
            order = method.__getattribute__("order")
            self.initializer.append(dict(name=name, method=method, order=order))

    def register(self, obj, url_prefix=None, static=None):

        '''
        This method parses the provided object
        
        :param obj: the object wich will be parsed for registration 
        :param url_prefix: that prefix for HTTP Endpoints
        :return: None
        '''
        self.register_http_endpoints(obj, url_prefix, static)
        self.bus.register_object(obj)
        # self.ws.register_object(obj)
        self.job.register_background_task(obj)
        self.register_on_startup(obj)

    def register_http_endpoints(self, obj, url_prefix=None, static=None):

        if url_prefix is None:
            logger.debug(
                "URL Prefix is None for %s. No endpoints will be registered. Please set / explicit if you want to add it to the root path" % obj)
            return
        routes = []
        for method in [getattr(obj, f) for f in dir(obj) if
                       callable(getattr(obj, f)) and hasattr(getattr(obj, f), "route")]:
            http_method = method.__getattribute__("method")
            path = method.__getattribute__("path")
            class_name = method.__self__.__class__.__name__
            logger.debug(
                "Register Endpoint : %s.%s %s %s%s " % (class_name, method.__name__, http_method, url_prefix, path))

            def add_post():
                routes.append(web.post(method.__getattribute__("path"), method))

            def add_get():
                routes.append(web.get(method.__getattribute__("path"), method, allow_head=False))

            def add_delete():
                routes.append(web.delete(path, method))

            def add_put():
                routes.append(web.put(path, method))

            switcher = {
                "POST": add_post,
                "GET": add_get,
                "DELETE": add_delete,
                "PUT": add_put
            }
            switcher[http_method]()

        if url_prefix != "/":
            logger.debug("URL Prefix: %s " % (url_prefix,))
            sub = web.Application()
            sub.add_routes(routes)
            if static is not None:
                # Only subapps that serve interface files get the cache policy;
                # API routes are left exactly as they were.
                sub.middlewares.append(_ui_cache_headers)
                sub.add_routes([web.static('/static', static, show_index=True)])
            self.app.add_subapp(url_prefix, sub)
        else:

            self.app.add_routes(routes)

    def _swagger_setup(self):
        '''
        Internal method to expose REST API documentation by swagger
        
        :return: 
        '''
        long_description = """
        This is the api for CraftBeerPi 4
        """
        setup_swagger(self.app,
                      description=long_description,
                      title="CraftBeerPi 4",
                      api_version=self.version,
                      contact="avollkopf@web.de")


    def notify(self, title: str, message: str, type: NotificationType = NotificationType.INFO, action=[]) -> None:
        self.notification.notify(title, message, type, action)
        
    def push_update(self, topic, data, retain=False) -> None:

        if self.satellite is not None:
            background_tasks = set()
            task = asyncio.create_task(self.satellite.publish(topic=topic, message=json.dumps(data), retain=retain))
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)

    async def call_initializer(self, app):
        self.initializer = sorted(self.initializer, key=lambda k: k['order'])
        for i in self.initializer:
            logger.info("CALL INITIALIZER %s - %s " % (i["name"], i["method"].__name__))
            await i["method"]()

    def _print_logo(self):
        from pyfiglet import Figlet
        f = Figlet(font='big')
        logger.warning("\n%s" % f.renderText("CraftBeerPi %s " % self.version))
        logger.warning("www.CraftBeerPi.com")
        logger.warning("(c) 2021 - 2024 Manuel Fritsch / Alexander Vollkopf")

    def _setup_http_index(self):
        async def http_index(request):
            url = self.config.static.get("index_url")

            if url is not None:

                raise web.HTTPFound(url)
            else:
                return web.Response(text="Hello from CraftbeerPi!")

        self.app.add_routes([web.get('/', http_index),
                             web.static('/static', os.path.join(os.path.dirname(__file__), "static"), show_index=True)])
        
    def testport(self, port=8000):
        HOST = "localhost"
        # Creates a new socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        # Try to connect to the given host and port
        if sock.connect_ex((HOST, port)) == 0:
            #print("Port " + str(port) + " is open") # Connected successfully
            isrunning = True
        else:
            #print("Port " + str(port) + " is closed") # Failed to connect because port is in use (or bad host)
            isrunning = False
        # Close the connection
        sock.close()
        return isrunning

    async def init_serivces(self):

        # First: a start-up that never finishes is restarted by systemd
        # (cbpi/api/sd_notify.py). Without this nothing watched start-up.
        self.heartbeat = Heartbeat()
        self.heartbeat.begin_startup()

        self._print_logo()

        await self.job.init()
        
        await self.config.init()
        if self.satellite is not None:
            await self.satellite.init()
        self._setup_http_index()
        self.plugin.load_plugins()
        self.plugin.load_plugins_from_evn()
        await self.fermenter.init()
        await self.sensor.init()
        await self.step.init()
        
        await self.actor.init()
        await self.kettle.init()
        await self.call_initializer(self.app)
        await self.dashboard.init()


        self._swagger_setup()

        # Ready - and the watchdog's running period armed - only once
        # everything is up: the last on_startup hook, after aiohttp's own.
        self.app.on_startup.append(self.heartbeat.on_startup)
        self.app.on_shutdown.append(self.heartbeat.stop)

        return self.app

    # Seconds aiohttp waits for open requests when stopping, before its
    # cleanup. Its default, 60, outlasted systemd's TimeoutStopSec (15), so
    # the cleanup that saves state never ran on a stop (DEFECTS F22).
    SHUTDOWN_TIMEOUT = 5

    def start(self):
        port=self.static_config.get("port",8000)
        if not self.testport(port):
            web.run_app(self.init_serivces(), port=port, shutdown_timeout=self.SHUTDOWN_TIMEOUT)
        else:
            logging.error("Port {} is already in use! Please check, if server is already running (e.g. in automode)".format(port))
            exit(1)