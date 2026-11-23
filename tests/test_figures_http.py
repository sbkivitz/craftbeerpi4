"""Figures from past brews over HTTP (BUILDERS.md C1, C2, C4): GET /figures, GET /figures/brews/{id}, USE, BACK TO
and LEAVE OUT with every refusal, what they write and where it's kept across a restart, BrewScribe there, absent or
slow, a simulator's brewery and a rig's. On a stand-in for CraftBeerPi with the real FiguresController, its endpoints
and its worker process, served on a free port of the test's own."""

import asyncio
import datetime
import json
import pathlib
import shutil
import types
import uuid

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from cbpi.api.dataclasses import Kettle, Props, Sensor
from cbpi.controller.figures_controller import FiguresController
from cbpi.craftbeerpi import CraftBeerPi, error_middleware
from cbpi.http_endpoints.http_figures import FiguresHttpEndpoints

HERE = pathlib.Path(__file__).resolve().parent


@pytest.fixture
def root():
    path = HERE / ("temp-figures-%s" % uuid.uuid4().hex[:8])
    path.mkdir()
    yield path
    shutil.rmtree(path, ignore_errors=True)


HLT, MT, BK = "hltK", "mashK", "boilK"
HLT_P, MT_P, BK_P = "hltProbe", "mashProbe", "boilProbe"
HLT_E, BK_E, PUMP, HLT_PUMP = "hltElement", "boilElement", "wortPump", "hltPump"
LITRES, WATTS = 40.0, 6500.0
DAY = 86400.0
# A fixed start on the brewing clock: the logs are stamped with it, never with this PC's clock.
BASE = 1_790_000_000.0
STEP_S = 10.0


