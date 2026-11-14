import asyncio
import glob
import importlib
import json
import logging
import os
import pathlib
import pkgutil
import shutil
import socket
import subprocess
import zipfile
from datetime import date, datetime, timedelta

import aiohttp
import psutil
from cbpi.api import *
from cbpi.api.base import CBPiBase
from cbpi.api.config import ConfigType
from cbpi.api.dataclasses import NotificationAction, NotificationType
from tabulate import tabulate
from voluptuous.schema_builder import message

try:
    from systemd import journal

    systemd_available = True
except Exception:
    logging.warning("Failed to load systemd library. logfile download not available")
    systemd_available = False


class SystemController:

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self.service = cbpi.actor
        self.logger = logging.getLogger(__name__)
        self.logsFolderPath = self.cbpi.config_folder.logsFolderPath

        self.cbpi.app.on_startup.append(self.check_for_update)

    async def check_for_update(self, app):
        pass

    async def restart(self):
        logging.info("RESTART")
        os.system("systemctl reboot")
        pass

    async def shutdown(self):
        logging.info("SHUTDOWN")
        os.system("systemctl poweroff")
        pass

    async def backupConfig(self):
        files = glob.glob("*cbpi4_config*.zip")
        for f in files:
            try:
                os.remove(f)
            except Exception as e:
                logging.error("Cannot remove old config backup: {}".format(e))

        try:
            current_date = date.today()
            current_date = str(current_date).replace("-", "_")
            output_filename = current_date + "_cbpi4_config"
        except:
            output_filename = "cbpi4_config"
        dir_name = pathlib.Path(self.cbpi.config_folder.get_file_path(""))
        shutil.make_archive(output_filename, "zip", dir_name)
        return output_filename + ".zip"

    async def plugins_list(self):
        result = []
        discovered_plugins = {
            name: importlib.import_module(name)
            for finder, name, ispkg in pkgutil.iter_modules()
            if name.startswith("cbpi") and len(name) > 4
        }
        for key, module in discovered_plugins.items():
            from importlib.metadata import version

            try:
                from importlib.metadata import distribution, metadata, version

                meta = metadata(key)
                result.append(
                    dict(
                        Name=meta["Name"],
                        Version=meta["Version"],
                        Author=meta["Author"],
                        Homepage=meta["Home-page"],
                        Summary=meta["Summary"],
                    )
                )

            except Exception as e:
                print(e)
        return tabulate(result, headers="keys")

    async def downloadlog(self, logtime):
        filename = "cbpi4.log"
        fullname = pathlib.Path(os.path.join(".", filename))
        pluginname = "cbpi4_plugins.txt"
        fullpluginname = pathlib.Path(os.path.join(".", pluginname))
        actorname = "cbpi4_actors.txt"
        fullactorname = pathlib.Path(os.path.join(".", actorname))
        sensorname = "cbpi4_sensors.txt"
        fullsensorname = pathlib.Path(os.path.join(".", sensorname))
        kettlename = "cbpi4_kettles.txt"
        fullkettlename = pathlib.Path(os.path.join(".", kettlename))

        output_filename = "cbpi4_log.zip"
        result = []
        if systemd_available:
            j = journal.Reader()
            if logtime == "b":
                j.this_boot()
            else:
                since = datetime.now() - timedelta(hours=int(logtime))
                j.seek_realtime(since)
            j.add_match(_SYSTEMD_UNIT="craftbeerpi.service")

            for entry in j:
                result.append(entry["MESSAGE"])
        else:
            try:
                logfilename = pathlib.Path(self.logsFolderPath + "/" + "cbpi.log")
                with open(logfilename) as f:
                    for line in f:
                        result.append(line.rstrip("\n"))
            except:
                pass

        try:
            with open(fullname, "w") as f:
                for line in result:
                    f.write(f"{line}\n")
        except Exception as e:
            logging.error(e)

        plugins = await self.plugins_list()
        with open(fullpluginname, "w") as f:
            f.write(plugins)

        try:
            actors = self.cbpi.actor.get_state()
            with open(fullactorname, "w") as f:
                json.dump(actors["data"], f, indent=4, sort_keys=True)
            sensors = self.cbpi.sensor.get_state()
            with open(fullsensorname, "w") as f:
                json.dump(sensors["data"], f, indent=4, sort_keys=True)
            kettles = self.cbpi.kettle.get_state()
            with open(fullkettlename, "w") as f:
                json.dump(kettles["data"], f, indent=4, sort_keys=True)
        except Exception as e:
            logging.info(e)
            self.cbpi.notify(
                "Error",
                "Creation of files failed: {}".format(e),
                NotificationType.ERROR,
            )

        try:
            with zipfile.ZipFile(output_filename, "w", zipfile.ZIP_DEFLATED) as zipObj:
                zipObj.write(fullname)
                zipObj.write(fullpluginname)
                zipObj.write(fullactorname)
                zipObj.write(fullsensorname)
                zipObj.write(fullkettlename)
        except Exception as e:
            logging.info(e)
            self.cbpi.notify(
                "Error", "Zip creation failed: {}".format(e), NotificationType.ERROR
            )

        try:
            os.remove(fullname)
            os.remove(fullpluginname)
            os.remove(fullactorname)
            os.remove(fullsensorname)
            os.remove(fullkettlename)
        except Exception as e:
            logging.info(e)
            self.cbpi.notify(
                "Error",
                "Removal of original files failed: {}".format(e),
                NotificationType.ERROR,
            )

    def allowed_file(self, filename, extension):
        return "." in filename and filename.rsplit(".", 1)[1] in set([extension])

    def recursive_chown(self, path, owner, group):
        for dirpath, dirnames, filenames in os.walk(path):
            shutil.chown(dirpath, owner, group)
            for filename in filenames:
                shutil.chown(os.path.join(dirpath, filename), owner, group)

    async def restoreConfig(self, data):
        fileData = data["File"]
        filename = fileData.filename
        backup_file = fileData.file
        content_type = fileData.content_type
        required_content = [
            "dashboard/",
            "recipes/",
            "upload/",
            "config.json",
            "config.yaml",
        ]

        if content_type == "application/x-zip-compressed" or content_type == "application/zip":
            try:
                content = backup_file.read()
                if backup_file and self.allowed_file(filename, "zip"):
                    self.path = os.path.join(
                        self.cbpi.config_folder.configFolderPath, "restored_config.zip"
                    )

                    with open(self.path, "wb") as f:
                        f.write(content)
                    with zipfile.ZipFile(self.path) as zip:
                        zip_content_list = zip.namelist()
                    zip_content = True
                    for content in required_content:
                        try:
                            check = zip_content_list.index(content)
                        except ValueError:
                            zip_content = False
                    if zip_content == True:
                        self.cbpi.notify(
                            "Success",
                            "Config backup has been uploaded",
                            NotificationType.SUCCESS,
                        )
                        self.cbpi.notify(
                            "Action Required!",
                            "Please restart the server",
                            NotificationType.WARNING,
                        )
                    else:
                        self.cbpi.notify(
                            "Error",
                            "Wrong content type. Upload failed",
                            NotificationType.ERROR,
                        )
                        os.remove(self.path)
            except Exception:
                self.cbpi.notify(
                    "Error", "Config backup upload failed", NotificationType.ERROR
                )
                pass
        else:
            self.cbpi.notify(
                "Error", "Wrong content type. Upload failed", NotificationType.ERROR
            )

    async def uploadSVG(self, data):
        fileData = data["File"]
        filename = fileData.filename
        svg_file = fileData.file
        content_type = fileData.content_type

        logging.info(content_type)

        if content_type == "image/svg+xml":
            try:
                content = svg_file.read().decode("utf-8", "replace")
                if svg_file and self.allowed_file(filename, "svg"):
                    self.path = os.path.join(
                        self.cbpi.config_folder.get_file_path("dashboard"),
                        "widgets",
                        filename,
                    )
                    logging.info(self.path)

                    with open(self.path, "w") as f:
                        f.write(content)
                    self.cbpi.notify(
                        "Success",
                        "SVG file ({}) has been uploaded.".format(filename),
                        NotificationType.SUCCESS,
                    )
            except Exception:
                self.cbpi.notify("Error", "SVG upload failed", NotificationType.ERROR)
                pass
        else:
            self.cbpi.notify(
                "Error", "Wrong content type. Upload failed", NotificationType.ERROR
            )

    async def systeminfo(self):
        """Gather system information in a worker thread.

        Everything _collect_systeminfo does is blocking I/O: psutil reads
        sysfs, and on Linux it runs iwlist. Run on the event loop, it would
        hold up every actor, sensor and step until it returned.

        The collector is a separate synchronous method so a test can put a
        slow one in its place and check that the loop keeps running.
        """
        return await asyncio.get_running_loop().run_in_executor(
            None, self._collect_systeminfo
        )

    @staticmethod
    def _cpu_temperature(fahrenheit):
        """The CPU's temperature from the cpu_thermal hwmon device, or 0.

        Only that device is read. psutil.sensors_temperatures() reads every
        hwmon device, and on a Raspberry Pi each DS18B20 probe is one: every
        read starts a new conversion on the 1-Wire bus the brewing probes
        share, at about 4 s per probe.
        """
        for hwmon in glob.glob("/sys/class/hwmon/hwmon*"):
            try:
                with open(os.path.join(hwmon, "name")) as f:
                    if f.read().strip() != "cpu_thermal":
                        continue
                with open(os.path.join(hwmon, "temp1_input")) as f:
                    celsius = int(f.read().strip()) / 1000
            except (OSError, ValueError):
                continue
            return round(celsius * 9 / 5 + 32 if fahrenheit else celsius, 1)
        return 0

    @staticmethod
    def _wlan0_rate():
        """wlan0's bit rate in Mb/s, as text, or "N/A".

        iwlist gets 2 s. A stuck iwlist would otherwise hold one of the
        default executor's worker threads for good, and other work shares
        those threads.
        """
        try:
            output = subprocess.run(
                ["iwlist", "wlan0", "rate"],
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return "N/A"
        for line in output.splitlines():
            if "Rate" in line and "=" in line and " Mb/s" in line:
                return line[line.find("=") + 1 : line.find(" Mb/s")]
        return "N/A"

    def _collect_systeminfo(self):
        logging.info("SYSTEMINFO")
        system = ""
        temp = 0
        cpuload = 0
        cpucount = 0
        cpufreq = 0
        totalmem = 0
        availmem = 0
        mempercent = 0
        eth0IP = "N/A"
        wlan0IP = "N/A"
        eth0speed = "N/A"
        wlan0speed = "N/A"

        TEMP_UNIT = self.cbpi.config.get("TEMP_UNIT", "C")
        FAHRENHEIT = False if TEMP_UNIT == "C" else True

        af_map = {
            socket.AF_INET: "IPv4",
            socket.AF_INET6: "IPv6",
        }

        try:
            if psutil.LINUX == True:
                system = "Linux"
            elif psutil.WINDOWS == True:
                system = "Windows"
            elif psutil.MACOS == True:
                system = "MacOS"
            cpuload = round(psutil.cpu_percent(interval=None), 1)
            cpucount = psutil.cpu_count(logical=False)
            cpufreq = psutil.cpu_freq()
            mem = psutil.virtual_memory()
            availmem = round((int(mem.available) / (1024 * 1024)), 1)
            mempercent = round(float(mem.percent), 1)
            totalmem = round((int(mem.total) / (1024 * 1024)), 1)
            if system == "Linux":
                temp = self._cpu_temperature(FAHRENHEIT)
            else:
                temp = "N/A"
            if system == "Linux":
                try:
                    ethernet = psutil.net_if_addrs()
                    for nic, addrs in ethernet.items():
                        if nic == "eth0":
                            for addr in addrs:
                                if (
                                    str(addr.family) == "AddressFamily.AF_INET"
                                    or str(addr.family) == "2"
                                ):
                                    if addr.address:
                                        eth0IP = addr.address
                        if nic == "wlan0":
                            for addr in addrs:
                                if (
                                    str(addr.family) == "AddressFamily.AF_INET"
                                    or str(addr.family) == "2"
                                ):
                                    if addr.address:
                                        wlan0IP = addr.address
                    info = psutil.net_if_stats()
                    try:
                        for nic in info:
                            if nic == "eth0":
                                if info[nic].isup == True:
                                    if info[nic].speed:
                                        eth0speed = info[nic].speed
                                else:
                                    eth0speed = "down"
                            if nic == "wlan0":
                                if info[nic].isup == True:
                                    wlan0speed = self._wlan0_rate()
                                else:
                                    wlan0speed = "down"
                    except Exception as e:
                        logging.info(e)
                except:
                    pass

            if system == "Windows":
                try:
                    ethernet = psutil.net_if_addrs()
                    for nic, addrs in ethernet.items():
                        if nic == "Ethernet":
                            for addr in addrs:
                                if str(addr.family) == "AddressFamily.AF_INET":
                                    if addr.address:
                                        eth0IP = addr.address
                        if nic == "WLAN":
                            for addr in addrs:
                                if str(addr.family) == "AddressFamily.AF_INET":
                                    if addr.address:
                                        wlan0IP = addr.address
                    info = psutil.net_if_stats()
                    try:
                        for nic in info:
                            if nic == "Ethernet":
                                if info[nic].isup == True:
                                    if info[nic].speed:
                                        eth0speed = info[nic].speed
                                else:
                                    eth0speed = "down"
                            if nic == "WLAN":
                                if info[nic].isup == True:
                                    if info[nic].speed:
                                        wlan0speed = info[nic].speed
                                else:
                                    wlan0speed = "down"
                    except Exception as e:
                        logging.info(e)
                except:
                    pass

        except:
            pass

        systeminfo = {
            "system": system,
            "cpuload": cpuload,
            "cpucount": cpucount,
            "cpufreq": cpufreq.current,
            "totalmem": totalmem,
            "availmem": availmem,
            "mempercent": mempercent,
            "temp": temp,
            "temp_unit": TEMP_UNIT,
            "eth0": eth0IP,
            "wlan0": wlan0IP,
            "eth0speed": eth0speed,
            "wlan0speed": wlan0speed,
        }
        return systeminfo

    async def get_memory_info(self):
        logging.info("SYSTEMINFO")
        minmem = self.cbpi.config.get("MIN_MEMORY", 200)

        # What this returns when it cannot measure matters more than it looks.
        #
        # DashboardContext polls this every five minutes and does:
        #
        #     if (data.meminfo.availmem < data.meminfo.minmem)
        #         window.location.reload(true);
        #
        # A fallback of availmem = 0 would be read as genuinely exhausted
        # memory, since 0 < 200, and force a page reload - every five minutes
        # for as long as the fault lasted, discarding whatever the brewer had
        # on screen. On a rig that can include an unanswered notification or a
        # prompt waiting on them.
        #
        # So an unavailable measurement omits the numbers entirely rather than
        # substituting one. `undefined < 200` is false in JavaScript, so the
        # shipped interface stops reloading without needing to be rebuilt.
        # Returning null would not do: JSON null coerces to 0 in `<`, which is
        # the same bug with a different spelling.
        #
        # `available` is the explicit signal for a client that wants to show
        # "unknown" rather than a blank, and for the guard that belongs in
        # DashboardContext - requiring a valid measured number before reloading,
        # instead of inferring it from a missing one.
        try:
            mem = psutil.virtual_memory()
            return {
                "totalmem": round((int(mem.total) / (1024 * 1024)), 1),
                "availmem": round((int(mem.available) / (1024 * 1024)), 1),
                "mempercent": round(float(mem.percent), 1),
                "minmem": minmem,
                "available": True,
            }
        except Exception as e:  # noqa: BLE001
            logging.warning("Could not read memory information: %s", e)
            return {"minmem": minmem, "available": False}
