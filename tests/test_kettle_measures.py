"""What a kettle measures, and an import's warnings when a recipe needs more than a vessel holds (4.7.7.post65).

Each kettle may say how much it holds (capacity), the least water that keeps its element covered (min_water), what it
weighs empty (empty_weight), what it's made of (material)
and, for "other", its specific heat (specific_heat): kept in kettle.json, served by GET /kettle, saved by PUT
/kettle/{id}, and carried by the kettle's settings file. Blank, missing or a value that can't be right, each counts
as unknown, and nothing is refused for it. An import warns for each of the HLT, the mash tun and the boil kettle
whose capacity is known and less than the recipe needs there.

Each test fails on a core before post65.
"""

import asyncio
import json
import types
from unittest import mock

import pytest

from cbpi.api import kettle_measures
from cbpi.api.dataclasses import Kettle, Props
from cbpi.controller import kettle_file
from cbpi.controller.kettle_controller import KettleController
from cbpi.controller.upload_controller import UploadController
from tests.cbpi_config_fixture import CraftBeerPiTestCase
from tests.test_recipe_pages import uploader

FIELDS = ("capacity", "min_water", "empty_weight", "material", "specific_heat")
#: Test Recipe v5's figures, as its BeerSmith export gives them: 23.0 gal before the boil, 20 lb of grain, 10.8 gal
#: of mash water.
V5 = {"boil_l": 87.064973, "grain_kg": 9.07184, "infusions_l": 40.731031, "infuse_l": 31.267501,
      "top_up_kettle_l": 0.0, "lauter_dead_l": 0.0, "lacks": []}
HLT_TEXT = ("This recipe needs about 25.4 gal of water (23.0 before the boil and 2.4 the grain keeps), and the HLT "
            "holds 20.0 gal. Check the batch size in BeerSmith, or plan to add water during the sparge.")
MT_TEXT = ("This recipe mashes about 12.4 gal (10.8 of water and 1.6 of grain), and the Mash Tun holds 10.0 gal. "
           "Check the batch size in BeerSmith.")
BK_TEXT = "This recipe boils 23.0 gal, and the Boil Kettle holds 20.0 gal. Check the batch size in BeerSmith."


# ---------------------------------------------------------------------------------------------- the fields

def test_a_kettle_from_an_older_kettle_json_loads_as_it_did_with_the_measurements_blank():
    older = {"id": "hlt", "name": "HLT", "type": "PIDBoil", "props": {"P": 2}, "sensor": "s1", "heater": "h1",
             "agitator": "", "target_temp": 168, "props_by_type": {}}
    item = KettleController.__new__(KettleController).create(dict(older))
    seen = item.to_dict()
    assert {k: seen[k] for k in FIELDS} == dict.fromkeys(FIELDS)
    assert {k: seen[k] for k in older} == older


@pytest.mark.parametrize("field, given, kept, used", [
    ("capacity", 20, 20, 20.0),
    ("capacity", "20,5", 20.5, 20.5),
    ("capacity", "  ", None, None),
    ("capacity", 0, 0, None),
    ("capacity", -5, -5, None),
    ("capacity", "twenty", "twenty", None),
    ("capacity", True, None, None),
    ("capacity", float("nan"), None, None),
    ("capacity", [20], None, None),
    ("empty_weight", "31.5", 31.5, 31.5),
    ("specific_heat", 0.12, 0.12, 0.12),
])
def test_a_measurement_is_kept_as_given_and_used_only_when_it_can_be_right(field, given, kept, used):
    assert kettle_measures.stored(field, given) == kept
    assert kettle_measures.usable(kettle_measures.stored(field, given)) == used


def test_a_material_is_one_of_three_or_unknown():
    assert [kettle_measures.material(m) for m in ("Stainless Steel ", "aluminium", "other", "copper", 1, None)] == \
        ["stainless_steel", "aluminium", "other", None, None, None]
    assert [kettle_measures.stored("material", m) for m in (" Aluminium ", "stainless-steel", "copper ")] == \
        ["aluminium", "stainless_steel", "copper"]


