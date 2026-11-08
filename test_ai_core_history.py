"""Тесты _history_to_contents из ai_core.py: перевод истории из БД в формат Gemini.
Сеть и база не нужны — функция чистая."""
import os

# ai_core при импорте создаёт клиент Gemini — даём заглушку ключа, если его нет в окружении
os.environ.setdefault("GOOGLE_API_KEY", "test-key")

from ai_core import _history_to_contents, CHAT_MESSAGE_MAX_CHARS


def _msg(role: str, text: str) -> dict:
    return {"role": role, "text": text}


def _roles(contents) -> list[str]:
    return [c.role for c in contents]


def _texts(contents) -> list[str]:
    return [c.parts[0].text for c in contents]


def test_empty_history_gives_empty_list():
    assert _history_to_contents([]) == []


def test_assistant_role_becomes_model():
    contents = _history_to_contents([_msg("user", "привет"), _msg("assistant", "здравствуй")])
    assert _roles(contents) == ["user", "model"]
    assert _texts(contents) == ["привет", "здравствуй"]


def test_leading_assistant_replies_are_dropped():
    # Окно из последних N реплик может начаться с ответа Евы — Gemini такое не принимает
    history = [
        _msg("assistant", "обрывок ответа"),
        _msg("assistant", "ещё один"),
        _msg("user", "вопрос"),
        _msg("assistant", "ответ"),
    ]
    contents = _history_to_contents(history)
    assert _roles(contents) == ["user", "model"]
    assert _texts(contents) == ["вопрос", "ответ"]


def test_only_assistant_replies_give_empty_list():
    assert _history_to_contents([_msg("assistant", "a"), _msg("assistant", "b")]) == []


def test_assistant_in_the_middle_is_kept():
    history = [_msg("user", "1"), _msg("assistant", "2"), _msg("user", "3")]
    assert _roles(_history_to_contents(history)) == ["user", "model", "user"]


def test_order_is_preserved():
    history = [_msg("user" if i % 2 == 0 else "assistant", f"реплика {i}") for i in range(6)]
    assert _texts(_history_to_contents(history)) == [f"реплика {i}" for i in range(6)]


def test_long_message_is_truncated():
    long_text = "я" * (CHAT_MESSAGE_MAX_CHARS + 500)
    contents = _history_to_contents([_msg("user", long_text)])
    assert len(_texts(contents)[0]) == CHAT_MESSAGE_MAX_CHARS


def test_consecutive_same_role_are_not_merged():
    # Фиксируем текущее поведение: подряд идущие реплики одной роли остаются отдельными
    contents = _history_to_contents([_msg("user", "a"), _msg("user", "b")])
    assert _roles(contents) == ["user", "user"]


def test_input_is_not_mutated():
    history = [_msg("assistant", "x"), _msg("user", "y")]
    snapshot = [dict(m) for m in history]
    _history_to_contents(history)
    assert history == snapshot
