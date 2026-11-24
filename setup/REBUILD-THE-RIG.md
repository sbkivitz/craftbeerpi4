# Rebuilding the brewing rig's Raspberry Pi

This takes the rig's Pi from a blank SD card to the state it runs now. It also says what to
back up from the Pi, how often, and how.

Read the whole file once before you start. The safety steps in part 7 matter more than
anything else here.

## 1. What the rig is

### The brewery

- An electric three-vessel HERMS: a hot liquor tank (HLT), a mash tun and a boil kettle
  (BK). All three are 20 gallon Spike Brewing stainless kettles, the original model, about
  33 lb each empty. A usual batch is 10 gallons. Temperatures are in °F everywhere.
- HERMS means the mash is pumped through a coil inside the HLT, so the HLT's element heats
  the mash. The mash tun has no element of its own.
- Two elements, each switched by a solid-state relay (SSR) that a Pi pin drives:
  - the HLT element, measured at 5,346 W (243 V × 22 A, 10 Oct 2026);
  - the boil kettle element, measured at 5,315.8 W.
- A mechanical selector switch powers only one element at a time: the HLT's or the boil
  kettle's, never both. The software can't see where it is set.
- Two pumps:
  - the **HLT pump** (`Recirc_PUMP` in CraftBeerPi) stirs the HLT, and during a fly sparge
    sends HLT water through the coil to the mash tun. It has a **physical ON override that
    bypasses the Pi**, so the Pi saying "off" doesn't prove it is off;
  - the **wort pump** (`Wort PUMP`) runs the mash through the coil and moves wort to the
    boil kettle.
- Three DS18B20 temperature probes on one 1-Wire bus, wired with their own power and
  ground (three wires each). The mash tun's probe sits at the HERMS coil's outlet, where
  the wort returns to the tun, not in the grain bed.

Sources: `RIG-NOTES.md` sections 16 and 18 and `AGENT-PRIMER.md` section 1 in the
private test bench; the rig's reports of 9, 10, 11 Oct and 22 Nov 2026 in the private
`cbpi-rig-comms` repository.

### The Pi

| | |
|---|---|
| Model | **Not recorded.** The owner describes it as a Raspberry Pi 3 or older. It isn't a Pi 5: it uses the RPi.GPIO library, which CraftBeerPi installs on any Pi but a Pi 5. `cat /proc/device-tree/model` on the Pi says which it is: write it here. |
| Clock board | None. The Pi sets its clock from internet time servers after it starts. |
| UPS | None. |
| OS | Raspberry Pi OS "Trixie" (Debian 13) |
| Python | 3.13.5 |
| Login user | `pi`. Every path below assumes `/home/pi`. |
| CraftBeerPi install | pipx, in `/home/pi/.local/share/pipx/venvs/cbpi4/`, started as `/home/pi/.local/bin/cbpi` |
| Service | systemd unit `craftbeerpi`, running as root, working folder `/home/pi` |
| Settings folder | `/home/pi/config` |
| Temperature logs | `/home/pi/logs` |
| Web page | port 8000 on the Pi's address |

The Pi's network address and name are private. The owner keeps them; they are also in
`AGENT-PRIMER.md`, section 0, in the private test bench.

### Pins

| Pi pin (GPIO number) | Header pin | What it drives | CraftBeerPi actor | Actor type and settings |
|---|---|---|---|---|
| GPIO 4 | 7 | The 1-Wire bus: all three probes | (none: a boot setting) | `dtoverlay=w1-gpio` |
| GPIO 5 | 29 | Boil kettle element's SSR | `BK_HEAT` | GPIOPWMActor, Frequency 0.5, Inverted No |
| GPIO 6 | 31 | HLT element's SSR | `HLT_HEAT` | GPIOPWMActor, Frequency 0.5 |
| GPIO 7 | 26 | HLT pump | `Recirc_PUMP` | GPIOActor, Inverted No |
| GPIO 8 | 24 | Wort pump | `Wort PUMP` | GPIOActor, Inverted No |

