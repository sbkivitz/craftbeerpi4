"""The strike water helper on the server: USE for the loaded recipe, its forecasts, and the commands the brew-day
screen sends (GET and POST /step2/strike, its water, grain, recipe figure and room writes, and a forecast for any
strike).

The model (cbpi.strike.model) runs in a worker process of its own (cbpi.strike.worker), at low priority, never in the
control loop. This controller looks at the numbers about once a real second: the recipe's (kept by its import, in
the basic data's mash_numbers, or given on Brew Day where it kept none and named there in typed; a mash step's own
grain weight where neither is), BrewStart's water and grain temperature or the brew day's own, the boil kettle's
probe for the room (or the room given on Brew Day while the probe gives no reading a room can have), the Mash Tun's
PID_HERMS settings and the rig's numbers (config/strike_plant.json). When they
move past what the last answer allows, it sets the worker on them and says so (working), keeping the last answer
until the new one comes. It never changes the strike water by itself: only USE and BACK TO do.

Every temperature the screen sees and sends is in the brewery's unit (TEMP_UNIT), volumes in BrewStart's
Volume_Unit, masses in lb or kg. The model works in F, gallons and kg; this controller converts at its edges.

The brew day's own figures (the HLT's water, the grain's temperature and the room's by hand, the strike water before
the first USE, and where each USE wrote its figure) and the last answer are kept in config/strike_day.json with the
recipe's step ids: loading a recipe, the same one included, starts a new brew day.

For the Figures page: as the malt goes in, the forecast for the strike in force is kept in
config/strike_landings.jsonl (S1), and a worker of its own fits the share of the tun's steel the strike water warms to
those landings (S2, cbpi.strike.landings), kept in config/strike_fit.json. The numbers the rig has measured are sent in
the state's `measured` (S3), and set_measured / clear_measured write them to the plant file.
"""

import asyncio
import base64
import dataclasses
import datetime
import hashlib
import importlib
import json
import logging
import math
import os
import queue
import subprocess
import sys
import threading
import time
import types

import yaml

from cbpi.api import clock, readings
from cbpi.api.persist import atomic_write_json
from cbpi.api.step import StepState
from cbpi.controller.step_controller import StepController
from cbpi.extension.hltpump import kettle_roles
from cbpi.strike import landings, model, vessels
from cbpi.strike import suggest as suggestion

logger = logging.getLogger(__name__)

#: How each problem with a reading is named, as the pre-brew check names it.
PROBE_REASONS = {"silent": "unreadable", "unproven": "never", "stale": "stale", "value": "not_a_number",
                 "implausible": "out_of_range"}


def _number(value):
    """A finite float, or None for a value missing, blank or not a number."""
    if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _prop(item, key):
    props = getattr(item, "props", None)
    try:
        return props.get(key) if props is not None else None
    except Exception:  # noqa: BLE001
        return None


def _id_list(steps):
    return isinstance(steps, list) and all(isinstance(s, str) for s in steps)


def _blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _written(entry):
    """Whether `entry` is a USE's record of a figure it wrote: {"step", "prop", "value", "unit"}."""
    return (isinstance(entry, dict) and isinstance(entry.get("step"), str)
            and entry.get("prop") in ("Temp", "Ready_Temp") and _number(entry.get("value")) is not None
            and entry.get("unit") in ("F", "C"))


class StrikeRefused(Exception):
    """A command refused, and nothing changed: the HTTP status, the error's code, and what else the answer says."""

    def __init__(self, status, error, **more):
        super().__init__(error)
        self.status = status
        self.error = error
        self.more = more

    def body(self):
        return dict(error=self.error, **self.more)


# ---------------------------------------------------------------------------------------------------------------
# The worker process
# ---------------------------------------------------------------------------------------------------------------

class WorkerClient:
    """The worker process from the server's side.

    Started when first needed, with no window, and lowering its own priority. A thread of its own writes to it, so a
    write never waits in the event loop, and another reads its answers and hands each to the event loop. One that has
    ended is started again by the next message; `on_message` hears {"exit": code} when it ends."""

    def __init__(self, on_message):
        self.on_message = on_message
        self.proc = None
        self._out = None
        self._loop = None
        self._stopped = False

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def send(self, message):
        # Once stop() has begun, nothing starts the worker again: the server is shutting down.
        if self._stopped:
            return
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        if not self.running():
            self._start()
        self._out.put(message)

    def _start(self):
        # The folder the server's own cbpi package is in, first on the worker's path, so the worker runs this code.
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(model.__file__))))
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(p for p in (root, env.get("PYTHONPATH")) if p)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        proc = subprocess.Popen([sys.executable, "-m", "cbpi.strike.worker"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=root,
                                creationflags=flags)
        out = queue.Queue()
        threading.Thread(target=self._write, args=(proc, out), name="strike-worker-in", daemon=True).start()
        threading.Thread(target=self._read, args=(proc, out), name="strike-worker-out", daemon=True).start()
        self.proc, self._out = proc, out

    @staticmethod
    def _write(proc, out):
        while True:
            message = out.get()
            if message is None:
                break
            try:
                proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
                proc.stdin.flush()
            except (OSError, ValueError):
                break
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass

    def _read(self, proc, out):
        for line in proc.stdout:
            try:
                message = json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            if isinstance(message, dict):
                self._hand(message)
        # The worker has ended, by itself or by stop(): its writer ends too, closing its end of the pipe, rather than
        # wait for a message that would never reach it. A worker started again has a writer and a pipe of its own.
        out.put(None)
        try:
            proc.stdout.close()
        except (OSError, ValueError):
            pass
        self._hand({"exit": proc.wait(), "pid": proc.pid})

    def _hand(self, message):
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self.on_message, message)
        except RuntimeError:  # the loop has closed
            pass

    def stop(self):
        self._stopped = True
        proc, out = self.proc, self._out
        self.proc = self._out = None
        if out is not None:
            out.put({"kind": "stop"})
            out.put(None)
        if proc is not None:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


# ---------------------------------------------------------------------------------------------------------------
# The rig's numbers
# ---------------------------------------------------------------------------------------------------------------

def _guess_fields(r):
    return dict(label=r.label, central=r.central, low=r.low, high=r.high, unit=r.unit, provenance=r.provenance,
                hot=r.hot, note=r.note)


def default_plant_file():
    """What config/strike_plant.json holds until the rig is measured further: the HLT as measured, each number not
    measured yet with its range, and the rig's setup. A number gets "measured": {"value", "date", "method"} only from
    a calibration result; it is then fixed at that value."""
    hlt = model.Hlt()
    return {
        "probe": "coil",
        "through_coil": True,
        "still_probe": 0.0,
        "hlt": dict(c=hlt.c, ua_pump_off=hlt.ua_pump_off, ua_pump_on=hlt.ua_pump_on, watts=hlt.watts,
                    probe_lag_s=hlt.probe_lag_s, steel_kg=hlt.steel_kg),
        "guesses": {name: {k: v for k, v in dict(central=r.central, low=r.low, high=r.high, note=r.note).items()
                           if v is not None}
                    for name, r in model.default_guesses(hlt).items()},
    }


def read_plant(data):
    """(Plant, setup) from the plant file's contents, each number not usable there taken from the defaults, and the
    setup: probe ("coil" or "tun"), through_coil, still_probe."""
    data = data if isinstance(data, dict) else {}
    base = model.Hlt()
    given = data.get("hlt") if isinstance(data.get("hlt"), dict) else {}
    hlt_fields = {}
    for name in ("c", "ua_pump_off", "ua_pump_on", "watts", "probe_lag_s", "steel_kg"):
        value = _number(given.get(name))
        hlt_fields[name] = value if value is not None and value > 0 else getattr(base, name)
    hlt = model.Hlt(**hlt_fields)
    guesses = model.default_guesses(hlt)
    stated = data.get("guesses") if isinstance(data.get("guesses"), dict) else {}
    for name, r in list(guesses.items()):
        entry = stated.get(name) if isinstance(stated.get(name), dict) else {}
        measured = entry.get("measured") if isinstance(entry.get("measured"), dict) else None
        value = _number(measured.get("value")) if measured else None
        if value is not None:
            guesses[name] = dataclasses.replace(r, central=value, low=value, high=value,
                                                      provenance=model.MEASURED)
            continue
        low, high = _number(entry.get("low")), _number(entry.get("high"))
        mid = _number(entry.get("central"))
        if name == "hlt_loss":
            if high is not None and high > 0:
                guesses[name] = dataclasses.replace(r, high=high)
            continue
        low = r.low if low is None else low
        high = r.high if high is None else high
        mid = r.central if mid is None else mid
        if low <= mid <= high:
            guesses[name] = dataclasses.replace(r, low=low, high=high, central=mid)
        else:
            logger.warning("strike_plant.json: %s's range %s-%s around %s doesn't hold together; the defaults are used",
                           name, low, high, mid)
    probe = data.get("probe") if data.get("probe") in ("coil", "tun") else "coil"
    still = _number(data.get("still_probe"))
    setup = dict(probe=probe, through_coil=data.get("through_coil") is not False,
                 still_probe=still if still is not None and 0.0 <= still <= 1.0 else 0.0)
    return model.Plant(hlt=hlt, guesses=guesses), setup


# ---------------------------------------------------------------------------------------------------------------
# The controller
# ---------------------------------------------------------------------------------------------------------------

