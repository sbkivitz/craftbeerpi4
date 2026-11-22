"""The strike water helper's worker: a process of its own that runs the model, at low priority, so working USE out
never holds up the control loop.

StrikeController starts it with `python -m cbpi.strike.worker` and talks to it in JSON, one message a line: jobs on
its stdin, answers on its stdout. Each message is an object with a "kind" and an "id":

- "plan": work USE out for a job (job_from), then send what it has in parts, each {"id", "part", ...}:
  "use" (USE, the cases the planner found, and a forecast without a search of its own for each strike asked for and
  for USE), "searched" (the same forecasts with their own searches), "levers" (how far USE would rise with each
  number known, and USE with all of them known), then "done". A part that fails is sent with "error" instead; one
  that fails at "use" ends the job.
- "forecast": one forecast without a search, for the cases given, answered at once as {"id", "forecast"}, even in
  the middle of a plan.
- "cancel": drop the plan in hand. "stop": end.

A newer "plan" replaces the one in hand: the worker looks at its inbox before every replay, answers a "forecast"
there and then, and drops the plan in hand for a newer one. Its stdout carries nothing but these answers.
"""

import importlib
import importlib.util
import json
import logging
import math
import os
import queue
import sys
import threading
import traceback

from cbpi.strike import model


class Superseded(Exception):
    """The plan in hand is dropped: `then` is the message to take up next, or None."""

    def __init__(self, then=None):
        super().__init__("superseded")
        self.then = then


def lower_priority():
    """Below the server and everything else on the computer: the replays may take all of one core for a minute."""
    try:
        os.nice(19)
    except (AttributeError, OSError):
        pass
    if os.name == "nt":
        try:
            import ctypes
            below_normal = 0x00004000
            kernel = ctypes.windll.kernel32
            kernel.SetPriorityClass(kernel.GetCurrentProcess(), below_normal)
        except Exception:  # noqa: BLE001 - a normal priority still works
            pass


_pids = {}


def pid_class(spec):
    """The PIDArduino class of the PID_HERMS plugin the server runs, by its module's name, or from its file when that
    name can't be imported here; with its OUTER_LOOP_RATIO and INTEGRAL_CARRY_MAX_AGE."""
    name, path = spec.get("module"), spec.get("file")
    key = (name, path)
    if key not in _pids:
        module = None
        if name:
            try:
                module = importlib.import_module(name)
            except ImportError:
                module = None
        if module is None:
            if not path:
                raise ImportError("PID_HERMS's module %r can't be imported, and no file was given" % (name,))
            loaded = importlib.util.spec_from_file_location("strike_pid_herms", path)
            module = importlib.util.module_from_spec(loaded)
            loaded.loader.exec_module(module)
        logic = getattr(module, "PID_HERMS", None)
        _pids[key] = (module.PIDArduino, getattr(logic, "OUTER_LOOP_RATIO", 5),
                      getattr(logic, "INTEGRAL_CARRY_MAX_AGE", 300))
    return _pids[key]


def job_from(job):
    """(Brew, Rig, Grid, rule) from a job's numbers: brew, plant, herms, flow (all in F, gallons and kg), pid, unit
    and rule."""
    pid, ratio, carry = pid_class(job["pid"])
    brew = model.Brew(**job["brew"])
    plant = job["plant"]
    guesses = {name: model.Ranged(**fields) for name, fields in plant["guesses"].items()}
    rig = model.Rig(plant=model.Plant(hlt=model.Hlt(**plant["hlt"]), guesses=guesses),
                    herms=model.Herms(pid=pid, outer_ratio=ratio, integral_carry_s=carry, **job["herms"]),
                    flow=model.Flow(**job["flow"]))
    return brew, rig, model.Grid(job.get("unit", "F")), job.get("rule", "no worse")


def step_of(rig):
    """The replay's step for the Mash Tun's SampleTime. StrikeController offers nothing for a SampleTime no step
    divides, so the 2 s step left for one only ends in the model's own error."""
    return model.step_for(rig.herms.sample_time) or model.STEP_S


def _number(x):
    return None if x is None or not math.isfinite(x) else x


def forecast_out(fc, roles):
    return {"strike_f": fc.strike_f, "for": list(roles), "searched": fc.searched, "low_f": fc.low_f,
            "high_f": fc.high_f, "highest_f": fc.highest_f, "reads_f": _number(fc.reads_f), "sims": fc.sims}