No two actors share a pin. GPIO 7 and 8 are also the SPI bus's chip selects, so SPI must
stay off (part 3). The header pin numbers are the standard 40-pin layout; the rig's
wiring hasn't been traced against them.

Frequency 0.5 means each element runs a 2-second on/off cycle. The rig was once found at
30 Hz; keep 0.5 (`RIG-NOTES.md` section 18).

### Probes

| CraftBeerPi sensor | Where | Offset | Read every |
|---|---|---|---|
| `Temp_HLT` | HLT | +0.23 °F | 1 s |
| `Temp_Mash` | Mash tun, at the HERMS coil's outlet | +4.0 °F | 1 s |
| `BK` | Boil kettle | +1.0 °F | 1 s |

The offsets are not a calibration. On 22 Nov the owner questioned the mash tun's and the
boil kettle's: at room temperature, without offsets, all three read within 0.2 °F of each
other. Nothing was changed. Each probe's 1-Wire ID is in the Pi's `config/sensor.json`; a
new probe has a new ID, so a replaced probe has to be chosen again on the Hardware page.

### Kettles

| Kettle | Logic (AUTO) | Probe | Element | Pump (agitator) |
|---|---|---|---|---|
| HLT | PIDBoil | `Temp_HLT` | `HLT_HEAT` | `Recirc_PUMP` |
| Mash Tun | PID_HERMS | `Temp_Mash` | `HLT_HEAT` | `Wort PUMP` |
| Boil Kettle | BoilPower | `BK` | `BK_HEAT` | none |

The HLT and the mash tun share the HLT element. Never run both kettles' AUTO at once: they
would fight over the element every second.

Each kettle's settings, as the rig held them on 22 Nov 2026 (its own settings-file export,
saved in `cbpi-rig-comms`, `rig/2026-11-22.md`):

- **HLT, PIDBoil:** P 15, I 0.004167, D 0, SampleTime 2, Max_Output 100,
  Boil_Threshold 208, Max_Boil_Output 85, Boil_Plateau_Minutes 0.
- **Mash Tun, PID_HERMS:** HLT_Sensor `Temp_HLT`, DeltaTemp 5.4, MashIn_DeltaTemp 12,
  P 2, I .005, D 0, HLT_P 15, HLT_I .004167, HLT_D 0, SampleTime 2, Max_Pump_Temp 207,
  Max_Output 100, Max_Boil_Output 85, Max_Boil_Temp 208.4, Max_PID_Temp 190.4,
  Sensor_Stale_Seconds 600, Rest_Interval 600, Rest_Time 60, Pump_Rest No.
  The rig update page also recommends HLT_Kettle = the HLT and MashIn_Pump_Within 10;
  the rig doesn't have those two set yet, and the owner deferred the decision.
- **Boil Kettle, BoilPower:** Boil_Threshold 210, Boil_Power 53, Max_Output 100, P, I
  and D empty (BoilPower then uses 14, 0.03 and 900), SampleTime 2.

Why each value is what it is: the rig update page, steps 8 and 8b. The HLT gains
(15 / 0.004167 / 0) were worked out from measurements of this rig. They are not the
autotuned 60.63 / 0.1846 / 16.59 that the rig once held: don't put those back.

### Settings that matter on this rig

From the Settings page. Checked on the rig on 22 Nov unless marked.

| Setting (key) | Value | Why |
|---|---|---|
| Temperature unit (`TEMP_UNIT`) | F | Everything the rig reports is in °F. Not read back by name |
| Boil temperature (`steps_boil_temp`) | 210 | The rig boils at 211.3–211.9 °F; at 212 a boil step's timer may never start |
| Switch with a hand override (`BREWDAY_OVERRIDE_ACTOR`) | `Recirc_PUMP` | The HLT pump is the one with the physical override |
| Start the HLT pump when AUTO or the HERMS heats the HLT (`HLT_PUMP_WITH_HEAT`) | No, for now | Set to No on 22 Nov because switching the HLT element on by hand also started the pump. A fix is being built; the owner decides when it goes back to Yes |
| Save readings to log files (`CSVLOGFILES`) | Yes | BrewScribe copies a finished brew's temperatures from these logs |
| Largest log file (`SENSOR_LOG_MAX_BYTES`) | 500000 | About a day of readings per probe |
| Old log files kept (`SENSOR_LOG_BACKUP_COUNT`) | 5 | |
| Whirlpool temperature (`steps_whirlpool_temp`) | 170 | The owner's choice for every recipe. Not read back from the rig |
| Sparge step type (`steps_sparge`) | SpargeStep | Recommended by the rig update page, step 9. Not read back from the rig |
| Add a mash out if the recipe has none (`AddMashOutStep`) | Yes | Recommended by the rig update page, step 9. Not read back from the rig |

