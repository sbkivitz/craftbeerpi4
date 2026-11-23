import copy
import json
import logging
import os.path
import re
from os import listdir
from os.path import isfile, join

import shortuuid
import yaml
from cbpi.api.beerxml import name_key
from cbpi.api.dataclasses import NotificationType
from cbpi.api.persist import atomic_write_bytes, atomic_write_text

from ..api.step import StepMove, StepResult, StepState


class RecipeController:

    #: In the basic data of a recipe an import put in the book: what it was
    #: imported from ("BeerXML"). Saving the brew steps to the book
    #: (StepController.book_data), the recipe editor's Save and Clone (save)
    #: all drop it, so a recipe the brewer saved, edited or cloned is never
    #: taken for an import - see keep_import.
    IMPORTED_FROM = "imported_from"

    #: In the basic data of a book recipe imported from BeerXML: the BeerXML file kept for it, relative to the config
    #: folder ("recipes/<file id>.xml", beside the recipe's own .yaml), and the NAME of the recipe in that file, the
    #: only RECIPE it holds. Plugins read them to find the recipe's planned values (OG, IBU, the mash pH...) from the
    #: recipe itself rather than from the last file uploaded. Only keep_import sets them; the recipe editor's Save
    #: keeps the book's own (save), and a clone or a new copy has none.
    SOURCE_XML = "source_xml"
    SOURCE_XML_RECIPE = "source_xml_recipe"
    SOURCE_KEYS = (SOURCE_XML, SOURCE_XML_RECIPE)
    #: A book file's id a kept BeerXML file can be named for: "recipes/<id>.xml" can then never leave the folder.
    SOURCE_ID = re.compile(r"[A-Za-z0-9_-]+")

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
        overwrite the brewer's edits. See keep_import.

        The BeerXML file a recipe was imported with (SOURCE_KEYS) is the book's
        to say, never the page's: a recipe saved in place keeps the one it has,
        whatever `data` holds, and a new one (a clone) has none."""
        basic = data.get("basic") if isinstance(data, dict) else None
        if isinstance(basic, dict):
            basic.pop(self.IMPORTED_FROM, None)
            for key in self.SOURCE_KEYS:
                basic.pop(key, None)
            basic.update(self.source_fields(name))
        path = self.cbpi.config_folder.get_recipe_file_by_id(name)
        logging.info(data)
        with open(path, "w") as file:
            yaml.dump(data, file, indent=4, sort_keys=True)

    def source_fields(self, file_id):
        """{source_xml, source_xml_recipe} as the book recipe `file_id` has them now, or {} when it has none, isn't
        in the book, or can't be read."""
        path = self.cbpi.config_folder.get_recipe_file_by_id(file_id)
        if not os.path.isfile(path):
            return {}
        try:
            with open(path) as file:
                basic = (yaml.load(file, Loader=yaml.FullLoader) or {}).get("basic") or {}
        except Exception as e:  # noqa: BLE001 - the recipe is then saved without them
            logging.warning("Recipe book: could not read %s to keep its BeerXML file: %s", file_id, e)
            return {}
        if not isinstance(basic, dict):
            return {}
        return {key: basic[key] for key in self.SOURCE_KEYS if key in basic}

    def _source_path(self, file_id):
        """Where the book recipe `file_id`'s BeerXML file is kept: beside it, as <file id>.xml. The book lists only
        .yaml files, so it is never taken for a recipe."""
        return os.path.splitext(self.cbpi.config_folder.get_recipe_file_by_id(file_id))[0] + ".xml"

    def _remove_source(self, file_id):
        """Delete the BeerXML file kept for the book recipe `file_id`, if it has one."""
        if not (isinstance(file_id, str) and self.SOURCE_ID.fullmatch(file_id)):
            return
        path = self._source_path(file_id)
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError as e:
            logging.warning("Recipe book: could not delete %s: %s", path, e)

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

    async def keep_import(self, data, source, source_xml=None):
        """Keep an imported recipe in the book. Returns (file id, replaced an earlier copy?).

        The book is where an import goes (UploadController.import_recipe): the
        brew steps are left as they are until the brewer loads it.

        Every import is kept, marked with where it came from. Importing a
        recipe of the same name again - changed in BeerSmith and exported again
        - replaces the copy the earlier import made, and never a recipe the
        brewer saved, edited or cloned, which carries no mark. The same name is
        the same exactly, or else by beerxml.name_key - "Kolsch" is "Kölsch" -
        the newest such copy. Written in one step, so a power cut leaves the
        old copy or the new one, not half a file.

        `source_xml`, from a BeerXML import: (the BeerXML file to keep for the
        recipe, as bytes; the NAME of the recipe in it). Kept beside the recipe
        as <file id>.xml, and named in its basic data (SOURCE_KEYS), before the
        recipe is written; if the recipe can't be written, the file is put back
        as it was. A file that can't be written leaves the recipe without one,
        as the log says, and imported all the same. Without one the recipe has
        no BeerXML file, and a copy it replaces loses its own.
        """
        entry = copy.deepcopy(data)
        basic = entry.setdefault("basic", {})
        basic[self.IMPORTED_FROM] = source
        for key in self.SOURCE_KEYS:
            basic.pop(key, None)
        name = basic.get("name")
        same = [(modified, file_id, b.get("name")) for file_id, b, modified in self._book_entries()
                if b.get(self.IMPORTED_FROM) and name_key(b.get("name")) == name_key(name)]
        earlier = [(m, f) for m, f, n in same if n == name] or [(m, f) for m, f, _ in same]
        file_id = max(earlier)[1] if earlier else shortuuid.uuid()
        path = self.cbpi.config_folder.get_recipe_file_by_id(file_id)
        xml_path = self._source_path(file_id)
        before = written = None
        if source_xml is not None and not self.SOURCE_ID.fullmatch(file_id):
            logging.warning("Recipe book: %s kept without a BeerXML file of its own: its file name, %s, can't "
                            "name one", name, file_id)
        elif source_xml is not None:
            content, recipe = source_xml
            try:
                try:
                    with open(xml_path, "rb") as file:
                        before = file.read()
                except FileNotFoundError:
                    before = None
                atomic_write_bytes(xml_path, content)
                written = True
            except Exception as e:  # noqa: BLE001 - the recipe is kept all the same
                logging.warning("Recipe book: %s kept without a BeerXML file of its own: %s couldn't be written: %s",
                                name, xml_path, e)
            if written:
                basic[self.SOURCE_XML] = "recipes/{}.xml".format(file_id)
                basic[self.SOURCE_XML_RECIPE] = recipe
        try:
            atomic_write_text(path, yaml.dump(entry, indent=4, sort_keys=True))
        except Exception:
            if written:
                try:
                    if before is None:
                        os.remove(xml_path)
                    else:
                        atomic_write_bytes(xml_path, before)
                except OSError as e:
                    logging.warning("Recipe book: could not put %s back: %s", xml_path, e)
            raise
        if not written:
            self._remove_source(file_id)
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
                    # the brew-day screen's Recipe Book.
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
        """Delete the book recipe `name`, and the BeerXML file kept for it."""
        path = self.cbpi.config_folder.get_recipe_file_by_id(name)
        os.remove(path)
        self._remove_source(name)

    async def brew(self, name):
        """LOAD: make the book recipe `name` the brew steps, which then know they are its copy, and when it was
        loaded (StepController.load_recipe). Raises StepRunning, changing nothing, while a step runs."""
        recipe_path = self.cbpi.config_folder.get_recipe_file_by_id(name)
        with open(recipe_path) as file:
            data = yaml.load(file, Loader=yaml.FullLoader)
        await self.cbpi.step.load_recipe(data, book_file=name, book_modified=os.path.getmtime(recipe_path))

    async def clone(self, id, new_name):
        recipe_path = self.cbpi.config_folder.get_recipe_file_by_id(id)
        with open(recipe_path) as file:
            data = yaml.load(file, Loader=yaml.FullLoader)
            data["basic"]["name"] = new_name
            new_id = shortuuid.uuid()
            await self.save(new_id, data)

            return new_id