def test_a_save_without_the_measurements_keeps_the_kettles_own_and_null_clears_one():
    kept = Kettle("hlt", "HLT", capacity=20, min_water=3.5, empty_weight=31.5, material="stainless_steel")
    assert KettleController.measures({"name": "HLT"}, kept=kept) == {
        "capacity": 20, "min_water": 3.5, "empty_weight": 31.5, "material": "stainless_steel", "specific_heat": None}
    assert KettleController.measures({"capacity": None, "material": "other", "specific_heat": "0.2",
                                      "min_water": "4,5"}, kept=kept) == {
        "capacity": None, "min_water": 4.5, "empty_weight": 31.5, "material": "other", "specific_heat": 0.2}
    assert KettleController.measures({"name": "New"}) == dict.fromkeys(FIELDS)
    assert KettleController.measures(None) == dict.fromkeys(FIELDS)


class KettleMeasuresHttpTestCase(CraftBeerPiTestCase):

    async def test_put_saves_them_get_shows_them_and_a_save_without_them_keeps_them(self):
        resp = await self.client.post(path="/kettle/", json={"name": "Measured", "type": "CustomKettleLogic"})
        assert resp.status == 200
        made = await resp.json()
        assert {k: made[k] for k in FIELDS} == dict.fromkeys(FIELDS)
        kettle_id = made["id"]
        try:
            resp = await self.client.put(path="/kettle/%s" % kettle_id, json=dict(
                made, capacity=20, empty_weight="31.5", material="stainless_steel"))
            assert resp.status == 200
            resp = await self.client.put(path="/kettle/%s" % kettle_id, json={"name": "Measured", "props": {}})
            assert resp.status == 200
            listed = await (await self.client.get("/kettle/")).json()
            seen = next(k for k in listed["data"] if k["id"] == kettle_id)
            assert {k: seen[k] for k in FIELDS} == {"capacity": 20, "min_water": None, "empty_weight": 31.5,
                                                    "material": "stainless_steel", "specific_heat": None}
            resp = await self.client.put(path="/kettle/%s" % kettle_id, json={"name": "Measured", "capacity": -1,
                                                                              "material": None})
            assert resp.status == 200
            seen = await resp.json()
            assert (seen["capacity"], seen["material"], seen["empty_weight"]) == (-1, None, 31.5)
        finally:
            await self.client.delete(path="/kettle/%s" % kettle_id)

    async def test_put_vessel_saves_only_the_measurements(self):
        resp = await self.client.post(path="/kettle/", json={"name": "Vessel", "type": "CustomKettleLogic",
                                                             "props": {"P": 2}})
        made = await resp.json()
        kettle_id = made["id"]
        try:
            resp = await self.client.put(path="/kettle/%s/vessel" % kettle_id, json={"capacity": "20", "name": "x"})
            assert resp.status == 200
            seen = await resp.json()
            assert (seen["capacity"], seen["name"], seen["props"]) == (20.0, "Vessel", {"P": 2})
            resp = await self.client.put(path="/kettle/%s/vessel" % kettle_id, json={"material": "aluminium"})
            seen = await resp.json()
            assert (seen["capacity"], seen["material"]) == (20.0, "aluminium")
            assert (await self.client.put(path="/kettle/%s/vessel" % kettle_id, json=[20])).status == 400
            assert (await self.client.put(path="/kettle/no-such/vessel", json={"capacity": 20})).status == 404
        finally:
            await self.client.delete(path="/kettle/%s" % kettle_id)


