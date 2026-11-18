"""Whether the system's clock - the real time of day, as against the brewing clock (cbpi.api.clock) - can be trusted.

A Raspberry Pi has no battery-backed clock. After a power cut it boots with the time it last saved
(fake-hwclock), which can be hours behind, and the right time comes from the network a minute or so later.
Whatever decides by the time of day just after a boot, or measures across a power cut, must know whether the
clock has been set since: a step carrying on by itself after a restart, which heats
(StepController._resume_interrupted), how long CraftBeerPi was off, shown on the brew-day timers
(TimersController), and the pre-brew check before START begins a recipe (PrecheckController).

One rule for all of them, here, so they can't drift apart: wherever timedatectl exists - Linux, and so a Raspberry
Pi - only its plain "yes" to NTPSynchronized counts. "no", a failure (a user not allowed to run it, D-Bus out
of reach), no answer within TIMEDATECTL_TIMEOUT seconds, or an answer that isn't yes or no all count as not
synchronized. Only a system with no timedatectl at all - Windows, as on the simulators - counts as
trustworthy: its battery-backed clock keeps time through a power cut.

ask_timedatectl, clock_answer and system_clock_trusted run a program: call them in a thread from the event loop.
"""

import os
import shutil
import subprocess

#: Where systemd puts timedatectl: looked for there too, so a service started with a short PATH still finds it.
TIMEDATECTL_PLACES = ("/usr/bin/timedatectl", "/bin/timedatectl")
#: Real seconds timedatectl has to answer. One that doesn't counts as not synchronized.
TIMEDATECTL_TIMEOUT = 5


def find_timedatectl():
    """timedatectl's path, or None on a system without it, such as Windows."""
    return shutil.which("timedatectl") or next((p for p in TIMEDATECTL_PLACES if os.path.exists(p)), None)


def ask_timedatectl():
    """What timedatectl says about the system clock:
    (True, None) - it has been set from a time server since boot;
    (False, why) - timedatectl exists and doesn't say so: why is what went wrong, or None for its plain "no";
    (None, None) - there is no timedatectl at all."""
    program = find_timedatectl()
    if program is None:
        return None, None
    try:
        out = subprocess.run([program, "show", "--property=NTPSynchronized", "--value"], capture_output=True,
                             text=True, errors="replace", timeout=TIMEDATECTL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"timedatectl didn't answer within {TIMEDATECTL_TIMEOUT} s"
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"timedatectl couldn't be run ({e})"
    answer = (out.stdout or "").strip()
    if out.returncode != 0:
        said = " ".join(((out.stderr or "").strip() or answer).split())[:120]
        return False, f"timedatectl failed (exit {out.returncode}{': ' + said if said else ''})"
    if answer.lower() == "yes":
        return True, None
    if answer.lower() == "no":
        return False, None
    return False, f"timedatectl answered {answer[:40]!r}, not yes or no"


def clock_answer():
    """The rule's answer with its reason, for a screen that puts it in words: a dict of
    - trusted: whether the clock counts as set, as system_clock_trusted says;
    - source: where the time comes from under the rule that gave the answer. "network": a time server, which
      timedatectl reports on - a Raspberry Pi without a clock board. "system": the computer's own battery-backed
      clock, where there is no timedatectl - Windows. A rule for a clock board would add its own;
    - reason: "synchronized", timedatectl's plain yes; "not_synchronized", its plain no - the time hasn't come from
      a time server since boot; "unconfirmed", timedatectl failed, didn't answer in time, couldn't be run or
      answered neither yes nor no, which counts as not synchronized; "no_timedatectl", there is none, so the
      system's own clock counts as set;
    - detail: what went wrong asking timedatectl, as ask_timedatectl says it, or None."""
    synced, why = ask_timedatectl()
    trusted = synced is not False
    if synced is None:
        source, reason = "system", "no_timedatectl"
    elif trusted:
        source, reason = "network", "synchronized"
    else:
        source, reason = "network", "unconfirmed" if why else "not_synchronized"
    return dict(trusted=trusted, source=source, reason=reason, detail=why)


def system_clock_trusted():
    """(trusted, why): the rule above. Not trusted only where timedatectl exists and doesn't say yes; why as
    ask_timedatectl gives it."""
    answer = clock_answer()
    return answer["trusted"], answer["detail"]
