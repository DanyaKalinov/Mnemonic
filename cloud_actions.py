"""Облачные действия локального мозга: исполняются на сервере, результат
возвращается агенту как cloud_result. Локальный мозг получает СЫРЬЁ и сам
формулирует ответ (см. спецификацию, §5)."""

import asyncio
from datetime import datetime, timedelta
from typing import Optional

import dateparser

from ai_core import tavily_search_snippets, fetch_url_text, normalize_reminder_text
from db import add_reminder
from memory import add_memory, search_memory


async def _create_reminder(user_id: int, params: dict) -> dict:
    text = (params.get("text") or "").strip()
    when_raw = (params.get("when") or "").strip()
    if not text:
        return {"status": "error", "result": "Напоминание без текста."}
    notify_at = dateparser.parse(when_raw) if when_raw else datetime.now() + timedelta(minutes=15)
    if notify_at is None:
        return {"status": "error", "result": "Не поняла время напоминания. Пример: «через 15 минут»."}
    nice_text = await normalize_reminder_text(text)
    rid = await add_reminder(user_id, nice_text, notify_at)
    return {"status": "done", "result": f"⏰ Напомню в {notify_at:%H:%M}: {nice_text}."}


async def _execute_memory_add(user_id: int, params: dict) -> dict:
    text = (params.get("text") or "").strip()
    if not text:
        return {"status": "error", "result": "Нечего сохранять."}
    await add_memory(user_id, text)
    return {"status": "done", "result": "✅ Запомнила."}


async def _execute_memory_search(user_id: int, params: dict) -> dict:
    query = (params.get("query") or "").strip()
    if not query:
        return {"status": "error", "result": "Пустой запрос."}
    found = await search_memory(user_id, query, k=5)
    if not found:
        return {"status": "done", "result": "Ничего не нашла в памяти."}
    return {"status": "done", "result": "\n".join(f"- {f}" for f in found)}


async def _fetch_web_results(user_id: int, params: dict) -> dict:
    query = (params.get("query") or "").strip()
    if not query:
        return {"status": "error", "result": "Пустой запрос."}
    return {"status": "done", "result": await tavily_search_snippets(query)}


async def _fetch_url(user_id: int, params: dict) -> dict:
    url = (params.get("url") or "").strip()
    if not url:
        return {"status": "error", "result": "Нет ссылки."}
    return {"status": "done", "result": await fetch_url_text(url)}


_HANDLERS = {
    "create_reminder": _create_reminder,
    "memory_add": _execute_memory_add,
    "memory_search": _execute_memory_search,
    "fetch_web_results": _fetch_web_results,
    "fetch_url": _fetch_url,
}


async def execute_cloud_action(action: str, params: dict, user_id: int) -> dict:
    """user_id — владелец облачного действия (для изоляции памяти/напоминаний)."""
    handler = _HANDLERS.get(action)
    if handler is None:
        return {"status": "error", "result": f"Неизвестное облачное действие: {action}"}
    try:
        return await handler(user_id, params or {})
    except Exception as e:
        print(f"❌ Ошибка облачного действия {action}: {e}")
        return {"status": "error", "result": "Ошибка сервера — не удалось выполнить."}