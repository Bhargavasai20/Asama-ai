#!/usr/bin/env python3
"""Local hardware assistant with typed, read-only Linux and Windows collectors."""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
MAX_BODY_BYTES = 48 * 1024
MAX_HISTORY_MESSAGES = 10
MAX_TOOL_ROUNDS = 3
LLM_URL = os.environ.get(
    "HARDWARE_ASSISTANT_LLM_URL", "http://127.0.0.1:1234/v1/chat/completions"
)
LLM_MODEL = os.environ.get("HARDWARE_ASSISTANT_LLM_MODEL", "local-model")
LLM_TIMEOUT_SECONDS = 20


class ToolError(Exception):
    """A safe, user-presentable tool collection failure."""


TOOL_DESCRIPTIONS = {
    "get_memory_summary": "Read installed and available memory from operating-system hardware reports.",
    "get_cpu_info": "Read processor model and logical CPU count from operating-system hardware reports.",
    "get_storage_devices": "List physical disks and their reported media and transport type.",
    "get_gpu_info": "List display adapters from operating-system hardware reports.",
    "get_memory_devices": "Read firmware memory slots and maximum capacity without privilege escalation.",
    "get_hardware_overview": "Collect a summary of memory, CPU, storage and graphics.",
}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    }
    for name, description in TOOL_DESCRIPTIONS.items()
]


def require_supported_platform() -> None:
    if platform.system() not in ("Linux", "Windows"):
        raise ToolError(
            f"Hardware collection is not yet supported on {platform.system()}. "
            "Supported platforms are Linux and Windows."
        )


def read_linux_file(path: str) -> str:
    if platform.system() != "Linux":
        raise ToolError("Linux system files are only available on Linux.")
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except PermissionError as error:
        raise ToolError(f"Permission denied reading {path}. No privilege escalation was attempted.") from error
    except FileNotFoundError as error:
        raise ToolError(f"{path} is unavailable on this system.") from error
    except OSError as error:
        raise ToolError(f"Could not read {path}: {error}") from error


def parse_meminfo(text: str) -> dict:
    values = {}
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z_()]+):\s+(\d+)\s*(kB)?\s*$", line)
        if match:
            values[match.group(1)] = int(match.group(2)) * (1024 if match.group(3) else 1)
    if "MemTotal" not in values:
        raise ToolError("The kernel memory report did not include MemTotal.")
    return {
        "totalBytes": values["MemTotal"],
        "availableBytes": values.get("MemAvailable"),
        "source": "/proc/meminfo",
        "collectedAt": time.time(),
    }


def parse_cpuinfo(text: str, logical_cpu_count: int | None = None) -> dict:
    model = None
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() in ("model name", "Hardware", "Processor"):
            model = value.strip()
            if model:
                break
    if not model:
        model = platform.machine() or "Unknown processor"
    return {
        "model": model,
        "logicalCpus": logical_cpu_count or os.cpu_count(),
        "architecture": platform.machine(),
        "source": "/proc/cpuinfo",
        "collectedAt": time.time(),
    }


def parse_lsblk(payload: dict) -> list[dict]:
    devices = []
    for device in payload.get("blockdevices", []):
        if device.get("type") != "disk":
            continue
        name = str(device.get("name", ""))
        transport = str(device.get("tran") or "").lower()
        if transport == "nvme" or name.startswith("nvme"):
            kind = "NVMe SSD"
        elif str(device.get("rota", "")) == "0":
            kind = "SSD"
        elif str(device.get("rota", "")) == "1":
            kind = "HDD"
        else:
            kind = "unknown"
        devices.append({
            "name": name,
            "sizeBytes": int(device["size"]) if device.get("size") is not None else None,
            "model": device.get("model"),
            "transport": device.get("tran"),
            "kind": kind,
            "mountpoints": device.get("mountpoints") or [],
        })
    return devices


