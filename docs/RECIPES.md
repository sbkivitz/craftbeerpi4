# Recipes in CraftBeerPi 4

How to write, import, and run a recipe — including the parts that aren't obvious
from the UI.

Everything here was verified against a running server. Where CraftBeerPi ignores
something you wrote, or takes a value from settings instead of your file, it says
so.

---

## The one thing to understand first

Importing a recipe **replaces your brew steps immediately**, and keeps a copy of
it in your **Recipe Book**. The brew steps are what CraftBeerPi runs on brew day,
mash to chill, on the **Brew Steps** page (called *Mash Profile* before cbpi4gui
0.5.1.a3.post8).

```
beer.xml  ──import──>  brew steps (step_data.json)  ──run──>  brew day
              └──────>  Recipe Book (config/recipes/)
```

If you have brew steps mid-brew, importing replaces them without asking.

The Recipe Book (`/recipe/`) keeps the recipes you import and the brew steps you
save. Before 4.7.7.post30 an import never went into it: the upload page keeps one
file, so uploading the next one replaced it, and the recipe was gone unless you'd
saved it yourself.

---

## Before your first import

Import reads your settings to decide which kettles and step types to use.
**If `MASH_TUN` is not set the import fails** — with a notification that is easy to
miss if you are watching the upload instead.

| Setting | Purpose | Default |
|---|---|---|
| `MASH_TUN` | kettle for all mash steps | none — **import fails** |
| `BoilKettle` | kettle for the boil | falls back to `MASH_TUN` |
| `TEMP_UNIT` | `C` or `F` | `C` |
| `steps_boil_temp` | boil target — **not** taken from the recipe | `99` °C / `212` °F |
| `steps_cooldown_temp` | temperature the cooldown step alerts at | `20` °C / `68` °F |
| `AddMashInStep` | insert a mash-in step if the recipe has none | `Yes` |
| `AutoMode` | start/stop kettle logic automatically per step | `Yes` |
| `steps_mashin`, `steps_mash`, `steps_boil`, `steps_cooldown` | override step types | built-in types |

Temperature defaults are seeded on first start and depend on `TEMP_UNIT`, so set
your unit before anything else — changing it later does **not** convert values that
are already stored.

Set them under **Settings** in the UI, or over the API. A complete first-time setup:

```bash
PI=http://<pi>:8000

# 1. Find your kettle IDs
curl -s $PI/kettle/ | python -m json.tool
# {"data": [
#   {"id": "aCCVgZ2Ht...", "name": "HLT",         ...},
#   {"id": "oQhZqfBjS...", "name": "Mash Tun",    ...},
#   {"id": "b7qhSvKJ2...", "name": "Boil Kettle", ...}
# ], ...}

# 2. Point the importer at them
curl -X PUT -H "Content-Type: application/json" \
     -d '{"name":"MASH_TUN","value":"oQhZqfBjS..."}' \
     $PI/config/MASH_TUN/

curl -X PUT -H "Content-Type: application/json" \
     -d '{"name":"BoilKettle","value":"b7qhSvKJ2..."}' \
     $PI/config/BoilKettle/

# 3. Confirm it took
curl -s $PI/config/ | python -c \
  "import json,sys; c=json.load(sys.stdin); print('MASH_TUN =', c['MASH_TUN']['value'])"
```

Note the trailing slash on `/config/MASH_TUN/`. Without it you get a `308
Permanent Redirect`, which curl does not follow unless you add `-L` — so the
request silently does nothing.

---

## Writing a BeerXML recipe

CraftBeerPi reads a small subset of BeerXML. Everything else in the file is
ignored, which is why exports from brewing software work — they simply carry more
than is needed.

A complete working example lives alongside this documentation in the testbench as
`samples/beer.xml`.

### What is actually read

| BeerXML path | Becomes |
|---|---|
| `RECIPE/NAME` | the recipe's name |
| `RECIPE/BOIL_TIME` | boil step timer, minutes |
| `RECIPE/MASH/MASH_STEPS/MASH_STEP/NAME` | step name |
| `…/MASH_STEP/STEP_TEMP` | step target temperature |
| `…/MASH_STEP/STEP_TIME` | step duration, minutes |
| `RECIPE/HOPS/HOP` with `USE=Boil` | hop alert during the boil |
| `RECIPE/HOPS/HOP` with `USE=First Wort` | first-wort reminder before the wort reaches the boil kettle (see below) |
| `RECIPE/HOPS/HOP` with `USE=Aroma` plus `TEMP` | whirlpool / hopstand steps |
| `RECIPE/MISCS/MISC` with `USE=Boil` | alert during the boil, same as a hop |

