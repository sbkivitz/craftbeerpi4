import glob
import json
import logging
import os
from os import listdir
from os.path import isfile, join

from cbpi.api.base import CBPiBase
from cbpi.api.config import ConfigType
from cbpi.api.dataclasses import NotificationType
from voluptuous.schema_builder import message


class DashboardController:

    def __init__(self, cbpi):
        self.caching = False
        self.cbpi = cbpi
        self.logger = logging.getLogger(__name__)
        self.cbpi.register(self)

        self.path = cbpi.config_folder.get_dashboard_path("cbpi_dashboard_1.json")
        # Bundled starter layouts ship inside the package (cbpi/config/dashboard/templates)
        # so they are available regardless of how the user config folder was provisioned.
        self.template_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "config",
            "dashboard",
            "templates",
        )

    async def init(self):
        pass

    async def get_content(self, dashboard_id):
        try:
            self.path = self.cbpi.config_folder.get_dashboard_path(
                "cbpi_dashboard_" + str(dashboard_id) + ".json"
            )
            logging.info(self.path)
            with open(self.path, encoding="utf-8") as json_file:
                data = json.load(json_file)
                return data
        except:
            return {"elements": [], "pathes": []}

    async def add_content(self, dashboard_id, data):
        # print(data)
        self.path = self.cbpi.config_folder.get_dashboard_path(
            "cbpi_dashboard_" + str(dashboard_id) + ".json"
        )
        with open(self.path, "w", encoding="utf-8") as outfile:
            json.dump(data, outfile, indent=4, sort_keys=True, ensure_ascii=False)
        self.cbpi.notify(
            title="Dashboard {}".format(dashboard_id),
            message="Saved Successfully",
            type=NotificationType.SUCCESS,
        )
        return {"status": "OK"}

    async def delete_content(self, dashboard_id):
        self.path = self.cbpi.config_folder.get_dashboard_path(
            "cbpi_dashboard_" + str(dashboard_id) + ".json"
        )
        if os.path.exists(self.path):
            os.remove(self.path)
            self.cbpi.notify(
                title="Dashboard {}".format(dashboard_id),
                message="Deleted Successfully",
                type=NotificationType.SUCCESS,
            )

    def _resolve_token(self, token):
        """Resolve a template binding token like '$kettle:HLT|Hot Liquor' to a
        configured item id. Each '|'-separated hint is matched case-insensitively
        as a substring of the item name, and the first matching item in
        configuration order wins. Returns the item id, or an empty string when
        nothing matches (so the user binds it manually). Strings that are not
        binding tokens are returned unchanged."""
        if not isinstance(token, str) or not token.startswith("$"):
            return token
        try:
            kind, hint = token[1:].split(":", 1)
        except ValueError:
            # '$something' with no ':' is not a binding token - leave it alone.
            return token
        collection = {
            "kettle": getattr(self.cbpi, "kettle", None),
            "sensor": getattr(self.cbpi, "sensor", None),
            "actor": getattr(self.cbpi, "actor", None),
        }.get(kind.strip().lower())
        if collection is None:
            return ""
        hints = [h.strip().lower() for h in hint.split("|") if h.strip()]
        try:
            items = collection.data
        except Exception:
            items = []
        for item in items:
            name = (getattr(item, "name", "") or "").lower()
            if any(h in name for h in hints):
                return getattr(item, "id", "")
        return ""

    def _resolve_bindings(self, data):
        """Walk every element's props and replace binding tokens with real ids."""
        for element in data.get("elements", []):
            props = element.get("props", {})
            for key, value in list(props.items()):
                if isinstance(value, str) and value.startswith("$"):
                    props[key] = self._resolve_token(value)
        return data

    async def get_template_list(self):
        """Return the list of bundled starter layouts with their metadata."""
        templates = []
        try:
            files = sorted(glob.glob(os.path.join(self.template_path, "*.json")))
        except Exception as e:
            self.logger.error("Could not list dashboard templates: {}".format(e))
            files = []
        for f in files:
            name = os.path.splitext(os.path.basename(f))[0]
            title, description = name, ""
            try:
                with open(f, encoding="utf-8") as json_file:
                    meta = json.load(json_file).get("meta", {})
                    title = meta.get("title", name)
                    description = meta.get("description", "")
            except Exception as e:
                self.logger.warning(
                    "Could not read template metadata for {}: {}".format(name, e)
                )
            templates.append({"name": name, "title": title, "description": description})
        return templates

    def _read_template(self, name):
        """Load and resolve a template by name. Returns None when the template does
        not exist or cannot be parsed, so callers can tell 'missing' apart from
        'genuinely empty' - important because applying a template overwrites a
        dashboard."""
        safe_name = os.path.basename(str(name))
        template_file = os.path.join(self.template_path, safe_name + ".json")
        if not os.path.isfile(template_file):
            return None
        try:
            with open(template_file, encoding="utf-8") as json_file:
                data = json.load(json_file)
        except Exception as e:
            self.logger.error("Could not read dashboard template {}: {}".format(safe_name, e))
            return None
        if not isinstance(data, dict):
            self.logger.error("Dashboard template {} is not a JSON object".format(safe_name))
            return None
        data.pop("meta", None)
        data.setdefault("elements", [])
        data.setdefault("pathes", [])
        return self._resolve_bindings(data)

    async def get_template(self, name):
        """Return a template's dashboard content ({elements, pathes}) with binding
        tokens resolved against the currently configured kettles/sensors/actors.
        Returns an empty layout if the template does not exist."""
        data = self._read_template(name)
        if data is None:
            return {"elements": [], "pathes": []}
        return data

    async def apply_template(self, dashboard_id, name):
        """Overwrite cbpi_dashboard_{id}.json with a resolved starter layout, so the
        existing UI renders it without any frontend change. This replaces the entire
        current content of that dashboard number and cannot be undone.

        Raises FileNotFoundError for an unknown template rather than overwriting the
        dashboard with an empty layout."""
        data = self._read_template(name)
        if data is None:
            raise FileNotFoundError("Unknown dashboard template '{}'".format(name))
        await self.add_content(dashboard_id, data)
        return {"status": "OK", "elements": len(data.get("elements", []))}

    async def get_custom_widgets(self):
        path = self.cbpi.config_folder.get_dashboard_path("widgets")
        onlyfiles = [
            os.path.splitext(f)[0]
            for f in sorted(listdir(path))
            if isfile(join(path, f)) and f.endswith(".svg")
        ]
        return onlyfiles

    async def get_dashboard_numbers(self):
        max_dashboard_number = self.cbpi.config.get("max_dashboard_number", 4)
        return max_dashboard_number

    async def get_current_dashboard(self):
        current_dashboard_number = self.cbpi.config.get("current_dashboard_number", 1)
        return current_dashboard_number

    async def set_current_dashboard(self, dashboard_id=1):
        await self.cbpi.config.set("current_dashboard_number", dashboard_id)
        return {"status": "OK"}

    async def get_current_grid(self):
        current_grid = self.cbpi.config.get("current_grid", 5)
        return current_grid

    async def set_current_grid(self, grid_width=5):
        await self.cbpi.config.set("current_grid", grid_width)
        return {"status": "OK"}

    async def get_slow_pipe_animation(self):
        slow_pipe_animation = self.cbpi.config.get("slow_pipe_animation", "Yes")
        return slow_pipe_animation
