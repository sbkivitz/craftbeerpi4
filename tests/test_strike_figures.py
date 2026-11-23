"""For CraftBeerPi's Figures page: where each brew's strike water landed, and what the rig has measured.

- S1: the first time the malt is in for the day's steps (MashIn on the Mash Tun done), the strike water box's forecast
  for the strike water in force goes into config/strike_landings.jsonl, once a brew day, with when the malt went in
  (the brewer's Next: the next step's start), the numbers and the plant file it worked from, and the model's inputs;
  none, and why, when the box had no forecast for it.
- S2: the fit of the tun's steel to the landings (cbpi.strike.landings): the mash probe's readings in each landing's
  window, from the live logs and archived simulator runs; the model's probe compared like with like, by least
  squares; a probe at the coil's outlet, a window not over, no readings and a probe that can't tell the steel each
  said; a landing found once kept when its log has rotated away.
- S3: the strike state's `measured`, the numbers the rig has measured, with their date, method and brews; and
  set_measured / clear_measured, which write them to config/strike_plant.json for Figures' USE and BACK TO.

They fail on 4.7.7.post61, which has none of these.
"""

import datetime
import json
import logging
import sys
import types
from unittest import mock

import pytest

from cbpi.api.dataclasses import Props, Step, StepState
from cbpi.controller import strike_controller as sc
from cbpi.controller.strike_controller import StrikeController, read_plant
from cbpi.strike import landings, model, worker


class Folder:
    def __init__(self, root):
        self.root = root
        self.logsFolderPath = str(root / "logs")

    def get_file_path(self, name):
        return str(self.root / name)


class Herms:
    OUTER_LOOP_RATIO = 4

    @staticmethod
    def blank_defaults(unit):
        return {}


HERMS_MODULE = types.ModuleType("strike_figures_pid_herms")
HERMS_MODULE.PIDArduino = object
Herms.__module__ = HERMS_MODULE.__name__


def kettle(kettle_id, name, logic, sensor, heater, props=None):
    return types.SimpleNamespace(id=kettle_id, name=name, type=logic, sensor=sensor, heater=heater, agitator=None,
                                 props=Props(props or {}), target_temp=0, instance=None)


def helper(tmp_path, monkeypatch, profile):
    monkeypatch.setitem(sys.modules, HERMS_MODULE.__name__, HERMS_MODULE)
    kettles = [kettle("hlt", "HLT", "Hysteresis", "hlt-probe", "hlt-heater"),
               kettle("mt", "Mash Tun", "PID_HERMS", "mt-probe", "hlt-heater", {"HLT_Kettle": "hlt"}),
               kettle("bk", "Boil Kettle", "BoilPower", None, "bk-heater")]
    cbpi = types.SimpleNamespace(
        config=types.SimpleNamespace(get=lambda key, default=None: {"TEMP_UNIT": "F"}.get(key, default)),
        config_folder=Folder(tmp_path),
        step=types.SimpleNamespace(profile=profile, basic_data={"name": "Rye Pale Ale"}, save=mock.AsyncMock()),
        kettle=types.SimpleNamespace(data=kettles, set_target_temp=mock.AsyncMock(),
                                     types={"PID_HERMS": {"class": Herms, "properties": [{"label": "HLT_Sensor"}]}}),
        sensor=types.SimpleNamespace(find_by_id=lambda sensor_id: None, get_sensor_value=lambda sensor_id: None))
    return StrikeController(cbpi, watch=False)


NEXT_AT = 1795327740.0


def brew_day(malt_in=StepState.DONE, strike_temp=151.9, started=NEXT_AT):
    """A strike water step on the HLT, MashIn on the Mash Tun, and the rest after it."""
    rest_props = {"Kettle": "mt", "Temp": 152}
    if started is not None:
        rest_props["_started_at"] = started
    return [Step("sw", "Strike water", type="MashInStep", status=StepState.DONE,
                 props=Props({"Kettle": "hlt", "Temp": strike_temp})),
            Step("mi", "MashIn", type="MashInStep", status=malt_in, props=Props({"Kettle": "mt", "Temp": 152})),
            Step("rest", "Saccharification", type="MashStep",
                 status=StepState.ACTIVE if malt_in == StepState.DONE else StepState.INITIAL, props=Props(rest_props))]


