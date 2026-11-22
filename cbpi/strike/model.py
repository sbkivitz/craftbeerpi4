"""The strike water helper's model: dough-in and the first rest on a HERMS, and the planner that picks USE.

USE is the hottest strike water, rounded down to 0.1 in the brewery's unit, that in every case takes the mash no
higher than water heated only to the rest would (the rule "no worse"), from the grain going in to an hour after Next.
Every case: each number the rig hasn't given yet, anywhere in its range. So a wrong guess lands the mash low, where
the HERMS can bring it up, never high, where nothing can bring it down. The stricter rule "rest" holds every case at
or under the rest itself; PID_HERMS can take a mash a little over its rest by itself, so that rule can leave no
figure at all.

This is the test bench's strike_model.py, which holds the model's report and the checks of its physics, without the
report and with the numbers passed in: the rig's (Plant), PID_HERMS's settings and code (Herms), and how the brew day
runs around it (Flow). Standard library only and nothing from CraftBeerPi, as it runs in a worker process of its own
(cbpi.strike.worker), handed the PIDArduino class of the PID_HERMS plugin the rig runs.

THE FLOW, as the steps and PID_HERMS run it (t = 0: the strike water is in the tun):

  1. The HLT holds all the water, strike and sparge, at the strike temperature.
  2. The HLT pump sends the strike water through the coil to the tun, whose steel is at the room's temperature,
     losing a little on the way. The sparge water stays in the HLT at the strike temperature.
  3. MashIn on the Mash Tun starts PID_HERMS with the tun marked as water only, so its outer loop's band is
     MashIn_DeltaTemp, and the wort pump runs once the HLT is within MashIn_Pump_Within of the target. Once the mash
     probe reads within CraftBeerPi's "target reached" tolerance of the rest, MashIn's one-second timer asks for the
     malt: the tun is marked as holding grain, so the band drops to DeltaTemp, and the wort pump is held
     (Stop_Pump_At_Strike), which also holds the HLT's setpoint at the rest.
  4. The grain goes in, never before the ask, and some minutes later the brewer presses Next.
  5. Next stops PID_HERMS and the next step starts a new one, which carries the old one's integral if it is young
     enough (INTEGRAL_CARRY_MAX_AGE) and runs the wort pump without rests. The HLT, still hotter than the mash, feeds
     it through the coil; PID_HERMS can add heat but never take it away.

The HLT pump runs throughout when HLT_PUMP_WITH_HEAT is on, so the HLT is stirred and loses heat at its pump-on rate.

THE PHYSICS. Two well-stirred vessels: the HLT (its water and steel) and the tun (the strike water, the share of the
tun's steel the water warms, then the grain). The coil moves eps * m_dot * c * (T_hlt - T_tun) while the wort pump
runs, eps = 1 - exp(-UA / (m_dot * c)); nothing while it is held. Each vessel loses UA * (T - T_room). The element adds
what PID_HERMS asks for. Each step is exact for the exchange and the losses, so energy is conserved to rounding, then
the element's heat is added. The probes follow with first-order lags. At the tun's outlet the mash probe reads the
tun; at the coil's outlet it reads the wort leaving the coil while the pump runs, and with the pump held it drifts to
Flow.still_probe's way between the HLT's water and the room, an assumption the rig hasn't checked yet.

UNITS. Temperatures in F, volumes in US gallons, masses in kg, inside and at this module's edges. A Celsius brewery's
numbers are converted by the caller; Grid rounds USE in the brewery's own unit.
"""

import dataclasses
import math

# --- water, steel and the units the rig's measurements were made in ---
L_PER_GAL = 3.785411784
RHO_WATER = 0.995        # kg in a litre of water at mash temperatures
C_WATER = 4181.0         # J/(kg K)
STEEL_C = 500.0          # J/(kg K), stainless steel
GAL_J_K = L_PER_GAL * RHO_WATER * C_WATER   # J/K in a gallon of water
CAL_G_C = 4186.8         # J/(kg K) in 1 cal/(g C), BeerSmith's unit
LB = 0.45359237          # kg in a pound

# MashInStep's own limits: the most a strike may be above the rest (MAX_STRIKE_RISE_C) and at all (MAX_STRIKE_ABS_C).
MAX_STRIKE_RISE_F = 15.0 * 1.8
MAX_STRIKE_F = 82.0 * 1.8 + 32.0
# When every case still stays within the line at the top of that, the planner looks up to here, only to say how far
# above the rest the figure works out at.
FAR_RISE_F = 60.0
BOIL_F = 212.0

# The planner's step. At 2 s PID_HERMS decides every 2 s, as the rig's SampleTime has it, and the plant moves once per
# decision; 1 s gives the same USE in the bench's cases, and 5 s changes PID_HERMS's own small overshoot.
STEP_S = 2

# The cautious search climbs from a few starting corners and has missed the worst case by at most 0.02 F by the rule
# "no worse" in the bench's checks, so the planner holds the worst case it finds this far under its line.
SEARCH_MARGIN_F = 0.02

# The replay's own wobble: PID_HERMS decides on a 2 s clock, so as the strike moves, a decision lands a step earlier
# or later and a case's peak moves by a few hundredths of a degree for reasons that aren't the strike (up to 0.024 F
# measured). A case counts as made worse by the strike only once it is further than this past its line.
WOBBLE_F = 0.04

# A room or a grain temperature the forecast can use, F: the bounds COMPARE PROBES holds a room's reading to.
READING_RANGE_F = (35.0, 120.0)