def stamp(t):
    return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class Logs:
    def __init__(self, folder):
        self.folder = pathlib.Path(folder)
        self.rows = {}

    def add(self, name, t, v):
        self.rows.setdefault(name, []).append((t, v))

    def write(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        for name, rows in self.rows.items():
            with open(self.folder / f"sensor_{name}.log", "a", encoding="utf-8", newline="\n") as f:
                for t, v in sorted(rows):
                    f.write(f"{stamp(t)},{v}\n")


def brew(logs, start, eff, boil_rate, over, pump_from_start=False, steps=True, brewstart=None):
    """One brew day on the logs from `start`: the HLT heated to 160 F by its element alone (or with the wort pump
    running) at `eff`, the mash to a rest at 152 F coming `over` F over, a step up to 168 F, and the boil kettle to
    the boil at `boil_rate` F a minute. Returns its times."""
    hlt_rate = eff * WATTS / (LITRES * 0.995 * 4181.0) * 1.8 * 60.0
    t_heat = start + 600.0
    heat_min = 100.0 / hlt_rate
    t_ready = t_heat + heat_min * 60.0
    t_mash = t_ready + 600.0
    climb = (152.0 + over - 60.0) / 1.5
    t_peak = t_mash + climb * 60.0
    t_flat = t_peak + 180.0
    t_up = t_flat + 3600.0
    t_168 = t_up + 16 * 60.0
    t_mash_end = t_168 + 600.0
    t_boil = t_mash_end + 600.0
    t_212 = t_boil + (212.0 - 60.0) / boil_rate * 60.0
    t_end = t_212 + 3600.0
    t = start
    while t <= t_end + 1800.0:
        if t < t_heat:
            h = 60.0
        elif t < t_ready:
            h = 60.0 + (t - t_heat) / 60.0 * hlt_rate
        else:
            h = 160.0
        if t < t_mash:
            m = 60.0 + (0.5 * (h - 60.0) / 100.0 if pump_from_start and t >= t_heat else 0.0)
        elif t < t_peak:
            m = 60.0 + (t - t_mash) / 60.0 * 1.5
        elif t < t_flat:
            m = 152.0 + over * (1.0 - (t - t_peak) / 180.0)
        elif t < t_up:
            m = 152.0
        elif t < t_168:
            m = 152.0 + (t - t_up) / 60.0
        elif t < t_mash_end:
            m = 168.0
        else:
            m = max(120.0, 168.0 - (t - t_mash_end) / 60.0 * 0.5)
        if t < t_boil:
            b = 60.0
        elif t < t_212:
            b = 60.0 + (t - t_boil) / 60.0 * boil_rate
        elif t < t_end:
            b = 212.0
        else:
            b = max(150.0, 212.0 - (t - t_end) / 60.0 * 2.0)
        logs.add(HLT_P, t, round(h, 2))
        logs.add(MT_P, t, round(m, 2))
        logs.add(BK_P, t, round(b, 2))
        t += STEP_S
    logs.add(f"{HLT}.target", start, 0)
    logs.add(f"{HLT}.target", t_heat, 160.0)
    logs.add(f"{HLT}.auto", start, 0)
    logs.add(f"{HLT}.auto", t_heat, 1)
    logs.add(f"{HLT}.auto", t_mash, 0)
    logs.add(f"{HLT_E}.out", start, 0)
    logs.add(f"{HLT_E}.out", t_heat, 100)
    logs.add(f"{HLT_E}.out", t_ready, 30)
    logs.add(f"{HLT_E}.out", t_mash_end, 0)
    logs.add(f"{PUMP}.out", start, 100 if pump_from_start else 0)
    logs.add(f"{PUMP}.out", t_mash, 100)
    logs.add(f"{PUMP}.out", t_mash_end, 0)
    logs.add(f"{MT}.target", t_mash, 152.0)
    logs.add(f"{MT}.target", t_up, 168.0)
    logs.add(f"{MT}.auto", start, 0)
    logs.add(f"{MT}.auto", t_mash, 1)
    logs.add(f"{MT}.auto", t_mash_end, 0)
    logs.add(f"{BK_E}.out", start, 0)
    logs.add(f"{BK_E}.out", t_boil, 100)
    logs.add(f"{BK_E}.out", t_end, 0)
    if steps:
        for when, n in ((t_heat, 1), (t_mash, 2), (t_up, 3), (t_boil, 4), (t_end, 0)):
            logs.add("steps.active", when, n)
    if brewstart:
        for key, value in brewstart.items():
            logs.add("brewstart." + key, t_heat, value)
        logs.add("brewstart.ready", t_ready, 160.0)
    return dict(start=start, heat=t_heat, ready=t_ready, mash=t_mash, flat=t_flat, up=t_up, boil=t_boil,
                at212=t_212, end=t_end)


def not_a_brew(logs, start):
    """The HLT heating the mash through the coil to 110 F and holding it there: never past 120 F, so no brew."""
    t = start
    while t <= start + 4 * 3600.0:
        k = min(1.0, max(0.0, (t - start - 600.0) / 1800.0))
        logs.add(HLT_P, t, round(60.0 + 55.0 * k, 2))
        logs.add(MT_P, t, round(60.0 + 50.0 * k, 2))
        logs.add(BK_P, t, 60.0)
        t += STEP_S


# ---------------------------------------------------------------------------------------------------------------
# A stand-in for CraftBeerPi
# ---------------------------------------------------------------------------------------------------------------

class Config:
    """ConfigController's get and set, kept in config.json: set changes only a setting that is registered."""

    def __init__(self, folder, values):
        self.path = folder / "config.json"
        if not self.path.exists():
            self.path.write_text(json.dumps(values), encoding="utf-8")
        self.cache = {k: types.SimpleNamespace(value=v) for k, v in json.loads(self.path.read_text()).items()}

    def get(self, name, default=None):
        item = self.cache.get(name)
        return item.value if item is not None and item.value is not None and item.value != "" else default

    async def set(self, name, value):
        if name in self.cache:
            self.cache[name].value = value
            self.path.write_text(json.dumps({k: v.value for k, v in self.cache.items()}), encoding="utf-8")

    def register(self, name, value=""):
        self.cache[name] = types.SimpleNamespace(value=value)
        self.path.write_text(json.dumps({k: v.value for k, v in self.cache.items()}), encoding="utf-8")


class Folder:
    def __init__(self, root):
        self.root = root
        self.logsFolderPath = str(root / "logs")

    def get_file_path(self, name):
        return str(self.root / "config" / name)


class Ws:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)


