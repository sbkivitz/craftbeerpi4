"""Figures from past brews (GET /figures): every brew in the logs and BrewScribe, and what each figure measured beside
the setting it would change, with USE, BACK TO and LEAVE OUT (BUILDERS.md, C1-C4).

The logs are read by a worker process of its own (cbpi.figures.worker), at low priority, never in the event loop:
what an archived simulator run gives is kept in config/figures_cache.json and read once, and the live logs are read
again only where they changed. BrewScribe is asked over HTTP on this CraftBeerPi's own port. Everything else - the
settings, the strike water box's files, putting the figures together (cbpi.figures.ledger) - is quick and done here.

USE and BACK TO change only the figure's own setting: BREWSTART_EFFICIENCY (as a percentage), the strike water box's
tun_share in config/strike_plant.json (cbpi.figures.plant), and GRAIN_ABSORPTION. What USE set, since when, from which
brews and what the setting held before, and the brews left out of each figure, are kept in config/figures.json.
"""

import asyncio
import datetime
import json
import logging
import math
import os
import queue
import subprocess
import sys
import threading

import aiohttp

from cbpi.api import clock
from cbpi.api.persist import atomic_write_json
from cbpi.extension.hltpump import kettle_roles
from cbpi.figures import analysis, ledger, plant

logger = logging.getLogger(__name__)

SIM_TYPE = "SimVessel"


class FiguresRefused(Exception):
    """A request refused, and nothing changed: the HTTP status, the error's code, and what else the answer says."""

    def __init__(self, status, error, **more):
        super().__init__(error)
        self.status = status
        self.error = error
        self.more = more

    def body(self):
        return dict(error=self.error, **self.more)


def _number(value):
    return ledger.number(value)


