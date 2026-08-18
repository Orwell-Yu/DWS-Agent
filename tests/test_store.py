import tempfile
import unittest
from pathlib import Path

from app.message_store import MessageStore
from app.models import Message


def msg(message_id, sender="user", conversation="conv", kind="private", received=1000):
    return Message(
        event_id="event-" + message_id,
        message_id=message_id,
        conversation_id=conversation,
        sender_id=sender,
        sender_name="name",
        conversation_type=kind,
        received_at=received,
        content_type="text",
        text="hello",
        source="private" if kind == "private" else "at",
    )


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MessageStore(str(Path(self.temp.name) / "state.sqlite3"))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_normal_private_messages_debounce_into_one_task(self):
        first = msg("m1", received=1000)
        second = msg("m2", received=1100)
        one = self.store.schedule(first, 1600, special_care=False)
        two = self.store.schedule(second, 1700, special_care=False)
        self.assertEqual(one, two)
        due = self.store.due_tasks(now=1700)
        self.assertEqual(1, len(due))
        self.assertEqual("m2", due[0].last_user_message_id)
        self.assertEqual(1700, due[0].reply_due_at)

    def test_special_private_and_group_are_independent(self):
        one = self.store.schedule(msg("m1"), 1000, special_care=True)
        two = self.store.schedule(msg("m2"), 1000, special_care=True)
        self.assertNotEqual(one, two)
        g1 = self.store.schedule(msg("g1", kind="group"), 1000, special_care=False)
        g2 = self.store.schedule(msg("g2", kind="group"), 1000, special_care=False)
        self.assertNotEqual(g1, g2)

    def test_seen_is_scoped_by_event_source(self):
        private = msg("m1")
        self.assertTrue(self.store.record_seen(private))
        self.assertFalse(self.store.record_seen(private))
        private.source = "at"
        private.event_id = "other-event"
        self.assertTrue(self.store.record_seen(private))

    def test_manual_reply_cancels_and_resets_counter(self):
        item = msg("m1")
        self.store.schedule(item, 1600, special_care=False)
        self.store.increment_reply_count("user")
        changed = self.store.cancel_for_self_message("conv", 1200, "private", "user")
        self.assertEqual(1, changed)
        self.assertEqual(0, self.store.reply_count("user"))
        self.assertEqual("cancelled:self_reply", self.store.recent_tasks()[0]["status"])

    def test_reset_invalidates_in_flight_task_and_clears_counts(self):
        task_id = self.store.schedule(msg("m1"), 1000, special_care=False)
        task = self.store.due_tasks(now=1000)[0]
        self.assertTrue(self.store.claim(task_id, task.generation))
        self.store.increment_reply_count("user")
        self.store.daily_reset(48 * 3600, 7 * 86400, now=2000)
        self.assertFalse(self.store.is_current(task_id, task.generation))
        self.assertEqual(0, self.store.reply_count("user"))
        self.assertEqual([], self.store.recent_tasks())

    def test_restart_recovery_discards_very_late_tasks(self):
        self.store.schedule(msg("old"), 100, special_care=False)
        self.store.schedule(msg("new", conversation="new-conv"), 990, special_care=False)
        result = self.store.recover(max_lateness_seconds=30, now=1000)
        self.assertEqual(1, result["stale"])
        statuses = {row["message_id"]: row["status"] for row in self.store.recent_tasks()}
        self.assertEqual("cancelled:restart_too_late", statuses["old"])
        self.assertEqual("pending", statuses["new"])


if __name__ == "__main__":
    unittest.main()
