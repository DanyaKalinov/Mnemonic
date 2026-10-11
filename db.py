import json
import os
from typing import Any, Optional

import asyncpg
from pgvector.asyncpg import register_vector

DATABASE_URL = os.getenv("DATABASE_URL")

_pool: Optional[asyncpg.Pool] = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    # Регистрируем тип vector, чтобы asyncpg умел кодировать/декодировать
    # эмбеддинги как обычные списки чисел Python (нужно для memory.py).
    await register_vector(conn)


async def init_db() -> None:
    """Создаёт пул соединений с Postgres и инициализирует необходимые таблицы."""
    global _pool
    if not DATABASE_URL:
        raise ValueError("❌ DATABASE_URL не задан в .env")
    _pool = await asyncpg.create_pool(
        dsn=DATABASE_URL,
        min_size=1,
        max_size=5,
        init=_init_connection,
        # Supabase Transaction pooler работает через PgBouncer в режиме
        # transaction, где соединения переиспользуются между запросами —
        # подготовленные выражения (prepared statements) там не работают.
        # asyncpg использует их по умолчанию, поэтому явно отключаем кэш.
        statement_cache_size=0,
    )
    
    # Автоматически создаем таблицу задач при запуске
    await create_tasks_table_if_not_exists()


async def close_db() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError(
            "База данных не инициализирована — вызови await init_db() при старте бота."
        )
    return _pool


def _serialize_settings(settings: Optional[dict]) -> str:
    return json.dumps(settings or {})


def _deserialize_settings(raw: Any) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}


# ----- Пользователи -----
async def add_user(
    user_id: int, settings: Optional[dict] = None, status: str = "pending", role: str = "user"
) -> None:
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, settings, status, role)
            VALUES ($1, $2::jsonb, $3, $4)
            ON CONFLICT (user_id) DO NOTHING
            """,
            user_id, _serialize_settings(settings), status, role,
        )


async def get_user(user_id: int) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, settings, status, role, eva_access, last_digest_at, created_at FROM users WHERE user_id = $1",
            user_id,
        )
    if row is None:
        return None
    return {
        "user_id": row["user_id"],
        "settings": _deserialize_settings(row["settings"]),
        "status": row["status"],
        "role": row["role"],
        "eva_access": row["eva_access"],
        "last_digest_at": row["last_digest_at"],
        "created_at": row["created_at"],
    }


async def set_user_role(user_id: int, role: str) -> bool:
    """role: 'user' или 'owner'. Владельцы могут отдавать команды Еве."""
    async with get_pool().acquire() as conn:
        result = await conn.execute(
            "UPDATE users SET role = $2 WHERE user_id = $1", user_id, role,
        )
    return int(result.split()[-1]) > 0


async def is_owner(user_id: int) -> bool:
    user = await get_user(user_id)
    return user is not None and user["role"] == "owner"


async def can_use_eva(user_id: int) -> bool:
    """Владелец — всегда может. Обычный пользователь — только если доступ выдан явно."""
    user = await get_user(user_id)
    if user is None:
        return False
    return user["role"] == "owner" or user["eva_access"] == "granted"


async def set_eva_access(user_id: int, access: str) -> bool:
    """access: 'none' | 'pending' | 'granted'."""
    async with get_pool().acquire() as conn:
        result = await conn.execute(
            "UPDATE users SET eva_access = $2 WHERE user_id = $1", user_id, access,
        )
    return int(result.split()[-1]) > 0


async def list_owner_ids() -> list[int]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM users WHERE role = 'owner'")
    return [row["user_id"] for row in rows]


async def list_eva_access_users() -> list[dict]:
    """Для просмотра владельцем — кто имеет/запросил доступ к Еве."""
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT user_id, role, eva_access
            FROM users
            WHERE role = 'owner' OR eva_access IN ('pending', 'granted')
            ORDER BY eva_access DESC, created_at
            """
        )
    return [dict(row) for row in rows]


async def set_user_status(user_id: int, status: str) -> None:
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE users SET status = $2 WHERE user_id = $1", user_id, status,
        )


async def set_user_setting_key(user_id: int, key: str, value) -> bool:
    """
    Точечная запись одного ключа в settings через jsonb_set.
    Не затирает остальные ключи (timezone и др.), безопасна при гонках с фоновым циклом.
    """
    value_json = json.dumps(value, ensure_ascii=False)
    async with get_pool().acquire() as conn:
        result = await conn.execute(
            """
            UPDATE users
            SET settings = jsonb_set(COALESCE(settings, '{}'::jsonb), ARRAY[$2::text], $3::jsonb, true)
            WHERE user_id = $1
            """,
            user_id, key, value_json,
        )
    return int(result.split()[-1]) > 0


