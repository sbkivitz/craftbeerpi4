# Getting a change from the workstation to the rig

This is how a change travels from the development PC to the brewing rig, written so you
can do it without an AI. WORKSTATION.md says how the PC is set up; REBUILD-THE-RIG.md
says what the rig is.

## Words used here

- **The rig update page**: `rig-update.html` in the private bench
  (`C:\src\personal\cbpi-testbench\rig-update.html`). Step-by-step commands to run on the
  Pi, generated from the repositories by `build_rig_page.py`, never typed by hand.
- **Rev**: the page's revision number. It goes up by one every time the page is built.
  Rev 69 is the last one published; it went on the rig on 21–22 Nov 2026.
- **Version**: each package's own number. The forks of other people's plugins add
  `.postN` to the original's number (`0.0.9.post9`), so the original number stays theirs.
  The owner's own boilpower plugin numbers its releases the same way (`0.0.3.post12`).
  BrewScribe uses `0.1.0.devN`. The web
  interface's source spells its number `0.5.1.a3.post25`; pip shows it as
  `0.5.1a3.post25`. Every change that reaches the rig needs a new version: pipx skips a
  package whose version hasn't moved, and the bench's `test_plugin_versions.py` fails
  when code changed without one.
- **Rig-followed branches**: the branches the rig installs from, on GitHub:

  | Repository | Branch |
  |---|---|
  | craftbeerpi4 | `master` |
  | craftbeerpi4-ui | `main` |
  | cbpi4-PIDHerms | `main` |
  | cbpi4-boilpower | `master` |
  | cbpi4-PIDBoil | `main` |
  | cbpi4-PID_AutoTune | `main` |
  | cbpi4_BrewStart | the branch ending `/wall-clock-and-i18n` |
  | cbpi4-BrewScribe | `main` (Porter moves it) |
  | cbpi4-PushOver | `phone-alerts`, once phone alerts join the rig |

  Pushing one of these is what makes a change reachable by the rig. Nothing else on GitHub
  matters to the rig. Branches named `backup/...` are only copies.

## The rule

The rig is updated only after a change has been tried on the simulators, and only when the
owner says so. The simulators always run the newest work; the rig is usually several
builds behind. That is on purpose.

## 1. Before publishing

1. The change is committed, with its new version.
2. Its tests pass, run one at a time under the bench lock (WORKSTATION.md, part 8),
   including every walk (`test_e2e_*`) through the behaviour that changed.
3. A test that fails right after a change the owner asked for is first a stale test: fix
   the test to act as the owner now would (press NEXT, answer the new question), in the
   same change.
4. Each new test has a negative control that it catches.
5. It is installed on both simulators (`restart-sims.ps1`, then start them and run
   `verify-sims.py`: WORKSTATION.md, part 7) and the owner has tried it there.
6. The owner says publish.

## 2. Publish

1. Push each rig-followed branch that has something new. From your own terminal (or an AI
   shell cleared as in WORKSTATION.md, part 11):

   ```powershell
   git -C C:\src\personal\craftbeerpi4 push fork master
   git -C C:\src\personal\craftbeerpi4-ui push fork main
   git -C C:\src\personal\_cbpi_plugins\cbpi4-PIDHerms push fork main
   git -C C:\src\personal\_cbpi_plugins\cbpi4-boilpower push origin master
   git -C C:\src\personal\_cbpi_plugins\cbpi4-PIDBoil push fork main
   git -C C:\src\personal\_cbpi_plugins\cbpi4-PID_AutoTune push fork main
   git -C C:\src\personal\_cbpi_plugins\cbpi4_BrewStart push fork HEAD
   ```

   The BrewStart line pushes the branch that folder has checked out: check it is the one
   ending `/wall-clock-and-i18n` first (`git -C ... branch --show-current`).

   If a branch holds commits that aren't ready for the rig, push only up to the last one
   that is: `git push fork <commit>:master`. The page is built from what your local folder
   has checked out, so build it from that same commit (an AI can do this with a separate
   worktree; by hand, check that commit out first).

2. Build the page, in the bench:

   ```powershell
   cd C:\src\personal\cbpi-testbench
   & C:\src\personal\craftbeerpi4\.venv\Scripts\python.exe build_rig_page.py
   ```

   It prints `wrote ...rig-update.html (rev NN)`, writes `recommended-versions.txt`, and
   lists each package with its branch, its version, and how many commits are unpushed.
   Every line must say `0 unpushed` and none `DIRTY`. Read the page in a browser before
   going on. The page's words about what changed come from constants near the top of
   `build_rig_page.py` (`RIG_INSTALL`, `LABELS_SINCE`, `LANDED`, the BrewScribe notes):
   update them when they no longer describe the rig.

3. Run the page's own tests: `test_rig_page_*.py`, `test_rig_check_recommended.py` and
   `test_setup_guide.py`, through `run_suite.py`.

4. Commit and push the bench. The rig side reads the page from the bench on GitHub:

   ```powershell
   git -C C:\src\personal\cbpi-testbench add rig-update.html recommended-versions.txt rig-page-rev.json build_rig_page.py
   git -C C:\src\personal\cbpi-testbench commit -m "Rig update page rev NN: <what it brings>"
   git -C C:\src\personal\cbpi-testbench push origin master
   ```

