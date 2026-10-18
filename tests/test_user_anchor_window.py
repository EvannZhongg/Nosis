import unittest
from datetime import datetime, timezone

from agent_core import (
    AgentConfig,
    ContextCompressionConfig,
    ContextManager,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Session,
    ToolConfig,
)


class AnchorProvider(LLMProvider):
    def __init__(self):
        self.counted_requests = []
        self.requests = []

    @property
    def max_context_tokens(self):
        return 100_000

    def count_input_tokens(self, request: LLMRequest):
        self.counted_requests.append(request)
        return sum(len((message.content or "").split()) for message in request.messages)

    def stream(self, request, on_text_delta, on_reasoning_delta=None):
        self.requests.append(request)
        return LLMResponse(content="- [ephemeral] Work continues.")


class UserAnchorWindowTest(unittest.TestCase):
    def session(self, inputs):
        session = Session("anchors")
        for index, content in enumerate(inputs, 1):
            session.begin_turn(f"turn-{index}")
            session.add_item(
                "user", content, datetime(2026, 10, 15, tzinfo=timezone.utc)
            )
            session.add_item("assistant", "answer")
            if index < len(inputs):
                session.finish_turn("completed")
        session.set_archived_summary("- [ephemeral] Checkpoint.", len(session.items))
        return session

    def context(self, session, budget=2000):
        provider = AnchorProvider()
        config = AgentConfig(
            max_same_tool_calls=5,
            output_reserve_tokens=100,
            tools=ToolConfig(enabled=()),
            workspace_instruction_files=(),
            context=ContextCompressionConfig(
                keep_recent_units=1, user_anchor_token_budget=budget
            ),
        )
        return ContextManager(provider, session, "System", "Consolidate", config), provider

    def rendered(self, request):
        return "\n".join(message.content or "" for message in request.messages)

    def test_extends_to_multiple_complete_turns_within_budget(self):
        inputs = [f"command-{index} " * 100 for index in range(5)]
        context, provider = self.context(self.session(inputs), budget=400)

        rendered = self.rendered(context.build_request())

        for content in inputs[:2]:
            self.assertNotIn(content, rendered)
        for content in inputs[2:]:
            self.assertEqual(rendered.count(content), 1)
        self.assertIn("turn_id=turn-3", rendered)
        for request in provider.counted_requests:
            self.assertEqual(request.system_prompt, "")
            self.assertEqual(request.tools, ())
            self.assertNotIn("answer", self.rendered(request))
            self.assertNotIn("Checkpoint.", self.rendered(request))

    def test_accepts_exact_budget_and_rejects_one_token_less(self):
        inputs = [f"command-{index} " * 10 for index in range(4)]
        session = self.session(inputs)
        context, provider = self.context(session)
        context.build_request()
        all_anchors = provider.counted_requests[-1]
        budget = provider.count_input_tokens(all_anchors)

        at_limit, _ = self.context(session, budget)
        below_limit, _ = self.context(session, budget - 1)

        self.assertIn(inputs[0], self.rendered(at_limit.build_request()))
        self.assertNotIn(inputs[0], self.rendered(below_limit.build_request()))
        for content in inputs[1:]:
            self.assertIn(content, self.rendered(below_limit.build_request()))

    def test_keeps_all_inputs_in_the_last_two_turns_over_budget(self):
        inputs = ["old command", "previous command " * 100, "current command " * 100]
        session = self.session(inputs)
        session.record_user_interaction("Question: target?\nUser answer: api", "question_response")
        session.record_user_interaction("User approved deployment", "approval_response")
        session.add_item("user", "Use staging instead", user_source="steering")
        session.set_archived_summary("- [ephemeral] Checkpoint.", len(session.items))
        context, _ = self.context(session, budget=1)

        rendered = self.rendered(context.build_request())

        self.assertNotIn(inputs[0], rendered)
        for content in inputs[1:] + [
            "Question: target?\nUser answer: api",
            "User approved deployment",
            "Use staging instead",
        ]:
            self.assertEqual(rendered.count(content), 1)

    def test_keeps_available_turns_when_fewer_than_two_exist(self):
        for inputs in ([], ["only command"], ["first command", "second command"]):
            with self.subTest(inputs=inputs):
                context, _ = self.context(self.session(inputs), budget=1)
                rendered = self.rendered(context.build_request())
                for content in inputs:
                    self.assertEqual(rendered.count(content), 1)

    def test_does_not_skip_a_large_turn_to_include_an_older_small_turn(self):
        inputs = ["old short command", "large command " * 500, "previous", "current"]
        context, _ = self.context(self.session(inputs), budget=200)

        rendered = self.rendered(context.build_request())

        self.assertNotIn(inputs[0], rendered)
        self.assertNotIn(inputs[1], rendered)
        for content in inputs[2:]:
            self.assertIn(content, rendered)

    def test_optional_turn_is_not_split_between_its_user_inputs(self):
        session = self.session(["oldest command", "optional command"])
        session.record_user_interaction("detailed correction " * 200, "question_response")
        session.finish_turn("completed")
        session.begin_turn("turn-3")
        session.add_item("user", "previous command")
        session.finish_turn("completed")
        session.begin_turn("turn-4")
        session.add_item("user", "current command")
        session.set_archived_summary("- [ephemeral] Checkpoint.", len(session.items))
        context, _ = self.context(session, budget=200)

        rendered = self.rendered(context.build_request())

        self.assertNotIn("oldest command", rendered)
        self.assertNotIn("optional command", rendered)
        self.assertNotIn("detailed correction", rendered)
        self.assertIn("previous command", rendered)
        self.assertIn("current command", rendered)

    def test_archive_cursor_does_not_change_the_budget_or_duplicate_inputs(self):
        inputs = [f"command-{index} " * 100 for index in range(5)]
        session = self.session(inputs[:3])
        session.record_user_interaction("Question: target?\nUser answer: api", "question_response")
        session.add_item("user", "Keep the complete input", user_source="steering")
        for index in (3, 4):
            session.finish_turn("completed")
            session.begin_turn(f"turn-{index + 1}")
            session.add_item("user", inputs[index])
        context, provider = self.context(session, budget=400)
        # The third turn initially remains entirely in the raw tail.
        session.set_archived_summary("- [ephemeral] Checkpoint.", 4)
        first = context.build_request()
        first_counts = tuple(provider.counted_requests)
        provider.counted_requests.clear()
        # Archive its initial input while its later interactions remain in the tail.
        session.set_archived_summary("- [ephemeral] Checkpoint.", 5)
        second = context.build_request()

        self.assertEqual(tuple(provider.counted_requests), first_counts)
        for request in (first, second):
            rendered = self.rendered(request)
            for content in inputs[:2]:
                self.assertNotIn(content, rendered)
            for content in inputs[2:]:
                self.assertEqual(rendered.count(content), 1)
            self.assertEqual(rendered.count("Question: target?\nUser answer: api"), 1)
            self.assertEqual(rendered.count("Keep the complete input"), 1)

    def test_tool_and_assistant_history_does_not_consume_the_anchor_budget(self):
        inputs = [f"command-{index} " * 100 for index in range(5)]
        session = self.session(inputs)
        context, provider = self.context(session, budget=400)
        context.build_request()
        original_counts = tuple(provider.counted_requests)
        provider.counted_requests.clear()
        session.add_item("assistant", "large output " * 10_000)
        session.add_item("user", "background output " * 10_000, origin="job_result")

        context.build_request()

        self.assertEqual(tuple(provider.counted_requests), original_counts)

    def test_repeated_archives_use_the_same_expanded_anchor_window(self):
        inputs = [f"command-{index}" for index in range(5)]
        session = self.session(inputs)
        session.set_archived_summary("- [ephemeral] Checkpoint.", 6)
        context, provider = self.context(session)

        for _ in range(2):
            session.add_item("assistant", "more work")
            context.archive(context.build_request())
            rendered = self.rendered(context.build_request())
            record = self.rendered(provider.requests[-1])
            for content in inputs:
                self.assertEqual(rendered.count(content), 1)
                self.assertIn(content, record)

    def test_replayed_session_retains_complete_historical_turns(self):
        inputs = [f"command-{index} " * 100 for index in range(5)]
        original = self.session(inputs)
        original.finish_turn("completed")
        replayed = Session("anchors")
        for event in original.journal:
            replayed.apply_event(event)
        context, _ = self.context(replayed, budget=400)

        rendered = self.rendered(context.build_request())

        for content in inputs[:2]:
            self.assertNotIn(content, rendered)
        for content in inputs[2:]:
            self.assertEqual(rendered.count(content), 1)
        self.assertEqual(
            [anchor.item_index for anchor in replayed.user_anchors],
            [0, 2, 4, 6, 8],
        )
