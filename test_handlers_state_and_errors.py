"""Тесты этапа 1: выход из состояния по команде и ошибки в истории handle_text."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

import handlers


def test_команда_сбрасывает_состояние_и_пропускается_дальше():
    state = MagicMock()
    state.clear = AsyncMock()
    message = MagicMock()

    with pytest.raises(SkipHandler):
        asyncio.run(handlers.reset_state_on_command(message, state))

    state.clear.assert_awaited_once()


def _подготовить_handle_text(monkeypatch, answer: str):
    """Заглушки для офлайн-ветки handle_text. Возвращает (message, state, sent, add_chat)."""
    sent = MagicMock(message_id=11)
    sent.edit_reply_markup = AsyncMock()
    sent.delete = AsyncMock()

    message = MagicMock()
    message.from_user.id = 1
    message.message_id = 10
    message.text = "расскажи анекдот"
    message.answer = AsyncMock(return_value=sent)

    state = MagicMock()
    state.get_state = AsyncMock(return_value=None)

    add_chat = AsyncMock(return_value=100)
    monkeypatch.setattr(handlers, "add_chat_message", add_chat)
    monkeypatch.setattr(handlers, "add_memory", AsyncMock())
    monkeypatch.setattr(handlers, "get_ai_response", AsyncMock(return_value=answer))
    monkeypatch.setattr("eva_engine.is_agent_online", lambda user_id: False)
    return message, state, sent, add_chat


def test_ошибка_модели_не_попадает_в_историю(monkeypatch):
    message, state, sent, add_chat = _подготовить_handle_text(monkeypatch, "😵 Извини, я завис")
    asyncio.run(handlers.handle_text(message, state))

    add_chat.assert_not_awaited()
    sent.edit_reply_markup.assert_not_awaited()


def test_нормальный_ответ_сохраняется_с_кнопкой_забыть(monkeypatch):
    message, state, sent, add_chat = _подготовить_handle_text(monkeypatch, "Вот анекдот...")
    asyncio.run(handlers.handle_text(message, state))

    assert add_chat.await_count == 2  # вопрос и ответ
    sent.edit_reply_markup.assert_awaited_once()


def test_роутер_команда_в_состоянии_доходит_до_своего_хендлера_и_сбрасывает_состояние(monkeypatch):
    """Сквозная проверка через настоящий Dispatcher: /help в состоянии создания задачи
    должен вызвать cmd_help (а не стать текстом задачи), состояние должно сброситься."""
    from datetime import datetime
    from aiogram import Dispatcher, Bot
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import Update, Message, Chat, User

    called = {"help": 0, "task": 0}

    async def fake_help(message):
        called["help"] += 1

    async def fake_add_single_task(**kwargs):
        called["task"] += 1

    # cmd_help уже зарегистрирован в роутере с оригинальной функцией, поэтому подменяем
    # зависимости, которые она использует: перехватываем message.answer
    async def run():
        storage = MemoryStorage()
        dp = Dispatcher(storage=storage)
        dp.include_router(handlers.router)
        bot = Bot("123:abc")

        sent = []

        async def fake_answer(self, text, **kw):
            sent.append(text)

        monkeypatch.setattr(Message, "answer", fake_answer)
        monkeypatch.setattr(handlers, "add_single_task", fake_add_single_task)
        monkeypatch.setattr(handlers, "is_owner", AsyncMock(return_value=False))
        monkeypatch.setattr(handlers, "can_use_eva", AsyncMock(return_value=False))

        key = StorageKey(bot_id=123, chat_id=1, user_id=1)
        await storage.set_state(key, handlers.TaskCreationStates.waiting_for_task_text)

        msg = Message(
            message_id=1, date=datetime.now(), chat=Chat(id=1, type="private"),
            from_user=User(id=1, is_bot=False, first_name="t"), text="/help",
            entities=[{"type": "bot_command", "offset": 0, "length": 5}],
        )
        await dp.feed_update(bot, Update(update_id=1, message=msg))
        return await storage.get_state(key), sent

    state_after, sent = asyncio.run(run())
    assert state_after is None            # состояние сброшено
    assert called["task"] == 0            # команда не стала задачей
    assert any("Что я умею" in s for s in sent)  # пришла справка
