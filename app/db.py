import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

DB_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


class Database:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()

    def init(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    username TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    object_key TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS prompt_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_prompt TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS registration_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    registrations_open INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schedule_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    next_prompt TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS banned_users (
                    telegram_id INTEGER PRIMARY KEY,
                    reason TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    is_admin INTEGER NOT NULL,
                    is_banned INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS push_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    endpoint TEXT UNIQUE NOT NULL,
                    p256dh TEXT NOT NULL,
                    auth TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES accounts(id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS account_photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    object_key TEXT NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES accounts(id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS prompt_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prompt_time TEXT NOT NULL,
                    minute_of_day INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO prompt_state (id, last_prompt) VALUES (1, NULL)"
            )
            now = datetime.now().astimezone().isoformat()
            self._conn.execute(
                """
                INSERT OR IGNORE INTO registration_state (id, registrations_open, updated_at)
                VALUES (1, 1, ?)
                """,
                (now,),
            )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO schedule_state (id, next_prompt, updated_at)
                VALUES (1, NULL, ?)
                """,
                (now,),
            )

    def are_registrations_open(self) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT registrations_open FROM registration_state WHERE id = 1"
            ).fetchone()
        if not row:
            return True
        return bool(int(row["registrations_open"]))

    def set_registrations_open(self, registrations_open: bool) -> None:
        now = datetime.now().astimezone().isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE registration_state
                SET registrations_open = ?, updated_at = ?
                WHERE id = 1
                """,
                (1 if registrations_open else 0, now),
            )

    def get_next_prompt(self) -> Optional[datetime]:
        with self._lock:
            row = self._conn.execute(
                "SELECT next_prompt FROM schedule_state WHERE id = 1"
            ).fetchone()
        if row and row["next_prompt"]:
            return datetime.fromisoformat(row["next_prompt"])
        return None

    def set_next_prompt(self, timestamp: Optional[datetime]) -> None:
        now = datetime.now().astimezone().isoformat()
        value = timestamp.isoformat() if timestamp else None
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE schedule_state
                SET next_prompt = ?, updated_at = ?
                WHERE id = 1
                """,
                (value, now),
            )

    def ban_user(self, telegram_id: int, reason: Optional[str]) -> None:
        now = datetime.now().astimezone().isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO banned_users (telegram_id, reason, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET reason = excluded.reason
                """,
                (telegram_id, reason, now),
            )

    def unban_user(self, telegram_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM banned_users WHERE telegram_id = ?",
                (telegram_id,),
            )

    def is_user_banned(self, telegram_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM banned_users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
        return row is not None

    def get_banned_users(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM banned_users ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def count_users(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"]) if row else 0

    def count_banned_users(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM banned_users").fetchone()
        return int(row["n"]) if row else 0

    def count_photos_total(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM photos").fetchone()
        return int(row["n"]) if row else 0

    def count_photos_for_user(self, user_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM photos WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def count_prompt_history(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM prompt_history").fetchone()
        return int(row["n"]) if row else 0

    def get_users_with_photo_counts(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    u.id,
                    u.telegram_id,
                    u.username,
                    u.created_at,
                    COUNT(p.id) AS photo_count
                FROM users u
                LEFT JOIN photos p ON p.user_id = u.id
                GROUP BY u.id
                ORDER BY u.id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_user(self, telegram_id: int) -> bool:
        user = self.get_user_by_telegram(telegram_id)
        if not user:
            return False
        user_id = int(user["id"])
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM photos WHERE user_id = ?", (user_id,))
            self._conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return True

    # Accounts (web / PWA)

    def create_account(self, username: str, password_hash: str, is_admin: bool) -> int:
        now = datetime.now().astimezone().isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO accounts (username, password_hash, is_admin, is_banned, created_at)
                VALUES (?, ?, ?, 0, ?)
                """,
                (username, password_hash, 1 if is_admin else 0, now),
            )
            row = self._conn.execute(
                "SELECT id FROM accounts WHERE username = ?",
                (username,),
            ).fetchone()
            return int(row["id"])

    def get_account_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM accounts WHERE username = ?",
                (username,),
            ).fetchone()
        return dict(row) if row else None

    def get_account_by_id(self, account_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
        return dict(row) if row else None

    def set_account_banned(self, account_id: int, is_banned: bool) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE accounts SET is_banned = ? WHERE id = ?",
                (1 if is_banned else 0, account_id),
            )

    def list_accounts_with_photo_counts(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    a.id,
                    a.username,
                    a.is_admin,
                    a.is_banned,
                    a.created_at,
                    COUNT(ap.id) AS photo_count
                FROM accounts a
                LEFT JOIN account_photos ap ON ap.account_id = a.id
                GROUP BY a.id
                ORDER BY a.id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def count_accounts(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()
        return int(row["n"]) if row else 0

    def count_account_photos_total(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM account_photos"
            ).fetchone()
        return int(row["n"]) if row else 0

    def add_account_photo(self, account_id: int, timestamp: str, object_key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO account_photos (account_id, timestamp, object_key)
                VALUES (?, ?, ?)
                """,
                (account_id, timestamp, object_key),
            )

    def list_account_photos(self, account_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, timestamp, object_key
                FROM account_photos
                WHERE account_id = ?
                ORDER BY timestamp DESC
                """,
                (account_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # Web Push subscriptions

    def upsert_push_subscription(
        self, account_id: int, endpoint: str, p256dh: str, auth: str
    ) -> None:
        now = datetime.now().astimezone().isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO push_subscriptions (account_id, endpoint, p256dh, auth, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(endpoint) DO UPDATE SET
                    account_id = excluded.account_id,
                    p256dh = excluded.p256dh,
                    auth = excluded.auth,
                    updated_at = excluded.updated_at
                """,
                (account_id, endpoint, p256dh, auth, now, now),
            )

    def delete_push_subscription(self, endpoint: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM push_subscriptions WHERE endpoint = ?",
                (endpoint,),
            )

    def list_push_subscriptions(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT ps.*, a.username, a.is_banned
                FROM push_subscriptions ps
                JOIN accounts a ON a.id = ps.account_id
                ORDER BY ps.id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_user(self, telegram_id: int, username: Optional[str]) -> int:
        now = datetime.now().astimezone().isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO users (telegram_id, username, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET username = excluded.username
                """,
                (telegram_id, username, now),
            )
            row = self._conn.execute(
                "SELECT id FROM users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
            return int(row["id"])

    def get_users(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def get_user_by_telegram(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
        return dict(row) if row else None

    def add_photo(self, user_id: int, timestamp: str, object_key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO photos (user_id, timestamp, object_key) VALUES (?, ?, ?)",
                (user_id, timestamp, object_key),
            )

    def list_photos_for_user(self, user_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, timestamp, object_key FROM photos WHERE user_id = ? ORDER BY timestamp DESC",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_last_prompt(self, timestamp: datetime) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE prompt_state SET last_prompt = ? WHERE id = 1",
                (timestamp.isoformat(),),
            )

    def get_last_prompt(self) -> Optional[datetime]:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_prompt FROM prompt_state WHERE id = 1"
            ).fetchone()
        if row and row["last_prompt"]:
            return datetime.fromisoformat(row["last_prompt"])
        return None

    def add_prompt_history(self, timestamp: datetime) -> None:
        minute_of_day = timestamp.hour * 60 + timestamp.minute
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO prompt_history (prompt_time, minute_of_day) VALUES (?, ?)",
                (timestamp.isoformat(), minute_of_day),
            )

    def get_recent_prompt_minutes(self, limit: int) -> List[int]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT minute_of_day
                FROM prompt_history
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [int(row["minute_of_day"]) for row in rows]

    def prune_prompt_history(self, limit: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                DELETE FROM prompt_history
                WHERE id NOT IN (
                    SELECT id FROM prompt_history ORDER BY id DESC LIMIT ?
                )
                """,
                (limit,),
            )