# A Spike 20 gal kettle, inside, about 17.5 in across.
KETTLE_INSIDE_IN = 17.5
IN3_PER_GAL = 231.0
# The fill the HLT's losses were measured at.
HLT_LOSS_GAL = 15.0

RULES = ("rest", "no worse")
MEASURED, DERIVED, ASSUMED, UNMEASURED = "MEASURED", "DERIVED", "ASSUMED", "UNMEASURED"

# The numbers not measured yet, in the order the search tries them. The order decides which of two equally good
# moves a climb takes, so it is kept as the bench model has it.
BOX_ORDER = ("coil_ua", "flow", "tun_loss", "hlt_loss", "hlt_steel", "dough_min", "next_min", "grain_c",
             "transfer_f", "tun_share", "probe_lag")
# The same numbers in the order the brew-day screen lists them.
SCREEN_ORDER = ("next_min", "tun_share", "tun_loss", "transfer_f", "coil_ua", "dough_min", "flow", "grain_c",
                "hlt_loss", "hlt_steel", "probe_lag")


def c_of(f):
    return (f - 32.0) / 1.8


def f_of(c):
    return c * 1.8 + 32.0


@dataclasses.dataclass(frozen=True)
class Grid:
    """USE's steps of 0.1 in the brewery's unit, "F" or "C". Strikes stay in F inside the model."""
    unit: str = "F"

    def show(self, f):
        """A temperature in F, in the brewery's unit."""
        return f if self.unit == "F" else c_of(f)

    def down(self, f):
        """The highest step at or under f, in F. Never rounded up: a figure rounded up could be over the line."""
        if self.unit == "F":
            return math.floor(f * 10.0 + 1e-9) / 10.0
        return f_of(math.floor(c_of(f) * 10.0 + 1e-9) / 10.0)

    def up(self, f):
        """The step above the step f, in F."""
        if self.unit == "F":
            return round(f + 0.1, 1)
        return f_of(round(c_of(f) + 0.1, 1))


# ---------------------------------------------------------------------------------------------------------------
# The rig
# ---------------------------------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Hlt:
    """The HLT as measured at about 15 gal: its heat capacity (J/K), its loss with its pump off and on (W/K), its
    element (W) and its probe's lag (s); and the steel and coil in it (kg), which that heat capacity includes."""
    c: float = 237686.0
    ua_pump_off: float = 4.45
    ua_pump_on: float = 9.0
    watts: float = 5346.0
    probe_lag_s: float = 65.0
    steel_kg: float = 10.0

    @property
    def measured_gal(self):
        """The water the measured heat capacity holds besides the steel: 14.78 gal on the rig."""
        return (self.c - self.steel_kg * STEEL_C) / GAL_J_K

    def heat_capacity(self, gal, steel_kg=None):
        """J/K holding `gal` of water: the steel and coil kept, the water scaled."""
        steel = self.steel_kg if steel_kg is None else steel_kg
        return steel * STEEL_C + gal * GAL_J_K

    def loss_ua(self, kettle_share, pump_running=True):
        """W/K to the room: the kettle's own pump-off loss times its share at this fill, plus the HLT pump's loop
        (the pump-on loss less the pump-off one) while that pump runs."""
        return self.ua_pump_off * kettle_share + ((self.ua_pump_on - self.ua_pump_off) if pump_running else 0.0)


def loss_area_share(gal, ref_gal=HLT_LOSS_GAL):
    """A kettle's loss area at a fill, as a share of its area at ref_gal: its bottom, its water surface and its wetted
    wall. The dry wall above the water is left out, so this is the least a fuller kettle's loss scales by."""
    bottom = math.pi * (KETTLE_INSIDE_IN / 2.0) ** 2

    def area(g):
        return 2.0 * bottom + math.pi * KETTLE_INSIDE_IN * (g * IN3_PER_GAL / bottom)

    return area(gal) / area(ref_gal)


@dataclasses.dataclass(frozen=True)
class Ranged:
    """A number the rig hasn't given: a central guess, the range it could take, and where they come from.

    hot: "low" or "high", the end that by the physics gives the hotter mash. The cautious search starts there and
    then checks the other values, so a wrong `hot` costs search time, not safety."""
    label: str
    central: float
    low: float
    high: float
    unit: str
    provenance: str
    hot: str
    note: str = ""

    @property
    def hot_value(self):
        return self.high if self.hot == "high" else self.low

    @property
    def cold_value(self):
        return self.low if self.hot == "high" else self.high

    @property
    def fixed(self):
        return self.low == self.high


