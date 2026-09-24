import asyncio
import logging

from cbpi.job.aiohttp import get_scheduler_from_app, setup

logger = logging.getLogger(__name__)


class JobController(object):

    def __init__(self, cbpi):
        self.cbpi = cbpi

    async def init(self):
        await setup(self.cbpi.app, self.cbpi)

    def register_background_task(self, obj):
        """
        This method parses all method for the @background_task decorator and registers the background job
        which will be launched during start up of the server

        :param obj: the object to parse
        :return:
        """

        async def job_loop(app, name, interval, method):
            logger.info(
                "Start Background Task %s Interval %s Method %s"
                % (name, interval, method)
            )
            # A background task that hangs, or raises, must not take its own
            # schedule down with it.
            #
            # This was `await method()` bare. An exception propagated out of
            # job_loop and ended the task permanently - silently, since the
            # scheduler simply has one fewer job - and a hang blocked every
            # later execution forever. Either way the job stops running and
            # nothing says so, which for anything doing safety-relevant
            # bookkeeping is the worst way to fail.
            #
            # The timeout is deliberately generous: five intervals, at least a
            # minute. It is there to catch a task that is never coming back,
            # not to police a slow one.
            try:
                budget = max(float(interval) * 5, 60.0)
            except (TypeError, ValueError):
                budget = 60.0
            while True:
                logger.debug(
                    "Execute Task %s - interval(%s second(s)" % (name, interval)
                )
                await asyncio.sleep(interval)
                try:
                    await asyncio.wait_for(method(), timeout=budget)
                except asyncio.TimeoutError:
                    logger.error(
                        "Background task %s did not finish within %.0fs and was "
                        "cancelled. Its schedule continues.",
                        name,
                        budget,
                    )
                except asyncio.CancelledError:
                    # The scheduler is shutting us down, not the task failing.
                    raise
                except Exception as e:  # noqa: BLE001
                    logger.error(
                        "Background task %s raised %s. Its schedule continues.",
                        name,
                        e,
                        exc_info=True,
                    )

        async def spawn_job(app):
            scheduler = get_scheduler_from_app(self.cbpi.app)
            for method in [
                getattr(obj, f)
                for f in dir(obj)
                if callable(getattr(obj, f))
                and hasattr(getattr(obj, f), "background_task")
            ]:
                name = method.__getattribute__("name")
                interval = method.__getattribute__("interval")
                job = await scheduler.spawn(
                    job_loop(self.app, name, interval, method), name, "background"
                )

        self.cbpi.app.on_startup.append(spawn_job)

    async def start_job(self, method, name, type):
        scheduler = get_scheduler_from_app(self.cbpi.app)
        return await scheduler.spawn(method, name, type)