class StrikeController:

    #: The websocket topic every screen hears: data is get_state().
    TOPIC = "strike"
    VERSION = 1
    DAY_FILE = "strike_day.json"
    PLANT_FILE = "strike_plant.json"
    RULE = "no worse"
    #: How far the HLT's water or a grain temperature given may move before USE is worked out again, in their units.
    SAME = 0.05
    #: How far the room may move before USE is worked out again.
    ROOM_SAME = {"F": 0.5, "C": 0.3}
    #: A room or a grain temperature the forecast can use, as COMPARE PROBES judges a room's reading.
    ROOM_RANGE = {"F": (35.0, 120.0), "C": (1.5, 49.0)}
    #: BeerSmith's mash figures a recipe can lack (its mash_numbers' lacks), all of them for one imported without.
    ALL_LACKS = ("grain", "water", "tun", "infuse_temp", "grain_temp", "tun_temp")
    #: The recipe's figures that can be given on Brew Day where BeerSmith's are missing: the command's field, the
    #: mash_numbers field it is kept in, and its name in lacks and typed.
    TYPED_FIELDS = {"grain": "grain_kg", "strike_vol": "infuse_l"}
    TYPED_NAMES = {"grain": "grain", "strike_vol": "water"}
    #: Where mash_numbers keeps BeerSmith's own figure for each one the recipe editor changed ({grain_kg, infuse_l}),
    #: for the way back to it. A figure changed from BeerSmith's is named in typed too, as it isn't BeerSmith's.
    BEERSMITH_KEPT = "beersmith"
    #: MashInStep's own copy of each figure, which the recipe's takes the place of.
    STEP_COPIES = {"grain": "Grain_Kg", "strike_vol": "Mash_Water"}
    #: What those may be, in the state's mass_unit and vol_unit: a grain bill and a strike water no brew of this rig
    #: comes near either end of.
    TYPED_RANGE = {"grain": {"lb": (0.5, 200.0), "kg": (0.2, 90.0)},
                   "strike_vol": {"gal": (0.5, 100.0), "L": (2.0, 380.0)}}
    #: More than this above the rest is too far to trust (MashInStep.MAX_STRIKE_RISE_C).
    FAR = {"F": 27.0, "C": 15.0}
    #: Strikes the forecast endpoint takes.
    FORECAST_RANGE = {"F": (32.0, 212.0), "C": (0.0, 100.0)}
    #: Real seconds the forecast endpoint waits for the worker: the replays take real time, whatever the brewing
    #: clock's speed.
    FORECAST_WAIT = 120.0
    #: The order a change is named in when several moved.
    WHY_ORDER = ("vol", "grain", "room", "rest", "rig")
    #: Seconds on CraftBeerPi's clock after a landing's window ends before its readings are looked for at last.
    FIT_MARGIN_S = 60.0
    #: Real seconds between looks for a landing's readings while its window isn't over, at most and at least, and
    #: before a fit that failed is tried again.
    FIT_RECHECK_S = (5.0, 300.0)
    FIT_RETRY_S = 600.0
    #: Real seconds a fit may take before it is set going again.
    FIT_TIMEOUT_S = 3600.0
    #: Why no landing was kept as the malt went in, as the log says it.
    NOT_LANDED = {"no_answer": "the box had no forecast for the strike water in force",
                  "no_strike": "the strike water in force isn't known"}

    def __init__(self, cbpi, watch=True):
        self.cbpi = cbpi
        self.watch = watch
        self.day = {"steps": None, "vol": None, "grain": None, "room": None, "was": None, "used": None, "landed": None}
        self.answer = None
        self.job = None
        self.failed = None
        self._ids = 0
        self._asked = {}
        self._unforecast = set()
        self._task = None
        self._pushed = None
        self._plant_cache = None
        self._planned_seen = None
        self._landing_said = None
        self.fit = None
        self._fitting = None
        self._fit_recheck = None
        self._fit_said = None
        self.client = WorkerClient(self._heard)
        self.fitter = WorkerClient(self._fit_heard)
        app = getattr(cbpi, "app", None)
        if app is not None and hasattr(app, "on_cleanup"):
            app.on_cleanup.append(self.shutdown)

    async def init(self):
        self._load_day()
        self._load_fit()
        if self.watch:
            self._task = asyncio.get_running_loop().create_task(self._watch())

    async def shutdown(self, app=None):
        if self._task is not None:
            self._task.cancel()
            self._task = None
        # A forecast asked for by the endpoint is ("ask", future); one asked for the state is ("state", plan, key).
        for entry in list(self._asked.values()):
            if entry[0] == "ask" and not entry[1].done():
                entry[1].cancel()
        self._asked.clear()
        self.client.stop()
        self.fitter.stop()

    async def _watch(self):
        said = None
        while True:
            # About once a real second, whatever the brewing clock's speed: a look at the numbers, not a timing.
            await clock.sleep(max(1.0, clock.scale()))
            try:
                self.update()
                said = None
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - one bad look must not end the watch
                if str(e) != said:
                    said = str(e)
                    logger.warning("Strike water: could not look at the numbers: %s", e)

    # -- the brew day's file -------------------------------------------------------------------------------------

    def _day_path(self):
        return self.cbpi.config_folder.get_file_path(self.DAY_FILE)

    def _load_day(self):
        try:
            with open(self._day_path(), encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:  # noqa: BLE001 - a brew day that can't be read starts afresh
            logger.warning("Strike water: %s could not be read, so the brew day starts afresh: %s", self.DAY_FILE, e)
            return
        if not isinstance(data, dict) or not _id_list(data.get("steps")):
            return
        self.day = {"steps": data["steps"]}
        for key in ("vol", "grain", "room", "was"):
            value = data.get(key)
            self.day[key] = value if isinstance(value, dict) and _number(value.get("value")) is not None else None
        used = data.get("used")
        used = [u for u in used if _written(u)] if isinstance(used, list) else []
        self.day["used"] = used or None
        landed = data.get("landed")
        self.day["landed"] = landed if landed is True or isinstance(landed, str) else None
        answer = data.get("answer")
        if isinstance(answer, dict) and isinstance(answer.get("plan"), str) and isinstance(answer.get("_job"), dict):
            if answer.get("levers_status") == "working":
                answer["levers_status"] = "overtaken"
            self.answer = answer

    def _save_day(self):
        data = dict(self.day, answer=self.answer)
        try:
            atomic_write_json(self._day_path(), data)
        except Exception as e:  # noqa: BLE001 - kept in memory either way
            logger.warning("Strike water: could not save %s: %s", self.DAY_FILE, e)

    def recipe_temps(self, profile):
        """The strike water as the Recipe Book keeps it (StepController.book_data), the recipe, not this brew:
        {step id: {prop: temperature}}, the brewery's unit, for each of the strike water step's Temp and BrewStart's
        Ready_Temp that still holds the figure USE wrote there - what it was before the first USE (the day's was).

        Empty when the brew day isn't the steps' in `profile`, there was no USE, or BACK TO since. A temperature typed
        over USE's is the brewer's own, so it isn't given: the book keeps it as typed.
        """
        ids = [getattr(s, "id", None) for s in profile]
        was, used = self.day.get("was"), self.day.get("used")
        if not ids or self.day.get("steps") != ids or not was or not used:
            return {}
        unit = readings.temp_unit(self.cbpi)
        back = self._to_unit(_number(was.get("value")), was.get("unit"), unit, "temp")
        if back is None:
            return {}
        steps = dict(zip(ids, profile))
        found = {}
        for entry in used:
            step = steps.get(entry["step"])
            held = _number(_prop(step, entry["prop"])) if step is not None else None
            wrote = self._to_unit(_number(entry["value"]), entry["unit"], unit, "temp")
            # USE writes its figure to the tenth, so one within half of that is still its own.
            if held is not None and abs(held - wrote) < 0.05:
                found.setdefault(entry["step"], {})[entry["prop"]] = round(back, 1)
        return found

    # -- the rig's file ------------------------------------------------------------------------------------------

    def _plant(self):
        path = self.cbpi.config_folder.get_file_path(self.PLANT_FILE)
        try:
            stamp = os.stat(path).st_mtime_ns
        except FileNotFoundError:
            stamp = None
            try:
                atomic_write_json(path, default_plant_file(), indent=4)
                stamp = os.stat(path).st_mtime_ns
            except Exception as e:  # noqa: BLE001 - the defaults are used either way
                logger.warning("Strike water: could not write %s: %s", self.PLANT_FILE, e)
        if self._plant_cache is not None and self._plant_cache[0] == stamp and stamp is not None:
            return self._plant_cache[1]
        data = {}
        if stamp is not None:
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:  # noqa: BLE001
                logger.warning("Strike water: %s could not be read, so the defaults are used: %s",
                               self.PLANT_FILE, e)
        found = read_plant(data)
        # The file as read, too: read_plant leaves out what a measured number says besides its value.
        self._plant_cache = (stamp, found, data if isinstance(data, dict) else {})
        return found

    def _plant_data(self):
        """The plant file as last read, a copy."""
        self._plant()
        return json.loads(json.dumps(self._plant_cache[2], default=str)) if self._plant_cache else {}

    # -- looking at the numbers ----------------------------------------------------------------------------------

    def _room(self, bk, unit):
        """(the room, why not): the boil kettle's probe less its offset, to 0.1, as COMPARE PROBES reads it."""
        if bk is None:
            return None, "no_kettle"
        sensor_id = getattr(bk, "sensor", None) or None
        if sensor_id is None:
            return None, "no_probe"
        sensors = getattr(self.cbpi, "sensor", None)
        sensor = sensors.find_by_id(sensor_id) if sensors is not None else None
        if sensor is None:
            return None, "missing"
        state = sensors.get_sensor_value(sensor_id) if getattr(sensor, "instance", None) is not None else None
        value, problem = readings.assess(state, unit=unit, celsius=readings.KETTLE_C)
        if problem is not None:
            return None, PROBE_REASONS.get(problem[0], problem[0])
        offset = _number(_prop(sensor, "offset")) or 0.0
        return round(value - offset, 1), None

    @staticmethod
    def _step_grain(mash_in, profile):
        """The grain weight, kg, a mash step keeps itself (MashInStep's Grain_Kg on Brew Steps), for a recipe whose
        import kept none of BeerSmith's and with none given on Brew Day: the mash step's, else the first MashInStep's
        that has one. None without."""
        for s in ([mash_in] if mash_in is not None else []) + [s for s in profile if s.type == "MashInStep"]:
            kg = _number(_prop(s, "Grain_Kg"))
            if kg is not None and kg > 0:
                return kg
        return None

    def _herms(self, mt, unit):
        """(PID_HERMS's settings for the model, in F, and where its code is), or None when the Mash Tun's logic isn't
        PID_HERMS. Each setting is read as PID_HERMS.run() reads it: blank is its own default."""
        types_ = getattr(self.cbpi.kettle, "types", None) or {}
        cls = (types_.get(getattr(mt, "type", None)) or {}).get("class")
        module = sys.modules.get(getattr(cls, "__module__", None) or "")
        if cls is None or module is None or not hasattr(module, "PIDArduino") \
                or not hasattr(cls, "OUTER_LOOP_RATIO"):
            return None
        try:
            blank = cls.blank_defaults(unit)
        except Exception:  # noqa: BLE001 - an older PID_HERMS: its table in C
            blank = dict(getattr(cls, "BLANK", {}))

        def read(name, default):
            value = _number(_prop(mt, name))
            return float(default) if value is None else value

        sample = read("SampleTime", blank.get("SampleTime", 5))
        if not 1.0 <= sample <= 60.0:
            sample = float(blank.get("SampleTime", 5))
        floor = 1.0 if unit == "C" else 2.0
        delta = max(read("DeltaTemp", blank.get("DeltaTemp", 3.0)), floor)
        mashin_delta = max(delta, read("MashIn_DeltaTemp", delta))
        within = _number(_prop(mt, "MashIn_Pump_Within"))
        within = within if within is not None and within > 0 else 0.0
        # Gains per degree, and bands in degrees, turned into F: the outer loop's gains are degrees per degree and
        # need nothing.
        per = 1.8 if unit == "C" else 1.0
        settings = dict(
            sample_time=sample,
            p=read("P", blank.get("P", 2.0)), i=read("I", blank.get("I", 0.005)), d=read("D", blank.get("D", 0.0)),
            hlt_p=read("HLT_P", blank.get("HLT_P", 117.0795)) / per,
            hlt_i=read("HLT_I", blank.get("HLT_I", 0.2747)) / per,
            hlt_d=read("HLT_D", blank.get("HLT_D", 41.58)) / per,
            delta_f=delta * per, mashin_delta_f=mashin_delta * per, mashin_pump_within_f=within * per,
            max_output=read("Max_Output", blank.get("Max_Output", 100)),
            # Pump rests aren't replayed: a pump that rests moves less heat than one that runs, so the replay with it
            # running counts the hotter mash, and USE errs low.
            pump_rest=False,
            tolerance_f=0.3 * 1.8 if unit == "C" else 0.5)
        return settings, {"module": cls.__module__, "file": getattr(module, "__file__", None)}

    def _vol_unit(self, bs, unit):
        if bs is not None:
            given = str(_prop(bs, "Volume_Unit") or "").strip().lower()
            if given.startswith("gal"):
                return "gal"
            if given in ("l", "liter", "litre", "liters", "litres"):
                return "L"
        return "gal" if unit == "F" else "L"

    @staticmethod
    def ceiling(unit):
        """MashInStep's ceiling for any strike water (MAX_STRIKE_ABS_C), in the brewery's unit: 179.6 F, 82.0 C."""
        return round(model.MAX_STRIKE_F, 1) if unit == "F" else model.MAX_STRIKE_C

    @staticmethod
    def _to_unit(value, from_unit, to_unit, kind):
        """A temperature ("temp") or a volume ("vol") in another unit."""
        if value is None or from_unit == to_unit:
            return value
        if kind == "temp":
            return model.f_of(value) if to_unit == "F" else model.c_of(value)
        return value * model.L_PER_GAL if to_unit == "L" else value / model.L_PER_GAL

    #: BrewStart's mark that it has begun heating its kettle itself (its HEATING_PROP, 1.3.0 on), for a BrewStart
    #: whose class doesn't say.
    BREWSTART_HEATING = "_brewstart_heating"

    @staticmethod
    def _heats_the_hlt(bs, hlt):
        """Whether BrewStart `bs` heats the HLT itself, holds the water and asks for it to be moved - a Ready by
        BrewStart on the HLT from 1.3.0, which has change_target for USE and BACK TO. With no strike water step on
        the HLT before the Mash Tun's MashIn, it is the strike water step then, its Ready_Temp the strike water's
        temperature."""
        if bs is None or hlt is None:
            return False
        if str(_prop(bs, "Timing") or "") != "Ready by" or _prop(bs, "Heat_Kettle") != hlt.id:
            return False
        return callable(getattr(getattr(bs, "instance", None), "change_target", None))

    def _heating(self, step):
        """Whether BrewStart `step` has begun heating its kettle itself: its mark, kept with the step."""
        key = getattr(type(getattr(step, "instance", None)), "HEATING_PROP", None) or self.BREWSTART_HEATING
        return bool(_prop(step, key))

    @staticmethod
    def _strike_prop(si, bs):
        """Where strike water step `si` keeps the strike water's temperature: Temp, or a BrewStart's Ready_Temp."""
        return "Ready_Temp" if si is not None and si is bs else "Temp"

    def _campden(self, bs, unit):
        """The Campden tablets BrewStart gives for its water volume, rounded as its own message rounds them (up to a
        quarter tablet), or None when its Campden reminder is off or its volume isn't usable."""
        instance = getattr(bs, "instance", None) if bs is not None else None
        if instance is None:
            return None
        dose = getattr(instance, "campden_dose", None)
        if callable(dose):
            try:
                return dose()
            except Exception:  # noqa: BLE001
                return None
        # A BrewStart before 1.2.0.post8: the same rule, from its own schedule module.
        if str(_prop(bs, "Campden_Reminder") or "No") != "Yes":
            return None
        try:
            package = type(instance).__module__.rpartition(".")[0]
            schedule = importlib.import_module(package + ".schedule")
            return schedule.campden_tablets(schedule.litres(_prop(bs, "Volume"), _prop(bs, "Volume_Unit"), unit))
        except Exception:  # noqa: BLE001
            return None

    def _look(self):
        """Everything worked out from what CraftBeerPi holds now, for the state, the decisions and the commands."""
        cbpi = self.cbpi
        unit = readings.temp_unit(cbpi)
        stepper = getattr(cbpi, "step", None)
        profile = list(getattr(stepper, "profile", None) or [])
        basic = getattr(stepper, "basic_data", None)
        basic = basic if isinstance(basic, dict) else {}
        L = types.SimpleNamespace(unit=unit, mass_unit="lb" if unit == "F" else "kg", ids=[s.id for s in profile],
                                  off=None, phase=None, by=None, bs=None, si=None, mi=None, hlt=None, mt=None, bk=None,
                                  herms=None, pid=None, sample_time=None, model=None, key=None, numbers=None,
                                  measured=None, vessels=None, tun_now=None)
        name = basic.get("name")
        L.name = name if profile and isinstance(name, str) and name.strip() else None
        try:
            roles = kettle_roles(cbpi)
        except Exception:  # noqa: BLE001
            roles = {}
        L.hlt, L.mt, L.bk = roles.get("hlt"), roles.get("mt"), roles.get("bk")

        if not profile:
            L.off = "no_recipe"
        elif L.mt is None:
            L.off = "no_mash_tun"
        elif L.hlt is None:
            L.off = "no_hlt"
        else:
            found = self._herms(L.mt, unit)
            if found is None:
                L.off = "not_herms"
            else:
                # A SampleTime the replay can't step through is a number missing, not a reason the helper is off: the
                # box stays, and says so.
                L.herms, L.pid = found
                L.sample_time = L.herms["sample_time"]
                at = next((i for i, s in enumerate(profile) if _prop(s, "Kettle") == L.mt.id), None)
                if at is None or profile[at].type != "MashInStep":
                    L.off = "no_mash_in"
                else:
                    L.mi = profile[at]
                    before = profile[:at]
                    L.si = next((s for s in before if s.type == "MashInStep" and _prop(s, "Kettle") == L.hlt.id), None)
                    L.bs = next((s for s in before if s.type == "BrewStart"), None)
                    if L.si is None and self._heats_the_hlt(L.bs, L.hlt):
                        L.si = L.bs
                    if L.mi.status == StepState.DONE:
                        L.off = "malt_in"
        if L.bs is None and profile:
            L.bs = next((s for s in profile if s.type == "BrewStart"), None)
        # Where the strike water step keeps the strike water's temperature: a BrewStart that heats the HLT itself in
        # its Ready_Temp.
        L.strike_prop = self._strike_prop(L.si, L.bs)
        L.by = None if L.off else ("step" if L.si is not None else "hand")
        if L.off is None:
            started = (StepState.ACTIVE, StepState.STOP, StepState.ERROR)
            # A BrewStart heats once its heating has begun: before that it waits for its time.
            heats = L.si is not None and (L.si is not L.bs or self._heating(L.si))
            if L.mi.status in started or (L.si is not None and L.si.status == StepState.DONE):
                L.phase = "moved"
            elif heats and L.si.status in started:
                L.phase = "heat"
            else:
                L.phase = "plan"

        # The recipe's numbers, as its import kept them, or as given on Brew Day where it kept none (typed). Without
        # either, the grain weight a mash step keeps itself on Brew Steps (MashInStep's Grain_Kg).
        L.vol_unit = self._vol_unit(L.bs, unit)
        numbers = basic.get("mash_numbers") if isinstance(basic.get("mash_numbers"), dict) else {}
        grain_kg = _number(numbers.get("grain_kg"))
        infuse_l = _number(numbers.get("infuse_l"))
        for_grain_l = _number(numbers.get("for_grain_l"))
        tun_kg = _number(numbers.get("tun_kg"))
        tun_heat = _number(numbers.get("tun_cal_g_c"))
        grain_kg = grain_kg if grain_kg and grain_kg > 0 else None
        infuse_l = infuse_l if infuse_l and infuse_l > 0 else None
        typed_figures = [t for t in numbers.get("typed", []) if t in self.TYPED_NAMES.values()] \
            if isinstance(numbers.get("typed"), list) else []
        grain_from = None if grain_kg is None else "typed" if "grain" in typed_figures else "beersmith"
        if grain_kg is None:
            grain_kg = self._step_grain(L.mi, profile)
            grain_from = None if grain_kg is None else "step"
        mass = (lambda kg: None if kg is None else round(kg / model.LB if unit == "F" else kg, 1))
        vol_of_l = (lambda litres: None if litres is None else
                    round(litres / model.L_PER_GAL if L.vol_unit == "gal" else litres, 2))
        temp_of_c = (lambda c: None if c is None else round(model.f_of(c) if unit == "F" else c, 1))
        strike_vol, for_grain = vol_of_l(infuse_l), vol_of_l(for_grain_l)
        dead = None
        if strike_vol is not None and for_grain is not None:
            dead = round(max(0.0, strike_vol - for_grain), 2)
        infuse_c = _number(numbers.get("infuse_temp_c"))
        lacks = [x for x in numbers.get("lacks", []) if isinstance(x, str)] if numbers else list(self.ALL_LACKS)
        # BeerSmith's own figure for each one changed from it in the recipe editor, in the state's units.
        kept = self._beersmith_kept(numbers)
        changed = {}
        if "grain" in typed_figures and kept.get("grain_kg") is not None:
            changed["grain"] = mass(kept["grain_kg"])
        if "water" in typed_figures and kept.get("infuse_l") is not None:
            changed["strike_vol"] = vol_of_l(kept["infuse_l"])
        L.recipe = None if not profile else dict(
            name=L.name, steps=list(L.ids), grain=mass(grain_kg), strike_vol=strike_vol, for_grain=for_grain,
            dead=dead, tun=mass(tun_kg if tun_kg and tun_kg > 0 else None),
            tun_heat=tun_heat if tun_kg and tun_kg > 0 else None,
            beersmith=None if infuse_c is None else dict(
                strike=temp_of_c(infuse_c), grain=temp_of_c(_number(numbers.get("grain_temp_c"))),
                tun=temp_of_c(_number(numbers.get("tun_temp_c")))),
            lacks=lacks, typed=typed_figures, grain_from=grain_from, changed=changed)
        # The tun's steel as the model counts it: the Mash Tun's own figures from the Hardware page when it has
        # them, else the recipe's (BeerSmith's TUN_WEIGHT and TUN_SPECIFIC_HEAT), else none.
        beersmith_tun = (tun_kg, tun_heat) if tun_kg and tun_kg > 0 and tun_heat else None
        mt_figure, hlt_figure = vessels.figure(L.mt, unit), vessels.figure(L.hlt, unit)
        if mt_figure is not None:
            L.tun_now = (mt_figure["kg"], mt_figure["specific_heat"])
        else:
            L.tun_now = beersmith_tun

        # The numbers now.
        rest = _number(_prop(L.mi, "Temp")) if L.mi is not None else None
        if L.si is not None:
            planned = _number(_prop(L.si, L.strike_prop))
        elif L.hlt is not None and L.off is None:
            planned = _number(getattr(L.hlt, "target_temp", None))
            planned = planned if planned is not None and planned > 0 else None
        else:
            planned = None
        if L.bs is not None:
            vol, vol_from = _number(_prop(L.bs, "Volume")), "brewstart"
            vol = vol if vol is not None and vol > 0 else None
            typed_raw = _prop(L.bs, "Grain_Temp")
            typed = None if _blank(typed_raw) else _number(typed_raw)
            typed_bad = not _blank(typed_raw) and typed is None
        else:
            kept = self.day.get("vol") if self.day.get("steps") == L.ids else None
            vol = self._to_unit(_number(kept.get("value")), kept.get("unit"), L.vol_unit, "vol") if kept else None
            vol_from = "brewday"
            kept = self.day.get("grain") if self.day.get("steps") == L.ids else None
            typed = self._to_unit(_number(kept.get("value")), kept.get("unit"), unit, "temp") if kept else None
            typed_bad = False
        low, high = self.ROOM_RANGE[unit]
        if typed is not None and not low <= typed <= high:
            typed_bad = True
        if typed is not None:
            typed = round(typed, 1)
        # The room: the boil kettle's probe less its offset, while it gives a reading a room can have; else the room's
        # temperature given on Brew Day for this brew, if any. Once the probe reads within that range again, it wins.
        probe_room, room_why = self._room(L.bk, unit)
        kept = self.day.get("room") if self.day.get("steps") == L.ids else None
        room_given = self._to_unit(_number(kept.get("value")), kept.get("unit"), unit, "temp") if kept else None
        room_given = None if room_given is None else round(room_given, 1)
        probe_usable = probe_room is not None and low <= probe_room <= high
        room, room_from = (room_given, "you") if not probe_usable and room_given is not None else (probe_room, "probe")
        grain = typed if typed is not None and not typed_bad else room
        left = None if vol is None or strike_vol is None else round(vol - strike_vol, 2)
        # The least the HLT keeps over its element and coil: the strike water may never take it lower.
        L.least = vessels.least_water(L.hlt, unit, L.vol_unit)
        plant, setup = self._plant()
        plant, hlt_from = vessels.with_hlt(plant, hlt_figure)
        L.vessels = vessels.shown(mt_figure, hlt_figure, hlt_from, beersmith_tun, unit)
        L.plant, L.setup = plant, setup
        place = basic.get(StepController.GRAIN_PLACE_KEY)
        L.inputs = dict(rest=rest, planned=None if planned is None else round(planned, 1),
                        vol=None if vol is None else round(vol, 2), vol_from=vol_from, left=left,
                        least=L.least["value"], least_from=L.least["from"],
                        campden=self._campden(L.bs, unit) if L.bs is not None and vol is not None else None,
                        room=room, room_why=room_why, room_from=room_from, room_given=room_given,
                        room_probe=probe_room, typed=None if typed_bad else typed, grain=grain,
                        grain_from="you" if typed is not None and not typed_bad else "probe", probe=setup["probe"],
                        grain_place=place if place in StepController.GRAIN_PLACES else None)
        # Every number missing, in the order the box names them; the first is the state's miss, which older screens read
        L.misses = [name for name, missing in (
            ("recipe", grain_kg is None or infuse_l is None or rest is None),
            # A SampleTime that isn't a whole number of seconds: no step of the replay divides it.
            ("sample_time", L.sample_time is not None and model.step_for(L.sample_time) is None),
            ("probe", room_from == "probe" and probe_room is None),
            ("room", room_from == "probe" and probe_room is not None and not probe_usable),
            ("grain", typed_bad),
            ("vol", vol is None or (strike_vol is not None and vol < strike_vol - 1e-9))) if missing]
        L.miss = L.misses[0] if L.misses else None
        # What the figures would do that they shouldn't, blocking nothing: the mash water leaving less in the HLT than
        # covers its element and coil.
        L.warnings = ["hlt_low"] if left is not None and left < L.least["value"] - 1e-9 else []

        L.rig = dict(through_coil=setup["through_coil"], still_probe=setup["still_probe"])
        L.guesses = None
        if L.miss is None and L.off is None:
            to_f = (lambda t: t) if unit == "F" else model.f_of
            gal = (lambda v: v) if L.vol_unit == "gal" else (lambda v: v / model.L_PER_GAL)
            brew = dict(grain_kg=grain_kg, strike_gal=infuse_l / model.L_PER_GAL, rest_f=to_f(rest),
                        hlt_total_gal=max(gal(vol), infuse_l / model.L_PER_GAL),
                        tun_kg=L.tun_now[0] if L.tun_now else 0.0,
                        tun_cal_g_c=L.tun_now[1] if L.tun_now else 0.0,
                        garage_f=to_f(room), grain_given_f=None if typed is None else to_f(typed),
                        probe=setup["probe"])
            flow = dict(stop_pump_at_strike=str(_prop(L.mi, "Stop_Pump_At_Strike") or "Yes") != "No",
                        hlt_pump_runs=str(self._config("HLT_PUMP_WITH_HEAT", "Yes")) == "Yes",
                        strike_through_coil=setup["through_coil"], still_probe=setup["still_probe"])
            L.model = dict(brew=brew, herms=L.herms, flow=flow, pid=L.pid, unit=unit, rule=self.RULE,
                           plant=dict(hlt=dataclass_fields(plant.hlt),
                                      guesses={n: _guess_fields(r) for n, r in plant.guesses.items()}))
            # What must be exactly the same for an answer to hold: all but the HLT's water, the room and the grain.
            exact = dict(brew={k: v for k, v in brew.items() if k not in ("hlt_total_gal", "garage_f", "grain_given_f")},
                         herms=L.herms, flow=flow, plant=L.model["plant"], unit=unit, rule=self.RULE,
                         pid=L.pid.get("module"))
            L.key = _digest(exact)
            L.numbers = dict(rest=rest, strike_vol=strike_vol, vol=round(vol, 2), vol_from=vol_from, room=room,
                             grain=grain, grain_from=L.inputs["grain_from"], probe=setup["probe"])
            try:
                box = model.ranges(model.Brew(**brew), plant)
                L.guesses = self._guesses_shown(box, unit, L.vol_unit)
                L.measured = self._measured_shown(box, unit, self._plant_cache[2] if self._plant_cache else {})
            except ValueError:
                L.guesses = None
        return L

    def _config(self, key, default=None):
        try:
            return self.cbpi.config.get(key, default)
        except Exception:  # noqa: BLE001
            return default

    @staticmethod
    def _guesses_shown(box, unit, vol_unit):
        shown = []
        for name in model.SCREEN_ORDER:
            r = box.get(name)
            if r is None or r.fixed:
                continue
            low, high, mid, shown_unit = r.low, r.high, r.central, r.unit
            if name == "transfer_f":
                shown_unit = unit
                if unit == "C":
                    low, high, mid = low / 1.8, high / 1.8, mid / 1.8
            entry = dict(name=name, low=round(low, 3), high=round(high, 3), mid=round(mid, 3), unit=shown_unit,
                         provenance=r.provenance)
            if name == "hlt_loss":
                entry["at"] = round(model.HLT_LOSS_GAL if vol_unit == "gal" else model.HLT_LOSS_GAL * model.L_PER_GAL,
                                    2)
            shown.append(entry)
        return shown

    @staticmethod
    def _measured_shown(box, unit, data):
        """The numbers the rig has measured, in SCREEN_ORDER: {"name", "value", "unit", "date", "method", "brews"}.
        value and unit as _guesses_shown gives a guess's; date and method as the plant file's `measured` says; brews,
        how many brews its list names, or None for a number measured some other way."""
        stated = data.get("guesses") if isinstance(data, dict) and isinstance(data.get("guesses"), dict) else {}
        shown = []
        for name in model.SCREEN_ORDER:
            r = box.get(name)
            if r is None or r.provenance != model.MEASURED:
                continue
            value, shown_unit = r.central, r.unit
            if name == "transfer_f":
                shown_unit = unit
                if unit == "C":
                    value = value / 1.8
            entry = stated.get(name) if isinstance(stated.get(name), dict) else {}
            measured = entry.get("measured") if isinstance(entry.get("measured"), dict) else {}
            brews = measured.get("brews")
            shown.append(dict(name=name, value=round(value, 3), unit=shown_unit,
                              date=measured.get("date") if isinstance(measured.get("date"), str) else None,
                              method=measured.get("method") if isinstance(measured.get("method"), str) else None,
                              brews=len(brews) if isinstance(brews, list) else None))
        return shown

    # -- deciding when to work USE out ---------------------------------------------------------------------------

    def _differs(self, a, look):
        """What differs between an answer's numbers `a` and the numbers now, beyond what the answer allows: a list in
        WHY_ORDER, empty when the answer holds."""
        n = look.numbers
        moved = []
        if abs((a.get("vol") or 0) - (n.get("vol") or 0)) >= self.SAME - 1e-9:
            moved.append("vol")
        if (look.inputs["grain_from"] == "you" or a.get("grain_from") == "you") \
                and abs((a.get("grain") or 0) - (n.get("grain") or 0)) >= self.SAME - 1e-9:
            moved.append("grain")
        if abs((a.get("room") or 0) - (n.get("room") or 0)) > self.ROOM_SAME[look.unit] + 1e-9:
            moved.append("room")
        if a.get("key") != look.key:
            moved.append("rest" if a.get("rest") != n.get("rest") else "rig")
        return moved

    def update(self):
        """Look at the numbers, decide whether to set the worker on them, and tell the screens what changed. Returns
        the look."""
        look = self._look()
        if self.day.get("steps") != look.ids:
            self._new_day(look.ids)
        if look.off == "malt_in":
            try:
                self._landed(look)
            except Exception as e:  # noqa: BLE001 - Figures' record never stops the strike water box
                if str(e) != self._landing_said:
                    self._landing_said = str(e)
                    logger.warning("Strike water: could not keep where the strike water landed: %s", e)
        elif look.off is None and look.inputs["planned"] is not None:
            # By hand the strike water in force is the HLT's target, which PID_HERMS has taken over by the time the
            # malt is in: the last one seen before then stands for it.
            self._planned_seen = (list(look.ids), look.inputs["planned"])
        if look.off is not None or look.miss is not None:
            self._stop_job()
        else:
            moved = self._differs(self.answer["numbers"], look) if self.answer is not None else ["recipe"]
            job = self.job
            if not moved:
                if job is not None and not job["quiet"]:
                    self._stop_job()
                    job = None
                if job is None and self.answer.get("levers_status") == "overtaken":
                    self._start_job(look, None, quiet=True)
                self._ask_missing(look)
            elif job is not None and not job["quiet"] and not self._differs(job["numbers"], look):
                pass
            elif self.failed is not None and not self._differs(self.failed["numbers"], look):
                pass
            else:
                self._start_job(look, moved[0])
        self._push(look)
        try:
            self._fit_check()
        except Exception as e:  # noqa: BLE001 - Figures' fit never stops the strike water box
            if str(e) != self._fit_said:
                self._fit_said = str(e)
                logger.warning("Strike water: could not look at the landings for Figures: %s", e)
        return look

    def _new_day(self, ids):
        self._stop_job()
        self.day = {"steps": list(ids), "vol": None, "grain": None, "room": None, "was": None, "used": None,
                    "landed": None}
        self.answer = None
        self.failed = None
        self._save_day()

    def _stop_job(self):
        if self.job is not None:
            self.client.send({"kind": "cancel"})
            if self.answer is not None and self.answer.get("levers_status") == "working":
                self.answer["levers_status"] = "overtaken"
            self.job = None

    def _start_job(self, look, why, quiet=False):
        """Sets the worker on the numbers now, or, quiet, on the answer's own numbers again for its levers."""
        self._ids += 1
        if self.job is not None and self.answer is not None and self.answer.get("levers_status") == "working":
            self.answer["levers_status"] = "overtaken"
        strikes = self._strikes(look)
        job = dict(look.model, strikes=strikes, levers=True)
        numbers = dict(look.numbers, key=look.key)
        plan_id = _digest(dict(key=look.key, vol=numbers["vol"], room=numbers["room"],
                               grain=numbers["grain"] if numbers["grain_from"] == "you" else None))[:8]
        if quiet:
            numbers, plan_id = self.answer["numbers"], self.answer["plan"]
            job = dict(self.answer["_job"], strikes=strikes, levers=True)
            self.answer["levers_status"] = "working"
        self.job = dict(id=self._ids, why=why, since=clock.now(), started=clock.monotonic(), numbers=numbers,
                        plan=plan_id, job=job, quiet=quiet, tries=0, unit=look.unit)
        if not quiet:
            self.failed = None
        self.client.send({"kind": "plan", "id": self._ids, "job": job})

    def _strikes(self, look):
        """The strikes the screen shows besides USE, each once, in F: the one planned, BACK TO's, the rest and
        BeerSmith's."""
        to_f = (lambda t: t) if look.unit == "F" else model.f_of
        rest = look.inputs["rest"]
        wanted = []
        for role, temp in (("planned", look.inputs["planned"]), ("back", self._back_to(look)), ("rest", rest),
                           ("beersmith", (look.recipe.get("beersmith") or {}).get("strike") if look.recipe else None)):
            if temp is None:
                continue
            same = next((w for w in wanted if abs(w["shown"] - round(temp, 1)) < 1e-6), None)
            if same is None:
                wanted.append({"shown": round(temp, 1), "strike_f": to_f(round(temp, 1)), "for": [role]})
            else:
                same["for"].append(role)
        return [{"strike_f": w["strike_f"], "for": w["for"]} for w in wanted]

    def _back_to(self, look):
        was = self.day.get("was") if self.day.get("steps") == look.ids else None
        if was is not None:
            return round(self._to_unit(_number(was.get("value")), was.get("unit"), look.unit, "temp"), 1)
        return look.inputs["rest"]

    def _ask_missing(self, look):
        """A forecast without a search for each strike the screen shows that the answer hasn't got. One that failed
        isn't asked for again for the same answer."""
        answer = self.answer
        if answer is None or answer.get("far"):
            return
        asked = {(entry[1], entry[2]) for entry in self._asked.values() if entry[0] == "state"}
        for s in self._strikes(look):
            key = self._key(look.unit, s["strike_f"])
            if key in answer["forecasts"] or (answer["plan"], key) in asked or (answer["plan"], key) in self._unforecast:
                continue
            self._ids += 1
            self._asked[self._ids] = ("state", answer["plan"], key)
            self.client.send({"kind": "forecast", "id": self._ids, "job": answer["_job"], "cases": answer["_cases"],
                              "strike_f": s["strike_f"], "for": s["for"]})

    @staticmethod
    def _key(unit, strike_f):
        return "%.1f" % (strike_f if unit == "F" else model.c_of(strike_f))

    # -- what the worker says ------------------------------------------------------------------------------------

    def _heard(self, message):
        try:
            self._take(message)
        except Exception as e:  # noqa: BLE001 - one answer that can't be used must not stop the next
            logger.warning("Strike water: could not use the worker's answer: %s", e)

    def _take(self, message):
        if "exit" in message:
            self._worker_ended(message)
            return
        if "fatal" in message:
            logger.warning("Strike water: the worker failed: %s", message.get("fatal"))
            return
        ident = message.get("id")
        if ident in self._asked:
            self._forecast_heard(ident, message)
            return
        job = self.job
        if job is None or ident != job["id"]:
            return
        part = message.get("part")
        if part == "use":
            if "error" in message:
                logger.warning("Strike water: USE could not be worked out: %s", message["error"])
                self.job = None
                if not job["quiet"]:
                    self.failed = dict(at=round(clock.now(), 3), detail=message["error"], numbers=job["numbers"])
                    self.answer = None
                elif self.answer is not None:
                    self.answer["levers_status"] = "failed"
                self._save_day()
                self._push()
                return
            self._answer_from(job, message)
        elif part == "searched" and self.answer is not None and self.answer["plan"] == job["plan"]:
            if "error" in message:
                logger.warning("Strike water: the forecasts' own searches failed: %s", message["error"])
            else:
                for fc in message.get("forecasts") or []:
                    shown = self._forecast_shown(job["unit"], fc)
                    self.answer["forecasts"][self._key(job["unit"], fc["strike_f"])] = shown
                self._fill_rest_peak()
            self._save_day()
            self._push()
        elif part == "levers" and self.answer is not None and self.answer["plan"] == job["plan"]:
            if "error" in message:
                logger.warning("Strike water: the levers could not be worked out: %s", message["error"])
                self.answer["levers_status"] = "failed"
            else:
                self.answer["levers"] = self._levers_shown(job["unit"], message)
                self.answer["levers_status"] = "ready"
            self._save_day()
            self._push()
        elif part == "done":
            self.job = None

    def _answer_from(self, job, message):
        unit = job["unit"]
        use_f = message.get("use_f")
        use = None if use_f is None else round(use_f if unit == "F" else model.c_of(use_f), 1)
        rest_f = job["job"]["brew"]["rest_f"]
        too_far = bool(message.get("far")) or model.beyond_limits(rest_f, use_f)
        forecasts = {}
        for fc in message.get("forecasts") or []:
            forecasts[self._key(unit, fc["strike_f"])] = self._forecast_shown(unit, fc)
        took = clock.monotonic() - job["started"]
        self.answer = dict(plan=job["plan"], at=round(clock.now(), 3), took=round(max(0.0, took), 1),
                           runs=int(message.get("sims") or 0), rule=self.RULE, numbers=job["numbers"], use=use,
                           use_f=use_f, far=too_far, rest_peak=None, forecasts=forecasts, levers=None,
                           levers_status="far" if too_far else "working", unit=unit, _job=job["job"],
                           _cases=message.get("cases") or {})
        self._fill_rest_peak()
        if job["quiet"]:
            job["why"] = None
        job["quiet"] = True
        if too_far:
            self.job = None
        self._save_day()
        self._push()

    def _fill_rest_peak(self):
        answer = self.answer
        rest = answer["numbers"].get("rest")
        fc = answer["forecasts"].get("%.1f" % rest) if rest is not None else None
        answer["rest_peak"] = None if fc is None else fc["peak"]

    @staticmethod
    def _forecast_shown(unit, fc):
        shown = (lambda f: f) if unit == "F" else model.c_of

        def temp(f, places=2):
            return None if f is None else round(shown(f), places)

        return dict(strike=temp(fc["strike_f"], 1), searched=bool(fc.get("searched")), peak=temp(fc["highest_f"]),
                    reads=temp(fc.get("reads_f")), lo=[temp(x) for x in fc["low_f"]],
                    hi=[temp(x) for x in fc["high_f"]])

    @staticmethod
    def _levers_shown(unit, message):
        rise = {name: value for name, value in (message.get("rise") or {}).items()}
        order = {name: i for i, name in enumerate(model.SCREEN_ORDER)}
        top = sorted((n for n, v in rise.items() if v is not None and v >= 0.1 - 1e-9),
                     key=lambda n: (-rise[n], order.get(n, 99)))[:3]
        mid_f = message.get("mid_f")
        mid = None if mid_f is None else round(mid_f if unit == "F" else model.c_of(mid_f), 1)
        return dict(rise=rise, top=top, mid=mid)

    def _forecast_heard(self, ident, message):
        kind, *rest = self._asked.pop(ident)
        if kind == "ask":
            future = rest[0]
            if not future.done():
                if "forecast" in message:
                    future.set_result(message["forecast"])
                else:
                    future.set_exception(RuntimeError(message.get("error") or "no forecast"))
            return
        plan, key = rest
        if "forecast" not in message:
            logger.warning("Strike water: no forecast for %s: %s", key, message.get("error"))
            self._unforecast.add((plan, key))
            return
        if self.answer is None or self.answer["plan"] != plan:
            return
        self.answer["forecasts"][key] = self._forecast_shown(self.answer["unit"], message["forecast"])
        self._fill_rest_peak()
        self._save_day()
        self._push()

    def _worker_ended(self, message):
        """The worker process ended. The plan in hand is sent again once; a second end is a failure."""
        logger.warning("Strike water: the worker process ended (%s)", message.get("exit"))
        for ident, entry in list(self._asked.items()):
            self._asked.pop(ident, None)
            if entry[0] == "ask" and not entry[1].done():
                entry[1].set_exception(RuntimeError("the worker process ended"))
        job = self.job
        if job is None:
            return
        if job["tries"] < 1:
            job["tries"] += 1
            self.client.send({"kind": "plan", "id": job["id"], "job": job["job"]})
            return
        self.job = None
        if self.answer is None or self.answer["plan"] != job["plan"]:
            self.failed = dict(at=round(clock.now(), 3), detail="the worker process ended", numbers=job["numbers"])
            self.answer = None
        else:
            self.answer["levers_status"] = "failed" if self.answer.get("levers_status") == "working" \
                else self.answer.get("levers_status")
        self._save_day()
        self._push()

    # -- the state -----------------------------------------------------------------------------------------------

    def _miss(self, look):
        if look.miss is not None:
            return look.miss
        answer = self.answer
        if answer is not None and answer.get("far"):
            return "far"
        return None

    def get_state(self, look=None):
        look = look or self._look()
        unit = look.unit
        working = None
        if self.job is not None and not self.job["quiet"]:
            working = dict(since=round(self.job["since"], 3), why=self.job["why"])
        was = None
        if self.day.get("steps") == look.ids and self.day.get("was"):
            was = round(self._to_unit(_number(self.day["was"].get("value")), self.day["was"].get("unit"), unit,
                                      "temp"), 1)
        miss = self._miss(look)
        state = dict(
            version=self.VERSION, unit=unit, vol_unit=look.vol_unit, mass_unit=look.mass_unit,
            ceiling=self.ceiling(unit),
            now=round(clock.now(), 3), scale=clock.scale(), recipe=look.recipe,
            applies=look.off is None, off=look.off, phase=look.phase, by=look.by,
            step_ids=dict(brewstart=getattr(look.bs, "id", None), strike=getattr(look.si, "id", None),
                          mash_in=getattr(look.mi, "id", None)),
            inputs=look.inputs, rig=look.rig, sample_time=look.sample_time,
            guesses=look.guesses if miss in (None, "far") else None,
            measured=look.measured if miss in (None, "far") else None,
            vessels=look.vessels,
            miss=miss, misses=list(look.misses) or ([miss] if miss else []), warnings=list(look.warnings),
            was=was, working=working,
            failed=None if self.failed is None else dict(at=self.failed["at"], detail=self.failed["detail"]),
            answer=self._answer_shown(look, was))
        return state

    def _answer_shown(self, look, was):
        answer = self.answer
        if answer is None or answer.get("unit") != look.unit:
            return None
        rest, planned = look.inputs["rest"], look.inputs["planned"]
        back = was if was is not None else rest
        bs = (look.recipe or {}).get("beersmith") or {}
        roles = (("planned", planned), ("use", answer["use"]), ("back", back), ("rest", rest),
                 ("beersmith", bs.get("strike")))
        forecasts = []
        for key in sorted(answer["forecasts"], key=float):
            fc = answer["forecasts"][key]
            given = [role for role, temp in roles if temp is not None and "%.1f" % temp == key]
            if given:
                forecasts.append(dict(fc, **{"for": given}))
        shown = {k: answer[k] for k in ("plan", "at", "took", "runs", "rule", "use", "rest_peak", "levers",
                                        "levers_status")}
        shown["numbers"] = {k: v for k, v in answer["numbers"].items() if k != "key"}
        shown["forecasts"] = forecasts
        return shown

    def _push(self, look=None):
        try:
            state = self.get_state(look)
        except Exception as e:  # noqa: BLE001
            logger.debug("Strike water: no state to send: %s", e)
            return
        same = dict(state, now=None)
        text = json.dumps(same, sort_keys=True, default=str)
        if text == self._pushed:
            return
        self._pushed = text
        ws = getattr(self.cbpi, "ws", None)
        if ws is None:
            return
        try:
            ws.send(dict(topic=self.TOPIC, data=state))
        except Exception as e:  # noqa: BLE001 - a screen that misses this hears the next one
            logger.debug("Strike water: could not tell the screens: %s", e)

    # -- the commands --------------------------------------------------------------------------------------------

    def _same_recipe(self, look, steps):
        if steps != look.ids:
            logger.warning("Strike water: nothing changed - the screen showed a recipe that isn't the one loaded now")
            raise StrikeRefused(409, "recipe_changed", name=look.name, steps=list(look.ids))

    def _plan_said(self, working):
        answer = self.answer
        return dict(working=working, plan=answer["plan"] if answer else None, use=answer["use"] if answer else None,
                    numbers={k: v for k, v in answer["numbers"].items() if k != "key"} if answer else None)

    async def strike(self, body):
        """USE or BACK TO (POST /step2/strike). Returns {"applied", "strike"}; raises StrikeRefused."""
        if not isinstance(body, dict):
            raise StrikeRefused(400, "bad_body", detail="Send a JSON object: {\"do\", \"temp\", \"steps\"}.")
        do, temp, steps = body.get("do"), _number(body.get("temp")), body.get("steps")
        if do not in ("use", "back") or temp is None or not _id_list(steps) \
                or (do == "use" and not isinstance(body.get("plan"), str)):
            raise StrikeRefused(400, "bad_body", detail="do must be use or back, temp a number, steps the recipe's "
                                                        "step ids, and for USE plan its plan id.")
        look = self.update()
        ceiling = self.ceiling(look.unit)
        if round(temp, 1) > ceiling + 1e-9:
            # MashInStep's own ceiling, whatever a plan says: no strike water is heated past it from here.
            raise StrikeRefused(400, "too_hot", max=ceiling)
        self._same_recipe(look, steps)
        if look.off is not None:
            raise StrikeRefused(409, "off", why=look.off, strike=self.get_state(look))
        if look.phase == "moved":
            raise StrikeRefused(409, "moved", strike=self.get_state(look))
        if do == "use":
            miss = self._miss(look)
            if miss is not None:
                raise StrikeRefused(409, "missing", miss=miss, strike=self.get_state(look))
            working = self.job is not None and not self.job["quiet"]
            if working or self.answer is None:
                raise StrikeRefused(409, "plan_changed", strike=self.get_state(look), **self._plan_said(True))
            use, rest = self.answer["use"], look.inputs["rest"]
            if use is None or temp > use + 0.05 or temp < rest - 0.05:
                raise StrikeRefused(409, "plan_changed", strike=self.get_state(look), **self._plan_said(False))
        else:
            target = self._back_to(look)
            if target is None or abs(temp - target) > 0.05:
                raise StrikeRefused(409, "back_changed", to=target, strike=self.get_state(look))
        applied = await self._apply_strike(look, do, round(temp, 1))
        return dict(applied=applied, strike=self.get_state(self.update()))

    def _forget_wait(self, si):
        """A strike water step stopped, or failed, after it asked for the water to be moved keeps that wait with it,
        and Start asks again at once, at whatever the HLT holds. For a new temperature the wait is forgotten, so Start
        heats to it first and asks once the HLT reads it, as change_target does on a running step. A question still
        kept for the step is taken down. True when there was a wait to forget."""
        instance = getattr(si, "instance", None)
        key = getattr(instance, "WAITING_FOR_MALT_PROP", "_waiting_for_malt")
        props = [p for p in (si.props, getattr(instance, "props", None)) if p is not None]
        if not any(p.get(key) for p in props):
            return False
        for p in props:
            p[key] = False
        if instance is not None:
            instance._waiting_for_malt = False
        withdraw = getattr(getattr(self.cbpi, "step", None), "withdraw_step_prompt", None)
        if callable(withdraw):
            withdraw(si.id, "malt")
        return True

    async def _apply_strike(self, look, do, to):
        unit, si, bs = look.unit, look.si, look.bs
        before = look.inputs["planned"]
        changed = []
        wrote = []
        if si is not None:
            prop = self._strike_prop(si, bs)
            si.props[prop] = to
            wrote.append((si.id, prop))
            changed.append("step")
            instance = getattr(si, "instance", None)
            change = getattr(instance, "change_target", None)
            if si.status == StepState.ACTIVE and callable(change):
                if await change(to):
                    changed.append("question")
                # A BrewStart moves the HLT's target only once it heats it: waiting, it plans from the new temperature.
                if si is not bs or getattr(instance, "heating", False):
                    changed.append("hlt")
            elif si is bs:
                forget = getattr(instance, "forget_wait", None)
                if callable(forget) and forget():
                    changed.append("question")
            elif self._forget_wait(si):
                changed.append("question")
            if bs is not None and bs.status != StepState.DONE and str(_prop(bs, "Timing") or "") == "Ready by":
                if bs is not si:
                    bs.props["Ready_Temp"] = to
                    wrote.append((bs.id, "Ready_Temp"))
                changed.append("brewstart")
            await self.cbpi.step.save()
        else:
            await self.cbpi.kettle.set_target_temp(look.hlt.id, to)
            changed.append("hlt")
        if do == "use":
            if self.day.get("was") is None:
                self.day["was"] = None if before is None else {"value": before, "unit": unit}
            # Where this USE wrote its figure, and the figure, so the Recipe Book can tell it from one typed over it
            # (recipe_temps). A figure an earlier USE wrote where this one didn't stays recorded.
            kept = [u for u in self.day.get("used") or [] if (u["step"], u["prop"]) not in wrote]
            self.day["used"] = kept + [dict(step=sid, prop=prop, value=to, unit=unit) for sid, prop in wrote] or None
        else:
            self.day["was"] = None
            self.day["used"] = None
        self._save_day()
        u = "°" + unit
        if si is not None:
            said = ("Strike water set to %.1f %s, was %.1f %s" % (to, u, before, u) if do == "use" and before is not None
                    else "Strike water set to %.1f %s" % (to, u) if do == "use"
                    else "Strike water set back to %.1f %s" % (to, u))
        else:
            said = ("Hot liquor tank target %.1f %s for the strike water%s" % (
                to, u, "" if before is None else ", was %.1f %s" % (before, u)) if do == "use"
                else "Hot liquor tank target back to %.1f %s" % (to, u))
        logger.info(said)
        return dict(do=do, to=to, **{"from": before}, by=look.by, changed=changed)

    async def set_water(self, body):
        """The HLT's water (POST /step2/strike/water): BrewStart's Volume, or by hand the brew day's figure. Refused
        (400 too_little) where it would leave the HLT less than the least that covers its element and coil once the
        mash water has gone."""
        if not isinstance(body, dict) or not _id_list(body.get("steps")):
            raise StrikeRefused(400, "bad_body", detail="Send {\"vol\": number, \"steps\": [...]}.")
        vol = _number(body.get("vol"))
        if vol is None or vol <= 0:
            raise StrikeRefused(400, "bad_body", detail="vol must be a number more than 0.")
        look = self.update()
        self._same_recipe(look, body["steps"])
        if not look.ids:
            raise StrikeRefused(409, "off", why="no_recipe", strike=self.get_state(look))
        strike = (look.recipe or {}).get("strike_vol")
        least = look.least["value"]
        if strike is not None and vol - strike < least - 1e-9:
            # The HLT keeps the mash water and, after it has gone, the least that covers its element and coil.
            raise StrikeRefused(400, "too_little", min=round(strike + least, 2), mash=strike,
                                left=round(vol - strike, 2), least=least, least_from=look.least["from"])
        to = round(vol, 2)
        before = look.inputs["vol"]
        if look.bs is not None:
            look.bs.props["Volume"] = to
            await self.cbpi.step.save()
            by = "brewstart"
        else:
            self.day["vol"] = {"value": to, "unit": look.vol_unit}
            self._save_day()
            by = "brewday"
        campden = self._campden(look.bs, look.unit) if look.bs is not None else None
        logger.info("HLT water set to %.1f %s%s", to, look.vol_unit,
                    "" if before is None else ", was %.1f %s" % (before, look.vol_unit))
        return dict(applied=dict(what="water", to=to, **{"from": before}, by=by, campden=campden),
                    strike=self.get_state(self.update()))

    async def set_grain(self, body):
        """The grain's temperature (POST /step2/strike/grain): BrewStart's Grain_Temp, or by hand the brew day's
        figure; null blanks it, so the boil kettle's probe stands for it.

        Also Brew Day's answer to where the grain spent the night, which it asks every brew: kept for this brew in
        the brew steps' basic data (StepController.GRAIN_PLACE_KEY), and shown as inputs.grain_place. A figure is
        "indoors"; null with place "garage" is the garage, the probe's. Null without a place answers nothing: an
        earlier "garage" stays, as it still holds, and an earlier "indoors" goes with its figure.
        """
        if not isinstance(body, dict) or not _id_list(body.get("steps")) or "grain" not in body:
            raise StrikeRefused(400, "bad_body", detail="Send {\"grain\": number or null, \"steps\": [...]}.")
        grain = body.get("grain")
        if grain is not None:
            grain = _number(grain)
            if grain is None:
                raise StrikeRefused(400, "bad_body", detail="grain must be a number, or null for the probe.")
        place = body.get("place")
        if place is not None and place not in StepController.GRAIN_PLACES:
            raise StrikeRefused(400, "bad_body", detail="place must be \"garage\" or \"indoors\".")
        if (place == "garage" and grain is not None) or (place == "indoors" and grain is None):
            raise StrikeRefused(400, "bad_body", detail="Grain indoors has its temperature; in the garage, null: the "
                                                        "boil kettle's probe stands for it.")
        look = self.update()
        self._same_recipe(look, body["steps"])
        if not look.ids:
            raise StrikeRefused(409, "off", why="no_recipe", strike=self.get_state(look))
        low, high = self.ROOM_RANGE[look.unit]
        if grain is not None and not low <= grain <= high:
            raise StrikeRefused(400, "out_of_range", low=low, high=high)
        to = None if grain is None else round(grain, 1)
        before = look.inputs["typed"]
        was_place = look.inputs["grain_place"]
        answer = "indoors" if to is not None else "garage" if place == "garage" or was_place == "garage" else None
        stepper = self.cbpi.step
        if look.bs is not None:
            look.bs.props["Grain_Temp"] = "" if to is None else to
            by = "brewstart"
        else:
            self.day["grain"] = None if to is None else {"value": to, "unit": look.unit}
            self._save_day()
            by = "brewday"
        basic = stepper.basic_data
        if answer is None:
            basic.pop(StepController.GRAIN_PLACE_KEY, None)
        else:
            basic[StepController.GRAIN_PLACE_KEY] = answer
        if look.bs is not None or answer != was_place:
            await stepper.save()
        u = "°" + look.unit
        if to is None:
            logger.info("Grain temperature from the boil kettle's probe%s",
                        "" if before is None else ", was %.1f %s" % (before, u))
        else:
            logger.info("Grain temperature set to %.1f %s, was %s", to, u,
                        "the boil kettle's probe" if before is None else "%.1f %s" % (before, u))
        return dict(applied=dict(what="grain", to=to, **{"from": before}, by=by, place=answer),
                    strike=self.get_state(self.update()))

    @staticmethod
    def refuse_mash_water(look, strike_vol):
        """Raise 400 too_much where mash water `strike_vol` (in look.vol_unit) would leave the HLT less than the least
        that covers its element and coil (look.least): {"what": "strike_vol", "hlt", "left", "least", "least_from",
        "most"}, most being the most mash water the HLT's water allows. Nothing to check while the HLT's water isn't
        known. `look` is a _look()."""
        hlt, least = look.inputs["vol"], look.least["value"]
        if hlt is not None and hlt - strike_vol < least - 1e-9:
            raise StrikeRefused(400, "too_much", what="strike_vol", hlt=hlt, left=round(hlt - strike_vol, 2),
                                least=least, least_from=look.least["from"], most=round(hlt - least, 2))

    @classmethod
    def _beersmith_kept(cls, numbers):
        """BeerSmith's own figure for each the recipe editor changed, as mash_numbers keeps it: {grain_kg, infuse_l},
        each a number more than 0, or none."""
        kept = numbers.get(cls.BEERSMITH_KEPT) if isinstance(numbers, dict) else None
        if not isinstance(kept, dict):
            return {}
        out = {}
        for field in cls.TYPED_FIELDS.values():
            value = _number(kept.get(field))
            if value is not None and value > 0:
                out[field] = value
        return out

    def _clear_step_copies(self, profile, keys):
        """Clear the copy of each figure in `keys` that a MashInStep still to come keeps itself (Grain_Kg,
        Mash_Water), so it reads the recipe's: one figure, never a second that disagrees. A step that has begun keeps
        what it began with. -> how many were cleared."""
        cleared = 0
        for s in profile:
            if s.type != "MashInStep" or s.status != StepState.INITIAL:
                continue
            for key in keys:
                prop = self.STEP_COPIES[key]
                if not _blank(_prop(s, prop)):
                    s.props[prop] = ""
                    cleared += 1
        return cleared

    async def set_recipe(self, body):
        """The recipe's figures, the grain weight in the state's mass_unit and the strike water to the mash tun (the
        mash water) in its vol_unit, either or both (POST /step2/strike/recipe): given on Brew Day where BeerSmith's are
        missing, or changed in the recipe editor. Kept with the loaded recipe, in its basic data's mash_numbers
        (grain_kg, infuse_l), taken out of its lacks and named in its typed, so they're never taken for BeerSmith's; USE
        is worked out from them at once. A figure BeerSmith gave is refused (409 beersmith), so its own stays, unless
        the recipe editor sends it with "edit": true: BeerSmith's own is then kept in mash_numbers' beersmith, and
        "back": [the figures' names] puts it back, BeerSmith's again, as does changing it to BeerSmith's own. One given
        or changed can be given again. Each MashInStep still to come that keeps its own copy of a figure given
        (Grain_Kg, Mash_Water) has it cleared, so it reads the recipe's. Mash water that would leave the HLT less than the
        least that covers its element and coil is refused (refuse_mash_water), unless it is BeerSmith's own."""
        keys = set(self.TYPED_FIELDS)
        back = body.get("back", []) if isinstance(body, dict) else None
        if not isinstance(body, dict) or not _id_list(body.get("steps")) or not isinstance(back, list) \
                or not all(k in keys for k in back) or not (keys & set(body) or back) or keys & set(body) & set(back):
            raise StrikeRefused(400, "bad_body", detail="Send {\"grain\": number, \"strike_vol\": number, "
                                                        "\"steps\": [...]}, with either or both figures; \"edit\": "
                                                        "true to change BeerSmith's; \"back\": [\"grain\", "
                                                        "\"strike_vol\"] for BeerSmith's own.")
        edit = body.get("edit") is True
        back = list(dict.fromkeys(back))
        given = {}
        for key in self.TYPED_FIELDS:
            if key in body:
                value = _number(body.get(key))
                if value is None or value <= 0:
                    raise StrikeRefused(400, "bad_body", detail="%s must be a number more than 0." % key)
                given[key] = value
        look = self.update()
        self._same_recipe(look, body["steps"])
        if not look.ids:
            raise StrikeRefused(409, "off", why="no_recipe", strike=self.get_state(look))
        units = {"grain": look.mass_unit, "strike_vol": look.vol_unit}
        for key, value in given.items():
            low, high = self.TYPED_RANGE[key][units[key]]
            if not low <= value <= high:
                raise StrikeRefused(400, "out_of_range", what=key, low=low, high=high)
        stepper = self.cbpi.step
        basic = stepper.basic_data
        numbers = dict(basic["mash_numbers"]) if isinstance(basic.get("mash_numbers"), dict) \
            else {"lacks": list(self.ALL_LACKS)}
        typed = [t for t in numbers.get("typed", []) if t in self.TYPED_NAMES.values()] \
            if isinstance(numbers.get("typed"), list) else []
        kept = self._beersmith_kept(numbers)
        for key in given:
            figure = _number(numbers.get(self.TYPED_FIELDS[key]))
            if figure is not None and figure > 0 and self.TYPED_NAMES[key] not in typed and not edit:
                raise StrikeRefused(409, "beersmith", what=key, strike=self.get_state(look))
        for key in back:
            field, name = self.TYPED_FIELDS[key], self.TYPED_NAMES[key]
            figure = _number(numbers.get(field))
            if field not in kept and (name in typed or figure is None or figure <= 0):
                raise StrikeRefused(409, "no_beersmith", what=key, strike=self.get_state(look))
        per = {"grain": model.LB if units["grain"] == "lb" else 1.0,
               "strike_vol": model.L_PER_GAL if units["strike_vol"] == "gal" else 1.0}
        # BeerSmith's own figure for each given, where the recipe has it: a figure given the same as it, to the places
        # the state shows it, is BeerSmith's again.
        places = {"grain": 1, "strike_vol": 2}
        own = dict(kept)
        for key in given:
            field, name = self.TYPED_FIELDS[key], self.TYPED_NAMES[key]
            figure = _number(numbers.get(field))
            if name not in typed and figure is not None and figure > 0:
                own[field] = figure
        # Where the recipe has no BeerSmith figure, its kept BeerSmith file's own: a figure given the same as it (a
        # suggestion taken) is BeerSmith's.
        missing = [key for key in given if self.TYPED_FIELDS[key] not in own]
        in_file = suggestion.read_file(self.cbpi, basic) if missing else None
        from_file = {self.TYPED_FIELDS[key]: _number(in_file.get(self.TYPED_FIELDS[key])) for key in missing
                     if in_file and (_number(in_file.get(self.TYPED_FIELDS[key])) or 0) > 0}
        same = {**from_file, **own}
        restored = [key for key, value in given.items() if self.TYPED_FIELDS[key] in same
                    and round(same[self.TYPED_FIELDS[key]] / per[key], places[key]) == round(value, places[key])]
        if "strike_vol" in given and "strike_vol" not in restored:
            self.refuse_mash_water(look, given["strike_vol"])
        before = {key: (look.recipe or {}).get(key) for key in list(given) + back}
        kept = own
        for key, value in given.items():
            field, name = self.TYPED_FIELDS[key], self.TYPED_NAMES[key]
            if key in restored:
                continue
            numbers[field] = round(value * per[key], 3)
            typed.append(name)
        for key in back + restored:
            field, name = self.TYPED_FIELDS[key], self.TYPED_NAMES[key]
            if field in kept:
                numbers[field] = kept.pop(field)
            elif field in from_file:
                numbers[field] = round(from_file[field], 6)
                numbers["lacks"] = [x for x in numbers.get("lacks", []) if x != name]
            typed = [t for t in typed if t != name]
        if kept:
            numbers[self.BEERSMITH_KEPT] = kept
        else:
            numbers.pop(self.BEERSMITH_KEPT, None)
        numbers["typed"] = sorted(set(typed))
        numbers["lacks"] = [x for x in numbers.get("lacks", []) if x not in numbers["typed"]]
        basic["mash_numbers"] = numbers
        self._clear_step_copies(stepper.profile, list(given) + back)
        await stepper.save()
        # Every screen hears the recipe's basic data with the steps, so the recipe pages have the figures at once.
        push = getattr(stepper, "push_udpate", None)
        if callable(push):
            push(complete=True)
        after = self.update().recipe or {}
        for key in list(given) + back:
            what = "Grain weight" if key == "grain" else "Mash water"
            was = "" if before[key] is None else ", was %s %s" % (before[key], units[key])
            if key in back or key in restored:
                logger.info("%s back to BeerSmith's %s %s for the strike water forecast%s", what, after.get(key),
                            units[key], was)
            else:
                logger.info("%s set to %s %s for the strike water forecast, %s%s", what, round(given[key], 2),
                            units[key], "changed from BeerSmith's in the recipe editor"
                            if key in after.get("changed", {}) else "given on Brew Day" if not edit
                            else "given in the recipe editor", was)
        applied = {key: round(value, 2) for key, value in given.items() if key not in restored}
        applied.update({key: after.get(key) for key in back + restored})
        if back or restored:
            applied["back"] = back + restored
        return dict(applied=dict(what="recipe", **applied, **{"from": before}), strike=self.get_state(self.update()))

    async def suggest(self, file=None):
        """A suggestion for the recipe's grain weight and mash water where it has none of its own, from BeerSmith's
        data (cbpi.strike.suggest), for a box that asks for either to open with (GET /step2/strike/suggest): the loaded
        recipe's, or with `file` the Recipe Book recipe's. Nothing is saved. Raises 404 not_found for a book file that
        isn't there."""
        unit = readings.temp_unit(self.cbpi)
        mass_unit = "lb" if unit == "F" else "kg"
        if file is None:
            look = self.update()
            basic = getattr(self.cbpi.step, "basic_data", None)
            basic = basic if isinstance(basic, dict) and look.ids else {}
            step_kg = self._step_grain(look.mi, list(getattr(self.cbpi.step, "profile", None) or [])) \
                if look.ids else None
            vol_unit, this_file = look.vol_unit, basic.get("book_file")
        else:
            from cbpi.controller.recipe_controller import RecipeController
            path = self.cbpi.config_folder.get_recipe_file_by_id(file) \
                if isinstance(file, str) and RecipeController.SOURCE_ID.fullmatch(file) else None
            if path is None or not os.path.isfile(path):
                raise StrikeRefused(404, "not_found", file=file)
            try:
                with open(path) as f:
                    entry = yaml.load(f, Loader=yaml.FullLoader) or {}
            except Exception:  # noqa: BLE001 - as one not there: the box opens empty
                raise StrikeRefused(404, "not_found", file=file)
            basic = entry.get("basic") if isinstance(entry.get("basic"), dict) else {}
            steps = [s for s in entry.get("steps") or [] if isinstance(s, dict)]
            bs = next((types.SimpleNamespace(props=s.get("props") or {}) for s in steps if s.get("type") == "BrewStart"),
                      None)
            vol_unit, this_file = self._vol_unit(bs, unit), file
            step_kg = next((kg for kg in (_number((s.get("props") or {}).get("Grain_Kg")) for s in steps
                                          if s.get("type") == "MashInStep") if kg is not None and kg > 0), None)
        found = suggestion.suggest(self.cbpi, basic, step_kg, mass_unit, vol_unit, this_file=this_file)
        return dict(mass_unit=mass_unit, vol_unit=vol_unit, **found)

    async def set_room(self, body):
        """The room's temperature given on Brew Day (POST /step2/strike/room), in the brewery's unit, for this brew:
        the room the forecast takes while the boil kettle's probe gives no reading a room can have. Once the probe reads
        within that range again, its reading is the room, and this one is kept aside (inputs.room_given) in case it
        stops again. null takes it back. Kept with the brew day, not with the recipe or BeerSmith's figures."""
        if not isinstance(body, dict) or not _id_list(body.get("steps")) or "room" not in body:
            raise StrikeRefused(400, "bad_body", detail="Send {\"room\": number or null, \"steps\": [...]}.")
        room = body.get("room")
        if room is not None:
            room = _number(room)
            if room is None:
                raise StrikeRefused(400, "bad_body", detail="room must be a number, or null to take it back.")
        look = self.update()
        self._same_recipe(look, body["steps"])
        if not look.ids:
            raise StrikeRefused(409, "off", why="no_recipe", strike=self.get_state(look))
        low, high = self.ROOM_RANGE[look.unit]
        if room is not None and not low <= room <= high:
            raise StrikeRefused(400, "out_of_range", low=low, high=high)
        to = None if room is None else round(room, 1)
        before = look.inputs["room_given"]
        self.day["room"] = None if to is None else {"value": to, "unit": look.unit}
        self._save_day()
        after = self.update()
        u = "°" + look.unit
        if to is None:
            logger.info("Room temperature given on Brew Day taken back%s",
                        "" if before is None else ", was %.1f %s" % (before, u))
        else:
            logger.info("Room temperature given on Brew Day: %.1f %s%s", to, u,
                        "" if after.inputs["room_from"] == "you" else
                        ", kept aside while the boil kettle's probe reads the room")
        return dict(applied=dict(what="room", to=to, **{"from": before}, used=after.inputs["room_from"] == "you"),
                    strike=self.get_state(after))

    async def forecast(self, temp_text):
        """A forecast without a search of its own for any strike (GET /step2/strike/forecast?temp=), for the answer's
        numbers, from the cases it found."""
        look = self.update()
        temp = _number(temp_text)
        low, high = self.FORECAST_RANGE[look.unit]
        if temp is None or not low <= temp <= high:
            raise StrikeRefused(400, "bad_temp", low=low, high=high)
        if look.off is not None:
            raise StrikeRefused(409, "off", why=look.off, strike=self.get_state(look))
        miss = self._miss(look)
        if miss is not None:
            raise StrikeRefused(409, "missing", miss=miss, strike=self.get_state(look))
        answer = self.answer
        if answer is None:
            raise StrikeRefused(409, "working", strike=self.get_state(look))
        future = asyncio.get_running_loop().create_future()
        self._ids += 1
        ident = self._ids
        self._asked[ident] = ("ask", future)
        strike_f = temp if look.unit == "F" else model.f_of(temp)
        self.client.send({"kind": "forecast", "id": ident, "job": answer["_job"], "cases": answer["_cases"],
                          "strike_f": strike_f, "for": ["asked"]})
        try:
            fc = await asyncio.wait_for(future, self.FORECAST_WAIT)
        except (asyncio.TimeoutError, RuntimeError) as e:
            self._asked.pop(ident, None)
            raise StrikeRefused(503, "failed", detail=str(e) or "no answer from the worker") from None
        return dict(plan=answer["plan"], forecast=dict(self._forecast_shown(answer["unit"], fc), **{"for": ["asked"]}))

    # -- Figures: where the strike water landed (S1), the fit (S2), the numbers measured (S3) ----------------------

    def _landings_path(self):
        return self.cbpi.config_folder.get_file_path(landings.LANDINGS_FILE)

    def _fit_path(self):
        return self.cbpi.config_folder.get_file_path(landings.FIT_FILE)

    def _landed(self, look):
        """The first time the malt is in for the day's steps, the box's forecast for the strike water in force goes
        into config/strike_landings.jsonl, once a brew day: strike_day.json's "landed" is then true, or why there was
        none to keep."""
        if self.day.get("landed") is not None or self.day.get("steps") != look.ids:
            return
        record, why = self._landing(look)
        if record is None:
            self.day["landed"] = why
            self._save_day()
            logger.info("Strike water: the malt is in, but %s, so no landing is kept for Figures",
                        self.NOT_LANDED.get(why, why))
            return
        try:
            landings.append_landing(self._landings_path(), record)
        except Exception as e:  # noqa: BLE001 - tried again at the next look
            if str(e) != self._landing_said:
                self._landing_said = str(e)
                logger.warning("Strike water: could not keep where the strike water landed: %s", e)
            return
        self._landing_said = None
        self.day["landed"] = True
        self._save_day()
        logger.info("Strike water: the malt is in; the forecast for %.1f °%s, %s to %s, kept for Figures",
                    record["strike"], record["unit"], record["lands"][0], record["lands"][1])

    def _landing(self, look):
        """(The landing's record, None), or (None, why there is none): no answer with a forecast for the strike water
        in force ("no_answer"), or no strike water in force known ("no_strike")."""
        answer = self.answer
        if answer is None or answer.get("unit") != look.unit:
            return None, "no_answer"
        strike = look.inputs["planned"]
        if strike is None and self._planned_seen is not None and self._planned_seen[0] == look.ids:
            strike = self._planned_seen[1]
        if strike is None:
            return None, "no_strike"
        fc = (answer.get("forecasts") or {}).get("%.1f" % strike)
        if not isinstance(fc, dict) or len(fc.get("lo") or []) != 61 or len(fc.get("hi") or []) != 61:
            return None, "no_answer"
        return dict(
            version=landings.VERSION, t=self._malt_in_at(look), steps=list(look.ids), name=look.name, unit=look.unit,
            strike=round(strike, 1), rest=look.inputs["rest"], lands=[fc["lo"][0], fc["hi"][0]], lo=list(fc["lo"]),
            hi=list(fc["hi"]), searched=bool(fc.get("searched")),
            numbers={k: v for k, v in answer["numbers"].items() if k != "key"}, plant=self._plant_data(),
            plan=answer["plan"], sensor=getattr(look.mt, "sensor", None) or None, job=answer["_job"],
            vessels=look.vessels), None

    def _malt_in_at(self, look):
        """When the malt went in, as CraftBeerPi knows it: the brewer's Next on MashIn, when the step after it started
        (its _started_at), else now."""
        profile = list(getattr(self.cbpi.step, "profile", None) or [])
        at = next((i for i, s in enumerate(profile) if s is look.mi), None)
        following = profile[at + 1] if at is not None and at + 1 < len(profile) else None
        started = _number(_prop(following, StepController.STARTED_AT_PROP)) if following is not None else None
        return round(started if started is not None else clock.now(), 3)

    def _logs_folder(self):
        log = getattr(self.cbpi, "log", None)
        return getattr(log, "logsFolderPath", None) or getattr(self.cbpi.config_folder, "logsFolderPath", None)

    def _load_fit(self):
        try:
            with open(self._fit_path(), encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:  # noqa: BLE001 - worked out again from the landings
            logger.warning("Strike water: %s could not be read, so the fit is worked out again: %s",
                           landings.FIT_FILE, e)
            return
        if isinstance(data, dict) and data.get("version") == landings.VERSION and list(data.get("grid") or []) \
                == list(landings.GRID) and isinstance(data.get("landings"), list):
            self.fit = data
            # A landing whose window wasn't over is looked at again now.
            if any(isinstance(e, dict) and e.get("why") == "waiting" for e in data["landings"]):
                self._fit_recheck = time.monotonic()

    def fit_state(self):
        """For Figures (S2): the fit as strike_fit.json keeps it, or None before the first, and whether it's being
        worked out."""
        return dict(fit=self.fit, working=self._fitting is not None)

    def _fit_check(self):
        """Sets the fit's own worker on the landings when one was added, or its window is over, or the file changed."""
        if self._fitting is not None:
            if time.monotonic() - self._fitting["started"] < self.FIT_TIMEOUT_S:
                return
            logger.warning("Strike water: the fit for Figures took over an hour, so it is worked out again")
            self.fitter.send({"kind": "cancel"})
            self._fitting = None
        path = self._landings_path()
        key = landings.file_key(path)
        if key is None:
            return
        fit = self.fit or {}
        tun = self._fit_tun()
        if fit.get("key") == key and fit.get("tun") == tun and (
                self._fit_recheck is None or time.monotonic() < self._fit_recheck):
            return
        self._fit_recheck = None
        records = [r for r in landings.read_landings(path) if landings.usable_record(r)]
        kept = {e.get("id"): e for e in fit.get("landings") or [] if isinstance(e, dict)}
        todo = []
        for record in records:
            entry = kept.get(landings.landing_id(record)) or {}
            # A probe at the coil's outlet can't tell the steel: no curves for it, only its readings.
            curves = landings.probe_of(record) != "coil" and not (
                isinstance(entry.get("mid"), list) and len(entry["mid"]) == len(landings.GRID)
                and entry.get("tun") == tun)
            if curves or entry.get("landed") is None:
                todo.append(dict(record=record, curves=curves, tun=tun))
        if not todo:
            self._fit_take(records, key, [], tun)
            return
        loop = asyncio.get_running_loop()
        self._ids += 1
        self._fitting = dict(id=self._ids, key=key, since=clock.now(), started=time.monotonic(), records=records,
                             tun=tun)
        sensors = {t["record"].get("sensor") for t in todo if t["record"].get("sensor")}
        message = {"kind": "fit", "id": self._ids, "landings": todo, "logs": self._logs_folder()}
        loop.create_task(self._send_fit(sensors, message))

    async def _send_fit(self, sensors, message):
        # The probe's readings of the last minute may still be waiting to be written: written first.
        flush = getattr(getattr(self.cbpi, "log", None), "_flush_datalogger", None)
        for sensor in sensors:
            if callable(flush):
                try:
                    await flush(sensor)
                except Exception as e:  # noqa: BLE001
                    logger.debug("Strike water: could not write %s's readings first: %s", sensor, e)
        self.fitter.send(message)

    def _fit_heard(self, message):
        try:
            self._fit_answer(message)
        except Exception as e:  # noqa: BLE001
            logger.warning("Strike water: could not use the fit's answer: %s", e)

    def _fit_answer(self, message):
        job = self._fitting
        if "exit" in message or "fatal" in message:
            if "fatal" in message:
                logger.warning("Strike water: the fit's worker failed: %s", message.get("fatal"))
            if job is not None:
                self._fitting = None
                self._fit_recheck = time.monotonic() + self.FIT_RETRY_S
            return
        if job is None or message.get("id") != job["id"]:
            return
        self._fitting = None
        self._fit_take(job["records"], job["key"], message.get("fit") or [], job.get("tun"))

    def _fit_tun(self):
        """The tun every landing is fitted with: the Mash Tun's own figures from the Hardware page, [kg, cal/g C],
        the starting point for all of them; or None, each landing fitted with the tun its forecast was worked out
        with, the recipe's."""
        try:
            mt = kettle_roles(self.cbpi).get("mt")
            found = vessels.figure(mt, readings.temp_unit(self.cbpi))
        except Exception:  # noqa: BLE001 - the landings' own then
            return None
        return None if found is None else [round(found["kg"], 4), found["specific_heat"]]

    def _fit_take(self, records, key, found, tun=None):
        """The landings' entries, kept ones with what the worker found added, and the fit across them, into
        strike_fit.json. `tun`: the tun the curves were worked out with (_fit_tun)."""
        kept = {e.get("id"): e for e in (self.fit or {}).get("landings") or [] if isinstance(e, dict)}
        got = {f.get("id"): f for f in found if isinstance(f, dict)}
        now = clock.now()
        entries, waiting = [], []
        for record in records:
            lid = landings.landing_id(record)
            entry = dict(kept.get(lid) or {})
            new = got.get(lid) or {}
            unit = record["unit"]
            probe = landings.probe_of(record)
            entry.update(id=lid, t=record["t"], steps=list(record["steps"]), name=record.get("name"), unit=unit,
                         strike=record["strike"], probe=probe, sensor=record.get("sensor"))
            curves = new.get("curves")
            if isinstance(curves, dict):
                entry.update(mid=curves["mid"], lo0=curves["lo0"], hi0=curves["hi0"],
                             next_min=curves.get("next_min"), tun=tun)
            if new.get("error"):
                entry["error"] = new["error"]
            # A landing found once is kept: its probe's log rotates away within days.
            if new.get("landed") is not None or entry.get("landed") is None:
                entry["landed"] = new.get("landed")
                entry["readings"] = new.get("readings", entry.get("readings"))
            has_curves = all(isinstance(entry.get(k), list) and len(entry[k]) == len(landings.GRID)
                             for k in ("mid", "lo0", "hi0"))
            spread = (max(entry["mid"]) - min(entry["mid"])) / landings.per_f(unit) if has_curves else None
            entry["spread"] = None if spread is None else round(spread * landings.per_f(unit), 3)
            end = landings.window(record)[1]
            late = entry.get("landed") is None and now < end + self.FIT_MARGIN_S
            if late:
                waiting.append(end + self.FIT_MARGIN_S)
            if probe == "coil":
                # It reads the wort leaving the coil, which PID_HERMS holds: not where the mash landed.
                entry["why"] = "probe_at_coil"
            elif not has_curves:
                entry["why"] = "no_model"
            elif entry.get("landed") is None:
                entry["why"] = "waiting" if late else "no_readings"
            elif spread < landings.BLIND_F:
                entry["why"] = "blind"
            else:
                entry["why"] = None
            entry["share"] = landings.best([entry])[0] if entry["why"] is None else None
            entries.append(entry)
        result = landings.fit(entries)
        for entry in entries:
            entry.update(result["landings"].get(entry["id"]) or dict(fit_mid=None, fit_lo=None, fit_hi=None,
                                                                     off=None))
        self.fit = dict(version=landings.VERSION, key=key, tun=tun, at=round(now, 3), grid=list(landings.GRID),
                        window_min=list(landings.WINDOW_MIN), guess=landings.FIT_GUESS, value=result["value"],
                        why=result["why"], brews=result["brews"], rms_f=result["rms_f"], edge=result["edge"],
                        landings=entries)
        try:
            atomic_write_json(self._fit_path(), self.fit)
        except Exception as e:  # noqa: BLE001 - kept in memory either way
            logger.warning("Strike water: could not save %s: %s", landings.FIT_FILE, e)
        if waiting:
            low, high = self.FIT_RECHECK_S
            wait = (min(waiting) - now) / max(clock.scale(), 1e-9)
            self._fit_recheck = time.monotonic() + min(high, max(low, wait))
        logger.info("Strike water: the tun's steel fitted to %d landing(s): %s", result["brews"],
                    "%.0f %%" % (result["value"] * 100) if result["value"] is not None else result["why"])

    def _plant_file(self):
        """(The plant file's path, its contents as they are on the disk now, or what it holds until the rig is
        measured further)."""
        path = self.cbpi.config_folder.get_file_path(self.PLANT_FILE)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = default_plant_file()
        if not isinstance(data, dict):
            raise ValueError("%s doesn't hold an object" % self.PLANT_FILE)
        return path, data

    def set_measured(self, name, value, brews=None, method="figures"):
        """One of the numbers the rig hadn't given, measured: config/strike_plant.json's guesses.<name>.measured =
        {"value", "date" (CraftBeerPi's clock), "method", "brews"}, written whole, so read_plant fixes it at `value`,
        and the strike water box works USE out again with it. For Figures' USE (C4).

        Returns {"name", "measured", "before": the measured entry it replaced or None, "range": the guess's
        {"low", "central", "high"} it had in the file, or the model's own}. Raises ValueError for a name that isn't
        one of them, or a value that can't be one."""
        guesses = model.default_guesses()
        if name not in guesses:
            raise ValueError("%r isn't one of the numbers the rig hasn't given" % (name,))
        number = _number(value)
        if number is None or (guesses[name].unit == "share" and not 0.0 < number <= 1.0):
            raise ValueError("%r can't be %s" % (value, guesses[name].label))
        if brews is not None and not _id_list(brews):
            raise ValueError("brews must be a list of the brews' ids")
        path, data = self._plant_file()
        stated = data.setdefault("guesses", {})
        entry = stated.get(name) if isinstance(stated.get(name), dict) else {}
        before = entry.get("measured") if isinstance(entry.get("measured"), dict) else None
        r = guesses[name]
        shown = dict(low=_number(entry.get("low")), central=_number(entry.get("central")),
                     high=_number(entry.get("high")))
        shown = {k: (getattr(r, k) if v is None else v) for k, v in shown.items()}
        measured = dict(value=number, date=datetime.datetime.fromtimestamp(clock.now()).isoformat(timespec="seconds"),
                        method=str(method), brews=list(brews) if brews is not None else None)
        stated[name] = dict(entry, measured=measured)
        self._write_plant(path, data)
        logger.info("Strike water: %s measured at %s (%s)", r.label, number, method)
        return dict(name=name, measured=measured, before=before, range=shown)

    def clear_measured(self, name):
        """The number measured for `name` taken out of config/strike_plant.json, so it's a range again: Figures'
        BACK TO (C4). Returns the measured entry taken out, or None when there was none."""
        if name not in model.default_guesses():
            raise ValueError("%r isn't one of the numbers the rig hasn't given" % (name,))
        path, data = self._plant_file()
        entry = (data.get("guesses") or {}).get(name)
        if not isinstance(entry, dict) or "measured" not in entry:
            return None
        before = entry.pop("measured")
        self._write_plant(path, data)
        logger.info("Strike water: %s is a range again", model.default_guesses()[name].label)
        return before if isinstance(before, dict) else None

    def _write_plant(self, path, data):
        atomic_write_json(path, data, indent=4)
        self._plant_cache = None
        # The box works USE out again with it at its next look, now when a loop runs.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self.update()


def dataclass_fields(item):
    return {f.name: getattr(item, f.name) for f in dataclasses.fields(item)}


def _digest(data):
    text = json.dumps(data, sort_keys=True, default=str, separators=(",", ":"))
    return base64.b32encode(hashlib.sha1(text.encode("utf-8")).digest()).decode("ascii").lower()