def default_guesses(hlt=None):
    """The numbers not measured yet on the rig, each with its central guess and range. hlt_loss's low end and central
    guess are None: they depend on the water left in the HLT (ranges)."""
    hlt = hlt or Hlt()
    return {
        "coil_ua": Ranged("coil UA", 300.0, 150.0, 600.0, "W/K", UNMEASURED, "high",
                          "the HERMS coil's heat transfer"),
        "flow": Ranged("wort flow", 6.0, 3.0, 12.0, "L/min", UNMEASURED, "high",
                       "the wort pump's flow through the coil"),
        "tun_loss": Ranged("tun loss", hlt.ua_pump_on, hlt.ua_pump_off, 15.0, "W/K", UNMEASURED, "low",
                           "the tun's loss with the wort pump running, and with it held the same share of that as "
                           "the HLT's pump-off loss is of its pump-on loss; the same kettle as the HLT, so the HLT's "
                           "figures are the central guess"),
        "hlt_loss": Ranged("HLT loss", None, None, 1.0, "share", ASSUMED, "low",
                           "the HLT kettle's own loss with the water left, as a share of its loss at 15 gal: from its "
                           "wetted area to all of it; its pump loop's loss is kept"),
        "hlt_steel": Ranged("HLT steel", hlt.steel_kg, hlt.steel_kg, 17.0, "kg", ASSUMED, "high",
                            "the HLT's steel and coil, still hot when the water goes"),
        "dough_min": Ranged("to the grain", 5.0, 2.0, 10.0, "min", ASSUMED, "low",
                            "from the strike water reaching the tun to the grain going in"),
        "next_min": Ranged("to Next", 5.0, 0.0, 10.0, "min", ASSUMED, "low",
                           "from the grain going in to pressing Next"),
        "grain_c": Ranged("grain heat", 0.40, 0.38, 0.42, "cal/g C", ASSUMED, "low",
                          "the malt's specific heat"),
        "transfer_f": Ranged("transfer loss", 1.0, 0.0, 2.0, "F", ASSUMED, "low",
                             "how much the strike water cools on its way to the tun"),
        "tun_share": Ranged("tun steel warmed", 0.75, 0.45, 1.0, "share", UNMEASURED, "low",
                            "the share of the tun's steel the strike water warms at once; less steel warming is the "
                            "hotter mash"),
        "probe_lag": Ranged("mash probe lag", 44.0, 44.0, 65.0, "s", ASSUMED, "high",
                            "how far the mash probe's reading trails the wort"),
    }


@dataclasses.dataclass(frozen=True)
class Plant:
    """The rig's numbers: the HLT as measured, and each number not measured yet with its range (guesses, by name)."""
    hlt: Hlt = dataclasses.field(default_factory=Hlt)
    guesses: dict = dataclasses.field(default_factory=default_guesses)


@dataclasses.dataclass(frozen=True)
class Herms:
    """PID_HERMS on the Mash Tun, in F, and the plugin's own code: its PIDArduino class (pid), its OUTER_LOOP_RATIO
    and its INTEGRAL_CARRY_MAX_AGE (integral_carry_s, seconds; below 0, none is carried). mashin_pump_within_f: 0
    when MashIn_Pump_Within is blank, so the pump circulates from the start. tolerance_f: CraftBeerPi's "target
    reached" tolerance in F (0.5 F, or 0.3 C)."""
    pid: object
    sample_time: float
    p: float
    i: float
    d: float
    hlt_p: float
    hlt_i: float
    hlt_d: float
    delta_f: float
    mashin_delta_f: float
    mashin_pump_within_f: float = 0.0
    max_output: float = 100.0
    pump_rest: bool = False
    outer_ratio: int = 5
    integral_carry_s: float = 300.0
    tolerance_f: float = 0.5


@dataclasses.dataclass(frozen=True)
class Flow:
    """How the brew day runs around PID_HERMS.

    stop_pump_at_strike: the Mash Tun MashIn step's Stop_Pump_At_Strike. hlt_pump_runs: the HLT pump stirs the HLT
    through dough-in and the rest (HLT_PUMP_WITH_HEAT). strike_through_coil: the strike water goes to the tun through
    the coil, so a mash probe at the coil's outlet reads it as MashIn starts; otherwise that probe starts at the
    room's temperature. still_probe: where a mash probe at the coil's outlet drifts while the wort pump is held, as a
    share of the way from the HLT's water (0) to the room (1). window_min: how long after Next the peak is looked
    for."""
    stop_pump_at_strike: bool = True
    hlt_pump_runs: bool = True
    strike_through_coil: bool = True
    still_probe: float = 0.0
    window_min: float = 60.0

    def __post_init__(self):
        if not 0.0 <= self.still_probe <= 1.0:
            raise ValueError("still_probe is a share of the way to the room, 0 to 1, not %r" % (self.still_probe,))


@dataclasses.dataclass(frozen=True)
class Rig:
    plant: Plant
    herms: Herms
    flow: Flow = dataclasses.field(default_factory=Flow)


# ---------------------------------------------------------------------------------------------------------------
# One brew's numbers
# ---------------------------------------------------------------------------------------------------------------

def usable_f(value):
    """A room's or grain's temperature the forecast can use: a number from 35 to 120 F."""
    low, high = READING_RANGE_F
    return isinstance(value, (int, float)) and math.isfinite(value) and low <= value <= high


