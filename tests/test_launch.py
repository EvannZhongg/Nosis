import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from interfaces import launch


class LaunchTest(unittest.TestCase):
    def test_temporary_creates_a_managed_workspace_for_the_tui(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config"
            config.mkdir()
            (config / "agent_config.json").write_text(
                json.dumps({"scratch_workspace_root": str(root / "scratch")}),
                encoding="utf-8",
            )
            bundle = root / "app.js"
            bundle.touch()

            with (
                patch.object(launch, "default_config_directory", return_value=config),
                patch.object(launch, "initialize_config_directory"),
                patch.object(launch, "ui_bundle_path", return_value=bundle),
                patch.object(launch.shutil, "which", return_value="node"),
                patch.object(launch.subprocess, "call", return_value=0) as call,
                patch.object(launch.sys, "argv", ["nosis", "--temporary"]),
                self.assertRaises(SystemExit) as exit_error,
            ):
                launch.main()

            self.assertEqual(exit_error.exception.code, 0)
            arguments = call.call_args.args[0]
            self.assertEqual(arguments[:2], ["node", str(bundle)])
            self.assertEqual(arguments[2], "--workspace")
            workspace = Path(arguments[3])
            self.assertEqual(workspace.parent, (root / "scratch").resolve())
            self.assertTrue(workspace.is_dir())

    def test_temporary_rejects_an_explicit_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config"
            config.mkdir()
            bundle = root / "app.js"
            bundle.touch()

            with (
                patch.object(launch, "default_config_directory", return_value=config),
                patch.object(launch, "initialize_config_directory"),
                patch.object(launch, "ui_bundle_path", return_value=bundle),
                patch.object(launch.shutil, "which", return_value="node"),
                patch.object(
                    launch.sys,
                    "argv",
                    ["nosis", "--temporary", "--workspace", str(root)],
                ),
                self.assertRaisesRegex(SystemExit, "cannot be used"),
            ):
                launch.main()


if __name__ == "__main__":
    unittest.main()