def stand_in(root, simulated=True, grain_kg=None, settings=None):
    cbpi = types.SimpleNamespace()
    cfg = root / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    cbpi.config = Config(cfg, dict({"TEMP_UNIT": "F", "BREWERY_NAME": "Test Brewery", "GRAIN_ABSORPTION": 0.12,
                                    "SIM_TIME_SCALE": 1}, **(settings or {})))
    cbpi.config_folder = Folder(root)
    cbpi.log = types.SimpleNamespace(logsFolderPath=str(root / "logs"))
    sim = "SimVessel" if simulated else "OneWire"
    sensors = {
        HLT_P: Sensor(id=HLT_P, name="HLT Temp", type=sim, props=Props({"Volume": LITRES, "Heater_Watts": WATTS})),
        MT_P: Sensor(id=MT_P, name="Mash Temp", type=sim, props=Props({"Grain_Kg": grain_kg} if grain_kg else {})),
        BK_P: Sensor(id=BK_P, name="Boil Temp", type=sim, props=Props({})),
    }
    cbpi.sensor = types.SimpleNamespace(find_by_id=sensors.get)
    cbpi.kettle = types.SimpleNamespace(types={}, data=[
        Kettle(id=HLT, name="HLT", type="PIDBoil", sensor=HLT_P, heater=HLT_E, agitator=HLT_PUMP, props=Props({})),
        Kettle(id=MT, name="Mash Tun", type="PID_HERMS", sensor=MT_P, heater=HLT_E, agitator=PUMP,
               props=Props({"DeltaTemp": 5.4, "HLT_Sensor": HLT_P})),
        Kettle(id=BK, name="Boil Kettle", type="BoilPower", sensor=BK_P, heater=BK_E, props=Props({}))])
    cbpi.step = types.SimpleNamespace(basic_data={}, add_listener=lambda fn: None)
    cbpi.recipe = types.SimpleNamespace(_book_entries=lambda: list(getattr(cbpi, "book", [])))
    cbpi.ws = Ws()
    cbpi.static_config = {"port": 1}
    cbpi.strike = object()
    cbpi.app = web.Application(middlewares=[web.normalize_path_middleware(), error_middleware])
    cbpi.register = lambda obj, url_prefix=None, static=None: CraftBeerPi.register_http_endpoints(
        cbpi, obj, url_prefix, static)
    cbpi.figures = FiguresController(cbpi)
    cbpi.http_figures = FiguresHttpEndpoints(cbpi)
    return cbpi


class Server:
    """The stand-in's app on a free port, with BrewScribe's routes as the test gives them."""

    def __init__(self, cbpi, brewscribe=None):
        self.cbpi = cbpi
        if brewscribe is not None:
            for path, handler in brewscribe.items():
                cbpi.app.router.add_get(path, handler)

    async def __aenter__(self):
        self.client = TestClient(TestServer(self.cbpi.app, host="127.0.0.1"))
        await self.client.start_server()
        port = self.client.server.port
        if port in (8000, 8012, 8100):
            raise RuntimeError("port %d is the brewery's" % port)
        return self

    async def __aexit__(self, *exc):
        await self.cbpi.figures._cleanup()
        await self.client.close()

    async def call(self, path, body=None, method="GET"):
        if method == "GET":
            r = await self.client.get(path)
        else:
            r = await self.client.post(path, data=json.dumps(body) if not isinstance(body, str) else body)
        try:
            data = await r.json(content_type=None)
        except ValueError:
            data = await r.text()
        return r.status, data


def by_start(figs):
    return sorted(figs["brews"], key=lambda b: b["start"])


def topics(cbpi):
    return [m["data"] for m in cbpi.ws.sent if m.get("topic") == "figures"]


def run(coro):
    return asyncio.run(coro)


def four_brews(root):
    logs = Logs(root / "logs")
    a = brew(logs, BASE, 0.84, 2.0, 0.3)
    b = brew(logs, BASE + DAY, 0.86, 2.1, 0.3)
    c = brew(logs, BASE + 2 * DAY, 0.95, 3.0, 0.3)
    d = brew(logs, BASE + 3 * DAY, 0.85, 2.05, 1.0, pump_from_start=True)
    not_a_brew(logs, BASE + 4 * DAY)
    logs.write()
    return {k: "log:now:%d" % int(v["heat"]) for k, v in dict(a=a, b=b, c=c, d=d).items()}, a


