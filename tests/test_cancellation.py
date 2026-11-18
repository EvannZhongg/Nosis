import unittest
from threading import Barrier, Thread

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

    def test_subscribing_after_both_sources_cancelled_notifies_once(self):
        parent = CancellationToken()
        child = TurnControl(parent)
        parent.cancel()
        child.cancel()
        calls = []
        child.subscribe(lambda: calls.append("cancelled"))()
        self.assertEqual(calls, ["cancelled"])

    def test_both_cancellation_orders_notify_each_subscription_once(self):
        for external_first in (False, True):
            with self.subTest(external_first=external_first):
                parent = CancellationToken()
                child = TurnControl(parent)
                calls = []
                callback = lambda: calls.append("cancelled")
                child.subscribe(callback)
                child.subscribe(callback)
                cancel_first, cancel_second = (
                    (parent.cancel, child.cancel) if external_first
                    else (child.cancel, parent.cancel)
                )
                cancel_first()
                cancel_second()
                self.assertEqual(calls, ["cancelled", "cancelled"])

    def test_concurrent_local_and_external_cancellation_notifies_once(self):
        parent = CancellationToken()
        child = TurnControl(parent)
        calls = []
        child.subscribe(lambda: calls.append("cancelled"))
        start = Barrier(3)

        def cancel(source):
            start.wait()
            source.cancel()

        workers = [Thread(target=cancel, args=(source,)) for source in (parent, child)]
        for worker in workers:
            worker.start()
        start.wait()
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(calls, ["cancelled"])
