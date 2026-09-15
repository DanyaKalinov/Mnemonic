"""
Реестр подключённых desktop-агентов Евы.

Вынесен в отдельный модуль специально, чтобы избежать циклического
импорта: eva_engine.py (общая логика команд) должен уметь отправлять
команды агенту, а eva_ws.py (сам WS-эндпоинт) должен уметь вызывать
eva_engine.py для голосовых команд. Если бы push_command_to_agents жила
в eva_ws.py, оба модуля пытались бы импортировать друг друга.
"""
import json
from typing import Optional

from aiohttp import web

from db import update_eva_command_status

# agent_id -> {"ws": WebSocketResponse, "owner_id": int}
# На практике агент обычно один (твой ПК), но структура уже поддерживает
# несколько — просто несколько записей в этом словаре.
_connected_agents: dict[str, dict] = {}


def register_agent(agent_id: str, ws: web.WebSocketResponse, owner_id: int) -> None:
    _connected_agents[agent_id] = {"ws": ws, "owner_id": owner_id}


def unregister_agent(agent_id: str) -> None:
    _connected_agents.pop(agent_id, None)


def is_agent_connected() -> bool:
    return len(_connected_agents) > 0


def get_owner_id_for_agent(agent_id: str) -> Optional[int]:
    entry = _connected_agents.get(agent_id)
    return entry["owner_id"] if entry else None


def get_user_agent(user_id: int) -> Optional[web.WebSocketResponse]:
    """WS-сокет агента, подключённого к этому пользователю (или None)."""
    for entry in _connected_agents.values():
        if entry["owner_id"] == user_id:
            return entry["ws"]
    return None


async def send_to_user_agent(user_id: int, payload: dict) -> bool:
    """Отправляет произвольный JSON-объект агенту конкретного пользователя."""
    ws = get_user_agent(user_id)
    if ws is None:
        return False
    try:
        await ws.send_str(json.dumps(payload))
        return True
    except Exception as e:
        print(f"⚠️ Не удалось отправить '{payload.get('type')}' агенту {user_id}: {e}", flush=True)
        return False


async def notify_agent_reminder(user_id: int, text: str) -> bool:
    """Отправить напоминание подключённому desktop-агенту (тост + звук на ПК).

    Возвращает True, если агент был онлайн и получил сообщение.
    """
    return await send_to_user_agent(user_id, {"type": "reminder", "text": text})


async def push_command_to_agents(command_id: int, action_json: str) -> bool:
    """Пытается сразу отправить структурированное действие подключённому
    агенту. action_json — JSON-строка вида {"action": "...", "params": {...}}
    (или {"steps": [...]}). Возвращает True, если хотя бы один агент
    получил её (иначе она останется в статусе 'pending' и уйдёт агенту при
    следующем подключении)."""
    if not _connected_agents:
        return False
    action_data = json.loads(action_json)
    payload = json.dumps({"type": "command", "id": command_id, **action_data})
    sent = False
    for entry in list(_connected_agents.values()):
        try:
            await entry["ws"].send_str(payload)
            sent = True
        except Exception as e:
            print(f"⚠️ Не удалось отправить команду агенту: {e}", flush=True)
    if sent:
        await update_eva_command_status(command_id, "sent")
    return sent


async def speak_to_owner(owner_id: int, text: str) -> bool:
    """Просит агент(ы) конкретного владельца озвучить текст через TTS —
    используется для голосовых ответов (ask_web, подтверждения и т.п.),
    когда команда изначально пришла голосом, а не текстом в Telegram."""
    sent = False
    payload = json.dumps({"type": "speak", "text": text})
    for entry in list(_connected_agents.values()):
        if entry["owner_id"] != owner_id:
            continue
        try:
            await entry["ws"].send_str(payload)
            sent = True
        except Exception as e:
            print(f"⚠️ Не удалось отправить 'speak' агенту: {e}", flush=True)
    return sent


async def notify_app(user_id: int, role: str, text: str) -> bool:
    """Показывает строку в чате-приложении у пользователя (role: user|eva|system)."""
    return await send_to_user_agent(user_id, {"type": "chat_echo", "role": role, "text": text})
