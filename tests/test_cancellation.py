import unittest

from agent_core.jobs import CancellationToken
from agent_core.turn_control import TurnControl


class CancellationNotificationsTest(unittest.TestCase):
    def test_cancel_notifies_all_subscribers_once_and_supports_unsubscribe(self):
        token = CancellationToken()
        calls = []
        token.subscribe(lambda: calls.append("first"))
        token.subscribe(lambda: calls.append("second"))
        unsubscribe = token.subscribe(lambda: calls.append("removed"))
        unsubscribe()
        token.cancel()
        token.cancel()
        self.assertCountEqual(calls, ["first", "second"])
        token.subscribe(lambda: calls.append("late"))()
        self.assertEqual(calls[-1], "late")

    def test_nested_turn_forwards_parent_cancellation_and_unsubscribes(self):
        parent = CancellationToken()
        child = TurnControl(TurnControl(parent))
        calls = []
        unsubscribe = child.subscribe(lambda: calls.append("removed"))
        unsubscribe()
        child.subscribe(lambda: calls.append("cancelled"))
        parent.cancel()
        self.assertTrue(child.cancelled)
        self.assertEqual(calls, ["cancelled"])

    def test_callback_can_read_turn_state_without_deadlocking(self):
        control = TurnControl()
        calls = []
        control.subscribe(lambda: calls.append(control.cancelled))
        self.assertTrue(control.cancel())
        self.assertEqual(calls, [True])
