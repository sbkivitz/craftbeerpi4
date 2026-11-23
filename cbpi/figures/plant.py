"""The strike water box's plant file (config/strike_plant.json) as Figures reads and writes it: one guess's range, and
a measured value for it, which read_plant() (cbpi/controller/strike_controller.py) fixes the guess at.

Only `guesses.<name>.measured` is written; everything else in the file is kept as it is. The strike water helper
reads the file again when its time changes, and works USE out again from the new number.
"""

import json
import logging

from cbpi.api.persist import atomic_write_json

logger = logging.getLogger(__name__)

PLANT_FILE = "strike_plant.json"


def read(path):
    """The plant file's contents, or {} when it's missing or can't be read."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning("Figures: %s could not be read: %s", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def _defaults(name):
    try:
        from cbpi.strike import model
        r = model.default_guesses(model.Hlt())[name]
        return {"low": r.low, "central": r.central, "high": r.high}
    except Exception:  # noqa: BLE001 - the strike model's own numbers, as its file lists them
        return {"low": 0.45, "central": 0.75, "high": 1.0} if name == "tun_share" else {}


def guess(data, name):
    """{"low", "central", "high", "measured"} for one guess: the file's range, else the model's, and the file's
    measured entry or None."""
    found = _defaults(name)
    entry = (data.get("guesses") or {}).get(name) if isinstance(data.get("guesses"), dict) else None
    entry = entry if isinstance(entry, dict) else {}
    for key in ("low", "central", "high"):
        value = entry.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            found[key] = float(value)
    found["measured"] = entry.get("measured") if isinstance(entry.get("measured"), dict) else None
    return found


def write_measured(path, name, measured):
    """Set guesses.<name>.measured to `measured` (a dict), or remove it with None, keeping the rest of the file."""
    data = read(path)
    guesses = data.get("guesses") if isinstance(data.get("guesses"), dict) else {}
    entry = guesses.get(name) if isinstance(guesses.get(name), dict) else {}
    if measured is None:
        entry.pop("measured", None)
    else:
        entry["measured"] = measured
    guesses[name] = entry
    data["guesses"] = guesses
    atomic_write_json(path, data, indent=4)
