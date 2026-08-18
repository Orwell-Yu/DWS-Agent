from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import Message, Task


class MessageStore:
    def __init__(self, path: str):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        os.chmod(self.path, 0o600)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    def _create_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            conversation_type TEXT NOT NULL,
            received_at REAL NOT NULL,
            reply_due_at REAL NOT NULL,
            reply_count INTEGER NOT NULL DEFAULT 0,
            last_user_message_id TEXT NOT NULL,
            last_self_message_at REAL,
            status TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0,
            special_care INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(status, reply_due_at);
        CREATE INDEX IF NOT EXISTS idx_tasks_conversation ON tasks(conversation_id, status);

        CREATE TABLE IF NOT EXISTS seen_events (
            event_id TEXT NOT NULL,
            source TEXT NOT NULL,
            seen_at REAL NOT NULL,
            PRIMARY KEY(event_id, source)
        );

        CREATE TABLE IF NOT EXISTS seen_messages (
            message_id TEXT NOT NULL,
            source TEXT NOT NULL,
            seen_at REAL NOT NULL,
            PRIMARY KEY(message_id, source)
        );

        CREATE TABLE IF NOT EXISTS reply_counters (
            sender_id TEXT PRIMARY KEY,
            reply_count INTEGER NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS send_log (
            send_uuid TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            attempted_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            outbound_message_id TEXT
        );

        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
        with self._lock:
            self._conn.executescript(schema)
            send_columns = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(send_log)").fetchall()
            }
            if "outbound_message_id" not in send_columns:
                self._conn.execute("ALTER TABLE send_log ADD COLUMN outbound_message_id TEXT")
            self._conn.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES('generation', '0')"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _generation(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM metadata WHERE key='generation'"
        ).fetchone()
        return int(row["value"]) if row else 0

    def record_seen(self, message: Message) -> bool:
        now = time.time()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO seen_events(event_id, source, seen_at) VALUES(?, ?, ?)",
                (message.event_id, message.source, now),
            )
            message_cursor = self._conn.execute(
                "INSERT OR IGNORE INTO seen_messages(message_id, source, seen_at) VALUES(?, ?, ?)",
                (message.message_id, message.source, now),
            )
            return cursor.rowcount == 1 and message_cursor.rowcount == 1

    def reply_count(self, sender_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT reply_count FROM reply_counters WHERE sender_id=?", (sender_id,)
            ).fetchone()
            return int(row["reply_count"]) if row else 0

    def increment_reply_count(self, sender_id: str) -> int:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO reply_counters(sender_id, reply_count, updated_at)
                VALUES(?, 1, ?)
                ON CONFLICT(sender_id) DO UPDATE SET
                    reply_count=reply_count+1,
                    updated_at=excluded.updated_at
                """,
                (sender_id, now),
            )
            return self.reply_count(sender_id)

    def reset_reply_count(self, sender_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM reply_counters WHERE sender_id=?", (sender_id,))

    def schedule(self, message: Message, due_at: float, special_care: bool) -> str:
        if message.conversation_type == "private" and not special_care:
            task_id = "private:%s" % message.conversation_id
        elif message.conversation_type == "private":
            task_id = "private:%s:%s" % (message.conversation_id, message.message_id)
        else:
            task_id = "group:%s" % message.message_id

        counter = self.reply_count(message.sender_id)
        with self._lock, self._conn:
            generation = self._generation()
            self._conn.execute(
                """
                INSERT INTO tasks(
                    task_id, message_id, conversation_id, sender_id,
                    conversation_type, received_at, reply_due_at, reply_count,
                    last_user_message_id, last_self_message_at, status,
                    generation, special_care
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'pending', ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    message_id=excluded.message_id,
                    sender_id=excluded.sender_id,
                    received_at=excluded.received_at,
                    reply_due_at=excluded.reply_due_at,
                    reply_count=excluded.reply_count,
                    last_user_message_id=excluded.last_user_message_id,
                    last_self_message_at=NULL,
                    status='pending',
                    generation=excluded.generation,
                    special_care=excluded.special_care
                """,
                (
                    task_id,
                    message.message_id,
                    message.conversation_id,
                    message.sender_id,
                    message.conversation_type,
                    message.received_at,
                    due_at,
                    counter,
                    message.message_id,
                    generation,
                    1 if special_care else 0,
                ),
            )
        return task_id

    @staticmethod
    def _task(row: sqlite3.Row) -> Task:
        return Task(
            task_id=row["task_id"],
            message_id=row["message_id"],
            conversation_id=row["conversation_id"],
            sender_id=row["sender_id"],
            conversation_type=row["conversation_type"],
            received_at=float(row["received_at"]),
            reply_due_at=float(row["reply_due_at"]),
            reply_count=int(row["reply_count"]),
            last_user_message_id=row["last_user_message_id"],
            last_self_message_at=row["last_self_message_at"],
            status=row["status"],
            generation=int(row["generation"]),
            special_care=bool(row["special_care"]),
        )

    def due_tasks(self, now: Optional[float] = None, limit: int = 20) -> List[Task]:
        now = now or time.time()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM tasks
                WHERE status='pending' AND reply_due_at <= ?
                ORDER BY special_care DESC, reply_due_at ASC
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            return [self._task(row) for row in rows]

    def claim(self, task_id: str, generation: int) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                UPDATE tasks SET status='generating'
                WHERE task_id=? AND status='pending' AND generation=?
                """,
                (task_id, generation),
            )
            return cursor.rowcount == 1

    def is_current(self, task_id: str, generation: int, status: str = "generating") -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT status, generation FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            return bool(
                row
                and int(row["generation"]) == generation
                and row["status"] == status
                and self._generation() == generation
            )

    def is_reply_task(self, task: Task, status: str = "generating") -> bool:
        """Return true only for an unchanged task backed by a real inbound event."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT message_id, conversation_id, sender_id, conversation_type,
                       status, generation
                FROM tasks WHERE task_id=?
                """,
                (task.task_id,),
            ).fetchone()
            if not row:
                return False
            matches = (
                row["message_id"] == task.message_id
                and row["conversation_id"] == task.conversation_id
                and row["sender_id"] == task.sender_id
                and row["conversation_type"] == task.conversation_type
                and row["status"] == status
                and int(row["generation"]) == task.generation
                and self._generation() == task.generation
            )
            if not matches:
                return False
            inbound = self._conn.execute(
                """
                SELECT 1 FROM seen_messages
                WHERE message_id=? AND source IN ('private', 'at')
                LIMIT 1
                """,
                (task.message_id,),
            ).fetchone()
            return inbound is not None

    def set_status(self, task_id: str, status: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE tasks SET status=? WHERE task_id=?", (status, task_id))

    def cancel_for_self_message(
        self,
        conversation_id: str,
        sent_at: float,
        conversation_type: str,
        sender_id: Optional[str] = None,
    ) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                UPDATE tasks
                SET status='cancelled:self_reply', last_self_message_at=?
                WHERE conversation_id=?
                  AND conversation_type=?
                  AND status IN ('pending', 'generating')
                  AND received_at <= ?
                """,
                (sent_at, conversation_id, conversation_type, sent_at),
            )
            if conversation_type == "private" and sender_id:
                self._conn.execute("DELETE FROM reply_counters WHERE sender_id=?", (sender_id,))
            return cursor.rowcount

    def recover(self, max_lateness_seconds: float, now: Optional[float] = None) -> Dict[str, int]:
        now = now or time.time()
        cutoff = now - max_lateness_seconds
        with self._lock, self._conn:
            stale = self._conn.execute(
                """
                UPDATE tasks SET status='cancelled:restart_too_late'
                WHERE status IN ('pending', 'generating') AND reply_due_at < ?
                """,
                (cutoff,),
            ).rowcount
            recovered = self._conn.execute(
                """
                UPDATE tasks SET status='pending'
                WHERE status='generating' AND reply_due_at >= ?
                """,
                (cutoff,),
            ).rowcount
            return {"stale": stale, "recovered": recovered}

    def upsert_send_log(
        self,
        send_uuid: str,
        task: Task,
        attempts: int,
        status: str,
        outbound_message_id: Optional[str] = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO send_log(
                    send_uuid, task_id, message_id, conversation_id,
                    attempted_at, attempts, status, outbound_message_id
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(send_uuid) DO UPDATE SET
                    attempted_at=excluded.attempted_at,
                    attempts=excluded.attempts,
                    status=excluded.status,
                    outbound_message_id=COALESCE(
                        excluded.outbound_message_id, send_log.outbound_message_id
                    )
                """,
                (
                    send_uuid,
                    task.task_id,
                    task.message_id,
                    task.conversation_id,
                    time.time(),
                    attempts,
                    status,
                    outbound_message_id,
                ),
            )

    def send_status(self, send_uuid: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM send_log WHERE send_uuid=?", (send_uuid,)
            ).fetchone()
            return row["status"] if row else None

    def sent_markers(self, conversation_id: str, since: float) -> Dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT attempted_at, outbound_message_id
                FROM send_log
                WHERE conversation_id=? AND status='sent' AND attempted_at >= ?
                """,
                (conversation_id, since),
            ).fetchall()
            return {
                "message_ids": {
                    row["outbound_message_id"]
                    for row in rows
                    if row["outbound_message_id"]
                },
                "attempted_at": [float(row["attempted_at"]) for row in rows],
            }

    def daily_reset(
        self,
        seen_retention_seconds: float,
        send_retention_seconds: float,
        now: Optional[float] = None,
    ) -> int:
        now = now or time.time()
        with self._lock, self._conn:
            generation = self._generation() + 1
            self._conn.execute(
                "UPDATE metadata SET value=? WHERE key='generation'", (str(generation),)
            )
            self._conn.execute("DELETE FROM tasks")
            self._conn.execute("DELETE FROM reply_counters")
            self._conn.execute(
                "DELETE FROM seen_events WHERE seen_at < ?", (now - seen_retention_seconds,)
            )
            self._conn.execute(
                "DELETE FROM seen_messages WHERE seen_at < ?", (now - seen_retention_seconds,)
            )
            self._conn.execute(
                "DELETE FROM send_log WHERE attempted_at < ?", (now - send_retention_seconds,)
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('last_reset_at', ?)",
                (str(now),),
            )
            return generation

    def get_meta(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM metadata WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES(?, ?)",
                (key, value),
            )

    def status_summary(self) -> Dict[str, Any]:
        with self._lock:
            statuses = {
                row["status"]: row["count"]
                for row in self._conn.execute(
                    "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
                ).fetchall()
            }
            replies = self._conn.execute(
                "SELECT COALESCE(SUM(reply_count), 0) AS total FROM reply_counters"
            ).fetchone()["total"]
            sent = self._conn.execute(
                "SELECT COUNT(*) AS count FROM send_log WHERE status='sent'"
            ).fetchone()["count"]
            return {
                "tasks": statuses,
                "private_reply_count": int(replies),
                "sent_records": int(sent),
                "generation": self._generation(),
            }

    def recent_tasks(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT task_id, message_id, conversation_id, sender_id,
                       conversation_type, received_at, reply_due_at, status,
                       special_care
                FROM tasks ORDER BY received_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]