def answer(strike="151.9", unit="F"):
    lo = [149.6 + m * 0.01 for m in range(61)]
    hi = [152.4 + m * 0.01 for m in range(61)]
    return dict(plan="p1", unit=unit, use=151.9, numbers=dict(rest=152.0, vol=15.0, room=60.4, probe="coil",
                                                              key="digest"),
                forecasts={strike: dict(strike=float(strike), searched=True, peak=152.3, reads=153.0, lo=lo, hi=hi)},
                _job=dict(brew=dict(probe="coil"), plant={}, herms={}, flow={}, pid={}), _cases={})


def on_the_day(strike, profile, given=None):
    """The brew day is the profile's, with `given` as its answer."""
    strike.day = {"steps": [s.id for s in profile], "vol": None, "grain": None, "was": None, "used": None,
                  "landed": None}
    strike.answer = given
    return strike


def kept(tmp_path):
    return landings.read_landings(tmp_path / landings.LANDINGS_FILE)


# ------------------------------------------------------------------ S1: the landing

def test_as_the_malt_goes_in_the_forecast_for_the_strike_in_force_is_kept_once(tmp_path, monkeypatch):
    profile = brew_day()
    strike = on_the_day(helper(tmp_path, monkeypatch, profile), profile, answer())
    strike.update()
    strike.update()
    found = kept(tmp_path)
    assert len(found) == 1, "kept more than once"
    record = found[0]
    assert (record["version"], record["t"], record["steps"], record["unit"]) == (1, NEXT_AT, ["sw", "mi", "rest"], "F")
    assert (record["strike"], record["rest"], record["lands"]) == (151.9, 152.0, [149.6, 152.4])
    assert len(record["lo"]) == len(record["hi"]) == 61 and record["searched"] is True
    assert record["numbers"] == dict(rest=152.0, vol=15.0, room=60.4, probe="coil"), "the key is left out"
    assert record["plant"]["probe"] == "coil" and "guesses" in record["plant"], "the plant file as read then"
    assert (record["sensor"], record["plan"], record["name"]) == ("mt-probe", "p1", "Rye Pale Ale")
    assert record["job"] == answer()["_job"]
    day = json.loads((tmp_path / "strike_day.json").read_text())
    assert day["landed"] is True and strike.day["landed"] is True


def test_without_the_next_steps_start_the_malt_went_in_now(tmp_path, monkeypatch):
    profile = brew_day(started=None)
    strike = on_the_day(helper(tmp_path, monkeypatch, profile), profile, answer())
    monkeypatch.setattr(sc.clock, "now", lambda: 1795330000.0)
    strike.update()
    assert kept(tmp_path)[0]["t"] == 1795330000.0


def test_before_the_malt_goes_in_nothing_is_kept(tmp_path, monkeypatch):
    profile = brew_day(malt_in=StepState.ACTIVE)
    strike = on_the_day(helper(tmp_path, monkeypatch, profile), profile, answer())
    strike.update()
    assert kept(tmp_path) == [] and strike.day["landed"] is None


@pytest.mark.parametrize("given, why", [(None, "no_answer"), (answer(strike="150.0"), "no_answer")],
                         ids=["no-answer", "no-forecast-for-the-strike"])
def test_no_forecast_for_the_strike_in_force_keeps_none_and_says_why(tmp_path, monkeypatch, caplog, given, why):
    caplog.set_level(logging.INFO)
    profile = brew_day()
    strike = on_the_day(helper(tmp_path, monkeypatch, profile), profile, given)
    strike.update()
    assert kept(tmp_path) == [] and strike.day["landed"] == why
    assert "no landing is kept for Figures" in caplog.text
    strike.answer = answer()
    strike.update()
    assert kept(tmp_path) == [], "the brew day's answer is final"


def test_by_hand_the_hlt_target_seen_before_the_malt_went_in_stands_for_it(tmp_path, monkeypatch):
    profile = brew_day()[1:]                        # no strike water step: by hand
    strike = on_the_day(helper(tmp_path, monkeypatch, profile), profile, answer())
    profile[0].status, profile[1].status = StepState.ACTIVE, StepState.INITIAL
    hlt = strike.cbpi.kettle.data[0]
    hlt.target_temp = 151.9
    strike.update()
    profile[0].status, profile[1].status = StepState.DONE, StepState.ACTIVE
    hlt.target_temp = 157.3                         # PID_HERMS's setpoint by now
    strike.update()
    assert [r["strike"] for r in kept(tmp_path)] == [151.9]


# ------------------------------------------------------------------ S3: the numbers measured

def plant_with(tmp_path, **measured):
    data = sc.default_plant_file()
    for name, entry in measured.items():
        data["guesses"].setdefault(name, {})["measured"] = entry
    (tmp_path / "strike_plant.json").write_text(json.dumps(data), encoding="utf-8")
    return data