def parse_lspci(text: str) -> list[dict]:
    adapters = []
    for line in text.splitlines():
        try:
            fields = shlex.split(line)
        except ValueError:
            continue
        if len(fields) >= 3 and fields[1] in (
            "VGA compatible controller", "3D controller", "Display controller"
        ):
            adapters.append({
                "class": fields[1],
                "vendor": fields[2],
                "model": " ".join(fields[3:]) or None,
            })
    return adapters


def parse_capacity_bytes(value: str | None) -> int | None:
    if not value:
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(bytes?|[kmgt]i?b)\s*", value, re.IGNORECASE)
    if not match:
        return None
    unit = match.group(2).lower()
    exponent = {"b": 0, "byte": 0, "bytes": 0, "kb": 1, "kib": 1,
                "mb": 2, "mib": 2, "gb": 3, "gib": 3, "tb": 4, "tib": 4}.get(unit)
    if exponent is None:
        return None
    return int(float(match.group(1)) * (1024 ** exponent))


def parse_dmidecode(text: str, array_text: str = "") -> dict:
    devices = []
    current = None
    for line in text.splitlines():
        if line.strip() == "Memory Device":
            if current:
                devices.append(current)
            current = {}
            continue
        if current is None or ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        if key in ("Size", "Locator", "Bank Locator", "Type", "Speed", "Configured Memory Speed"):
            current[key] = value
    if current:
        devices.append(current)
    installed = [item for item in devices if item.get("Size") not in (None, "No Module Installed", "Unknown")]
    empty_slots = sum(item.get("Size") == "No Module Installed" for item in devices)
    maximum_match = re.search(r"^\s*Maximum Capacity:\s*(.+?)\s*$", array_text, re.MULTILINE)
    maximum_capacity = parse_capacity_bytes(maximum_match.group(1)) if maximum_match else None
    for item in devices:
        item["sizeBytes"] = parse_capacity_bytes(item.get("Size"))
    return {
        "devices": devices,
        "installedSlots": len(installed),
        "emptySlots": empty_slots,
        "totalSlotsReported": len(devices),
        "installedBytes": (
            sum(item["sizeBytes"] or 0 for item in installed)
            if any(item["sizeBytes"] is not None for item in installed)
            else None
        ),
        "maximumCapacityBytes": maximum_capacity,
        "source": "dmidecode --type memory",
        "collectedAt": time.time(),
    }


def parse_windows_disks(payload: object) -> list[dict]:
    devices = payload if isinstance(payload, list) else [payload]
    result = []
    for device in devices:
        if not isinstance(device, dict):
            continue
        bus = str(device.get("busType") or device.get("interfaceType") or "").strip()
        media = str(device.get("mediaType") or "").strip()
        pnp_id = str(device.get("pnpDeviceId") or "").lower()
        if bus.lower() == "nvme" or "nvme" in pnp_id:
            kind = "NVMe SSD"
        elif media.lower() in ("ssd", "solid state"):
            kind = "SSD"
        elif media.lower() in ("hdd", "hard disk"):
            kind = "HDD"
        else:
            kind = "unknown"
        size = device.get("size")
        try:
            size_bytes = int(size) if size is not None else None
        except (TypeError, ValueError):
            size_bytes = None
        name = device.get("name") or device.get("deviceId") or device.get("model")
        result.append({
            "name": str(name or "Disk"),
            "sizeBytes": size_bytes,
            "model": device.get("model") or device.get("friendlyName"),
            "transport": bus or None,
            "kind": kind,
            "mountpoints": [],
        })
    return result


