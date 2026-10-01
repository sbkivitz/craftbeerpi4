import json
import logging
import os
from pathlib import Path

from cbpi.api.config import ConfigType
from cbpi.api.dataclasses import Config
from cbpi.api.persist import atomic_write_json
from cbpi.utils import load_config


class ConfigController:

    def __init__(self, cbpi):
        self.cache = {}
        self.logger = logging.getLogger(__name__)
        self.cbpi = cbpi
        self.cbpi.register(self)
        self.path = cbpi.config_folder.get_file_path("config.json")
        self.path_static = cbpi.config_folder.get_file_path("config.yaml")
        self.logger.info(
            "Config folder path : "
            + os.path.join(Path(self.cbpi.config_folder.configFolderPath).absolute())
        )

    def get_state(self):
        result = {}
        for key, value in self.cache.items():
            result[key] = value.to_dict()
        return result

    async def init(self):
        self.static = load_config(self.path_static)
        with open(self.path) as json_file:
            data = json.load(json_file)
            for key, value in data.items():
                self.cache[key] = Config(
                    name=value.get("name"),
                    value=value.get("value"),
                    description=value.get("description"),
                    type=ConfigType(value.get("type", "string")),
                    source=value.get("source", "craftbeerpi"),
                    options=value.get("options", None),
                )

    def get(self, name, default=None):
        self.logger.debug("GET CONFIG VALUE %s (default %s)" % (name, default))
        if (
            name in self.cache
            and self.cache[name].value is not None
            and self.cache[name].value != ""
        ):
            return self.cache[name].value
        else:
            return default

    async def set(self, name, value):
        if name in self.cache:

            self.cache[name].value = value

            data = {}
            for key, value in self.cache.items():
                data[key] = value.to_dict()
            atomic_write_json(self.path, data, sort_keys=True)
            await self.push_update()

    async def push_update(self):
        """Tell every connected client the configuration changed.

        Nothing did this. set() updated the cache and wrote the file and told
        no one, and there was no config topic anywhere in the server, so a
        browser's idea of the configuration was whatever it fetched when the
        page loaded and never changed again.

        The interface appeared to work only because its own save path calls
        navigate(0) - a full page reload - which re-fetches everything. A
        change from anywhere else was invisible: the API, a plugin, a second
        browser, a phone in the brewery while the laptop sits in the kitchen.

        That is worse than a stale display. Observed on the rig: the server
        was running at SIM_TIME_SCALE 60 while the settings menu showed 1, so
        the brewer read a number, believed it, and was wrong. The same applies
        to TEMP_UNIT, CONFIRM_BEFORE_BOIL and NOTIFY_ON_ERROR - settings that
        change what the rig does and what it warns about.

        Sent as the whole config rather than one key, matching how kettles,
        actors and sensors already broadcast, so a client that missed an
        earlier message cannot drift.
        """
        try:
            self.cbpi.ws.send(
                dict(topic="configupdate", data=self.get_state())
            )
        except Exception as e:  # noqa: BLE001 - never fail a write on telemetry
            self.logger.warning("Could not push config update: %s", e)

    async def add(
        self,
        name,
        value,
        type: ConfigType,
        description,
        source="craftbeerpi",
        options=None,
    ):
        self.cache[name] = Config(name, value, description, type, source, options)
        data = {}
        for key, value in self.cache.items():
            data[key] = value.to_dict()
        atomic_write_json(self.path, data, sort_keys=True)
        await self.push_update()

    async def remove(self, name):
        data = {}
        self.testcache = {}
        success = False
        for key, value in self.cache.items():
            try:
                if key != name:
                    data[key] = value.to_dict()
                    self.testcache[key] = Config(
                        name=data[key].get("name"),
                        value=data[key].get("value"),
                        description=data[key].get("description"),
                        type=ConfigType(data[key].get("type", "string")),
                        options=data[key].get("options", None),
                        source=data[key].get("source", "craftbeerpi"),
                    )
                    success = True
            except Exception as e:
                print(e)
                success = False
        if success == True:
            atomic_write_json(self.path, data, sort_keys=True)
            self.cache = self.testcache
            await self.push_update()

    async def obsolete(self, remove=False):
        result = {}
        for key, value in self.cache.items():
            if value.source not in ("craftbeerpi", "steps", "hidden"):
                test = await self.cbpi.plugin.load_plugin_list(value.source)
                if test == []:
                    update = self.get(str(value.source) + "_update")
                    if update:
                        result[str(value.source) + "_update"] = {"value": update}
                        if remove:
                            await self.remove(str(value.source) + "_update")
                    if remove:
                        await self.remove(key)
                    result[key] = value.to_dict()
        return result
