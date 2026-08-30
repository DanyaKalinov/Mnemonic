"""
WebSocket-мост между сервером (Мнемоник/Ева) и desktop-агентом на ПК.

Как это работает:
1. Desktop-агент подключается к /ws/eva с заголовком X-Eva-Agent-Secret.
2. Сразу после подключения сервер отправляет ему все команды, которые
   накопились в очереди, пока агента не было на связи (status='pending').
3. Когда владелец пишет команду боту в Telegram — она сохраняется в БД
   и, если агент сейчас на связи, сразу же отправляется ему.
4. Агент выполняет команду и присылает результат обратно
   ({"type": "result", "command_id": ..., "status": "done"|"error", "result": "..."}) —
   сервер обновляет БД и уведомляет владельца, который её запросил.

Пока это только инфраструктура (этап 1) — самого desktop-агента ещё нет,
но эндпоинт уже готов его принять.
"""
import json
from typing import Optional

from aiogram import Bot
from aiohttp import web, WSMsgType

from config import EVA_AGENT_SECRET
from db import get_pending_eva_commands, update_eva_command_status, get_eva_command

# Подключённые агенты. На практике обычно один (твой ПК), но структура
# уже поддерживает несколько — просто словарь ws-соединений.
_connected_agents: dict[str, web.WebSocketResponse] = {}


def is_agent_connected() -> bool:
    return len(_connected_agents) > 0


async def push_command_to_agents(command_id: int, action_json: str) -> bool:
    """Пытается сразу отправить структурированное действие подключённому
    агенту. action_json — JSON-строка вида {"action": "...", "params": {...}}.
    Возвращает True, если хотя бы один агент получил её (иначе она
    останется в статусе 'pending' и уйдёт агенту при следующем подключении)."""
    if not _connected_agents:
        return False
    action_data = json.loads(action_json)
    payload = json.dumps({"type": "command", "id": command_id, **action_data})
    sent = False
    for ws in list(_connected_agents.values()):
        try:
            await ws.send_str(payload)
            sent = True
        except Exception as e:
            print(f"⚠️ Не удалось отправить команду агенту: {e}", flush=True)
    if sent:
        await update_eva_command_status(command_id, "sent")
    return sent


async def eva_ws_handler(request: web.Request) -> web.WebSocketResponse:
    if not EVA_AGENT_SECRET:
        return web.Response(status=503, text="EVA_AGENT_SECRET не настроен на сервере")

    if request.headers.get("X-Eva-Agent-Secret") != EVA_AGENT_SECRET:
        return web.Response(status=403, text="Неверный секрет агента")

    agent_id = request.headers.get("X-Eva-Agent-Id", "default")

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    _connected_agents[agent_id] = ws
    print(f"🔌 Агент Евы подключился: {agent_id}", flush=True)

    bot: Bot = request.app["bot"]

    # Досылаем всё, что накопилось, пока агента не было на связи
    pending = await get_pending_eva_commands()
    for cmd in pending:
        if cmd.get("action_json"):
            await push_command_to_agents(cmd["id"], cmd["action_json"])

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await _handle_agent_message(msg.data, bot)
            elif msg.type == WSMsgType.ERROR:
                print(f"⚠️ Ошибка WS-соединения с агентом {agent_id}: {ws.exception()}", flush=True)
    finally:
        _connected_agents.pop(agent_id, None)
        print(f"🔌 Агент Евы отключился: {agent_id}", flush=True)

    return ws


async def _handle_agent_message(raw: str, bot: Bot) -> None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"⚠️ Агент прислал не-JSON: {raw[:200]}", flush=True)
        return

    if data.get("type") != "result":
        return

    command_id: Optional[int] = data.get("command_id")
    status: str = data.get("status", "error")
    result: str = data.get("result", "")

    if command_id is None:
        return

    await update_eva_command_status(command_id, status, result)

    command = await get_eva_command(command_id)
    if not command:
        return

    icon = "✅" if status == "done" else "⚠️"
    try:
        await bot.send_message(
            command["requested_by"],
            f"{icon} Ева выполнила команду «{command['command_text']}»:\n{result}",
        )
    except Exception as e:
        print(f"⚠️ Не удалось уведомить о результате команды {command_id}: {e}", flush=True)
