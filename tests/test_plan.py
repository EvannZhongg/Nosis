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
    ToolCall,
    ToolConfig,
    ToolExecutionContext,
    Workspace,
    builtin_catalog,
)
from agent_core.plan import MAX_PLAN_STEP_OUTCOME_CHARS
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


class PlanToolTest(unittest.TestCase):
    def test_updates_the_session_plan_and_increments_revision(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            session = Session("session-1")
            manager = PlanManager(session)
            context = ToolExecutionContext(
                workspace=Workspace(Path(root)),
                session=session,
                plan=manager,
            )
            tools = builtin_catalog().select(("update_plan",), context)

            first = tools.execute(ToolCall("call-1", "update_plan", {
                "goal": "Add plans",
                "steps": [
                    {"id": "inspect", "title": "Inspect", "status": "completed"},
                    {"id": "runtime", "title": "Build runtime", "status": "in_progress"},
                ],
            }))
            second = tools.execute(ToolCall("call-2", "update_plan", {
                "goal": "Add plans",
                "steps": [
                    {"id": "inspect", "title": "Inspect", "status": "completed"},
                    {
                        "id": "runtime",
                        "title": "Build runtime",
                        "status": "completed",
                        "outcome": "Plan state is persisted in the session journal.",
                    },
                ],
            }))

            self.assertIsNone(first.error)
            self.assertIsNone(second.error)
            self.assertEqual(first.output["revision"], 1)
            self.assertEqual(second.output["revision"], 2)
            self.assertEqual(first.output["plan_id"], second.output["plan_id"])
            self.assertEqual(session.plan.revision, 2)
            self.assertEqual(
                session.plan.steps[1].outcome,
                "Plan state is persisted in the session journal.",
            )
            self.assertEqual(session.journal[-1].event_type, "plan_updated")
            self.assertIsNone(session.journal[-1].turn_id)

    def test_rejects_duplicate_ids(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            session = Session("session-1")
            context = ToolExecutionContext(
                workspace=Workspace(Path(root)),
                session=session,
                plan=PlanManager(session),
            )
            tools = builtin_catalog().select(("update_plan",), context)
            result = tools.execute(ToolCall("call-1", "update_plan", {
                "goal": "Invalid",
                "steps": [
                    {"id": "same", "title": "One", "status": "in_progress"},
                    {"id": "same", "title": "Two", "status": "pending"},
                ],
            }))

            self.assertIsNotNone(result.error)
            self.assertIn("duplicate plan step id", result.error.message)
            self.assertIsNone(session.plan)

    def test_rejects_multiple_active_steps(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            session = Session("session-1")
            context = ToolExecutionContext(
                workspace=Workspace(Path(root)),
                session=session,
                plan=PlanManager(session),
            )
            tools = builtin_catalog().select(("update_plan",), context)
            result = tools.execute(ToolCall("call-1", "update_plan", {
                "goal": "Invalid",
                "steps": [
                    {"id": "one", "title": "One", "status": "in_progress"},
                    {"id": "two", "title": "Two", "status": "in_progress"},
                ],
            }))

            self.assertIsNotNone(result.error)
            self.assertIn("at most one in_progress", result.error.message)

    def test_rejects_an_outcome_over_the_length_limit(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            session = Session("session-1")
            context = ToolExecutionContext(
                workspace=Workspace(Path(root)),
                session=session,
                plan=PlanManager(session),
            )
            tools = builtin_catalog().select(("update_plan",), context)
            result = tools.execute(ToolCall("call-1", "update_plan", {
                "goal": "Invalid",
                "steps": [{
                    "id": "one",
                    "title": "One",
                    "status": "completed",
                    "outcome": "x" * (MAX_PLAN_STEP_OUTCOME_CHARS + 1),
                }],
            }))

            self.assertIsNotNone(result.error)
            self.assertIn("exceeds maximum length", result.error.message)

    def test_rejects_an_outcome_for_an_unfinished_step(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            session = Session("session-1")
            context = ToolExecutionContext(
                workspace=Workspace(Path(root)),
                session=session,
                plan=PlanManager(session),
            )
            tools = builtin_catalog().select(("update_plan",), context)
            result = tools.execute(ToolCall("call-1", "update_plan", {
                "goal": "Invalid",
                "steps": [{
                    "id": "one",
                    "title": "One",
                    "status": "in_progress",
                    "outcome": "Still working.",
                }],
            }))

            self.assertIsNotNone(result.error)
            self.assertIn("completed or blocked", result.error.message)

    def test_is_unavailable_without_a_plan_manager(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            context = ToolExecutionContext(
                workspace=Workspace(Path(root)),
                session=Session("session-1"),
            )
            tools = builtin_catalog().select(("update_plan",), context)
            self.assertEqual(tools.definitions, ())


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
