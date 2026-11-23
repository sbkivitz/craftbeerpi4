"""Figures' worker: a process of its own, at low priority, that reads the logs and works out the brews in them, so
months of logs never hold up the brewing controls.

FiguresController starts it with `python -m cbpi.figures.worker` and talks to it in JSON, one message a line: a job on
its stdin, its answer on its stdout, {"id", "result"} or {"id", "error"}. Jobs, each with an "id":

- "analyse": the brews in every folder of logs (logs.sources), with BrewScribe's days and the strike water helper's
  landings: {"logs", "cache", "params", "days", "landings"}. What an archived run gives is kept in the "cache" file
  with its logs' fingerprint, so a run is read once. The live logs are read again when they change, each file kept in
  memory while it doesn't (a rotated file keeps its size and time).
- "graph": one vessel's readings for a graph: {"logs", "source", "params", "vessel", "t0", "t1", "points"}.
- "stop": end.

It ends by itself after IDLE_S without a job, giving back what it kept in memory; the next job starts it again.
"""

import hashlib
import json
import os
import queue
import sys
import threading
import traceback

# Imported here, before the thread that reads stdin starts: on Windows, importing numpy and pandas while that
# thread waits on the pipe waits with it.
from cbpi.figures import analysis, graph, logs

IDLE_S = 600.0


def lower_priority():
    """Below the server and everything else on the computer: reading an archive may take a core for a minute."""
    try:
        os.nice(19)
    except (AttributeError, OSError):
        pass
    if os.name == "nt":
        try:
            import ctypes
            below_normal = 0x00004000
            kernel = ctypes.windll.kernel32
            kernel.SetPriorityClass(kernel.GetCurrentProcess(), below_normal)
        except Exception:  # noqa: BLE001 - a normal priority still works
            pass


def _digest(value):
    return hashlib.sha1(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _within(items, span):
    if not span:
        return []
    return [d for d in items
            if float(d.get("end", d.get("t"))) >= span[0] and float(d.get("start", d.get("t"))) <= span[1]]


class Worker:

    def __init__(self):
        self.files = {}

    def _read_cache(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                kept = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(kept, dict) or kept.get("version") != analysis.VERSION:
            return {}
        sources = kept.get("sources")
        return sources if isinstance(sources, dict) else {}

    @staticmethod
    def _write_cache(path, sources):
        if not path:
            return
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": analysis.VERSION, "sources": sources}, f, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def analyse(self, job):
        params = job["params"]
        days = job.get("days") or []
        landings = job.get("landings") or []
        cache_path = job.get("cache")
        kept = self._read_cache(cache_path) if cache_path else {}
        params_key = _digest(params)
        out, read = {}, []
        seen_files = set()
        for source, folder in logs.sources(job["logs"]):
            live = source == logs.LIVE
            cache = self.files if live else None
            a = analysis.Analysis(folder, params, cache)
            names = a.names()
            fingerprint = logs.fingerprint(folder, names)
            old = kept.get(source)
            if isinstance(old, dict) and old.get("logs") == fingerprint and old.get("params") == params_key:
                span = old.get("span")
                if old.get("inputs") == _digest([_within(days, span), _within(landings, span)]):
                    out[source] = old["result"]
                    continue
            if not live and a.nothing_heated():
                result = {"span": None, "brews": [], "days": {}}
            else:
                result = analysis.analyse(folder, params, days, landings, cache=cache)
            span = result.get("span")
            kept[source] = {"logs": fingerprint, "params": params_key, "span": span,
                            "inputs": _digest([_within(days, span), _within(landings, span)]), "result": result}
            out[source] = result
            read.append(source)
            if live:
                for name in names:
                    for path in logs.log_files(folder, name):
                        seen_files.add(os.path.basename(path).split(".log", 1)[0])
        # Keep only what the live logs still hold: a file rotated out of them is gone.
        for key in [k for k in self.files if k[0] not in seen_files]:
            del self.files[key]
        kept = {s: v for s, v in kept.items() if s in out}
        if read:
            self._write_cache(cache_path, kept)
        return {"sources": out, "read": read}

    def graph(self, job):
        folder = logs.source_folder(job["logs"], job["source"])
        if folder is None:
            return None
        return graph.window(folder, job["params"], job["vessel"], float(job["t0"]), float(job["t1"]),
                            int(job.get("points") or 600))

    def serve(self, inbox, send):
        while True:
            try:
                message = inbox.get(timeout=IDLE_S)
            except queue.Empty:
                return
            kind = message.get("kind")
            if kind == "stop":
                return
            ident = message.get("id")
            try:
                if kind == "analyse":
                    send({"id": ident, "result": self.analyse(message)})
                elif kind == "graph":
                    send({"id": ident, "result": self.graph(message)})
                else:
                    send({"id": ident, "error": "unknown job %r" % (kind,)})
            except Exception as e:  # noqa: BLE001 - answered as an error; the worker carries on
                send({"id": ident, "error": "%s: %s" % (type(e).__name__, e),
                      "trace": traceback.format_exc(limit=6)})


def _read(stream, inbox):
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(message, dict):
            inbox.put(message)
    inbox.put({"kind": "stop"})


def main():
    lower_priority()
    out = sys.stdout.buffer
    # Anything else that writes to stdout goes to stderr instead, so stdout carries only answers.
    sys.stdout = sys.stderr
    lock = threading.Lock()

    def send(message):
        data = (json.dumps(message, allow_nan=False, default=float) + "\n").encode("utf-8")
        with lock:
            out.write(data)
            out.flush()

    inbox = queue.Queue()
    threading.Thread(target=_read, args=(sys.stdin.buffer, inbox), name="figures-inbox", daemon=True).start()
    Worker().serve(inbox, send)
    return 0


if __name__ == "__main__":
    sys.exit(main())