def parse_windows_memory_devices(payload: dict) -> dict:
    raw_devices = payload.get("devices", [])
    devices = raw_devices if isinstance(raw_devices, list) else [raw_devices]
    devices = [item for item in devices if isinstance(item, dict)]
    arrays = payload.get("arrays", [])
    arrays = arrays if isinstance(arrays, list) else [arrays]

    installed = []
    for device in devices:
        size = device.get("capacity")
        try:
            size_bytes = int(size) if size not in (None, "") else None
        except (TypeError, ValueError):
            size_bytes = None
        if size_bytes is not None and size_bytes > 0:
            installed.append(size_bytes)
        device["sizeBytes"] = size_bytes

    maximum_capacity = 0
    for array in arrays:
        if not isinstance(array, dict):
            continue
        try:
            maximum_capacity += int(array.get("maxCapacity") or 0)
        except (TypeError, ValueError):
            pass

    return {
        "devices": devices,
        "installedSlots": None,
        "emptySlots": None,
        "totalSlotsReported": None,
        "reportedDeviceCount": len(devices),
        "installedBytes": sum(installed) if installed else None,
        "maximumCapacityBytes": maximum_capacity or None,
        "source": "Windows CIM memory reports",
        "collectedAt": time.time(),
    }


def run_powershell_json(script: str) -> object:
    if platform.system() != "Windows":
        raise ToolError("Windows hardware inspection is only available on Windows.")
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if not executable:
        raise ToolError("Windows PowerShell was not found. Hardware inspection is unavailable.")
    try:
        result = subprocess.run(
            [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired as error:
        raise ToolError("Windows hardware inspection timed out.") from error
    except OSError as error:
        raise ToolError(f"Could not start Windows PowerShell: {error}") from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise ToolError(f"Windows hardware inspection failed: {detail[:300] or 'command failed'}.")
    output = result.stdout.strip()
    if not output:
        return []
    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        raise ToolError("Windows PowerShell returned data in an unsupported format.") from error


def run_fixed_command(arguments: list[str], executable_name: str) -> str:
    if platform.system() != "Linux":
        raise ToolError(f"{executable_name} inspection is only available on Linux.")
    executable = shutil.which(executable_name)
    if not executable:
        raise ToolError(f"{executable_name} is not installed. This inspection is unavailable.")
    try:
        result = subprocess.run(
            [executable, *arguments],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except subprocess.TimeoutExpired as error:
        raise ToolError(f"{executable_name} timed out while reading hardware information.") from error
    except OSError as error:
        raise ToolError(f"Could not run {executable_name}: {error}") from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        if result.returncode in (1, 2) and ("permission" in detail.lower() or "root" in detail.lower()):
            raise ToolError(
                f"{executable_name} needs additional permissions for this information. "
                "No sudo command was run; you can skip this inspection."
            )
        raise ToolError(f"{executable_name} could not collect this information: {detail[:300] or 'command failed'}.")
    return result.stdout


def get_memory_summary() -> dict:
    if platform.system() == "Windows":
        payload = run_powershell_json(
            "$computer = Get-CimInstance -ClassName Win32_ComputerSystem; "
            "$os = Get-CimInstance -ClassName Win32_OperatingSystem; "
            "[pscustomobject]@{ totalBytes = [long]$computer.TotalPhysicalMemory; "
            "availableBytes = [long]$os.FreePhysicalMemory * 1024; "
            "source = 'Win32_ComputerSystem and Win32_OperatingSystem' } | ConvertTo-Json -Compress"
        )
        if not isinstance(payload, dict) or not payload.get("totalBytes"):
            raise ToolError("Windows did not report the total installed memory.")
        payload["collectedAt"] = time.time()
        return payload
    require_supported_platform()
    return parse_meminfo(read_linux_file("/proc/meminfo"))


def get_cpu_info() -> dict:
    if platform.system() == "Windows":
        payload = run_powershell_json(
            "$cpu = Get-CimInstance -ClassName Win32_Processor | Select-Object -First 1; "
            "[pscustomobject]@{ model = $cpu.Name; logicalCpus = $cpu.NumberOfLogicalProcessors; "
            "architecture = $cpu.AddressWidth; source = 'Win32_Processor' } | ConvertTo-Json -Compress"
        )
        if not isinstance(payload, dict) or not payload.get("model"):
            raise ToolError("Windows did not report processor details.")
        payload["collectedAt"] = time.time()
        return payload
    return parse_cpuinfo(read_linux_file("/proc/cpuinfo"))


def get_storage_devices() -> dict:
    if platform.system() == "Windows":
        payload = run_powershell_json(
            "$physical = @(); "
            "try { $physical = @(Get-PhysicalDisk -ErrorAction Stop) } catch { $physical = @() }; "
            "$disks = if ($physical.Count -gt 0) { "
            "@($physical | ForEach-Object { [pscustomobject]@{ "
            "name = $_.DeviceId; friendlyName = $_.FriendlyName; size = $_.Size; "
            "mediaType = [string]$_.MediaType; busType = [string]$_.BusType } }) "
            "} else { "
            "@(Get-CimInstance -ClassName Win32_DiskDrive | ForEach-Object { "
            "[pscustomobject]@{ deviceId = $_.DeviceID; model = $_.Model; size = $_.Size; "
            "mediaType = [string]$_.MediaType; interfaceType = [string]$_.InterfaceType; "
            "pnpDeviceId = [string]$_.PNPDeviceID } }) "
            "}; @($disks) | ConvertTo-Json -Depth 3 -Compress"
        )
        return {
            "devices": parse_windows_disks(payload),
            "source": "Windows physical disk reports",
            "collectedAt": time.time(),
        }
    output = run_fixed_command(
        ["-J", "-b", "-o", "NAME,TYPE,SIZE,ROTA,TRAN,MODEL,MOUNTPOINTS"], "lsblk"
    )
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise ToolError("lsblk returned data in an unsupported format.") from error
    return {"devices": parse_lsblk(payload), "source": "lsblk", "collectedAt": time.time()}


def get_gpu_info() -> dict:
    if platform.system() == "Windows":
        payload = run_powershell_json(
            "@(Get-CimInstance -ClassName Win32_VideoController | ForEach-Object { "
            "[pscustomobject]@{ name = $_.Name; adapterRamBytes = $_.AdapterRAM; "
            "driverVersion = $_.DriverVersion } }) | ConvertTo-Json -Depth 3 -Compress"
        )
        adapters = payload if isinstance(payload, list) else ([payload] if isinstance(payload, dict) else [])
        return {"adapters": adapters, "source": "Win32_VideoController", "collectedAt": time.time()}
    return {"adapters": parse_lspci(run_fixed_command(["-mm"], "lspci")),
            "source": "lspci", "collectedAt": time.time()}


def get_memory_devices() -> dict:
    if platform.system() == "Windows":
        payload = run_powershell_json(
            "$devices = @(Get-CimInstance -ClassName Win32_PhysicalMemory | ForEach-Object { "
            "[pscustomobject]@{ capacity = $_.Capacity; locator = $_.DeviceLocator; "
            "bankLabel = $_.BankLabel; memoryType = $_.SMBIOSMemoryType; speed = $_.Speed } }); "
            "$arrays = @(Get-CimInstance -ClassName Win32_PhysicalMemoryArray | ForEach-Object { "
            "[pscustomobject]@{ memoryDevices = $_.MemoryDevices; "
            "maxCapacity = if ($_.MaxCapacityEx) { [long]$_.MaxCapacityEx * 1024 } "
            "elseif ($_.MaxCapacity) { [long]$_.MaxCapacity * 1024 } else { 0 } } }); "
            "[pscustomobject]@{ devices = $devices; arrays = $arrays } | ConvertTo-Json -Depth 4 -Compress"
        )
        if not isinstance(payload, dict):
            raise ToolError("Windows did not return a memory device report.")
        return parse_windows_memory_devices(payload)
    devices = run_fixed_command(["--type", "memory"], "dmidecode")
    try:
        memory_array = run_fixed_command(["--type", "16"], "dmidecode")
        return parse_dmidecode(devices, memory_array)
    except ToolError as error:
        result = parse_dmidecode(devices)
        result["capacityUnavailable"] = str(error)
        return result


def get_hardware_overview() -> dict:
    result = {}
    for name, collector in (
        ("memory", get_memory_summary),
        ("cpu", get_cpu_info),
        ("storage", get_storage_devices),
        ("graphics", get_gpu_info),
    ):
        try:
            result[name] = collector()
        except ToolError as error:
            result[name] = {"unavailable": str(error)}
    return result


COLLECTORS = {
    "get_memory_summary": get_memory_summary,
    "get_cpu_info": get_cpu_info,
    "get_storage_devices": get_storage_devices,
    "get_gpu_info": get_gpu_info,
    "get_memory_devices": get_memory_devices,
    "get_hardware_overview": get_hardware_overview,
}


def execute_tool(name: str, arguments: object) -> dict:
    if name not in COLLECTORS:
        raise ToolError(f"Unsupported inspection tool: {name}")
    if not isinstance(arguments, dict) or arguments:
        raise ToolError(f"{name} accepts no arguments.")
    return COLLECTORS[name]()


def select_offline_tool(question: str) -> str | None:
    text = question.lower()
    if any(word in text for word in ("slot", "upgrade", "add more ram", "maximum memory", "max ram", "memory device")):
        return "get_memory_devices"
    if any(word in text for word in ("gpu", "graphics", "video card", "display adapter")):
        return "get_gpu_info"
    if any(word in text for word in ("ssd", "nvme", "disk", "drive", "storage")):
        return "get_storage_devices"
    if any(word in text for word in ("cpu", "processor", "core")):
        return "get_cpu_info"
    if any(word in text for word in ("ram", "memory")):
        return "get_memory_summary"
    if any(word in text for word in ("hardware", "specs", "specification", "overview", "my machine")):
        return "get_hardware_overview"
    return None


def format_bytes(value: int | None) -> str:
    if value is None:
        return "unavailable"
    return f"{value / (1024 ** 3):.1f} GiB"


def offline_answer(question: str) -> dict:
    tool_name = select_offline_tool(question)
    if tool_name is None:
        return {
            "answer": "I can inspect RAM, memory slots, CPU, storage, GPU, or give a hardware overview. What would you like to know?",
            "toolResults": [],
            "mode": "offline",
        }
    try:
        result = execute_tool(tool_name, {})
    except ToolError as error:
        return {
            "answer": str(error),
            "toolResults": [{"tool": tool_name, "error": str(error)}],
            "mode": "offline",
        }

    if tool_name == "get_memory_summary":
        available = format_bytes(result["availableBytes"])
        answer = f"This system has {format_bytes(result['totalBytes'])} of RAM installed. {available} is currently available."
    elif tool_name == "get_memory_devices":
        if result["devices"]:
            if result["source"] == "Windows CIM memory reports":
                answer = (
                    f"Windows reports {result['reportedDeviceCount']} firmware memory-device "
                    f"entries with {format_bytes(result['installedBytes'])} installed. "
                    "These entries do not reliably show how many physical upgrade slots are free."
                )
                if result["maximumCapacityBytes"] is not None:
                    answer += (
                        f" Firmware reports a maximum capacity of "
                        f"{format_bytes(result['maximumCapacityBytes'])}, but check your PC "
                        "model's service manual to confirm whether the memory is upgradeable."
                    )
                else:
                    answer += " Windows could not verify the motherboard's maximum capacity."
                return {
                    "answer": answer,
                    "toolResults": [{"tool": tool_name, "result": result}],
                    "mode": "offline",
                }
            answer = (
                f"Firmware reports {result['totalSlotsReported']} memory slot(s): "
                f"{result['installedSlots']} occupied and {result['emptySlots']} empty, "
                f"with {format_bytes(result['installedBytes'])} installed."
            )
            if result["maximumCapacityBytes"] is not None:
                answer += f" The reported maximum memory capacity is {format_bytes(result['maximumCapacityBytes'])}."
            elif result.get("capacityUnavailable"):
                answer += f" I could not verify the maximum capacity: {result['capacityUnavailable']}"
            else:
                answer += " Firmware did not report the motherboard's maximum capacity."
        else:
            answer = "Firmware did not report any memory slots, so I can't verify the slot count or upgrade capacity."
    elif tool_name == "get_cpu_info":
        answer = f"Processor: {result['model']}. Logical CPUs: {result['logicalCpus'] or 'unknown'}."
    elif tool_name == "get_storage_devices":
        devices = result["devices"]
        answer = (
            "Storage: " + "; ".join(
                f"{item['name']} ({item['kind']}, {format_bytes(item['sizeBytes'])})"
                for item in devices
            )
            if devices else "No physical disk devices were reported by lsblk."
        )
    elif tool_name == "get_gpu_info":
        adapters = result["adapters"]
        answer = (
            "Graphics: " + "; ".join(
                item.get("name") or " ".join(
                    part for part in (item.get("vendor"), item.get("model")) if part
                )
                for item in adapters
            )
            if adapters else "No display adapters were reported by this system."
        )
    else:
        answer = "Hardware overview collected. See the inspection details below."
    return {"answer": answer, "toolResults": [{"tool": tool_name, "result": result}], "mode": "offline"}


SYSTEM_PROMPT = (
    "You answer questions about the user's Linux or Windows hardware. Hardware facts must come only "
    "from inspection tool results in this conversation. Never guess missing specifications. "
    "Call only the provided read-only tools, with empty JSON arguments. If a tool reports "
    "unavailable or permission denied, say exactly what could not be verified and do not "
    "claim a value. Do not ask for or execute shell commands."
)


def validate_llm_url() -> str:
    parsed = urllib.parse.urlparse(LLM_URL)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ToolError("The configured model URL must be an http URL. HTTPS endpoints are not enabled in this dependency-free demo.")
    host = parsed.hostname.lower()
    if host not in ("127.0.0.1", "localhost", "::1") and os.environ.get("HARDWARE_ASSISTANT_ALLOW_REMOTE_LLM") != "1":
        raise ToolError(
            "The configured model is not on this machine. Set HARDWARE_ASSISTANT_ALLOW_REMOTE_LLM=1 "
            "only if you intentionally want hardware questions and tool results sent to that server."
        )
    return LLM_URL


def call_llm(messages: list[dict]) -> dict:
    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": messages,
        "tools": TOOL_SCHEMAS,
        "tool_choice": "auto",
        "temperature": 0.1,
    }).encode("utf-8")
    request = urllib.request.Request(
        validate_llm_url(),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=LLM_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read(1024 * 1024))
    except urllib.error.HTTPError as error:
        detail = error.read(500).decode("utf-8", errors="replace")
        raise ToolError(f"Local model request failed ({error.code}): {detail}") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise ToolError(f"Could not reach the configured model endpoint: {error}") from error
    try:
        return body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as error:
        raise ToolError("The model endpoint returned an unsupported chat response.") from error


def llm_answer(question: str, history: list[dict]) -> dict:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for item in history[-MAX_HISTORY_MESSAGES:]:
        if isinstance(item, dict) and item.get("role") in ("user", "assistant") and isinstance(item.get("content"), str):
            messages.append({"role": item["role"], "content": item["content"][:2000]})
    messages.append({"role": "user", "content": question})
    results = []
    for _ in range(MAX_TOOL_ROUNDS + 1):
        response = call_llm(messages)
        tool_calls = response.get("tool_calls") or []
        if not tool_calls:
            answer = response.get("content")
            if not isinstance(answer, str) or not answer.strip():
                raise ToolError("The model returned an empty answer.")
            return {"answer": answer.strip(), "toolResults": results, "mode": "local-llm"}
        messages.append({
            "role": "assistant",
            "content": response.get("content"),
            "tool_calls": tool_calls,
        })
        if len(tool_calls) > 4:
            raise ToolError("The model requested too many inspection tools in one step.")
        for call in tool_calls:
            function = call.get("function") or {}
            name = function.get("name")
            try:
                arguments = json.loads(function.get("arguments", "{}"))
                result = execute_tool(name, arguments)
                result_content = {"result": result}
                results.append({"tool": name, "result": result})
            except (ToolError, json.JSONDecodeError) as error:
                result_content = {"error": str(error)}
                results.append({"tool": name, "error": str(error)})
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "content": json.dumps(result_content),
            })
    raise ToolError("The model used the maximum number of tool rounds without answering.")


