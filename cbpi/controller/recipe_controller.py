import copy
import json
import logging
import os.path
import re
from os import listdir
from os.path import isfile, join

import shortuuid
import yaml
from cbpi.api.dataclasses import NotificationType
from cbpi.api.persist import atomic_write_text

from ..api.step import StepMove, StepResult, StepState


class RecipeController:

    #: In the basic data of a recipe an import put in the book: what it was
    #: imported from ("BeerXML"). Saving the brew steps to the book
    #: (StepController.book_data), the recipe editor's Save and Clone (save)
    #: all drop it, so a recipe the brewer saved, edited or cloned is never
    #: taken for an import - see keep_import.
    IMPORTED_FROM = "imported_from"

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self.logger = logging.getLogger(__name__)

    def urlify(self, s):

        # Remove all non-word characters (everything except numbers and letters)
        s = re.sub(r"[^\w\s]", "", s)

        # Replace all runs of whitespace with a single dash
        s = re.sub(r"\s+", "-", s)

        return s

    async def create(self, name):
        id = shortuuid.uuid()
        path = self.cbpi.config_folder.get_recipe_file_by_id(id)
        data = dict(
            basic=dict(name=name, author=self.cbpi.config.get("AUTHOR", "John Doe")),
            steps=[],
        )
        with open(path, "w") as file:
            yaml.dump(data, file)
        return id

    async def save(self, name, data):
        """Write a recipe the brewer made or changed: the recipe editor's Save,
        and Clone. Either makes the recipe the brewer's own, so the import mark
        goes: a later import of the same name then adds a copy rather than
        overwrite the brewer's edits. See keep_import."""
        basic = data.get("basic") if isinstance(data, dict) else None
        if isinstance(basic, dict):
            basic.pop(self.IMPORTED_FROM, None)
        path = self.cbpi.config_folder.get_recipe_file_by_id(name)
        logging.info(data)
        with open(path, "w") as file:
            yaml.dump(data, file, indent=4, sort_keys=True)

    def _book_entries(self):
        """(file id, basic data, last modified) for each readable recipe in the book."""
        path = self.cbpi.config_folder.get_file_path("recipes")
        if not os.path.isdir(path):
            return []
        found = []
        for f in listdir(path):
            full = join(path, f)
            if not (isfile(full) and f.endswith(".yaml")):
                continue
            try:
                with open(full) as file:
                    basic = (yaml.load(file, Loader=yaml.FullLoader) or {}).get("basic") or {}
                found.append((os.path.splitext(f)[0], basic, os.path.getmtime(full)))
            except Exception as e:  # noqa: BLE001 - get_recipes reports these to the brewer
                logging.warning("Recipe book: skipped %s: %s", f, e)
        return found

    async def keep_import(self, data, source):
        """Keep an imported recipe in the book. Returns (file id, replaced an earlier copy?).

        Scott, 5 Nov: "As soon as I loaded the v4 recipe, I cannot see the v3
        recipe any longer in my recipe book." An import only replaced the brew
        steps, and the Upload page keeps one file, so a recipe was gone once
        another was uploaded: the book held only what the brewer saved by hand.

        Now every import is kept, marked with where it came from. Importing a
        recipe of the same name again - changed in BeerSmith and exported again
        - replaces the copy the earlier import made, and never a recipe the
        brewer saved, edited or cloned, which carries no mark. Written in one
        step, so a power cut leaves the old copy or the new one, not half a
        file.
        """
        entry = copy.deepcopy(data)
        basic = entry.setdefault("basic", {})
        basic[self.IMPORTED_FROM] = source
        name = basic.get("name")
        earlier = [(modified, file_id) for file_id, b, modified in self._book_entries()
                   if b.get(self.IMPORTED_FROM) and b.get("name") == name]
        file_id = max(earlier)[1] if earlier else shortuuid.uuid()
        atomic_write_text(self.cbpi.config_folder.get_recipe_file_by_id(file_id),
                          yaml.dump(entry, indent=4, sort_keys=True))
        return file_id, bool(earlier)

    async def get_recipes(self):
        path = self.cbpi.config_folder.get_file_path("recipes")
        onlyfiles = [
            os.path.splitext(f)[0]
            for f in listdir(path)
            if isfile(join(path, f)) and f.endswith(".yaml")
        ]

        result = []
        for filename in onlyfiles:
            recipe_path = self.cbpi.config_folder.get_recipe_file_by_id(filename)
            with open(recipe_path) as file:
                try:
                    data = yaml.load(file, Loader=yaml.FullLoader)
                    dataset = data["basic"]
                    dataset["file"] = filename
                    # When it last changed, so a list can put the newest first:
                    # the brew-day screen's Recipe Book (Scott's layout B, 9 Nov).
                    dataset["modified"] = os.path.getmtime(recipe_path)
                    result.append(dataset)
                except Exception as e:
                    self.cbpi.notify(
                        "Error",
                        f"Invalid recipe file skipped: {filename}",
                        NotificationType.ERROR,
                    )
                    logging.error(f"Skip invalid file: {e}")
        return result

    async def get_by_name(self, name):

        recipe_path = self.cbpi.config_folder.get_recipe_file_by_id(name)
        with open(recipe_path) as file:
            return yaml.load(file, Loader=yaml.FullLoader)

    async def remove(self, name):
        path = self.cbpi.config_folder.get_recipe_file_by_id(name)
        os.remove(path)

    async def brew(self, name):

        recipe_path = self.cbpi.config_folder.get_recipe_file_by_id(name)
        with open(recipe_path) as file:
            data = yaml.load(file, Loader=yaml.FullLoader)
            await self.cbpi.step.load_recipe(data)

    async def clone(self, id, new_name):
        recipe_path = self.cbpi.config_folder.get_recipe_file_by_id(id)
        with open(recipe_path) as file:
            data = yaml.load(file, Loader=yaml.FullLoader)
            data["basic"]["name"] = new_name
            new_id = shortuuid.uuid()
            await self.save(new_id, data)

            return new_id
