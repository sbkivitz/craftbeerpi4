# Recipes in CraftBeerPi 4

How to write, import, and run a recipe — including the parts that aren't obvious
from the UI.

Everything here was verified against a running server. Where CraftBeerPi ignores
something you wrote, or takes a value from settings instead of your file, it says
so.

---

## The one thing to understand first

Importing a recipe **overwrites your active brew profile immediately**. It does not
go into the recipe book and wait for you to pick it.

```
beer.xml  ──import──>  active brew profile (step_data.json)  ──run──>  brew day
```

The recipe book (`/recipe/`) is a *separate* store for profiles you want to keep.
Importing does not touch it. If you have a profile mid-brew, importing replaces it
without asking.

---

## Before your first import

Import reads your settings to decide which kettles and step types to use.
**If `MASH_TUN` is not set the import fails** — with a notification that is easy to
miss if you are watching the upload instead.

| Setting | Purpose | If unset |
|---|---|---|
| `MASH_TUN` | kettle for all mash steps | **import fails** |
| `BoilKettle` | kettle for the boil | falls back to `MASH_TUN` |
| `TEMP_UNIT` | `C` or `F` | `C` |
| `steps_boil_temp` | boil target — **not** taken from the recipe | `98` |
| `AddMashInStep` | insert a mash-in step if the recipe has none | `Yes` |
| `AutoMode` | start/stop kettle logic automatically per step | `Yes` |
| `steps_cooldown_temp` | temperature the cooldown step alerts at | `25` |
| `steps_mashin`, `steps_mash`, `steps_boil`, `steps_cooldown` | override step types | built-in types |

Set them under **Settings** in the UI, or over the API:

```bash
curl -X PUT -H "Content-Type: application/json" \
     -d '{"name":"MASH_TUN","value":"<kettle-id>"}' \
     http://<pi>:8000/config/MASH_TUN/
```

Kettle IDs come from `GET /kettle/`.

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
| `RECIPE/NAME` | profile name |
| `RECIPE/BOIL_TIME` | boil step timer, minutes |
| `RECIPE/MASH/MASH_STEPS/MASH_STEP/NAME` | step name |
| `…/MASH_STEP/STEP_TEMP` | step target temperature |
| `…/MASH_STEP/STEP_TIME` | step duration, minutes |
| `RECIPE/HOPS/HOP` with `USE=Boil` | hop alert during the boil |
| `RECIPE/HOPS/HOP` with `USE=First Wort` | first-wort alert on the boil step |
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

### Hop and misc timings

`TIME` is **minutes remaining in the boil**, not elapsed. A 60-minute addition goes
in at the start of a 60-minute boil.

Hops and miscs are merged into one list, sorted by descending time, and assigned to
slots `Hop_1`…`Hop_5`. A misc can therefore land in a slot named "Hop" — that is
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

> **Five boil additions maximum.** Slots run `Hop_1` to `Hop_5`. A sixth is silently
> dropped. There is a `Hop_6` property on the boil step, but nothing populates it.

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

**Upload** → choose your `.xml` → pick the recipe from the list.

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

The example file produces this profile:

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

1. **Read the profile first.** Import is mechanical; it cannot tell you the recipe
   assumed a different rig.
2. **Check each step's kettle and sensor.** These are resolved from your settings at
   import time. Change kettles afterwards and the profile still points at the old
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

---

## Saving and reusing

Import overwrites the active profile, so save anything worth keeping:

```bash
curl http://<pi>:8000/recipe/                    # list saved recipes
curl -X POST http://<pi>:8000/recipe/<name>/brew # load a saved one as the profile
```

Saving to the recipe book stores the profile *as it is now*, including manual edits.
That is usually what you want: import once, adjust for your rig, save, and brew from
the book thereafter.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `GET /upload/xml` returns `[]` after a successful upload | Content type was not `text/xml`. Re-upload with `;type=text/xml`. |
| Import appears to do nothing | `MASH_TUN` not set. Check notifications. |
| Boil runs at the wrong temperature | It comes from `steps_boil_temp`, not the recipe. |
| A hop addition is missing | More than five boil additions; only `Hop_1`–`Hop_5` exist. |
| Whirlpool runs 15 min instead of what I wrote | Aroma `TIME` is ignored; the wait is hardcoded. |
| Mash temperatures lost their decimals | Step temperatures are truncated to integers on import. |
| Steps point at the wrong kettle | Kettles are resolved at import time. Re-import after changing them. |
| My saved recipe vanished | Import overwrites the active profile. Save to the recipe book first. |