@dataclasses.dataclass(frozen=True)
class Brew:
    """One brew's numbers, in F, gallons and kg.

    From the recipe: grain_kg, strike_gal (all the water the tun gets, the dead space under the false bottom
    included), rest_f (MashIn on the Mash Tun's temperature). From its equipment profile: tun_kg and tun_cal_g_c; 0 for
    a recipe without one, which leaves the tun's steel out and can only make USE cooler. hlt_total_gal: all the water
    in the HLT before the strike water moves. garage_f: the room, which both vessels lose heat to and the empty tun
    sits at. grain_given_f: the grain's temperature when given, else None and the grain is at the room's. probe:
    where the mash probe sits, "coil" (the coil's outlet) or "tun" (the tun's)."""
    grain_kg: float
    strike_gal: float
    rest_f: float
    hlt_total_gal: float
    tun_kg: float
    tun_cal_g_c: float
    garage_f: float
    grain_given_f: float = None
    probe: str = "coil"

    def __post_init__(self):
        if not usable_f(self.garage_f):
            raise ValueError("the room's temperature, %r, must be from %g to %g F" % ((self.garage_f,) + READING_RANGE_F))
        if self.grain_given_f is not None and not usable_f(self.grain_given_f):
            raise ValueError("the grain's temperature, %r, must be from %g to %g F"
                             % ((self.grain_given_f,) + READING_RANGE_F))
        if self.probe not in ("coil", "tun"):
            raise ValueError("probe must be 'coil' or 'tun', not %r" % (self.probe,))
        for field in ("grain_kg", "strike_gal"):
            if not getattr(self, field) > 0:
                raise ValueError("%s must be more than 0" % field)
        for field in ("tun_kg", "tun_cal_g_c"):
            if not getattr(self, field) >= 0:
                raise ValueError("%s can't be under 0" % field)
        if not math.isfinite(self.rest_f):
            raise ValueError("the rest must be a number")
        if not self.hlt_total_gal >= self.strike_gal:
            raise ValueError("the HLT holds %.2f gal, less than the %.2f gal of strike water"
                             % (self.hlt_total_gal, self.strike_gal))

    @property
    def hlt_left_gal(self):
        return self.hlt_total_gal - self.strike_gal

    @property
    def grain_f(self):
        return self.garage_f if self.grain_given_f is None else self.grain_given_f


def ranges(brew, plant):
    """The numbers not measured yet for this brew, {name: Ranged}, in BOX_ORDER. The HLT's loss share depends on how
    full it is left, so the box is per brew."""
    box = {}
    for name in BOX_ORDER:
        r = plant.guesses[name]
        if name == "hlt_loss" and r.low is None:
            share = loss_area_share(brew.hlt_left_gal)
            high = max(r.high, share)
            r = dataclasses.replace(r, low=round(share, 3), central=round((share + high) / 2.0, 3), high=high)
        box[name] = r
    return box


def central(box):
    return {name: r.central for name, r in box.items()}


def hot_corner(box):
    return {name: r.hot_value for name, r in box.items()}


def long_hold_corner(box):
    """The hot corner with the brew day's minutes at their long ends. The peak has two sources: the HLT's surplus
    soon after Next, worst with a short hold, and PID_HERMS's integral, which a long wait with the tun under the rest
    winds up. Each is the other's local worst, so the search starts from both."""
    corner = hot_corner(box)
    for name in ("dough_min", "next_min"):
        if name in box:
            corner[name] = box[name].high
    return corner


def cold_corner(box):
    return {name: r.cold_value for name, r in box.items()}


def known(box, name, value=None):
    """The box with one number known: fixed at `value`, its central guess if not given."""
    r = box[name]
    value = r.central if value is None else value
    return dict(box, **{name: dataclasses.replace(r, central=value, low=value, high=value)})


# ---------------------------------------------------------------------------------------------------------------
# PID_HERMS, one run of it
# ---------------------------------------------------------------------------------------------------------------

class _Logic:
    """One run of PID_HERMS, from a step switching it on to a step switching it off."""

    def __init__(self, herms, sample_time, clock, carried=None):
        self.herms = herms
        self.outer = herms.pid(sample_time * herms.outer_ratio, herms.p, herms.i, herms.d, 0, herms.delta_f,
                               getTimeMs=clock)
        self.inner = herms.pid(sample_time, herms.hlt_p, herms.hlt_i, herms.hlt_d, 0, herms.max_output,
                               getTimeMs=clock)
        self.band = herms.delta_f
        if carried is not None:
            self.outer._iTerm, self.inner._iTerm = carried
        self.grain_present = True
        self.pump_hold = False
        self.mashin_pump_started = False

    def pump_wanted(self, hlt_reading, target):
        """The wort pump: held for the dough-in; while the tun holds water only, off until the HLT nears the target
        (MashIn_Pump_Within), then on until the grain goes in; otherwise on (no rests)."""
        if self.pump_hold:
            return False
        within = self.herms.mashin_pump_within_f
        if self.grain_present or not within:
            self.mashin_pump_started = False
            return True
        if not self.mashin_pump_started:
            if hlt_reading < target - within:
                return False
            self.mashin_pump_started = True
        return True

    def decide(self, mash_reading, hlt_reading, target, pumping):
        """The cascade, once: (element percent, HLT setpoint). The setpoint is held at the target while the pump is
        held, as PID_HERMS clamps it."""
        herms = self.herms
        wanted = herms.delta_f if self.grain_present else max(herms.delta_f, herms.mashin_delta_f)
        if wanted != self.band:
            self.outer.set_output_max(wanted)
            self.band = wanted
        setpoint = target + self.outer.calc(mash_reading, target)
        if not pumping:
            setpoint = min(setpoint, target)
        return self.inner.calc(hlt_reading, setpoint), setpoint

    def carry(self):
        return self.outer._iTerm, self.inner._iTerm


# ---------------------------------------------------------------------------------------------------------------
# One dough-in
# ---------------------------------------------------------------------------------------------------------------

