from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import TestCase

from app.scheduler.poller import _check_for_reply


class _ChatClient:
    def __init__(self, messages):
        self.messages = messages

    def get_chat_messages(self, chat_id, limit=20):
        return self.messages


class ReplyDetectionTests(TestCase):
    def task(self):
        return SimpleNamespace(
            chat_id="chat",
            initial_sent_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        )

    def test_numeric_zero_is_inbound(self):
        client = _ChatClient([{"is_sender": 0, "timestamp": "2026-08-11T17:43:48.662Z"}])
        self.assertTrue(_check_for_reply(client, self.task()))

    def test_boolean_false_is_inbound(self):
        client = _ChatClient([{"is_sender": False, "timestamp": "2026-08-13T01:00:00Z"}])
        self.assertTrue(_check_for_reply(client, self.task()))

    def test_direction_fallback_is_inbound(self):
        client = _ChatClient([{"direction": "inbound", "timestamp": "2026-08-13T01:00:00Z"}])
        self.assertTrue(_check_for_reply(client, self.task()))
