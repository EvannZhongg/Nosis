import io
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from interfaces.bridge import __main__ as bridge_main


class BridgeMainTest(unittest.TestCase):
    def test_reports_first_use_initialization_failure_over_protocol(self) -> None:
        protocol = io.StringIO()
        with (
            patch.object(bridge_main, "_claim_stdout", return_value=protocol),
            patch("agent_runtime.config.initialize_config_directory", side_effect=PermissionError("config directory")),
            self.assertRaises(SystemExit) as failure,
        ):
            bridge_main.main()
        self.assertEqual(failure.exception.code, 1)
        message = json.loads(protocol.getvalue())
        self.assertEqual(message["type"], "fatal")
        self.assertEqual(message["event_sequence"], 1)
        self.assertEqual(message["error"]["type"], "PermissionError")

    def test_control_plane_import_does_not_load_provider_or_mcp_sdks(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", (
                "import sys, os; import interfaces.bridge.bridge; "
                "assert not {'litellm', 'openai', 'mcp'} & sys.modules.keys(); "
                "assert os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] == 'True'"
            )],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_reports_bridge_construction_failure_over_protocol(self) -> None:
        protocol = io.StringIO()

        with (
            patch.object(bridge_main, "_claim_stdout", return_value=protocol),
            patch("agent_runtime.config.initialize_config_directory"),
            patch(
                "interfaces.bridge.bridge.Bridge",
                side_effect=KeyError("execution_scope"),
            ),
            self.assertRaises(SystemExit),
        ):
            bridge_main.main()

        message = json.loads(protocol.getvalue())
        self.assertEqual(message["type"], "fatal")
        self.assertEqual(message["error"]["type"], "KeyError")
        self.assertIn("execution_scope", message["error"]["message"])


if __name__ == "__main__":
    unittest.main()