5. Tell the rig side, in `cbpi-rig-comms`:

   ```powershell
   cd C:\src\personal\cbpi-testbench
   & C:\src\personal\craftbeerpi4\.venv\Scripts\python.exe announce_versions.py
   cd C:\src\personal\cbpi-rig-comms
   .\say.ps1 workstation "<a note: what the update brings, what to check, what to report>"
   ```

   `announce_versions.py` posts the versions block from `recommended-versions.txt`
   (`--dry-run` shows it first). Past notes, as examples: `coordination\release\gauge-note-rev69.md`
   in the bench.

6. Update the files in this `setup` folder: CHECK.md.

**Not tested end to end** while writing this; nothing was published. Sources: the plan for
rev 70 in the coordinator's checkpoint 62, `build_rig_page.py`'s `main()`,
`announce_versions.py`'s description, and the rev 69 notes.

## 3. What the rig does with it

On the rig side, someone (the rig-side AI, Gauge, with the owner at the rig, or the owner
alone over SSH) pulls the bench, opens `rig-update.html`, checks its title says the new
rev, and follows it on the Pi. In short:

1. Back up the settings folder, and record the installed packages.
2. Switch everything off, stop the service, and make sure it stopped.
3. Check the service's stop-and-restart add-on is in place.
4. `pipx upgrade cbpi4`, then `pipx inject cbpi4 --force <url>` for each plugin. Never
   `pipx install --force` for an update: it deletes every plugin.
5. Check the new files are the ones installed (a list of lines that must all print `True`).
6. Start the service, hard-refresh the browser, check every actor is off and the clock is
   trusted.
7. Check the plugins registered.
8. Check each kettle's logic and settings.
9. Check the settings that change how it brews.
10. Supervised acceptance with water, with the owner at the rig (step 11).
11. BrewScribe last, in a step of its own (step 11b).

Then the rig side reports in `cbpi-rig-comms`, `rig/<date>.md`, and runs
`rig_check.py --recommended`, which should find 0 problems.

The rig update page is the authority. If anything here disagrees with it, the page wins.

## 4. Going back

**The project's rule is not to go back.** The rig only moves forward: if something is
wrong after an update, report it, fix it in the fork, try the fix on the simulators, and
publish a new rev. Undoing a change works the same way: revert it in the fork and publish.
BrewScribe alone can be taken off without touching the rest (the page's step 11b says how);
its data stays.

The rig update page's step 12 says so ("Do not reinstall an older version"), and it has
been the owner's rule since October. Every fix is checked on the simulators before the rig
gets it, so a fix forward is the tested path; a step back to older code is not.

If the owner decides the rig must go back before a brew day anyway, that is his call. The
way would be:

1. Find each package's commit for the rev to go back to. Commit messages name the version,
   at the start or in brackets at the end, so for example
   `git -C C:\src\personal\craftbeerpi4 log --oneline fork/master | Select-String -SimpleMatch "4.7.7.post48"`.
   The web interface's is the commit whose message starts `Build 0.5.1.a3.post25`.
2. On the Pi, with everything off and the service stopped, install those commits in the
   same pipx way: `pipx install --force git+https://github.com/sbkivitz/craftbeerpi4.git@<commit>`,
   then each plugin with `pipx inject cbpi4 --force git+<url>@<commit>`.
3. Restore the settings backup taken before the update (REBUILD-THE-RIG.md, part 6.3).
4. Do the page's checks again (steps 5 to 7) and the acceptance test.

**Not tested.** Nobody has rolled the rig back.

## 5. Release history

| Rev | Date | What it brought | On the rig? |
|---|---|---|---|
| 65 | 10 Nov 2026 | core 4.7.7.post34, web interface post12: boil power, the 25% power fix | Superseded |
| 66 | 13 Nov | core post37, web interface post13: the sparge step, missing-target warning | Not installed; replaced |
| 67 | 16 Nov | core post40, web interface post14, PIDHerms post8, BoilPower post11: kettle settings files, boil additions warning, HLT pump off at recipe end | Not installed; replaced |
| 68 | 18 Nov | core post47, web interface post24, PIDBoil post4, PIDHerms post9, BoilPower post12, AutoTune post4: Hardware page, timers kept through a restart, pre-brew check, empty-setting defaults | Not installed; replaced |
| 69 | 19 Nov | core post48, web interface post25, PIDBoil post5, AutoTune post6, BrewStart 1.2.0.post7, BrewScribe dev48 (new on the rig) | Installed 21–22 Nov. The water test (step 11) was put off by the owner and hasn't been done |
| 70 | prepared 22 Nov | core post58, web interface post33, BoilPower post13, BrewStart post9, BrewScribe dev51, phone alerts | **Never published**, by the owner's choice: the next update brings everything built since instead |

Before rev 65 the rig's last install was on 11 Oct (core 4.7.7.post1). The bench's git log
for `rig-update.html` has every rev.