@dataclasses.dataclass
class Run:
    """What one dough-in did. Temperatures are the mash's own, not a probe's, unless named so.

    landing_f: the mash right after the grain. peak_f: its highest from the grain going in to the end of the window,
    peak_min minutes after the grain. tun_minutes, probe_minutes: the mash and the Mash Tun probe each minute for 60
    minutes from the grain going in, minute 0 the landing. probe_doughin_max_f: the probe's highest reading from the
    ask for the malt to Next. ask_s, grain_s, next_s: seconds after the water reached the tun. ledger: energy, kJ,
    above 0 C: start, grain, element, lost, end, and the residual (end - start - grain - element + lost)."""
    strike_f: float
    landing_f: float
    peak_f: float
    peak_min: float
    tun_30_f: float
    end_f: float
    ask_s: float
    grain_s: float
    next_s: float
    tun_minutes: list
    probe_minutes: list
    probe_doughin_max_f: float
    ledger: dict


def simulate(brew, strike_f, params, rig, dt=STEP_S, losses=True, controller=True):
    """One dough-in at strike_f with one set of the numbers not measured yet (params: a value for each name in the
    box).

    dt: the step, 1, 2 or 5 s. losses=False: no loss to the room anywhere. controller=False: PID_HERMS never heats; the
    steps and the pumps run as usual."""
    if dt not in (1, 2, 5):
        raise ValueError("dt must be 1, 2 or 5 s")
    plant, herms, flow = rig.plant, rig.herms, rig.flow
    if herms.pump_rest:
        raise NotImplementedError("pump rests aren't modelled: the rig runs Pump_Rest No")
    measured = plant.hlt
    p = params
    sample = max(herms.sample_time, dt)
    if sample % dt:
        raise ValueError("the step must divide PID_HERMS's SampleTime")
    rest = brew.rest_f
    garage = c_of(brew.garage_f)
    tolerance = herms.tolerance_f

    c_strike = brew.strike_gal * GAL_J_K
    c_steel = brew.tun_kg * brew.tun_cal_g_c * CAL_G_C * p["tun_share"]
    c_grain = brew.grain_kg * p["grain_c"] * CAL_G_C
    c_hlt = measured.heat_capacity(brew.hlt_left_gal, p["hlt_steel"])
    if losses:
        ua_tun = {True: p["tun_loss"], False: p["tun_loss"] * measured.ua_pump_off / measured.ua_pump_on}
        ua_hlt = measured.loss_ua(p["hlt_loss"], flow.hlt_pump_runs)
    else:
        ua_tun = {True: 0.0, False: 0.0}
        ua_hlt = 0.0
    mdc = p["flow"] / 60.0 * RHO_WATER * C_WATER
    eps = 1.0 - math.exp(-p["coil_ua"] / mdc)
    k_coil = eps * mdc
    lag_mash = 1.0 - math.exp(-dt / p["probe_lag"])
    lag_hlt = 1.0 - math.exp(-dt / measured.probe_lag_s)
    still = flow.still_probe

    water = c_of(strike_f - p["transfer_f"])
    c_tun = c_strike + c_steel
    tun = (c_strike * water + c_steel * garage) / c_tun
    hlt = c_of(strike_f)
    hlt_reading = strike_f
    if brew.probe == "tun":
        mash_reading = f_of(tun)
    else:
        mash_reading = strike_f if flow.strike_through_coil else brew.garage_f

    factors = {}

    def factors_for(pumping):
        """Per-step factors, exact for the exchange and the losses at this step and these heat capacities."""
        key = (pumping, c_tun)
        if key not in factors:
            if pumping:
                exch = 1.0 - math.exp(-k_coil * (1.0 / c_hlt + 1.0 / c_tun) * dt)
            else:
                exch = 0.0
            factors[key] = (exch * c_tun / (c_hlt + c_tun), exch * c_hlt / (c_hlt + c_tun),
                            math.exp(-ua_hlt * dt / c_hlt), math.exp(-ua_tun[pumping] * dt / c_tun))
        return factors[key]

    clock = {"ms": 0.0}

    def now():
        return clock["ms"]

    logic = _Logic(herms, sample, now)
    logic.grain_present = False                 # MashInStep.on_start: water only
    logic_start = 0
    energy = {"start": (c_hlt * hlt + c_tun * tun) / 1000.0, "grain": 0.0, "element": 0.0, "lost": 0.0}
    heat = 0.0
    pumping = False
    reached_at = ask_at = grain_at = next_at = end_at = peak_from = None
    landing = None
    peak, peak_at = -1e9, None
    tun_30 = None
    tun_minutes, probe_minutes = [], []
    probe_doughin = None
    grain_wanted = int(round(p["dough_min"] * 60.0 / dt)) * dt
    next_after = int(round(p["next_min"] * 60.0 / dt)) * dt
    limit = 4 * 3600

    t = 0
    while True:
        clock["ms"] = t * 1000.0

        # --- the steps ---
        if ask_at is not None and t == ask_at:
            # MashInStep.on_timer_done: grain from here on, and the pump held for the dough-in.
            logic.grain_present = True
            logic.pump_hold = flow.stop_pump_at_strike
            probe_doughin = mash_reading
            grain_at = max(grain_wanted, ask_at)
            next_at = grain_at + next_after
            end_at = max(next_at + int(round(flow.window_min * 60.0 / dt)) * dt, grain_at + 3600)
            # The mash holds grain from here: the peak counts from it, not only after Next.
            peak_from = grain_at
        if grain_at is not None and t == grain_at:
            tun = (c_tun * tun + c_grain * c_of(brew.grain_f)) / (c_tun + c_grain)
            c_tun += c_grain
            energy["grain"] = c_grain * c_of(brew.grain_f) / 1000.0
            landing = f_of(tun)
            tun_minutes.append(landing)
            probe_minutes.append(mash_reading)
            if t >= peak_from:
                peak, peak_at = landing, t
        if next_at is not None and t == next_at:
            # MashInStep.on_stop switches PID_HERMS off, which keeps its integral; the next step switches a new one
            # on at once, with grain, no hold, and that integral, as it is younger than INTEGRAL_CARRY_MAX_AGE.
            carried = logic.carry() if 0 <= herms.integral_carry_s else None
            logic = _Logic(herms, sample, now, carried=carried)
            logic_start = t
            heat = 0.0
        if end_at is not None and t >= end_at:
            break
        if ask_at is None and t > limit:
            raise RuntimeError("MashIn never asked for the malt at %.1f F: the probe never reached %.1f F"
                               % (strike_f, rest - tolerance))

        # --- PID_HERMS: the pump every second, the cascade every SampleTime ---
        pumping = logic.pump_wanted(hlt_reading, rest)
        if (t - logic_start) % sample == 0:
            if controller:
                heat, _ = logic.decide(mash_reading, hlt_reading, rest, pumping)
            else:
                heat = 0.0
        watts = measured.watts * max(0.0, heat) / 100.0

        # --- the plant, one step ---
        to_hlt, to_tun, keep_hlt, keep_tun = factors_for(pumping)
        gap = hlt - tun
        hlt -= gap * to_hlt
        tun += gap * to_tun
        hlt_before, tun_before = hlt, tun
        hlt = garage + (hlt - garage) * keep_hlt
        tun = garage + (tun - garage) * keep_tun
        energy["lost"] += (c_hlt * (hlt_before - hlt) + c_tun * (tun_before - tun)) / 1000.0
        hlt += watts * dt / c_hlt
        energy["element"] += watts * dt / 1000.0

        # --- the probes ---
        if brew.probe == "tun":
            seen = f_of(tun)
        elif pumping:
            seen = f_of(tun + eps * (hlt - tun))
        else:
            seen = (1.0 - still) * f_of(hlt) + still * brew.garage_f
        mash_reading += (seen - mash_reading) * lag_mash
        hlt_reading += (f_of(hlt) - hlt_reading) * lag_hlt
        t += dt

        # --- MashInStep.run: arrival, then its one-second timer ---
        if reached_at is None and mash_reading >= rest - tolerance:
            reached_at = t
            ask_at = int(math.ceil((t + 1.0) / dt)) * dt

        tun_f = f_of(tun)
        if ask_at is not None and t > ask_at and (next_at is None or t <= next_at):
            probe_doughin = max(probe_doughin, mash_reading)
        if grain_at is not None and t > grain_at:
            if (t - grain_at) % 60 == 0 and t - grain_at <= 3600:
                tun_minutes.append(tun_f)
                probe_minutes.append(mash_reading)
            if t > peak_from and tun_f > peak:
                peak, peak_at = tun_f, t
            if t >= next_at and tun_30 is None and t >= next_at + 1800:
                tun_30 = tun_f

    energy["end"] = (c_hlt * hlt + c_tun * tun) / 1000.0
    energy["residual"] = (energy["end"] - energy["start"] - energy["grain"] - energy["element"]
                          + energy["lost"])
    return Run(strike_f=strike_f, landing_f=landing, peak_f=peak, peak_min=(peak_at - grain_at) / 60.0,
               tun_30_f=tun_30, end_f=f_of(tun), ask_s=ask_at, grain_s=grain_at, next_s=next_at,
               tun_minutes=tun_minutes, probe_minutes=probe_minutes, probe_doughin_max_f=probe_doughin,
               ledger=energy)