class Worker:

    def __init__(self, inbox, send):
        self.inbox = inbox
        self.send = send
        self.later = None

    def serve(self):
        while True:
            message, self.later = (self.later or self.inbox.get()), None
            kind = message.get("kind")
            if kind == "stop":
                return
            if kind == "forecast":
                self.answer_forecast(message)
            elif kind == "plan":
                try:
                    self.run_plan(message)
                except Superseded as dropped:
                    self.later = dropped.then

    def tick(self):
        """Before every replay of a plan: answer the forecasts waiting, and drop the plan for a newer one."""
        while True:
            try:
                message = self.inbox.get_nowait()
            except queue.Empty:
                return
            kind = message.get("kind")
            if kind == "forecast":
                self.answer_forecast(message)
            elif kind in ("plan", "stop"):
                raise Superseded(message)
            elif kind == "cancel":
                raise Superseded(None)

    def answer_forecast(self, message):
        try:
            brew, rig, _, _ = job_from(message["job"])
            fc = model.forecast(brew, message["strike_f"], rig, message["cases"], dt=step_of(rig))
            self.send({"id": message.get("id"), "forecast": forecast_out(fc, message.get("for") or [])})
        except Exception as e:  # noqa: BLE001 - answered as an error; the worker carries on
            self.send({"id": message.get("id"), "error": _said(e)})

    def run_plan(self, message):
        ident = message.get("id")
        job = message["job"]
        try:
            brew, rig, grid, rule = job_from(job)
            dt = step_of(rig)
            box = model.ranges(brew, rig.plant)
            planned = model.plan(brew, rig, box=box, rule=rule, dt=dt, grid=grid, tick=self.tick)
        except Superseded:
            raise
        except Exception as e:  # noqa: BLE001
            self.send({"id": ident, "part": "use", "error": _said(e)})
            return
        strikes = [dict(s) for s in job.get("strikes") or []]
        use_f = planned.use_f
        if use_f is not None:
            same = next((s for s in strikes if abs(s["strike_f"] - use_f) < 1e-6), None)
            if same is None:
                strikes.append({"strike_f": use_f, "for": ["use"]})
            else:
                same["for"] = list(same["for"]) + ["use"]
        found = planned.found
        cases = model.quick_cases(box, found, planned.cautious)
        forecasts = []
        sims = planned.sims
        # Beyond MashInStep's own limits - more than its rise above the rest, or over its ceiling - is too far to
        # offer: the screen shows no forecast for it.
        too_far = model.beyond_limits(brew.rest_f, use_f, planned.far)
        if not too_far:
            try:
                for s in strikes:
                    fc = model.forecast(brew, s["strike_f"], rig, cases, dt=dt, tick=self.tick)
                    sims += fc.sims
                    forecasts.append(forecast_out(fc, s["for"]))
            except Superseded:
                raise
            except Exception as e:  # noqa: BLE001
                self.send({"id": ident, "part": "use", "error": _said(e)})
                return
        self.send({"id": ident, "part": "use", "use_f": use_f, "use_exact_f": planned.use_exact_f,
                   "far": too_far, "sims": sims, "found": found, "cautious": planned.cautious,
                   "cases": cases, "forecasts": forecasts})
        if too_far:
            self.send({"id": ident, "part": "done"})
            return
        try:
            searched = []
            for s in strikes:
                fc = model.searched_forecast(brew, s["strike_f"], rig, box, found, cases, dt=dt, tick=self.tick)
                searched.append(forecast_out(fc, s["for"]))
            self.send({"id": ident, "part": "searched", "forecasts": searched})
        except Superseded:
            raise
        except Exception as e:  # noqa: BLE001
            self.send({"id": ident, "part": "searched", "error": _said(e)})
        if job.get("levers", True):
            try:
                rise, mid_f = model.levers(brew, rig, box, use_f, rule=rule, dt=dt, grid=grid, tick=self.tick)
                self.send({"id": ident, "part": "levers", "rise": rise, "mid_f": mid_f})
            except Superseded:
                raise
            except Exception as e:  # noqa: BLE001
                self.send({"id": ident, "part": "levers", "error": _said(e)})
        self.send({"id": ident, "part": "done"})


def _said(error):
    return "%s: %s" % (type(error).__name__, error) if str(error) else type(error).__name__


def _read(stream, inbox):
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(message, dict):
            inbox.put(message)
    inbox.put({"kind": "stop"})


def main():
    lower_priority()
    logging.disable(logging.CRITICAL)
    out = sys.stdout.buffer
    # Anything else that writes to stdout - a module's print - goes to stderr instead, so stdout carries only answers.
    sys.stdout = sys.stderr
    lock = threading.Lock()

    def send(message):
        data = (json.dumps(message, allow_nan=False) + "\n").encode("utf-8")
        with lock:
            out.write(data)
            out.flush()

    inbox = queue.Queue()
    threading.Thread(target=_read, args=(sys.stdin.buffer, inbox), name="strike-inbox", daemon=True).start()
    try:
        Worker(inbox, send).serve()
    except Exception:  # noqa: BLE001 - said before the process ends, so the server knows why
        try:
            send({"fatal": traceback.format_exc(limit=8)})
        except Exception:  # noqa: BLE001
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
