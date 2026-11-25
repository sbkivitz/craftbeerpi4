"""A steam condenser on the boil kettle's lid, and its mister: what the boil step and the watch after it share.

A boil kettle may have a steam condenser on its lid - a Steam Slayer, say - whose cold-water mister turns the boil's
steam back into water (cbpi.api.kettle_measures: the kettle's condenser, "fitted", and its condenser_name). Its mister
goes on as the wort nears the boil, when the boil's lid reminder (BoilStep's LidAlert) says so, and off once the boil
is over and the wort has fallen back below the same temperature: one figure for on and off, lid_temp(), so a setting
for it later moves both. The mister is set by hand, or switched by CraftBeerPi when the steps_boil_actor setting
names the actor that switches it (a solenoid valve).

The watch after the boil is cbpi.extension.condenser. What the boil and the watch have done is kept with the boil
step's props, underscored like BoilStep.LID_ALERT_DONE_PROP, so a restart neither asks twice nor forgets to ask and
Brew Day can read it after a reload; a reset clears them, and a recipe never carries them.

Without a condenser fitted none of this is used: the lid reminder, steps_boil_actor and the boil's end are as they
always were.
"""

#: What a page can tell the two reminders by, whatever their words (NotificationController.notify's kind): the boil's
#: lid reminder, where a condenser is fitted, and the reminder to turn its mister off after the boil.
KIND_ON = "condenser_on"
KIND_OFF = "condenser_off"

#: The boil step's props. The lid reminder answered (its Done): the mister set by hand was asked on this brew.
LID_ANSWERED_PROP = "_lid_alert_answered"
#: steps_boil_actor switched on by the boil this brew: the switched mister was on.
ACTOR_ON_PROP = "_lid_actor_on"
#: After the boil, the wort below lid_temp(): by hand, the reminder to turn the mister off has been given; switched,
#: the mister has been switched off (or was found off).
OFF_ASKED_PROP = "_condenser_off_asked"
#: That reminder answered (its Done); switched, set with OFF_ASKED_PROP, as nothing waits for an answer.
OFF_ANSWERED_PROP = "_condenser_off_answered"

#: The step type whose end starts the watch.
BOIL_TYPE = "BoilStep"

OFF_TITLE = "Turn off the {} mister"
OFF_TEXT = "The boil is over and the wort is below {}"
SWITCHED_OFF_TITLE = "{} mister off"
SWITCHED_OFF_TEXT = "the boil is over and the wort is below {}"


def lid_temp(unit):
    """The lid reminder's temperature, in the brewery's TEMP_UNIT: 95 in C, 203 in F (anything but "C" is F, as the
    boil step has always read it). The mister goes on above it during the boil, and off below it after."""
    return 95 if unit == "C" else 203


def degrees(unit):
    """lid_temp() as the reminders say it: "95 °C" or "203 °F"."""
    return "{} °{}".format(lid_temp(unit), "C" if unit == "C" else "F")


def capitalised(name):
    """A name as a sentence starts with it: "steam condenser" -> "Steam condenser"; "Steam Slayer" as it is."""
    text = str(name or "")
    return text[:1].upper() + text[1:]