def test_the_numbers_measured_are_listed_with_their_date_method_and_brews(tmp_path):
    data = plant_with(tmp_path, tun_share=dict(value=0.62, date="2026-11-23T10:15:00", method="figures",
                                               brews=["a", "b", "c", "d"]),
                      transfer_f=dict(value=0.9, date="2026-11-20T09:00:00", method="thermometer"))
    plant, _ = read_plant(data)
    brew = model.Brew(grain_kg=10.0, strike_gal=8.0, rest_f=152.0, hlt_total_gal=15.0, tun_kg=15.0, tun_cal_g_c=0.12,
                      garage_f=60.0)
    box = model.ranges(brew, plant)
    assert StrikeController._measured_shown(box, "F", data) == [
        dict(name="tun_share", value=0.62, unit="share", date="2026-11-23T10:15:00", method="figures", brews=4),
        dict(name="transfer_f", value=0.9, unit="F", date="2026-11-20T09:00:00", method="thermometer", brews=None)]
    assert StrikeController._measured_shown(box, "C", data)[1] == dict(
        name="transfer_f", value=0.5, unit="C", date="2026-11-20T09:00:00", method="thermometer", brews=None)
    shown = StrikeController._guesses_shown(box, "F", "gal")
    assert {"tun_share", "transfer_f"}.isdisjoint(g["name"] for g in shown), "a number measured isn't a range"
    plain, _ = read_plant(sc.default_plant_file())
    assert StrikeController._measured_shown(model.ranges(brew, plain), "F", {}) == []


def test_the_state_sends_measured_whenever_it_sends_the_guesses(tmp_path, monkeypatch):
    strike = helper(tmp_path, monkeypatch, brew_day(malt_in=StepState.INITIAL))
    look = strike._look()
    look.guesses, look.measured, look.miss = [{"name": "next_min"}], [{"name": "tun_share"}], None
    assert strike.get_state(look)["measured"] == [{"name": "tun_share"}]
    look.miss = "recipe"
    assert strike.get_state(look)["measured"] is None and strike.get_state(look)["guesses"] is None


def test_set_measured_writes_the_plant_file_and_clear_takes_it_out(tmp_path, monkeypatch):
    strike = helper(tmp_path, monkeypatch, brew_day(malt_in=StepState.INITIAL))
    monkeypatch.setattr(sc.clock, "now", lambda: datetime.datetime(2026, 11, 23, 10, 15).timestamp())
    out = strike.set_measured("tun_share", 0.62, brews=["log:now:1", "log:now:2"])
    assert out == dict(name="tun_share", before=None, range=dict(low=0.45, central=0.75, high=1.0),
                       measured=dict(value=0.62, date="2026-11-23T10:15:00", method="figures",
                                     brews=["log:now:1", "log:now:2"]))
    data = json.loads((tmp_path / "strike_plant.json").read_text())
    assert data["guesses"]["tun_share"]["measured"] == out["measured"]
    assert data["guesses"]["tun_share"]["low"] == 0.45, "the range is kept beside it, for BACK TO"
    plant, _ = read_plant(data)
    r = plant.guesses["tun_share"]
    assert (r.low, r.central, r.high, r.provenance) == (0.62, 0.62, 0.62, model.MEASURED)
    again = strike.set_measured("tun_share", 0.6, brews=["log:now:3"])
    assert again["before"] == out["measured"]
    assert strike.clear_measured("tun_share") == again["measured"]
    plant, _ = read_plant(json.loads((tmp_path / "strike_plant.json").read_text()))
    assert plant.guesses["tun_share"].provenance == model.UNMEASURED and plant.guesses["tun_share"].low == 0.45
    assert strike.clear_measured("tun_share") is None


@pytest.mark.parametrize("name, value, brews", [("tun_shares", 0.6, None), ("tun_share", 1.4, None),
                                                ("tun_share", "x", None), ("tun_share", 0.6, "brew")])
def test_set_measured_refuses_what_cant_be_a_measured_number(tmp_path, monkeypatch, name, value, brews):
    strike = helper(tmp_path, monkeypatch, brew_day(malt_in=StepState.INITIAL))
    with pytest.raises(ValueError):
        strike.set_measured(name, value, brews=brews)
    assert not (tmp_path / "strike_plant.json").exists()


# ------------------------------------------------------------------ S2: the probe's readings

