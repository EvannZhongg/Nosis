import unittest
from unittest.mock import Mock

from agent_core.loop_policy import (
    ToolCallLimitExceededError, ToolCallRepetitionGuard, TurnContinuationPolicy,
)
from agent_core.tools import ToolCall
from agent_core.turn_control import AgentCancelled, TurnControl


class LoopPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.control = TurnControl()
        self.guard = ToolCallRepetitionGuard(1)
        self.call = ToolCall("one", "echo", {"text": "same"})
        self.append = Mock()
        self.jobs = Mock(return_value=(False, False))
        self.policy = TurnContinuationPolicy(
            self.control, self.guard, self.jobs, self.append,
        )

    def test_argument_order_and_call_ids_do_not_reset_repetition(self) -> None:
        guard = ToolCallRepetitionGuard(1)
        guard.check((ToolCall("a", "echo", {"a": 1, "b": 2}),))
        with self.assertRaises(ToolCallLimitExceededError):
            guard.check((ToolCall("b", "echo", {"b": 2, "a": 1}),))

    def test_rejected_batch_does_not_commit_a_partial_count(self) -> None:
        with self.assertRaises(ToolCallLimitExceededError):
            self.guard.check((self.call, self.call))
        self.guard.check((self.call,))

    def test_job_result_resets_repetition_and_keeps_turn_open(self) -> None:
        self.guard.check((self.call,))
        self.jobs.return_value = (True, False)
        self.assertTrue(self.policy.after_response(Mock()))
        self.guard.check((self.call,))
        self.assertTrue(self.control.steer("next", "continue"))

    def test_job_notifications_without_results_do_not_trigger_model_calls(self) -> None:
        self.jobs.side_effect = [(False, True), (False, True), (True, False)]
        wait = Mock(return_value=True)
        self.assertTrue(self.policy.after_response(wait))
        self.assertEqual(wait.call_count, 2)

    def test_steering_interrupts_job_wait_and_resets_repetition(self) -> None:
        self.guard.check((self.call,))
        self.jobs.return_value = (False, True)

        def wait():
            self.control.steer("steer", "new direction")
            return False

        self.assertTrue(self.policy.after_response(wait))
        self.assertEqual(self.append.call_args.args[0][0].text, "new direction")
        self.guard.check((self.call,))

    def test_cancel_during_job_wait_prevents_completion(self) -> None:
        self.jobs.return_value = (False, True)
        with self.assertRaises(AgentCancelled):
            self.policy.after_response(self.control.cancel)
        self.append.assert_not_called()

    def test_steering_at_completion_is_consumed_before_closing(self) -> None:
        self.guard.check((self.call,))
        self.control.steer("steer", "one more thing")
        self.assertTrue(self.policy.after_response(Mock()))
        self.guard.check((self.call,))
        self.assertFalse(self.policy.after_response(Mock()))
        self.assertFalse(self.control.steer("late", "too late"))

    def test_external_cancel_at_finish_does_not_return_success(self) -> None:
        cancelled = False
        control = TurnControl(lambda: cancelled)
        original_finish = control.finish

        def finish():
            nonlocal cancelled
            cancelled = True
            return original_finish()

        control.finish = finish
        policy = TurnContinuationPolicy(control, self.guard, self.jobs, self.append)
        with self.assertRaises(AgentCancelled):
            policy.after_response(Mock())