def _seconds(text):
    """Seconds since the epoch from an ISO time (local time when it names no zone), or None."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        moment = datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.timestamp()


class Clock:
    """BrewScribe's wall-clock times on this CraftBeerPi's clock: through the times BrewScribe kept with its step
    entries (`anchors`: [(wall, brewing)]) on a simulator, as they are on a brewery at 1x."""

    def __init__(self, anchors, simulator, rate):
        self.anchors = sorted(anchors)
        self.simulator = simulator
        self.rate = rate if rate and rate > 0 else 1.0

    @property
    def matched(self):
        return not self.simulator or bool(self.anchors)

    def __call__(self, wall):
        if wall is None:
            return None
        if not self.simulator:
            return wall
        a = self.anchors
        if not a:
            return None
        if len(a) == 1:
            return a[0][1] + (wall - a[0][0]) * self.rate
        if wall <= a[0][0]:
            (w0, b0), (w1, b1) = a[0], a[1]
        elif wall >= a[-1][0]:
            (w0, b0), (w1, b1) = a[-2], a[-1]
        else:
            k = next(i for i in range(1, len(a)) if a[i][0] >= wall)
            (w0, b0), (w1, b1) = a[k - 1], a[k]
        slope = (b1 - b0) / (w1 - w0) if w1 != w0 else self.rate
        return b0 + (wall - w0) * slope


class WorkerClient:
    """The worker process from the server's side: started when first needed, with no window and at low priority, and
    asked one job at a time from a thread, so the event loop never waits on it. A worker that ended (it ends itself
    when idle) is started again by the next job."""

    def __init__(self):
        self.proc = None
        self._answers = None
        self._lock = threading.Lock()
        self._next = 0
        self._stopped = False

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def _start(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(ledger.__file__))))
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(p for p in (root, env.get("PYTHONPATH")) if p)
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        proc = subprocess.Popen([sys.executable, "-m", "cbpi.figures.worker"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=root,
                                creationflags=flags)
        answers = queue.Queue()
        threading.Thread(target=self._read, args=(proc, answers), name="figures-worker-out", daemon=True).start()
        self.proc, self._answers = proc, answers

    @staticmethod
    def _read(proc, answers):
        for line in proc.stdout:
            try:
                message = json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            if isinstance(message, dict):
                answers.put(message)
        answers.put({"exit": proc.wait()})

    def ask(self, job, timeout):
        """The worker's answer to `job` (blocking: call it from a thread). Raises RuntimeError with the worker's
        reason, or when it gave none in `timeout` seconds; a worker that gave none is ended."""
        with self._lock:
            if self._stopped:
                raise RuntimeError("stopping")
            if not self.running():
                self._start()
            self._next += 1
            ident = self._next
            message = dict(job, id=ident)
            try:
                self.proc.stdin.write((json.dumps(message, allow_nan=False) + "\n").encode("utf-8"))
                self.proc.stdin.flush()
            except (OSError, ValueError) as e:
                self.kill()
                raise RuntimeError("the worker could not be reached: %s" % e)
            while True:
                try:
                    answer = self._answers.get(timeout=timeout)
                except queue.Empty:
                    self.kill()
                    raise RuntimeError("the worker gave no answer in %d s" % timeout)
                if "exit" in answer:
                    self.proc = None
                    raise RuntimeError("the worker ended (%s)" % answer["exit"])
                if answer.get("id") != ident:
                    continue
                if "error" in answer:
                    raise RuntimeError(answer["error"])
                return answer.get("result")

    def kill(self):
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def stop(self):
        self._stopped = True
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.write(b'{"kind": "stop"}\n')
            proc.stdin.flush()
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001 - ended below either way
            pass
        self.kill()


class FiguresController:

    STORE = "figures.json"
    CACHE = "figures_cache.json"
    #: The last brews worked out, for the first request after a restart while they're worked out again.
    LAST = "figures_last.json"
    LANDINGS = "strike_landings.jsonl"
    FIT = "strike_fit.json"
    TOPIC = "figures"
    VERSION = 1
    BREWSCRIBE_TIMEOUT_S = 5.0
    #: How long the worker may take: reading a simulator's months of archived runs for the first time takes minutes
    #: on a Raspberry Pi.
    ANALYSE_TIMEOUT_S = 1800.0
    GRAPH_TIMEOUT_S = 120.0
    GRAPH_POINTS = 600
    #: Seconds a request waits for the brews being worked out before it's answered with the last ones and "working":
    #: a person's wait, so real seconds, whatever the brewing clock's speed. The first time on a simulator's months of
    #: archived runs takes minutes.
    WAIT_S = 3.0
    #: Seconds on the brewing clock with nothing heating, after something did, before a brew found only in the logs
    #: has ended: stretches of heating less apart than this are one brew (cbpi.figures.analysis).
    QUIET_S = analysis.BREW_GAP_S
    #: How often the watch for that looks, on the brewing clock.
    WATCH_S = 60.0

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self.worker = WorkerClient()
        self._made = None
        self._working = None
        self._waited = False
        self._days = {}
        self._heated_at = None
        self._watching = None
        app = getattr(cbpi, "app", None)
        if app is not None:
            app.on_cleanup.append(self._cleanup)

    async def init(self):
        steps = getattr(self.cbpi, "step", None)
        if steps is not None and hasattr(steps, "add_listener"):
            steps.add_listener(self._step_told)
        self._watching = asyncio.create_task(self._watch())

    async def _cleanup(self, app=None):
        if self._watching is not None:
            self._watching.cancel()
        await asyncio.get_running_loop().run_in_executor(None, self.worker.stop)

    def _step_told(self, event, snapshot):
        """A recipe that has just finished is a brew ended: screens read the figures again."""
        if event != "step_done":
            return
        steps = snapshot.get("steps") or []
        if steps and all(s.get("status") == "D" for s in steps):
            self._made = None
            # Told now, so not again when the heating has been off long enough.
            self._heated_at = None
            self._send("brew", None)

    def _heating(self):
        """Whether the HLT, the mash tun or the boil kettle is being heated now: its AUTO on, or its element."""
        actors = getattr(self.cbpi, "actor", None)
        for kettle in self._roles().values():
            if kettle is None:
                continue
            if getattr(getattr(kettle, "instance", None), "state", False) is True:
                return True
            try:
                element = actors.find_by_id(kettle.heater) if actors is not None and kettle.heater else None
            except Exception:  # noqa: BLE001 - an element that can't be found isn't heating
                element = None
            if getattr(getattr(element, "instance", None), "state", False) is True:
                return True
        return False

    async def _watch(self):
        """A brew with no recipe running ends with no step to say so. Once something has heated and nothing has for
        QUIET_S on the brewing clock - as long as the logs' brews are apart - the brew it was is over: screens are
        told, as at the end of a recipe."""
        while True:
            await clock.sleep(self.WATCH_S)
            try:
                if self._heating():
                    self._heated_at = clock.monotonic()
                elif self._heated_at is not None and clock.monotonic() - self._heated_at >= self.QUIET_S:
                    self._heated_at = None
                    self._made = None
                    self._send("brew", None)
            except Exception as e:  # noqa: BLE001 - the watch carries on
                logger.warning("Figures: could not tell whether a vessel is heating: %s", e)

    def _send(self, changed, figure):
        try:
            self.cbpi.ws.send(dict(topic=self.TOPIC, data={"changed": changed, "figure": figure}))
        except Exception as e:  # noqa: BLE001 - telling screens never fails a change
            logger.warning("Figures: could not tell the screens: %s", e)

    # -- what this CraftBeerPi is --------------------------------------------------------------------------------

    def _path(self, name):
        return self.cbpi.config_folder.get_file_path(name)

    def _logs_folder(self):
        log = getattr(self.cbpi, "log", None)
        return getattr(log, "logsFolderPath", None) or self.cbpi.config_folder.logsFolderPath

    def _unit(self):
        return "C" if str(self.cbpi.config.get("TEMP_UNIT", "C") or "C").upper().startswith("C") else "F"

    def _sensor(self, sensor_id):
        sensors = getattr(self.cbpi, "sensor", None)
        try:
            return sensors.find_by_id(sensor_id) if sensors is not None and sensor_id else None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _prop(item, key):
        props = getattr(item, "props", None)
        try:
            return props.get(key) if props is not None else None
        except Exception:  # noqa: BLE001
            return None

    def _roles(self):
        try:
            return kettle_roles(self.cbpi)
        except Exception as e:  # noqa: BLE001 - no kettles yet is no figures, not an error
            logger.warning("Figures: could not tell the vessels apart: %s", e)
            return {"hlt": None, "mt": None, "bk": None}

    def _params(self):
        roles = self._roles()
        vessels = {}
        for name, role in (("hlt", "hlt"), ("mash", "mt"), ("boil", "bk")):
            k = roles.get(role)
            if k is None:
                vessels[name] = {}
                continue
            vessels[name] = {"probe": k.sensor or None, "kettle": k.id, "element": k.heater or None,
                             "pump": k.agitator or None}
        hlt_sensor = self._sensor(vessels["hlt"].get("probe"))
        hlt_sim = None
        if hlt_sensor is not None and hlt_sensor.type == SIM_TYPE:
            litres = _number(self._prop(hlt_sensor, "Volume"))
            watts = _number(self._prop(hlt_sensor, "Heater_Watts"))
            hlt_sim = {"litres": 40.0 if litres is None else litres, "watts": 5500.0 if watts is None else watts}
        return {"unit": self._unit(), "vessels": vessels, "hlt_sim": hlt_sim}

    def _simulated(self, params):
        scale = _number(self.cbpi.config.get("SIM_TIME_SCALE", 1))
        if clock.is_scaled() or (scale is not None and scale != 1):
            return True
        return any(getattr(self._sensor(v.get("probe")), "type", None) == SIM_TYPE
                   for v in params["vessels"].values())

    def _no_grain(self, params):
        """A SimVessel mash tun with no Grain_Kg: the strike never lands on any grain there."""
        s = self._sensor(params["vessels"]["mash"].get("probe"))
        if s is None or s.type != SIM_TYPE:
            return False
        grain = _number(self._prop(s, "Grain_Kg"))
        return grain is None or grain <= 0

    # -- the files -----------------------------------------------------------------------------------------------

    def _read_json(self, name, default):
        try:
            with open(self._path(name), encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return default
        except (OSError, ValueError) as e:
            logger.warning("Figures: %s could not be read: %s", name, e)
            return default
        return data

    def store(self):
        """config/figures.json: {"version", "in_use": {figure: {"value", "since", "brews", "was"}}, "out": {figure:
        [brew ids]}}."""
        data = self._read_json(self.STORE, {})
        data = data if isinstance(data, dict) else {}
        in_use = data.get("in_use") if isinstance(data.get("in_use"), dict) else {}
        out = data.get("out") if isinstance(data.get("out"), dict) else {}
        return {"version": self.VERSION,
                "in_use": {k: v for k, v in in_use.items() if k in ledger.SETTABLE and isinstance(v, dict)},
                "out": {k: [b for b in v if isinstance(b, str)] for k, v in out.items()
                        if k in ledger.FIGURES and isinstance(v, list)}}

    def _save_store(self, data):
        atomic_write_json(self._path(self.STORE), data, indent=2, sort_keys=True)

    def _landings(self):
        """The strike water box's record of each brew's forecast as the malt went in (S1), oldest first."""
        found = []
        try:
            with open(self._path(self.LANDINGS), encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(entry, dict) and _number(entry.get("t")) is not None:
                        found.append(entry)
        except OSError:
            pass
        return found

    def _fit(self):
        """(S2's fit of the tun's steel, whether it's being worked out). The fit is {"value", "landings": [{"t",
        "fit_lo", "fit_hi", "share"}, ...]}, from the strike water helper's fit_state() where it has one, else
        config/strike_fit.json; None until it has a value."""
        state = getattr(getattr(self.cbpi, "strike", None), "fit_state", None)
        working = False
        if callable(state):
            try:
                found = state() or {}
                data, working = found.get("fit"), bool(found.get("working"))
            except Exception as e:  # noqa: BLE001 - the file below says the same
                logger.warning("Figures: the strike water helper's fit could not be read: %s", e)
                data = self._read_json(self.FIT, None)
        else:
            data = self._read_json(self.FIT, None)
        fit = data if isinstance(data, dict) and _number(data.get("value")) is not None else None
        return fit, working

    def _recipe_numbers(self, name):
        """mash_numbers of the recipe a brew was brewed from, by its name: the loaded recipe's, else the newest in
        the recipe book with that name; or None."""
        if not name:
            return None
        steps = getattr(self.cbpi, "step", None)
        basic = getattr(steps, "basic_data", None)
        if isinstance(basic, dict) and basic.get("name") == name and isinstance(basic.get("mash_numbers"), dict):
            return basic["mash_numbers"]
        recipes = getattr(self.cbpi, "recipe", None)
        try:
            book = recipes._book_entries() if recipes is not None else []
        except Exception:  # noqa: BLE001
            book = []
        best = None
        for _, entry, mtime in book:
            if isinstance(entry, dict) and entry.get("name") == name and isinstance(entry.get("mash_numbers"), dict):
                if best is None or mtime > best[0]:
                    best = (mtime, entry["mash_numbers"])
        return best[1] if best else None

    # -- BrewScribe ----------------------------------------------------------------------------------------------

    async def _brewscribe(self, port):
        """("ok" | "none" | "no_answer", [day]): BrewScribe's finished brews, each with its entries."""
        base = "http://127.0.0.1:%d/brewscribe/api" % port
        timeout = aiohttp.ClientTimeout(total=self.BREWSCRIBE_TIMEOUT_S)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(base + "/history") as r:
                    if r.status == 404:
                        return "none", []
                    if r.status != 200:
                        return "no_answer", []
                    history = await r.json(content_type=None)
                days = []
                for brew in (history.get("brews") or []) if isinstance(history, dict) else []:
                    if not isinstance(brew, dict) or not isinstance(brew.get("day_id"), str):
                        continue
                    key = json.dumps(brew, sort_keys=True, default=str)
                    kept = self._days.get(brew["day_id"])
                    if kept is not None and kept[0] == key:
                        days.append(kept[1])
                        continue
                    entries = []
                    try:
                        async with session.get("%s/days/%s" % (base, brew["day_id"])) as r:
                            if r.status == 200:
                                snapshot = await r.json(content_type=None)
                                entries = snapshot.get("entries") or [] if isinstance(snapshot, dict) else []
                    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                        logger.warning("Figures: BrewScribe's day %s could not be read: %s", brew["day_id"], e)
                        continue
                    day = {"day_id": brew["day_id"], "name": brew.get("name") or None,
                           "started_at": brew.get("started_at"), "finished_at": brew.get("finished_at"),
                           "simulator": brew.get("simulator"), "entries": entries}
                    self._days[brew["day_id"]] = (key, day)
                    days.append(day)
                return "ok", days
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError) as e:
            logger.info("Figures: BrewScribe gave no answer: %s", e)
            return "no_answer", []

    def _day_clock(self, day, simulated):
        anchors = []
        for e in day["entries"]:
            data = e.get("data") if isinstance(e.get("data"), dict) else {}
            native = data.get("native_observation") if isinstance(data.get("native_observation"), dict) else {}
            brewing = _number(native.get("brewing_at"))
            wall = _seconds(e.get("received_at"))
            if e.get("kind") == "step" and brewing is not None and wall is not None:
                anchors.append((wall, brewing))
        flag = day.get("simulator")
        simulator = bool(flag) if flag is not None else simulated
        return Clock(anchors, simulator, clock.scale())

    def _day_plan(self, day, simulated):
        """What the worker needs of a day: its window on this CraftBeerPi's clock and the probes for its volumes; and
        its volumes, each with its time on that clock. None for the window when it can't be placed."""
        c = self._day_clock(day, simulated)
        walls = [_seconds(e.get("occurred_at") or e.get("received_at")) for e in day["entries"]]
        walls = [w for w in walls if w is not None]
        start = _seconds(day.get("started_at")) or (min(walls) if walls else None)
        end = _seconds(day.get("finished_at")) or (max(walls) if walls else None)
        found = ledger.volumes(day["entries"])
        readings, probes = [], []
        for stage, e in found.items():
            t = c(_seconds(e.get("occurred_at") or e.get("received_at")))
            readings.append((t, stage, e))
            if t is not None and ledger.entry_celsius(e) is None:
                probes.append({"key": e["entry_id"], "vessel": ledger.PROBE_FOR[stage], "t": t})
            # When the HLT's level before the sparge was given, if BrewScribe kept it (level_before_at, from
            # 0.1.0.dev56): the HLT's reading then stands for that level's temperature.
            before = (e.get("data") or {}).get("level_before_at") if stage == "sparge_water" else None
            before_t = c(_number(before) if _number(before) is not None else _seconds(before))
            if before_t is not None and ledger.entry_celsius(e) is None:
                probes.append({"key": e["entry_id"] + ledger.BEFORE, "vessel": "hlt", "t": before_t})
        window = None
        if c.matched and start is not None and end is not None:
            window = {"id": day["day_id"], "start": c(start), "end": c(end), "probes": probes}
        return {"window": window, "volumes": found, "readings": sorted(readings, key=lambda r: r[0] or 0),
                "wall_start": start, "wall_end": end}

    # -- the brews -----------------------------------------------------------------------------------------------

    async def _analyse(self, job, timeout):
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self.worker.ask, job, timeout)
        except RuntimeError as e:
            raise FiguresRefused(503, "failed", reason=str(e))

    async def brews(self, port, fresh=True):
        """((the brews as GET /figures sends them, each brew's own record by id, the answer's head, the job's
        params), working). Worked out again, unless `fresh` is false and some are known; two requests at once share
        the work. A request waits for it at most WAIT_S: then it has the last brews worked out - from before a restart
        too (LAST) - or none, with working true, and the screens are told when the new ones are ready (the topic
        figures, "worked")."""
        working = self._working is not None and not self._working.done()
        if not fresh:
            made = self._made or self._kept()
            if made is not None:
                return made, working
        if not working:
            self._working = asyncio.ensure_future(self._work(port))
            self._working.add_done_callback(self._worked)
        try:
            return await asyncio.wait_for(asyncio.shield(self._working), self.WAIT_S), False
        except asyncio.TimeoutError:
            self._waited = True
            return self._made or self._kept() or self._nothing_yet(), True

    def _worked(self, task):
        """The brews worked out: the screens a request answered "working" are told they're ready."""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.warning("Figures: the brews could not be worked out: %s",
                           getattr(error, "more", None) or error)
            self._waited = False
            return
        if self._waited:
            self._waited = False
            self._send("worked", None)

    def _kept(self):
        """The last brews worked out before a restart (LAST), or None."""
        data = self._read_json(self.LAST, None)
        if not isinstance(data, dict) or data.get("version") != self.VERSION:
            return None
        try:
            made = (list(data["brews"]), dict(data["records"]), dict(data["head"]), dict(data["params"]))
        except (KeyError, TypeError, ValueError):
            return None
        self._made = made
        return made

    def _keep(self, made):
        public, records, head, params = made
        try:
            atomic_write_json(self._path(self.LAST), {"version": self.VERSION, "brews": public, "records": records,
                                                      "head": head, "params": params})
        except Exception as e:  # noqa: BLE001 - kept in memory either way
            logger.warning("Figures: could not keep the brews in %s: %s", self.LAST, e)

    def _head(self, params, simulated, brewscribe):
        unit = params["unit"]
        return {"brewery": self.cbpi.config.get("BREWERY_NAME", None) or "CraftBeerPi", "simulated": simulated,
                "unit": unit, "vol_unit": "L" if unit == "C" else "gal", "mass_unit": "kg" if unit == "C" else "lb",
                "brewscribe": brewscribe, "as_of": None}

    def _nothing_yet(self):
        """No brews worked out yet: the answer's head, BrewScribe not asked yet (null)."""
        params = self._params()
        return [], {}, self._head(params, self._simulated(params), None), params

    async def _work(self, port):
        params = self._params()
        unit = params["unit"]
        simulated = self._simulated(params)
        state, days = await self._brewscribe(port)
        plans = {d["day_id"]: self._day_plan(d, simulated) for d in days}
        landings = self._landings()
        job = {"kind": "analyse", "logs": self._logs_folder(), "cache": self._path(self.CACHE), "params": params,
               "days": [p["window"] for p in plans.values() if p["window"] is not None],
               "landings": [{"t": float(entry["t"])} for entry in landings]}
        result = await self._analyse(job, self.ANALYSE_TIMEOUT_S)
        if result.get("read"):
            logger.debug("Figures: read the logs of %s", ", ".join(result["read"]))
        fit, _ = self._fit()
        fits = {round(float(x["t"]), 3): x for x in (fit or {}).get("landings") or []
                if isinstance(x, dict) and _number(x.get("t")) is not None}
        no_grain = self._no_grain(params)

        def strike(brew):
            """(the brew's strike cell, its landing or None)."""
            if no_grain:
                return {"none": "no_grain"}, None
            if brew.get("start") is None:
                return {"none": "no_forecast"}, None
            for entry in landings:
                t = float(entry["t"])
                if brew["start"] <= t <= brew["end"]:
                    landed = (brew.get("landed") or {}).get(repr(t))
                    return ledger.strike_cell(entry, landed, fits.get(round(t, 3)), unit), entry
            return {"none": "no_forecast"}, None

        records, public = {}, []
        sources = result.get("sources") or {}
        for source in sorted(sources):
            for brew in sources[source].get("brews") or []:
                ident = "log:%s:%d" % (source, int(brew["start"]))
                cell, landing = strike(brew)
                cells = dict(brew["cells"], strike=cell, grain={"none": "no_brewscribe"})
                run = None if source == "now" else source
                public.append(self._public(ident, brew, cells, None, None, run, simulated or run is not None))
                records[ident] = {"source": source, "brew": brew, "landing": landing, "readings": None}
        vol = (lambda litres: litres) if unit == "C" else (lambda litres: litres / ledger.L_PER_GAL)
        for day in days:
            plan = plans[day["day_id"]]
            ident = "bs:%s" % day["day_id"]
            # The folder whose logs hold the most of the day's heating.
            best, best_source = None, None
            for source in sorted(sources):
                found = (sources[source].get("days") or {}).get(day["day_id"])
                if found is not None and (best is None or found.get("heated", 0) > best.get("heated", 0)):
                    best, best_source = found, source
            if best is None:
                window = plan["window"]
                best = {"start": window["start"] if window else plan["wall_start"],
                        "end": window["end"] if window else plan["wall_end"], "probes": {},
                        "cells": {"hlt": {"none": "no_logs"}, "mash": {"none": "no_logs"},
                                  "boil": {"none": "no_logs"}}}
            cell, landing = strike(best)
            grain = ledger.grain_cell(plan["volumes"], best.get("probes"),
                                      self._recipe_numbers(day.get("name")), unit)
            cells = dict(best["cells"], strike=cell, grain=grain)
            flag = day.get("simulator")
            run = None if best_source in (None, "now") else best_source
            public.append(self._public(ident, best, cells, day.get("name"), day["day_id"], run,
                                       bool(flag) if flag is not None else simulated))
            readings = [[round(t, 3), stage, round(vol(ledger.to_litres(float(e["data"]["value"]),
                                                                        e["data"]["unit"])), 2)]
                        for t, stage, e in plan["readings"] if t is not None]
            records[ident] = {"source": best_source, "brew": best, "landing": landing, "readings": readings}
        public.sort(key=lambda b: b["start"] if b["start"] is not None else -math.inf, reverse=True)
        head = dict(self._head(params, simulated, state), as_of=clock.now())
        self._made = (public, records, head, params)
        await asyncio.get_running_loop().run_in_executor(None, self._keep, self._made)
        return self._made

    @staticmethod
    def _public(ident, brew, cells, name, day_id, run, simulator):
        return {"id": ident, "start": brew.get("start"), "end": brew.get("end"), "name": name, "day_id": day_id,
                "run": run, "simulator": bool(simulator), "rests": brew.get("rests") or [],
                "boil": brew.get("boil"), "boil_at": brew.get("boil_at"), "auto": brew.get("auto") or [],
                "stopped_before_rest": bool(brew.get("stopped_before_rest")), "cells": cells}

    # -- the figures ---------------------------------------------------------------------------------------------

    def _plant_guess(self):
        data = plant.read(self._path(plant.PLANT_FILE))
        return plant.guess(data, "tun_share")

    def _settings(self, fit=None, working=False):
        config = self.cbpi.config
        cache = getattr(config, "cache", {}) or {}
        roles = self._roles()
        mt = roles.get("mt")
        mash = None
        if mt is not None and mt.type == "PID_HERMS":
            mash = {"setting": "DeltaTemp", "value": _number(self._prop(mt, "DeltaTemp")), "kettle": mt.id}
        return {
            "hlt": {"registered": "BREWSTART_EFFICIENCY" in cache, "value": config.get("BREWSTART_EFFICIENCY", None)},
            "grain": {"value": config.get("GRAIN_ABSORPTION", None)},
            "strike": {"available": getattr(self.cbpi, "strike", None) is not None, "guess": self._plant_guess(),
                       "fit": _number((fit or {}).get("value")), "working": working},
            "mash": mash,
        }

    def figures(self, brews, unit):
        return ledger.all_figures(brews, self.store(), unit, self._settings(*self._fit()))

    async def state(self, port, fresh=True):
        """GET /figures."""
        (public, _, head, params), working = await self.brews(port, fresh=fresh)
        return dict({"version": self.VERSION, "now": clock.now(), "scale": clock.scale()}, **head, working=working,
                    brews=public, figures=self.figures(public, head["unit"]))

    # -- C2: one brew's graph ------------------------------------------------------------------------------------

    VESSEL_FOR = {"hlt": "hlt", "strike": "mash", "mash": "mash", "grain": "mash", "boil": "boil"}

    async def graph(self, brew_id, figure, port):
        if figure not in ledger.FIGURES:
            raise FiguresRefused(404, "no_figure")
        (public, records, head, params), working = await self.brews(port, fresh=False)
        record = records.get(brew_id)
        if record is None:
            (public, records, head, params), working = await self.brews(port)
            record = records.get(brew_id)
        if record is None:
            raise self._refuse(409, "working", public, head["unit"]) if working else FiguresRefused(404, "no_brew")
        brew = record["brew"]
        vessel = self.VESSEL_FOR[figure]
        start, end = brew.get("start"), brew.get("end")
        t0, t1 = start, end
        stretches = list((brew.get("stretches") or {}).get(figure) or [])
        landing = record.get("landing")
        rests = ((brew.get("cells") or {}).get("mash") or {}).get("rests") or []
        if figure == "strike":
            stretches = []
            if landing is not None:
                t = float(landing["t"])
                t0, t1 = t - 360.0, t + 2400.0
                stretches = [{"t0": t + 300.0, "t1": t + 900.0}]
        elif figure == "mash" and rests:
            window = (brew.get("windows") or {}).get("mash")
            if window:
                t0, t1 = window
            else:
                t0, t1 = rests[0]["t0"] - 900.0, rests[-1]["t1"] + 720.0
        elif figure == "grain":
            readings = record.get("readings") or []
            times = {stage: t for t, stage, _ in readings}
            stretches = ([{"t0": times["strike_water"], "t1": times["pre_boil"]}]
                         if "strike_water" in times and "pre_boil" in times else [])
        answer = {"id": brew_id, "figure": figure, "vessel": vessel, "t0": t0, "t1": t1,
                  "temp": {"t": [], "v": []}, "target": None, "element": None, "steps": None,
                  "readings": record.get("readings"), "stretches": stretches,
                  "forecast": None}
        if figure == "strike" and landing is not None:
            cell = next((b["cells"]["strike"] for b in public if b["id"] == brew_id), {})
            answer["forecast"] = {"lo": cell.get("lo"), "hi": cell.get("hi")}
        if record.get("source") and t0 is not None and t1 is not None:
            job = {"kind": "graph", "logs": self._logs_folder(), "source": record["source"], "params": params,
                   "vessel": vessel, "t0": t0, "t1": t1, "points": self.GRAPH_POINTS}
            drawn = await self._analyse(job, self.GRAPH_TIMEOUT_S)
            if drawn:
                answer.update(drawn)
        return answer

    # -- C4: USE, BACK TO and LEAVE OUT --------------------------------------------------------------------------

    def _refuse(self, status, error, public, unit):
        return FiguresRefused(status, error, figures=self.figures(public, unit))

    @staticmethod
    def _percent(fraction):
        value = round(fraction * 100.0, 2)
        return int(value) if value == int(value) else value

    async def use(self, figure, body, port):
        if figure not in ledger.SETTABLE:
            raise FiguresRefused(404, "no_figure")
        value = _number(body.get("value")) if isinstance(body, dict) else None
        brews = body.get("brews") if isinstance(body, dict) else None
        if value is None or not isinstance(brews, list) or not all(isinstance(b, str) for b in brews):
            raise FiguresRefused(400, "bad_body")
        (public, _, head, _), working = await self.brews(port)
        unit = head["unit"]
        if working:
            raise self._refuse(409, "working", public, unit)
        now = self.figures(public, unit)[figure]
        if figure == "hlt" and now["why_not"] == "brewstart_old":
            raise self._refuse(409, "brewstart_old", public, unit)
        if now["state"] != "use":
            raise self._refuse(409, "not_offered", public, unit)
        if not ledger._same(now["use"]["value"], value) or sorted(now["use"]["brews"]) != sorted(brews):
            raise self._refuse(409, "brews_changed", public, unit)
        store = self.store()
        old = store["in_use"].get(figure)
        settings = self._settings()
        applied = {"figure": figure, "to": value}
        if figure == "hlt":
            fraction, built_in = ledger.efficiency_fraction(settings["hlt"]["value"])
            standing = ledger.standing(old, None if built_in else fraction)
            was = standing.get("was") if standing else (None if built_in else round(fraction, 4))
            applied.update({"from": round(fraction, 4), "from_built_in": built_in})
            await self.cbpi.config.set("BREWSTART_EFFICIENCY", self._percent(value))
        elif figure == "grain":
            current = _number(settings["grain"]["value"])
            standing = ledger.standing(old, current)
            was = standing.get("was") if standing else current
            applied["from"] = current
            await self.cbpi.config.set("GRAIN_ABSORPTION", value)
        else:
            guess = settings["strike"]["guess"]
            measured = guess.get("measured") if isinstance(guess.get("measured"), dict) else None
            current = _number(measured.get("value")) if measured else None
            standing = ledger.standing(old, current) if measured and measured.get("method") == "figures" else None
            was = standing.get("was") if standing else {k: guess.get(k) for k in ("low", "central", "high")}
            applied["from"] = current
            if current is None:
                applied["from_range"] = {k: guess.get(k) for k in ("low", "central", "high")}
            stamp = datetime.datetime.fromtimestamp(clock.now()).isoformat(timespec="seconds")
            plant.write_measured(self._path(plant.PLANT_FILE), "tun_share",
                                 {"value": value, "date": stamp, "method": "figures", "brews": list(brews)})
        store["in_use"][figure] = {"value": value, "since": clock.now(), "brews": list(brews), "was": was}
        self._save_store(store)
        self._send("use", figure)
        return {"applied": applied, "figures": self.figures(public, unit)}

    async def back(self, figure, body, port):
        if figure not in ledger.SETTABLE:
            raise FiguresRefused(404, "no_figure")
        value = _number(body.get("value")) if isinstance(body, dict) else None
        if value is None:
            raise FiguresRefused(400, "bad_body")
        (public, _, head, _), _ = await self.brews(port, fresh=False)
        unit = head["unit"]
        store = self.store()
        record = store["in_use"].get(figure)
        if record is None:
            raise self._refuse(409, "not_in_use", public, unit)
        settings = self._settings()
        if figure == "hlt":
            fraction, built_in = ledger.efficiency_fraction(settings["hlt"]["value"])
            current = None if built_in else fraction
        elif figure == "grain":
            current = _number(settings["grain"]["value"])
        else:
            measured = settings["strike"]["guess"].get("measured")
            current = _number(measured.get("value")) if isinstance(measured, dict) else None
        if not ledger._same(current, record.get("value")) or not ledger._same(value, record.get("value")):
            raise self._refuse(409, "back_changed", public, unit)
        was = record.get("was")
        applied = {"figure": figure, "from": record.get("value")}
        if figure == "hlt":
            was_n = _number(was)
            await self.cbpi.config.set("BREWSTART_EFFICIENCY", "" if was_n is None else self._percent(was_n))
            applied.update({"to": ledger.BUILT_IN_EFFICIENCY if was_n is None else was_n,
                            "to_built_in": was_n is None})
        elif figure == "grain":
            await self.cbpi.config.set("GRAIN_ABSORPTION", was)
            applied["to"] = was
        else:
            plant.write_measured(self._path(plant.PLANT_FILE), "tun_share", None)
            applied["to"] = None
            applied["to_range"] = was if isinstance(was, dict) else None
        del store["in_use"][figure]
        self._save_store(store)
        self._send("back", figure)
        return {"applied": applied, "figures": self.figures(public, unit)}

    async def out(self, figure, body, port):
        if figure not in ledger.FIGURES:
            raise FiguresRefused(404, "no_figure")
        brew = body.get("brew") if isinstance(body, dict) else None
        leave = body.get("out") if isinstance(body, dict) else None
        if not isinstance(brew, str) or not isinstance(leave, bool):
            raise FiguresRefused(400, "bad_body")
        (public, _, head, _), working = await self.brews(port, fresh=False)
        if not any(b["id"] == brew for b in public):
            (public, _, head, _), working = await self.brews(port)
            if not any(b["id"] == brew for b in public):
                if working:
                    raise self._refuse(409, "working", public, head["unit"])
                raise FiguresRefused(404, "no_brew")
        store = self.store()
        left = [b for b in store["out"].get(figure, []) if b != brew]
        if leave:
            left.append(brew)
        store["out"][figure] = left
        self._save_store(store)
        self._send("out", figure)
        return {"figures": self.figures(public, head["unit"])}
