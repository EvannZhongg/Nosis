import json
import tempfile
import unittest
from pathlib import Path

from agent_core import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    MemoryCandidate,
    MemoryDocument,
    MemoryManager,
    MemoryReconciler,
    MemoryStore,
    RememberTool,
    Session,
    ToolExecutionContext,
    Workspace,
)


class ReconcilerProvider(LLMProvider):
    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.requests: list[LLMRequest] = []

    @property
    def max_context_tokens(self) -> int:
        return 10000

    def count_input_tokens(self, request: LLMRequest) -> int:
        return len(str(request.messages[0].content).split())

    def stream(self, request, on_text_delta, on_reasoning_delta=None):
        self.requests.append(request)
        content = next(self.responses)
        on_text_delta(content)
        return LLMResponse(content=content)


class MemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.store = MemoryStore(
            self.root / "MEMORY.md",
            self.root / "workspaces" / "MEMORY.md",
        )
        self.store.initialize()

    def manager(self, provider: LLMProvider) -> MemoryManager:
        return MemoryManager(
            self.workspace,
            self.store,
            MemoryReconciler(
                provider,
                global_prompt="global prompt",
                workspace_prompt="workspace prompt",
                global_max_tokens=2000,
                workspace_max_tokens=3000,
            ),
            self.store.load(self.workspace),
        )

    def test_remember_only_submits_a_candidate(self) -> None:
        provider = ReconcilerProvider([])
        manager = self.manager(provider)
        context = ToolExecutionContext(
            workspace=Workspace(self.workspace),
            session=Session(),
            memory=manager,
        )

        result = RememberTool().execute(
            {
                "kind": "preference",
                "scope": "global",
                "content": "Prefer concise answers.",
            },
            context,
        )

        self.assertEqual(result, {
            "accepted": True,
            "scope": "global",
            "kind": "preference",
        })
        self.assertEqual(
            manager.pending,
            (MemoryCandidate("preference", "global", "Prefer concise answers."),),
        )
        self.assertNotIn(
            "Prefer concise answers",
            (self.root / "MEMORY.md").read_text(encoding="utf-8"),
        )

    def test_batches_candidates_and_keeps_scopes_isolated(self) -> None:
        provider = ReconcilerProvider([
            json.dumps({
                "entries": [
                    {"kind": "preference", "content": "Prefer concise answers."},
                    {"kind": "fact", "content": "The user works in Shanghai."},
                ]
            }),
            json.dumps({
                "entries": [
                    {"kind": "decision", "content": "Use Bridge as the only runtime channel."}
                ]
            }),
        ])
        manager = self.manager(provider)
        manager.remember(MemoryCandidate("preference", "global", "Be concise."))
        manager.remember(MemoryCandidate("fact", "global", "Lives in Shanghai."))
        manager.remember(MemoryCandidate("decision", "workspace", "Use Bridge."))

        self.assertTrue(manager.reconcile_pending())

        self.assertEqual(len(provider.requests), 2)
        global_payload = json.loads(provider.requests[0].messages[0].content)
        workspace_payload = json.loads(provider.requests[1].messages[0].content)
        self.assertEqual(global_payload["scope"], "global")
        self.assertEqual(len(global_payload["candidates"]), 2)
        self.assertEqual(workspace_payload["scope"], "workspace")
        self.assertNotIn("max_tokens", global_payload)
        self.assertIsNone(provider.requests[0].max_generation_tokens)
        self.assertIsNone(provider.requests[1].max_generation_tokens)
        context = self.store.load(self.workspace)
        self.assertEqual(
            context.global_memory.preferences,
            ("Prefer concise answers.",),
        )
        self.assertEqual(
            context.workspace_memory.decisions,
            ("Use Bridge as the only runtime channel.",),
        )
        other = self.store.load(self.root / "other")
        self.assertTrue(other.workspace_memory.is_empty())

    def test_token_limits_are_only_rendered_as_prompt_guidance(self) -> None:
        provider = ReconcilerProvider([
            json.dumps({
                "entries": [
                    {"kind": "fact", "content": "A durable fact."},
                ]
            })
        ])
        reconciler = MemoryReconciler(
            provider,
            global_prompt="Keep this around {{global_max_tokens}} tokens.",
            workspace_prompt=(
                "Keep this around {{workspace_max_tokens}} tokens."
            ),
            global_max_tokens=12,
            workspace_max_tokens=34,
        )

        reconciler.reconcile(
            "global",
            MemoryDocument(),
            (MemoryCandidate("fact", "global", "Remember this."),),
        )

        self.assertIn("around 12 tokens", provider.requests[0].system_prompt)
        self.assertIsNone(provider.requests[0].max_generation_tokens)

    def test_store_round_trips_stable_markdown(self) -> None:
        self.store.write_updates(
            self.workspace,
            global_memory=MemoryDocument(facts=("A stable fact.",)),
            workspace_memory=MemoryDocument(decisions=("A project decision.",)),
        )

        loaded = self.store.load(self.workspace)

        self.assertEqual(loaded.global_memory.facts, ("A stable fact.",))
        self.assertEqual(
            loaded.workspace_memory.decisions,
            ("A project decision.",),
        )
        self.assertIn("# Nosis Global Memory", self.store.global_path.read_text())
        self.assertIn(
            f"## Workspace: {json.dumps(str(self.workspace.resolve()))}",
            self.store.workspace_path.read_text(),
        )

    def test_prompt_states_memory_priority(self) -> None:
        self.store.write_updates(
            self.workspace,
            global_memory=MemoryDocument(preferences=("Use Chinese.",)),
        )

        section = self.store.load(self.workspace).prompt_section()

        self.assertIn("current request has highest priority", section)
        self.assertIn("workspace instructions and project rules come next", section)
        self.assertIn("Use Chinese.", section)

    def test_empty_memory_still_states_priority_when_enabled(self) -> None:
        section = self.store.load(self.workspace).prompt_section()

        self.assertIn("current request has highest priority", section)
        self.assertIn("No long-term memory is currently recorded", section)

    def test_rejects_a_stale_write_without_losing_new_memory(self) -> None:
        stale = self.store.load(self.workspace)
        self.store.write_updates(
            self.workspace,
            global_memory=MemoryDocument(facts=("Newer fact.",)),
        )

        with self.assertRaisesRegex(RuntimeError, "changed during reconciliation"):
            self.store.write_updates(
                self.workspace,
                global_memory=MemoryDocument(facts=("Stale fact.",)),
                expected=stale,
            )

        self.assertEqual(
            self.store.load(self.workspace).global_memory.facts,
            ("Newer fact.",),
        )


if __name__ == "__main__":
    unittest.main()