# ---------------------------------------------------------------------------------------------------------------
# The cautious case and the planner
# ---------------------------------------------------------------------------------------------------------------

def _grid(r, points):
    if r.low == r.high:
        return (r.low,)
    return tuple(r.low + (r.high - r.low) * i / (points - 1) for i in range(points))


def planner_aim(brew):
    """The line the planner holds each case's score to: SEARCH_MARGIN_F under the rest, and WOBBLE_F more, which
    over_line gives back as the replay's own wobble."""
    return brew.rest_f - SEARCH_MARGIN_F - WOBBLE_F


def over_line(score, aim):
    """Is a case's score past the planner's aim by more than the replay's own wobble? Both rules judge by this."""
    return score > aim + WOBBLE_F


class Judge:
    """How a case is judged at a strike, by the rule USE works to.

    "rest": its peak, which must stay at or under the rest. "no worse": its peak less however far the same case
    already goes over the planner's aim with the water at the rest, so USE is held to what the strike adds. Remembers
    each case's peak at the rest and each run, and counts the runs. tick, if given, is called before every replay: the
    worker's chance to answer a quick request, or to stop this work for newer numbers by raising."""

    def __init__(self, brew, rig, rule="no worse", dt=STEP_S, tick=None):
        if rule not in RULES:
            raise ValueError("rule must be one of %r, not %r" % (RULES, rule))
        self.brew, self.rig, self.rule, self.dt, self.tick = brew, rig, rule, dt, tick
        self.aim = planner_aim(brew)
        self.at_rest = {}
        self.seen = {}
        self.sims = 0

    def _simulate(self, strike_f, params):
        if self.tick is not None:
            self.tick()
        self.sims += 1
        return simulate(self.brew, strike_f, params, self.rig, dt=self.dt)

    def allowance(self, params):
        """How far this case goes over the planner's aim with the water at the rest; 0 by the rule "rest"."""
        if self.rule == "rest":
            return 0.0
        key = tuple(sorted(params.items()))
        if key not in self.at_rest:
            peak = self._simulate(self.brew.rest_f, params).peak_f
            self.at_rest[key] = max(0.0, peak - self.aim)
        return self.at_rest[key]

    def run(self, strike_f, params):
        """(the run, its score: its peak less its allowance). A case already run at this strike isn't run again."""
        key = (strike_f, tuple(sorted(params.items())))
        if key not in self.seen:
            run = self._simulate(strike_f, params)
            self.seen[key] = (run, run.peak_f - self.allowance(params))
        return self.seen[key]

    def passes(self, strike_f, params):
        return not over_line(self.run(strike_f, params)[1], self.aim)


