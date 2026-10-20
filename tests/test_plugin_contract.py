"""Keep the portable authoring tools aligned with Runtime's package contract."""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_core import SkillLoader
from agent_core.tools import ROLE_TOOL_NAMES
from agent_runtime.plugins import PluginLoader, PluginManager
from tests.test_plugins import write_agent, write_manifest, write_skill


SCRIPTS = (Path(__file__).resolve().parents[1] / "agent_runtime" / "defaults"
           / "skills" / "plugin-creator" / "scripts")
spec = importlib.util.spec_from_file_location("plugin_validator", SCRIPTS / "validate_plugin.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


class PluginContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_role_names_and_dynamic_references_match(self) -> None:
        self.assertEqual(validator.ROLE_TOOL_NAMES, ROLE_TOOL_NAMES)
        manifest = write_manifest(self.root, "review-kit", components={"agents": ["agents/reviewer.md"]})
        for tools, valid in (
            (None, True), ([], True), (["shell", "read_file"], True),
            (["read_file", "mcp__review-kit_analyzer__analyze_code"], True),
            (["mcp__other_server__run"], True),
            (["subagent"], False), (["job_control"], False),
            (["read_skill"], False), (["generate_image"], False),
            (["mcp__server__"], False), (["mcp__bad.name__run"], False),
            (["read_file", " read_file "], False), ([1], False),
            ("read_file", False),
        ):
            with self.subTest(tools=tools):
                fields = "" if tools is None else f"tools: {json.dumps(tools)}\n"
                write_agent(manifest.parent, fields=fields)
                errors, _ = validator.validate_plugin(manifest.parent)
                agents, warnings = PluginManager.discover(self.root).load_agents()
                self.assertEqual(not errors, valid, errors)
                self.assertEqual(bool(agents), valid, warnings)

    def test_manifest_types_paths_and_optional_nulls_match(self) -> None:
        manifest = write_manifest(self.root, "test")
        for fields, valid in (
            ({}, True), ({"version": None, "description": None}, True),
            ({"name": "Mixed.Name_1"}, True), ({"name": "bad:name"}, False),
            ({"extra": "value"}, False), ({"enabled": 1}, False),
            ({"capabilities": None}, False), ({"components": None}, False),
            ({"components": {"mcp": None}}, False),
            ({"components": {"extra": []}}, False),
            ({"components": {"skills": ["../escape"]}}, False),
            ({"components": {"skills": [str(self.root)]}}, False),
            ({"components": {"mcp": ["a", " a "]}}, False),
        ):
            with self.subTest(fields=fields):
                manifest.write_text(json.dumps({"name": "test", **fields}), encoding="utf-8")
                try:
                    PluginLoader().load(manifest)
                    accepted = True
                except ValueError:
                    accepted = False
                errors, _ = validator.validate_plugin(manifest.parent)
                self.assertEqual(accepted, valid)
                self.assertEqual(not errors, valid, errors)

    def test_mcp_configuration_contract_matches(self) -> None:
        manifest = write_manifest(self.root, "test", components={"mcp": [".mcp.json"]})
        cases = (
            ({"command": "python"}, True),
            ({"command": "python", "type": "stdio", "args": ["", "server.py"], "cwd": None}, True),
            ({"url": "http://localhost/mcp", "type": "http", "headers": {"X-Key": "${KEY}"}}, True),
            ({"url": "http://localhost/mcp", "transport": "streamable_http"}, True),
            ({"command": "python", "type": None}, True),
            ({"command": "python", "tools": {"enabled": ["one"], "approval": {"always": ["one"]}}}, True),
            ({"command": "python", "tools": {"enabled": [], "approval": "never"}}, True),
            ({"command": "python", "enabled": False}, True),
            ({"command": "python", "startup_timeout_seconds": True}, False),
            ({"command": "python", "call_timeout_seconds": 0}, False),
            ({"command": "python", "enabled": "false"}, False),
            ({"command": "python", "transport": None}, False),
            ({"url": "http://localhost/mcp", "type": "streamable_http"}, False),
            ({"command": "python", "type": "stdio", "transport": "stdio"}, False),
            ({"command": "python", "url": "http://localhost/mcp"}, False),
            ({"command": "python", "env": {"X": 1}}, False),
            ({"command": "python", "env": {"X": "${}"}}, False),
            ({"command": "python", "args": [1]}, False),
            ({"command": "python", "scope": "workspace"}, False),
            ({"command": "python", "tools": {"enabled": ["one", "one"]}}, False),
            ({"command": "python", "tools": {"enabled": None}}, False),
            ({"command": "python", "tools": {"approval": {}}}, False),
            ({"command": "python", "tools": {"approval": True}}, False),
            ({"command": "python", "tools": {"enabled": [], "approval": {"always": ["one"]}}}, False),
            ({"command": "python", "tools": {"unknown": True}}, False),
            ({"command": "python", "unknown": True}, False),
        )
        from unittest.mock import patch
        with patch.dict("os.environ", {"KEY": "test-key"}):
            for server, valid in cases:
                with self.subTest(server=server):
                    (manifest.parent / ".mcp.json").write_text(json.dumps({"analyzer": server}), encoding="utf-8")
                    errors, _ = validator.validate_plugin(manifest.parent)
                    servers, warnings = PluginManager.discover(self.root).load_mcp_servers()
                    self.assertEqual(not errors, valid, errors)
                    self.assertEqual(bool(servers), valid, warnings)

    def test_duplicate_component_identities_are_diagnosed(self) -> None:
        manifest = write_manifest(self.root, "test", components={
            "mcp": ["first.json", "second.json"],
            "agents": ["agents/reviewer.md", "agents/copy.md"],
        })
        source = write_agent(manifest.parent)
        source.with_name("copy.md").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        for filename in ("first.json", "second.json"):
            (manifest.parent / filename).write_text(json.dumps({"analyzer": {"command": "python"}}), encoding="utf-8")
        manager = PluginManager.discover(self.root)
        servers, warnings = manager.load_mcp_servers()
        agents, agent_warnings = manager.load_agents()
        errors, _ = validator.validate_plugin(manifest.parent)
        self.assertEqual(len(servers), 1)
        self.assertEqual(len(agents), 1)
        self.assertIn("first.json", warnings[0])
        self.assertIn("second.json", warnings[0])
        self.assertIn("duplicate Agent", agent_warnings[0])
        self.assertTrue(any("duplicate mcp" in error for error in errors))
        self.assertTrue(any("duplicate agents" in error for error in errors))

    def test_hidden_skills_and_null_agent_model_match(self) -> None:
        manifest = write_manifest(self.root, "test", version=None, components={
            "skills": ["skills"], "agents": ["agents/reviewer.md"],
        })
        write_agent(manifest.parent, fields="model: null\ncolor: green\n")
        write_skill(manifest.parent)
        path = manifest.parent / "skills" / "shared" / "SKILL.md"
        path.write_text("---\nname: hidden\ndescription: Hidden.\nmetadata:\n  nosis:\n    register: false\n---\n", encoding="utf-8")
        errors, entries = validator.validate_plugin(manifest.parent)
        self.assertEqual(errors, [])
        self.assertEqual(entries, ["test:reviewer"])
        self.assertEqual(SkillLoader().load(PluginManager.discover(self.root).skill_sources()).names, ())

    def test_scaffold_and_validator_run_without_nosis_imports(self) -> None:
        created = subprocess.run([
            sys.executable, "-I", str(SCRIPTS / "create_basic_plugin.py"),
            "test-kit", "--path", str(self.root), "--with-skills", "--with-agents", "--with-mcp",
        ], capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(created.returncode, 0, created.stderr)
        validated = subprocess.run([
            sys.executable, "-I", str(SCRIPTS / "validate_plugin.py"), str(self.root / "test-kit"),
        ], capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
        self.assertIn("Static validation only", validated.stdout)
        self.assertEqual(PluginManager.discover(self.root).load_agents()[1], ())
