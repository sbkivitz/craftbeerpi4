"""One kettle's settings as a file of its own: what the Hardware page's "Save to
file..." downloads and "Load from file..." reads back.

The file holds the name of the kettle it was saved from, its logic, the logic's
settings, and what the kettle is linked to - its sensor, heater and agitator,
and any sensor, actor, kettle or fermenter one of the settings names - each by
id and by name. The ids find them again on the same CraftBeerPi; the names find
them after a reinstall, where every id is new.

Loading takes two requests. plan() works out what loading a file would change,
and changes nothing: the brewer looks at that and confirms. KettleController
then applies the same plan, provided the kettle is still as plan() found it
(the plan's token) and its AUTO is off.

A file is checked against the logic as this CraftBeerPi declares it. A setting
the logic doesn't declare, or a value its declaration rules out, is left out
and named, and the kettle keeps what it had. So is a link that can't be found
by id or by name.

A setting loading would leave empty - one the file leaves out that is empty
now, one the file has empty, or any the file leaves out of a new kettle - is
listed in the plan's "blank", with the value the logic uses for it when it is
empty (CBPiKettleLogic.blank_defaults). Planned with fill, those values are
filled in instead: they show as changes, and loading saves them.

What the kettle measures (cbpi.api.kettle_measures: capacity, empty weight,
material, specific heat) goes in the file's "measurements" when the kettle has
any. Loading a file with them sets each one it has; one it leaves out stays as
it is. A file without them - every file saved before they existed - plans and
loads exactly as it did, and an odd value in one never refuses the file.
"""

import hashlib
import json
import math
import re
from datetime import datetime

from cbpi.api import clock, kettle_measures

FORMAT = "CraftBeerPi kettle settings"
VERSION = 1
# A kettle's file is a few kilobytes. Anything near this is some other file.
MAX_CHARS = 1_000_000
# The kettle's own links: the key in the file and in kettle.json, the word the
# kettle form uses for it, and the kind of thing it links to.
LINKS = (("sensor", "Sensor", "sensor"), ("heater", "Heater", "actor"), ("agitator", "Agitator", "actor"))
# Property types whose value is the id of another sensor, actor, kettle or fermenter.
LINK_TYPES = ("sensor", "actor", "kettle", "fermenter")


class KettleFileError(ValueError):
    """A file that can't be loaded. str() says why, in words for the brewer."""


class KettleFileRefused(Exception):
    """A file that can be loaded, but not now. str() says why, in words for the
    brewer; `reason` says it for a program: "auto", the kettle's AUTO is
    running, or "changed", the kettle has changed since its plan was made."""

    def __init__(self, message, reason):
        super().__init__(message)
        self.reason = reason


class NoSuchKettle(LookupError):
    """No kettle has the id asked for."""


