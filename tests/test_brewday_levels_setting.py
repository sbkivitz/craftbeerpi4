"""BREWDAY_LEVELS: whether the brew-day screen shows the liquid in each vessel (design/brewday LEVELS.md).

- A configuration without it gets "Yes", a select of Yes and No.
- A saved "No" is kept when a new version brings the description up to date; anything but Yes or No becomes Yes.

Each test fails on a core before the setting (4.7.7.post92).
"""

import asyncio
import types

from cbpi.extension.ConfigUpdate import ConfigUpdate

OPTIONS = [{"label": "Yes", "value": "Yes"}, {"label": "No", "value": "No"}]


class Config:
    """The settings, recording what ConfigUpdate adds."""

    def __init__(self, values=None):
        self.values, self.added = dict(values or {}), {}
        self.cache = {name: types.SimpleNamespace(value=value) for name, value in self.values.items()}

    def get(self, name, default=None):
        value = self.values.get(name)
        return default if value is None or value == "" else value

    async def add(self, name, value, type=None, description="", source="craftbeerpi", options=None):
        self.values[name] = value
        self.cache[name] = types.SimpleNamespace(value=value)
        self.added[name] = dict(value=value, type=getattr(type, "value", type), description=description,
                                source=source, options=options)


async def _list(*a, **k):
    return []


def configured(tmp_path, values):
    cbpi = types.SimpleNamespace(
        config=Config(values), static_config={"mqtt_offset": False},
        plugin=types.SimpleNamespace(load_plugin_list=_list),
        config_folder=types.SimpleNamespace(get_dashboard_path=lambda name: str(tmp_path / name),
                                            get_file_path=lambda name: str(tmp_path / name)))
    update = ConfigUpdate.__new__(ConfigUpdate)
    update.cbpi = cbpi
    asyncio.run(update.run())
    return cbpi.config


def test_a_configuration_without_it_gets_yes(tmp_path):
    added = configured(tmp_path, {"TEMP_UNIT": "F"}).added.get("BREWDAY_LEVELS")
    assert added is not None
    assert added["value"] == "Yes" and added["type"] == "select" and added["options"] == OPTIONS
    assert added["source"] == "craftbeerpi" and "liquid in each vessel" in added["description"]


def test_a_saved_no_is_kept_on_a_new_version(tmp_path):
    added = configured(tmp_path, {"TEMP_UNIT": "F", "BREWDAY_LEVELS": "No",
                                  "CONFIG_STATUS": "4.7.7.post1"}).added.get("BREWDAY_LEVELS")
    assert added is not None and added["value"] == "No" and added["options"] == OPTIONS


def test_anything_but_yes_or_no_becomes_yes(tmp_path):
    added = configured(tmp_path, {"TEMP_UNIT": "F", "BREWDAY_LEVELS": "tint",
                                  "CONFIG_STATUS": "4.7.7.post1"}).added.get("BREWDAY_LEVELS")
    assert added is not None and added["value"] == "Yes"
