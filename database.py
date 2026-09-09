"""
database.py — لایه‌ی دیتابیس النا (Elena)

از PostgreSQL روی Railway استفاده می‌کند (asyncpg، بدون ORM سنگین).
مسئولیت‌ها:
    - ساخت جدول‌ها در startup
    - محدودیت پیام (75/روز ، 30/۴ساعت) — بدون race condition (شمارش اتمیک با COUNT در تراکنش)
    - تاریخچه‌ی گفتگو (per chat، محدود به آخرین N پیام)
    - cooldown برای ورود خودکار النا به بحث گروه‌ها
    - حافظه‌ی سبک کاربر (/forget قابل پاک‌کردن)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import asyncpg

logger = logging.getLogger("elena.database")

DAILY_LIMIT = 75
WINDOW_4H_LIMIT = 30
HISTORY_LIMIT = 16  # تعداد پیام‌های اخیر هر چت که در Context نگه داشته می‌شود
GROUP_COOLDOWN_SECONDS = 240  # فاصله‌ی حداقلی بین دو ورود خودکار النا به بحث یک گروه

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     BIGINT PRIMARY KEY,
    first_name  TEXT,
    username    TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    premium     BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS message_log (
    id          BIGSERIAL PRIMARY KEY,
    chat_id     BIGINT NOT NULL,
    user_id     BIGINT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_message_log_lookup
    ON message_log (chat_id, user_id, created_at);

CREATE TABLE IF NOT EXISTS conversation (
    id          BIGSERIAL PRIMARY KEY,
    chat_id     BIGINT NOT NULL,
    role        TEXT NOT NULL,          -- 'user' | 'model'
    author_name TEXT,
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_conversation_chat
    ON conversation (chat_id, created_at);

CREATE TABLE IF NOT EXISTS group_cooldown (
    chat_id             BIGINT PRIMARY KEY,
    last_ambient_reply  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS memory_note (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    note        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_memory_user ON memory_note (user_id);
"""


class Database:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=10)
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA)
        logger.info("Database connected and schema ensured.")

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    # ---------- کاربران ----------

    async def upsert_user(self, user_id: int, first_name: str, username: str | None) -> None:
        await self.pool.execute(
            """
            INSERT INTO users (user_id, first_name, username)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE
                SET first_name = EXCLUDED.first_name,
                    username = EXCLUDED.username
            """,
            user_id, first_name, username,
        )

    async def is_premium(self, user_id: int) -> bool:
        row = await self.pool.fetchrow("SELECT premium FROM users WHERE user_id=$1", user_id)
        return bool(row and row["premium"])

    # ---------- محدودیت پیام (اتمیک) ----------

    async def register_message_and_check_limit(
        self, chat_id: int, user_id: int
    ) -> tuple[bool, int, int, int, int]:
        """
        یک پیام را ثبت می‌کند و همزمان محدودیت را در یک تراکنش بررسی می‌کند
        (INSERT + COUNT در یک تراکنش، برای جلوگیری از race condition).

        خروجی: (allowed, used_today, limit_today, used_4h, limit_4h)
        """
        limit_today = DAILY_LIMIT
        limit_4h = WINDOW_4H_LIMIT
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                now = datetime.now(timezone.utc)
                day_ago = now - timedelta(hours=24)
                four_h_ago = now - timedelta(hours=4)

                used_today = await conn.fetchval(
                    "SELECT count(*) FROM message_log WHERE chat_id=$1 AND user_id=$2 AND created_at > $3",
                    chat_id, user_id, day_ago,
                )
                used_4h = await conn.fetchval(
                    "SELECT count(*) FROM message_log WHERE chat_id=$1 AND user_id=$2 AND created_at > $3",
                    chat_id, user_id, four_h_ago,
                )

                allowed = used_today < limit_today and used_4h < limit_4h
                if allowed:
                    await conn.execute(
                        "INSERT INTO message_log (chat_id, user_id) VALUES ($1, $2)",
                        chat_id, user_id,
                    )
                    used_today += 1
                    used_4h += 1

        return allowed, used_today, limit_today, used_4h, limit_4h

    async def get_usage(self, chat_id: int, user_id: int) -> tuple[int, int]:
        now = datetime.now(timezone.utc)
        day_ago = now - timedelta(hours=24)
        four_h_ago = now - timedelta(hours=4)
        used_today = await self.pool.fetchval(
            "SELECT count(*) FROM message_log WHERE chat_id=$1 AND user_id=$2 AND created_at > $3",
            chat_id, user_id, day_ago,
        )
        used_4h = await self.pool.fetchval(
            "SELECT count(*) FROM message_log WHERE chat_id=$1 AND user_id=$2 AND created_at > $3",
            chat_id, user_id, four_h_ago,
        )
        return used_today, used_4h

    # ---------- تاریخچه‌ی گفتگو ----------

    async def add_turn(self, chat_id: int, role: str, content: str, author_name: str | None = None) -> None:
        await self.pool.execute(
            "INSERT INTO conversation (chat_id, role, author_name, content) VALUES ($1, $2, $3, $4)",
            chat_id, role, author_name, content,
        )
        # نگه‌داشتن فقط آخرین HISTORY_LIMIT پیام هر چت (پاک‌سازی سبک، بدون رشد نامحدود)
        await self.pool.execute(
            """
            DELETE FROM conversation
            WHERE id IN (
                SELECT id FROM conversation
                WHERE chat_id=$1
                ORDER BY created_at DESC
                OFFSET $2
            )
            """,
            chat_id, HISTORY_LIMIT,
        )

    async def get_history(self, chat_id: int, limit: int = HISTORY_LIMIT) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT role, author_name, content FROM conversation WHERE chat_id=$1 ORDER BY created_at DESC LIMIT $2",
            chat_id, limit,
        )
        return [dict(r) for r in reversed(rows)]

    # ---------- Cooldown حضور خودکار در گروه ----------

    async def ambient_cooldown_ok(self, chat_id: int) -> bool:
        row = await self.pool.fetchrow(
            "SELECT last_ambient_reply FROM group_cooldown WHERE chat_id=$1", chat_id
        )
        if not row or not row["last_ambient_reply"]:
            return True
        elapsed = (datetime.now(timezone.utc) - row["last_ambient_reply"]).total_seconds()
        return elapsed >= GROUP_COOLDOWN_SECONDS

    async def mark_ambient_reply(self, chat_id: int) -> None:
        await self.pool.execute(
            """
            INSERT INTO group_cooldown (chat_id, last_ambient_reply)
            VALUES ($1, now())
            ON CONFLICT (chat_id) DO UPDATE SET last_ambient_reply = now()
            """,
            chat_id,
        )

    # ---------- حافظه‌ی سبک کاربر ----------

    async def add_memory_note(self, user_id: int, note: str) -> None:
        await self.pool.execute(
            "INSERT INTO memory_note (user_id, note) VALUES ($1, $2)", user_id, note
        )

    async def get_memory_notes(self, user_id: int, limit: int = 20) -> list[str]:
        rows = await self.pool.fetch(
            "SELECT note FROM memory_note WHERE user_id=$1 ORDER BY created_at DESC LIMIT $2",
            user_id, limit,
        )
        return [r["note"] for r in rows]

    async def forget_user(self, user_id: int) -> None:
        await self.pool.execute("DELETE FROM memory_note WHERE user_id=$1", user_id)
