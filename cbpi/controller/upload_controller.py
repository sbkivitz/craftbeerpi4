import asyncio
import base64
import html
import json
import logging
import math
import os
import os.path
import pathlib
import re
import sqlite3
import webbrowser
import xml.etree.ElementTree
from os import listdir
from os.path import isfile, join

import aiohttp
import yaml
from aiohttp import web
from cbpi.api import *
from cbpi.api import beerxml, kettle_measures
from cbpi.api.base import CBPiBase
from cbpi.api.config import ConfigType
from cbpi.api.dataclasses import (Actor, Kettle, NotificationAction,
                                  NotificationType, Sensor)
from cbpi.controller.kettle_controller import KettleController
from cbpi.controller.recipe_controller import UpdateRefused
from cbpi.controller.step_controller import StepController
from voluptuous.schema_builder import message

from ..api.step import StepMove, StepResult, StepState

# aiohttp's default is ClientTimeout(total=5*60). Five minutes is not a hang,
# but it is far too long for a call made while the operator is waiting on a
# page, and long enough that a wedged Brewfather looks like a wedged server.
#
# Brewfather is a third party over the internet: give it room for a slow
# response but not for a stall. The local call is to this very process, so if
# it has not answered in a few seconds something is badly wrong and waiting
# longer will not help.
BREWFATHER_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)
LOCAL_API_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)