`SIM_TIME_SCALE` must not exist on the rig. Only the simulator plugin adds it, and it
speeds up every timer. Never copy a settings folder from the workstation to the Pi.

## 2. What the rig runs: rig update rev 69

These are the versions the latest published rig update page recommends. The rig installed
them on 21–22 Nov 2026 and reported them back.

```text
RECOMMENDED VERSIONS rig-update.html rev 69
cbpi4==4.7.7.post48
cbpi4gui==0.5.1a3.post25
cbpi4-PIDHerms==0.0.9.post9
cbpi4-boilpower==0.0.3.post12
cbpi4-PIDBoil==0.0.9.post5
cbpi4-PID_AutoTune==0.1.1.post6
cbpi4_BrewStart==1.2.0.post7
cbpi4-BrewScribe==0.1.0.dev48
END RECOMMENDED VERSIONS
```

| Package | What it is | Branch the rig installs |
|---|---|---|
| cbpi4 | CraftBeerPi itself, the server | `master` |
| cbpi4gui | The web interface (ships already built) | `main` |
| cbpi4-PIDHerms | The mash tun's HERMS logic | `main` |
| cbpi4-boilpower | The boil kettle's logic | `master` |
| cbpi4-PIDBoil | The HLT's logic | `main` |
| cbpi4-PID_AutoTune | Relay autotune (not used for the rig's gains) | `main` |
| cbpi4_BrewStart | Heats water by a set time ("Ready by") | `wall-clock-and-i18n`, whose full name is in the install line below |
| cbpi4-BrewScribe | The brew-day log. Private repository | `main` |

Never on the rig:

- **cbpi4-simvessel**, the simulated kettles. On the rig it could let a simulated vessel
  be chosen by mistake on brew day.
- cbpi4-PushOver (phone alerts) isn't on the rig yet. It is planned for the next update,
  from its `phone-alerts` branch.
- cbpi4-BM_PID_SmartBoilWithPump and cbpi4-IFM-SM-Flow-Sensor: not used. The rig once had
  a flow sensor set up as an experiment; the owner had it deleted on 22 Nov.

A newer rig update page replaces this table. Its step 4 is always the authority on what to
install.

## 3. Set up the Pi

### 3.1 Before you start

1. Switch off, at the rig, the power to both elements and both pumps, including the HLT
   pump's override. Leave them off until part 7 says otherwise.
2. Have to hand: the Pi's address and name, the Wi-Fi details, and a login password (the
   owner keeps all of these), and your latest Pi backup (part 6).

### 3.2 Flash the SD card

1. On any computer, install Raspberry Pi Imager from https://www.raspberrypi.com/software/.
2. Choose the Pi model, then Raspberry Pi OS (the release called Trixie, Debian 13), then
   the SD card.
3. In the settings it offers: user name `pi`, the password, the Pi's name, the Wi-Fi
   details if the Pi isn't on a cable, and **Enable SSH**.
4. Write the card, put it in the Pi, and start it.

**Not tested.** The OS and user come from the rig's own reports (`RIG-NOTES.md` section
16); whether the rig has the desktop or the Lite version isn't recorded, and either works.

### 3.3 First login

From another computer:

```bash
ssh pi@<the Pi's address>
sudo apt update && sudo apt full-upgrade -y
cat /etc/os-release | grep VERSION_CODENAME
python3 --version
cat /proc/device-tree/model; echo
```

You should see `VERSION_CODENAME=trixie` and `Python 3.13.x`. Write the model line into the
table in part 1.