# Rounds a climb may take, one move each.
CLIMB_ROUNDS = 20


def _climb(judge, strike_f, box, start, points):
    """One climb from `start`: (params, Run, score). Each round tries every number at `points` values across its range,
    the others held, and takes the one change that raises the score most, until none does."""
    current = dict(start)
    best, best_score = judge.run(strike_f, current)
    for _ in range(CLIMB_ROUNDS):
        move = None
        for name, r in box.items():
            for value in _grid(r, points):
                if value == current[name]:
                    continue
                trial = dict(current, **{name: value})
                run, score = judge.run(strike_f, trial)
                bar = best_score if move is None else move[2]
                if score > bar + 1e-6:
                    move = (trial, run, score)
        if move is None:
            break
        current, best, best_score = move
    return current, best, best_score


def cautious_case(brew, strike_f, box, judge, starts=None, points=3):
    """The numbers, within their ranges, that give this strike the highest score: (params, Run). A climb from each
    start - the hot and the long-hold corners if none are given - and the highest of them."""
    if not starts:
        starts = [hot_corner(box), long_hold_corner(box)]
    best_params, best_run, best_score = None, None, None
    tried = []
    for start in starts:
        if start in tried:
            continue
        tried.append(start)
        params, run, score = _climb(judge, strike_f, box, start, points)
        if best_score is None or score > best_score:
            best_params, best_run, best_score = params, run, score
    return best_params, best_run


def hottest_under(brew, cases, aim_f, judge, step_f=3.0, resolution_f=0.01, top=None):
    """The hottest strike at which no case in `cases` goes past aim_f, by over_line, to resolution_f; None if none
    from the rest up does.

    Walks down from `top` (MashInStep's limits if not given) in step_f until every case stays under, then halves the
    step above it. Down, not up: a colder strike can go over too, as PID_HERMS carries mash-in's integral into the
    rest."""
    if top is None:
        top = min(brew.rest_f + MAX_STRIKE_RISE_F, MAX_STRIKE_F)

    def ok(strike):
        for params in cases:
            if over_line(judge.run(strike, params)[1], aim_f):
                return False
        return True

    lo, hi, strike = None, None, top
    while True:
        if ok(strike):
            lo = strike
            break
        hi = strike
        if strike <= brew.rest_f:
            break
        strike = max(strike - step_f, brew.rest_f)
    if lo is not None and hi is not None:
        while hi - lo > resolution_f:
            mid = (lo + hi) / 2.0
            if ok(mid):
                lo = mid
            else:
                hi = mid
    return lo


@dataclasses.dataclass
class Plan:
    """USE for one brew.

    use_f: USE, a step of grid in F, never under the rest; None if no strike from the rest up passes (only by the
    rule "rest"). use_exact_f: the same before rounding. far: every case still passed at the top of MashInStep's
    limits, so the search went on above them to find use_f. cautious: USE's worst case. found: every case the planner
    held the strike to. confirmed: the cautious search at USE found nothing past the line."""
    use_f: float
    use_exact_f: float
    far: bool
    rule: str
    cautious: dict
    found: list
    confirmed: bool
    sims: int


def plan(brew, rig, box=None, rule="no worse", dt=STEP_S, grid=Grid(), tick=None):
    """USE: the hottest strike, rounded down to a step of `grid`, that keeps every case within its line from the
    grain going in.

    Starts from the hot and long-hold corners: the hottest strike for them, rounded down, then the cautious search
    there. A case the search finds past the line joins them, and the strike is found again, until a search at USE
    finds nothing past it."""
    box = box or ranges(brew, rig.plant)
    judge = Judge(brew, rig, rule, dt, tick)
    aim = judge.aim
    top = min(brew.rest_f + MAX_STRIKE_RISE_F, MAX_STRIKE_F)
    found = [hot_corner(box), long_hold_corner(box)]
    exact = use = None
    worst, confirmed = found[0], False
    for _ in range(6):
        exact = hottest_under(brew, found, aim, judge, top=top)
        # Never under the rest: a strike at the rest is the colder way, not a figure to round.
        use = None if exact is None else max(grid.down(exact), brew.rest_f)
        if use is not None:
            # The halving ends within its resolution of where a case crosses the line, and that case's own wobble
            # can leave it just under a step every case passes: try that step too.
            above = grid.up(use)
            if all(judge.passes(above, params) for params in found):
                use = exact = above
        at = brew.rest_f if use is None else use
        worst, run = cautious_case(brew, at, box, judge, starts=found)
        if use is None:
            break
        if not over_line(run.peak_f - judge.allowance(worst), aim):
            confirmed = True
            break
        found.append(worst)
    far = False
    if exact is not None and exact >= top - 1e-6:
        # Every case passed at the top: look higher, only to say how far the figure works out at.
        far = True
        exact = hottest_under(brew, found, aim, judge, top=min(brew.rest_f + FAR_RISE_F, BOIL_F))
        use = max(grid.down(exact), brew.rest_f)
    return Plan(use_f=use, use_exact_f=exact, far=far, rule=rule, cautious=worst, found=found, confirmed=confirmed,
                sims=judge.sims)


