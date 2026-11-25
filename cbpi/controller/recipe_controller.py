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


class UpdateRefused(Exception):
    """UPDATE from an import refused, with nothing changed: why, in the brewer's words."""


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

    #: In the basic data of a BeerXML import: BeerSmith's mash figures (UploadController.MASH_NUMBERS_KEY), which
    #: UPDATE adds to a recipe the brewer changed (update_from_import).
    MASH_NUMBERS = "mash_numbers"
    #: And what goes into the mash and the sparge water, the mash profile's pH and the yeasts
    #: (StepController.RECIPE_FACTS), which UPDATE brings to a recipe the brewer changed as the import has them: taken
    #: where it has them, dropped where it hasn't, as BeerSmith now has the recipe.
    RECIPE_FACTS = ("mash_additions", "sparge_additions", "mash_ph", "yeasts")
    #: Why UPDATE changed nothing, in the brewer's words.
    UPDATE_GONE = "That recipe is no longer in the Recipe Book: it may have been deleted on another screen."
    UPDATE_UNREADABLE = "CraftBeerPi couldn't read {} in the Recipe Book, so it wasn't updated."
    UPDATE_OTHER_NAME = "The Recipe Book's {} isn't the file's {}, so it wasn't updated."

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

        The basic data is merged, never replaced: every key of the book's own
        that `data` doesn't send (its mash figures, mash_numbers, among them)
        stays as the book has it, and one sent replaces it, so an editor that
        sends only the name, author and notes loses nothing, and one that sends
        changed mash figures has them kept.

        The BeerXML file a recipe was imported with (SOURCE_KEYS) is the book's
        to say, never the page's: a recipe saved in place keeps the one it has,
        whatever `data` holds, and a new one (a clone) has none."""
        if isinstance(data, dict):
            book = self._book_basic(name)
            if not isinstance(data.get("basic"), dict):
                data["basic"] = {}
            basic = data["basic"]
            for key, value in book.items():
                if key not in basic:
                    basic[key] = value
            basic.pop(self.IMPORTED_FROM, None)
            for key in self.SOURCE_KEYS:
                basic.pop(key, None)
            basic.update({key: book[key] for key in self.SOURCE_KEYS if key in book})
        path = self.cbpi.config_folder.get_recipe_file_by_id(name)
        logging.debug("Recipe book: %s saved", name)
        with open(path, "w") as file:
            yaml.dump(data, file, indent=4, sort_keys=True)

    def _book_basic(self, file_id):
        """The basic data of book recipe `file_id` as its file has it now, or {} when it isn't in the book or can't be
        read."""
        path = self.cbpi.config_folder.get_recipe_file_by_id(file_id)
        if not os.path.isfile(path):
            return {}
        try:
            with open(path) as file:
                basic = (yaml.load(file, Loader=yaml.FullLoader) or {}).get("basic") or {}
        except Exception as e:  # noqa: BLE001 - the recipe is then saved as sent
            logging.warning("Recipe book: could not read %s to keep what the save doesn't send: %s", file_id, e)
            return {}
        return basic if isinstance(basic, dict) else {}

    def source_fields(self, file_id):
        """{source_xml, source_xml_recipe} as the book recipe `file_id` has them now, or {} when it has none, isn't
        in the book, or can't be read."""
        basic = self._book_basic(file_id)
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

    async def keep_import(self, data, source, source_xml=None, new=False):
        """Keep an imported recipe in the book. Returns (file id, replaced an earlier copy?).

        The book is where an import goes (UploadController.import_recipe): the
        brew steps are left as they are until the brewer loads it.

        Every import is kept, marked with where it came from. Importing a
        recipe of the same name again - changed in BeerSmith and exported again
        - replaces the copy the earlier import made, and never a recipe the
        brewer saved, edited or cloned, which carries no mark. The same name is
        the same exactly, or else by beerxml.name_key - "Kolsch" is "Kölsch" -
        the newest such copy. Written in one step, so a power cut leaves the
        old copy or the new one, not half a file. With `new` (ADD AS A NEW
        RECIPE) it replaces nothing: always a book recipe of its own.

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
        same = [] if new else [(modified, file_id, b.get("name")) for file_id, b, modified in self._book_entries()
                               if b.get(self.IMPORTED_FROM) and name_key(b.get("name")) == name_key(name)]
        earlier = [(m, f) for m, f, n in same if n == name] or [(m, f) for m, f, _ in same]
        file_id = max(earlier)[1] if earlier else shortuuid.uuid()
        self._write_with_source(file_id, entry, source_xml)
        return file_id, bool(earlier)

    def _write_with_source(self, file_id, entry, source_xml):
        """Write the book recipe `file_id` as `entry`, with the BeerXML file `source_xml` (keep_import) kept beside it
        and named in its basic data, or none: see keep_import."""
        basic = entry["basic"]
        name = basic.get("name")
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

    async def update_from_import(self, file_id, data, source, source_xml=None):
        """UPDATE: bring the book recipe `file_id` up to date with an import of the same name (`data`, `source` and
        `source_xml` as keep_import takes them), keeping every change the brewer made to it.

        - A recipe still as an earlier import left it (IMPORTED_FROM) is replaced by this import, as keep_import
          replaces one: the brewer changed nothing in it, so its steps become the file's.
        - Any other - changed in the book, kept with SAVE TO BOOK, copied, or written by hand - keeps its steps,
          name, author and notes, and gains the import's mash figures (MASH_NUMBERS) and BeerXML file
          (SOURCE_KEYS), and its mash and sparge additions, mash pH and yeasts as the import has them
          (RECIPE_FACTS). It stays the brewer's own, so a later import of the name never overwrites it.

        Written in one step, as keep_import writes. Returns {"steps": "kept" or "file", "same_steps": whether the
        book's steps are as they were, "basic": the basic data written, "book_steps": the steps written, "was": the
        book file's modified time before, "modified": after}. Raises UpdateRefused, with nothing changed, when
        `file_id` isn't a book recipe, can't be read, or isn't the import's name (by beerxml.name_key)."""
        if not (isinstance(file_id, str) and self.SOURCE_ID.fullmatch(file_id)):
            raise UpdateRefused(self.UPDATE_GONE)
        path = self.cbpi.config_folder.get_recipe_file_by_id(file_id)
        if not os.path.isfile(path):
            raise UpdateRefused(self.UPDATE_GONE)
        name = (data.get("basic") or {}).get("name")
        try:
            with open(path) as file:
                entry = yaml.load(file, Loader=yaml.FullLoader)
            if not isinstance(entry, dict) or not isinstance(entry.get("basic"), dict):
                raise ValueError("no basic data")
            was = os.path.getmtime(path)
        except Exception as e:  # noqa: BLE001 - refused, nothing written
            logging.warning("Recipe book: could not read %s to update it: %s", file_id, e)
            raise UpdateRefused(self.UPDATE_UNREADABLE.format(name or "it"))
        old = entry["basic"]
        if name_key(old.get("name")) != name_key(name):
            raise UpdateRefused(self.UPDATE_OTHER_NAME.format(old.get("name") or "recipe", name))
        if old.get(self.IMPORTED_FROM):
            updated = copy.deepcopy(data)
            basic = updated.setdefault("basic", {})
            basic[self.IMPORTED_FROM] = source
            kept = "file"
        else:
            updated = copy.deepcopy(entry)
            basic = updated["basic"]
            figures = (data.get("basic") or {}).get(self.MASH_NUMBERS)
            if figures:
                basic[self.MASH_NUMBERS] = copy.deepcopy(figures)
            for key in self.RECIPE_FACTS:
                fact = (data.get("basic") or {}).get(key)
                if fact:
                    basic[key] = copy.deepcopy(fact)
                else:
                    basic.pop(key, None)
            kept = "kept"
        for key in self.SOURCE_KEYS:
            basic.pop(key, None)
        self._write_with_source(file_id, updated, source_xml)
        return {"steps": kept, "same_steps": updated.get("steps") == entry.get("steps"), "basic": basic, "was": was,
                "modified": os.path.getmtime(path), "book_steps": updated.get("steps")}

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
