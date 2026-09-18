import tempfile
import unittest
from pathlib import Path

from agent_core import (
    JsonlSessionStore,
    PlanManager,
    PlanStep,
    Session,
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


if __name__ == "__main__":
    unittest.main()
