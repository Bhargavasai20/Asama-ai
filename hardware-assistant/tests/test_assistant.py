import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


ASSISTANT_PATH = Path(__file__).resolve().parents[1] / "assistant.py"
SPEC = importlib.util.spec_from_file_location("hardware_assistant", ASSISTANT_PATH)
assistant = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(assistant)


class ParserTests(unittest.TestCase):
    def test_parse_memory_report(self):
        result = assistant.parse_meminfo("MemTotal:       16384000 kB\nMemAvailable:    8192000 kB\n")
        self.assertEqual(result["totalBytes"], 16384000 * 1024)
        self.assertEqual(result["availableBytes"], 8192000 * 1024)

    def test_memory_report_without_total_is_rejected(self):
        with self.assertRaises(assistant.ToolError):
            assistant.parse_meminfo("MemFree: 100 kB\n")

    def test_parse_cpu_model_and_logical_count(self):
        result = assistant.parse_cpuinfo("processor : 0\nmodel name : Test CPU\n", 8)
        self.assertEqual(result["model"], "Test CPU")
        self.assertEqual(result["logicalCpus"], 8)

    def test_nvme_device_is_not_mislabeled_as_generic_ssd(self):
        result = assistant.parse_lsblk({
            "blockdevices": [
                {"name": "nvme0n1", "type": "disk", "size": "512000000000",
                 "rota": "0", "tran": "nvme", "model": "Example", "mountpoints": ["/"]}
            ]
        })
        self.assertEqual(result[0]["kind"], "NVMe SSD")

    def test_parse_graphics_adapter(self):
        result = assistant.parse_lspci(
            '01:00.0 "VGA compatible controller" "Example Vendor" "Example GPU"\n'
            '00:1f.0 "Audio device" "Other" "Audio"\n'
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["model"], "Example GPU")

    def test_parse_memory_slots_and_empty_slot(self):
        result = assistant.parse_dmidecode(
            "Memory Device\n\tSize: 16 GB\n\tLocator: DIMM 0\n"
            "Memory Device\n\tSize: No Module Installed\n\tLocator: DIMM 1\n",
            "Physical Memory Array\n\tMaximum Capacity: 64 GB\n",
        )
        self.assertEqual(result["installedSlots"], 1)
        self.assertEqual(result["emptySlots"], 1)
        self.assertEqual(result["totalSlotsReported"], 2)
        self.assertEqual(result["installedBytes"], 16 * 1024**3)
        self.assertEqual(result["maximumCapacityBytes"], 64 * 1024**3)

    def test_parse_windows_nvme_disk(self):
        result = assistant.parse_windows_disks({
            "name": "0",
            "friendlyName": "Example drive",
            "size": "512000000000",
            "mediaType": "SSD",
            "busType": "NVMe",
        })
        self.assertEqual(result[0]["kind"], "NVMe SSD")
        self.assertEqual(result[0]["sizeBytes"], 512000000000)

    def test_parse_windows_memory_slots_and_max_capacity(self):
        result = assistant.parse_windows_memory_devices({
            "devices": [
                {"capacity": "17179869184", "locator": "DIMM 0"},
                {"capacity": "0", "locator": "DIMM 1"},
            ],
            "arrays": [{"memoryDevices": 2, "maxCapacity": "68719476736"}],
        })
        self.assertIsNone(result["installedSlots"])
        self.assertIsNone(result["emptySlots"])
        self.assertEqual(result["reportedDeviceCount"], 2)
        self.assertEqual(result["maximumCapacityBytes"], 64 * 1024**3)


class SafetyTests(unittest.TestCase):
    def test_tool_allowlist_rejects_unknown_commands(self):
        with self.assertRaises(assistant.ToolError):
            assistant.execute_tool("run_shell_command", {"command": "whoami"})

    def test_tools_reject_arguments(self):
        with self.assertRaises(assistant.ToolError):
            assistant.execute_tool("get_memory_summary", {"path": "/etc/passwd"})

    def test_question_router_selects_specific_read_only_tool(self):
        self.assertEqual(assistant.select_offline_tool("Is my SSD NVMe?"), "get_storage_devices")
        self.assertEqual(assistant.select_offline_tool("What GPU do I have?"), "get_gpu_info")
        self.assertIsNone(assistant.select_offline_tool("Tell me a story"))

    def test_windows_memory_collector_uses_fixed_read_only_query(self):
        with patch.object(assistant.platform, "system", return_value="Windows"), patch.object(
            assistant, "run_powershell_json", return_value={
                "totalBytes": 32 * 1024**3,
                "availableBytes": 12 * 1024**3,
                "source": "Win32_ComputerSystem and Win32_OperatingSystem",
            },
        ) as powershell:
            result = assistant.get_memory_summary()
        self.assertEqual(result["totalBytes"], 32 * 1024**3)
        self.assertEqual(result["availableBytes"], 12 * 1024**3)
        self.assertIn("Get-CimInstance", powershell.call_args.args[0])

    def test_windows_gpu_answer_uses_reported_name(self):
        with patch.object(assistant, "execute_tool", return_value={
            "adapters": [{"name": "Example Graphics"}],
            "source": "Win32_VideoController",
        }):
            answer = assistant.offline_answer("What GPU do I have?")
        self.assertIn("Example Graphics", answer["answer"])


if __name__ == "__main__":
    unittest.main()
