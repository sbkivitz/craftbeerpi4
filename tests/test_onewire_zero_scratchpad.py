"""An all-zero scratchpad passes CRC and must not become a temperature.

A DS18B20 that is present on the bus but not answering properly returns nine
zero bytes. The w1_therm driver computes the CRC over those bytes, gets 0x00,
compares it against the stored CRC byte which is also 0x00, and prints YES.
`t=0` then parses to a confident 0.000 C.

Observed on the rig on 11 Oct, not theorised. A failing probe branch did this
to two physically healthy probes: both read 0 C, both passed CRC, and both
published with a FRESH AGE, because the read "succeeded" and refreshed
last_success. Every staleness protection downstream keys off last_success, so
none of them fired. On a Fahrenheit rig that is 32 F on every probe at once,
and a kettle logic sees a vessel 120 F below target and calls for full power.

The scratchpads below are real, captured from the rig by the rig agent while
the bus was healthy, which is why the fixtures are trustworthy rather than
invented.
"""

from cbpi.extension.onewire import _scratchpad_is_empty


# Captured from the physical rig, 11 Oct 2026, both probes reading correctly.
BK_HEALTHY = "17 02 4b 46 7f ff 0c 10 36 : crc=36 YES"
MASH_HEALTHY = "0c 01 4b 46 7f ff 0c 10 ca : crc=ca YES"

# What a failing part returns. Nine zeros, and crc=00 YES because the CRC of
# nine zero bytes is itself zero.
ALL_ZERO = "00 00 00 00 00 00 00 00 00 : crc=00 YES"

# A probe genuinely sitting in ice water: temperature bytes zero, alarm
# registers and configuration intact. This MUST still be accepted.
GENUINE_ZERO_C = "00 00 4b 46 7f ff 0c 10 1c : crc=1c YES"


def test_all_zero_scratchpad_is_rejected():
    assert _scratchpad_is_empty(ALL_ZERO) is True


def test_a_real_zero_celsius_reading_is_not_rejected():
    """The whole point of checking the scratchpad rather than the value.

    Rejecting `t=0` outright would also reject a probe in ice water, which is
    a legitimate reading a brewer might take while calibrating.
    """
    assert _scratchpad_is_empty(GENUINE_ZERO_C) is False


def test_healthy_rig_scratchpads_are_not_rejected():
    for line in (BK_HEALTHY, MASH_HEALTHY):
        assert _scratchpad_is_empty(line) is False


def test_unparseable_line_is_left_to_the_normal_path():
    """No verdict invented from a line we cannot read.

    Returning True here would turn any parsing surprise into a sensor fault;
    returning False lets the existing CRC branch and its exception handler
    deal with it as they already do.
    """
    assert _scratchpad_is_empty("") is False
    assert _scratchpad_is_empty("not hex at all here ok") is False
    assert _scratchpad_is_empty("00 00 00") is False


def test_the_fixtures_are_consistent_with_their_stated_temperatures():
    """Guards the fixtures themselves, so a typo cannot quietly weaken them.

    Bytes 0 and 1 are the temperature, little-endian, in sixteenths of a
    degree. BK was reported as t=33437 and Mash as t=16750.
    """
    def temp_c(line):
        b = line.split(" ")
        raw = int(b[1], 16) << 8 | int(b[0], 16)
        return raw / 16.0

    assert abs(temp_c(BK_HEALTHY) - 33.437) < 0.001
    assert abs(temp_c(MASH_HEALTHY) - 16.750) < 0.001
    assert temp_c(GENUINE_ZERO_C) == 0.0