def test_get_figures(root):
    ids, a = four_brews(root)

    async def go():
        async with Server(stand_in(root)) as s:
            return await s.call("/figures")

    status, figs = run(go())
    assert status == 200
    assert {k: figs[k] for k in ("version", "brewery", "simulated", "unit", "vol_unit", "mass_unit", "brewscribe",
                                 "scale")} == {"version": 1, "brewery": "Test Brewery", "simulated": True, "unit": "F",
                                               "vol_unit": "gal", "mass_unit": "lb", "brewscribe": "none",
                                               "scale": 1.0}
    assert [b["id"] for b in figs["brews"]] == [ids["d"], ids["c"], ids["b"], ids["a"]]
    cells = {b["id"]: b["cells"] for b in figs["brews"]}
    assert cells[ids["a"]]["hlt"]["eff"] == pytest.approx(0.84, abs=0.004)
    assert cells[ids["d"]]["hlt"] == {"none": "pump_on"}
    assert cells[ids["a"]]["strike"] == {"none": "no_grain"}
    assert cells[ids["a"]]["grain"] == {"none": "no_brewscribe"}
    assert [r["target"] for r in cells[ids["a"]]["mash"]["rests"]] == [152.0, 168.0]
    assert cells[ids["a"]]["boil"]["rate"] == 2.0
    f = figs["figures"]
    assert f["hlt"]["state"] == "disagree" and f["hlt"]["why_not"] == "brewstart_old"
    assert f["hlt"]["set"] == {"setting": "BREWSTART_EFFICIENCY", "value": 0.9, "built_in": True, "since": None}
    assert f["mash"]["state"] == "caution"
    assert f["mash"]["set"] == {"setting": "DeltaTemp", "value": 5.4, "kettle": MT}
    assert f["boil"]["state"] == "disagree" and f["boil"]["set"] is None
    assert f["grain"]["state"] == "none"
    assert f["strike"]["state"] == "none"


def test_use_back_and_leave_out_kept_across_a_restart(root):
    ids, a = four_brews(root)
    two = [ids["b"], ids["a"]]

    async def first():
        cbpi = stand_in(root)
        async with Server(cbpi) as s:
            out = {}
            out["leave"] = await s.call("/figures/hlt/out", {"brew": ids["c"], "out": True}, "POST")
            out["old"] = await s.call("/figures/hlt/use", {"value": 0.85, "brews": two}, "POST")
            cbpi.config.register("BREWSTART_EFFICIENCY", "")
            out["changed"] = await s.call("/figures/hlt/use", {"value": 0.85, "brews": [ids["a"], ids["c"]]}, "POST")
            out["value"] = await s.call("/figures/hlt/use", {"value": 0.86, "brews": two}, "POST")
            out["bad"] = await s.call("/figures/hlt/use", {"brews": two}, "POST")
            out["mash"] = await s.call("/figures/mash/use", {"value": 1, "brews": []}, "POST")
            out["use"] = await s.call("/figures/hlt/use", {"value": 0.85, "brews": two}, "POST")
            out["again"] = await s.call("/figures/hlt/use", {"value": 0.85, "brews": two}, "POST")
            out["setting"] = cbpi.config.get("BREWSTART_EFFICIENCY")
            out["topics"] = topics(cbpi)
            return out

    out = run(first())
    status, body = out["leave"]
    assert status == 200 and body["figures"]["hlt"]["state"] == "use"
    assert body["figures"]["hlt"]["use"] == {"value": 0.85, "brews": two}
    assert out["old"][0] == 409 and out["old"][1]["error"] == "brewstart_old" and "figures" in out["old"][1]
    assert (out["changed"][0], out["changed"][1]["error"]) == (409, "brews_changed")
    assert (out["value"][0], out["value"][1]["error"]) == (409, "brews_changed")
    assert (out["bad"][0], out["bad"][1]["error"]) == (400, "bad_body")
    assert (out["mash"][0], out["mash"][1]["error"]) == (404, "no_figure")
    status, body = out["use"]
    assert status == 200
    assert body["applied"] == {"figure": "hlt", "to": 0.85, "from": 0.9, "from_built_in": True}
    assert body["figures"]["hlt"]["state"] == "in_use"
    assert (out["again"][0], out["again"][1]["error"]) == (409, "not_offered")
    assert out["setting"] == 85
    assert out["topics"] == [{"changed": "out", "figure": "hlt"}, {"changed": "use", "figure": "hlt"}]
    kept = json.loads((root / "config" / "figures.json").read_text())
    assert kept["out"] == {"hlt": [ids["c"]]}
    assert kept["in_use"]["hlt"]["value"] == 0.85 and kept["in_use"]["hlt"]["was"] is None

    async def after_restart():
        cbpi = stand_in(root)
        async with Server(cbpi) as s:
            out = {"figs": await s.call("/figures")}
            out["wrong"] = await s.call("/figures/hlt/back", {"value": 0.84}, "POST")
            out["back"] = await s.call("/figures/hlt/back", {"value": 0.85}, "POST")
            out["none"] = await s.call("/figures/hlt/back", {"value": 0.85}, "POST")
            out["setting"] = cbpi.config.cache["BREWSTART_EFFICIENCY"].value
            out["no_brew"] = await s.call("/figures/hlt/out", {"brew": "log:now:1", "out": True}, "POST")
            out["no_figure"] = await s.call("/figures/x/out", {"brew": ids["a"], "out": True}, "POST")
            out["bad"] = await s.call("/figures/hlt/out", {"brew": ids["a"]}, "POST")
            out["boil"] = await s.call("/figures/boil/out", {"brew": ids["c"], "out": True}, "POST")
            return out

    out = run(after_restart())
    hlt = out["figs"][1]["figures"]["hlt"]
    assert hlt["state"] == "in_use" and hlt["set"]["value"] == 0.85 and hlt["set"]["was"] is None
    assert (out["wrong"][0], out["wrong"][1]["error"]) == (409, "back_changed")
    status, body = out["back"]
    assert status == 200 and body["applied"] == {"figure": "hlt", "from": 0.85, "to": 0.9, "to_built_in": True}
    assert out["setting"] == ""
    assert (out["none"][0], out["none"][1]["error"]) == (409, "not_in_use")
    assert (out["no_brew"][0], out["no_brew"][1]["error"]) == (404, "no_brew")
    assert (out["no_figure"][0], out["no_figure"][1]["error"]) == (404, "no_figure")
    assert (out["bad"][0], out["bad"][1]["error"]) == (400, "bad_body")
    assert out["boil"][0] == 200 and out["boil"][1]["figures"]["boil"]["state"] == "info"


