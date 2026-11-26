# The development workstation

The workstation is the Windows PC where the software is changed, tested and tried on two
simulated breweries before anything goes to the rig. This file says what is on it and how to
put it back.

Everything lives under `C:\src\personal\`. The paths below assume that; if you choose
another folder, some tools (the bench's own scripts, `installed_sync.py`) have it written in
and need the same change.

## 1. Programs to install

| Program | Used for | What the workstation has now |
|---|---|---|
| Git | Everything | 2.x |
| GitHub CLI (`gh`) | Signing in to GitHub to push and to clone the private repositories | |
| Python 3.12 (with the `py` launcher) | CraftBeerPi, the bench, the tools | 3.12.10 in the project's venv |
| Node.js and npm | Building the web interface | Node 26.13.0, npm 11.20.0 |
| PowerShell 7 (`pwsh`) | Some scripts; Windows PowerShell 5.1 runs the bench | |
| Playwright's Chromium | The bench's browser tests | installed into the venv (part 4) |
| WSL with Ubuntu 24.04 | One bench test runs a real systemd; checking Linux installs | Ubuntu-24.04 |

Sign in to GitHub once, as the owner: `gh auth login`.

## 2. The repositories

| Folder in `C:\src\personal\` | GitHub | Branch that matters | What it is |
|---|---|---|---|
| `craftbeerpi4` | https://github.com/sbkivitz/craftbeerpi4.git (public) | `master`: what the rig installs once pushed, and what simulator 8000 runs | CraftBeerPi's server. This `setup` folder is in it |
| `craftbeerpi4-graphs` | the same repository, a second working folder (git worktree) | `brewday-graphs`: kept equal to `master`; simulator 8012 runs it | |
| `craftbeerpi4-ui` | https://github.com/sbkivitz/craftbeerpi4-ui.git (public) | `main`: what the rig installs once pushed | The web interface, its source and its built bundle, and the approved designs in `design\` |
| `craftbeerpi4-ui-graphs` | the same repository, a worktree | `brewday-graphs`: fast-forwarded to `main` before 8012 restarts; 8012 serves its build | |
| `cbpi-testbench` | https://github.com/sbkivitz/cbpi-testbench.git (private) | `master` | The tests, the rig update page and its generator, the rig notes, simulator 8000's settings, and the `coordination` folder (part 6) |
| `cbpi-rig-comms` | https://github.com/sbkivitz/cbpi-rig-comms.git (private) | `main` | The log between the rig side and the workstation |
| `_cbpi_plugins\cbpi4-PIDHerms` | https://github.com/sbkivitz/cbpi4-PIDHerms.git | `main` | The mash tun's HERMS logic |
| `_cbpi_plugins\cbpi4-PIDBoil` | https://github.com/sbkivitz/cbpi4-PIDBoil.git | `main` | The HLT's logic |
| `_cbpi_plugins\cbpi4-PID_AutoTune` | https://github.com/sbkivitz/cbpi4-PID_AutoTune.git | `main` | Relay autotune |
| `_cbpi_plugins\cbpi4-boilpower` | https://github.com/sbkivitz/cbpi4-boilpower.git | `master` | The boil kettle's logic (the owner's own plugin, not a fork) |
| `_cbpi_plugins\cbpi4_BrewStart` | https://github.com/sbkivitz/cbpi4_BrewStart.git | the branch ending `/wall-clock-and-i18n` (the rig's); newer work on `next`, in the worktree `_cbpi_plugins\cbpi4_BrewStart-next` | Heats water by a set time |
| `_cbpi_plugins\cbpi4-PushOver` | https://github.com/sbkivitz/cbpi4-PushOver.git | `phone-alerts` | Phone alerts (ntfy by default, or Pushover). Not on the rig yet: rig update rev 71 installs it |
| `_cbpi_plugins\cbpi4-simvessel` | https://github.com/sbkivitz/cbpi4-simvessel.git (private) | `master` | The simulated kettles. Never goes on the rig |
| `_cbpi_plugins\cbpi4-BrewScribe` | https://github.com/sbkivitz/cbpi4-BrewScribe.git (private) | `main` = released; `impeccable-redesign` = development builds, in the worktree `_cbpi_plugins\cbpi4-BrewScribe-impeccable` | The brew-day log. Porter's (AI-SEED.md) |
| `_cbpi_plugins\cbpi4-BM_PID_SmartBoilWithPump` | https://github.com/sbkivitz/cbpi4-BM_PID_SmartBoilWithPump.git | `main` | Not used |
| `_cbpi_plugins\cbpi4-IFM-SM-Flow-Sensor` | https://github.com/walkuf/cbpi4-IFM-SM-Flow-Sensor.git (someone else's) | `main` | Not used |
| `craftbeerpi4-documentation` | https://github.com/sbkivitz/craftbeerpi4-documentation.git | `docs/hardened-control-and-safety` | A fork of the documentation, with a proposed safety section |

For the forks of other people's projects, the remote called `fork` is the owner's copy on
GitHub and `origin` is the original project. **Never push to `origin`** on those. The
owner's own repositories (boilpower, simvessel, BrewScribe, the bench, rig-comms) have only
`origin`.

The other `craftbeerpi4-*`, `craftbeerpi4-ui-*` and plugin `-*` folders are worktrees
helpers made for one piece of work each. They hold nothing that isn't on a branch. A new PC
doesn't need them.

### Cloning them again

1. The core, with the remote names the tools expect, and 8012's worktree:

   ```powershell
   cd C:\src\personal
   git clone https://github.com/sbkivitz/craftbeerpi4.git craftbeerpi4
   git -C craftbeerpi4 remote rename origin fork
   git -C craftbeerpi4 remote add origin https://github.com/PiBrewing/craftbeerpi4.git
   git -C craftbeerpi4 worktree add ..\craftbeerpi4-graphs brewday-graphs
   ```

2. The web interface, the same way:

   ```powershell
   git clone https://github.com/sbkivitz/craftbeerpi4-ui.git craftbeerpi4-ui
   git -C craftbeerpi4-ui remote rename origin fork
   git -C craftbeerpi4-ui remote add origin https://github.com/PiBrewing/craftbeerpi4-ui.git
   git -C craftbeerpi4-ui worktree add ..\craftbeerpi4-ui-graphs brewday-graphs
   ```

3. The bench and the rig log (private: `gh auth login` first). Then run `.\setup.ps1`
   once in `cbpi-rig-comms`, which sets git up to sign in through `gh`:

   ```powershell
   git clone https://github.com/sbkivitz/cbpi-testbench.git cbpi-testbench
   git clone https://github.com/sbkivitz/cbpi-rig-comms.git cbpi-rig-comms
   ```

4. Each plugin into `C:\src\personal\_cbpi_plugins\`, with `git clone <URL from the
   table> <folder name from the table>`. For a fork, rename `origin` to `fork` as above.
   BrewStart's rig branch:

   ```powershell
   cd C:\src\personal\_cbpi_plugins
   git clone https://github.com/sbkivitz/cbpi4_BrewStart.git cbpi4_BrewStart
   git -C cbpi4_BrewStart remote rename origin fork
   git -C cbpi4_BrewStart switch --track (git -C cbpi4_BrewStart branch -r --list 'fork/*wall-clock-and-i18n').Trim()
   ```

   For PushOver: `git -C cbpi4-PushOver switch phone-alerts`.

**Tested** on 23 Nov 2026 in a throwaway folder: steps 1 and 2 (without step 2's
`remote add`), the PIDHerms clone, and the BrewStart lines. The core and its worktree came
out at the commit the rig runs; BrewStart ended on its `wall-clock-and-i18n` branch.

## 3. The design folders

The approved designs builders follow are in the web interface repository, on `main`:

| Folder | What |
|---|---|
| `C:\src\personal\craftbeerpi4-ui\design\brewday` | Brew Day, the main brewing screen: `DESIGN.md` (the look and its rules), `BUILD-NOTES.md` (each change as it should be built), `PRODUCT.md`, `direction-log.md`, and one file per feature |
| `C:\src\personal\craftbeerpi4-ui\design\hardware` | The Hardware page |
| `C:\src\personal\craftbeerpi4-ui\design\recipes` | The recipe pages |
| `C:\src\personal\craftbeerpi4-ui\design\analytics` | The FIGURES page, which replaced Analytics |
| `C:\src\personal\craftbeerpi4-ui\design\settings` | The Settings page |

Designers' proposals in progress (clickable mock-ups) are in their own worktrees, for
example `C:\src\personal\craftbeerpi4-ui-impeccable\proposal`. A proposal is only a
proposal until the owner approves it and it is copied into `design\`.

## 4. The Python environments (venvs)

There are two, with the same packages installed the same way:

| Environment | Used by | Who installs into it |
|---|---|---|
| `C:\src\personal\craftbeerpi4\.venv` | The test bench: every test, and the servers the tests start | Anyone working on the bench, but only while holding the bench's "suites" lock, because running tests import from it |
| `C:\src\personal\sims-venv` | The two simulators, 8000 and 8012 (part 7) | Only the coordinator, by bringing it in step with `installed_sync.py` (part 5). Helpers never install into it |

Why two: when the simulators shared the bench's environment, putting a new build on them
had to wait for the bench's lock, which on a busy evening meant hours, and an install for a
test could change what the owner was trying on a simulator. Now the simulators are updated
when the owner wants, and the bench's installs never touch them.

Make each one the same way. For the bench's, `$venv` is `C:\src\personal\craftbeerpi4\.venv`;
for the simulators', `C:\src\personal\sims-venv`.

1. Make it and install CraftBeerPi from the core folder, editable, so a restart picks up a
   change to the core:

   ```powershell
   $venv = 'C:\src\personal\craftbeerpi4\.venv'
   py -3.12 -m venv $venv
   $py = "$venv\Scripts\python.exe"
   & $py -m pip install --upgrade pip
   & $py -m pip install -e C:\src\personal\craftbeerpi4
   ```

2. Install the web interface and each plugin **not** editable: CraftBeerPi can't find a
   plugin installed with `-e`.

   ```powershell
   foreach ($p in 'craftbeerpi4-ui', '_cbpi_plugins\cbpi4-PIDHerms', '_cbpi_plugins\cbpi4-PIDBoil',
                  '_cbpi_plugins\cbpi4-PID_AutoTune', '_cbpi_plugins\cbpi4-boilpower',
                  '_cbpi_plugins\cbpi4_BrewStart', '_cbpi_plugins\cbpi4-PushOver',
                  '_cbpi_plugins\cbpi4-simvessel') {
       & $py -m pip install --no-deps --force-reinstall "C:\src\personal\$p"
   }
   ```

3. The bench's own needs (the bench's environment only):

   ```powershell
   & $py -m pip install pytest pytest-aiohttp playwright
   & $py -m playwright install chromium
   ```

4. BrewScribe: install the newest wheel Porter released (part 9), or build one from
   `main`, with `pip install --no-deps <wheel>`.

5. Check it with `installed_sync.py` (next section). It should say everything is in sync.

**Tested** on 23 Nov 2026 in a throwaway folder: step 1, then step 2 for the web interface,
PIDHerms and BrewStart. CraftBeerPi and the plugins imported, and `installed_sync.py`
pointed at that venv reported them in sync. Step 3 is **not tested**: the package names are
the ones the bench's environment has now. sims-venv was made the same way (`python -m venv`,
Python 3.12.10) and holds the same packages.

## 5. installed_sync.py: is an environment running what is committed?

`C:\src\personal\cbpi-testbench\installed_sync.py` compares every package an environment
installed from a folder under `C:\src\personal` with that folder's last commit, file by
file. Today it covers `craftbeerpi4` (editable), `craftbeerpi4-ui`, `cbpi4-boilpower`,
`cbpi4_BrewStart`, `cbpi4-PID_AutoTune`, `cbpi4-PIDBoil`, `cbpi4-PIDHerms`,
`cbpi4-PushOver` and `cbpi4-simvessel`. BrewScribe is installed from Porter's wheel, so it
isn't in that list.

```powershell
cd C:\src\personal\cbpi-testbench
& $py installed_sync.py          # report; exit 1 if anything differs
& $py installed_sync.py --fix    # reinstall what differs, from its last commit
```

It checks the bench's environment unless `CBPI_VENV` names another. For the simulators':

```powershell
$env:CBPI_VENV = 'C:\src\personal\sims-venv'
& C:\src\personal\sims-venv\Scripts\python.exe installed_sync.py
```

`--fix` refuses while a server from that environment runs: stop the simulators first
(part 7), or let `restart-sims.ps1` do it.
Windows can't replace a running `cbpi.exe`, and a half-done install leaves CraftBeerPi
unable to start.

## 6. The coordinator's tools, and where their copy lives

The AI coordinator's tools and briefs live in its session folder on this PC,
`%USERPROFILE%\.copilot\session-state\1d63408d-765e-411b-9fd7-0ae2af470eec\files`. If the PC
dies, that folder goes with it. A copy is kept in the private bench, in its `coordination`
folder: its `README.md` lists every file, what it is for and what depends on it, and how to
put the copies back (`refresh.py --restore`).

The most important ones:

| Tool | What it does |
|---|---|
| `test-audit\run_suite.py <group> <test file>` | Runs one suite through the bench's runner, while holding the bench's "suites" lock so only one suite runs at a time |
| `test-audit\try_control.py <group> [filter]` | Tries proposed negative controls (part 8) |
| `test-audit\bench_commit.py <group> <message file> <paths>` | Commits bench files under the bench's "git" lock, with its rules |
| `test-audit\land_core.py <branch>` | Fast-forwards the core's `master`, then `brewday-graphs`, to a branch, under the suites lock |
| `test-audit\merge_ui_controls.py` | Merges a web interface build's proposed controls into the bench |
| `test-audit\CRITERIA.md`, `groups.json` | The shared brief for everyone working on the bench, and which group owns which test |
| `verify-sims.py` | Checks both simulators after a restart |
| `wmi-bypass\sitecustomize.py` | Stops Python hanging on this PC's Windows WMI service. Put its folder on `PYTHONPATH` for every server and test |
| `plans\` | The contracts: what each side of a feature agreed to |
| `release\` | Notes to the rig-side AI for each update, and the simulator restart scripts |

## 7. The two simulators

Two CraftBeerPi servers on this PC stand in for the rig, with simulated kettles
(cbpi4-simvessel) and fake pins. They run the newest code, ahead of the rig, on purpose:
that is where the owner tries changes before they go to the rig. Never set them back to
what the rig runs.

Both run from their own environment, `C:\src\personal\sims-venv` (part 4).

| Port | Speed | Runs | Settings folder | Log |
|---|---|---|---|---|
| 8000 | real time (1×) | the core from `C:\src\personal\craftbeerpi4` and the web interface installed in sims-venv | `C:\src\personal\cbpi-testbench\config` | `%TEMP%\cbpi-sim8000.log` |
| 8012 | 60× (an hour in a minute) | the core from `craftbeerpi4-graphs` and the web interface build in `craftbeerpi4-ui-graphs` | a folder in `%TEMP%` whose name starts `cbpi-brewday-` and ends `-8012`, its `config` subfolder | `%TEMP%\cbpi-brewday-8012.log` |

The speed is the `SIM_TIME_SCALE` setting in each one's settings. Both settings folders
are kept out of git; their copy is in the bench's `coordination\sim-config` folder. 8012's
lives in `%TEMP%`, which Windows may clear: on a new PC, put it somewhere else, for example
a `sim-8012` folder under `C:\src\personal`, and point the commands below at it.

### Starting them

The owner doesn't want console windows popping up, so start them hidden. In PowerShell:

```powershell
$files = "$env:USERPROFILE\.copilot\session-state\1d63408d-765e-411b-9fd7-0ae2af470eec\files"
$cbpi  = 'C:\src\personal\sims-venv\Scripts\cbpi.exe'