### Mash steps

```xml
<MASH_STEP>
  <NAME>Saccharification</NAME>
  <STEP_TEMP>67.0</STEP_TEMP>   <!-- in TEMP_UNIT; converted if you use F -->
  <STEP_TIME>60.0</STEP_TIME>   <!-- minutes at temperature -->
</MASH_STEP>
```

**`STEP_TIME` of 0 means mash-in.** CraftBeerPi heats to `STEP_TEMP`, then *pauses
and waits for you* to dough in and acknowledge. That pause is the point — the timer
for the following rest does not start until you confirm.

If no zero-time step exists and `AddMashInStep` is `Yes`, one is inserted using the
first step's temperature.

> **Step temperatures are truncated to whole numbers on import.** `67.5` becomes
> `67`. If you mash at half degrees, edit the step after importing.

#### Example: a step mash

Each rest is its own `MASH_STEP`, in order. `STEP_TIME` is time *at* that
temperature; the ramp between rests is not timed.

```xml
<MASH_STEPS>
  <MASH_STEP>                       <!-- mash-in: waits for you -->
    <NAME>Dough In</NAME>
    <STEP_TEMP>52.0</STEP_TEMP>
    <STEP_TIME>0.0</STEP_TIME>
  </MASH_STEP>
  <MASH_STEP>                       <!-- protein rest -->
    <NAME>Protein Rest</NAME>
    <STEP_TEMP>52.0</STEP_TEMP>
    <STEP_TIME>20.0</STEP_TIME>
  </MASH_STEP>
  <MASH_STEP>                       <!-- beta -->
    <NAME>Beta Amylase</NAME>
    <STEP_TEMP>63.0</STEP_TEMP>
    <STEP_TIME>45.0</STEP_TIME>
  </MASH_STEP>
  <MASH_STEP>                       <!-- alpha -->
    <NAME>Alpha Amylase</NAME>
    <STEP_TEMP>72.0</STEP_TEMP>
    <STEP_TIME>20.0</STEP_TIME>
  </MASH_STEP>
  <MASH_STEP>                       <!-- mash out -->
    <NAME>Mash Out</NAME>
    <STEP_TEMP>76.0</STEP_TEMP>
    <STEP_TIME>10.0</STEP_TIME>
  </MASH_STEP>
</MASH_STEPS>
```

#### Temperature units

**`STEP_TEMP` is always Celsius**, which is what the BeerXML specification says. If
`TEMP_UNIT` is `F`, CraftBeerPi converts on import; if it is `C`, the value is used
as-is.

```xml
<STEP_TEMP>67.0</STEP_TEMP>
```

- server set to `C` → step is **67 °C**
- server set to `F` → step is **152.6 °F**, then truncated to **152**

So write Celsius in the file regardless of your server's unit. Do **not** write
`152.0` because you brew in Fahrenheit — that would be read as 152 °C and converted
to 305 °F.

Hop `TEMP` (used for the whirlpool) is handled the same way.

### Hop and misc timings

`TIME` is **minutes remaining in the boil**, not elapsed. A 60-minute addition goes
in at the start of a 60-minute boil.

Hops and miscs are merged into one list, sorted by descending time, and assigned to
slots `Hop_1`…`Hop_6`. A misc can therefore land in a slot named "Hop" — that is
normal. This:

```xml
<HOP><NAME>Magnum</NAME><USE>Boil</USE><TIME>60.0</TIME></HOP>
<HOP><NAME>Centennial</NAME><USE>Boil</USE><TIME>15.0</TIME></HOP>
<HOP><NAME>Cascade</NAME><USE>Boil</USE><TIME>5.0</TIME></HOP>
<MISC><NAME>Whirlfloc</NAME><USE>Boil</USE><TIME>10.0</TIME></MISC>
```

produces:

```
Hop_1 = 60  Magnum
Hop_2 = 15  Centennial
Hop_3 = 10  Whirlfloc     <- the misc
Hop_4 = 5   Cascade
```

> **Six boil additions maximum.** Slots run `Hop_1` to `Hop_6`, filled from the
> sorted list. A seventh addition is silently dropped.
>
> Up to and including 4.7.7 the sixth slot was populated on import and shown in the
> UI but never announced, because the boil step read only `Hop_1`–`Hop_5`. If you
> are on an unpatched build, keep your boil additions to five or verify the last
> one fires.

### First-wort hops