def test_saving_only_the_measurements_leaves_auto_heating():
    async def run():
        driven = []

        async def heating():
            while True:
                driven.append(1)
                await asyncio.sleep(0.01)

        controller = KettleController.__new__(KettleController)
        item = Kettle("hlt", "HLT", capacity=15)
        item.instance = types.SimpleNamespace(state=True, running=True, task=asyncio.create_task(heating()))
        controller.data = [item]
        controller.save, controller.stop, controller.start = mock.AsyncMock(), mock.AsyncMock(), mock.AsyncMock()
        await asyncio.sleep(0.03)
        saved = await controller.save_measures("hlt", {"capacity": 20, "material": "Stainless Steel"})
        before = len(driven)
        await asyncio.sleep(0.03)
        try:
            assert saved is item and (item.capacity, item.material) == (20, "stainless_steel")
            assert item.instance.state is True and not item.instance.task.done() and len(driven) > before
            controller.stop.assert_not_awaited()
            controller.start.assert_not_awaited()
            controller.save.assert_awaited_once()
            assert await controller.save_measures("nope", {"capacity": 1}) is None
        finally:
            item.instance.task.cancel()

    asyncio.run(run())


# ------------------------------------------------------------------------------------------ the kettle file

def a_kettle(**measured):
    return Kettle("hlt", "HLT", type=None, props=Props({}), **measured)


def test_a_kettle_without_measurements_makes_and_loads_the_file_it_always_did():
    data = kettle_file.export(a_kettle(), {}, {}, "4.7.7.post65")
    assert "measurements" not in data
    plan = kettle_file.plan(a_kettle(), kettle_file.parse(json.dumps(data)), {}, {})
    assert "measurements" not in plan and "measurements" not in plan["after"] and plan["changes"] == 0


def test_a_kettles_measurements_go_in_its_file_and_load_from_it():
    data = kettle_file.export(a_kettle(capacity=20, material="aluminium"), {}, {}, "4.7.7.post65")
    assert data["measurements"] == {"capacity": 20, "material": "aluminium"}
    item = a_kettle(capacity=15, empty_weight=31.5)
    plan = kettle_file.plan(item, kettle_file.parse(json.dumps(data)), {}, {})
    assert plan["after"]["measurements"] == {"capacity": 20, "material": "aluminium"}
    assert plan["changes"] == 2 and [m["field"] for m in plan["measurements"]] == ["capacity", "material"]

    controller = KettleController.__new__(KettleController)
    controller.data, controller.types, controller.cbpi = [item], {}, types.SimpleNamespace()
    controller.update = mock.AsyncMock(side_effect=lambda kettle: kettle)
    loaded, _ = asyncio.run(controller.load_file("hlt", json.dumps(data), plan["token"]))
    assert (loaded.capacity, loaded.empty_weight, loaded.material) == (20, 31.5, "aluminium")


def test_odd_measurements_in_a_file_never_refuse_it():
    data = kettle_file.export(a_kettle(), {}, {}, "4.7.7.post65")
    for odd in ("twenty", [1], {"capacity": {"gal": 20}, "material": 3, "empty_weight": "heavy"}):
        parsed = kettle_file.parse(json.dumps(dict(data, measurements=odd)))
        kettle_file.plan(a_kettle(), parsed, {}, {})
    assert parsed["measurements"] == {"capacity": None, "material": None, "empty_weight": "heavy"}


# ------------------------------------------------------------------------------- the import's warnings

def brewery(unit="F", absorption=0.12, mt=None, hlt=None, bk=None):
    """An UploadController on a HERMS: the Mash Tun's PID_HERMS heats through the HLT."""
    kettles = [Kettle("mt", "Mash Tun", props=Props({"HLT_Kettle": "hlt"}), type="PID_HERMS", capacity=mt),
               Kettle("hlt", "HLT", capacity=hlt), Kettle("bk", "Boil Kettle", capacity=bk)]
    values = {"TEMP_UNIT": unit, "GRAIN_ABSORPTION": absorption, "MASH_TUN": "mt", "BoilKettle": "bk"}
    upload = UploadController.__new__(UploadController)
    upload.cbpi = types.SimpleNamespace(
        config=types.SimpleNamespace(get=lambda k, d=None: values.get(k, d)),
        kettle=types.SimpleNamespace(data=kettles, types={"PID_HERMS": {"properties": [{"label": "HLT_Sensor"}]}}))
    return upload


