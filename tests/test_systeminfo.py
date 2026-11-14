import os
import subprocess
import tempfile
from unittest import mock

from cbpi.controller import system_controller
from cbpi.controller.system_controller import SystemController


def fake_hwmon(root, devices):
    """One hwmon folder per (name, millidegrees), in the order given."""
    paths = []
    for i, (name, millidegrees) in enumerate(devices):
        path = os.path.join(root, "hwmon%d" % i)
        os.makedirs(path)
        with open(os.path.join(path, "name"), "w") as f:
            f.write(name + "\n")
        with open(os.path.join(path, "temp1_input"), "w") as f:
            f.write("%d\n" % millidegrees)
        paths.append(path)
    return paths


def test_cpu_temperature_reads_only_the_cpu_sensor():
    with tempfile.TemporaryDirectory() as root:
        # 1-Wire probes listed first: reading one starts a conversion on the bus
        paths = fake_hwmon(
            root,
            [("w1_slave_temp", 19000), ("w1_slave_temp", 64000), ("cpu_thermal", 55500)],
        )
        real_open = open
        opened = []

        def recording_open(path, *args, **kwargs):
            opened.append(os.path.normpath(str(path)))
            return real_open(path, *args, **kwargs)

        with mock.patch.object(system_controller.glob, "glob", return_value=paths), \
                mock.patch("builtins.open", side_effect=recording_open):
            assert SystemController._cpu_temperature(False) == 55.5
            assert SystemController._cpu_temperature(True) == 131.9

        probe_readings = {os.path.normpath(os.path.join(p, "temp1_input")) for p in paths[:2]}
        assert not probe_readings & set(opened)


def test_cpu_temperature_without_a_cpu_sensor():
    with tempfile.TemporaryDirectory() as root:
        paths = fake_hwmon(root, [("w1_slave_temp", 19000)])
        with mock.patch.object(system_controller.glob, "glob", return_value=paths):
            assert SystemController._cpu_temperature(False) == 0


def test_wlan0_rate():
    out = "wlan0     unknown bit-rate information.\n          Current Bit Rate=72.2 Mb/s\n"
    done = mock.Mock(stdout=out)
    with mock.patch.object(system_controller.subprocess, "run", return_value=done) as run:
        assert SystemController._wlan0_rate() == "72.2"
    assert run.call_args.kwargs["timeout"] == 2


def test_wlan0_rate_stuck_missing_or_silent():
    for error in (subprocess.TimeoutExpired("iwlist", 2), FileNotFoundError("iwlist")):
        with mock.patch.object(system_controller.subprocess, "run", side_effect=error):
            assert SystemController._wlan0_rate() == "N/A"
    silent = mock.Mock(stdout="wlan0     no bit-rate information.\n")
    with mock.patch.object(system_controller.subprocess, "run", return_value=silent):
        assert SystemController._wlan0_rate() == "N/A"


def test_collector_on_linux_uses_the_cpu_sensor_and_bounded_iwlist():
    ctrl = SystemController.__new__(SystemController)
    ctrl.cbpi = mock.Mock()
    ctrl.cbpi.config.get.return_value = "C"
    stats = {"wlan0": mock.Mock(isup=True, speed=0)}
    with mock.patch.object(system_controller.psutil, "LINUX", True), \
            mock.patch.object(system_controller.psutil, "net_if_addrs", return_value={}), \
            mock.patch.object(system_controller.psutil, "net_if_stats", return_value=stats), \
            mock.patch.object(system_controller.psutil, "sensors_temperatures", create=True,
                              side_effect=AssertionError("reads every hwmon device")), \
            mock.patch.object(SystemController, "_cpu_temperature", return_value=48.3), \
            mock.patch.object(SystemController, "_wlan0_rate", return_value="72.2"):
        info = ctrl._collect_systeminfo()
    assert info["temp"] == 48.3
    assert info["wlan0speed"] == "72.2"
