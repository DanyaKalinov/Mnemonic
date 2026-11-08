"""Тесты офлайн-ветки process_eva_command: сохранение ответов в историю,
отсутствие двойного ответа для ask_web, игнорирование сообщений об ошибке.
Сеть, база и Telegram замоканы; async-код запускается через asyncio.run,
поэтому pytest-asyncio не нужен."""
import asyncio
import os

os.environ.setdefault("GOOGLE_API_KEY", "test-key")

import pytest

import ai_core
import eva_engine


@pytest.fixture
def env(monkeypatch):
    """Подменяет всё внешнее и возвращает журналы вызовов."""
    log = {"saved": [], "replies": [], "web_calls": [], "ai_calls": []}

    monkeypatch.setattr(eva_engine, "is_agent_online", lambda user_id: False)

    async def fake_save(user_id, role, text, tg_message_id=None):
        log["saved"].append((role, text))
        return len(log["saved"])

    monkeypatch.setattr(eva_engine, "save_chat_message", fake_save)

    async def fake_ask_web(query):
        log["web_calls"].append(query)
        return log.get("web_answer", "ответ из интернета")

    monkeypatch.setattr(eva_engine, "ask_web", fake_ask_web)

    async def fake_ai(user_id, question):
        log["ai_calls"].append(question)
        return log.get("ai_answer", "ответ Евы")

    # в process_eva_command функция импортируется внутри: from ai_core import get_ai_response
    monkeypatch.setattr(ai_core, "get_ai_response", fake_ai)

    async def reply(text):
        log["replies"].append(text)

    log["reply"] = reply
    return log


def _run(env, monkeypatch, command_text, steps):
    """steps — список (action, params, requires_confirmation, error) для каждого шага."""
    texts = [f"шаг {i}" for i in range(len(steps))]
    monkeypatch.setattr(eva_engine, "split_into_subcommands", lambda text: texts)
    it = iter(steps)

    async def fake_resolve(step_text):
        return next(it)

    monkeypatch.setattr(eva_engine, "_resolve_step", fake_resolve)
    asyncio.run(eva_engine.process_eva_command(None, 1, command_text, env["reply"]))


def test_question_without_steps_goes_to_gemini_and_is_saved(env, monkeypatch):
    # Пустой список шагов: все шаги были ask_web, кроме случая, когда шагов нет вовсе
    monkeypatch.setattr(eva_engine, "split_into_subcommands", lambda text: [])
    asyncio.run(eva_engine.process_eva_command(None, 1, "как дела", env["reply"]))
    assert env["ai_calls"] == ["как дела"]
    assert env["replies"] == ["ответ Евы"]
    assert env["saved"] == [("user", "как дела"), ("assistant", "ответ Евы")]


def test_ask_web_only_answers_once_and_saves_pair(env, monkeypatch):
    _run(env, monkeypatch, "найди в интернете погоду", [("ask_web", {"query": "погода"}, False, None)])
    assert env["web_calls"] == ["погода"]
    assert env["ai_calls"] == []  # раньше здесь Gemini отвечал второй раз
    assert env["replies"] == ["🌐 ответ из интернета"]
    # в истории — ответ без префикса «🌐»
    assert env["saved"] == [("user", "шаг 0"), ("assistant", "ответ из интернета")]


def test_error_reply_from_gemini_is_not_saved(env, monkeypatch):
    env["ai_answer"] = "😵 Извини, я завис. Попробуй ещё раз позже."
    monkeypatch.setattr(eva_engine, "split_into_subcommands", lambda text: [])
    asyncio.run(eva_engine.process_eva_command(None, 1, "как дела", env["reply"]))
    assert env["replies"] == [env["ai_answer"]]  # пользователь ошибку видит
    assert env["saved"] == []  # а в историю она не попадает


def test_error_reply_from_ask_web_is_not_saved(env, monkeypatch):
    env["web_answer"] = "⚠️ Не настроен ключ Tavily (TAVILY_API_KEY)."
    _run(env, monkeypatch, "найди в интернете X", [("ask_web", {"query": "X"}, False, None)])
    assert env["saved"] == []
    assert env["ai_calls"] == []


@pytest.mark.parametrize("text,expected", [
    ("⚠️ Ошибка", True),
    ("😵 Извини, я завис.", True),
    ("  ⚠️ с пробелом в начале", True),
    ("", True),
    (None, True),
    ("Обычный ответ", False),
    ("Ответ, в котором есть ⚠️ не в начале", False),
])
def test_is_error_reply(text, expected):
    assert eva_engine.is_error_reply(text) is expected
