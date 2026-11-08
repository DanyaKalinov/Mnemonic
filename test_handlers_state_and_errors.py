"""Тесты этапа 1: выход из состояния по команде и ошибки в истории handle_text."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import SkipHandler

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
