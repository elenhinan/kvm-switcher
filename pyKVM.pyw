import ctypes
from ctypes import wintypes
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
import webbrowser
import winreg

from monitorcontrol import get_monitors
from monitorcontrol.vcp.vcp_windows import WindowsVCP
from PIL import Image, ImageDraw
import pystray
import pythoncom
import win32api
import wmi

# ==============================================================================
# APP METADATA, PATHS & DEFAULT CONFIGURATION
# ==============================================================================
APP_NAME = "pyKVM"
APP_VERSION = "1.0.0"
GITHUB_URL = "https://github.com/elenhinan/kvm-switcher"

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_PATH = os.path.join(BASE_DIR, "usb_kvm_switcher.log")

DEFAULT_CONFIG = {
    "target_usb_id": "VID_XXXX&PID_XXXX",
    "target_usb_name": "",
    "this_pc_inputs": [15, 15],
    "other_pc_inputs": [17, 17],
    "monitor_map": {},  # Optional per-monitor hardware ID mapping: {"AUS2707": {"this_pc": 15, "other_pc": 17}}
    "debounce_seconds": 2.5,
    "run_at_startup": False,
}

STARTUP_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_REG_NAME = "pyKVM"


def get_startup_command():
    """Returns the command string used to launch the app silently into the system tray at login."""
    if getattr(sys, "frozen", False):
        return f'"{os.path.abspath(sys.executable)}" --tray'
    py_dir = os.path.dirname(os.path.abspath(sys.executable))
    pythonw = os.path.join(py_dir, "pythonw.exe")
    interpreter = pythonw if os.path.exists(pythonw) else os.path.abspath(sys.executable)
    script_path = os.path.abspath(__file__)
    return f'"{interpreter}" "{script_path}" --tray'


def is_startup_enabled():
    """Checks Windows Registry (HKCU\\...\\Run) to see if startup is enabled."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_PATH, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, STARTUP_REG_NAME)
            return bool(val)
    except FileNotFoundError:
        return False
    except OSError as e:
        logging.debug(f"Registry read error: {e}")
        return False


def set_startup_enabled(enabled):
    """Adds or removes the application from Windows current-user startup registry key."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                cmd = get_startup_command()
                winreg.SetValueEx(key, STARTUP_REG_NAME, 0, winreg.REG_SZ, cmd)
                logging.info(f"Enabled Windows startup: {cmd}")
            else:
                try:
                    winreg.DeleteValue(key, STARTUP_REG_NAME)
                    logging.info("Disabled Windows startup.")
                except FileNotFoundError:
                    pass
        return True
    except OSError as e:
        logging.error(f"Failed to update Windows startup registry: {e}")
        return False

# Standard VCP 0x60 (Input Source) codes
KNOWN_VCP_INPUTS = [
    (15, "DisplayPort 1"),
    (16, "DisplayPort 2"),
    (17, "HDMI 1"),
    (18, "HDMI 2"),
    (19, "HDMI 3 / MHL"),
    (27, "USB-C / Thunderbolt 1"),
    (26, "USB-C / Thunderbolt 2"),
    (25, "USB-C / DP Alt"),
    (1, "VGA 1"),
    (2, "VGA 2"),
    (3, "DVI 1"),
    (4, "DVI 2"),
]
KNOWN_VCP_DICT = dict(KNOWN_VCP_INPUTS)

# ==============================================================================
# LOGGING & RUNTIME STATE
# ==============================================================================
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

config_lock = threading.Lock()
current_config = DEFAULT_CONFIG.copy()
auto_switch_enabled = True
stop_event = threading.Event()
ui_queue = queue.Queue()


def load_config():
    """Loads configuration from config.json, falling back to defaults."""
    global current_config
    loaded = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                # Support legacy keys if present
                if "desktop_inputs" in data and "this_pc_inputs" not in data:
                    data["this_pc_inputs"] = data["desktop_inputs"]
                if "laptop_inputs" in data and "other_pc_inputs" not in data:
                    data["other_pc_inputs"] = data["laptop_inputs"]
                loaded.update(data)
        except Exception as e:
            logging.error(f"Failed to load config.json: {e}")
    with config_lock:
        current_config = loaded
    return loaded


def save_config(new_cfg):
    """Persists configuration to config.json and updates active runtime state."""
    global current_config
    with config_lock:
        current_config = new_cfg.copy()
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(new_cfg, f, indent=2)
        logging.info(
            f"Configuration saved: target={new_cfg.get('target_usb_id')} "
            f"({new_cfg.get('target_usb_name')}), "
            f"this_pc={new_cfg.get('this_pc_inputs')}, "
            f"other_pc={new_cfg.get('other_pc_inputs')}"
        )
        return True
    except Exception as e:
        logging.error(f"Failed to save config.json: {e}")
        return False


def is_config_valid(cfg=None):
    if cfg is None:
        with config_lock:
            cfg = current_config.copy()
    usb_id = (cfg.get("target_usb_id") or "").strip().upper()
    return bool(usb_id and usb_id != "VID_XXXX&PID_XXXX")


# ==============================================================================
# WINDOWS CFGMGR32 DEVICE TREE & DISPLAY HELPERS
# ==============================================================================
cfgmgr32 = ctypes.windll.cfgmgr32


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wintypes.ULONG)]


def _make_guid(d1, d2, d3, b):
    return GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*b))


DEVPKEY_Device_BusReportedDeviceDesc = DEVPROPKEY(
    _make_guid(0x540B947E, 0x8B40, 0x45BC, [0xA8, 0xA2, 0x6A, 0x0B, 0x89, 0x4C, 0xBD, 0xA2]), 4
)
DEVPKEY_Device_DeviceDesc = DEVPROPKEY(
    _make_guid(0xA45C254E, 0xDF1C, 0x4EFD, [0x80, 0x20, 0x67, 0xD1, 0x46, 0xA8, 0x50, 0xE0]), 2
)
DEVPKEY_Device_FriendlyName = DEVPROPKEY(
    _make_guid(0xA45C254E, 0xDF1C, 0x4EFD, [0x80, 0x20, 0x67, 0xD1, 0x46, 0xA8, 0x50, 0xE0]), 14
)
DEVPKEY_Device_Class = DEVPROPKEY(
    _make_guid(0xA45C254E, 0xDF1C, 0x4EFD, [0x80, 0x20, 0x67, 0xD1, 0x46, 0xA8, 0x50, 0xE0]), 9
)
DEVPKEY_Device_Service = DEVPROPKEY(
    _make_guid(0xA45C254E, 0xDF1C, 0x4EFD, [0x80, 0x20, 0x67, 0xD1, 0x46, 0xA8, 0x50, 0xE0]), 6
)