### 3.4 The tools CraftBeerPi needs to install

```bash
sudo apt install -y git pipx libsystemd-dev pkg-config build-essential python3-dev
pipx ensurepath
```

Log out and back in, so `~/.local/bin` is on your PATH.

`libsystemd-dev`, `pkg-config`, `build-essential` and `python3-dev` are there because
CraftBeerPi needs the `systemd-python` package on Linux, and pip has to build it.
**Tested on Linux:** without `libsystemd-dev`, `pipx install` failed with
"Failed to build 'systemd-python'"; with it, the install went through. **Not tested on a
Pi**, where a ready-built package might make them unnecessary. They do no harm.

### 3.5 Turn on the 1-Wire bus, and keep SPI off

1. Open the boot settings: `sudo nano /boot/firmware/config.txt`.
2. Add this line at the end, then save:

   ```text
   dtoverlay=w1-gpio
   ```

   That puts the probes' bus on GPIO 4, header pin 7, as on the rig.
3. Make sure the file has **no** `dtparam=spi=on` line, and no `gpio=` line naming 5, 6, 7
   or 8. SPI would take GPIO 7 and 8, the pumps' pins.
4. `sudo reboot`, log in again, and look for the probes:

   ```bash
   ls /sys/bus/w1/devices/
   ```

   You should see `w1_bus_master1` and one `28-…` entry per connected probe: three.

**Not tested.** The line is the rig's, as the coordinator recorded it (session checkpoint
56) and `RIG-NOTES.md` section 18 describes the wiring. CraftBeerPi's own
`cbpi onewire --setup` adds a longer form,
`dtoverlay=w1-gpio,gpiopin=4,pullup=on`; the rig's probes have worked with the short one.

## 4. Install CraftBeerPi and the plugins

1. Install the server:

   ```bash
   pipx install git+https://github.com/sbkivitz/craftbeerpi4.git@master
   ```

2. Install each plugin into the same environment:

   ```bash
   pipx inject cbpi4 --force git+https://github.com/sbkivitz/craftbeerpi4-ui.git@main
   pipx inject cbpi4 --force git+https://github.com/sbkivitz/cbpi4-PIDHerms.git@main
   pipx inject cbpi4 --force git+https://github.com/sbkivitz/cbpi4-boilpower.git@master
   pipx inject cbpi4 --force git+https://github.com/sbkivitz/cbpi4-PIDBoil.git@main
   pipx inject cbpi4 --force git+https://github.com/sbkivitz/cbpi4-PID_AutoTune.git@main
   pipx inject cbpi4 --force git+https://github.com/sbkivitz/cbpi4_BrewStart.git@sbkivitz/wall-clock-and-i18n
   ```

3. Check what went in:

   ```bash
   pipx list --include-injected
   ```

   You should see cbpi4 and the six plugins at the versions in part 2.

4. Check the files are the new ones. This is the rig update page's step 5 check; every
   line must print `True`:

   ```bash
   CBPI_PY="$(head -1 "$(which cbpi)" | sed 's|^#!||; s| .*||')"
   echo "$CBPI_PY"
   ```

   Then paste the block from step 5 of the rig update page. On a version 4.7.7.post48
   install it prints seventeen lines ending `True`, then `loaded from`.

**Tested on Linux** on 23 Nov 2026 with rev 69's branches: the install and all six
injects worked, `pipx list` showed exactly the versions in part 2, and step 5's check
printed seventeen `True`s.

Never use plain `pip3` or `python3` for any of this. They are the system's Python, not the
one CraftBeerPi runs in, and they would install into or check the wrong place
(`RIG-NOTES.md` section 16 tells that story).

BrewScribe comes last, in part 8.

## 5. The settings folder and the service

### 5.1 The settings folder

If you have a backup, restore it as in part 6.3 and skip to 5.2.

With no backup, make an empty settings folder:

```bash
cd ~
cbpi setup
ls ~/config
```