async def claim_task_reminder_run(user_id: int, old_next_run: str, new_next_run: str) -> bool:
    """
    «Занимает» запуск напоминания: атомарно меняет next_run_at, только если он ещё равен old_next_run.
    Если два экземпляра бота (деплой на Render) работают одновременно — сообщение уйдёт только из одного.
    """
    async with get_pool().acquire() as conn:
        result = await conn.execute(
            """
            UPDATE users
            SET settings = jsonb_set(settings, '{task_reminders,next_run_at}', to_jsonb($3::text), true)
            WHERE user_id = $1 AND settings->'task_reminders'->>'next_run_at' = $2
            """,
            user_id, old_next_run, new_next_run,
        )
    return int(result.split()[-1]) > 0


async def disable_task_reminders(user_id: int) -> None:
    """Выключает напоминания о задачах (например, если пользователь заблокировал бота)."""
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET settings = jsonb_set(settings, '{task_reminders,enabled}', 'false'::jsonb, true)
            WHERE user_id = $1 AND settings ? 'task_reminders'
            """,
            user_id,
        )


async def get_users_with_task_reminders() -> list[dict]:
    """Пользователи с включёнными напоминаниями о задачах."""
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT user_id, settings
            FROM users
            WHERE settings->'task_reminders'->>'enabled' = 'true'
            """
        )
    return [dict(row) for row in rows]


async def update_user_settings(user_id: int, settings: dict) -> bool:
    async with get_pool().acquire() as conn:
        result = await conn.execute(
            "UPDATE users SET settings = $2::jsonb WHERE user_id = $1",
            user_id, _serialize_settings(settings),
        )
    return int(result.split()[-1]) > 0


async def user_exists(user_id: int) -> bool:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM users WHERE user_id = $1 LIMIT 1", user_id
        )
    return row is not None


async def list_users(limit: int = 50) -> list[dict]:
    """Для админ-команды /users — последние зарегистрированные пользователи."""
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT user_id, status, created_at
            FROM users
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )
    return [dict(row) for row in rows]


async def list_approved_users() -> list[int]:
    """Для дайджестов и рассылок — только одобренные пользователи."""
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM users WHERE status = 'approved'")
    return [row["user_id"] for row in rows]


async def set_last_digest_at(user_id: int) -> None:
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE users SET last_digest_at = now() WHERE user_id = $1", user_id
        )


async def get_stats() -> dict:
    """Для админ-команды /stats."""
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM users) AS total_users,
                (SELECT count(*) FROM users WHERE status = 'approved') AS approved_users,
                (SELECT count(*) FROM users WHERE status = 'pending') AS pending_users,
                (SELECT count(*) FROM memories) AS total_memories,
                (SELECT count(*) FROM reminders WHERE is_sent = false) AS active_reminders
            """
        )
    return dict(row)


# ----- Напоминания -----
async def add_reminder(
    user_id: int, text: str, notify_at, recurrence: Optional[str] = None
) -> int:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO reminders (user_id, text, notify_at, recurrence)
            VALUES ($1, $2, $3, $4)
            RETURNING id
            """,
            user_id, text, notify_at, recurrence,
        )
    return row["id"]


async def get_due_reminders() -> list[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, user_id, text, notify_at, recurrence
            FROM reminders
            WHERE notify_at <= now() AND is_sent = false
            """
        )
    return [dict(row) for row in rows]


async def mark_reminder_sent(reminder_id: int) -> None:
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE reminders SET is_sent = true WHERE id = $1", reminder_id
        )


async def reschedule_reminder(reminder_id: int, recurrence: str) -> None:
    """Переносит повторяющееся напоминание на следующий срок, не помечая его отправленным."""
    interval = {"daily": "1 day", "weekly": "7 days", "monthly": "1 month"}[recurrence]
    async with get_pool().acquire() as conn:
        await conn.execute(
            f"UPDATE reminders SET notify_at = notify_at + interval '{interval}' WHERE id = $1",
            reminder_id,
        )


async def snooze_reminder(reminder_id: int, minutes: int = 60) -> None:
    """Откладывает напоминание на N минут и снова делает его активным."""
    async with get_pool().acquire() as conn:
        await conn.execute(
            f"UPDATE reminders SET notify_at = now() + interval '{minutes} minutes', is_sent = false WHERE id = $1",
            reminder_id,
        )


async def get_user_reminders(user_id: int) -> list[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, text, notify_at, is_sent, recurrence
            FROM reminders
            WHERE user_id = $1
            ORDER BY notify_at
            """,
            user_id,
        )
    return [dict(row) for row in rows]