def file_name(kettle_name):
    """The downloaded file's name: "<kettle> settings.json", without the
    characters a file name can't hold."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", str(kettle_name or ""))
    name = re.sub(r"\s+", " ", name).strip(" .")
    return "{} settings.json".format(name or "Kettle")


def declared(types, logic):
    """The settings a registered logic declares, in its own order, each label once."""
    out, seen = [], set()
    for p in ((types or {}).get(logic) or {}).get("properties") or []:
        label = p.get("label") if isinstance(p, dict) else None
        if label and label not in seen:
            seen.add(label)
            out.append(p)
    return out


def _blank(value):
    return value is None or value == ""


def _props(kettle):
    props = getattr(kettle, "props", None)
    if hasattr(props, "to_dict"):
        return props.to_dict()
    return dict(props or {})


def _name_of(item_id, items):
    if _blank(item_id):
        return None
    return next((name for i, name in items or [] if i == item_id), None)


def _link(item_id, items):
    """A link as the file holds it: its id and the name it has here."""
    if _blank(item_id):
        return {"id": None, "name": None}
    return {"id": item_id, "name": _name_of(item_id, items)}


def export(kettle, types, things, version):
    """The file for `kettle`, as a dict to be written as JSON.

    `things`: {"sensor" | "actor" | "kettle" | "fermenter": [(id, name), ...]},
    what a link can name, for the names that go with the ids.
    """
    props = _props(kettle)
    own = declared(types, kettle.type)
    if own:
        # The logic's settings, in its order. Anything else in props was left
        # behind by another logic, and loading would only refuse it.
        settings = {p["label"]: props[p["label"]] for p in own if p["label"] in props}
    else:
        # A logic this CraftBeerPi doesn't have: nothing to tell them apart by.
        settings = dict(props)
    setting_links = {}
    for p in own:
        value = settings.get(p["label"])
        if p.get("type") in LINK_TYPES and isinstance(value, str) and value:
            setting_links[p["label"]] = _link(value, things.get(p["type"]))
    data = {
        "format": FORMAT,
        "format_version": VERSION,
        "craftbeerpi": version,
        "saved": datetime.fromtimestamp(clock.now()).astimezone().isoformat(timespec="seconds"),
        "kettle": kettle.name,
        "logic": kettle.type or None,
        "settings": settings,
    }
    for key, _word, kind in LINKS:
        data[key] = _link(getattr(kettle, key, None), things.get(kind))
    data["setting_links"] = setting_links
    # Only what the kettle has: a kettle without measurements makes the file it always made.
    measured = kettle_measures.of(kettle)
    if measured:
        data["measurements"] = measured
    return data


def _parse_link(value, what):
    if value is None:
        return {"id": None, "name": None}
    if isinstance(value, str):
        return {"id": value or None, "name": None}
    if isinstance(value, dict):
        out = {}
        for key in ("id", "name"):
            part = value.get(key)
            if part is not None and not isinstance(part, str):
                raise KettleFileError(
                    "This kettle settings file can't be read: its {} has a {} that isn't text.".format(what, key)
                )
            out[key] = part or None
        return out
    raise KettleFileError("This kettle settings file can't be read: its {} isn't an id and a name.".format(what))


def parse(text):
    """The file's contents, checked to be a kettle settings file this CraftBeerPi
    can read, or KettleFileError saying why it isn't."""
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8")
        except UnicodeDecodeError:
            raise KettleFileError("This isn't a kettle settings file: it isn't a text file.") from None
    if not isinstance(text, str) or not text.strip():
        raise KettleFileError("The file is empty.")
    if len(text) > MAX_CHARS:
        raise KettleFileError("This file is too big to be a kettle settings file.")
    try:
        data = json.loads(text.lstrip("\ufeff"))
    except ValueError:
        raise KettleFileError(
            'This isn\'t a kettle settings file: it isn\'t JSON, the kind of text file that "Save to file" makes.'
        ) from None
    if not isinstance(data, dict):
        raise KettleFileError("This isn't a kettle settings file: it holds no kettle.")
    if data.get("format") != FORMAT:
        if isinstance(data.get("data"), list):
            raise KettleFileError(
                'This looks like CraftBeerPi\'s own kettle.json, which holds every kettle. Choose a file made '
                'with "Save to file".'
            )
        raise KettleFileError(
            'This isn\'t a kettle settings file: it has no "format": "{}" in it.'.format(FORMAT)
        )
    version = data.get("format_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise KettleFileError("This kettle settings file has no version number CraftBeerPi can read.")
    if version > VERSION:
        raise KettleFileError(
            "This file was saved by a newer CraftBeerPi, in version {} of the kettle settings format. This "
            "one reads version {}. Update CraftBeerPi to load it.".format(version, VERSION)
        )
    logic = data.get("logic")
    if logic is not None and not isinstance(logic, str):
        raise KettleFileError("This kettle settings file can't be read: its logic isn't a name.")
    name = data.get("kettle")
    if name is not None and not isinstance(name, str):
        raise KettleFileError("This kettle settings file can't be read: the kettle's name isn't text.")
    settings = data.get("settings")
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise KettleFileError("This kettle settings file can't be read: its settings aren't names and values.")
    setting_links = data.get("setting_links")
    if setting_links is None:
        setting_links = {}
    if not isinstance(setting_links, dict):
        raise KettleFileError("This kettle settings file can't be read: its setting_links aren't names and links.")
    saved_by = data.get("craftbeerpi")
    saved = data.get("saved")
    measured = data.get("measurements")
    return {
        "format_version": version,
        "craftbeerpi": saved_by if isinstance(saved_by, str) else None,
        "saved": saved if isinstance(saved, str) else None,
        "kettle": name,
        "logic": logic or None,
        "settings": settings,
        # Only the links the file has. One it leaves out stays as it is.
        "links": {key: _parse_link(data[key], word.lower()) for key, word, _kind in LINKS if key in data},
        "setting_links": {
            str(label): _parse_link(link, "link for {}".format(label)) for label, link in setting_links.items()
        },
        # Only the measurements the file has, each as kettle.json would keep it; never a reason to refuse a file.
        "measurements": {
            field: kettle_measures.stored(field, measured[field])
            for field in kettle_measures.FIELDS if field in measured
        } if isinstance(measured, dict) else {},
    }


def _bound(value):
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _number(value):
    """The value as a finite float, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value.strip() if isinstance(value, str) else value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _show(value):
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return str(value)


def check(value, prop):
    """(True, value to keep) or (False, why not): a setting's value from a file,
    against its declaration. A blank is always allowed: each logic takes a blank
    setting as a value of its own (CBPiKettleLogic.blank_defaults says which,
    for a logic that tells)."""
    if _blank(value):
        return True, ""
    kind = prop.get("type")
    if kind == "number":
        number = _number(value)
        if number is None:
            return False, "isn't a number"
        low, high = _bound(prop.get("min")), _bound(prop.get("max"))
        if (low is not None and number < low) or (high is not None and number > high):
            if low is not None and high is not None:
                return False, "is outside its range, {} to {}".format(_show(low), _show(high))
            if low is not None:
                return False, "is below its lowest, {}".format(_show(low))
            return False, "is above its highest, {}".format(_show(high))
        return True, value
    if isinstance(value, (dict, list)):
        return False, "isn't a single value"
    if kind == "select":
        options = prop.get("options") or []
        if not options or any(str(o) == str(value) for o in options if not isinstance(o, (dict, list))):
            return True, value
        return False, "isn't one of its choices ({})".format(", ".join(str(o) for o in options))
    if kind in ("text", "date", "time"):
        if isinstance(value, str):
            return True, value
        return False, "isn't text"
    # A kind of setting this CraftBeerPi doesn't know: nothing to check it against.
    return True, value


def same(a, b, kind=None):
    """Whether two values of a setting mean the same: 2 and "2" do, for a number or a choice."""
    if _blank(a) or _blank(b):
        return _blank(a) and _blank(b)
    if kind == "number":
        x, y = _number(a), _number(b)
        if x is not None and y is not None:
            return x == y
    return str(a) == str(b)


def _plain(value):
    """Whether JSON holds the value as it is: a finite number, text, or true/false."""
    if isinstance(value, (bool, str)):
        return True
    if isinstance(value, (int, float)):
        try:
            return math.isfinite(value)
        except OverflowError:
            return False
    return False


def usable_blank_defaults(given, own):
    """What a logic's blank_defaults() said, sorted into what can be used and
    what can't: ({label: value}, [(label, value, why not), ...]).

    `own`: the logic's settings, as declared() gives them. An entry can be used
    when it names one of them that isn't a link, and its value is a finite
    number, text or true/false that isn't blank and that the setting's
    declaration allows (check()): a choice's value one of its options, a
    number inside its range.
    """
    props = {p["label"]: p for p in own}
    usable, unusable = {}, []
    for label, value in given.items():
        p = props.get(label) if isinstance(label, str) else None
        if p is None:
            why = "isn't one of its settings"
        elif p.get("type") in LINK_TYPES:
            why = "is a link to a {}, which has no value of its own".format(p["type"])
        elif not _plain(value):
            why = "isn't a finite number, text or true/false"
        elif _blank(value):
            why = "is blank"
        else:
            ok, kept = check(value, p)
            why = None if ok else kept
        if why:
            unusable.append((label, value, why))
        else:
            usable[label] = value
    return usable, unusable


def _norm(name):
    return " ".join(str(name).split()).casefold()


def _match(wanted, items):
    """Find a link here: by its id, then by its name.

    -> (id, how): how is "none" for a link to nothing, "id" or "name" for the
    way it was found, and "missing" or "ambiguous", with id None, when it
    wasn't."""
    wanted_id, wanted_name = wanted.get("id"), wanted.get("name")
    if not wanted_id and not wanted_name:
        return None, "none"
    items = items or []
    if wanted_id and any(i == wanted_id for i, _ in items):
        return wanted_id, "id"
    if wanted_name:
        hits = [i for i, name in items if _norm(name) == _norm(wanted_name)]
        if len(hits) == 1:
            return hits[0], "name"
        if len(hits) > 1:
            return None, "ambiguous"
    return None, "missing"


def _say(value):
    """A value as a sentence about it shows it: text in quotes, a number as it is."""
    if _blank(value):
        return "blank"
    if isinstance(value, str):
        return value if _number(value) is not None else "'{}'".format(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _show(value)
    return json.dumps(value)


def _say_link(item_id, items):
    if _blank(item_id):
        return "none"
    name = _name_of(item_id, items)
    return "'{}'".format(name) if name is not None else "an unknown one ({})".format(item_id)


def _say_own(value):
    """A logic's own value for a setting, as a sentence shows it: a number to
    seven figures at most, since one worked out for Fahrenheit has fifteen."""
    if isinstance(value, float) and math.isfinite(value):
        return "{:.7g}".format(value)
    return _say(value)


def _empty(label, word, logic, defaults):
    """A setting's line in the plan's "blank": loading would leave it empty."""
    if label in defaults:
        return {"label": label, "name": word, "value": defaults[label],
                "text": "{} will be empty, so {} uses its own value, {}.".format(word, logic, _say_own(defaults[label]))}
    return {"label": label, "name": word, "value": None,
            "text": "{} will be empty, and {} gives no value to fill it in with.".format(word, logic)}


def _filled(row, now, value, logic):
    """A setting's row in the plan, filled in with the logic's own value."""
    row.update({"now": "" if _blank(now) else now, "after": value, "changes": True, "filled": True,
                "note": "filled in with {}'s own value".format(logic)})
    return row


def _not_found(what, kind, wanted, how, now, items):
    called = "called '{}'".format(wanted["name"]) if wanted.get("name") else "with the id {}".format(wanted["id"])
    if how == "ambiguous":
        reason = "more than one {} here is {}".format(kind, called)
    else:
        reason = "there's no {} {} here".format(kind, called)
    return "{}: {}, so it stays {}.".format(what, reason, _say_link(now, items))


def _link_row(key, word, kind, now, wanted, items):
    """A link's line in the plan: what it is now, what it would be, and how it was found."""
    target, how = _match(wanted, items)
    found = how in ("none", "id", "name")
    after = (target or "") if found else now
    row = {
        "key": key,
        "name": word,
        "link": kind,
        "now": {"id": now or None, "name": _name_of(now, items)},
        "after": {"id": after or None, "name": _name_of(after, items)},
        "changes": (now or None) != (after or None),
        "found": how,
        "note": None,
    }
    if how == "name":
        row["note"] = "found by name"
    elif how == "id" and wanted.get("name") and _norm(wanted["name"]) != _norm(row["after"]["name"] or ""):
        row["note"] = "found by its id; it's called '{}' here".format(row["after"]["name"])
    elif not found:
        row["note"] = _not_found(word, kind, wanted, how, now, items)
    return row, after


def _record(kettle):
    if kettle is None:
        return None
    record = {
        "name": kettle.name,
        "type": kettle.type or None,
        "props": _props(kettle),
        "sensor": getattr(kettle, "sensor", None) or None,
        "heater": getattr(kettle, "heater", None) or None,
        "agitator": getattr(kettle, "agitator", None) or None,
        "props_by_type": getattr(kettle, "props_by_type", None) or {},
    }
    measured = kettle_measures.of(kettle)
    if measured:
        record["measurements"] = measured
    return record


def token(before, after):
    """A short fingerprint of a plan: the kettle as it was found, and as the plan would leave it."""
    text = json.dumps({"before": before, "after": after}, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


def plan(current, data, types, things, defaults=None, fill=False):
    """What loading `data` (from parse()) into the kettle `current` would do, or
    into a new kettle when `current` is None. Changes nothing.

    The plan's "after" is the kettle as loading would leave it, and its token
    is what applying it must be given back: a kettle changed in between - on
    another screen, say - has another token.

    `defaults`: what the file's logic uses for each setting left empty
    ({label: value}, KettleController.blank_defaults()). Each declared setting
    that isn't a link and that loading would leave empty is in the plan's
    "blank", with that value, or None where the logic gives none. With `fill`,
    each one the logic gives a value for is filled in with it instead: it shows
    as a change, "after" and the token include it, and applying the plan saves
    it. A setting the logic gives no value for stays empty either way.
    """
    logic = data["logic"]
    if logic and logic not in (types or {}):
        raise KettleFileError(
            "This file is for the logic {}, which this CraftBeerPi doesn't have. Install its plugin, then "
            "load the file again.".format(logic)
        )
    defaults = dict(defaults or {})
    own = declared(types, logic)
    now_type = (getattr(current, "type", None) or None) if current is not None else None
    if current is None or not logic:
        base = {}
    elif now_type == logic:
        base = _props(current)
    else:
        # A new logic starts from the settings it last had on this kettle, as
        # in the kettle form; the old logic's are put aside for switching back.
        base = dict(((getattr(current, "props_by_type", None) or {}).get(logic)) or {})

    problems, left, rows, blank = [], [], [], []
    after_props = dict(base)
    settings = data["settings"]
    for p in own:
        label = p["label"]
        word = p.get("display_label") or label
        kind = p.get("type")
        now = base.get(label)
        row = {"label": label, "name": word, "type": kind, "unit": p.get("unit") or None, "note": None,
               "filled": False}
        # Filled in, when asked, rather than left empty.
        filling = fill and kind not in LINK_TYPES and label in defaults
        if label not in settings:
            if not _blank(now):
                left.append({"label": label, "name": word, "value": now,
                             "text": "{} stays {}: the file doesn't have it.".format(word, _say(now))})
            elif filling:
                rows.append(_filled(row, now, defaults[label], logic))
                after_props[label] = defaults[label]
            elif kind not in LINK_TYPES:
                blank.append(_empty(label, word, logic, defaults))
            continue
        value = settings[label]
        if kind in LINK_TYPES:
            items = things.get(kind)
            if not _blank(value) and not isinstance(value, str):
                problems.append({"label": label, "text": "{}: {} isn't the id of a {}, so it stays {}.".format(
                    word, _say(value), kind, _say_link(now, items))})
                continue
            wanted = dict(data["setting_links"].get(label) or {})
            wanted["id"] = value or None
            if not wanted["id"]:
                wanted["name"] = None
            link_row, after = _link_row(label, word, kind, now, wanted, items)
            if link_row["found"] in ("missing", "ambiguous"):
                problems.append({"label": label, "text": link_row["note"]})
            row.update({k: link_row[k] for k in ("link", "now", "after", "changes", "found", "note")})
            if row["changes"]:
                after_props[label] = after or ""
            rows.append(row)
            continue
        ok, kept = check(value, p)
        if not ok:
            if filling and _blank(now):
                problems.append({"label": label, "text": "{}: {} {}, so it's filled in with {}'s own value, {}.".format(
                    word, _say(value), kept, logic, _say_own(defaults[label]))})
                rows.append(_filled(row, now, defaults[label], logic))
                after_props[label] = defaults[label]
                continue
            problems.append({"label": label, "text": "{}: {} {}, so it stays {}.".format(word, _say(value), kept, _say(now))})
            if _blank(now):
                blank.append(_empty(label, word, logic, defaults))
            continue
        filled = filling and _blank(kept)
        if filled:
            kept = defaults[label]
        changes = not same(now, kept, kind)
        if changes:
            after_props[label] = kept
        row.update({"now": "" if _blank(now) else now, "after": kept if changes else ("" if _blank(now) else now),
                    "changes": changes})
        if filled and changes:
            _filled(row, now, kept, logic)
        rows.append(row)
        if _blank(after_props.get(label)):
            blank.append(_empty(label, word, logic, defaults))

    known = {p["label"] for p in own}
    skipped = [label for label in settings if label not in known]
    for label in skipped:
        if logic:
            text = "{} isn't a setting of {}, so it wasn't loaded.".format(label, logic)
        else:
            text = "{}: the file has no logic for it, so it wasn't loaded.".format(label)
        problems.append({"label": label, "text": text})

    links, after_links = [], {}
    for key, word, kind in LINKS:
        now = getattr(current, key, None) if current is not None else None
        now = now or None
        if key not in data["links"]:
            after_links[key] = now or ""
            if now:
                left.append({"label": key, "name": word, "value": _name_of(now, things.get(kind)) or now,
                             "text": "{} stays {}: the file doesn't say.".format(word, _say_link(now, things.get(kind)))})
            continue
        link_row, after = _link_row(key, word, kind, now, data["links"][key], things.get(kind))
        if link_row["found"] in ("missing", "ambiguous"):
            problems.append({"label": key, "text": link_row["note"]})
        after_links[key] = after or ""
        links.append(link_row)

    after = {
        "name": current.name if current is not None else (data["kettle"] or ""),
        "type": logic,
        "props": after_props,
        "sensor": after_links["sensor"],
        "heater": after_links["heater"],
        "agitator": after_links["agitator"],
    }
    # The measurements the file has (kettle_measures), each changing what the kettle has; one it leaves out stays.
    # A file without any plans exactly as before.
    measures = []
    for field, value in (data.get("measurements") or {}).items():
        now = getattr(current, field, None) if current is not None else None
        measures.append({"field": field, "now": now, "after": value, "changes": now != value})
    if measures:
        after["measurements"] = {m["field"]: m["after"] for m in measures}
    logic_row = {"now": now_type, "after": logic, "changes": now_type != logic, "note": None}
    if logic_row["changes"] and now_type:
        logic_row["note"] = "{}'s settings are put aside on this kettle, for switching back.".format(now_type)
    changes = (sum(1 for r in rows if r["changes"]) + sum(1 for r in links if r["changes"]) + int(logic_row["changes"])
               + sum(1 for m in measures if m["changes"]))
    other = (
        current is not None and bool(data["kettle"]) and bool(current.name)
        and _norm(data["kettle"]) != _norm(current.name)
    )
    result = {
        "kettle": None if current is None else {"id": current.id, "name": current.name},
        "file": {
            "kettle": data["kettle"],
            "logic": logic,
            "craftbeerpi": data["craftbeerpi"],
            "saved": data["saved"],
            "format_version": data["format_version"],
        },
        "other_kettle": other,
        "logic": logic_row,
        "links": links,
        "settings": rows,
        "problems": problems,
        "left_as_is": left,
        "blank": blank,
        "fill": bool(fill),
        "changes": changes,
        "unchanged": sum(1 for r in rows if not r["changes"]),
        "after": after,
        "token": token(_record(current), after),
    }
    if measures:
        result["measurements"] = measures
    return result