def test_a_setting_changed_by_hand_after_use(root):
    ids, a = four_brews(root)
    two = [ids["b"], ids["a"]]

    async def go():
        cbpi = stand_in(root)
        cbpi.config.register("BREWSTART_EFFICIENCY", "")
        async with Server(cbpi) as s:
            await s.call("/figures/hlt/out", {"brew": ids["c"], "out": True}, "POST")
            await s.call("/figures/hlt/use", {"value": 0.85, "brews": two}, "POST")
            await cbpi.config.set("BREWSTART_EFFICIENCY", 88)
            figs = await s.call("/figures")
            back = await s.call("/figures/hlt/back", {"value": 0.85}, "POST")
            use = await s.call("/figures/hlt/use", {"value": 0.85, "brews": two}, "POST")
            undo = await s.call("/figures/hlt/back", {"value": 0.85}, "POST")
            return figs, back, use, undo, cbpi.config.get("BREWSTART_EFFICIENCY")

    figs, back, use, undo, setting = run(go())
    hlt = figs[1]["figures"]["hlt"]
    assert hlt["state"] == "use" and hlt["set"]["value"] == 0.88 and hlt["set"]["since"] is None
    assert (back[0], back[1]["error"]) == (409, "back_changed")
    assert use[1]["applied"]["from"] == 0.88 and use[1]["figures"]["hlt"]["set"]["was"] == 0.88
    assert undo[1]["applied"] == {"figure": "hlt", "from": 0.85, "to": 0.88, "to_built_in": False}
    assert setting == 88