First-wort hops go into the boil kettle before the wort does, so CraftBeerPi asks
for them before the wort reaches it (since 4.7.7.post31), once a brew:

1. **When a step named for the runoff starts.** Its name starts with Sparge, Fly
   Sparge, Batch Sparge, Continuous Sparge, Lauter or Runoff, and doesn't mention
   water, heat, prepare, salts or treatment ("Heat Sparge Water" doesn't count).
   The reminder reads "Please add 1.00 oz Magnum to the boil kettle before the
   first runnings reach it", with an **Added** button.
2. **Otherwise, in the question before the boil:** "Ready to boil? Add 1.00 oz
   Magnum to the boil kettle for first wort, then move the wort into it …". A
   restart while the boil waits asks again there.
3. **With `CONFIRM_BEFORE_BOIL` set to No**, when the boil step starts, which is
   then the end of the mash.

Starting the boil, or Stop and Start, doesn't ask again; Reset does, for the next
brew. Before 4.7.7.post31 the boil only asked as it started, which with the boil
waiting for you came after the wort was in the kettle.

### Whirlpool / hopstand

An `Aroma` hop with a `TEMP` produces two steps: a notification telling you to cool
to that temperature and add the hops, then a wait.

```xml
<HOP>
  <NAME>Citra Whirlpool</NAME>
  <USE>Aroma</USE>
  <TIME>20.0</TIME>     <!-- IGNORED -->
  <TEMP>80.0</TEMP>     <!-- used in the notification -->
</HOP>
```

> ⚠️ **`TIME` is ignored for aroma hops.** The whirlpool wait is hardcoded to
> **15 minutes**. If you want 20, edit the wait step after importing.

### Boil temperature does not come from your recipe

BeerXML has no field for it. The boil step uses `steps_boil_temp` from settings
(default 98 °C). If your boil runs at a different temperature — altitude, or a
sensor offset — set it there, not in the file.

---

## Importing

### From the UI

**Recipe Book** → **Import recipe** → upload your `.xml` → pick the recipe from the
list → **Create Recipe from BeerXML recipe**. The notification says whether it was
kept in the Recipe Book.

### From the API

```bash
# 1. Upload. The content type matters - see the warning below.
curl -F "File=@myrecipe.xml;type=text/xml" http://<pi>:8000/upload/

# 2. List the recipes inside the file
curl http://<pi>:8000/upload/xml
# [{"value": "1", "label": "HERMS Test Pale Ale"}]

# 3. Import one, by its "value" (1-based)
curl -X POST -H "Content-Type: application/json" \
     -d '{"id":"1"}' http://<pi>:8000/upload/xml
```

> ⚠️ **The upload is content-type driven and fails silently.** The server stores the
> file only if the content type is exactly `text/xml`. Send `application/xml`, or
> let a tool guess, and you get **HTTP 200 with nothing saved** — then step 2
> returns `[]` and it looks as though your file contains no recipes. Always pass
> `;type=text/xml` with curl.

Two more things worth knowing:

- The uploaded file is always stored as `beer.xml`, whatever you named it. Only one
  BeerXML file exists at a time; uploading replaces it.
- A file may contain several `<RECIPE>` elements. Step 2 lists them, and `value` is
  a **1-based index**, not an ID from the file.

### Other formats

The same flow works for Kleiner Brauhelfer (`/upload/kbh`, a `.db`) and MMuM JSON
(`/upload/json`). Brewfather is pulled over its API rather than uploaded
(`/upload/bf/...`).

---

## What you get

The example file produces these brew steps:

| # | Step | Type | Details |
|---|---|---|---|
| 1 | Mash In | `MashInStep` | 67 °C, waits for you to add malt |
| 2 | Saccharification | `MashStep` | 67 °C, 60 min |
| 3 | Mash Out | `MashStep` | 76 °C, 10 min |
| 4 | Boil Step | `BoilStep` | 99 °C, 60 min, 4 alerts |
| 5 | Whirlpool Hop | `NotificationStep` | "Cool down to 80 °C, then add hops" |
| 6 | Whirlpool | `WaitStep` | 15 min |

Step 4 is at **99 °C from settings**, not from the recipe, and step 6 is
**15 minutes regardless** of what the file said.

A `CoolDown` step is appended when `steps_cooldown` is configured.

---

## Running it

1. **Read the brew steps first.** Import is mechanical; it cannot tell you the recipe
   assumed a different rig.
2. **Check each step's kettle and sensor.** These are resolved from your settings at
   import time. Change kettles afterwards and the brew steps still point at the old
   ones.
3. **Start.** Steps advance automatically when their timers expire, except those
   that wait for you.
4. **Acknowledge the prompts.** Mash-in and whirlpool stop and wait; the brew does
   not continue until you confirm.

With `AutoMode` set to `Yes`, each step switches its kettle's logic on when it
starts and off when it ends. With `No`, you control the kettles yourself and steps
only manage timing.

### Alerts

Hop and step notifications appear in the UI and auto-dismiss after a few seconds.
**Away from the screen, you will miss them.** There is no audible alarm built in.
For a 5-minute hop addition that matters — consider an MQTT actor driving a buzzer,
or keep the screen within earshot.

### Mash pH reminder (for plugins such as BrewScribe)

Off unless a plugin turns it on. When it's on, CraftBeerPi reminds you to check
the mash pH 15 minutes after you confirm mash-in, on every brew.

A plugin turns it on once, when it loads, in its `setup(cbpi)` function or at
start-up, and can read where it's up to (added in 4.7.7.post28):

```python
if hasattr(cbpi.step, "enable_mash_ph_reminder"):
    cbpi.step.enable_mash_ph_reminder()           # calling it again does no harm
    status = cbpi.step.mash_ph_reminder_status()  # a copy: reading it changes nothing
```

**What starts it.** Only you confirming the malt is in, on the built-in mash-in
step (`MashInStep`): its **Next Step** button on the "please add malt" message,
or the dashboard's **Next** while that step waits for the malt. Heating,
reaching strike, pressing Next before the water was hot, or turning the
reminder on after you'd already confirmed mash-in don't start it. A plugin
can't start it with a time of its own, and mash-in steps from other plugins
don't start it.

**What you see.** 15 brewing minutes after you confirm, every page shows
**Check mash pH** with an **OK** button, including pages opened later, until you
press OK on one of them. OK only clears the reminder. It doesn't record a
measurement, move the brew on or switch anything.

**Once per brew.** It keeps counting while the brew steps are stopped, and Stop and
Start don't start another one. Reset, Clear, loading or importing a recipe,
deleting the mash-in step or changing its type, and the end of the brew cancel
it.

**After a restart.** It's saved in `step_data.json` under
`basic._mash_ph_reminder`, not in the step's settings. A restart brings back a
countdown still running, or the reminder itself if it was due and not yet
answered; one you answered never comes back. If no plugin turns the reminder on
after the restart, nothing is shown. Saving the brew steps to the Recipe Book
leaves it out, and an import throws it away.

**On a simulator.** The 15 minutes are brewing time, from `cbpi.api.clock`: at
`SIM_TIME_SCALE` 60 the reminder comes 15 real seconds after mash-in, and it
keeps up if you change the speed while it counts down. If the simulator
restarts mid-countdown, the time left carries over, and the time it was off
counts at the speed it ran before. Like every time saved for a restart, this
relies on the computer's clock being right.

**Status.** `mash_ph_reminder_status()` always has `enabled` and `state`.
`state` is `disabled` (no plugin turned it on, even if a reminder is saved),
`waiting_for_mash_in`, `pending` (counting down), `shown` (on screen) or
`acknowledged`. Once mash-in is confirmed it also has `run_id` (new each brew),
`step_id` (the mash-in step), `confirmed_at` and `due_at` (brewing-clock
seconds; after a simulator restart `due_at` is moved onto the new clock) and
`remaining_seconds`. Use these two functions rather than editing
`basic._mash_ph_reminder`. There's no web address for it.

### Following the brew steps (for plugins)

A plugin can be told as the brew steps change, without running them (added in
4.7.7.post30). BrewScribe uses it to remind you before the mash out.

```python
def on_steps(event, snapshot):
    ...                                    # read only, and quick

if hasattr(cbpi.step, "add_listener"):
    cbpi.step.add_listener(on_steps)       # adding it again does no harm
    now = cbpi.step.snapshot()             # where the brew is up to, at start-up
```

Add it when the plugin loads. It isn't saved, so add it on every start.
`cbpi.step.remove_listener(on_steps)` stops it.

**When you're told.** Only when something changes, never once a second. The
names are in `cbpi.step.LISTENER_EVENTS`.

| Event | When |
|---|---|
| `step_started` | A step starts: Start, Resume, Next, or moving on by itself. Its countdown may not have begun. |
| `step_held` | The brew waits for you to start the next step: the boil, while you move the wort. |
| `timer_started` | The active step's countdown begins. From now on you know when it ends. |
| `timer_changed` | Its end moved: **Add 5 minutes**. |
| `timer_stopped` | It stopped before its end: Stop, Next, Reset or a shutdown. The time left is kept. |
| `timer_done` | It reached its end. |
| `step_done` | A step finished, by its countdown or by Next. |
| `step_failed` | A step failed. The brew steps stop there. |
| `stopped` | You pressed Stop. |
| `reset` | You pressed Reset: every step is waiting again. |
| `loaded` | The brew steps were replaced: an import, Brew from the Recipe Book, or Clear. |

**What you get.** Plain data, and a copy of your own: changing it changes
nothing.

```python
{
    "event": "timer_started",
    "at": 1234567.0,           # brewing-clock seconds, as cbpi.api.clock.now()
    "step_id": "...",          # the step it happened to; None for reset and loaded
    "active_id": "...",        # the step running, or None
    "held_id": None,           # the step waiting for you to start it, or None
    "basic": {"name": "...", "author": "..."},   # without basic._mash_ph_reminder
    "steps": [                 # every step, in order
        {"id": "...", "name": "Saccharification", "type": "MashStep",
         "status": "A",        # I waiting, A active, S stopped, D done, E failed
         "props": {}, "state_text": "", "state_text2": None,
         "timer": {"counting": True,
                   "started_at": 1234567.0,      # None unless counting
                   "ends_at": 1238167.0,         # None unless counting
                   "seconds_left": 3600.0,
                   "length": 3600.0}},           # or None: no countdown yet
    ],
}
```

**Timing.**

- A rest's real end is known only from `timer_started`. A mash step starts
  counting when the mash reaches the rest temperature, not when the step
  starts. After a power cut it counts down what was left, so use `seconds_left`
  and `ends_at`, not the step's `Timer` setting.
- Treat each snapshot as the whole story. Cancel what you planned on
  `timer_stopped`, `stopped`, `reset` and `loaded`; plan again on
  `timer_started` and `timer_changed`.
- Wait with `await clock.sleep_until(ends_at - lead)` from `cbpi.api.clock`.
  Don't use the PC's clock, and don't poll: this is exact at any simulator speed.
- Moving on is immediate. `timer_done`, `step_done` and the next step's
  `step_started` all come at the same brewing instant. To do something *before*
  the mash out, plan it from the end of the rest before it.

**Read only.** A listener must not start, stop, move on or reset the brew steps,
or switch anything. A plain function runs on the server's event loop right after
the change, so keep it quick; an `async def` runs as a task of its own, just
after. If a listener raises, CraftBeerPi logs it and carries on, and the other
listeners are still told. The mash pH reminder above is separate: listening
neither starts it nor adds a second one.

---

## Saving and reusing

Every import is kept in the Recipe Book, marked *Imported from BeerXML* (or the
format it came in; `imported_from` in the recipe's data). Importing a recipe of the
same name again - changed in BeerSmith and exported again - replaces that copy, and
the notification says so.

An import never replaces a copy of your own. A copy is yours once you:

- press **Save** on the Brew Steps page, which stores the brew steps *as they are
  now*, including your edits;
- change a recipe in the Recipe Book and save it; or
- clone it.

An import of the same name then adds a new copy next to yours.

```bash
curl http://<pi>:8000/recipe/                  # list the Recipe Book; "file" is each id
curl -X POST http://<pi>:8000/recipe/<id>/brew # make one your brew steps
```

A good routine: import, adjust for your rig, save, and brew from the book
thereafter.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `GET /upload/xml` returns `[]` after a successful upload | Content type was not `text/xml`. Re-upload with `;type=text/xml`. |
| Import appears to do nothing | `MASH_TUN` not set. Check notifications. |
| Boil runs at the wrong temperature | It comes from `steps_boil_temp`, not the recipe. |
| A hop addition is missing | More than six boil additions — hops and miscs share six slots. On an unpatched 4.7.7 the sixth is also never announced. |
| Whirlpool runs 15 min instead of what I wrote | Aroma `TIME` is ignored; the wait is hardcoded. |
| Mash temperatures lost their decimals | Step temperatures are truncated to integers on import. |
| Steps point at the wrong kettle | Kettles are resolved at import time. Re-import after changing them. |
| My brew steps were replaced | An import replaces them. Save them to the Recipe Book first. |
| A recipe I imported before 4.7.7.post30 is gone | Imports weren't kept then, and the next upload replaced the file. Import it again: it's kept now. |
| "Recipe not kept" after an import | The recipe is your brew steps, but the Recipe Book couldn't be written (a full disk, say). Fix that, then press **Save** on the Brew Steps page. |