def test_a_recipe_needing_more_than_each_vessel_holds_is_warned_of_for_each():
    assert brewery(mt=10, hlt=20, bk=20).too_full(V5) == [
        ("More than the HLT holds", HLT_TEXT), ("More than the Mash Tun holds", MT_TEXT),
        ("More than the Boil Kettle holds", BK_TEXT)]
    assert brewery(unit="C", absorption=1.0, mt=40, hlt=75, bk=80).too_full(V5) == [
        ("More than the HLT holds", "This recipe needs about 96.1 L of water (87.1 before the boil and 9.1 the grain "
                                    "keeps), and the HLT holds 75.0 L. Check the batch size in BeerSmith, or plan to "
                                    "add water during the sparge."),
        ("More than the Mash Tun holds", "This recipe mashes about 46.8 L (40.7 of water and 6.1 of grain), and the "
                                         "Mash Tun holds 40.0 L. Check the batch size in BeerSmith."),
        ("More than the Boil Kettle holds", "This recipe boils 87.1 L, and the Boil Kettle holds 80.0 L. Check the "
                                            "batch size in BeerSmith.")]
    dead = brewery(hlt=20).too_full(dict(V5, lauter_dead_l=1.892706))
    assert dead[0][1].startswith("This recipe needs about 25.9 gal of water (23.0 before the boil, 2.4 the grain "
                                 "keeps and 0.5 left under the mash tun's false bottom)")
    strike_only = brewery(mt=9).too_full({k: v for k, v in V5.items() if k != "infusions_l"})
    assert strike_only == [("More than the Mash Tun holds", "This recipe mashes about 9.9 gal (8.3 of water and 1.6 "
                                                            "of grain), and the Mash Tun holds 9.0 gal. Check the "
                                                            "batch size in BeerSmith.")]


@pytest.mark.parametrize("kettles, numbers", [
    (dict(mt=12.4, hlt=25.4, bk=23.0), V5),
    (dict(), V5),
    (dict(mt="", hlt=None, bk="  "), V5),
    (dict(mt=0, hlt=-20, bk="twenty"), V5),
    (dict(mt=1, hlt=1, bk=1), {k: v for k, v in V5.items() if k not in ("boil_l", "infusions_l", "infuse_l")}),
    (dict(mt=1, hlt=1, bk=1), {k: v for k, v in V5.items() if k != "grain_kg" and k != "boil_l"}),
    (dict(mt=1, hlt=1, bk=1), None),
], ids=["it fits", "none known", "blank", "can't be right", "no boil or water", "no grain or boil", "no figures"])
def test_no_warning_when_it_fits_or_there_is_nothing_to_check(kettles, numbers):
    assert brewery(**kettles).too_full(numbers) == []


def test_no_warning_for_a_vessel_that_cant_be_told():
    upload = brewery(mt=1, hlt=1, bk=1)
    upload.cbpi.kettle.data = [Kettle("k1", "Kettle", capacity=1)]
    upload.cbpi.config = types.SimpleNamespace(get=lambda k, d=None: {"TEMP_UNIT": "F"}.get(k, d))
    assert upload.too_full(V5) == []
    upload.cbpi.kettle = None
    assert upload.too_full(V5) == []


def test_the_import_answers_the_warnings_and_keeps_the_recipe():
    upload, cbpi = uploader(held=())
    known = brewery(mt=10, hlt=20, bk=20)
    cbpi.config.get.side_effect = known.cbpi.config.get
    cbpi.kettle = known.cbpi.kettle
    read = upload.xml_recipe_creation

    async def v5(recipe_id):
        await read(recipe_id)
        upload._staged["mash_numbers"] = dict(V5)

    upload.xml_recipe_creation = v5
    ok, answer = asyncio.run(upload.import_recipe("xml", "1"))
    assert ok is True and answer["file"] == "book-1"
    assert answer["warnings"] == [{"title": "More than the HLT holds", "text": HLT_TEXT},
                                  {"title": "More than the Mash Tun holds", "text": MT_TEXT},
                                  {"title": "More than the Boil Kettle holds", "text": BK_TEXT}]
    assert [c.args[:2] for c in cbpi.notify.call_args_list][-1] == ("More than the Boil Kettle holds", BK_TEXT)