class Handler(BaseHTTPRequestHandler):
    server_version = "HardwareAssistant/1.0"

    def log_message(self, format_string: str, *args: object) -> None:
        print(f"{self.address_string()} - {format_string % args}")

    def send_json(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/health":
            model_mode = "offline"
            if os.environ.get("HARDWARE_ASSISTANT_USE_LLM") == "1":
                model_host = urllib.parse.urlparse(LLM_URL).hostname or ""
                model_mode = "local-model" if model_host in ("127.0.0.1", "localhost", "::1") else "remote-model"
            self.send_json(200, {
                "ok": True,
                "service": "hardware-assistant",
                "platform": platform.system(),
                "modelMode": model_mode,
            })
            return
        if path == "/api/tools":
            self.send_json(200, {"tools": [
                {"name": name, "description": description}
                for name, description in TOOL_DESCRIPTIONS.items()
            ]})
            return
        if path == "/":
            self.serve_file("index.html")
            return
        if path.startswith("/web/"):
            self.serve_file(path.removeprefix("/web/"))
            return
        self.send_json(404, {"error": "Route not found"})

    def do_POST(self) -> None:
        if self.path != "/api/chat":
            self.send_json(404, {"error": "Route not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                self.send_json(413 if length > MAX_BODY_BYTES else 400, {"error": "Invalid request body size"})
                return
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Body must be a JSON object")
            question = body.get("message")
            history = body.get("history", [])
            if not isinstance(question, str) or not question.strip() or len(question) > 2000:
                self.send_json(400, {"error": "message is required and must be at most 2000 characters"})
                return
            if not isinstance(history, list):
                self.send_json(400, {"error": "history must be an array"})
                return
            if os.environ.get("HARDWARE_ASSISTANT_USE_LLM") == "1":
                result = llm_answer(question.strip(), history)
            else:
                result = offline_answer(question.strip())
            self.send_json(200, result)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            self.send_json(400, {"error": str(error) or "Body must be valid JSON"})
        except ToolError as error:
            self.send_json(503, {"error": str(error)})
        except Exception as error:
            print(f"Request failed: {error}")
            self.send_json(500, {"error": "Internal server error"})

    def serve_file(self, relative_path: str) -> None:
        requested = (WEB_ROOT / relative_path).resolve()
        if not requested.is_relative_to(WEB_ROOT.resolve()) or not requested.is_file():
            self.send_json(404, {"error": "File not found"})
            return
        content = requested.read_bytes()
        content_type = "text/html; charset=utf-8" if requested.suffix == ".html" else (
            "text/javascript; charset=utf-8" if requested.suffix == ".js" else "text/css; charset=utf-8"
        )
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def main() -> None:
    host = os.environ.get("HARDWARE_ASSISTANT_HOST", "127.0.0.1")
    port = int(os.environ.get("HARDWARE_ASSISTANT_PORT", "8001"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Hardware assistant running at http://{host}:{port}")
    print("Default mode is local/offline. Hardware collectors support Linux and Windows.")
    if os.environ.get("HARDWARE_ASSISTANT_USE_LLM") == "1":
        print(f"Tool-calling model endpoint: {LLM_URL} (model: {LLM_MODEL})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping hardware assistant.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