async def delete_reminder(reminder_id: int, user_id: int) -> bool:
    """Удаляет напоминание, только если оно принадлежит этому же пользователю."""
    async with get_pool().acquire() as conn:
        result = await conn.execute(
            "DELETE FROM reminders WHERE id = $1 AND user_id = $2",
            reminder_id, user_id,
        )
    return int(result.split()[-1]) > 0


# ----- Память (списки/удаление — сам поиск по смыслу остался в memory.py) -----
async def get_user_memories(user_id: int, limit: int = 10) -> list[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, text, tag, created_at
            FROM memories
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            user_id, limit,
        )
    return [dict(row) for row in rows]


async def delete_memory(memory_id: int, user_id: int) -> bool:
    """Удаляет запись, только если она принадлежит этому же пользователю."""
    async with get_pool().acquire() as conn:
        result = await conn.execute(
            "DELETE FROM memories WHERE id = $1 AND user_id = $2",
            memory_id, user_id,
        )
    return int(result.split()[-1]) > 0


async def get_recent_memories(user_id: int, hours: int = 24) -> list[dict]:
    """Для дневного дайджеста — записи за последние N часов."""
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT text, tag, created_at
            FROM memories
            WHERE user_id = $1 AND created_at >= now() - interval '{hours} hours'
            ORDER BY created_at
            """,
            user_id,
        )
    return [dict(row) for row in rows]


# ----- Команды для desktop-агента Евы -----
async def add_eva_command(
    requested_by: int, command_text: str, action_json: Optional[str] = None, status: str = "pending"
) -> int:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO eva_commands (requested_by, command_text, action_json, status)
            VALUES ($1, $2, $3, $4)
            RETURNING id
            """,
            requested_by, command_text, action_json, status,
        )
    return row["id"]


async def get_pending_eva_commands() -> list[dict]:
    """Команды, которые ещё не были отправлены ни одному агенту.
    Команды в статусе 'awaiting_confirmation' сюда не попадают —
    они ждут решения владельца в Telegram, а не подключения агента."""
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, requested_by, command_text, action_json
            FROM eva_commands
            WHERE status = 'pending'
            ORDER BY created_at
            """
        )
    return [dict(row) for row in rows]


async def get_eva_command(command_id: int) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, requested_by, command_text, action_json, status, result_text
            FROM eva_commands WHERE id = $1
            """,
            command_id,
        )
    return dict(row) if row else None