# ---------------------------------------------------------------------------------------------------------------
# What the brew-day screen shows
# ---------------------------------------------------------------------------------------------------------------

def _key(params):
    return tuple(sorted(params.items()))


def quick_cases(box, found, cautious=None):
    """The cases a forecast without a search of its own takes: every case the planner found, USE's worst, the
    central guesses, and the hot, long-hold and coolest corners; each once."""
    named = [("found %d" % i, params) for i, params in enumerate(found)]
    if cautious is not None:
        named.append(("cautious", cautious))
    named += [("central", central(box)), ("hot corner", hot_corner(box)), ("long hold", long_hold_corner(box)),
              ("coolest corner", cold_corner(box))]
    cases, seen = {}, set()
    for name, params in named:
        if _key(params) not in seen:
            seen.add(_key(params))
            cases[name] = dict(params)
    return cases


@dataclasses.dataclass
class Forecast:
    """The mash at one strike across the cases. low_f, high_f: its lowest and highest at each minute from the grain
    going in, 0 to 60, minute 0 where it lands. highest_f: the highest any case reaches from the grain going in to an
    hour after Next. reads_f: the Mash Tun probe's highest reading from the ask for the malt to Next. searched: the
    cases include this strike's own worst, searched for."""
    strike_f: float
    low_f: list
    high_f: list
    highest_f: float
    reads_f: float
    searched: bool
    sims: int


def forecast(brew, strike_f, rig, cases, dt=STEP_S, searched=False, tick=None):
    """The mash at strike_f across `cases` ({name: params}), each replayed once."""
    runs = []
    for params in cases.values():
        if tick is not None:
            tick()
        runs.append(simulate(brew, strike_f, params, rig, dt=dt))
    return Forecast(strike_f=strike_f,
                    low_f=[min(run.tun_minutes[m] for run in runs) for m in range(61)],
                    high_f=[max(run.tun_minutes[m] for run in runs) for m in range(61)],
                    highest_f=max(run.peak_f for run in runs),
                    reads_f=max(run.probe_doughin_max_f for run in runs),
                    searched=searched, sims=len(runs))


def searched_forecast(brew, strike_f, rig, box, found, cases, dt=STEP_S, tick=None):
    """A forecast with this strike's own worst case searched for, from the cases the planner found, added to `cases`
    (by the rule "rest": the highest peak itself)."""
    judge = Judge(brew, rig, "rest", dt, tick)
    worst, _ = cautious_case(brew, strike_f, box, judge, starts=list(found))
    more = dict(cases)
    if _key(worst) not in {_key(p) for p in cases.values()}:
        more["searched"] = worst
    fc = forecast(brew, strike_f, rig, more, dt=dt, searched=True, tick=tick)
    fc.sims += judge.sims
    return fc


def levers(brew, rig, box, use_f, rule="no worse", dt=STEP_S, grid=Grid(), tick=None):
    """How much USE would rise if each number alone were known to be at its central guess, the others still anywhere
    in their ranges: ({name: rise in the brewery's unit}, USE with every number at its central guess, F), for the
    numbers not fixed, in SCREEN_ORDER."""
    rise = {}
    for name in SCREEN_ORDER:
        if name not in box or box[name].fixed:
            continue
        other = plan(brew, rig, box=known(box, name), rule=rule, dt=dt, grid=grid, tick=tick).use_f
        rise[name] = None if other is None or use_f is None else round(grid.show(other) - grid.show(use_f), 1)
    middle = box
    for name in box:
        middle = known(middle, name)
    mid = plan(brew, rig, box=middle, rule=rule, dt=dt, grid=grid, tick=tick).use_f
    return rise, mid


def energy_in_closed_form(brew, strike_f, params, rig):
    """With no losses and no heat added, where everything evens out: the strike water (cooled on its way), the share
    of the tun's steel it warms, the grain, and the HLT's water and steel at the strike, F."""
    hlt = rig.plant.hlt
    c_strike = brew.strike_gal * GAL_J_K
    c_steel = brew.tun_kg * brew.tun_cal_g_c * CAL_G_C * params["tun_share"]
    c_grain = brew.grain_kg * params["grain_c"] * CAL_G_C
    c_hlt = hlt.heat_capacity(brew.hlt_left_gal, params["hlt_steel"])
    total = (c_strike * c_of(strike_f - params["transfer_f"]) + c_steel * c_of(brew.garage_f)
             + c_grain * c_of(brew.grain_f) + c_hlt * c_of(strike_f))
    return f_of(total / (c_strike + c_steel + c_grain + c_hlt))


__all__ = [
    "Brew", "Flow", "Forecast", "Grid", "Herms", "Hlt", "Plan", "Plant", "Ranged", "Rig", "Run", "BOX_ORDER",
    "SCREEN_ORDER", "STEP_S", "WOBBLE_F", "SEARCH_MARGIN_F", "MAX_STRIKE_RISE_F", "READING_RANGE_F", "c_of", "f_of",
    "default_guesses", "ranges", "central", "hot_corner", "long_hold_corner", "cold_corner", "known", "simulate",
    "plan", "quick_cases", "forecast", "searched_forecast", "levers", "usable_f", "energy_in_closed_form",
]
