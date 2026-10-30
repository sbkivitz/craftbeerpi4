import logging
from unittest import mock
from aiohttp.test_utils import unittest_run_loop
from tests.cbpi_config_fixture import CraftBeerPiTestCase

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')


class ActorTestCase(CraftBeerPiTestCase):

    async def test_actor_switch(self):

        resp = await self.client.post(path="/login", data={"username": "cbpi", "password": "123"})
        assert resp.status == 200, "login should be successful"

        resp = await self.client.request("POST", "/actor/3CUJte4bkxDMFCtLX8eqsX/on")
        assert resp.status == 200, "switching actor on should work"
        i = self.cbpi.actor.find_by_id("3CUJte4bkxDMFCtLX8eqsX")

        assert i.instance.state is True

        resp = await self.client.request("POST", "/actor/3CUJte4bkxDMFCtLX8eqsX/off")
        assert resp.status == 200
        i = self.cbpi.actor.find_by_id("3CUJte4bkxDMFCtLX8eqsX")
        assert i.instance.state is False

    async def test_crud(self):
        data = {
            "name": "SomeActor",
            "power": 100,
            "props": {
            },
            "state": False,
            "type": "DummyActor"
        }

        # Add new sensor
        resp = await self.client.post(path="/actor/", json=data)
        assert resp.status == 200

        m = await resp.json()
        sensor_id = m["id"]

        # Get sensor
        resp = await self.client.get(path="/actor/%s" % sensor_id)
        assert resp.status == 200

        m2 = await resp.json()
        sensor_id = m2["id"]

        resp = await self.client.request("POST", "/actor/%s/on" % sensor_id)
        assert resp.status == 200



        # Update Sensor
        resp = await self.client.put(path="/actor/%s" % sensor_id, json=m)
        assert resp.status == 200

        # # Delete Sensor
        resp = await self.client.delete(path="/actor/%s" % sensor_id)
        assert resp.status == 200

    async def test_crud_negative(self):
        data = {
            "name": "CustomActor",
            "type": "CustomActor",
            "config": {
                "interval": 5
            }
        }

        # Get actor which not exists
        resp = await self.client.get(path="/actor/%s" % 9999)
        assert resp.status == 500

        # Update not existing actor
        resp = await self.client.put(path="/actor/%s" % 9999, json=data)
        assert resp.status == 500

    async def test_actor_action(self):
        # A declared action on a real actor runs, and says so. DummyActor's
        # helloWorld ("SAY HELLO") sends a notification - the proof it ran.
        actor = self.cbpi.actor.find_by_id("3CUJte4bkxDMFCtLX8eqsX")
        with mock.patch.object(self.cbpi, "notify") as notify:
            resp = await self.client.post(path="/actor/3CUJte4bkxDMFCtLX8eqsX/action",
                                          json=dict(action="helloWorld", parameter={}))
        assert resp.status == 200, "a declared action should run"
        assert [c.args[:2] for c in notify.call_args_list if c.args[:1] == ("HELLO",)] \
            == [("HELLO", "WOOHO")], "and it should actually have run"

        # Upstream answered 200 to these too, having logged that nothing ran -
        # so the dashboard showed a refused action as applied. This fork
        # answers 400. The test asserted 200 for a missing actor and an
        # undeclared action until 30 Oct: it was asserting the bug.
        resp = await self.client.post(path="/actor/1/action",
                                      json=dict(name="myAction", parameter=dict(name="Manuel")))
        assert resp.status == 400, "an actor that does not exist cannot run an action"

        resp = await self.client.post(path="/actor/3CUJte4bkxDMFCtLX8eqsX/action",
                                      json=dict(action="on", parameter={}))
        assert resp.status == 400, "a method that is not a declared action must be refused"
        assert actor.instance.state is False, "and must not have run"