GENERIC_DEVICE_NAMES = {
    "hid keyboard device",
    "hid-compliant mouse",
    "usb input device",
    "usb composite device",
    "hid-compliant device",
    "hid-compliant consumer control device",
    "hid-compliant system controller",
    "hid-compliant vendor-defined device",
    "enhanced (101- or 102-key)",
    "standard ps/2 keyboard",
    "ps/2 compatible mouse",
    "generic usb hub",
    "generic superspeed usb hub",
}

MOUSE_KEYWORDS = (
    "mouse", "basilisk", "deathadder", "viper", "naga", "orochi", "cobra",
    "g502", "g305", "g703", "g903", "superlight", "mx master", "mx anywhere",
    "mx ergo", "trackball", "aerox", "rival", "sensei", "prime", "harpoon",
    "scimitar", "dark core", "katar", "m65", "sabre", "kone", "burst", "glorious model",
)

KEYBOARD_KEYWORDS = (
    "keyboard", "keyb", "kbd", "apex", "tkl", "blackwidow", "huntsman",
    "ornata", "deathstalker", "keychron", "wooting", "k70", "k95", "k100",
    "k65", "g915", "g815", "g715", "g512", "g413", "ducky", "leopold",
    "akko", "epomaker", "nuphy", "hhkb", "realforce", "das keyboard",
    "vulcan", "magma", "cynosa",
)


