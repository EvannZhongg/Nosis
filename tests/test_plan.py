import tempfile
import unittest
from pathlib import Path

from agent_core import (
    AgentConfig,
    ContextManager,
    JsonlSessionStore,
    PlanManager,
    PlanStep,
    Session,
    ToolConfig,
)
from agent_core.config import ContextCompressionConfig
from agent_core.llm import LLMProvider, LLMRequest, LLMResponse, ProviderCapabilities


class _Provider(LLMProvider):
    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(frozenset({"text"}))

    @property
    def max_context_tokens(self) -> int:
        return 1000

    def count_input_tokens(self, request: LLMRequest) -> int:
        return 1

    def stream(self, request, on_delta, on_reasoning_delta=None) -> LLMResponse:
        raise AssertionError("not used")


def _config() -> AgentConfig:
    return AgentConfig(
        max_same_tool_calls=5,
        output_reserve_tokens=100,
        max_generation_tokens=None,
        tools=ToolConfig(()),
        context=ContextCompressionConfig(
            enabled=False,
            trigger_ratio=None,
            keep_recent_units=6,
        ),
    )


class PlanPersistenceTest(unittest.TestCase):
    def test_restores_the_latest_plan_independently_of_turns(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root) / "workspace"
            workspace.mkdir()
            store = JsonlSessionStore(Path(root) / "sessions")
            session = Session("session-1")
            store.bind_workspace(session.session_id, workspace)
            session.attach_journal_sink(
                lambda events: store.append_events(
                    session.session_id, events, workspace=workspace
                )
            )
            manager = PlanManager(session)
            manager.update(
                "Persistent plan",
                (
                    PlanStep("inspect", "Inspect", "completed"),
                    PlanStep(
                        "verify",
                        "Verify",
                        "blocked",
                        "Unit tests are ready.",
                    ),
                ),
            )

            loaded = store.load(session.session_id, recover=False)

            self.assertEqual(loaded.plan, session.plan)
            self.assertEqual(loaded.plan.goal, "Persistent plan")
            self.assertEqual(loaded.plan.steps[1].status, "blocked")
            self.assertEqual(loaded.plan.steps[1].outcome, "Unit tests are ready.")

            self.assertEqual(store.list_workspace_sessions(workspace), [])

    def test_current_plan_is_injected_into_every_model_request(self) -> None:
        session = Session("session-1")
        PlanManager(session).update(
            "Persistent plan",
            (PlanStep("verify", "Verify recovery", "in_progress"),),
        )
        manager = ContextManager(_Provider(), session, "System", _config())

        prompt = manager.build_request().system_prompt

        self.assertIn("[Current Plan]", prompt)
        self.assertIn('"goal": "Persistent plan"', prompt)
        self.assertIn('"status": "in_progress"', prompt)

    def test_completed_plan_is_not_injected_into_the_model_request(self) -> None:
        session = Session("session-1")
        PlanManager(session).update(
            "Finished plan",
            (PlanStep("verify", "Verify recovery", "completed", "Passed."),),
        )
        manager = ContextManager(_Provider(), session, "System", _config())

        prompt = manager.build_request().system_prompt

        self.assertEqual(prompt, "System")
        self.assertEqual(session.plan.steps[0].outcome, "Passed.")


if __name__ == "__main__":
    unittest.main()