def test_one_brews_graph(root):
    ids, a = four_brews(root)

    async def go():
        async with Server(stand_in(root)) as s:
            figs = (await s.call("/figures"))[1]
            return figs, {f: await s.call("/figures/brews/%s?figure=%s" % (ids["a"], f))
                          for f in ("hlt", "mash", "boil", "strike")}, [
                await s.call("/figures/brews/log:now:5?figure=hlt"),
                await s.call("/figures/brews/%s?figure=nope" % ids["a"])]

    figs, graphs, refused = run(go())
    cells = {b["id"]: b["cells"] for b in figs["brews"]}[ids["a"]]
    status, g = graphs["hlt"]
    assert status == 200 and g["vessel"] == "hlt" and g["figure"] == "hlt"
    assert 300 < len(g["temp"]["t"]) <= 602
    assert g["stretches"] == [{"t0": a["heat"], "t1": cells["hlt"]["t1"]}]
    pairs = list(zip(g["target"]["t"], g["target"]["v"]))
    assert (a["heat"], 160.0) in pairs and all(v == 0.0 for t, v in pairs if t < a["heat"])
    assert [a["heat"], 1] in g["steps"] and g["readings"] is None and g["forecast"] is None
    status, g = graphs["mash"]
    rests = cells["mash"]["rests"]
    assert g["vessel"] == "mash" and g["t0"] == pytest.approx(rests[0]["t0"] - 900, abs=1)
    assert g["t1"] == pytest.approx(rests[-1]["t1"] + 720, abs=1)
    assert g["stretches"] == [{"t0": r["t0"], "t1": r["t1"]} for r in rests]
    assert graphs["boil"][1]["stretches"] == [{"t0": cells["boil"]["t0"], "t1": cells["boil"]["t1"]}]
    assert graphs["strike"][1]["stretches"] == [] and graphs["strike"][1]["forecast"] is None
    assert [(s, b["error"]) for s, b in refused] == [(404, "no_brew"), (404, "no_figure")]


def test_brewscribe_days_and_grain_absorption(root):
    logs = Logs(root / "logs")
    a = brew(logs, BASE, 0.84, 2.0, 0.3)
    b = brew(logs, BASE + DAY, 0.86, 2.0, 0.3)
    logs.write()

    def iso(t):
        return datetime.datetime.fromtimestamp(t).astimezone().isoformat()

    def vol(eid, stage, value, t, temp=None, **more):
        data = dict({"vessel": "x", "measurement_stage": stage, "value": value, "unit": "us_gal"}, **more)
        if temp is not None:
            data.update(temperature=temp, temperature_unit="f")
        return {"sequence": 1, "entry_id": eid, "kind": "volume", "occurred_at": iso(t), "received_at": iso(t),
                "data": data}

    days = {"d-a": [vol("a1", "strike_water", 15.0, a["heat"], 68.0),
                    vol("a2", "sparge_water", 5.4, a["up"], 68.0, level_before=9.9, level_after=4.5),
                    vol("a3", "pre_boil", 8.6, a["boil"], 68.0)],
            "d-b": [vol("b1", "strike_water", 15.0, b["heat"], 68.0), vol("b2", "sparge_water", 5.4, b["up"], 68.0),
                    vol("b3", "pre_boil", 8.6, b["boil"], 68.0)]}
    history = {"brews": [{"day_id": d, "name": "Rye Pale Ale", "started_at": iso(x["start"]),
                          "finished_at": iso(x["end"] + 1200), "simulator": False}
                         for d, x in (("d-a", a), ("d-b", b))]}

    async def get_history(request):
        return web.json_response(history)

    async def get_day(request):
        return web.json_response({"entries": days[request.match_info["day"]]})

    async def slow(request):
        await asyncio.sleep(3)
        return web.json_response(history)

    async def go():
        cbpi = stand_in(root, simulated=False)
        cbpi.book = [("r", {"name": "Rye Pale Ale", "mash_numbers": {"grain_kg": 13.5 / 2.2046226218,
                                                                      "lauter_dead_l": 0.5 * 3.785411784}}, 1.0)]
        async with Server(cbpi, {"/brewscribe/api/history": get_history, "/brewscribe/api/days/{day}": get_day}) as s:
            figs = (await s.call("/figures"))[1]
        cbpi = stand_in(root, simulated=False)
        cbpi.figures.BREWSCRIBE_TIMEOUT_S = 1.0
        async with Server(cbpi, {"/brewscribe/api/history": slow}) as s:
            late = (await s.call("/figures"))[1]
        return figs, late

    figs, late = run(go())
    assert figs["brewscribe"] == "ok" and figs["simulated"] is False
    brews = by_start(figs)
    assert [x["id"] for x in brews] == ["bs:d-a", "bs:d-b"]
    assert brews[0]["name"] == "Rye Pale Ale" and brews[0]["day_id"] == "d-a" and brews[0]["simulator"] is False
    assert brews[0]["cells"]["grain"] == {"per": 0.104, "held": 1.4, "strike": 5.1, "hlt_fill": 15.0,
                                          "sparge_before": 9.9, "strike_from": "hlt_fill", "sparge": 5.4,
                                          "pre_boil": 8.6, "dead": 0.5, "grain": 13.5}
    assert brews[1]["cells"]["grain"] == {"none": "no_levels"}
    assert brews[0]["cells"]["hlt"] == {"none": "no_water"}
    assert late["brewscribe"] == "no_answer" and all(x["id"].startswith("log:") for x in late["brews"])


