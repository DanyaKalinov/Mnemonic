"""
FSM-хранилище состояний в PostgreSQL (Supabase).

Заменяет MemoryStorage: состояния диалогов (например, создание задачи
или настройка напоминаний) переживают перезапуск и деплой на Render.
SQL-запросы находятся в db.py, здесь только адаптер под интерфейс aiogram.
"""
from typing import Any, Optional

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey

from db import fsm_get_state, fsm_get_data, fsm_set_state, fsm_set_data


def _key_to_str(key: StorageKey) -> str:
    """Превращает StorageKey в строковый ключ для таблицы fsm_states."""
    # thread_id и business_connection_id есть не во всех версиях aiogram, поэтому getattr
    thread = getattr(key, "thread_id", None) or 0
    biz = getattr(key, "business_connection_id", None) or ""
    return f"{key.bot_id}:{key.chat_id}:{key.user_id}:{thread}:{biz}:{key.destiny}"


def _state_to_str(state: Any) -> Optional[str]:
    """Состояние может прийти как State, как строка или как None."""
    if isinstance(state, State):
        return state.state
    return state


class PostgresStorage(BaseStorage):
    """Хранит состояние и данные FSM в таблице fsm_states."""

    async def set_state(self, key: StorageKey, state: Any = None) -> None:
        await fsm_set_state(_key_to_str(key), _state_to_str(state))

    async def get_state(self, key: StorageKey) -> Optional[str]:
        return await fsm_get_state(_key_to_str(key))

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        await fsm_set_data(_key_to_str(key), data)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        return await fsm_get_data(_key_to_str(key))

    async def close(self) -> None:
        # Пул соединений закрывается в db.close_db(), здесь ничего делать не нужно
        return None