# 8000
$psi = [System.Diagnostics.ProcessStartInfo]::new($env:ComSpec)
$psi.Arguments = '/c ""' + $cbpi + '" -c "C:\src\personal\cbpi-testbench\config" start > "%TEMP%\cbpi-sim8000.log" 2>&1"'
$psi.WorkingDirectory = 'C:\src\personal\cbpi-testbench'
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.Environment['PYTHONPATH'] = "$files\wmi-bypass"
$psi.Environment['PYTHONUTF8'] = '1'
$psi.Environment['PYTHONIOENCODING'] = 'utf-8'
[void][System.Diagnostics.Process]::Start($psi)

# 8012
$sim8012 = (Get-ChildItem $env:TEMP -Directory -Filter 'cbpi-brewday-*-8012').FullName
$psi = [System.Diagnostics.ProcessStartInfo]::new($env:ComSpec)
$psi.Arguments = '/c ""' + $cbpi + '" -c config start > "%TEMP%\cbpi-brewday-8012.log" 2>&1"'
$psi.WorkingDirectory = $sim8012
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.Environment['PYTHONPATH'] = "C:\src\personal\craftbeerpi4-graphs;C:\src\personal\craftbeerpi4-ui-graphs;$files\wmi-bypass"
$psi.Environment['PYTHONUTF8'] = '1'
$psi.Environment['PYTHONIOENCODING'] = 'utf-8'
[void][System.Diagnostics.Process]::Start($psi)
```

They keep running after you close PowerShell. Give them half a minute, then open
http://127.0.0.1:8000 and http://127.0.0.1:8012.

**Tested** on 23 Nov 2026: the 8000 form of this, with the bench environment's `cbpi.exe`
and a scratch settings folder on a spare port, started a hidden server that answered, and
the stop below stopped it. Not run against 8000 or 8012 themselves. The coordinator's own
start commands are in the comments at the end of `restart-sims.ps1` (below).

### Before a restart

- The PC sleeps at night, so the simulators are often down in the morning. That's normal:
  start them by hand. Don't use Windows Task Scheduler for them; the owner said no.
- Before stopping one, look at it: no recipe step running, no kettle in AUTO, no switch
  on. If the owner is using it, ask first.
- Before restarting 8012, bring its web interface up to date:
  `git -C C:\src\personal\craftbeerpi4-ui-graphs merge --ff-only main`.
- When the core's `master` moves, both simulators run the new core at their next restart
  (`land_core.py` moves `brewday-graphs` with it).
- After installing anything into sims-venv, run `installed_sync.py` on it (part 5).

### Stopping one

Each simulator is a small family of processes. This finds the family of the one on a port,
and nothing else:

```powershell
$port = 8000
$listener = (Get-NetTCPConnection -State Listen -LocalPort $port).OwningProcess | Select-Object -First 1
$tree = @(); $id = $listener
while ($id) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$id"
    if (-not $proc -or $proc.CommandLine -notmatch 'cbpi') { break }
    $tree += [int]$id
    if ($proc.Name -eq 'cmd.exe') { break }
    $id = $proc.ParentProcessId
}
$tree
Stop-Process -Id $tree -Force
```

### The restart script

`restart-sims.ps1`, in the coordinator's `release` folder (a copy is in the bench's
`coordination\release`), does the routine in one go:

1. says what each simulator is doing (steps running, kettles in AUTO, switches on);
2. stops 8000 and 8012, never 8100;
3. backs up both simulators' BrewScribe data into the session folder, and stops if the copy
   is incomplete;
4. brings sims-venv in step with `installed_sync.py --fix`;
5. installs a BrewScribe wheel if given one, after checking its SHA-256:
   `restart-sims.ps1 -Wheel <path to the .whl> -WheelSha <its SHA-256>`.

It doesn't start them again, and it doesn't wait for the bench's lock. Its last comment
lines are the two start commands; start them as above, then check them.

### Checking them

```powershell
$env:PYTHONPATH = "$files\wmi-bypass"
& C:\src\personal\sims-venv\Scripts\python.exe "$files\verify-sims.py" <BrewScribe version> 8000 8012
```

For example `0.1.0.dev60`. It checks the web interface, BrewScribe's page and API, the
websocket, the logs, and that both simulators have the same switch behind each of Brew
Day's buttons. It ends `N/N passed`.

### Port 8100: hands off

http://127.0.0.1:8100/ is Porter's BrewScribe demo. Never stop, restart or change it. The
one exception Porter allows: after the PC restarts, start it again with his own
`start-demo.ps1`, run detached, from his session folder
`%USERPROFILE%\.copilot\session-state\4619bef4-201b-4d27-a5f0-ad622700cd84\files\brewscribe-redesign-approval`.

### Other ports

The bench's tests start servers of their own on other ports (8001 for the brew-day walks,
and many in the 8011–8099 range, each named in its test). Use a port none of them names
for anything you start by hand.

## 8. The test bench

`C:\src\personal\cbpi-testbench` holds about 285 test files (`test_*.py`), each a script
that prints `N/M checks passed` and exits non-zero on a failure. 40 are `test_e2e_*`
walks: whole simulated brew days in a real server and browser, minutes each.

- **One suite**, the safe way, under the shared lock:

  ```powershell
  & $py "$files\test-audit\run_suite.py" <group> test_something.py
  ```

  `<group>` is the suite's group in `test-audit\groups.json`. The verdict goes to
  `test-audit\runs.tsv` and the full output to `test-audit\logs\`.
- **Everything**, when nothing else uses the bench:

  ```powershell
  cd C:\src\personal\cbpi-testbench
  .\run_all_tests.ps1                 # every suite but the walks
  .\run_all_tests.ps1 -IncludeE2E     # the walks too
  .\run_all_tests.ps1 -Filter strike  # only suites whose name contains "strike"
  ```

  It prints one table; `ok` and `part` count as passed.
- **Negative controls** (`negative_controls.py`, about 660): each one puts a known fault
  back into the product code, in memory only, and runs the suite that should catch it. A
  control the suite doesn't catch means the test is too weak. `run_all_tests.ps1 -Filter
  negative_controls -AllControls` runs them all, which takes hours.
- **DEFECTS.md** in the bench is the list of every product fault found, fixed or open.
- **RIG-NOTES.md** is what was learned about the rig and the servers the hard way.
- The bench's tests never touch ports 8000, 8012 or 8100.

## 9. BrewScribe builds

BrewScribe is Porter's. He builds each release as a wheel in his session folder,
`%USERPROFILE%\.copilot\session-state\4619bef4-201b-4d27-a5f0-ad622700cd84\files\brewscribe-redesign-approval\package-r<N>\dist\`,
and says which one, with its size and SHA-256. The simulators get it through
`restart-sims.ps1 -Wheel ... -WheelSha ...` (part 7), which checks the SHA-256 and backs up
both simulators' `config\brewscribe` folders first: a newer BrewScribe may change its
database so an older one can't read it. The bench's environment gets the same wheel
separately, while holding the bench's lock, because the walks that load BrewScribe use it.

Without Porter, build one from the released branch:

```powershell
& $py -m pip wheel --no-deps -w <folder for the wheel> C:\src\personal\_cbpi_plugins\cbpi4-BrewScribe
```

**Tested** on 23 Nov 2026 on a fresh clone of `main`: it made
`cbpi4_brewscribe-0.1.0.dev51-py3-none-any.whl`. Not installed from there. BrewScribe's
README says to install it from its folder or its git URL, never editable.

## 10. Building the web interface

The interface is a React app in `C:\src\personal\craftbeerpi4-ui\cbpi4gui`. The built
bundle is committed (`cbpi4gui\build`), so the rig needs no build step.

1. Once, after cloning: `cd C:\src\personal\craftbeerpi4-ui\cbpi4gui` then `npm ci`.
2. Build into a side folder, so a running 8012 doesn't serve a half-written build:

   ```powershell
   cd C:\src\personal\craftbeerpi4-ui\cbpi4gui
   $env:BUILD_PATH = 'build-next'; $env:CI = 'false'; npm run build
   ```

   About a minute. "Compiled with warnings" is normal; the warnings are old ones.
3. Swap it in: rename `build` to `build-old`, `build-next` to `build`, then delete
   `build-old`.
4. Raise the version in `cbpi4gui\version.py` (for example `0.5.1.a3.post40` to
   `0.5.1.a3.post41`) and commit the version and the new build together, in one commit.

**Not tested** while writing this (a build would have changed the repository). Source: the
coordinator's notes for each build from checkpoint 32 on, and `package.json`'s `build`
script.

## 11. Pushing to GitHub

From your own terminal, after `gh auth login`, an ordinary `git push` works.

AI agents' shells on this PC carry a different GitHub sign-in in `GH_TOKEN`,
`GITHUB_TOKEN` and `GIT_CONFIG_PARAMETERS`, and a push from there fails with 403. In such a
shell, clear those three and push through `gh`:

```powershell
Remove-Item Env:GH_TOKEN, Env:GITHUB_TOKEN, Env:GIT_CONFIG_PARAMETERS -ErrorAction SilentlyContinue
git -c credential.helper= -c "credential.helper=!gh auth git-credential" push fork <branch>
```

Never write a token into `.git/config` or a remote's URL.

Which branches go to the rig, and when: RELEASES.md.