You should see `actor.json`, `config.json`, `config.yaml`, `kettle.json`, `sensor.json`,
`step_data.json` and a few folders. **Tested on Linux.** Then, once the server is running
(5.2), set up the actors, probes and kettles from part 1 on the Hardware page, and the
settings from part 1 on the Settings page. If you have the three kettle settings files
(the rig's exports are in `cbpi-rig-comms`, `rig/2026-11-22.md`), save each as a `.json`
file and load it on the Hardware page with **LOAD FROM FILE…**: it shows what would change
before it changes anything.

### 5.2 The service

The rig runs upstream CraftBeerPi's service file plus a small add-on file. Write both by
hand. Don't use `cbpi autostart on`: this fork's version of it writes a different file,
with a watchdog line that hasn't been tried on a real Pi.

1. The service file:

   ```bash
   sudo tee /etc/systemd/system/craftbeerpi.service >/dev/null <<'EOF'
   [Unit]
   Description=Craftbeer Pi
   After=mosquitto.service influxdb.service

   [Service]
   WorkingDirectory=/home/pi
   ExecStartPre=/bin/sleep 5
   ExecStart=/home/pi/.local/bin/cbpi start
   KillSignal=SIGKILL
   TimeoutStopSec=15

   [Install]
   WantedBy=multi-user.target
   EOF
   ```

2. The add-on file, which makes a stop switch every actor off and brings the server back
   after a crash:

   ```bash
   sudo mkdir -p /etc/systemd/system/craftbeerpi.service.d
   sudo tee /etc/systemd/system/craftbeerpi.service.d/stop-and-restart.conf >/dev/null <<'EOF'
   # From rig-update.html step 3b, approved on 23 Oct 2026.
   # A stop asks the server to shut down instead of killing it, and its shutdown
   # switches every actor off. A crash restarts it after 5 s with its outputs
   # off, and it never gives up restarting.
   [Unit]
   StartLimitIntervalSec=0

   [Service]
   Restart=always
   RestartSec=5
   KillSignal=SIGTERM
   EOF
   sudo systemctl daemon-reload
   sudo systemctl enable craftbeerpi
   ```

3. Check it:

   ```bash
   systemctl show craftbeerpi -p KillSignal -p Restart -p RestartUSec \
     -p StartLimitIntervalUSec -p TimeoutStopUSec -p DropInPaths
   ```

   You should see `KillSignal=15`, `Restart=always`, `RestartUSec=5s`,
   `StartLimitIntervalUSec=0`, `TimeoutStopUSec=15s`, and `DropInPaths=` naming only
   `stop-and-restart.conf`. That is what the rig read back on 22 Nov.

**Not tested** as a pair on a Pi. The service file is upstream's template
(`cbpi/config/craftbeerpi.template` on upstream's `master`) filled in for user `pi` and a
pipx install, which is what the rig update page says the rig has. The add-on file is the
rig update page's step 3b, word for word, and the rig reported the values above. If you
have the Pi backup from part 6, its copy of the service file is better than this one.

## 6. Backups

### 6.1 What to back up

| What | Where on the Pi | Why |
|---|---|---|
| The settings folder | `/home/pi/config` | Everything CraftBeerPi knows: actors, probes, kettles and their settings, Settings, recipes, the loaded recipe, timers, strike water figures, and BrewScribe's brew history (`config/brewscribe`, the only copy) |
| The temperature logs | `/home/pi/logs` | The readings; BrewScribe copies a finished brew's from here |
| The service files | `/etc/systemd/system/craftbeerpi.service` and `/etc/systemd/system/craftbeerpi.service.d/` | Part 5.2 |
| The boot settings | `/boot/firmware/config.txt` | The 1-Wire line |
| The list of installed packages | the output of `pipx list --include-injected` | What to install again |

The BrewScribe key in `/home/pi/.git-credentials` is not backed up on purpose. Make a new
one instead (rig update page, step 11b).

### 6.2 When, and how

Back up after every rig update, after any change of settings, and after each brew day once
the brew is finished in BrewScribe. Keep the copies **off the Pi**, in at least two places,
and private: they hold your recipes and brew notes.

1. At the rig, with no recipe running and every kettle's AUTO off, stop the server:

   ```bash
   sudo systemctl stop craftbeerpi
   systemctl is-active craftbeerpi
   ```

   You should see `inactive`.

2. Make one file of it all:

   ```bash
   cd ~
   pipx list --include-injected > pipx-list.txt 2>&1
   sudo tar -czf "cbpi-backup-$(date +%Y%m%d-%H%M).tgz" config logs pipx-list.txt \
     /etc/systemd/system/craftbeerpi.service /etc/systemd/system/craftbeerpi.service.d \
     /boot/firmware/config.txt
   sudo chown pi: cbpi-backup-*.tgz
   sudo systemctl start craftbeerpi
   ```

   `sudo` is needed because the server runs as root, so some of its files belong to root.
   tar says "Removing leading `/' from member names": that is expected.

3. From another computer, copy it off the Pi (Windows has `scp` built in):

   ```bash
   scp "pi@<the Pi's address>:cbpi-backup-*.tgz" .
   ```

**Not tested on the Pi.** The `tar` and `scp` lines are standard; the files to include are
the ones the rig update page's step 2 backs up, plus the logs and system files.

The rig update page's step 2 also copies the settings folder before every update
(`config.backup-<date>`), but that copy stays on the Pi, so it is lost with the SD card.

### 6.3 Restore

On the rebuilt Pi, with the server stopped and the backup copied to `/home/pi`:

```bash
sudo systemctl stop craftbeerpi
cd ~
[ -d config ] && sudo mv config "config.before-restore-$(date +%Y%m%d-%H%M)"
sudo tar -xzf cbpi-backup-<date>.tgz config logs
```

Then compare the backup's service files and `config.txt` line with part 5.2 and part 3.5,
and don't start the server until part 7.

**Not tested.**

### 6.4 What exists today

As of 23 Nov 2026 **there is no complete backup of the rig off the Pi.** The complete copy
the rig update made on 21 Nov, `config.backup-complete-20261121-231314`, is on the Pi's own
SD card. Off the Pi there are only:

- the three kettle settings files, in `cbpi-rig-comms`, `rig/2026-11-22.md`;
- an export of the rig's actors, probes, kettles and settings from 11 Oct, in
  `cbpi-rig-comms`, `rig/evidence-20261011/probe-fault-evidence.zip`;
- whatever the rig-side AI keeps in its own session folder on the brewery laptop.

The first backup in 6.2 closes that gap.

## 7. First power-on: check before anything can heat

Do all of this with the element and pump power still **off at the rig** (3.1). No AI does
any of it on its own: an AI may read and report, but switching anything on, and starting
or stopping the service or a kettle's AUTO, is for the owner, at the rig.

1. Start the server and look at its log:

   ```bash
   sudo systemctl start craftbeerpi
   sleep 15
   systemctl is-active craftbeerpi
   journalctl -u craftbeerpi -n 40 --no-pager
   ```

   You should see `active`. "Missing OneWire sensors" is expected for an unplugged probe.

2. Every actor must read off:

   ```bash
   curl -s localhost:8000/actor/ | python3 -m json.tool | grep -E '"name"|"state"'
   ```

   Every `"state"` should be `false`. CraftBeerPi never switches an actor back on after a
   start, by design. If anything says `true`, stop and find out why.

3. Every kettle's AUTO must be off, and no recipe step running:

   ```bash
   curl -s localhost:8000/kettle/ | python3 -c "import sys,json; [print(k['name'], k['type'], 'AUTO ON' if k['state'] else 'off') for k in json.load(sys.stdin)['data']]"
   curl -s localhost:8000/step2/ | python3 -c "import sys,json; [print(s['name'], s['type'], s['status']) for s in json.load(sys.stdin)['steps']]"
   ```

   Each kettle should say `off`. Each step's status should be `I` (not started) or `D`
   (done); an `A` is a step running. A restored backup can hold a running BrewStart "Ready
   by" step, which carries on heating by itself after a restart for up to two hours past
   its ready time: if you see one, press STOP on the brew-day screen before power goes back
   on.

4. Each actor has a pin of its own:

   ```bash
   curl -s localhost:8000/actor/ | python3 -c "
   import sys,json
   for a in json.load(sys.stdin)['data']:
     print(a['name'], a.get('type'), (a.get('props') or {}).get('GPIO'))
   "
   ```

   You should see 5, 6, 7 and 8, each once, matching the table in part 1.

5. The plugins registered:

   ```bash
   curl -s localhost:8000/kettle/ | python3 -c "import sys,json; print(sorted(json.load(sys.stdin)['types']))"
   curl -s localhost:8000/step2/ | python3 -c "import sys,json; print('BrewStart' in json.load(sys.stdin)['types'])"
   ```

   Expect `['BoilPower', 'Hysteresis', 'PIDAutoTune', 'PIDBoil', 'PID_HERMS']`, then `True`.

6. The clock is trusted (the brew-day timers, the check START makes, and Ready by need
   this):

   ```bash
   CBPI_USER="$(systemctl show craftbeerpi -p User --value)"
   echo "CraftBeerPi runs as: ${CBPI_USER:-root}"
   sudo -u "${CBPI_USER:-root}" timedatectl show --property=NTPSynchronized --value
   ```

   You should see `CraftBeerPi runs as: root`, then `yes`. `no` just after a start means
   the time hasn't arrived from the internet yet: ask again in a minute.

7. Nothing else drives the pins. These are read-only:

   ```bash
   sudo fuser -v /dev/gpiomem /dev/mem 2>&1; pgrep -af python
   grep -nE '^[[:space:]]*(gpio=|dtparam=spi|dtoverlay=)' /boot/firmware/config.txt
   pinctrl get 5-8 2>/dev/null || raspi-gpio get 5-8
   ```

   The CraftBeerPi server should be the only program using the pins, and the boot settings
   should show only the `dtoverlay=w1-gpio` line. The log's "This channel is already in
   use, continuing anyway" warning is known on the rig and not yet explained: these checks
   are how to narrow it down (the coordinator's note to the rig of 22 Nov, in
   `cbpi-rig-comms`, `workstation/2026-11-22.md`).

8. The probes read: open the brew-day screen at `http://<the Pi's address>:8000`, and with
   every vessel at room temperature press **COMPARE PROBES**. Each row shows the offset from
   part 1. The HLT and the mash tun should agree within about a degree.

**Tested** on the workstation's simulator: steps 2 to 5's commands, against its own
server, printed what they should. **Not tested on the Pi.** Steps 1, 6 and 7 come from the
rig update page (steps 3, 6 and 7) and the coordinator's GPIO note.

Only when all eight look right: the owner, at the rig, puts the element and pump power
back, and does the rig update page's step 11, the supervised test with water. Don't brew
before that has passed.

## 8. BrewScribe, last

BrewScribe's repository is private, so the Pi needs a key that can only read it. Follow the
rig update page's step 11b exactly: it says how to make the key on GitHub, store it on the
Pi without it showing, install, and check. Do it only after step 11 has passed. Never put
the key in a command, a file in a repository, or a message to anyone, an AI included.

Once the key is stored for user `pi`, the install itself is one line, with the server
stopped:

```bash
sudo systemctl stop craftbeerpi
pipx inject cbpi4 --force git+https://github.com/sbkivitz/cbpi4-BrewScribe.git@main
sudo systemctl start craftbeerpi
```

Then do step 11b's checks. BrewScribe's data is in `/home/pi/config/brewscribe`, which the
backup in part 6 includes.

**Not tested** here, as it needs the Pi's key. The line is step 11b's; the rig ran it on
22 Nov.

## 9. Check the result

From a computer with the private test bench and `cbpi-rig-comms` cloned (WORKSTATION.md),
after `.\read.ps1` in the `cbpi-rig-comms` clone:

```powershell
python rig_check.py --base http://<the Pi's address>:8000 --recommended
```

It only reads. It compares the rig with the newest recommended versions posted in
`cbpi-rig-comms`, checks which kettle logics registered, whether any actor is on, and
whether two actors claim one pin. It exits 0 when nothing needs attention.

**Not tested** against the rig while writing this. Source: `rig_check.py`'s own
description, and the rig's report of 22 Nov ("0 problems").

Then make a backup (part 6.2).