def test_strike_use_writes_the_plant_file(root):
    logs = Logs(root / "logs")
    a = brew(logs, BASE, 0.84, 2.0, 0.3)
    b = brew(logs, BASE + DAY, 0.86, 2.0, 0.3)
    logs.write()
    cfg = root / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    landings = [{"t": x["flat"] - 600.0, "unit": "F", "lo": [149.0] * 61, "hi": [152.0] * 61} for x in (a, b)]
    (cfg / "strike_landings.jsonl").write_text("\n".join(json.dumps(x) for x in landings) + "\n")
    (cfg / "strike_fit.json").write_text(json.dumps({"value": 0.62, "landings": [
        {"t": landings[0]["t"], "fit_lo": 150.5, "fit_hi": 151.5, "share": 0.61},
        {"t": landings[1]["t"], "fit_lo": 150.5, "fit_hi": 151.5, "share": 0.63}]}))
    (cfg / "strike_plant.json").write_text(json.dumps({"hlt": {"watts": 5346.0}, "guesses": {
        "tun_share": {"central": 0.75, "low": 0.45, "high": 1.0, "note": "kept"}}}))

    async def go():
        async with Server(stand_in(root, grain_kg=6.0)) as s:
            figs = (await s.call("/figures"))[1]
            use = await s.call("/figures/strike/use", figs["figures"]["strike"]["use"], "POST")
            written = json.loads((cfg / "strike_plant.json").read_text())
            back = await s.call("/figures/strike/back", {"value": 0.62}, "POST")
            return figs, use, written, back, json.loads((cfg / "strike_plant.json").read_text())

    figs, use, written, back, after = run(go())
    cell = by_start(figs)[0]["cells"]["strike"]
    assert cell["lo"] == 149.0 and cell["hi"] == 152.0 and cell["fit_lo"] == 150.5 and cell["landed"] is not None
    assert figs["figures"]["strike"]["state"] == "use" and figs["figures"]["strike"]["measured"] == 0.62
    assert use[0] == 200 and use[1]["figures"]["strike"]["state"] == "in_use"
    measured = written["guesses"]["tun_share"]["measured"]
    assert measured["value"] == 0.62 and measured["method"] == "figures" and len(measured["brews"]) == 2
    assert written["guesses"]["tun_share"]["note"] == "kept" and written["hlt"] == {"watts": 5346.0}
    assert back[0] == 200 and "measured" not in after["guesses"]["tun_share"]
    assert after["guesses"]["tun_share"]["note"] == "kept"


def test_a_rigs_heat_up_needs_brewstarts_record(root):
    logs = Logs(root / "logs")
    brew(logs, BASE, 0.84, 2.0, 0.3)
    b = brew(logs, BASE + DAY, 0.86, 2.0, 0.3, brewstart={"planned_min": 30.0, "ready_by": BASE + DAY + 3000,
                                                          "litres": LITRES, "watts": WATTS, "efficiency": 0.9})
    logs.write()

    async def go():
        async with Server(stand_in(root, simulated=False)) as s:
            return (await s.call("/figures"))[1]

    brews = by_start(run(go()))
    assert brews[0]["cells"]["hlt"] == {"none": "no_water"}
    h = brews[1]["cells"]["hlt"]
    assert h["eff"] == pytest.approx(0.86, abs=0.004) and h["planned_by"] == "brewstart" and h["planned"] == 30.0
    assert h["early"] == pytest.approx((BASE + DAY + 3000 - b["ready"]) / 60, abs=0.2)