async def update_eva_command_status(
    command_id: int, status: str, result_text: Optional[str] = None
) -> None:
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            UPDATE eva_commands
            SET status = $2, result_text = $3, updated_at = now()
            WHERE id = $1
            """,
            command_id, status, result_text,
        )


# ----- Задачи (Tasks) -----
async def create_tasks_table_if_not_exists() -> None:
    """
    Создаёт таблицу tasks и таблицу task_counters, если их ещё нет.
    Выполняет бэкфилл user_seq для существующих строк.
    Идемпотентна — безопасна при каждом запуске.
    """
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            # 1. Добавляем колонку user_seq, если её нет
            await conn.execute(
                """
                ALTER TABLE tasks ADD COLUMN IF NOT EXISTS user_seq INT;
                """
            )

            # 2. Создаём таблицу счётчиков пользователей, если её нет
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_counters (
                    user_id BIGINT PRIMARY KEY,
                    last_seq INT NOT NULL DEFAULT 0
                );
                """
            )

            # 3. Бэкфилл: назначаем user_seq там, где он NULL
            # ROW_NUMBER() даёт 1,2,3... в порядке created_at, id
            await conn.execute(
                """
                UPDATE tasks AS t
                SET user_seq = sub.user_seq
                FROM (
                    SELECT id, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY created_at, id) AS user_seq
                    FROM tasks
                ) AS sub
                WHERE t.id = sub.id AND t.user_seq IS NULL;
                """
            )

            # 4. Синхронизируем task_counters.last_seq = MAX(user_seq) по пользователю
            # UPSERT: вставляем новых, обновляем существующих
            await conn.execute(
                """
                INSERT INTO task_counters (user_id, last_seq)
                SELECT user_id, MAX(user_seq)
                FROM tasks
                GROUP BY user_id
                ON CONFLICT (user_id) DO UPDATE SET last_seq = EXCLUDED.last_seq;
                """
            )

            # 5. Создаём уникальный индекс на (user_id, user_seq)
            await conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS tasks_user_seq_uidx
                ON tasks (user_id, user_seq);
                """
            )


async def add_single_task(user_id: int, title: str) -> int:
    """
    Добавляет одну задачу для пользователя.
    Атомарно получает следующий user_seq из task_counters.
    Возвращает user_seq (номер задачи для пользователя).
    """
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            # Получаем следующий номер атомарно
            row = await conn.fetchrow(
                """
                INSERT INTO task_counters (user_id, last_seq)
                VALUES ($1, 1)
                ON CONFLICT (user_id) DO UPDATE SET last_seq = task_counters.last_seq + 1
                RETURNING last_seq
                """,
                user_id,
            )
            user_seq = row["last_seq"]

            # Вставляем задачу с полученным номером
            await conn.execute(
                """
                INSERT INTO tasks (user_id, title, user_seq)
                VALUES ($1, $2, $3)
                """,
                user_id, title, user_seq,
            )
    return user_seq


async def add_tasks_batch(user_id: int, tasks_titles: list[str], interval_minutes: int = 0) -> list[int]:
    """
    Массово добавляет список задач.
    Атомарно получает следующие user_seq из task_counters для каждой задачи.
    Возвращает список user_seq (номера задач для пользователя).
    """
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            inserted_seqs = []
            for title in tasks_titles:
                # Получаем следующий номер атомарно
                row = await conn.fetchrow(
                    """
                    INSERT INTO task_counters (user_id, last_seq)
                    VALUES ($1, 1)
                    ON CONFLICT (user_id) DO UPDATE SET last_seq = task_counters.last_seq + 1
                    RETURNING last_seq
                    """,
                    user_id,
                )
                user_seq = row["last_seq"]

                # Вставляем задачу с полученным номером
                await conn.execute(
                    """
                    INSERT INTO tasks (user_id, title, interval_minutes, user_seq)
                    VALUES ($1, $2, $3, $4)
                    """,
                    user_id, title, interval_minutes, user_seq,
                )
                inserted_seqs.append(user_seq)
            return inserted_seqs


async def get_user_tasks(user_id: int) -> list[dict]:
    """Возвращает все задачи пользователя с их user_seq."""
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, user_id, title, status, interval_minutes, created_at, user_seq
            FROM tasks
            WHERE user_id = $1
            ORDER BY created_at DESC
            """,
            user_id,
        )
    return [dict(row) for row in rows]


async def toggle_task_status(user_id: int, user_seq: int) -> Optional[str]:
    """
    Переключает статус задачи с pending на completed и наоборот.
    Использует (user_id, user_seq) вместо глобального id — это закрывает дыру,
    когда чужую задачу можно переключить подделанным callback.
    Возвращает новый статус или None, если задача не найдена.
    """
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM tasks WHERE user_id = $1 AND user_seq = $2",
            user_id, user_seq,
        )
        if not row:
            return None
        new_status = "completed" if row["status"] == "pending" else "pending"
        await conn.execute(
            "UPDATE tasks SET status = $3 WHERE user_id = $1 AND user_seq = $2",
            user_id, user_seq, new_status,
        )
    return new_status


async def edit_task_title(user_id: int, user_seq: int, new_title: str) -> bool:
    """
    Обновляет название задачи.
    Использует (user_id, user_seq) вместо глобального id — безопасность.
    Возвращает True, если задача обновлена.
    """
    async with get_pool().acquire() as conn:
        result = await conn.execute(
            "UPDATE tasks SET title = $3 WHERE user_id = $1 AND user_seq = $2",
            user_id, user_seq, new_title,
        )
    return int(result.split()[-1]) > 0


async def clear_completed_tasks(user_id: int) -> int:
    """Удаляет все завершённые задачи пользователя."""
    async with get_pool().acquire() as conn:
        result = await conn.execute(
            "DELETE FROM tasks WHERE user_id = $1 AND status = 'completed'", user_id
        )
    return int(result.split()[-1])


async def get_pending_task_reminders() -> list[dict]:
    """Возвращает все невыполненные задачи для отправки напоминаний."""
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, user_id, title, interval_minutes, created_at
            FROM tasks
            WHERE status = 'pending'
            ORDER BY created_at
            """
        )
    return [dict(row) for row in rows]