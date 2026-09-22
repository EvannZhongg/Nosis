import io
import json
import unittest
from unittest.mock import patch

from interfaces.bridge import __main__ as bridge_main


class BridgeMainTest(unittest.TestCase):
    def test_reports_bridge_construction_failure_over_protocol(self) -> None:
        protocol = io.StringIO()

        with (
            patch.object(bridge_main, "_claim_stdout", return_value=protocol),
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