def write_log(path, start, end, value, step=2.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    t = start
    while t <= end:
        rows.append("%s,%s" % (datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                               value(t) if callable(value) else value))
        t += step
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_the_probes_readings_in_the_window_come_from_the_live_and_archived_logs(tmp_path):
    logs = tmp_path / "logs"
    t = NEXT_AT
    write_log(logs / "sensor_mt-probe.log.1", t, t + 600, 150.0)              # rotated: the older rows
    write_log(logs / "sensor_mt-probe.log", t + 602, t + 1200, 152.0)
    write_log(logs / "sensor_other.log", t, t + 1200, 99.0)
    write_log(logs / "archive" / "20261123-101500-simulation" / "sensor_mt-probe.log", t - 7200, t - 3600, 140.0)
    start, end = landings.window({"t": t})
    found = landings.probe_readings(str(logs), "mt-probe", start, end)
    assert found[0][0] >= start and found[-1][0] <= end and [v for _, v in found] == sorted(v for _, v in found)
    assert {v for _, v in found} == {150.0, 152.0}
    assert landings.landed_from(found, start, end) == pytest.approx((150.0 * 151 + 152.0 * 150) / 301, abs=0.001)
    early = landings.probe_readings(str(logs), "mt-probe", t - 7000, t - 6000)
    assert {v for _, v in early} == {140.0}, "an archived simulator run's readings"


def test_a_landing_needs_readings_over_half_its_window(tmp_path):
    start, end = 0.0, 600.0
    assert landings.landed_from([(10.0 * i, 150.0) for i in range(4)], start, end) is None
    assert landings.landed_from([(10.0 * i, 150.0) for i in range(20)], start, end) is None
    assert landings.landed_from([(20.0 * i, 150.0) for i in range(20)], start, end) == 150.0


def test_the_models_window_average_is_what_readings_all_through_it_average_to():
    minutes = [100.0 + m for m in range(61)]
    assert landings.window_average(minutes, 10.0, 20.0) == pytest.approx(115.0)
    assert landings.window_average([0.0, 10.0, 10.0] + [10.0] * 58, 0.0, 2.0) == pytest.approx(7.5)


def test_a_broken_line_in_the_landings_file_is_left_out(tmp_path):
    path = tmp_path / landings.LANDINGS_FILE
    landings.append_landing(path, {"t": 1.0, "steps": ["a"]})
    with open(path, "ab") as f:
        f.write(b'{"t": 2.0, "ste\x00\x00')
    landings.append_landing(path, {"t": 3.0, "steps": ["b"]})
    assert [r["t"] for r in landings.read_landings(path)] == [1.0, 3.0]


# ------------------------------------------------------------------ S2: the fit

def entry(lid, landed, slope=-1.5, at=124.0, unit="F", why=None):
    """A landing as strike_fit.json keeps it, whose model reads `at` at 75 % and `slope` F more for each 1.0 of
    share."""
    mid = [landings.from_f(at + slope * (v - 0.75), unit) for v in landings.GRID]
    return dict(id=lid, unit=unit, landed=None if landed is None else landings.from_f(landed, unit), why=why, mid=mid,
                lo0=[x - 2.0 for x in mid], hi0=[x + 2.0 for x in mid])


def test_the_fit_is_the_least_squares_share_across_the_landings_counted():
    one = entry("a", 124.0 - 1.5 * (0.60 - 0.75))
    two = entry("b", 124.0 - 1.5 * (0.64 - 0.75), unit="C")
    out = landings.fit([one, two])
    assert out["value"] == 0.62 and out["brews"] == 2 and out["why"] is None and out["edge"] is False
    assert out["rms_f"] == pytest.approx(0.03, abs=0.001)
    assert out["landings"]["a"]["off"] == pytest.approx(0.03, abs=0.005)
    assert landings.best([one])[0] == 0.60 and landings.best([two])[0] == 0.64
    left = landings.fit([one, dict(two, why="probe_at_coil")])
    assert left["value"] == 0.60 and left["brews"] == 1


@pytest.mark.parametrize("whys, said", [((), "no_landings"), (("no_readings", "waiting"), "waiting"),
                                        (("blind", "probe_at_coil"), "probe_at_coil"),
                                        (("no_readings", "blind"), "blind")])
def test_with_none_counted_the_fit_says_why(whys, said):
    out = landings.fit([entry(str(i), None if w in ("waiting", "no_readings") else 124.0, why=w)
                        for i, w in enumerate(whys)])
    assert (out["value"], out["why"], out["brews"]) == (None, said, 0)


def landing_record(t=NEXT_AT, probe="tun", unit="F"):
    return dict(version=1, t=t, steps=["sw", "mi", "rest"], name="Rye Pale Ale", unit=unit, strike=151.9, rest=152.0,
                numbers=dict(probe=probe), sensor="mt-probe", job=dict(brew=dict(probe=probe)))


def test_each_landing_says_why_it_doesnt_count(tmp_path, monkeypatch):
    strike = helper(tmp_path, monkeypatch, brew_day(malt_in=StepState.INITIAL))
    now = NEXT_AT + 3600
    monkeypatch.setattr(sc.clock, "now", lambda: now)
    flat = dict(mid=[124.0] * 56, lo0=[122.0] * 56, hi0=[126.0] * 56)
    moving = {k: [124.0 - 1.5 * (v - 0.75) + (0 if k == "mid" else (-2.0 if k == "lo0" else 2.0))
                  for v in landings.GRID] for k in ("mid", "lo0", "hi0")}
    records = [landing_record(t=NEXT_AT), landing_record(t=NEXT_AT + 1), landing_record(t=NEXT_AT + 2, probe="coil"),
               landing_record(t=now - 300), landing_record(t=NEXT_AT + 3)]
    found = [dict(id=landings.landing_id(records[0]), landed=124.1, readings=300, curves=moving),
             dict(id=landings.landing_id(records[1]), landed=124.1, readings=300, curves=flat),
             dict(id=landings.landing_id(records[2]), landed=126.0, readings=300),
             dict(id=landings.landing_id(records[3]), landed=None, readings=0, curves=moving),
             dict(id=landings.landing_id(records[4]), landed=None, readings=0, curves=moving)]
    strike._fit_take(records, {"size": 1, "mtime_ns": 1}, found)
    fit = strike.fit
    assert [e["why"] for e in fit["landings"]] == [None, "blind", "probe_at_coil", "waiting", "no_readings"]
    assert fit["value"] == pytest.approx(0.68) and fit["brews"] == 1
    assert json.loads((tmp_path / landings.FIT_FILE).read_text())["value"] == fit["value"]
    assert strike._fit_recheck is not None, "looked at again once the waiting window is over"
    assert strike.fit_state() == dict(fit=fit, working=False)


def test_a_landing_found_once_is_kept_when_its_log_has_gone(tmp_path, monkeypatch):
    strike = helper(tmp_path, monkeypatch, brew_day(malt_in=StepState.INITIAL))
    monkeypatch.setattr(sc.clock, "now", lambda: NEXT_AT + 86400 * 5)
    moving = {k: [124.0 - 1.5 * (v - 0.75) for v in landings.GRID] for k in ("mid", "lo0", "hi0")}
    record = landing_record()
    lid = landings.landing_id(record)
    strike._fit_take([record], {"size": 1, "mtime_ns": 1}, [dict(id=lid, landed=124.1, readings=300, curves=moving)])
    strike._fit_take([record, landing_record(t=NEXT_AT + 9)], {"size": 2, "mtime_ns": 2},
                     [dict(id=lid, landed=None, readings=0)])
    assert strike.fit["landings"][0]["landed"] == 124.1 and strike.fit["landings"][0]["why"] is None


def test_the_fit_has_a_worker_of_its_own(tmp_path, monkeypatch):
    strike = helper(tmp_path, monkeypatch, brew_day(malt_in=StepState.INITIAL))
    assert strike.fitter is not strike.client and strike.fitter.on_message == strike._fit_heard


# ------------------------------------------------------------------ S2: the worker

def test_fit_landing_reads_the_probe_and_runs_the_model(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    write_log(logs / "sensor_mt-probe.log", NEXT_AT + 240, NEXT_AT + 960, 124.5)
    monkeypatch.setattr(worker, "landing_curves", lambda record, tick=None: {"mid": [1.0], "lo0": [], "hi0": []})
    out = worker.fit_landing(landing_record(), str(logs))
    assert (out["landed"], out["readings"], out["curves"]["mid"]) == (124.5, 301, [1.0])
    assert "curves" not in worker.fit_landing(landing_record(), str(logs), curves=False)
    assert worker.fit_landing({"t": "x"}, str(logs))["error"]


def test_a_newer_fit_supersedes_the_one_in_hand():
    import queue
    inbox, sent = queue.Queue(), []
    w = worker.Worker(inbox, sent.append)
    inbox.put({"kind": "fit", "id": 2, "landings": []})
    with pytest.raises(worker.Superseded) as dropped:
        w.run_fit({"kind": "fit", "id": 1, "landings": [{"record": {}, "curves": False}]})
    assert dropped.value.then["id"] == 2 and sent == []
