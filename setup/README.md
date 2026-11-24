# Setting this project up again

These files say how to put the brewery's software back together if something is lost: the
Raspberry Pi that runs the brewing rig, the Windows PC where the software is developed and
tested, or the AI helpers that have done most of the work so far. They are written so a person
with no AI help can follow them.

They live in the `setup` folder of the CraftBeerPi core fork,
https://github.com/sbkivitz/craftbeerpi4, because that repository is public and is the one
thing every rebuild starts from.

## Which file to read

| What happened | Read |
|---|---|
| The Pi died, its SD card failed, or the rig needs a fresh install | [REBUILD-THE-RIG.md](REBUILD-THE-RIG.md) |
| The development PC died, or you are setting up a new one | [WORKSTATION.md](WORKSTATION.md) |
| You want to put a change on the rig yourself, without an AI | [RELEASES.md](RELEASES.md) |
| The AI helpers are gone and you carry on alone | [RELEASES.md](RELEASES.md) first, then [WORKSTATION.md](WORKSTATION.md) |
| A new AI is going to help | Give it [AI-SEED.md](AI-SEED.md) and ask it to read the rest of this folder |
| A rig update has just been published | Go through [CHECK.md](CHECK.md) so these files stay true |

## What is public and what is not

This folder is public, so it holds no addresses, passwords, keys, Wi-Fi details,
phone-alert topics or personal names. Where a step needs one of those, it says where the
owner keeps it.

Some of the project is in private GitHub repositories. You need to be signed in to GitHub as
the owner to clone them:

| Repository | What it holds |
|---|---|
| https://github.com/sbkivitz/cbpi-testbench | The test bench: tests, the rig update page and its generator, notes about the rig (`RIG-NOTES.md`, `AGENT-PRIMER.md`), and a copy of the coordinator's tools in its `coordination` folder |
| https://github.com/sbkivitz/cbpi-rig-comms | The log the rig-side AI and the workstation AI write to each other: the rig's own reports, readings and settings exports |
| https://github.com/sbkivitz/cbpi4-BrewScribe | BrewScribe, the brew-day log |
| https://github.com/sbkivitz/cbpi4-simvessel | The simulated kettles the workstation's simulators use |

## How sure these files are

Each command was either run while writing these files, or is marked **Not tested**, with
where it came from. "Tested on Linux" means it ran on Ubuntu 24.04 under Windows (WSL), not
on a Pi: it proves the command and the package names, not the hardware.

The last rig update these files were checked against is in the versions block in
[REBUILD-THE-RIG.md](REBUILD-THE-RIG.md). A test in the bench, `test_setup_guide.py`, fails when
that block falls behind the newest rig update page, when a repository the workstation
installs from isn't named in [WORKSTATION.md](WORKSTATION.md), or when a file these guides
point to has gone.