def _get_devnode_str(devinst, key):
    prop_type = wintypes.ULONG()
    buf_size = wintypes.ULONG(0)
    cfgmgr32.CM_Get_DevNode_PropertyW(
        devinst, ctypes.byref(key), ctypes.byref(prop_type), None, ctypes.byref(buf_size), 0
    )
    if buf_size.value == 0:
        return None
    buf = (ctypes.c_wchar * (buf_size.value // 2))()
    res = cfgmgr32.CM_Get_DevNode_PropertyW(
        devinst, ctypes.byref(key), ctypes.byref(prop_type), buf, ctypes.byref(buf_size), 0
    )
    if res == 0:
        return buf.value
    return None


def _get_device_id(devinst):
    buf = (ctypes.c_wchar * 512)()
    if cfgmgr32.CM_Get_Device_IDW(devinst, buf, 512, 0) == 0:
        return buf.value
    return None


def extract_vid_pid(text):
    if not text:
        return None
    m = re.search(r"(VID_[0-9A-F]{4}&PID_[0-9A-F]{4})", text.upper())
    if m:
        return m.group(1)
    m_bt = re.search(r"(VID&[0-9A-F]+_PID&[0-9A-F]+)", text.upper())
    return m_bt.group(1) if m_bt else None


def _is_hub_or_controller(did, cls, srv, desc):
    did_u = (did or "").upper()
    srv_l = (srv or "").lower()
    desc_l = (desc or "").lower()
    if did_u.startswith(("USB\\ROOT_HUB", "PCI\\", "ACPI\\", "HTREE\\", "ROOT\\", "USB4\\")):
        return True
    if srv_l in ("usbhub", "usbhub3", "usbxhci", "usbehci", "usbuhci", "usbohci", "bthusb", "bthenum"):
        return True
    if "hub" in desc_l or "host controller" in desc_l:
        return True
    return False


def _determine_primary_type(name, leaves):
    """
    Infers whether a composite USB device is primarily a Keyboard or a Mouse
    using leaf descriptions, device name keywords, and interface 0 (MI_00) layout.
    """
    name_l = (name or "").lower()
    for kw in MOUSE_KEYWORDS:
        if kw in name_l:
            return "Mouse"
    for kw in KEYBOARD_KEYWORDS:
        if kw in name_l:
            return "Keyboard"

    # Check if any leaf has a non-generic description (e.g. Razer sets the Mouse leaf to product name)
    for leaf_cls, leaf_info in leaves:
        for prop in (leaf_info.get("bus"), leaf_info.get("fn"), leaf_info.get("desc")):
            if prop and prop.strip().lower() not in GENERIC_DEVICE_NAMES:
                return leaf_cls

    # Check MI_00 without COL (dedicated boot protocol interface)
    for leaf_cls, leaf_info in leaves:
        lid = (leaf_info.get("id") or "").upper()
        if "&MI_00\\" in lid and "&COL" not in lid:
            return leaf_cls

    classes = {leaf_cls for leaf_cls, _ in leaves}
    if classes == {"Keyboard"}:
        return "Keyboard"
    if classes == {"Mouse"}:
        return "Mouse"

    # Count MI_00 classes or total leaves
    kbd_count = sum(1 for c, _ in leaves if c == "Keyboard")
    mou_count = sum(1 for c, _ in leaves if c == "Mouse")
    return "Keyboard" if kbd_count >= mou_count else "Mouse"


def discover_input_devices():
    """
    Scans the Windows PnP device tree for Keyboards and Mice, walking up the parent
    chain so composite devices (with multiple MI_xx interfaces and HID collections)
    are collapsed into a single physical peripheral entry, grouped under any external USB hub.
    """
    root = wintypes.DWORD()
    if cfgmgr32.CM_Locate_DevNodeW(ctypes.byref(root), None, 0) != 0:
        return [], []

    leaves = []
    stack = [root.value]
    while stack:
        node = stack.pop()
        cls = _get_devnode_str(node, DEVPKEY_Device_Class)
        if cls in ("Keyboard", "Mouse"):
            leaves.append((node, cls))
        child = wintypes.DWORD()
        if cfgmgr32.CM_Get_Child(ctypes.byref(child), node, 0) == 0:
            cur = child.value
            while True:
                stack.append(cur)
                sib = wintypes.DWORD()
                if cfgmgr32.CM_Get_Sibling(ctypes.byref(sib), cur, 0) == 0:
                    cur = sib.value
                else:
                    break

    grouped_devices = {}
    hubs = {}

    for leaf_node, leaf_cls in leaves:
        chain = []
        cur = leaf_node
        while cur:
            did = _get_device_id(cur)
            cls = _get_devnode_str(cur, DEVPKEY_Device_Class)
            srv = _get_devnode_str(cur, DEVPKEY_Device_Service)
            bus = _get_devnode_str(cur, DEVPKEY_Device_BusReportedDeviceDesc)
            fn = _get_devnode_str(cur, DEVPKEY_Device_FriendlyName)
            desc = _get_devnode_str(cur, DEVPKEY_Device_DeviceDesc)
            chain.append({
                "node": cur,
                "id": did or "",
                "cls": cls or "",
                "srv": srv or "",
                "bus": bus,
                "fn": fn,
                "desc": desc,
            })
            parent = wintypes.DWORD()
            if cfgmgr32.CM_Get_Parent(ctypes.byref(parent), cur, 0) == 0:
                cur = parent.value
            else:
                break

        # Walk up from the HID leaf to the highest physical device node before a USB hub / host controller
        phys_idx = 0
        parent_hub_item = None
        for i, item in enumerate(chain):
            if _is_hub_or_controller(item["id"], item["cls"], item["srv"], item["desc"] or item["bus"]):
                # Record external USB hub if it has a VID_XXXX&PID_YYYY
                if extract_vid_pid(item["id"]):
                    parent_hub_item = item
                break
            if extract_vid_pid(item["id"]) or i == 0:
                phys_idx = i

        phys = chain[phys_idx]
        phys_key = phys["id"].upper()
        vid_pid = extract_vid_pid(phys["id"]) or extract_vid_pid(chain[0]["id"]) or phys["id"]

        if phys_key not in grouped_devices:
            grouped_devices[phys_key] = {
                "instance_id": phys["id"],
                "vid_pid": vid_pid,
                "leaves": [],
                "names": [],
                "fallback_names": [],
                "hub_key": None,
            }

        entry = grouped_devices[phys_key]
        entry["leaves"].append((leaf_cls, chain[0]))

        # Collect specific bus-reported or friendly names across the device's subtree
        for step in chain[: phys_idx + 1]:
            for cand in (step["bus"], step["fn"], step["desc"]):
                if cand:
                    c_clean = cand.strip()
                    if c_clean.lower() not in GENERIC_DEVICE_NAMES:
                        if c_clean not in entry["names"]:
                            entry["names"].append(c_clean)
                    else:
                        if c_clean not in entry["fallback_names"]:
                            entry["fallback_names"].append(c_clean)

        if parent_hub_item:
            hub_key = parent_hub_item["id"].upper()
            entry["hub_key"] = hub_key
            if hub_key not in hubs:
                hub_vid_pid = extract_vid_pid(parent_hub_item["id"])
                hub_name = (
                    parent_hub_item["bus"]
                    or parent_hub_item["fn"]
                    or parent_hub_item["desc"]
                    or "USB Hub"
                )
                hubs[hub_key] = {
                    "instance_id": parent_hub_item["id"],
                    "vid_pid": hub_vid_pid,
                    "name": hub_name,
                    "type": "USB Hub",
                }

    device_list = []
    for phys_key, data in grouped_devices.items():
        best_name = (
            data["names"][0]
            if data["names"]
            else (data["fallback_names"][0] if data["fallback_names"] else data["vid_pid"])
        )
        dev_type = _determine_primary_type(best_name, data["leaves"])
        device_list.append({
            "key": phys_key,
            "instance_id": data["instance_id"],
            "vid_pid": data["vid_pid"],
            "name": best_name,
            "type": dev_type,
            "leaf_count": len(data["leaves"]),
            "hub_key": data["hub_key"],
        })

    # Sort: Keyboards first, then Mice, then alphabetically
    device_list.sort(key=lambda d: (0 if d["type"] == "Keyboard" else 1, d["name"].lower()))
    hub_list = list(hubs.values())
    return device_list, hub_list


def format_vcp_input(code):
    """Formats an integer VCP 0x60 input code for combobox display."""
    try:
        c = int(code)
    except (ValueError, TypeError):
        return str(code)
    label = KNOWN_VCP_DICT.get(c, f"Custom Input (0x{c:02X})")
    return f"{c} - {label}"


def parse_vcp_input(text):
    """Extracts the integer input code from a combobox string like '15 - DisplayPort 1' or '0x11'."""
    if isinstance(text, int):
        return text
    s = str(text).strip()
    if not s:
        return 15
    m_prefix = re.match(r"^(\d+)\s*(?:-|$)", s)
    if m_prefix:
        return int(m_prefix.group(1))
    m_hex = re.match(r"^0x([0-9a-fA-F]+)$", s)
    if m_hex:
        return int(m_hex.group(1), 16)
    m_any = re.search(r"(\d+)", s)
    if m_any:
        return int(m_any.group(1))
    return 15


def discover_monitors():
    """
    Enumerates physical monitors via monitorcontrol + Win32/CfgMgr32 to retrieve:
    - Model Name (e.g. 'VG27AQ1A')
    - Hardware ID (e.g. 'AUS2707')
    - Resolution & Primary status
    - Current active VCP 0x60 input code
    - Reported capability input codes (if supported by monitor)
    """
    results = []
    try:
        hmonitors = WindowsVCP._get_hmonitors()
    except Exception:
        hmonitors = []

    try:
        monitors = get_monitors()
    except Exception as e:
        logging.error(f"Failed to enumerate monitors: {e}")
        return results

    for idx, mon in enumerate(monitors):
        model_name = f"Monitor {idx + 1}"
        hw_id = f"MON_{idx}"
        resolution = ""
        is_primary = False
        display_dev = f"DISPLAY{idx + 1}"

        if idx < len(hmonitors):
            try:
                info = win32api.GetMonitorInfo(hmonitors[idx].value)
                dev_name = info.get("Device", "")
                display_dev = dev_name.replace("\\\\.\\", "")
                rect = info.get("Monitor", (0, 0, 0, 0))
                w, h = rect[2] - rect[0], rect[3] - rect[1]
                resolution = f"{w}x{h}"
                is_primary = bool(info.get("Flags", 0) & 1)

                mon_dev_id = win32api.EnumDisplayDevices(dev_name, 0, 1).DeviceID
                if mon_dev_id and mon_dev_id.startswith("\\\\?\\"):
                    parts = mon_dev_id[4:].split("#")
                    if len(parts) >= 3:
                        instance_id = "\\".join(parts[:3])
                        hw_id = parts[1].upper()
                        devinst = wintypes.DWORD()
                        if cfgmgr32.CM_Locate_DevNodeW(ctypes.byref(devinst), instance_id, 0) == 0:
                            fn = (
                                _get_devnode_str(devinst.value, DEVPKEY_Device_FriendlyName)
                                or _get_devnode_str(devinst.value, DEVPKEY_Device_BusReportedDeviceDesc)
                                or _get_devnode_str(devinst.value, DEVPKEY_Device_DeviceDesc)
                            )
                            if fn:
                                m = re.search(r"\(([^)]+)\)", fn)
                                model_name = m.group(1) if m else fn
            except Exception as e:
                logging.debug(f"Display metadata lookup error for monitor {idx}: {e}")

        current_input = None
        reported_inputs = []
        with mon:
            try:
                val, _ = mon.vcp.get_vcp_feature(0x60)
                current_input = int(val)
            except Exception as e:
                logging.debug(f"Monitor {idx} VCP 0x60 read error: {e}")

            try:
                caps = mon.get_vcp_capabilities()
                if isinstance(caps, dict) and "inputs" in caps:
                    for inp in caps["inputs"]:
                        code = getattr(inp, "value", inp)
                        if isinstance(code, int):
                            reported_inputs.append(code)
            except Exception:
                pass

        results.append({
            "index": idx,
            "model_name": model_name,
            "hw_id": hw_id,
            "display_dev": display_dev,
            "resolution": resolution,
            "is_primary": is_primary,
            "current_input": current_input,
            "reported_inputs": reported_inputs,
        })

    return results


# ==============================================================================
# MONITOR SWITCHING LOGIC
# ==============================================================================
def switch_single_monitor(monitor_index, target_code, label=""):
    """Switches a single monitor by index to the specified VCP 0x60 input code."""
    try:
        monitors = get_monitors()
        if 0 <= monitor_index < len(monitors):
            with monitors[monitor_index]:
                monitors[monitor_index].vcp.set_vcp_feature(0x60, int(target_code))
            logging.info(
                f"Monitor {monitor_index} switched to {label} code {target_code} (0x{int(target_code):02X})."
            )
            return True
    except Exception as e:
        logging.error(f"Monitor {monitor_index} failed to switch: {e}")
    return False


def _get_monitor_hw_id_by_index(idx, hmonitors):
    """Returns the EDID hardware ID (e.g., 'AUS2707') for the given monitor index if available."""
    if idx < len(hmonitors):
        try:
            info = win32api.GetMonitorInfo(hmonitors[idx].value)
            dev_name = info.get("Device", "")
            mon_dev_id = win32api.EnumDisplayDevices(dev_name, 0, 1).DeviceID
            if mon_dev_id and mon_dev_id.startswith("\\\\?\\"):
                parts = mon_dev_id[4:].split("#")
                if len(parts) >= 3:
                    return parts[1].upper()
        except Exception:
            pass
    return None


def switch_monitors_for_mode(mode_key, label=""):
    """
    Switches all connected monitors for either 'this_pc' or 'other_pc' mode.
    Uses per-monitor hardware ID mapping if available, falling back to index list.
    """
    with config_lock:
        cfg = current_config.copy()

    input_list = cfg.get(f"{mode_key}_inputs", [15, 15])
    if not input_list:
        input_list = [15]
    monitor_map = cfg.get("monitor_map", {})

    logging.info(f"Switching monitors to {label} ({mode_key}: {input_list})...")
    try:
        try:
            hmonitors = WindowsVCP._get_hmonitors()
        except Exception:
            hmonitors = []

        monitors = get_monitors()
        for idx, monitor in enumerate(monitors):
            hw_id = _get_monitor_hw_id_by_index(idx, hmonitors)
            if hw_id and hw_id in monitor_map and mode_key in monitor_map[hw_id]:
                target_code = monitor_map[hw_id][mode_key]
            else:
                target_code = input_list[idx] if idx < len(input_list) else input_list[0]
            try:
                with monitor:
                    monitor.vcp.set_vcp_feature(0x60, int(target_code))
                logging.info(
                    f"Monitor {idx} ({hw_id or 'Unknown'}) switched to code {target_code} (0x{int(target_code):02X})."
                )
            except Exception as e:
                logging.error(f"Monitor {idx} failed to switch: {e}")
    except Exception as e:
        logging.error(f"Failed to enumerate monitors: {e}")


def log_monitor_capabilities(icon=None, item=None):
    """Logs reported DDC/CI capabilities and current input codes to the log file."""
    logging.info("--- Scanning Monitor DDC/CI Capabilities ---")
    try:
        for idx, monitor in enumerate(get_monitors()):
            with monitor:
                try:
                    current_val, _ = monitor.vcp.get_vcp_feature(0x60)
                    try:
                        caps = monitor.get_vcp_capabilities()
                        inputs_str = caps.get("inputs", "Unknown")
                    except Exception as cap_err:
                        inputs_str = f"Capabilities read unsupported ({cap_err})"
                    logging.info(
                        f"Monitor {idx} | Current Input Code: {current_val} (0x{current_val:02X}) "
                        f"| Reported Inputs: {inputs_str}"
                    )
                except Exception as e:
                    logging.error(f"Monitor {idx} capability read error: {e}")
    except Exception as e:
        logging.error(f"Error accessing monitors: {e}")


# ==============================================================================
# WMI USB WATCHER THREAD
# ==============================================================================
def usb_watcher_loop():
    """Background thread listening for PnP device arrival/removal via WMI."""
    pythoncom.CoInitialize()
    c = wmi.WMI()

    watcher_connect = c.Win32_PnPEntity.watch_for("creation", delay_secs=1)
    watcher_disconnect = c.Win32_PnPEntity.watch_for("deletion", delay_secs=1)

    last_trigger_time = 0
    last_state = None

    with config_lock:
        initial_id = current_config.get("target_usb_id", "")
    logging.info(f"Started WMI watcher looking for '{initial_id}'")

    while not stop_event.is_set():
        with config_lock:
            target_id = (current_config.get("target_usb_id") or "").strip().upper()
            debounce = float(current_config.get("debounce_seconds", 2.5))

        is_valid_target = bool(target_id and target_id != "VID_XXXX&PID_XXXX")

        # 1. Check for USB Connection
        try:
            event = watcher_connect(timeout_ms=250)
            if is_valid_target and event and target_id in (event.DeviceID or "").upper():
                now = time.time()
                if auto_switch_enabled and (now - last_trigger_time > debounce) and last_state != "THIS_PC":
                    last_trigger_time = now
                    last_state = "THIS_PC"
                    logging.info(f"Target USB connected: {event.DeviceID}")
                    switch_monitors_for_mode("this_pc", "This PC")
        except wmi.x_wmi_timed_out:
            pass
        except Exception as e:
            logging.error(f"WMI Connect Watcher error: {e}")

        # 2. Check for USB Disconnection
        try:
            event = watcher_disconnect(timeout_ms=250)
            if is_valid_target and event and target_id in (event.DeviceID or "").upper():
                now = time.time()
                if auto_switch_enabled and (now - last_trigger_time > debounce) and last_state != "OTHER_PC":
                    last_trigger_time = now
                    last_state = "OTHER_PC"
                    logging.info(f"Target USB disconnected: {event.DeviceID}")
                    switch_monitors_for_mode("other_pc", "Other PC")
        except wmi.x_wmi_timed_out:
            pass
        except Exception as e:
            logging.error(f"WMI Disconnect Watcher error: {e}")

    pythoncom.CoUninitialize()


# ==============================================================================
# CONFIGURATION GUI (TKINTER)
# ==============================================================================
class KVMConfigWindow:
    def __init__(self, root):
        self.root = root
        self.win = None
        self.devices = []
        self.hubs = []
        self.monitors = []
        self.monitor_rows = []
        self.item_to_usb = {}

    def show(self):
        if self.win is not None and self.win.winfo_exists():
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()
            return

        self.win = tk.Toplevel(self.root)
        self.win.title(f"USB KVM Switcher ({APP_NAME} v{APP_VERSION}) — Configuration")
        self.win.geometry("860x700")
        self.win.minsize(800, 600)
        self.win.configure(bg="#f5f6f8")

        try:
            bundle_dir = getattr(sys, "_MEIPASS", BASE_DIR)
            ico_path = os.path.join(bundle_dir, "pyKVM.ico")
            if not os.path.exists(ico_path):
                ico_path = os.path.join(BASE_DIR, "pyKVM.ico")
            if os.path.exists(ico_path):
                self.win.iconbitmap(ico_path)
        except Exception:
            pass

        self._setup_styles()
        self._build_ui()
        self.refresh_all()

        self.win.protocol("WM_DELETE_WINDOW", self.win.destroy)
        self.win.lift()
        self.win.focus_force()

    def _setup_styles(self):
        style = ttk.Style(self.win)
        try:
            style.theme_use("vista")
        except Exception:
            pass

        style.configure("Main.TFrame", background="#f5f6f8")
        style.configure("Card.TLabelframe", background="#ffffff", padding=12)
        style.configure(
            "Card.TLabelframe.Label",
            background="#ffffff",
            foreground="#1e293b",
            font=("Segoe UI", 10, "bold"),
        )
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("Card.TLabel", background="#ffffff", foreground="#1e293b", font=("Segoe UI", 9))
        style.configure(
            "Subtle.TLabel", background="#ffffff", foreground="#64748b", font=("Segoe UI", 8)
        )
        style.configure(
            "Header.TLabel", background="#ffffff", foreground="#0f172a", font=("Segoe UI", 9, "bold")
        )
        style.configure(
            "Badge.TLabel", background="#e2e8f0", foreground="#334155", font=("Segoe UI", 8, "bold")
        )
        style.configure("Treeview", font=("Segoe UI", 9), rowheight=26)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        style.configure("Accent.TButton", font=("Segoe UI", 9, "bold"), padding=(12, 5))
        style.configure("Action.TButton", font=("Segoe UI", 9), padding=(8, 4))

    def _build_ui(self):
        with config_lock:
            cfg = current_config.copy()

        container = ttk.Frame(self.win, style="Main.TFrame", padding=14)
        container.pack(fill="both", expand=True)

        # ------------------------------------------------------------------
        # 1. USB DEVICE SELECTION SECTION
        # ------------------------------------------------------------------
        usb_frame = ttk.LabelFrame(
            container,
            text=" 1. Select USB Trigger Device (Keyboard, Mouse, or USB Switch Hub) ",
            style="Card.TLabelframe",
        )
        usb_frame.pack(fill="both", expand=True, pady=(0, 12))

        top_bar = ttk.Frame(usb_frame, style="Card.TFrame")
        top_bar.pack(fill="x", pady=(0, 8))

        ttk.Label(
            top_bar,
            text="Composite HID interfaces are automatically merged into physical USB devices using the PnP connection tree.",
            style="Subtle.TLabel",
        ).pack(side="left")

        ttk.Button(
            top_bar, text="↻ Refresh USB Tree", style="Action.TButton", command=self.refresh_usb_devices
        ).pack(side="right")

        # Treeview for Connection Tree
        tree_container = ttk.Frame(usb_frame, style="Card.TFrame")
        tree_container.pack(fill="both", expand=True)

        columns = ("type", "vid_pid", "details")
        self.tree = ttk.Treeview(
            tree_container, columns=columns, show="tree headings", selectmode="browse", height=6
        )
        self.tree.heading("#0", text="Device / USB Connection Tree", anchor="w")
        self.tree.heading("type", text="Device Type", anchor="w")
        self.tree.heading("vid_pid", text="Hardware ID (VID & PID)", anchor="w")
        self.tree.heading("details", text="Connection Details", anchor="w")

        self.tree.column("#0", width=310, minwidth=220, stretch=True)
        self.tree.column("type", width=110, minwidth=90, stretch=False)
        self.tree.column("vid_pid", width=165, minwidth=140, stretch=False)
        self.tree.column("details", width=170, minwidth=120, stretch=True)

        scrollbar = ttk.Scrollbar(tree_container, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # Selected USB Target Row
        sel_row = ttk.Frame(usb_frame, style="Card.TFrame")
        sel_row.pack(fill="x", pady=(10, 0))

        ttk.Label(sel_row, text="Selected Trigger ID:", style="Header.TLabel").pack(side="left")
        self.usb_id_var = tk.StringVar(value=cfg.get("target_usb_id", ""))
        self.usb_name_var = tk.StringVar(value=cfg.get("target_usb_name", ""))

        usb_entry = ttk.Entry(sel_row, textvariable=self.usb_id_var, width=24, font=("Consolas", 9))
        usb_entry.pack(side="left", padx=(8, 8))

        self.usb_name_label = ttk.Label(sel_row, textvariable=self.usb_name_var, style="Subtle.TLabel")
        self.usb_name_label.pack(side="left", padx=(0, 16))

        ttk.Label(sel_row, text="Debounce (s):", style="Card.TLabel").pack(side="right", padx=(0, 4))
        self.debounce_var = tk.StringVar(value=str(cfg.get("debounce_seconds", 2.5)))
        debounce_spin = ttk.Spinbox(
            sel_row, from_=0.5, to=15.0, increment=0.5, textvariable=self.debounce_var, width=6
        )
        debounce_spin.pack(side="right", padx=(0, 6))

        # Startup option row
        startup_row = ttk.Frame(usb_frame, style="Card.TFrame")
        startup_row.pack(fill="x", pady=(8, 0))
        self.startup_var = tk.BooleanVar(value=is_startup_enabled() or bool(cfg.get("run_at_startup", False)))
        startup_cb = ttk.Checkbutton(
            startup_row,
            text="Run at Windows startup (automatically start minimized in system tray)",
            variable=self.startup_var,
            command=self._on_startup_checkbox_toggle,
        )
        startup_cb.pack(side="left")

        # ------------------------------------------------------------------
        # 2. MONITOR INPUT CONFIGURATION SECTION
        # ------------------------------------------------------------------
        mon_frame = ttk.LabelFrame(
            container,
            text=" 2. Monitor Input Assignment (DDC/CI VCP 0x60) ",
            style="Card.TLabelframe",
        )
        mon_frame.pack(fill="both", expand=True, pady=(0, 12))

        mon_top = ttk.Frame(mon_frame, style="Card.TFrame")
        mon_top.pack(fill="x", pady=(0, 10))

        ttk.Label(
            mon_top,
            text="Assign each monitor's input source for This PC (USB connected) vs. Other PC (USB disconnected).",
            style="Subtle.TLabel",
        ).pack(side="left")

        ttk.Button(
            mon_top,
            text="↻ Detect Monitors",
            style="Action.TButton",
            command=self.refresh_monitors,
        ).pack(side="right", padx=(6, 0))

        ttk.Button(
            mon_top,
            text="↙ Set All 'This PC' to Active",
            style="Action.TButton",
            command=self.use_all_current_inputs,
        ).pack(side="right")

        # Table container for monitor rows
        self.mon_table = ttk.Frame(mon_frame, style="Card.TFrame")
        self.mon_table.pack(fill="both", expand=True)

        # Quick Test All Bar
        test_bar = ttk.Frame(mon_frame, style="Card.TFrame")
        test_bar.pack(fill="x", pady=(8, 0))
        ttk.Label(test_bar, text="Quick Test All Monitors:", style="Subtle.TLabel").pack(side="left")
        ttk.Button(
            test_bar,
            text="▶ Test Switch All to 'This PC'",
            style="Action.TButton",
            command=lambda: self.test_all_monitors("this_pc"),
        ).pack(side="left", padx=(8, 6))
        ttk.Button(
            test_bar,
            text="▶ Test Switch All to 'Other PC'",
            style="Action.TButton",
            command=lambda: self.test_all_monitors("other_pc"),
        ).pack(side="left")

        # ------------------------------------------------------------------
        # 3. FOOTER / STATUS, VERSION LINK & SAVE BUTTONS
        # ------------------------------------------------------------------
        footer = ttk.Frame(container, style="Main.TFrame")
        footer.pack(fill="x")

        left_footer = ttk.Frame(footer, style="Main.TFrame")
        left_footer.pack(side="left", fill="x", expand=True)

        self.status_var = tk.StringVar(value="Ready.")
        status_lbl = ttk.Label(
            left_footer,
            textvariable=self.status_var,
            foreground="#475569",
            background="#f5f6f8",
            font=("Segoe UI", 9),
        )
        status_lbl.pack(anchor="w")

        meta_row = ttk.Frame(left_footer, style="Main.TFrame")
        meta_row.pack(anchor="w", pady=(2, 0))
        ttk.Label(
            meta_row,
            text=f"{APP_NAME} v{APP_VERSION}  •  ",
            foreground="#64748b",
            background="#f5f6f8",
            font=("Segoe UI", 8),
        ).pack(side="left")
        gh_link = tk.Label(
            meta_row,
            text="GitHub (github.com/elenhinan/kvm-switcher)",
            fg="#2563eb",
            bg="#f5f6f8",
            cursor="hand2",
            font=("Segoe UI", 8, "underline"),
        )
        gh_link.pack(side="left")
        gh_link.bind("<Button-1>", lambda _e: webbrowser.open(GITHUB_URL))

        ttk.Button(footer, text="Cancel", style="Action.TButton", command=self.win.destroy).pack(
            side="right", padx=(6, 0)
        )
        ttk.Button(
            footer, text="Apply", style="Action.TButton", command=lambda: self.save_settings(close=False)
        ).pack(side="right", padx=(6, 0))
        ttk.Button(
            footer,
            text="Save & Close",
            style="Accent.TButton",
            command=lambda: self.save_settings(close=True),
        ).pack(side="right")

    def refresh_all(self):
        self.refresh_usb_devices()
        self.refresh_monitors()

    def refresh_usb_devices(self):
        self.status_var.set("Scanning USB connection tree...")
        self.win.update_idletasks()

        self.devices, self.hubs = discover_input_devices()
        self.item_to_usb.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)

        hub_nodes = {}
        # Only create parent hub tree nodes for hubs that actually have detected input devices
        used_hub_keys = {d["hub_key"] for d in self.devices if d.get("hub_key")}
        for hub in self.hubs:
            hkey = hub["instance_id"].upper()
            if hkey in used_hub_keys:
                child_count = sum(1 for d in self.devices if d.get("hub_key") == hkey)
                iid = self.tree.insert(
                    "",
                    "end",
                    text=f"  [Hub] {hub['name']}",
                    values=("USB Hub", hub["vid_pid"], f"{child_count} connected input device(s)"),
                    open=True,
                )
                hub_nodes[hkey] = iid
                self.item_to_usb[iid] = (hub["vid_pid"], f"{hub['name']} (USB Hub)")

        target_id = self.usb_id_var.get().strip().upper()
        item_to_select = None

        for dev in self.devices:
            parent_iid = hub_nodes.get(dev["hub_key"], "")
            prefix = "[Keyboard]" if dev["type"] == "Keyboard" else "[Mouse]"
            merged_txt = (
                f"{dev['leaf_count']} HID endpoints merged"
                if dev["leaf_count"] > 1
                else "1 HID endpoint"
            )
            iid = self.tree.insert(
                parent_iid,
                "end",
                text=f"  {prefix} {dev['name']}",
                values=(dev["type"], dev["vid_pid"], merged_txt),
            )
            self.item_to_usb[iid] = (dev["vid_pid"], dev["name"])
            if target_id and target_id in dev["vid_pid"].upper():
                item_to_select = iid

        # Check if user had selected a hub directly
        if not item_to_select and target_id:
            for iid, (vpid, _) in self.item_to_usb.items():
                if target_id in vpid.upper():
                    item_to_select = iid
                    break

        if item_to_select:
            self.tree.selection_set(item_to_select)
            self.tree.see(item_to_select)

        total_leaves = sum(d["leaf_count"] for d in self.devices)
        self.status_var.set(
            f"Detected {len(self.devices)} physical input device(s) (merged from {total_leaves} HID endpoints)."
        )

    def _on_tree_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        if iid in self.item_to_usb:
            vid_pid, name = self.item_to_usb[iid]
            self.usb_id_var.set(vid_pid)
            self.usb_name_var.set(f"({name})")

    def _on_startup_checkbox_toggle(self):
        enabled = bool(self.startup_var.get())
        if set_startup_enabled(enabled):
            with config_lock:
                current_config["run_at_startup"] = enabled
            state_txt = "Enabled" if enabled else "Disabled"
            self.status_var.set(f"{state_txt} automatic launch at Windows startup.")
        else:
            self.status_var.set("Failed to update Windows startup registry setting.")

    def refresh_monitors(self):
        self.status_var.set("Reading monitor DDC/CI information...")
        self.win.update_idletasks()

        for widget in self.mon_table.winfo_children():
            widget.destroy()
        self.monitor_rows.clear()

        with config_lock:
            cfg = current_config.copy()

        this_pc_list = cfg.get("this_pc_inputs", [15, 15])
        other_pc_list = cfg.get("other_pc_inputs", [17, 17])
        monitor_map = cfg.get("monitor_map", {})

        self.monitors = discover_monitors()

        if not self.monitors:
            ttk.Label(
                self.mon_table,
                text="No DDC/CI compatible monitors were detected. Make sure DDC/CI is enabled in your monitor OSD.",
                style="Card.TLabel",
            ).pack(anchor="w", pady=8)
            self.status_var.set("No DDC/CI monitors detected.")
            return

        # Table Headers
        headers = [
            ("Monitor", 0),
            ("Active Input", 1),
            ("'This PC' Input (USB Connected)", 2),
            ("'Other PC' Input (USB Disconnected)", 3),
        ]
        for title, col in headers:
            ttk.Label(self.mon_table, text=title, style="Header.TLabel").grid(
                row=0, column=col, sticky="w", padx=(0, 14), pady=(0, 6)
            )
        self.mon_table.columnconfigure(0, weight=1)
        self.mon_table.columnconfigure(2, weight=1)
        self.mon_table.columnconfigure(3, weight=1)

        for idx, mon in enumerate(self.monitors):
            row_idx = idx + 1
            hw_id = mon["hw_id"]

            # Determine saved values for this monitor
            saved_map = monitor_map.get(hw_id, {})
            default_this = saved_map.get(
                "this_pc",
                this_pc_list[idx] if idx < len(this_pc_list) else (this_pc_list[0] if this_pc_list else 15),
            )
            default_other = saved_map.get(
                "other_pc",
                other_pc_list[idx] if idx < len(other_pc_list) else (other_pc_list[0] if other_pc_list else 17),
            )

            # Build option list (standard VCP codes + any custom/active codes on this monitor)
            option_codes = [code for code, _ in KNOWN_VCP_INPUTS]
            for extra_code in [mon["current_input"], default_this, default_other] + mon["reported_inputs"]:
                if isinstance(extra_code, int) and extra_code not in option_codes:
                    option_codes.insert(0, extra_code)
            combobox_values = [format_vcp_input(c) for c in option_codes]

            # Column 0: Monitor Name & Resolution
            info_frame = ttk.Frame(self.mon_table, style="Card.TFrame")
            info_frame.grid(row=row_idx, column=0, sticky="w", padx=(0, 14), pady=6)

            primary_tag = " • Primary" if mon["is_primary"] else ""
            ttk.Label(
                info_frame,
                text=f"Monitor {idx + 1}: {mon['model_name']}",
                style="Header.TLabel",
            ).pack(anchor="w")
            ttk.Label(
                info_frame,
                text=f"{mon['display_dev']} ({mon['resolution']}{primary_tag})",
                style="Subtle.TLabel",
            ).pack(anchor="w")

            # Column 1: Current Active Input + "Use for This PC" button
            cur_frame = ttk.Frame(self.mon_table, style="Card.TFrame")
            cur_frame.grid(row=row_idx, column=1, sticky="w", padx=(0, 14), pady=6)

            cur_code = mon["current_input"]
            cur_text = format_vcp_input(cur_code) if cur_code is not None else "Unknown"
            ttk.Label(cur_frame, text=cur_text, style="Card.TLabel").pack(anchor="w")

            this_var = tk.StringVar(value=format_vcp_input(default_this))
            other_var = tk.StringVar(value=format_vcp_input(default_other))

            if cur_code is not None:
                ttk.Button(
                    cur_frame,
                    text="Use for 'This PC' →",
                    style="Action.TButton",
                    command=lambda v=this_var, c=cur_code: v.set(format_vcp_input(c)),
                ).pack(anchor="w", pady=(2, 0))

            # Column 2: This PC Combobox + Test button
            this_frame = ttk.Frame(self.mon_table, style="Card.TFrame")
            this_frame.grid(row=row_idx, column=2, sticky="ew", padx=(0, 14), pady=6)
            this_cb = ttk.Combobox(this_frame, textvariable=this_var, values=combobox_values, width=23)
            this_cb.pack(side="left", fill="x", expand=True)
            ttk.Button(
                this_frame,
                text="Test",
                width=5,
                style="Action.TButton",
                command=lambda i=idx, v=this_var: self.test_single_monitor(i, v.get(), "This PC"),
            ).pack(side="left", padx=(4, 0))

            # Column 3: Other PC Combobox + Test button
            other_frame = ttk.Frame(self.mon_table, style="Card.TFrame")
            other_frame.grid(row=row_idx, column=3, sticky="ew", pady=6)
            other_cb = ttk.Combobox(other_frame, textvariable=other_var, values=combobox_values, width=23)
            other_cb.pack(side="left", fill="x", expand=True)
            ttk.Button(
                other_frame,
                text="Test",
                width=5,
                style="Action.TButton",
                command=lambda i=idx, v=other_var: self.test_single_monitor(i, v.get(), "Other PC"),
            ).pack(side="left", padx=(4, 0))

            self.monitor_rows.append({
                "index": idx,
                "hw_id": hw_id,
                "model_name": mon["model_name"],
                "current_input": cur_code,
                "this_var": this_var,
                "other_var": other_var,
            })

        self.status_var.set(
            f"Loaded {len(self.devices)} physical USB input device(s) and {len(self.monitors)} monitor(s)."
        )

    def use_all_current_inputs(self):
        updated = 0
        for row in self.monitor_rows:
            if row["current_input"] is not None:
                row["this_var"].set(format_vcp_input(row["current_input"]))
                updated += 1
        self.status_var.set(f"Assigned current active inputs to 'This PC' for {updated} monitor(s).")

    def test_single_monitor(self, monitor_index, raw_value, mode_label):
        code = parse_vcp_input(raw_value)
        self.status_var.set(f"Testing Monitor {monitor_index + 1} -> {format_vcp_input(code)} ({mode_label})...")
        self.win.update_idletasks()
        ok = switch_single_monitor(monitor_index, code, mode_label)
        if ok:
            self.status_var.set(f"Switched Monitor {monitor_index + 1} to {format_vcp_input(code)}.")
        else:
            self.status_var.set(f"Failed to switch Monitor {monitor_index + 1}.")

    def test_all_monitors(self, mode_key):
        for row in self.monitor_rows:
            val_str = row["this_var"].get() if mode_key == "this_pc" else row["other_var"].get()
            code = parse_vcp_input(val_str)
            switch_single_monitor(row["index"], code, mode_key)
        label = "This PC" if mode_key == "this_pc" else "Other PC"
        self.status_var.set(f"Sent '{label}' input commands to all {len(self.monitor_rows)} monitor(s).")

    def save_settings(self, close=False):
        usb_id = self.usb_id_var.get().strip().upper()
        if not usb_id or usb_id == "VID_XXXX&PID_XXXX":
            messagebox.showwarning(
                "Select USB Trigger Device",
                "Please select a Keyboard, Mouse, or USB Hub from the list (or enter a valid VID_XXXX&PID_XXXX).",
                parent=self.win,
            )
            return

        try:
            debounce = float(self.debounce_var.get())
            if debounce < 0.1:
                debounce = 0.5
        except ValueError:
            debounce = 2.5

        this_pc_inputs = []
        other_pc_inputs = []
        monitor_map = {}

        for row in self.monitor_rows:
            t_code = parse_vcp_input(row["this_var"].get())
            o_code = parse_vcp_input(row["other_var"].get())
            this_pc_inputs.append(t_code)
            other_pc_inputs.append(o_code)
            monitor_map[row["hw_id"]] = {"this_pc": t_code, "other_pc": o_code}

        # Preserve previous list if no monitors were currently detected
        if not this_pc_inputs:
            with config_lock:
                this_pc_inputs = current_config.get("this_pc_inputs", [15, 15])
                other_pc_inputs = current_config.get("other_pc_inputs", [17, 17])
                monitor_map = current_config.get("monitor_map", {})

        clean_name = self.usb_name_var.get().strip().strip("()")
        run_at_startup = bool(self.startup_var.get())
        set_startup_enabled(run_at_startup)

        new_cfg = {
            "target_usb_id": usb_id,
            "target_usb_name": clean_name,
            "this_pc_inputs": this_pc_inputs,
            "other_pc_inputs": other_pc_inputs,
            "monitor_map": monitor_map,
            "debounce_seconds": debounce,
            "run_at_startup": run_at_startup,
        }

        if save_config(new_cfg):
            self.status_var.set(f"Configuration saved to {os.path.basename(CONFIG_PATH)}.")
            if close and self.win is not None:
                self.win.destroy()
        else:
            messagebox.showerror("Error", "Failed to save config.json. Check log for details.", parent=self.win)


# ==============================================================================
# SYSTEM TRAY & APPLICATION LIFECYCLE
# ==============================================================================
def create_tray_icon(color="#2ecc71"):
    """Generates a simple 64x64 monitor icon in memory for the system tray."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Monitor bezel
    draw.rectangle([6, 8, 58, 44], outline=color, width=4)
    draw.rectangle([12, 14, 52, 38], fill=color)
    # Stand
    draw.rectangle([28, 44, 36, 54], fill=color)
    draw.rectangle([18, 54, 46, 58], fill=color)
    return img


def toggle_auto_switch(icon, item):
    global auto_switch_enabled
    auto_switch_enabled = not auto_switch_enabled
    icon.icon = create_tray_icon("#2ecc71" if auto_switch_enabled else "#95a5a6")
    logging.info(f"Auto-switching set to: {auto_switch_enabled}")


def toggle_run_at_startup(icon, item):
    new_state = not is_startup_enabled()
    if set_startup_enabled(new_state):
        with config_lock:
            cfg = current_config.copy()
        cfg["run_at_startup"] = new_state
        save_config(cfg)
        ui_queue.put("SYNC_STARTUP")


def request_open_config(icon=None, item=None):
    ui_queue.put("OPEN_CONFIG")


def quit_app(icon, item):
    logging.info("Shutting down USB KVM Tray Switcher...")
    stop_event.set()
    icon.stop()
    ui_queue.put("QUIT")


def main():
    # Enable High-DPI awareness on Windows for crisp GUI fonts
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    cfg = load_config()
    if cfg.get("run_at_startup"):
        set_startup_enabled(True)

    # Hidden Tkinter root on main thread to host the Configuration Toplevel safely
    root = tk.Tk()
    root.withdraw()
    config_ui = KVMConfigWindow(root)

    # Start WMI USB watcher thread
    watcher_thread = threading.Thread(target=usb_watcher_loop, daemon=True)
    watcher_thread.start()

    # Build and start System Tray icon in a background thread
    menu = pystray.Menu(
        pystray.MenuItem("Configure...", request_open_config, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Auto-Switch Enabled",
            toggle_auto_switch,
            checked=lambda item: auto_switch_enabled,
        ),
        pystray.MenuItem(
            "Run at Windows Startup",
            toggle_run_at_startup,
            checked=lambda item: is_startup_enabled(),
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Switch to This PC",
            lambda icon, item: switch_monitors_for_mode("this_pc", "This PC"),
        ),
        pystray.MenuItem(
            "Switch to Other PC",
            lambda icon, item: switch_monitors_for_mode("other_pc", "Other PC"),
        ),
        pystray.MenuItem("Log Detected Inputs", log_monitor_capabilities),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            f"{APP_NAME} v{APP_VERSION} (GitHub)",
            lambda icon, item: webbrowser.open(GITHUB_URL),
        ),
        pystray.MenuItem("Quit", quit_app),
    )

    icon = pystray.Icon(
        "USB_KVM_Switcher",
        create_tray_icon("#2ecc71"),
        f"USB KVM Display Switcher (v{APP_VERSION})",
        menu,
    )
    tray_thread = threading.Thread(target=icon.run, daemon=True)
    tray_thread.start()

    # Start silently in the system tray when launched with --tray (Windows startup)
    # or when already configured (unless --config is explicitly passed)
    force_tray_only = any(arg in ("--tray", "--startup", "--minimized") for arg in sys.argv)
    if "--config" in sys.argv or (not force_tray_only and not is_config_valid()):
        ui_queue.put("OPEN_CONFIG")

    def poll_ui_queue():
        try:
            while True:
                msg = ui_queue.get_nowait()
                if msg == "OPEN_CONFIG":
                    config_ui.show()
                elif msg == "SYNC_STARTUP":
                    if config_ui.win is not None and config_ui.win.winfo_exists() and hasattr(config_ui, "startup_var"):
                        config_ui.startup_var.set(is_startup_enabled())
                elif msg == "QUIT":
                    root.quit()
                    root.destroy()
                    return
        except queue.Empty:
            pass
        if not stop_event.is_set():
            root.after(150, poll_ui_queue)

    root.after(100, poll_ui_queue)
    root.mainloop()


if __name__ == "__main__":
    main()
