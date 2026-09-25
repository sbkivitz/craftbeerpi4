"""Writing state so that losing power cannot destroy it.

Every controller in this project persisted state like this:

    with open(self.path, "w") as file:
        json.dump(data, file, indent=4, sort_keys=True)

Opening a file "w" truncates it before anything is written. Between that
truncation and the completed write the file on disk is empty, and if the
process stops in that window - a power cut, a kill, a crash, a full disk - that
is what survives the reboot.

The window is not rare. step_controller.save() runs on every step transition
and on every progress checkpoint (CBPiStep.ELAPSED_SAVE_INTERVAL, 30 brewing
seconds), so a single sixty minute mash rewrites step_data.json around a
hundred and twenty times. Each one is an opportunity to come back to an empty
mash profile, and an empty step_data.json is exactly 39 bytes of
{"basic": {}, "steps": []} - indistinguishable from never having had a recipe.

For a controller that runs unattended next to a kettle, on a Raspberry Pi,
usually on an SD card, that is not an acceptable way to store the thing the
brew day depends on.

The fix is the standard one: write a temporary file alongside the target, flush
it to the platter, then rename it over the original. Rename is atomic on both
POSIX and Windows via os.replace, so a reader either sees the whole previous
file or the whole new one and never a partial write.
"""

import json
import os
import tempfile
import time


def atomic_write_text(path, text, encoding="utf-8"):
    """Replace `path` with `text`, atomically.

    Writes a sibling temporary file, forces it to disk, then renames it over
    the target. The temporary file is created in the same directory because a
    rename is only atomic within one filesystem.

    A write whose content matches what is already there is skipped. This runs
    on a Raspberry Pi booting from an SD card, where the cost of a write is not
    the bytes but the erase block: a few unchanged bytes rewritten repeatedly
    wear the card exactly as fast as useful ones. Several callers here save
    unconditionally after operations that often change nothing.
    """
    path = str(path)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)

    try:
        with open(path, "r", encoding=encoding) as existing:
            if existing.read() == text:
                return
    except (OSError, ValueError):
        pass

    fd, tmp = tempfile.mkstemp(
        prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            # Without this the bytes may still be in the kernel's cache, and a
            # power cut loses them even though the rename succeeded.
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def atomic_write_json(path, data, **dump_kwargs):
    """Serialise `data` to `path` atomically.

    Serialising to a string first is deliberate: if the data cannot be encoded,
    that raises here, before the existing file has been touched. The old
    truncate-then-dump pattern destroyed the previous contents first and only
    then discovered it could not write the new ones.
    """
    dump_kwargs.setdefault("indent", 4)
    atomic_write_text(path, json.dumps(data, **dump_kwargs))


def quarantine(path):
    """Move an unreadable state file aside instead of deleting it.

    When a config file failed to parse, every controller here did this:

        os.remove(self.path)
        ... write an empty one ...

    which is the worst possible response. The file that failed to parse is the
    only remaining copy of the brewer's mash profile, or kettle definitions, or
    settings - and a parse failure usually means a truncated write, so most of
    the content is still sitting there. Deleting it converts a recoverable
    problem into a permanent loss, silently, during startup.

    Renaming keeps it. Returns the new path, or None if there was nothing to
    move or it could not be moved.
    """
    path = str(path)
    if not os.path.exists(path):
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = f"{path}.corrupt-{stamp}"
    try:
        os.replace(path, target)
        return target
    except OSError:
        try:
            os.remove(path)
        except OSError:
            pass
        return None
