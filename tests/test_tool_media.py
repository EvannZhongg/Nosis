"""How a synthesized media message behaves outside the turn that made it.

A message carrying a tool's images has the user role, because that is
the only role an image can travel in.  Everything that treats "the user
role" as "what the person said" therefore has to distinguish them.
"""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agent_core.content import FilePart, ImagePart, TextPart
from agent_core.session import Message, Session
from agent_core.session_store import JsonlSessionStore
from agent_core.subagent import _latest_attachments


class ToolMediaInheritanceTest(unittest.TestCase):
    """A sub-agent inherits the user's attachment, not the parent's."""

    def session_with(self, *items: Message) -> Session:
        session = Session()
        session.items.extend(items)
        return session

    def user_message(self, *parts) -> Message:
        return Message(role="user", content=tuple(parts))

    def media_message(self, *parts) -> Message:
        return Message(
            role="user", content=tuple(parts), origin="tool_media"
        )

    def test_a_media_message_does_not_shadow_the_user_attachment(self) -> None:
        """The child's task refers to what the person attached."""
        session = self.session_with(
            self.user_message(
                TextPart(text="compare these"),
                ImagePart(path="typed.png"),
            ),
            Message(role="assistant", content="looking"),
            self.media_message(
                TextPart(text="[Tool output]"),
                ImagePart(path="found.png"),
            ),
        )

        self.assertEqual(
            _latest_attachments(session), (ImagePart(path="typed.png"),)
        )

    def test_no_attachment_when_the_user_sent_none(self) -> None:
        """A tool's image must not be mistaken for a user attachment."""
        session = self.session_with(
            self.user_message(TextPart(text="what is in the repo?")),
            self.media_message(ImagePart(path="found.png")),
        )

        self.assertEqual(_latest_attachments(session), ())

    def test_the_newest_user_attachment_still_wins(self) -> None:
        session = self.session_with(
            self.user_message(ImagePart(path="old.png")),
            self.media_message(ImagePart(path="found.png")),
            self.user_message(ImagePart(path="new.png")),
        )

        self.assertEqual(
            _latest_attachments(session), (ImagePart(path="new.png"),)
        )

    def test_a_file_attachment_is_inherited_by_the_child(self) -> None:
        attachment = FilePart(
            path=".nosis/attachments/report.pdf",
            filename="report.pdf",
            mime_type="application/pdf",
            size_bytes=123,
        )
        session = self.session_with(self.user_message(attachment))

        self.assertEqual(_latest_attachments(session), (attachment,))


class ToolMediaPersistenceTest(unittest.TestCase):
    """The origin has to survive a reload, or the UI regains the ghost."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)
        self.store = JsonlSessionStore(self.root / "sessions")
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def round_trip(self, *items: Message) -> list[Message]:
        session = Session()
        self.store.bind_workspace(session.session_id, self.workspace)
        session.begin_turn("turn-1")
        for item in items:
            session.add_item(
                item.role, item.content, item.timestamp_utc, item.tool_calls,
                item.tool_call_id, item.reasoning, origin=item.origin,
            )
        session.finish_turn("completed")
        self.store.append_events(session.session_id, session.journal, workspace=self.workspace)
        return self.store.load(
            session.session_id, workspace=self.workspace
        ).items

    def test_the_origin_survives_a_reload(self) -> None:
        loaded = self.round_trip(
            Message(
                role="user",
                content="typed",
                timestamp_utc=datetime(2024, 1, 1, tzinfo=timezone.utc),
            ),
            Message(
                role="user",
                content=(
                    ImagePart(
                        path="found.png",
                        mime_type="image/png",
                        filename="found.png",
                        size_bytes=123,
                    ),
                ),
                timestamp_utc=datetime(2024, 1, 1, tzinfo=timezone.utc),
                origin="tool_media",
            ),
        )

        self.assertEqual(
            [item.origin for item in loaded], ["conversation", "tool_media"]
        )

    def test_an_ordinary_message_reloads_as_a_user_message(self) -> None:
        loaded = self.round_trip(Message(role="user", content="hello"))

        self.assertEqual(loaded[0].origin, "conversation")
        self.assertFalse(loaded[0].is_tool_media)


if __name__ == "__main__":
    unittest.main()