class UploadController:
    # Fallback step temperatures, in the unit the system is configured for.
    #
    # ConfigUpdate seeds these settings with UNIT-AWARE values, 99 C / 212 F
    # for boil and 20 C / 68 F for cooldown, and a fallback must agree with
    # them. A bare `config.get("steps_boil_temp", 98)` would not:
    #
    #   * On a Fahrenheit rig with the setting absent, the importer would build
    #     a boil step targeting 98 F. A kettle cannot boil at 98 F, so the step
    #     either never starts its timer or runs the whole boil far below
    #     boiling - and nothing reports an error, because 98 is a perfectly
    #     valid number.
    #   * Even in Celsius it would disagree: 98 against 99.
    #
    # A fallback that contradicts the value the application seeds is not a
    # fallback, it is a second configuration. One definition, unit-aware.
    DEFAULT_BOIL_TEMP = {"C": 99, "F": 212}
    DEFAULT_COOLDOWN_TEMP = {"C": 20, "F": 68}
    # What to cool to before whirlpool hops go in, when the recipe gives no
    # temperature for them - which a BeerXML file cannot: the format has no
    # temperature on a hop, so BeerSmith's Steep/Whirlpool hops arrive with a
    # steep time and nothing else. The setting is steps_whirlpool_temp; these are
    # the numbers every import used before it existed, so nothing changes until
    # someone sets it.
    DEFAULT_WHIRLPOOL_TEMP = {"C": 80, "F": 176}

    #: Minutes of whirlpool when the recipe gives no steep time.
    DEFAULT_WHIRLPOOL_MINUTES = 15

    #: The Mash Out an import adds before a sparge step when the recipe has
    #: none (AddMashOutStep): the mash to 168 F, 76 C, for 10
    #: minutes.
    MASH_OUT_TEMP = {"C": 76, "F": 168}
    MASH_OUT_MINUTES = 10

    #: In the basic data of a BeerXML recipe imported: what a strike water forecast, and the HLT's water for a new
    #: BrewStart, need from it (getMashNumbers). Part of the recipe, like its name: kept through a reset, a restart
    #: and the Recipe Book.
    MASH_NUMBERS_KEY = "mash_numbers"

    #: The BeerXML file the Recipe Book keeps for a recipe imported from one (_kept_recipe,
    #: RecipeController.keep_import): its one RECIPE, in UTF-8, at most this many bytes. Plugins that read it
    #: refuse a bigger file, or one with a DOCTYPE or an ENTITY, which this never writes.
    KEPT_XML_MAX_BYTES = 2 * 1024 * 1024
    KEPT_XML_HEAD = b'<?xml version="1.0" encoding="UTF-8"?>\n<RECIPES>\n'
    KEPT_XML_TAIL = b"\n</RECIPES>\n"

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self.logger = logging.getLogger(__name__)
        # One import at a time, gathered before it is kept in the Recipe Book.
        # See import_recipe.
        self._import_lock = asyncio.Lock()
        self._staging = False
        self._staged = None
        self._import_error = None
        self._import_warnings = []

    #: How many addition reminders the boil step holds: Hop_1 to Hop_6.
    BOIL_REMINDERS = 6

    #: The importers, by the kind the upload endpoints name, and the word the
    #: brewer is told.
    IMPORTERS = {
        "xml": ("xml_recipe_creation", "BeerXML"),
        "kbh": ("kbh_recipe_creation", "Kleiner Brauhelfer"),
        "json": ("json_recipe_creation", "MMuM-JSON"),
        "bf": ("bf_recipe_creation", "Brewfather"),
    }

    async def import_recipe(self, kind, recipe_id, update=None, new=False):
        """Keep one recipe in the Recipe Book: all of it, or none of it. The
        brew steps are left as they are; LOAD makes a book recipe the brew
        steps (POST /recipe/{file}/brew).

        Returns (True, {"name", "file", "replaced", "warnings"}) - the book
        file's id, whether it replaced an earlier import of the same name
        (RecipeController.keep_import), and the import's warnings as
        [{"title", "text"}], each also kept on every screen until OK - or
        (False, why not).

        When a recipe of the same name is in the book already, the page asks
        first, and says which way:
        - `new` (ADD AS A NEW RECIPE): kept as a book recipe of its own, never
          replacing one;
        - `update`, a book file's id (UPDATE, BeerXML only): that recipe is
          brought up to date with the import, keeping every change the brewer
          made to it (RecipeController.update_from_import). The answer adds
          "updated": true, "steps": "kept" (the book's steps as they were, with
          the import's mash figures and file) or "file" (an earlier import the
          brewer never changed, replaced), and "loaded": whether the brew steps
          loaded from it took the figures too, at once, whatever runs
          (StepController.take_book_figures). A refusal says nothing was
          changed in the Recipe Book.
        Neither: as always, replacing an earlier import of the name.

        Writing the recipe while reading it would go wrong two ways:
        - a recipe that cannot be read, such as one with a temperature of
          "not-a-number", would leave half a recipe behind;
        - two imports at once - IMPORT pressed twice - would each add their
          steps to the same recipe: every rest and the boil twice.

        So one import runs at a time. Its steps are gathered as the recipe is
        read (create_recipe, create_step) and written to the book in one
        write, only once the whole recipe has been read. A failure says so,
        adds nothing, and is reported to the caller. The boil additions the
        boil step had no reminder for go in the same write, with the recipe's
        name (StepController.NO_REMINDER_KEY), and so do a BeerXML recipe's
        mash numbers (MASH_NUMBERS_KEY), its mash and sparge additions, mash pH
        and yeasts (StepController.RECIPE_FACTS), and the BeerXML file kept for
        it (_kept_recipe, RecipeController.keep_import). A recipe that needs more
        than a vessel holds is imported with a warning for it (too_full), and one
        with additions no step reminds of with a warning naming them
        (_additions_warnings).

        Refused before anything is read when the settings would put the boil
        on the wrong vessel - see _import_problem. Never for a running step:
        the brew steps aren't touched.
        """
        method_name, label = self.IMPORTERS[kind]
        if update is not None:
            if new:
                return self._not_updated("Choose UPDATE or ADD AS A NEW RECIPE, not both.")
            if kind != "xml":
                return self._not_updated("Only a BeerXML file has BeerSmith's mash figures, so nothing was updated "
                                         "from this {} recipe.".format(label))
        if recipe_id is None or recipe_id == "" or recipe_id == []:
            return self._not_imported("Choose a recipe to import first.")
        refuse = self._not_imported if update is None else self._not_updated
        async with self._import_lock:
            self.get_config_values()
            problem = self._import_problem()
            if problem is not None:
                return refuse(problem)
            self._staging, self._staged, self._import_error = True, None, None
            self._import_warnings = []
            try:
                await getattr(self, method_name)(recipe_id)
            except Exception as e:  # noqa: BLE001 - most importers catch their own
                logging.exception("%s recipe import failed", label)
                if self._import_error is None:
                    self._import_error = str(e) or type(e).__name__
            finally:
                self._staging = False
            staged, self._staged = self._staged, None
            held, self._import_warnings = self._import_warnings, []
            error = self._import_error
            if error is None and (staged is None or not staged["steps"]):
                error = "it gave no steps"
            if error is not None:
                if update is not None:
                    return self._not_updated("The {} recipe couldn't be read: {}.".format(label, error))
                return self._not_imported(
                    "The {} recipe was not imported: {}.".format(label, error))
            staged["steps"] = self._with_mash_out(staged["steps"])
            basic = {"name": staged["name"], "author": self.cbpi.config.get("AUTHOR", "John Doe")}
            if staged.get("without_reminder"):
                basic[StepController.NO_REMINDER_KEY] = staged["without_reminder"]
            if staged.get("mash_numbers"):
                basic[self.MASH_NUMBERS_KEY] = staged["mash_numbers"]
            for key in StepController.RECIPE_FACTS:
                if staged.get(key):
                    basic[key] = staged[key]
            capacity = self.too_full(staged.get("mash_numbers"))
            held.extend(capacity)
            # As the book keeps a recipe: each step's name, kind, settings, and waiting.
            steps = [dict(name=s.get("name"), type=s.get("type"), status="I", props=s.get("props") or {})
                     for s in staged["steps"]]
            if update is not None:
                return await self._update_book(update, dict(basic=basic, steps=steps), label, staged, held, capacity)
            held.extend(self._additions_warnings(staged, steps))
            try:
                file_id, replaced = await self.cbpi.recipe.keep_import(dict(basic=basic, steps=steps), label,
                                                                       source_xml=staged.get("source_xml"),
                                                                       **({"new": True} if new else {}))
            except Exception as e:  # noqa: BLE001
                logging.exception("could not keep the imported recipe in the Recipe Book")
                return self._not_imported(
                    "The {} recipe could not be kept in the Recipe Book: {}.".format(label, e))
            kept = ("Its earlier copy in your Recipe Book was replaced." if replaced
                    else "Kept in your Recipe Book.")
            self.cbpi.notify("{} recipe imported".format(label),
                             "{}. {}".format(staged["name"], kept), NotificationType.INFO)
            for title, text in held:
                self._warn_brewer(title, text)
            return True, dict(name=staged["name"], file=file_id, replaced=bool(replaced),
                              warnings=[dict(title=title, text=text) for title, text in held])

    async def _update_book(self, file_id, data, label, staged, held, capacity):
        """import_recipe's UPDATE of the book recipe `file_id` with the import `data`, and of the brew steps loaded
        from it. Where the book's steps are kept, only the warnings about the recipe's figures (`capacity`) and its
        additions (_additions_warnings, for the book's steps) apply: the others are about the file's steps."""
        try:
            done = await self.cbpi.recipe.update_from_import(file_id, data, label,
                                                             source_xml=staged.get("source_xml"))
        except UpdateRefused as e:
            return self._not_updated(str(e))
        except Exception as e:  # noqa: BLE001
            logging.exception("could not update the Recipe Book's copy from the import")
            return self._not_updated("{} couldn't be updated in the Recipe Book: {}.".format(staged["name"], e))
        kept = done["steps"] == "kept"
        warnings = list(capacity) if kept else list(held)
        warnings.extend(self._additions_warnings(staged, done.get("book_steps") if kept else data["steps"]))
        loaded = False
        take = getattr(self.cbpi.step, "take_book_figures", None)
        if callable(take):
            source = {key: done["basic"][key] for key in StepController.SOURCE_KEYS if key in done["basic"]}
            facts = {key: data["basic"].get(key) for key in StepController.RECIPE_FACTS}
            try:
                loaded = bool(await take(file_id, data["basic"].get(self.MASH_NUMBERS_KEY), source,
                                         modified=(done["was"], done["modified"]) if done["same_steps"] else None,
                                         facts=facts))
            except Exception:  # noqa: BLE001 - the book is updated; LOAD brings the figures
                logging.exception("the brew steps could not take the updated recipe's mash figures")
        said = ("Updated in your Recipe Book with BeerSmith's mash figures and its file. Your steps are as they were."
                if kept else "Updated in your Recipe Book: its steps, mash figures and file are this file's, as you "
                             "hadn't changed it there.")
        if loaded:
            said += " Brew Day's copy has the mash figures too, and its steps are as they were."
        self.cbpi.notify("{} recipe updated".format(label), "{}. {}".format(staged["name"], said),
                         NotificationType.INFO)
        for title, text in warnings:
            self._warn_brewer(title, text)
        return True, dict(name=staged["name"], file=file_id, replaced=False, updated=True,
                          steps="kept" if kept else "file", loaded=loaded,
                          warnings=[dict(title=title, text=text) for title, text in warnings])

    def _not_updated(self, why):
        """(False, why, saying nothing was changed in the Recipe Book): UPDATE refused, told to every screen."""
        message = "{} Nothing was changed in the Recipe Book.".format(why)
        logging.warning("Recipe not updated: %s", message)
        self.cbpi.notify("Recipe not updated", message, NotificationType.ERROR)
        return False, message

    def _not_imported(self, why, kind=NotificationType.ERROR):
        """(False, why, saying nothing was added to the Recipe Book), after telling every screen: an error, unless
        `kind` says otherwise - an info for a refusal that needs nothing from the brewer."""
        message = "{} Nothing was added to the Recipe Book.".format(why)
        logging.warning("Recipe not imported: %s", message)
        self.cbpi.notify("Recipe not imported", message, kind)
        return False, message

    def _import_problem(self):
        """Why the settings must not be imported with, or None. Read before the
        recipe is.

        A blank BoilKettle setting puts the boil on the mash kettle. On a HERMS
        that is the Mash Tun, whose logic is PID_HERMS: the Boil step would
        drive the HLT element and the pump towards 210 F, and nothing would
        heat the boil kettle. Right for a one-vessel system, so it is
        refused only when there is more than one kettle, naming the vessel the
        boil would have gone on.
        """
        if self.kettle is None:
            return "No mash kettle is set. Choose it in Settings, MASH_TUN."
        kettles = list(getattr(self.cbpi.kettle, "data", None) or [])
        chosen = self.cbpi.config.get("BoilKettle", None)
        if chosen in (None, ""):
            if len(kettles) > 1:
                return ("No boil kettle is set, so the boil would go on '{}', the "
                        "mash kettle. Choose the boil kettle in Settings, BoilKettle."
                        .format(getattr(self.kettle, "name", self.id)))
            return None
        if self.boilkettle is None:
            return ("The boil kettle in Settings, BoilKettle, is not one of this "
                    "system's kettles. Choose it again.")
        return None

    def _creation_failed(self, label, error, name=""):
        """An importer could not finish. Inside import_recipe that is reported
        once, there, saying nothing was added to the Recipe Book."""
        logging.error("%s: %s", label, error)
        if self._staging:
            if self._import_error is None:
                self._import_error = str(error) or type(error).__name__
            return
        self.cbpi.notify("{}: {}".format(label, error), name, NotificationType.ERROR)

    def _creation_done(self, title, name):
        """An importer finished. Inside import_recipe the brewer is told once
        the recipe is in the Recipe Book, not before."""
        if not self._staging:
            self.cbpi.notify(title, name, NotificationType.INFO)

    def _import_warning(self, title, text):
        """Warn about the recipe being read. Inside import_recipe the warning
        waits until the import has succeeded, so it follows "recipe imported"
        and is never given for a recipe that was not imported."""
        if self._staging:
            self._import_warnings.append((title, text))
        else:
            self._warn_brewer(title, text)

    def _warn_brewer(self, title, text):
        """A warning that stays on every screen until OK is pressed."""
        self.cbpi.notify(title, text, NotificationType.WARNING,
                         action=[NotificationAction("OK")], keep=True)

    #: Litres in a US gallon, kilograms in a pound.
    GAL_L, LB_KG = 3.785411784, 0.45359237
    #: The room grain takes up in the mash tun, beside its water: gal/lb on a F brewery, L/kg on a C one.
    GRAIN_VOLUME = {"F": 0.08, "C": 0.67}

    @staticmethod
    def _figure(value):
        """A setting or a recipe figure as a number, or None: blank, not a number, or not finite."""
        if value is None or isinstance(value, bool):
            return None
        try:
            found = float(value.strip().replace(",", ".")) if isinstance(value, str) else float(value)
        except (TypeError, ValueError):
            return None
        return found if math.isfinite(found) else None

    def hlt_water(self, numbers):
        """The water a recipe needs in the HLT, worked out as the recipe pages work it out (recipeWater): its volume
        before the boil (mash_numbers' boil_l, less top_up_kettle_l), what the grain keeps (grain_kg by
        GRAIN_ABSORPTION, in gal/lb on F, L/kg on C) and the lauter dead space (lauter_dead_l). In the brewery's
        volume unit: {"total", "before", "kept", "lost", "unit"}, unit "gal" on F and "L" on C. None without a volume
        before the boil, a grain weight or GRAIN_ABSORPTION."""
        numbers = numbers if isinstance(numbers, dict) else {}
        absorption = self._figure(self.cbpi.config.get("GRAIN_ABSORPTION", None))
        boil, grain = self._figure(numbers.get("boil_l")), self._figure(numbers.get("grain_kg"))
        if boil is None or grain is None or absorption is None:
            return None
        gallons = str(self.cbpi.config.get("TEMP_UNIT", "C")).upper() == "F"
        per_kg = absorption * self.GAL_L / self.LB_KG if gallons else absorption
        before = boil - (self._figure(numbers.get("top_up_kettle_l")) or 0)
        kept = grain * per_kg
        lost = self._figure(numbers.get("lauter_dead_l")) or 0
        to_unit = self.GAL_L if gallons else 1.0
        return dict(total=(before + kept + lost) / to_unit, before=before / to_unit, kept=kept / to_unit,
                    lost=lost / to_unit, unit="gal" if gallons else "L")

    def mash_volume(self, numbers):
        """What the mash tun holds at its fullest: the water the mash steps add (mash_numbers' infusions_l, every
        infusion added up, else infuse_l, the strike water) and the grain's own volume, GRAIN_VOLUME. In the
        brewery's volume unit: {"total", "water", "grain", "unit"}. None without the water or a grain weight."""
        numbers = numbers if isinstance(numbers, dict) else {}
        water = self._figure(numbers.get("infusions_l"))
        if water is None:
            water = self._figure(numbers.get("infuse_l"))
        grain = self._figure(numbers.get("grain_kg"))
        if water is None or grain is None:
            return None
        if str(self.cbpi.config.get("TEMP_UNIT", "C")).upper() == "F":
            water, grain, unit = water / self.GAL_L, grain / self.LB_KG * self.GRAIN_VOLUME["F"], "gal"
        else:
            grain, unit = grain * self.GRAIN_VOLUME["C"], "L"
        return dict(total=water + grain, water=water, grain=grain, unit=unit)

    def boil_volume(self, numbers):
        """The recipe's volume before the boil (mash_numbers' boil_l) in the brewery's volume unit:
        {"total", "unit"}, or None without one."""
        boil = self._figure((numbers if isinstance(numbers, dict) else {}).get("boil_l"))
        if boil is None:
            return None
        if str(self.cbpi.config.get("TEMP_UNIT", "C")).upper() == "F":
            return dict(total=boil / self.GAL_L, unit="gal")
        return dict(total=boil, unit="L")

    def too_full(self, numbers):
        """The import's warnings, [(title, text)], for each vessel the recipe with these mash_numbers needs more of
        than its capacity, to 0.1 in the brewery's volume unit: the HLT its water (hlt_water), the mash tun its mash
        (mash_volume), the boil kettle its boil (boil_volume). The vessels are found as the brew-day screen and the
        strike water helper find them (hltpump.kettle_roles); one that can't be told, a capacity that is blank or
        can't be right (kettle_measures.usable), or a figure the recipe doesn't give, is no warning. Only a BeerXML
        import gives the figures. Never raises: the import goes on without warnings."""
        try:
            from cbpi.extension.hltpump import kettle_roles

            roles = kettle_roles(self.cbpi)
            found = []
            for role, word, need in (("hlt", "HLT", self.hlt_water), ("mt", "mash tun", self.mash_volume),
                                     ("bk", "boil kettle", self.boil_volume)):
                kettle = roles.get(role)
                capacity = kettle_measures.usable(getattr(kettle, "capacity", None)) if kettle is not None else None
                needed = need(numbers) if capacity is not None else None
                if needed is None or round(needed["total"], 1) <= capacity:
                    continue
                name = "the {}".format(str(getattr(kettle, "name", "") or "").strip() or word)
                holds = "{} holds {:.1f} {}".format(name, capacity, needed["unit"])
                if role == "hlt":
                    parts = ["{:.1f} before the boil".format(needed["before"]),
                             "{:.1f} the grain keeps".format(needed["kept"])]
                    if round(needed["lost"], 1) > 0:
                        parts.append("{:.1f} left under the mash tun's false bottom".format(needed["lost"]))
                    text = ("This recipe needs about {:.1f} {} of water ({}), and {}. Check the batch size in "
                            "BeerSmith, or plan to add water during the sparge.".format(
                                needed["total"], needed["unit"], ", ".join(parts[:-1]) + " and " + parts[-1], holds))
                elif role == "mt":
                    text = ("This recipe mashes about {:.1f} {} ({:.1f} of water and {:.1f} of grain), and {}. Check "
                            "the batch size in BeerSmith.".format(needed["total"], needed["unit"], needed["water"],
                                                                  needed["grain"], holds))
                else:
                    text = "This recipe boils {:.1f} {}, and {}. Check the batch size in BeerSmith.".format(
                        needed["total"], needed["unit"], holds)
                found.append(("More than {} holds".format(name), text))
            return found
        except Exception as e:  # noqa: BLE001 - the recipe is imported all the same
            logging.warning("Could not check the recipe against the vessels' capacities: %s", e)
            return []

    @staticmethod
    def _reminder_minutes(minutes):
        """An addition's minutes before the end of the boil, or None when the
        boil step cannot remind of it: not a number, or below -1."""
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            return None
        return minutes if minutes > -1 else None

    @staticmethod
    def _before_end(minutes):
        if minutes <= 0:
            return "at the end of the boil"
        return "{:g} minute{} before the end".format(minutes, "" if minutes == 1 else "s")

    def _unreminded_text(self, alerts):
        """The warning for more boil additions than the boil step has reminders.
        alerts is longest first, as getBoilAlerts fills the reminders, so the
        additions left out are the last ones."""
        missed = alerts[self.BOIL_REMINDERS:]
        named = "; ".join("{}, {}".format(label or "an addition", self._before_end(minutes))
                          for minutes, label in missed)
        return ("This recipe has {} boil additions, but the boil step can remind you "
                "of only six. No reminder for: {}. Watch the clock for {}, or set a "
                "timer on the brew-day screen.".format(
                    len(alerts), named, "it" if len(missed) == 1 else "them"))

    def _keep_unreminded(self, missed):
        """Keep the boil additions left without a reminder with the recipe, so
        the pre-brew check can name them again on brew day: [[minutes, label],
        ...] as getBoilAlerts has them.

        Inside import_recipe they go with the recipe it writes. An importer
        called on its own has made the mash profile already (create_recipe), so
        they go in its basic data, which the boil step's own POST saves next.
        """
        kept = [dict(name=label or None, minutes=minutes) for minutes, label in missed]
        if self._staging:
            if self._staged is not None:
                self._staged.setdefault("without_reminder", []).extend(kept)
            return
        basic = getattr(getattr(self.cbpi, "step", None), "basic_data", None)
        if isinstance(basic, dict):
            basic.setdefault(StepController.NO_REMINDER_KEY, []).extend(kept)

    async def get_kbh_recipes(self):
        try:
            path = self.cbpi.config_folder.get_upload_file("kbh.db")
            conn = sqlite3.connect(path)
            try:
                c = conn.cursor()
                c.execute("SELECT ID, Sudname, Status FROM Sud")
                data = c.fetchall()
                result = []
                for row in data:
                    element = {"value": str(row[0]), "label": str(row[1])}
                    result.append(element)
                return result
            finally:
                conn.close()
        except Exception:
            return []

    async def get_xml_recipes(self):
        try:
            path = self.cbpi.config_folder.get_upload_file("beer.xml")
            e = self._xml_file_root(path)
            result = []
            counter = 1
            for idx, val in enumerate(e.findall("RECIPE")):
                element = {"value": str(counter), "label": val.find("NAME").text}
                result.append(element)
                counter += 1
            return result
        except Exception:
            return []

    async def get_json_recipes(self):
        try:
            path = self.cbpi.config_folder.get_upload_file("mmum.json")
            with open(path, encoding="utf-8-sig") as f:
                e = json.load(f)
            result = []
            result.append({"value": str(1), "label": e["Name"]})
            return result
        except Exception:
            return []

    async def get_brewfather_recipes(self, offset=0):
        limit = 50
        length = self.cbpi.config.get("brewfather_list_length", 50)
        repeat = True
        brewfather = True
        result = []
        self.url = "https://api.brewfather.app/v2/recipes"
        brewfather_user_id = self.cbpi.config.get("brewfather_user_id", None)
        if brewfather_user_id == "" or brewfather_user_id is None:
            brewfather = False

        brewfather_api_key = self.cbpi.config.get("brewfather_api_key", None)
        if brewfather_api_key == "" or brewfather_api_key is None:
            brewfather = False

        if brewfather == True:
            encodedData = base64.b64encode(
                bytes(f"{brewfather_user_id}:{brewfather_api_key}", "ISO-8859-1")
            ).decode("ascii")
            headers = {"Authorization": "Basic %s" % encodedData}
            parameters = {"limit": limit}
            while repeat == True:
                try:
                    async with aiohttp.ClientSession(
                        headers=headers, timeout=BREWFATHER_TIMEOUT
                    ) as bf_session:
                        async with bf_session.get(self.url, params=parameters) as r:
                            if r.status == 429:
                                try:
                                    seconds = int(r.headers["Retry-After"])
                                    minutes = round(seconds / 60)
                                except:
                                    seconds = None
                                if not seconds:
                                    logging.error(
                                        "Too many requests to BF api. Try again later"
                                    )
                                    self.cbpi.notify(
                                        "Error",
                                        "Too many requests to BF api. Try again later",
                                        NotificationType.ERROR,
                                    )
                                else:
                                    logging.error(
                                        f"Too many requests to BF api. Try in {minutes} minutes again."
                                    )
                                    self.cbpi.notify(
                                        "Error",
                                        f"Too many requests to BF api. Try in {minutes} minutes again.",
                                        NotificationType.ERROR,
                                    )
                                repeat = False
                                logging.error(r.headers["Retry-After"])
                            else:
                                bf_recipe_list = await r.json()
                        await bf_session.close()
                except Exception as e:
                    logging.error(e)
                    repeat = False
                try:
                    if bf_recipe_list:
                        for row in bf_recipe_list:
                            recipe_id = row["_id"]
                            name = row["name"]
                            element = {"value": recipe_id, "label": name}
                            result.append(element)
                    else:
                        repeat = False
                except Exception as e:
                    logging.error(e)
                try:
                    if len(bf_recipe_list) != limit:
                        repeat = False
                    else:
                        parameters = {"limit": limit, "start_after": recipe_id}
                except Exception as e:
                    logging.error(e)

        try:
            newlist = sorted(result, key=lambda d: d["label"])
            listlength = len(newlist)
            max = math.floor(listlength / length)
            sortlist = []
            for i in range(0, max + 1):
                sortlist.append({"value": i * length, "label": i * length})
            return newlist, sortlist, length
        except:
            logging.error("Return empty BF recipe list")
            sortlist = [{"value": 0, "label": "0"}]
            return result, sortlist, length

    def get_creation_path(self):
        creation_path = self.cbpi.config.get("RECIPE_CREATION_PATH", "upload")
        path = {"path": "upload"} if creation_path == "" else {"path": creation_path}
        return path

    def allowed_file(self, filename, extension):
        return "." in filename and filename.rsplit(".", 1)[1] in set([extension])

    #: What an uploaded recipe file is: by its name first, then by the type the
    #: browser sent. Browsers disagree about the type: Firefox and Safari send
    #: application/xml for a .xml file, which checking the type alone would
    #: refuse as "Wrong content type".
    UPLOAD_KINDS = {
        "xml": ("beer.xml", ("text/xml", "application/xml")),
        "json": ("mmum.json", ("application/json",)),
        "sqlite": ("kbh.db", ("application/octet-stream", "application/x-sqlite3",
                              "application/vnd.sqlite3")),
    }

    def _upload_kind(self, filename, content_type):
        extension = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
        if extension in self.UPLOAD_KINDS:
            return extension
        return next((kind for kind, (_, types) in self.UPLOAD_KINDS.items()
                     if content_type in types), None)

    @staticmethod
    def _kept_bytes(kind, content):
        """What is kept of an uploaded BeerXML or MMuM file: the bytes as sent.

        Decoded as UTF-8 and written back as text, a file would not be what
        was sent: on Windows every line ending would gain a carriage return
        (CR CR LF), and a BeerXML file declares its own encoding - BeerSmith's
        says ISO-8859-1 - so a raw degree sign in one would come back as a
        replacement character.

        BeerXML that does not parse as sent is read as beerxml.read reads it -
        in its declared encoding, UTF-8, Windows-1252 or Latin-1, or else made
        plain English letters - and kept as UTF-8 under a UTF-8 declaration, so
        it parses: a Latin-1 o-umlaut under a UTF-8 declaration stays an
        o-umlaut, a NUL byte goes. So is BeerXML declaring ISO-8859-1 with a
        byte from 0x80 to 0x9F, which beerxml.read takes as Windows-1252, as
        browsers do: BeerSmith's 0x92 is an apostrophe, not a control
        character. The log says how it was read. JSON that is
        not UTF-8, and a file not even that makes XML, are still decoded
        leniently, so they import with odd characters rather than not at all.
        """
        if kind == "xml":
            declared = beerxml.declared_encoding(content) or "UTF-8 (none declared)"
            as_declared = beerxml.read_as_declared(content)
            if as_declared:
                try:
                    xml.etree.ElementTree.fromstring(content)
                    logging.info("BeerXML upload: read in its declared encoding, %s", declared)
                    return content
                except Exception:  # noqa: BLE001 - ParseError, or LookupError for an encoding Python doesn't know
                    pass
            try:
                found = beerxml.read(content)
            except Exception as e:  # noqa: BLE001
                logging.warning("BeerXML upload: not XML in any encoding (%s); kept with stand-in characters", e)
            else:
                if found.plain:
                    logging.warning("BeerXML upload: doesn't parse as %s in any of %s; read as %s, made plain "
                                    "English letters, and kept as UTF-8", declared,
                                    ", ".join(beerxml.decode_order(content)), found.encoding)
                elif not as_declared and found.encoding == "cp1252":
                    logging.info("BeerXML upload: its declared %s read as Windows-1252, as browsers read it, and "
                                 "kept as UTF-8", declared)
                elif not as_declared:
                    logging.warning("BeerXML upload: its declared %s is read as Windows-1252, as browsers read it, "
                                    "but has a byte Windows-1252 leaves undefined; read as %s, and kept as UTF-8",
                                    declared, found.encoding)
                else:
                    logging.warning("BeerXML upload: doesn't parse as its declared %s; read as %s, and kept as "
                                    "UTF-8", declared, found.encoding)
                return beerxml.utf8_document(found.text)
        else:
            try:
                content.decode("utf-8-sig")
                return content
            except UnicodeDecodeError:
                pass
        return content.decode("utf-8", "replace").encode("utf-8")

    async def FileUpload(self, data):
        """Keep an uploaded recipe file. Returns True if it was kept.

        Reported to the caller, so the Upload page does not reload over the
        error: it reloads on success.
        """
        fileData = data["File"]
        filename = fileData.filename
        recipe_file = fileData.file
        kind = self._upload_kind(filename, fileData.content_type)
        if kind is None:
            self.cbpi.notify(
                "Error",
                "{} is not a recipe file this can read - a BeerXML .xml, an MMuM "
                ".json or a Kleiner Brauhelfer .sqlite. Upload failed".format(filename),
                NotificationType.ERROR,
            )
            return False
        target, _ = self.UPLOAD_KINDS[kind]
        try:
            content = recipe_file.read()
            self.path = self.cbpi.config_folder.get_upload_file(target)
            if kind != "sqlite":
                content = self._kept_bytes(kind, content)
            with open(self.path, "wb") as f:
                f.write(content)
        except Exception as e:  # noqa: BLE001
            self.cbpi.notify("Error", "Upload of {} failed: {}".format(filename, e),
                             NotificationType.ERROR)
            return False
        what = {"xml": "XML Recipe {}".format(filename),
                "json": "JSON Recipe {}".format(filename),
                "sqlite": "Kleiner Brauhelfer database"}[kind]
        self.cbpi.notify("Success", "{} has been uploaded".format(what),
                         NotificationType.SUCCESS)
        return True

    # function to create a recipe from the Kleiner Brauhelfer database
    async def kbh_recipe_creation(self, Recipe_ID):
        config = self.get_config_values()
        name = ""
        if self.kettle is not None:
            # check if kbh database is available in the upload folder and connect to it
            self.path = self.cbpi.config_folder.get_upload_file("kbh.db")
            if os.path.exists(self.path) is False:
                self.cbpi.notify(
                    "File Not Found",
                    "Please upload a kbh V2 database file",
                    NotificationType.ERROR,
                )

            conn = None
            try:
                # Get Recipe Nmae
                conn = sqlite3.connect(self.path)
                c = conn.cursor()
                c.execute("SELECT Sudname FROM Sud WHERE ID = ?", (Recipe_ID,))
                row = c.fetchone()
                name = row[0]
                # get MashIn Temp
                mashin_temp = None
                c.execute(
                    "SELECT TempWasser FROM Maischplan WHERE Typ = 0 AND SudID = ?",
                    (Recipe_ID,),
                )
                row = c.fetchone()
                try:
                    if self.cbpi.config.get("TEMP_UNIT", "C") == "C":
                        mashin_temp = str(int(row[0]))
                    else:
                        mashin_temp = str(round(9.0 / 5.0 * int(row[0]) + 32))
                except:
                    pass
                # get the hop addition times
                c.execute(
                    "SELECT Zeit, Name, Vorderwuerze FROM Hopfengaben WHERE Vorderwuerze <> 1 AND SudID = ?",
                    (Recipe_ID,),
                )
                hops = c.fetchall()
                whirlpool = []
                #for hop in hops:
                #    if hop[2] == 5: # whirlpool hops are defined with Vorderwuerze = 5 in the KBH database (Ausschlagen)
                #        whirlpool.append(hop)
                #for whirl in whirlpool:
                #    hops.remove(whirl)
                # get the misc addition times
                
                c.execute(
                    "SELECT Zugabedauer, Name FROM WeitereZutatenGaben WHERE Zeitpunkt = 1 AND SudID = ?",
                    (Recipe_ID,),
                )
                miscs = c.fetchall()
                try:
                    c.execute(
                        "SELECT Zeit, Name FROM Hopfengaben WHERE Vorderwuerze = 1 AND SudID = ?",
                        (Recipe_ID,),
                    )
                    FW_Hops = c.fetchall()
                    FirstWort = self.getFirstWort(FW_Hops, "kbh")
                except:
                    FirstWort = "No"

                # get the boiltime from the database
                c.execute("SELECT Kochdauer FROM Sud WHERE ID = ?", (Recipe_ID,))
                row = c.fetchone()
                BoilTime = str(int(row[0]))

                await self.create_recipe(name)

                # create a mashin step if mashin_temp is available (just heating up to temp and waiting for user input)
                if mashin_temp is not None:
                    step_type = self.mashin if self.mashin != "" else "MashInStep"
                    step_string = {
                        "name": "MashIn",
                        "props": {
                            "AutoMode": self.AutoMode,
                            "Kettle": self.id,
                            "Sensor": self.kettle.sensor,
                            "Temp": mashin_temp,
                            "Timer": "0",
                            "Notification": "Target temperature reached. Please add malt.",
                        },
                        "status_text": "",
                        "status": "I",
                        "type": step_type,
                    }
                    await self.create_step(step_string)

                # if mashin_temp is not available, create a mashin step with the temp of the first mash step if addmashin is set to yes
                step_temp_old = 0
                first_rest = True
                for row in c.execute(
                    "SELECT Name, TempRast, DauerRast FROM Maischplan WHERE Typ <> 0 AND SudID = ?",
                    (Recipe_ID,),
                ):
                    step_temp = (float(row[1]))
                    # Only before the first rest: a later rest is never a mash-in.
                    if first_rest and mashin_temp is None and self.addmashin == "Yes":
                        step_type = self.mashin if self.mashin != "" else "MashInStep"
                        step_string = {
                            "name": "MashIn",
                            "props": {
                                "AutoMode": self.AutoMode,
                                "Kettle": self.id,
                                "Sensor": self.kettle.sensor,
                                "Temp": (
                                    str(step_temp)
                                    if self.TEMP_UNIT == "C"
                                    else str(round(9.0 / 5.0 * step_temp + 32,1))
                                ),
                                "Timer": "0",
                                "Notification": "Target temperature reached. Please add malt.",
                            },
                            "status_text": "",
                            "status": "I",
                            "type": step_type,
                        }
                        await self.create_step(step_string)
                    first_rest = False

                    try:
                        if float(step_temp) < float(step_temp_old):
                            step_string = {
                                "name": "Temp reduction!",
                                "props": {
                                    "AutoNext": "No",
                                    "Kettle": self.id,
                                    "Notification": f"Temperature reduction from {step_temp_old}  {self.TEMP_UNIT} to {step_temp} {self.TEMP_UNIT}. Please reduce the temperature manually.",
                                },
                                "status_text": "",
                                "status": "I",
                                "type": "NotificationStep",
                            }

                            await self.create_step(step_string)
                    except Exception as e:
                        logging.error(e)


                    # create the mash steps based on the recipe settings (time and temp)
                    step_type = self.mash if self.mash != "" else "MashStep"
                    step_string = {
                        "name": str(row[0]),
                        "props": {
                            "AutoMode": self.AutoMode,
                            "Kettle": self.id,
                            "Sensor": self.kettle.sensor,
                            "Temp": (
                                str(int(row[1]))
                                if self.TEMP_UNIT == "C"
                                else str(round(9.0 / 5.0 * int(row[1]) + 32))
                            ),
                            "Timer": str(int(row[2])),
                        },
                        "status_text": "",
                        "status": "I",
                        "type": step_type,
                    }
                    await self.create_step(step_string)
                    step_temp_old = step_temp

                # MashOut -> Notification step that sends notification and waits for user input to move to next step (AutoNext=No)
                if self.mashout == "NotificationStep":
                    step_string = {
                        "name": "Lautering",
                        "props": {
                            "AutoNext": "No",
                            "Kettle": self.id,
                            "Notification": "Mash Process completed. Please start lautering and press next to start boil.",
                        },
                        "status_text": "",
                        "status": "I",
                        "type": self.mashout,
                    }
                    await self.create_step(step_string)

                # create a boil step with hop alarms and alarm for first wort hops
                Hops , Whirlpool= self.getBoilAlerts(hops, miscs, "kbh")
                step_type = self.boil if self.boil != "" else "BoilStep"
                step_string = {
                    "name": "Boil Step",
                    "props": {
                        "AutoMode": self.AutoMode,
                        "Kettle": self.boilid,
                        "Sensor": self.boilkettle.sensor,
                        "Temp": int(self.BoilTemp),
                        "Timer": BoilTime,
                        "First_Wort": FirstWort[0],
                        "First_Wort_text": FirstWort[1],
                        "LidAlert": "Yes",
                        "Hop_1": Hops[0][0],
                        "Hop_1_text": Hops[0][1],
                        "Hop_2": Hops[1][0],
                        "Hop_2_text": Hops[1][1],
                        "Hop_3": Hops[2][0],
                        "Hop_3_text": Hops[2][1],
                        "Hop_4": Hops[3][0],
                        "Hop_4_text": Hops[3][1],
                        "Hop_5": Hops[4][0],
                        "Hop_5_text": Hops[4][1],
                        "Hop_6": Hops[5][0],
                        "Hop_6_text": Hops[5][1],
                    },
                    "status_text": "",
                    "status": "I",
                    "type": step_type,
                }

                await self.create_step(step_string)

                # whirlpool hops are added at the end of the boil step
                # and the kettle is cooled down to the whirlpool temperature
                # the whirlpool temperature is set in the recipe and is used to cool down the kettle
                
                if Whirlpool != []:
 
                    step_type = self.cooldown
                    step_name = "CoolDown for Whirlpool Hop"
                    cooldown_sensor = ""
                    step_temp = float(Whirlpool)
                    step_timer = ""

                    if step_type.find("Cooldown") != -1 and self.cooldown != "":
                        cooldown_sensor = (
                                self.boilkettle.sensor
                            )  # fall back to boilkettle sensor if no other sensor is specified
                        step_string = {
                            "name": "Cooldown for Whirlpool Hop",
                            "props": {
                                "Kettle": self.boilid,
                                "Timer": step_timer,
                                "Temp": step_temp,
                                "Sensor": cooldown_sensor,
                                "Actor": self.CoolDownActor,
                            },
                            "status_text": "",
                            "status": "I",
                            "type": step_type,
                        }
                        await self.create_step(step_string)

                    if self.cooldown.find("Cooldown") != -1:
                        notification = "Target Whirlpool temperature reached. Please add Whirlpool hops." 
                        autonext = "No"
                    else: 
                        notification= f"Cool down to {step_temp} {self.TEMP_UNIT}. Then add Whirlpool hops."
                        autonext = "No"
                    step_string = {
                        "name": "Whirlpool Hop",
                        "props": {
                            "AutoNext": autonext,
                            "Kettle": self.id,
                            "Notification": notification,
                        },
                        "status_text": "",
                        "status": "I",
                        "type": "NotificationStep",
                    }
                    await self.create_step(step_string)

                # create a whirlpool step with optional cooldown
                whirlpool_temp = step_temp if Whirlpool != [] else None
                if not whirlpool:
                    await self.create_Whirlpool_Cooldown(temp=whirlpool_temp)
                else:
                    await self.create_Whirlpool_Cooldown(
                        str(abs(whirlpool[0][0])), temp=whirlpool_temp
                    )  # from kbh this value comes as negative but must be positive

                self._creation_done("KBH Recipe created", name)

            except Exception as e:
                self._creation_failed("KBH Recipe creation failure", e, name)
            finally:
                if conn is not None:
                    conn.close()
        else:
            self._creation_failed(
                "Recipe Upload",
                "No default Kettle defined. Please specify default Kettle in settings",
            )

    def getJsonMashin(self, id):
        self.path = self.cbpi.config_folder.get_upload_file("mmum.json")
        with open(self.path, encoding="utf-8-sig") as f:
            e = json.load(f)
        return float(e["Einmaischtemperatur"])

    # function to create a recipe from a MUMM json recipe file
    async def json_recipe_creation(self, Recipe_ID):
        config = self.get_config_values()
        name = ""
        try:
            if self.kettle is not None:
                # check if  mmum-json file is located in upload folder and load it
                self.path = self.cbpi.config_folder.get_upload_file("mmum.json")
                if os.path.exists(self.path) is False:
                    self.cbpi.notify(
                        "File Not Found",
                        "Please upload a MMuM-JSON File",
                        NotificationType.ERROR,
                    )

                with open(self.path, encoding="utf-8-sig") as f:
                    e = json.load(f)
                logging.info(json.dumps(e, indent=4))
                name = e["Name"]
                boil_time = float(e["Kochzeit_Wuerze"])

                logging.info(name)
                logging.info(boil_time)

                await self.create_recipe(name)

                # get the hop addition times
                hops = []
                firstHops = []
                whirlpool_hops = []

                Hopfenliste=e["Hopfenkochen"]
                for Hopfen in Hopfenliste:
                    #logging.error(Hopfen)
                    if Hopfen["Typ"] == "Standard":
                        hops.append({"name": Hopfen["Sorte"], "time": Hopfen["Zeit"]})
                    if Hopfen["Typ"] == "Vorderwuerze":
                        firstHops.append({"name": Hopfen["Sorte"]})
                    if Hopfen["Typ"] == "Whirlpool":
                        whirlpool_hops.append({"name": Hopfen["Sorte"]})
 
                #logging.error(hops)
                #logging.error(firstHops)
                #logging.error(whirlpool_hops)

                FirstWort = self.getFirstWort(firstHops, "json")
                #logging.error(FirstWort)
                miscs = []
                try:
                    weitere_zutaten=e["Gewuerze_etc"]
                    for zutat in weitere_zutaten:
                        miscs.append({"name": zutat["Name"], "time": zutat["Kochzeit"]})
                #logging.error(miscs)
                except:
                    pass
                # Mash Steps -> first step is different as it heats up to defined temp and stops with notification to add malt
                # AutoMode is yes to start and stop automatic mode or each step
                MashIn_Flag = True
                step_kettle = self.id
                last_step_temp = 0
                step_temp_old = 0
                logging.info(
                    step_kettle
                )  ###################################################
                for row in self.getSteps(Recipe_ID, "json"):
                    step_name = str(row.get("name"))
                    step_timer = str(int(row.get("timer")))
                    step_temp = str(int(row.get("temp")))
                    last_step_temp = step_temp
                    sensor = self.kettle.sensor
                    if MashIn_Flag == True:
                        if row.get("timer") == 0:
                            step_type = (
                                self.mashin if self.mashin != "" else "MashInStep"
                            )
                            Notification = (
                                "Target temperature reached. Please add malt."
                            )
                            MashIn_Flag = False
                            if step_name is None or step_name == "":
                                step_name = "MashIn"
                        elif self.addmashin == "Yes":
                            step_type = (
                                self.mashin if self.mashin != "" else "MashInStep"
                            )
                            Notification = (
                                "Target temperature reached. Please add malt."
                            )
                            MashIn_Flag = False
                            step_string = {
                                "name": "MashIn",
                                "props": {
                                    "AutoMode": self.AutoMode,
                                    "Kettle": self.id,
                                    "Sensor": self.kettle.sensor,
                                    "Temp": self.getJsonMashin(Recipe_ID),
                                    "Timer": 0,
                                    "Notification": Notification,
                                },
                                "status_text": "",
                                "status": "I",
                                "type": step_type,
                            }
                            await self.create_step(step_string)
                            logging.info(
                                step_kettle
                            )  ###################################################

                            step_type = self.mash if self.mash != "" else "MashStep"
                            Notification = ""
                        else:
                            step_type = self.mash if self.mash != "" else "MashStep"
                            Notification = ""
                            # Only the first rest can be the mash-in: a later one of 0 minutes stays a rest.
                            MashIn_Flag = False

                    else:
                        step_type = self.mash if self.mash != "" else "MashStep"
                        Notification = ""

                    try:
                        if float(step_temp) < float(step_temp_old):
                            step_string = {
                                "name": "Temp reduction!",
                                "props": {
                                    "AutoNext": "No",
                                    "Kettle": self.id,
                                    "Notification": f"Temperature reduction from {step_temp_old}  {self.TEMP_UNIT} to {step_temp} {self.TEMP_UNIT}. Please reduce the temperature manually.",
                                },
                                "status_text": "",
                                "status": "I",
                                "type": "NotificationStep",
                            }

                            await self.create_step(step_string)
                    except Exception as e:
                        logging.error(e)

                    step_string = {
                        "name": step_name,
                        "props": {
                            "AutoMode": self.AutoMode,
                            "Kettle": self.id,
                            "Sensor": self.kettle.sensor,
                            "Temp": step_temp,
                            "Timer": step_timer,
                            "Notification": Notification,
                        },
                        "status_text": "",
                        "status": "I",
                        "type": step_type,
                    }

                    await self.create_step(step_string)
                    step_temp_old = step_temp
                # MashOut -> mashStep to reach mashout-temp for 1 min
                if last_step_temp != e["Abmaischtemperatur"]:
                    step_string = {
                        "name": "MashOut",
                        "props": {
                            "AutoMode": self.AutoMode,
                            "Kettle": self.id,
                            "Sensor": self.kettle.sensor,
                            "Temp": e["Abmaischtemperatur"],
                            "Timer": 1,
                            "Notification": "",
                        },
                        "status_text": "",
                        "status": "I",
                        "type": "MashStep",
                    }

                    await self.create_step(step_string)
                # Lautering -> Simple step that sends notification and waits for user input to move to next step (AutoNext=No)
                if self.mashout == "NotificationStep":
                    step_string = {
                        "name": "Lautering",
                        "props": {
                            "AutoNext": "No",
                            "Kettle": self.id,
                            "Notification": "Mash Process completed. Please start lautering and press next to start boil.",
                        },
                        "status_text": "",
                        "status": "I",
                        "type": self.mashout,
                    }
                    await self.create_step(step_string)

                # Measure Original Gravity -> Simple step that sends notification
                step_string = {
                    "name": "Measure Original Gravity",
                    "props": {
                        "AutoNext": "No",
                        "Kettle": self.id,
                        "Notification": "What is the original gravity of the beer wort?",
                    },
                    "status_text": "",
                    "status": "I",
                    "type": "NotificationStep",
                }
                await self.create_step(step_string)

                # Boil step including hop alarms and alarm for first wort hops -> Automode is set tu yes
                Hops, Whirlpool = self.getBoilAlerts(hops, miscs, "json")
                step_kettle = self.boilid
                step_type = self.boil if self.boil != "" else "BoilStep"
                step_time = str(int(boil_time))
                step_temp = self.BoilTemp
                sensor = self.boilkettle.sensor
                LidAlert = "Yes"

                logging.info(
                    step_temp
                )  ###################################################

                step_string = {
                    "name": "Boil Step",
                    "props": {
                        "AutoMode": self.AutoMode,
                        "Kettle": step_kettle,
                        "Sensor": sensor,
                        "Temp": step_temp,
                        "Timer": step_time,
                        "First_Wort": FirstWort[0],
                        "First_Wort_text": FirstWort[1],
                        "LidAlert": LidAlert,
                        "Hop_1": Hops[0][0],
                        "Hop_1_text": Hops[0][1],
                        "Hop_2": Hops[1][0],
                        "Hop_2_text": Hops[1][1],
                        "Hop_3": Hops[2][0],
                        "Hop_3_text": Hops[2][1],
                        "Hop_4": Hops[3][0],
                        "Hop_4_text": Hops[3][1],
                        "Hop_5": Hops[4][0],
                        "Hop_5_text": Hops[4][1],
                        "Hop_6": Hops[5][0],
                        "Hop_6_text": Hops[5][1],
                    },
                    "status_text": "",
                    "status": "I",
                    "type": step_type,
                }

                await self.create_step(step_string)

                # Measure Original Gravity -> Simple step that sends notification
                step_string = {
                    "name": "Measure Original Gravity",
                    "props": {
                        "AutoNext": "No",
                        "Kettle": self.id,
                        "Notification": "What is the original gravity of the beer wort?",
                    },
                    "status_text": "",
                    "status": "I",
                    "type": "NotificationStep",
                }
                await self.create_step(step_string)

                                # whirlpool hops are added at the end of the boil step
                # and the kettle is cooled down to the whirlpool temperature
                # the whirlpool temperature is set in the recipe and is used to cool down the kettle
                
                if whirlpool_hops != []:

                    step_temp = 80 if self.TEMP_UNIT == "C" else 176

                    step_type = self.cooldown
                    step_name = "CoolDown for Whirlpool Hop"
                    cooldown_sensor = ""
                    step_timer = ""

                    if step_type.find("Cooldown") != -1 and self.cooldown != "":
                        cooldown_sensor = (
                                self.boilkettle.sensor
                            )  # fall back to boilkettle sensor if no other sensor is specified
                        step_string = {
                            "name": "Cooldown for Whirlpool Hop",
                            "props": {
                                "Kettle": self.boilid,
                                "Timer": step_timer,
                                "Temp": step_temp,
                                "Sensor": cooldown_sensor,
                                "Actor": self.CoolDownActor,
                            },
                            "status_text": "",
                            "status": "I",
                            "type": step_type,
                        }
                        await self.create_step(step_string)

                    if self.cooldown.find("Cooldown") != -1:
                        notification = "Target Whirlpool temperature reached. Please add Whirlpool hops." 
                        autonext = "No"
                    else: 
                        notification= f"Cool down to {step_temp} {self.TEMP_UNIT}. Then add Whirlpool hops."
                        autonext = "No"
                    step_string = {
                        "name": "Whirlpool Hop",
                        "props": {
                            "AutoNext": autonext,
                            "Kettle": self.id,
                            "Notification": notification,
                        },
                        "status_text": "",
                        "status": "I",
                        "type": "NotificationStep",
                    }
                    await self.create_step(step_string)



                await self.create_Whirlpool_Cooldown(
                    temp=step_temp if whirlpool_hops != [] else None)

                self._creation_done("MMuM-JSON Recipe created ", name)
            else:
                self._creation_failed(
                    "Recipe Upload",
                    "No default Kettle defined. Please specify default Kettle in settings",
                )
        except Exception as e:
            self._creation_failed("MMuM-JSON Recipe creation failure", e, name)

    @staticmethod
    def _xml_root(content):
        """The BeerXML bytes `content`, parsed: as sent, in the encoding they declare, unless beerxml.read reads them
        otherwise (a declared ISO-8859-1 with a byte from 0x80 to 0x9F, read as Windows-1252) or they don't parse
        that way; then as beerxml.read reads them, which the log says. An upload is kept so that it parses as sent
        (_kept_bytes); a beer.xml an older CraftBeerPi kept may not."""
        as_declared = beerxml.read_as_declared(content)
        if as_declared:
            try:
                return xml.etree.ElementTree.fromstring(content)
            except Exception:  # noqa: BLE001 - ParseError, or LookupError for an encoding Python doesn't know
                pass
        found = beerxml.read(content)
        windows_1252 = not as_declared and found.encoding == "cp1252" and not found.plain
        (logging.info if windows_1252 else logging.warning)(
            "BeerXML: the uploaded file read as %s%s", found.encoding,
            ", made plain English letters" if found.plain else "")
        return found.root

    def _xml_file_root(self, path):
        with open(path, "rb") as file:
            return self._xml_root(file.read())

    def _kept_recipe(self, path, recipe_id, name):
        """The BeerXML file the Recipe Book keeps for the recipe being imported: the RECIPE `recipe_id` (its place
        among the file's RECIPEs, from 1) of the uploaded file at `path`, named `name`. (bytes, the NAME in them), or
        None, with the reason in the log, when it is to have none.

        Only that RECIPE, in a RECIPES of its own, as the upload has it: an export can hold many recipes, and a
        plugin reads the file whole. The upload is read in the encoding it declares - BeerSmith's says ISO-8859-1,
        and writes a degree sign or the o of Kölsch as one byte, which isn't UTF-8 - and the kept file is written
        as UTF-8 under a UTF-8 declaration. ElementTree writes no DOCTYPE, ENTITY, comment or CDATA, so an
        upload's DOCTYPE is gone and the entities it declared are written out. The NAME given back is the one the
        kept file has, exactly. None when the recipe alone would be more than KEPT_XML_MAX_BYTES, or when the kept
        file, read back, wouldn't give a recipe of that name (beerxml.name_key).
        """
        def none(why):
            logging.warning("BeerXML import: %s is kept in the Recipe Book without a BeerXML file of its own: %s",
                            name, why)
            return None

        try:
            with open(path, "rb") as file:
                content = file.read()
            recipe = self._xml_root(content).find("./RECIPE[%s]" % recipe_id)
            if recipe is None or beerxml.name_key(recipe.findtext("NAME")) != beerxml.name_key(name):
                return none("recipe {} of the uploaded file isn't it".format(recipe_id))
            recipe.tail = None
            kept = (self.KEPT_XML_HEAD + xml.etree.ElementTree.tostring(recipe, encoding="unicode").encode("utf-8")
                    + self.KEPT_XML_TAIL)
            if len(kept) > self.KEPT_XML_MAX_BYTES:
                return none("the recipe alone is {} bytes, more than {}".format(len(kept), self.KEPT_XML_MAX_BYTES))
            again = xml.etree.ElementTree.fromstring(kept).find("RECIPE")
            found = None if again is None else again.findtext("NAME")
            if not found or beerxml.name_key(found) != beerxml.name_key(name):
                return none("its file, read back, names the recipe {!r}".format(found))
        except Exception as e:  # noqa: BLE001 - the recipe is imported all the same
            return none("its file could not be made: {}".format(e))
        return kept, found

    async def xml_recipe_creation(self, Recipe_ID):
        config = self.get_config_values()
        name = ""
        try:
            if self.kettle is not None:
                # load beerxml file located in upload folder
                self.path = self.cbpi.config_folder.get_upload_file("beer.xml")
                if os.path.exists(self.path) is False:
                    self.cbpi.notify(
                        "File Not Found",
                        "Please upload a Beer.xml File",
                        NotificationType.ERROR,
                    )

                e = self._xml_file_root(self.path)
                recipe = e.find("./RECIPE[%s]" % (str(Recipe_ID)))
                hops = recipe.findall("./HOPS/HOP")
                miscs = recipe.findall('MISCS/MISC[USE="Boil"]')
                name = e.find("./RECIPE[%s]/NAME" % (str(Recipe_ID))).text
                boil_time = float(
                    e.find("./RECIPE[%s]/BOIL_TIME" % (str(Recipe_ID))).text
                )
                grain_kg = self.getGrainMass(recipe)
                FirstWort = self.getFirstWort(hops, "xml")

                await self.create_recipe(name)
                if self._staging and self._staged is not None:
                    self._staged["mash_numbers"] = self.getMashNumbers(recipe)
                    self._staged["source_xml"] = self._kept_recipe(self.path, Recipe_ID, name)
                    mash_additions, sparge_additions, not_placed = self.getMashSpargeAdditions(recipe)
                    self._staged[StepController.MASH_ADDITIONS_KEY] = mash_additions
                    self._staged[StepController.SPARGE_ADDITIONS_KEY] = sparge_additions
                    self._staged[StepController.MASH_PH_KEY] = self.getMashPh(recipe)
                    self._staged[StepController.YEASTS_KEY] = self.getYeasts(recipe)
                    self._staged["not_placed"] = not_placed
                # Mash Steps -> first step is different as it heats up to defined temp and stops with notification to add malt
                # AutoMode is yes to start and stop automatic mode or each step
                MashIn_Flag = True
                step_kettle = self.id
                step_temp_old = 0
                for row in self.getSteps(Recipe_ID, "xml"):
                    step_name = str(row.get("name"))
                    step_timer = str(int(row.get("timer")))
                    # Rounded to a tenth, not cut to a whole degree: a 152.5 F
                    # rest would come in as 152, and on a Celsius server a
                    # 152 F (66.67 C) rest as 66 C, 150.8 F. Whole numbers stay
                    # as they were: "152".
                    step_temp = self._format_temp(row.get("temp"))
                    sensor = self.kettle.sensor
                    if MashIn_Flag == True:
                        if row.get("timer") == 0:
                            step_type = (
                                self.mashin if self.mashin != "" else "MashInStep"
                            )
                            Notification = (
                                "Target temperature reached. Please add malt."
                            )
                            MashIn_Flag = False
                            if step_name is None or step_name == "":
                                step_name = "MashIn"
                        elif self.addmashin == "Yes":
                            step_type = (
                                self.mashin if self.mashin != "" else "MashInStep"
                            )
                            Notification = (
                                "Target temperature reached. Please add malt."
                            )
                            MashIn_Flag = False
                            step_string = {
                                "name": "MashIn",
                                "props": {
                                    "AutoMode": self.AutoMode,
                                    "Kettle": self.id,
                                    "Sensor": self.kettle.sensor,
                                    "Temp": step_temp,
                                    "Timer": 0,
                                    "Notification": Notification,
                                    # Carried from the recipe so the step can
                                    # work out a strike temperature. It still
                                    # needs an ambient sensor and a mash water
                                    # volume, which are rig properties rather
                                    # than recipe ones, and without those it
                                    # heats to Temp exactly as before.
                                    "Grain_Kg": grain_kg,
                                },
                                "status_text": "",
                                "status": "I",
                                "type": step_type,
                            }
                            await self.create_step(step_string)

                            step_type = self.mash if self.mash != "" else "MashStep"
                            Notification = ""
                        else:
                            step_type = self.mash if self.mash != "" else "MashStep"
                            Notification = ""
                            # Only the first step can be the mash-in: a later one of 0 minutes, such as BeerSmith's
                            # Mash Out, is a rest, never a question for the malt.
                            MashIn_Flag = False

                    else:
                        step_type = self.mash if self.mash != "" else "MashStep"
                        Notification = ""

                    try:
                        if float(step_temp) < float(step_temp_old):
                            step_string = {
                                "name": "Temp reduction!",
                                "props": {
                                    "AutoNext": "No",
                                    "Kettle": self.id,
                                    "Notification": f"Temperature reduction from {step_temp_old}  {self.TEMP_UNIT} to {step_temp} {self.TEMP_UNIT}. Please reduce the temperature manually.",
                                },
                                "status_text": "",
                                "status": "I",
                                "type": "NotificationStep",
                            }

                            await self.create_step(step_string)
                    except Exception as e:
                        logging.error(e)

                    step_string = {
                        "name": step_name,
                        "props": {
                            "AutoMode": self.AutoMode,
                            "Kettle": self.id,
                            "Sensor": self.kettle.sensor,
                            "Temp": step_temp,
                            "Timer": step_timer,
                            "Notification": Notification,
                        },
                        "status_text": "",
                        "status": "I",
                        "type": step_type,
                    }

                    await self.create_step(step_string)
                    step_temp_old = step_temp

                # MashOut -> Simple step that sends notification and waits for user input to move to next step (AutoNext=No)
                if self.mashout == "NotificationStep":
                    step_string = {
                        "name": "Lautering",
                        "props": {
                            "AutoNext": "No",
                            "Kettle": self.id,
                            "Notification": "Mash Process completed. Please start lautering and press next to start boil.",
                        },
                        "status_text": "",
                        "status": "I",
                        "type": self.mashout,
                    }
                    await self.create_step(step_string)

                # Boil step including hop alarms and alarm for first wort hops -> Automode is set tu yes
                Hops , Whirlpool = self.getBoilAlerts(hops, miscs, "xml")
                whirlpool_additions = self.getWhirlpoolAdditions(hops)
                step_kettle = self.boilid
                step_type = self.boil if self.boil != "" else "BoilStep"
                step_time = str(int(boil_time))
                step_temp = self.BoilTemp
                sensor = self.boilkettle.sensor
                LidAlert = "Yes"

                step_string = {
                    "name": "Boil Step",
                    "props": {
                        "AutoMode": self.AutoMode,
                        "Kettle": step_kettle,
                        "Sensor": sensor,
                        "Temp": step_temp,
                        "Timer": step_time,
                        "First_Wort": FirstWort[0],
                        "First_Wort_text": FirstWort[1],
                        "LidAlert": LidAlert,
                        "Hop_1": Hops[0][0],
                        "Hop_1_text": Hops[0][1],
                        "Hop_2": Hops[1][0],
                        "Hop_2_text": Hops[1][1],
                        "Hop_3": Hops[2][0],
                        "Hop_3_text": Hops[2][1],
                        "Hop_4": Hops[3][0],
                        "Hop_4_text": Hops[3][1],
                        "Hop_5": Hops[4][0],
                        "Hop_5_text": Hops[4][1],
                        "Hop_6": Hops[5][0],
                        "Hop_6_text": Hops[5][1],
                    },
                    "status_text": "",
                    "status": "I",
                    "type": step_type,
                }

                await self.create_step(step_string)

                # whirlpool hops are added at the end of the boil step
                # and the kettle is cooled down to the whirlpool temperature
                # the whirlpool temperature is set in the recipe and is used to cool down the kettle
                
                if Whirlpool != []:
                    logging.info(
                        "Whirlpool Temp: {}".format(Whirlpool)
                    )

                    step_type = self.cooldown
                    step_name = "CoolDown for Whirlpool Hop"
                    cooldown_sensor = ""
                    step_temp = float(Whirlpool)
                    step_timer = ""

                    if step_type.find("Cooldown") != -1 and self.cooldown != "":
                        cooldown_sensor = (
                                self.boilkettle.sensor
                            )  # fall back to boilkettle sensor if no other sensor is specified
                        step_string = {
                            "name": "Cooldown for Whirlpool Hop",
                            "props": {
                                "Kettle": self.boilid,
                                "Timer": step_timer,
                                "Temp": step_temp,
                                "Sensor": cooldown_sensor,
                                "Actor": self.CoolDownActor,
                            },
                            "status_text": "",
                            "status": "I",
                            "type": step_type,
                        }
                        await self.create_step(step_string)

                    hops_text = self._whirlpool_hops_text(whirlpool_additions)
                    if self.cooldown.find("Cooldown") != -1:
                        notification = "Target whirlpool temperature reached. Please add {}.".format(hops_text)
                        autonext = "No"
                    else:
                        notification = "Cool down to {} {}. Then add {}.".format(
                            self._format_temp(step_temp), self.TEMP_UNIT, hops_text)
                        autonext = "No"
                    step_string = {
                        "name": "Whirlpool Hop",
                        "props": {
                            "AutoNext": autonext,
                            # The boil kettle holds the wort. The step only
                            # notifies, so this is what the profile shows it on.
                            "Kettle": self.boilid,
                            "Notification": notification,
                        },
                        "status_text": "",
                        "status": "I",
                        "type": "NotificationStep",
                    }
                    await self.create_step(step_string)

                # The stand runs for the recipe's longest steep time, 15 minutes
                # when it gives none.
                await self.create_Whirlpool_Cooldown(
                    str(self._whirlpool_minutes(whirlpool_additions)),
                    temp=step_temp if Whirlpool != [] else None,
                )

                self._creation_done("BeerXML Recipe created ", name)
            else:
                self._creation_failed(
                    "Recipe Upload",
                    "No default Kettle defined. Please specify default Kettle in settings",
                )
        except Exception as e:
            self._creation_failed("BeerXML Recipe creation failure", e, name)

    # XML functions to retrieve xml repice parameters (if multiple recipes are stored in one xml file, id could be used)
    def getSteps(self, id, recipe_type):
        steps = []
        if recipe_type == "xml":
            e = self._xml_file_root(self.path)
            for e in e.findall("./RECIPE[%s]/MASH/MASH_STEPS/MASH_STEP" % (str(id))):
                if self.cbpi.config.get("TEMP_UNIT", "C") == "C":
                    temp = float(e.find("STEP_TEMP").text)
                else:
                    temp = round(9.0 / 5.0 * float(e.find("STEP_TEMP").text) + 32, 2)
                steps.append(
                    {
                        "name": e.find("NAME").text,
                        "temp": temp,
                        "timer": float(e.find("STEP_TIME").text),
                    }
                )
        elif recipe_type == "json":
            self.path = self.cbpi.config_folder.get_upload_file("mmum.json")
            with open(self.path, encoding="utf-8-sig") as f:
                e = json.load(f)
            Rasten= e["Rasten"]
            idx = 1
            for Rast in Rasten:
                if self.cbpi.config.get("TEMP_UNIT", "C") == "C":
                    temp = float(Rast["Temperatur"])
                else:
                    temp = round(
                        9.0 / 5.0 * float(Rast["Temperatur"])
                        + 32,
                        2,
                    )

                time= float(Rast["Zeit"])
                steps.append(
                    {
                        "name": "Rast {}".format(idx),
                        "temp": temp,
                        "timer": time,
                    }
                )
                idx += 1
            logging.info(steps)
        return steps

    async def bf_recipe_creation(self, Recipe_ID):
        config = self.get_config_values()

        if self.kettle is not None:

            brewfather = True
            result = []
            bf_recipe = ""
            self.bf_url = "https://api.brewfather.app/v2/recipes/" + Recipe_ID
            brewfather_user_id = self.cbpi.config.get("brewfather_user_id", None)
            if brewfather_user_id == "" or brewfather_user_id is None:
                brewfather = False

            brewfather_api_key = self.cbpi.config.get("brewfather_api_key", None)
            if brewfather_api_key == "" or brewfather_api_key is None:
                brewfather = False

            if brewfather == True:
                encodedData = base64.b64encode(
                    bytes(f"{brewfather_user_id}:{brewfather_api_key}", "ISO-8859-1")
                ).decode("ascii")
                headers = {"Authorization": "Basic %s" % encodedData}
                bf_recipe = ""

                async with aiohttp.ClientSession(
                    headers=headers, timeout=BREWFATHER_TIMEOUT
                ) as bf_session:
                    async with bf_session.get(self.bf_url) as r:
                        bf_recipe = await r.json()
                    await bf_session.close()

            if bf_recipe != "":
                try:
                    StrikeTemp = bf_recipe["data"]["strikeTemp"]
                except:
                    StrikeTemp = None
                # BF is sending all Temeprature values in °C. If system is running in F, values need to be converted
                if StrikeTemp is not None and self.TEMP_UNIT != "C":
                    StrikeTemp = round((9.0 / 5.0 * float(StrikeTemp) + 32))

                RecipeName = bf_recipe["name"]
                BoilTime = bf_recipe["boilTime"]
                mash_steps = bf_recipe["mash"]["steps"]
                hops = bf_recipe["hops"]

                try:
                    miscs = bf_recipe["miscs"]
                except:
                    miscs = None

                try:
                    fermentation_steps = bf_recipe["fermentation"]["steps"]
                except:
                    fermentation_steps = None

                if fermentation_steps is not None:
                    try:
                        step = fermentation_steps[0]
                        self.fermentation_step_temp = int(step["stepTemp"])
                    except:
                        self.fermentation_step_temp = None

                if self.fermentation_step_temp is not None and self.TEMP_UNIT != "C":
                    self.fermentation_step_temp = round(
                        (9.0 / 5.0 * float(self.fermentation_step_temp) + 32)
                    )

                FirstWort = self.getFirstWort(hops, "bf")

                await self.create_recipe(RecipeName)

                # Mash Steps -> first step is different as it heats up to defined temp and stops with notification to add malt
                # AutoMode is yes to start and stop automatic mode or each step
                MashIn_Flag = True
                step_kettle = self.id
                step_temp_old = 0
                for step in mash_steps:
                    try:
                        step_name = step["name"]
                        if step_name == "":
                            step_name = "MashStep"
                    except:
                        step_name = "MashStep"

                    step_timer = str(int(step["stepTime"]))

                    if self.TEMP_UNIT == "C":
                        step_temp = str(int(step["stepTemp"]))
                    else:
                        step_temp = str(round((9.0 / 5.0 * int(step["stepTemp"]) + 32)))

                    sensor = self.kettle.sensor
                    if MashIn_Flag == True:

                        if int(step_timer) == 0:
                            step_type = (
                                self.mashin if self.mashin != "" else "MashInStep"
                            )
                            Notification = (
                                "Target temperature reached. Please add malt."
                            )
                            MashIn_Flag = False

                        elif self.addmashin == "Yes":
                            mashin_temp = (
                                str(round(StrikeTemp))
                                if StrikeTemp is not None
                                else step_temp
                            )
                            step_type = (
                                self.mashin if self.mashin != "" else "MashInStep"
                            )
                            Notification = (
                                "Target temperature reached. Please add malt."
                            )
                            MashIn_Flag = False
                            step_string = {
                                "name": "MashIn",
                                "props": {
                                    "AutoMode": self.AutoMode,
                                    "Kettle": self.id,
                                    "Sensor": self.kettle.sensor,
                                    "Temp": mashin_temp,
                                    "Timer": 0,
                                    "Notification": Notification,
                                },
                                "status_text": "",
                                "status": "I",
                                "type": step_type,
                            }
                            await self.create_step(step_string)

                            step_type = self.mash if self.mash != "" else "MashStep"
                            Notification = ""
                        else:
                            step_type = self.mash if self.mash != "" else "MashStep"
                            Notification = ""
                            # Only the first mash step can be the mash-in: a later one of 0 minutes, such as a Mash
                            # Out, is a rest.
                            MashIn_Flag = False

                    else:
                        step_type = self.mash if self.mash != "" else "MashStep"
                        Notification = ""
                    try:
                        if float(step_temp) < float(step_temp_old):
                            step_string = {
                                "name": "Temp reduction!",
                                "props": {
                                    "AutoNext": "No",
                                    "Kettle": self.id,
                                    "Notification": f"Temperature reduction from {step_temp_old}  {self.TEMP_UNIT} to {step_temp} {self.TEMP_UNIT}. Please reduce the temperature manually.",
                                },
                                "status_text": "",
                                "status": "I",
                                "type": "NotificationStep",
                            }

                            await self.create_step(step_string)
                    except Exception as e:
                        logging.error(e)

                    step_string = {
                        "name": step_name,
                        "props": {
                            "AutoMode": self.AutoMode,
                            "Kettle": self.id,
                            "Sensor": self.kettle.sensor,
                            "Temp": step_temp,
                            "Timer": step_timer,
                            "Notification": Notification,
                        },
                        "status_text": "",
                        "status": "I",
                        "type": step_type,
                    }

                    await self.create_step(step_string)
                    step_temp_old = step_temp


                # MashOut -> Simple step that sends notification and waits for user input to move to next step (AutoNext=No)

                if self.mashout == "NotificationStep":
                    step_string = {
                        "name": "Lautering",
                        "props": {
                            "AutoNext": "No",
                            "Kettle": self.id,
                            "Notification": "Mash Process completed. Please start lautering and press next to start boil.",
                        },
                        "status_text": "",
                        "status": "I",
                        "type": self.mashout,
                    }
                    await self.create_step(step_string)

                # Boil step including hop alarms and alarm for first wort hops -> Automode is set tu yes
                Hops, Whirlpool = self.getBoilAlerts(hops, miscs, "bf")

                step_kettle = self.boilid
                step_time = str(int(BoilTime))
                step_type = self.boil if self.boil != "" else "BoilStep"
                step_temp = self.BoilTemp
                sensor = self.boilkettle.sensor
                LidAlert = "Yes"

                step_string = {
                    "name": "Boil Step",
                    "props": {
                        "AutoMode": self.AutoMode,
                        "Kettle": step_kettle,
                        "Sensor": sensor,
                        "Temp": step_temp,
                        "Timer": step_time,
                        "First_Wort": FirstWort[0],
                        "First_Wort_text": FirstWort[1],
                        "LidAlert": LidAlert,
                        "Hop_1": Hops[0][0],
                        "Hop_1_text": Hops[0][1],
                        "Hop_2": Hops[1][0],
                        "Hop_2_text": Hops[1][1],
                        "Hop_3": Hops[2][0],
                        "Hop_3_text": Hops[2][1],
                        "Hop_4": Hops[3][0],
                        "Hop_4_text": Hops[3][1],
                        "Hop_5": Hops[4][0],
                        "Hop_5_text": Hops[4][1],
                        "Hop_6": Hops[5][0],
                        "Hop_6_text": Hops[5][1],
                    },
                    "status_text": "",
                    "status": "I",
                    "type": step_type,
                }

                await self.create_step(step_string)

                # whirlpool hops are added at the end of the boil step
                # and the kettle is cooled down to the whirlpool temperature
                # the whirlpool temperature is set in the recipe and is used to cool down the kettle
                
                if Whirlpool != []:
                    logging.info(
                        "Whirlpool Temp: {}".format(Whirlpool)
                    )

                    step_type = self.cooldown
                    step_name = "CoolDown for Whirlpool Hop"
                    cooldown_sensor = ""
                    step_temp = float(Whirlpool)
                    step_timer = ""

                    if step_type.find("Cooldown") != -1 and self.cooldown != "":
                        cooldown_sensor = (
                                self.boilkettle.sensor
                            )  # fall back to boilkettle sensor if no other sensor is specified
                        step_string = {
                            "name": "Cooldown for Whirlpool Hop",
                            "props": {
                                "Kettle": self.boilid,
                                "Timer": step_timer,
                                "Temp": step_temp,
                                "Sensor": cooldown_sensor,
                                "Actor": self.CoolDownActor,
                            },
                            "status_text": "",
                            "status": "I",
                            "type": step_type,
                        }
                        await self.create_step(step_string)

                    if self.cooldown.find("Cooldown") != -1:
                        notification = "Target Whirlpool temperature reached. Please add Whirlpool hops." 
                        autonext = "No"
                    else: 
                        notification= f"Cool down to {step_temp} {self.TEMP_UNIT}. Then add Whirlpool hops."
                        autonext = "No"
                    step_string = {
                        "name": "Whirlpool Hop",
                        "props": {
                            "AutoNext": autonext,
                            "Kettle": self.id,
                            "Notification": notification,
                        },
                        "status_text": "",
                        "status": "I",
                        "type": "NotificationStep",
                    }
                    await self.create_step(step_string)


                await self.create_Whirlpool_Cooldown(
                    temp=step_temp if Whirlpool != [] else None)

                self._creation_done("Brewfather App Recipe created: ", RecipeName)
            else:
                self._creation_failed(
                    "Brewfather",
                    "no recipe came back - check brewfather_user_id and "
                    "brewfather_api_key in Settings",
                )
        else:
            self._creation_failed(
                "Recipe Upload",
                "No default Kettle defined. Please specify default Kettle in settings",
            )

    def getBoilAlerts(self, hops, miscs, recipe_type):
        alerts = []
        whirlpool = []
        for hop in hops:
            if recipe_type == "xml":
                use = hop.find("USE").text
                ## Hops which are not used in the boil step should not cause alerts
                if use == "Boil":
                    alerts.append([float(hop.find("TIME").text), self._xml_addition_label(hop)])
                elif use == "Aroma":
                    try:
                        if self.TEMP_UNIT == "C":
                            temp = float(hop.find("TEMP").text)
                        else:
                            temp = round(9.0 / 5.0 * float(hop.find("TEMP").text) + 32, 2)
                    except:
                        temp = self._whirlpool_default()
                    whirlpool.append([temp, hop.find("NAME").text])

            elif recipe_type == "bf":
                use = hop["use"]
                if use == "Boil":
                    alerts.append([float(hop["time"]), hop["name"]])  ## TODO: Testing
                elif use == "Aroma":
                    try:
                        if self.TEMP_UNIT == "C":
                            temp = float(hop["temp"])
                        else:
                            temp = round(9.0 / 5.0 * float(hop["temp"]) + 32, 2)
                    except:
                        temp = self._whirlpool_default()
                    whirlpool.append([temp, hop["name"]])

            elif recipe_type == "kbh":
                if hop[2] != 5:
                    alerts.append([float(hop[0]), hop[1]])
                if hop[2] == 5:
                    temp = self._whirlpool_default()
                    whirlpool.append([temp, hop[1]])

            elif recipe_type == "json":
                alerts.append([float(hop["time"]), hop["name"]])



        ## There might also be miscelaneous additions during boild time
        if miscs is not None:
            for misc in miscs:
                if recipe_type == "xml":
                    alerts.append(
                        [float(misc.find("TIME").text), self._xml_misc_label(misc)]
                    )
                elif recipe_type == "bf":
                    use = misc["use"]
                    if use != "Aroma" and use != "Boil":
                        continue
                    alerts.append([float(misc["time"]), misc["name"]])  ## TODO: Testing
                elif recipe_type == "kbh":
                    alerts.append([float(misc[0]), misc[1]])
                elif recipe_type == "json":
                    alerts.append([float(misc["time"]), misc["name"]])
        # The additions the boil step can remind of, longest first. It holds
        # BOIL_REMINDERS of them; any more are named in a warning instead.
        alerts = [[m, label] for m, label in
                  ((self._reminder_minutes(a[0]), a[1]) for a in alerts) if m is not None]
        alerts = sorted(alerts, key=lambda x: x[0], reverse=True)
        hop_alerts = [[None, None] for _ in range(self.BOIL_REMINDERS)]
        for i, alert in enumerate(alerts[:self.BOIL_REMINDERS]):
            hop_alerts[i] = alert
        if len(alerts) > self.BOIL_REMINDERS:
            self._import_warning("Boil additions without a reminder",
                                 self._unreminded_text(alerts))
            self._keep_unreminded(alerts[self.BOIL_REMINDERS:])
        whirlpool_temps = sorted(whirlpool, key=lambda x: x[0], reverse=True)
        logging.info("Whirlpool Hops: {}".format(whirlpool_temps))
        try:
            whirlpool_temp = whirlpool_temps[0][0]
        except:
            whirlpool_temp = []
        logging.info("Whirlpool Temp: {}".format(whirlpool_temp))
        return hop_alerts, whirlpool_temp

    def _xml_amount(self, element):
        """How much of a BeerXML addition, in the brewer's units, or None.

        BeerSmith writes the amount as it displays it (DISPLAY_AMOUNT, "6.00 oz"),
        which is used as written. Otherwise AMOUNT is kilograms, by the BeerXML
        definition: shown in ounces on a Fahrenheit system and grams on a Celsius
        one, the nearest thing here to a unit preference for weights.
        """
        shown = element.find("DISPLAY_AMOUNT")
        if shown is not None and shown.text and shown.text.strip():
            return shown.text.strip()
        try:
            kg = float(element.find("AMOUNT").text)
        except (AttributeError, TypeError, ValueError):
            return None
        if not math.isfinite(kg) or kg <= 0:
            return None
        if self.TEMP_UNIT == "F":
            return "{:.2f} oz".format(kg * 35.27396195)
        grams = kg * 1000.0
        return "{:.0f} g".format(grams) if grams >= 10 else "{:.1f} g".format(grams)

    def _xml_addition_label(self, element):
        """'6.00 oz Willamette': what a hop prompt names, amount first.

        On brew day, with the recipe sheet somewhere else, the amount is the
        part you need.
        """
        name = element.find("NAME").text
        amount = self._xml_amount(element)
        return "{} {}".format(amount, name) if amount else name

    #: Units that only count things. "1.00 Items Whirlfloc Tablet" reads
    #: badly; "1 Whirlfloc Tablet" is what the brewer would say.
    _COUNT_UNITS = ("item", "items", "each", "ea")

    def _xml_misc_amount(self, misc):
        """How much of a BeerXML misc addition, or None if the file does not say.

        DISPLAY_AMOUNT as written ("1.00 tsp"), but a count as a plain number:
        BeerSmith writes a Whirlfloc tablet as "1.00 Items", Brewfather as
        "0.5 items". Without DISPLAY_AMOUNT, only a weight is used. AMOUNT is
        otherwise litres, and BeerXML has no unit for a count, so 1 tablet and
        1 litre are written the same way - better the name alone, as before,
        than "1000 ml Whirlfloc".
        """
        shown = misc.find("DISPLAY_AMOUNT")
        if shown is not None and shown.text and shown.text.strip():
            text = shown.text.strip()
            parts = text.split()
            if len(parts) == 2 and parts[1].lower() in self._COUNT_UNITS:
                try:
                    count = float(parts[0])
                except ValueError:
                    return text
                if math.isfinite(count) and count > 0:
                    return "{:g}".format(count)
            return text
        weight = misc.find("AMOUNT_IS_WEIGHT")
        if weight is None or (weight.text or "").strip().upper() != "TRUE":
            return None
        return self._xml_amount(misc)

    def _xml_misc_label(self, misc):
        """'1 Whirlfloc Tablet': a misc boil addition, amount first, like a hop.

        The prompt says how many, as a hop prompt says "6.00 oz Willamette".
        """
        name = misc.find("NAME").text
        amount = self._xml_misc_amount(misc)
        return "{} {}".format(amount, name) if amount else name

    def getWhirlpoolAdditions(self, hops):
        """BeerXML hops added after the boil: [(label, steep minutes), ...].

        "Aroma" is BeerXML's only word for a hop added after the boil, and it is
        what BeerSmith writes for its Steep/Whirlpool use, with the steep time
        in TIME. In recipe order.
        """
        additions = []
        for hop in hops:
            use = hop.find("USE")
            if use is None or use.text != "Aroma":
                continue
            try:
                minutes = float(hop.find("TIME").text)
            except (AttributeError, TypeError, ValueError):
                minutes = 0.0
            if not math.isfinite(minutes) or minutes < 0:
                minutes = 0.0
            additions.append((self._xml_addition_label(hop), minutes))
        return additions

    def _whirlpool_default(self):
        """Whirlpool temperature when the recipe gives none: the setting."""
        fallback = self.DEFAULT_WHIRLPOOL_TEMP.get(getattr(self, "TEMP_UNIT", "C"), 80)
        try:
            value = float(getattr(self, "WhirlpoolTemp", fallback))
        except (TypeError, ValueError):
            return fallback
        return value if math.isfinite(value) else fallback

    def _whirlpool_minutes(self, additions):
        """Length of the stand: the longest steep time, or the default."""
        longest = max((minutes for _, minutes in additions), default=0.0)
        if longest > 0:
            return max(1, int(round(longest)))
        return self.DEFAULT_WHIRLPOOL_MINUTES

    @staticmethod
    def _whirlpool_hops_text(additions):
        """What to add, and when: '1.50 oz Calypso', or with a second addition
        '1.50 oz Calypso now, then 1.00 oz Citra with 10 minutes left'.

        The stand runs as long as the longest steep time, so a hop with a
        shorter one goes in that many minutes before it ends.
        """
        if not additions:
            return "Whirlpool hops"
        ordered = sorted(additions, key=lambda a: -a[1])
        longest = ordered[0][1]
        first = [label for label, minutes in ordered if minutes >= longest]
        text = " and ".join(first)
        later = []
        for label, minutes in ordered:
            if minutes >= longest:
                continue
            whole = int(round(minutes))
            if whole > 0:
                later.append("{} with {} minute{} left".format(
                    label, whole, "" if whole == 1 else "s"))
            else:
                later.append("{} at the end".format(label))
        if later:
            text += " now, then " + ", then ".join(later)
        return text

    @staticmethod
    def _format_temp(value):
        """170.0 -> '170', 170.62 -> '170.6'."""
        text = "{:.1f}".format(float(value))
        return text[:-2] if text.endswith(".0") else text

    def getGrainMass(self, recipe):
        """Total grain bill in kg, from a BeerXML recipe element.

        The importer has always read hops, miscs and mash steps but never the
        fermentables, so the grain mass - which the recipe carries and which is
        exactly what a strike-temperature calculation needs - was discarded at
        import. Without it, MashIn heats to the rest temperature and the mash
        drops several degrees the moment the grain goes in.

        BeerXML AMOUNT is in kilograms. Only mashed fermentables are counted:
        sugars and liquid extracts dissolve rather than acting as a thermal mass
        in the tun, and including them would overstate the strike temperature.

        Returns 0 when there is nothing usable, which the step treats as "no
        strike calculation" rather than as a grain bill of zero.
        """
        total = 0.0
        mashed = ("grain", "adjunct", "dry extract")
        try:
            for f in recipe.findall("./FERMENTABLES/FERMENTABLE"):
                ftype = f.find("TYPE")
                if ftype is not None and ftype.text:
                    if ftype.text.strip().lower() not in mashed:
                        continue
                amount = f.find("AMOUNT")
                if amount is None or not amount.text:
                    continue
                try:
                    total += float(amount.text)
                except (TypeError, ValueError):
                    continue
        except Exception as e:
            logging.warning("Could not read fermentables for grain mass: %s", e)
            return 0.0
        return round(total, 3)

    #: Litres in each unit BeerSmith's display amounts use.
    _LITRES = {"qt": 0.946352946, "gal": 3.785411784, "l": 1.0, "ml": 0.001, "pt": 0.473176473,
               "floz": 0.0295735296, "oz": 0.0295735296}
    #: Kilograms in each unit its display weights use.
    _KILOGRAMS = {"lb": 0.45359237, "lbs": 0.45359237, "oz": 0.028349523125, "kg": 1.0, "g": 0.001}

    @staticmethod
    def _xml_number(element, path):
        if element is None:
            return None
        text = element.findtext(path)
        try:
            number = float(str(text).strip())
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @classmethod
    def _display_quantity(cls, text, units):
        """BeerSmith's display amount, such as "33.04 qt" or "33 lbs", in the unit `units` counts in; None if it
        can't be read."""
        parts = str(text or "").strip().split()
        if len(parts) != 2:
            return None
        try:
            number = float(parts[0])
        except ValueError:
            return None
        per = units.get(parts[1].lower().replace(" ", "").rstrip("."))
        return number * per if per is not None and math.isfinite(number) else None

    @staticmethod
    def _display_celsius(text):
        """A display temperature such as "134.6 F" or "57.0 C", in C; None if it can't be read."""
        parts = str(text or "").strip().split()
        try:
            number = float(parts[0])
        except (IndexError, ValueError):
            return None
        unit = parts[1].upper() if len(parts) > 1 else "C"
        if not math.isfinite(number):
            return None
        return (number - 32.0) / 1.8 if unit.startswith("F") else number

    def getMashNumbers(self, recipe):
        """What a strike water forecast, and the HLT's water for a new BrewStart, need from a BeerXML recipe, kept
        with the recipe in its basic data as mash_numbers, in SI units and C as BeerXML has them (StrikeController
        reads them):

        - grain_kg: the grain bill, as getGrainMass counts it, unrounded;
        - infuse_l: all the water of the first infusion as BeerSmith shows it (DISPLAY_INFUSE_AMT), the recoverable
          dead space it adds included; INFUSE_AMOUNT without it;
        - for_grain_l: INFUSE_AMOUNT, the part for the grain;
        - infusions_l: every infusion as BeerSmith shows it, added up: the strike water and any water added later,
          such as BeerSmith's at 168 F to mash out, whatever the step's TYPE;
        - boil_l: the volume before the boil (the recipe's BOIL_SIZE, else the equipment profile's);
        - top_up_kettle_l and lauter_dead_l: the equipment profile's TOP_UP_KETTLE and LAUTER_DEADSPACE, 0 kept;
        - tun_kg and tun_cal_g_c: the equipment profile's TUN_WEIGHT and TUN_SPECIFIC_HEAT, else the mash section's -
          its DISPLAY_TUN_WEIGHT, or its TUN_WEIGHT, which BeerSmith writes in ounces;
        - infuse_temp_c, grain_temp_c, tun_temp_c: BeerSmith's own strike (INFUSE_TEMP) and the grain and tun
          temperatures it was worked out for (GRAIN_TEMP, TUN_TEMP);
        - lacks: those the recipe doesn't have, of grain, water, tun, infuse_temp, grain_temp and tun_temp.

        A figure the file doesn't have is left out. Never raises: a recipe that can't be read lacks them all.
        """
        found = {}
        try:
            total, mashed = 0.0, ("grain", "adjunct", "dry extract")
            for f in recipe.findall("./FERMENTABLES/FERMENTABLE"):
                kind = (f.findtext("TYPE") or "").strip().lower()
                if kind and kind not in mashed:
                    continue
                amount = self._xml_number(f, "AMOUNT")
                if amount is not None:
                    total += amount
            if total > 0:
                found["grain_kg"] = round(total, 6)
            mash = recipe.find("MASH")
            infusion = None
            infusions = 0.0
            if mash is not None:
                infusion = next((s for s in mash.iter("MASH_STEP")
                                 if (s.findtext("TYPE") or "").strip().lower() == "infusion"), None)
                for step in mash.iter("MASH_STEP"):
                    added = (self._display_quantity(step.findtext("DISPLAY_INFUSE_AMT"), self._LITRES)
                             or self._xml_number(step, "INFUSE_AMOUNT"))
                    if added and added > 0:
                        infusions += added
            if infusions > 0:
                found["infusions_l"] = round(infusions, 6)
            if infusion is not None:
                for_grain = self._xml_number(infusion, "INFUSE_AMOUNT")
                shown = self._display_quantity(infusion.findtext("DISPLAY_INFUSE_AMT"), self._LITRES)
                water = shown if shown else for_grain
                if water and water > 0:
                    found["infuse_l"] = round(water, 6)
                if for_grain and for_grain > 0:
                    found["for_grain_l"] = round(for_grain, 6)
                infuse = self._display_celsius(infusion.findtext("INFUSE_TEMP"))
                if infuse is not None:
                    found["infuse_temp_c"] = round(infuse, 4)
            equipment = recipe.find("EQUIPMENT")
            boil = self._xml_number(recipe, "BOIL_SIZE") or self._xml_number(equipment, "BOIL_SIZE")
            if boil and boil > 0:
                found["boil_l"] = round(boil, 6)
            for key, path in (("top_up_kettle_l", "TOP_UP_KETTLE"), ("lauter_dead_l", "LAUTER_DEADSPACE")):
                value = self._xml_number(equipment, path)
                if value is not None and value >= 0:
                    found[key] = round(value, 6)
            tun = self._xml_number(equipment, "TUN_WEIGHT")
            if not tun and mash is not None:
                tun = self._display_quantity(mash.findtext("DISPLAY_TUN_WEIGHT"), self._KILOGRAMS)
                if not tun:
                    ounces = self._xml_number(mash, "TUN_WEIGHT")
                    tun = ounces * self._KILOGRAMS["oz"] if ounces else None
            heat = self._xml_number(equipment, "TUN_SPECIFIC_HEAT") or self._xml_number(mash, "TUN_SPECIFIC_HEAT")
            if tun and tun > 0 and heat and heat > 0:
                found["tun_kg"] = round(tun, 6)
                found["tun_cal_g_c"] = heat
            for key, path in (("grain_temp_c", "GRAIN_TEMP"), ("tun_temp_c", "TUN_TEMP")):
                value = self._xml_number(mash, path)
                if value is not None:
                    found[key] = round(value, 4)
        except Exception as e:  # noqa: BLE001 - the recipe still imports, without them
            logging.warning("Could not read the recipe's mash numbers: %s", e)
            found = {}
        names = (("grain", "grain_kg"), ("water", "infuse_l"), ("tun", "tun_kg"), ("infuse_temp", "infuse_temp_c"),
                 ("grain_temp", "grain_temp_c"), ("tun_temp", "tun_temp_c"))
        found["lacks"] = [name for name, key in names if key not in found]
        return found

    #: Where a BeerXML MISC goes, by its USE, read without regard to case: into the mash, or into the sparge water.
    #: "Boil" is the boil step's (getBoilAlerts); any other USE - Primary, Secondary, Bottling - has no step here.
    ADDITION_PLACES = {"mash": StepController.MASH_ADDITIONS_KEY, "sparge": StepController.SPARGE_ADDITIONS_KEY}

    def _addition_amount(self, misc):
        """How much of a mash or sparge MISC, as BeerSmith shows it (DISPLAY_AMOUNT, "5.00 g", "1.00 tsp", "2.00
        ml"), or None. Without DISPLAY_AMOUNT, from AMOUNT: a water agent in grams when a weight, else in ml, as
        AMOUNT is then litres; any other MISC by weight only, as _xml_misc_amount has it."""
        shown = (misc.findtext("DISPLAY_AMOUNT") or "").strip()
        if shown:
            return shown
        amount = self._xml_number(misc, "AMOUNT")
        if amount is None or amount <= 0:
            return None
        weight = (misc.findtext("AMOUNT_IS_WEIGHT") or "").strip().upper() == "TRUE"
        if (misc.findtext("TYPE") or "").strip().lower() == "water agent":
            return "{:.2f} {}".format(amount * 1000.0, "g" if weight else "ml")
        return self._xml_amount(misc) if weight else None

    def _addition(self, misc):
        """One MISC as the basic data keeps it: {"name", "type", "use", "amount", "label", "remind"}. The label is what
        a reminder names it by, amount first, a count as a plain number ("2 Campden Tablets"), as a boil addition's
        (_xml_misc_label); remind, whether the dough-in or the sparge's start does - a water salt or acid
        (StepController.is_water_treatment). None when the MISC has no name."""
        name = (misc.findtext("NAME") or "").strip()
        if not name:
            return None
        amount = self._addition_amount(misc)
        said = self._xml_misc_amount(misc) if (misc.findtext("DISPLAY_AMOUNT") or "").strip() else amount
        kind = (misc.findtext("TYPE") or "").strip() or None
        return {"name": name, "type": kind,
                "use": (misc.findtext("USE") or "").strip() or None, "amount": amount,
                "label": "{} {}".format(said, name) if said else name,
                StepController.REMIND_KEY: StepController.is_water_treatment(kind, name)}

    def getMashSpargeAdditions(self, recipe):
        """A BeerXML recipe's MISCs for the mash and for the sparge water - BeerSmith's water agents and acids (TYPE
        "Water Agent", USE "Mash" or "Sparge"), and anything else it adds there, rice hulls too, the reminders naming
        only the water salts and acids (remind) - in recipe order, and those no step here reminds of: ([mash],
        [sparge], [not placed]), each as _addition has it. The boil's ("Boil") are the boil step's, and in none of
        them. Never raises: a recipe that can't be read has none."""
        mash, sparge, not_placed = [], [], []
        try:
            for misc in recipe.findall("MISCS/MISC"):
                use = (misc.findtext("USE") or "").strip()
                if use == "Boil":
                    continue
                entry = self._addition(misc)
                if entry is None:
                    continue
                place = self.ADDITION_PLACES.get(use.lower())
                if place == StepController.MASH_ADDITIONS_KEY:
                    mash.append(entry)
                elif place == StepController.SPARGE_ADDITIONS_KEY:
                    sparge.append(entry)
                else:
                    not_placed.append(entry)
        except Exception as e:  # noqa: BLE001 - the recipe still imports, without them
            logging.warning("Could not read the recipe's mash and sparge additions: %s", e)
            return [], [], []
        return mash, sparge, not_placed

    def getMashPh(self, recipe):
        """BeerXML's MASH PH, {"value": 5.4, "from": "mash_profile"}, or None when the file has none from 0 to 14.

        BeerXML 1.0 calls it the "PH of the sparge" and no more. BeerSmith 2 writes there the pH of the recipe's mash
        profile (its F_MH_PH, 5.4 in its stock profiles), not the recipe's target mash pH (F_R_TARGET_PH) nor its
        estimate (F_R_MASH_PH), which no BeerXML export carries. So it is kept as the mash profile's, never as a
        target: Brew Day's pH box shows a recipe target only from mash_ph_target in the recipe's basic data, which
        BrewScribe reads and this never writes."""
        value = self._xml_number(recipe.find("MASH"), "PH") if recipe is not None else None
        if value is None or not 0 < value <= 14:
            return None
        return {"value": round(value, 2), "from": "mash_profile"}

    #: The most of a yeast's NOTES kept, in characters: enough for a plugin's notes, not the lab's whole sheet.
    YEAST_NOTES_MAX = 280

    @staticmethod
    def _short(text, most):
        """`text` on one line, cut at a word to at most `most` characters, "…" ending what was cut; None if blank."""
        words = " ".join(str(text or "").split())
        if not words:
            return None
        if len(words) <= most:
            return words
        cut = words[:most - 1].rsplit(" ", 1)[0] or words[:most - 1]
        return cut.rstrip(" ,;:.") + "…"

    def getYeasts(self, recipe):
        """A BeerXML recipe's yeasts, in recipe order, for a plugin's notes (BrewScribe): each {"name", "laboratory",
        "product_id", "type", "form", "amount", "amount_value", "amount_is_weight", "attenuation", "min_temp",
        "max_temp", "temp_unit", "add_to_secondary", "notes"}. amount is as BeerSmith shows it (DISPLAY_AMOUNT), else
        from AMOUNT; amount_value is AMOUNT, in kg when amount_is_weight, else in litres; the temperatures are in the
        brewery's TEMP_UNIT, from BeerXML's C; notes is the start of NOTES (YEAST_NOTES_MAX), with the HTML entities
        BeerSmith's yeast notes carry (&ldquo;) read as the characters they stand for. What the file doesn't
        say is None. Never raises: a recipe that can't be read has none."""
        unit = "F" if str(getattr(self, "TEMP_UNIT", "C")).upper().startswith("F") else "C"
        found = []
        try:
            for yeast in recipe.findall("YEASTS/YEAST"):
                name = (yeast.findtext("NAME") or "").strip()
                if not name:
                    continue

                def text(tag, yeast=yeast):
                    return (yeast.findtext(tag) or "").strip() or None

                weight = (yeast.findtext("AMOUNT_IS_WEIGHT") or "").strip().upper() == "TRUE"
                value = self._xml_number(yeast, "AMOUNT")
                value = value if value is not None and value > 0 else None
                amount = text("DISPLAY_AMOUNT")
                if amount is None and value is not None:
                    amount = "{:.1f} {}".format(value * 1000.0, "g" if weight else "ml")
                temps = []
                for tag in ("MIN_TEMPERATURE", "MAX_TEMPERATURE"):
                    c = self._xml_number(yeast, tag)
                    temps.append(None if c is None else round(c * 1.8 + 32 if unit == "F" else c, 1))
                found.append({
                    "name": name, "laboratory": text("LABORATORY"), "product_id": text("PRODUCT_ID"),
                    "type": text("TYPE"), "form": text("FORM"), "amount": amount, "amount_value": value,
                    "amount_is_weight": weight, "attenuation": self._xml_number(yeast, "ATTENUATION"),
                    "min_temp": temps[0], "max_temp": temps[1], "temp_unit": unit,
                    "add_to_secondary": (yeast.findtext("ADD_TO_SECONDARY") or "").strip().upper() == "TRUE",
                    "notes": self._short(html.unescape(yeast.findtext("NOTES") or ""), self.YEAST_NOTES_MAX),
                })
        except Exception as e:  # noqa: BLE001 - the recipe still imports, without them
            logging.warning("Could not read the recipe's yeasts: %s", e)
            return []
        return found

    def _additions_warnings(self, staged, steps):
        """The import's warnings about its mash and sparge additions, for a recipe kept with `steps` (the book's
        dicts): [(title, text)]. A MISC for the fermenter or the bottles (getMashSpargeAdditions' not placed) is named
        as not imported; water salts and acids for the mash with no MashInStep to ask for them at the dough-in, or for
        the sparge water with no runoff step (StepController._collects_wort), are named as without a reminder. The
        other mash and sparge additions are kept, and never reminded of, so never named. [] for a recipe with none."""
        found = []
        not_placed = [entry for entry in staged.get("not_placed") or [] if isinstance(entry, dict)]
        if not_placed:
            found.append(("Additions not imported", "Not imported, as CraftBeerPi keeps only what goes into the mash, "
                          "the sparge water and the boil: {}. Add them yourself.".format(", ".join(
                              "{} ({})".format(entry["label"], entry.get("use") or "no use given")
                              for entry in not_placed))))
        steps = [s for s in steps or [] if isinstance(s, dict)]

        def words(key):
            return ", ".join(entry["label"] for entry in staged.get(key) or []
                             if isinstance(entry, dict) and StepController.reminded(entry))

        mash = words(StepController.MASH_ADDITIONS_KEY)
        if mash and not any(s.get("type") == "MashInStep" for s in steps):
            found.append(("Mash additions without a reminder", "This recipe has no MashInStep to ask for the malt, so "
                          "nothing will remind you to add {} to the mash.".format(mash)))
        sparge = words(StepController.SPARGE_ADDITIONS_KEY)
        if sparge:
            from types import SimpleNamespace
            if not any(StepController._collects_wort(SimpleNamespace(name=s.get("name"), type=s.get("type")))
                       for s in steps):
                found.append(("Sparge additions without a reminder", "This recipe has no sparge step, so nothing "
                              "will remind you to add {} to the sparge water.".format(sparge)))
        return found

    def getFirstWort(self, hops, recipe_type):
        alert = "No"
        names = []
        if recipe_type == "kbh":
            if len(hops) != 0:
                alert = "Yes"
                for hop in hops:
                    names.append(hop[1])
        elif recipe_type == "xml":
            for hop in hops:
                use = hop.find("USE").text
                ## Hops which are not used in the boil step should not cause alerts
                if use != "First Wort":
                    continue
                alert = "Yes"
                names.append(self._xml_addition_label(hop))
        elif recipe_type == "bf":
            for hop in hops:
                if hop["use"] == "First Wort":
                    alert = "Yes"
                    names.append(hop["name"])  ## TODO: Testing
        elif recipe_type == "json":
            if len(hops) != 0:
                alert = "Yes"
                for hop in hops:
                    names.append(hop["name"])

        return [alert, " and ".join(names)]

    async def create_Whirlpool_Cooldown(self, time: str = "15", temp=None):
        # The whirlpool stand: a WhirlpoolStep, a WaitStep that knows the
        # whirlpool temperature. If the wort cools more than 2 C / 3.6 F below
        # it, the brewer is asked whether to heat it back and hold it. `temp`
        # only with whirlpool hops: without them the stand only settles the
        # trub, and nothing need ask.
        if self.cooldown != "WaiStep" and self.cooldown != "":
            props = {"Kettle": self.boilid, "Timer": time}
            if temp is not None:
                props["Temp"] = self._format_temp(temp)
            step_string = {
                "name": "Whirlpool",
                "props": props,
                "status_text": "",
                "status": "I",
                "type": "WhirlpoolStep",
            }
            await self.create_step(step_string)

        # CoolDown step is sending a notification when cooldowntemp is reached
        step_type = self.cooldown if self.cooldown != "" else "WaitStep"
        step_name = "CoolDown"
        cooldown_sensor = ""
        step_temp = ""
        step_timer = time
        if step_type.find("Cooldown") != -1:
            cooldown_sensor = self.cbpi.config.get("steps_cooldown_sensor", None)
            if cooldown_sensor is None or cooldown_sensor == "":
                cooldown_sensor = (
                    self.boilkettle.sensor
                )  # fall back to boilkettle sensor if no other sensor is specified
            step_timer = ""
            try:
                step_temp = (
                    int(self.CoolDownTemp)
                    if (
                        self.fermentation_step_temp is None
                        or self.fermentation_step_temp <= int(self.CoolDownTemp)
                    )
                    else self.fermentation_step_temp
                )
            except:
                step_temp = int(self.CoolDownTemp)
            step_string = {
                "name": "Cooldown",
                "props": {
                    "Kettle": self.boilid,
                    "Timer": step_timer,
                    "Temp": step_temp,
                    "Sensor": cooldown_sensor,
                    "Actor": self.CoolDownActor,
                },
                "status_text": "",
                "status": "I",
                "type": step_type,
            }
            await self.create_step(step_string)
      
    def get_config_values(self):
        self.kettle = None
        self.boilkettle = None
        # Define MashSteps
        self.TEMP_UNIT = self.cbpi.config.get("TEMP_UNIT", "C")
        self.AutoMode = self.cbpi.config.get("AutoMode", "Yes")
        self.mashin = self.cbpi.config.get("steps_mashin", "MashInStep")
        self.mash = self.cbpi.config.get("steps_mash", "MashStep")
        # A mash step named for the sparge (Sparge, Fly Sparge, Batch Sparge,
        # Lauter, Runoff) comes in as this type: SpargeStep heats the HLT and
        # leaves both pumps to the brewer. Set it to MashStep for the sparge
        # as a mash rest.
        self.sparge = self.cbpi.config.get("steps_sparge", "SpargeStep") or "SpargeStep"
        self.mashout = self.cbpi.config.get(
            "steps_mashout", None
        )  # Currently used only for the Braumeister
        self.boil = self.cbpi.config.get("steps_boil", "BoilStep")
        self.whirlpool = "Waitstep"
        self.cooldown = self.cbpi.config.get("steps_cooldown", "WaitStep")
        # get default boil temp from settings
        self.BoilTemp = self.cbpi.config.get(
            "steps_boil_temp", self.DEFAULT_BOIL_TEMP.get(self.TEMP_UNIT, 99)
        )
        # get default cooldown temp alarm setting
        self.CoolDownTemp = self.cbpi.config.get(
            "steps_cooldown_temp",
            self.DEFAULT_COOLDOWN_TEMP.get(self.TEMP_UNIT, 20),
        )
        self.CoolDownActor = self.cbpi.config.get("steps_cooldown_actor", None)
        self.WhirlpoolTemp = self.cbpi.config.get(
            "steps_whirlpool_temp",
            self.DEFAULT_WHIRLPOOL_TEMP.get(self.TEMP_UNIT, 80),
        )
        # get default Kettle from Settings
        self.id = self.cbpi.config.get("MASH_TUN", None)
        self.boilid = self.cbpi.config.get("BoilKettle", None)
        # Blank as well as unset: a one-vessel system boils in its mash kettle.
        # On more than one, import_recipe refuses that (_import_problem).
        if not self.boilid:
            self.boilid = self.id
        # If next parameter is Yes, MashIn Ste will be added before first mash step if not included in recipe
        self.addmashin = self.cbpi.config.get("AddMashInStep", "Yes")
        # Yes: a Mash Out before a sparge step, if the recipe has none (_with_mash_out).
        self.addmashout = self.cbpi.config.get("AddMashOutStep", "Yes")

        try:
            self.kettle = self.cbpi.kettle.find_by_id(self.id)
        except:
            self.cbpi.notify(
                "Recipe Upload",
                "No default Kettle defined. Please specify default Kettle in settings",
                NotificationType.ERROR,
            )
        try:
            self.boilkettle = self.cbpi.kettle.find_by_id(self.boilid)
        except:
            self.boilkettle = self.kettle

        config_values = {
            "kettle": self.kettle,
            "kettle_id": str(self.id),
            "boilkettle": self.boilkettle,
            "boilkettle_id": str(self.boilid),
            "mashin": str(self.mashin),
            "mash": str(self.mash),
            "mashout": str(self.mashout),
            "boil": str(self.boil),
            "whirlpool": str(self.whirlpool),
            "cooldown": str(self.cooldown),
            "boiltemp": str(self.BoilTemp),
            "cooldowntemp": str(self.CoolDownTemp),
            "whirlpooltemp": str(self.WhirlpoolTemp),
            "cooldownactor": self.CoolDownActor,
            "temp_unit": str(self.TEMP_UNIT),
            "AutoMode": str(self.AutoMode),
        }
        logging.info(config_values)
        return config_values

    async def create_recipe(self, name):
        """Start the recipe being imported.

        Inside import_recipe nothing is changed yet: the steps that follow are
        gathered, and kept in the Recipe Book only once the whole recipe has
        been read. An importer called on its own builds the brew steps as it
        reads, as it always has.
        """
        if self._staging:
            self._staged = {"name": name, "steps": []}
            return
        # Create recipe in recipe Book with name of first recipe in xml file
        self.recipeID = await self.cbpi.recipe.create(name)
        # send recipe to mash profile
        await self.cbpi.recipe.brew(self.recipeID)
        # remove empty recipe from recipe book
        await self.cbpi.recipe.remove(self.recipeID)

    def _as_sparge(self, step_string):
        """A mash step named for the sparge, made a SpargeStep.

        Every recipe source makes its mash steps with the steps_mash type. The
        one named for the runoff (StepController._collects_wort: Sparge, Fly
        Sparge, Batch Sparge, Lauter, Runoff, not "Sparge Water") becomes the
        steps_sparge type instead, SpargeStep unless the setting says other:
        it heats the HLT to the step's temperature, with the mash tun's AUTO
        off and both pumps left to the brewer. It needs an HLT, found as the
        hltpump extension finds it - the kettle the mash tun's HERMS works on.
        Without one, or with steps_sparge set to the mash type, the step comes
        in as a mash step. Never raises: the step then comes in as it was.
        """
        try:
            mash = getattr(self, "mash", None) or "MashStep"
            sparge = getattr(self, "sparge", None) or "SpargeStep"
            if not isinstance(step_string, dict) or step_string.get("type") != mash or sparge == mash:
                return step_string
            from types import SimpleNamespace

            from cbpi.controller.step_controller import StepController

            if not StepController._collects_wort(
                    SimpleNamespace(name=step_string.get("name"), type=step_string.get("type"))):
                return step_string
            from cbpi.extension.hltpump import kettle_roles

            hlt = kettle_roles(self.cbpi)["hlt"]
            props = step_string.get("props") or {}
            mash_tun = props.get("Kettle")
            if hlt is None or hlt.id == mash_tun:
                return step_string
            converted = dict(step_string)
            converted["type"] = sparge
            converted["props"] = {
                "Kettle": hlt.id,
                "Sensor": hlt.sensor,
                "Mash_Tun": mash_tun,
                "Temp": props.get("Temp"),
                "Timer": props.get("Timer"),
                "Notification": "",
            }
            return converted
        except Exception as e:  # noqa: BLE001
            logging.warning("Could not make '%s' a sparge step: %s",
                            (step_string or {}).get("name") if isinstance(step_string, dict) else "?", e)
            return step_string

    def _with_mash_out(self, steps):
        """The recipe's steps with a Mash Out before its sparge step, if it has none.

        A Sparge that comes in as a mash rest at 168 F does a mash out through
        the HERMS; as a SpargeStep (_as_sparge) it heats only the HLT, so the
        mash out has to be a step of its own. With AddMashOutStep Yes, a recipe
        with a steps_sparge step - a SpargeStep, not a mash rest - after a mash
        step, and before it no step named for a mash out (StepController.
        _ends_mash) and no rest already as hot, gets one right before the
        sparge: a mash step on the mash tun at MASH_OUT_TEMP for
        MASH_OUT_MINUTES. Heating the mash through the coil leaves the HLT hot
        too, so the sparge water is ready almost at once. Never raises: the
        steps then come in as they were.
        """
        try:
            if str(getattr(self, "addmashout", "Yes")) != "Yes":
                return steps
            mash = getattr(self, "mash", None) or "MashStep"
            mashin = getattr(self, "mashin", None) or "MashInStep"
            sparge = getattr(self, "sparge", None) or "SpargeStep"
            if sparge == mash:
                return steps
            at = next((i for i, s in enumerate(steps) if s.get("type") == sparge), None)
            if at is None:
                return steps
            from types import SimpleNamespace

            from cbpi.controller.step_controller import StepController

            before = steps[:at]
            if any(StepController._ends_mash(SimpleNamespace(name=s.get("name"))) for s in before):
                return steps
            temps = []
            for s in before:
                if s.get("type") in (mash, mashin):
                    try:
                        temps.append(float((s.get("props") or {}).get("Temp")))
                    except (TypeError, ValueError):
                        pass
            unit = "F" if str(getattr(self, "TEMP_UNIT", "C")).upper().startswith("F") else "C"
            temp = self.MASH_OUT_TEMP[unit]
            if not temps or max(temps) >= temp:
                return steps
            kettle = getattr(self, "kettle", None)
            mash_out = {
                "name": "Mash Out",
                "props": {
                    "AutoMode": self.AutoMode,
                    "Kettle": self.id,
                    "Sensor": getattr(kettle, "sensor", None) if kettle is not None else None,
                    "Temp": self._format_temp(temp),
                    "Timer": str(self.MASH_OUT_MINUTES),
                    "Notification": "",
                },
                "status_text": "",
                "status": "I",
                "type": mash,
            }
            logging.info("Import: a Mash Out at %s %s before '%s'", temp, unit, steps[at].get("name"))
            return steps[:at] + [mash_out] + steps[at:]
        except Exception as e:  # noqa: BLE001
            logging.warning("Could not add a Mash Out to the imported recipe: %s", e)
            return steps

    # function to create json to be send to api to add a step to the current mash profile. Currently all properties are send to each step which does not cuase an issue
    async def create_step(self, step_string):
        step_string = self._as_sparge(step_string)
        if self._staging:
            if self._staged is None:
                raise RuntimeError("a step came before its recipe")
            # As the /step2/ endpoint would have received it: through JSON, which
            # also refuses anything that could not be saved.
            self._staged["steps"].append(json.loads(json.dumps(step_string)))
            return
        # get server port from settings and define url for api calls -> adding steps
        self.port = str(self.cbpi.static_config.get("port", 8000))
        self.url = "http://127.0.0.1:" + self.port + "/step2/"
        # convert step:string to json required for api call.
        step = json.dumps(step_string)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        async with aiohttp.ClientSession(
            headers=headers, timeout=LOCAL_API_TIMEOUT
        ) as session:
            async with session.post(self.url, data=step) as response:
                return await response.text()
            await self.push_update()