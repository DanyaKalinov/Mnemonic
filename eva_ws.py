"""
WebSocket-мост между сервером (Ева) и desktop-агентом на ПК.

Как это работает:
1. Desktop-агент подключается к /ws/eva с заголовками X-Eva-Agent-Secret
   (секрет из .env, общий с сервером) и X-Eva-Owner-Id (Telegram ID
   владельца, которому принадлежит этот ПК — проверяется по базе: должен
   быть либо role='owner', либо иметь eva_access='granted').
2. Сразу после подключения сервер отправляет ему все команды, которые
   накопились в очереди, пока агента не было на связи (status='pending').
3. Когда владелец пишет команду боту в Telegram (или произносит голосом —
   см. eva_engine.py) — она сохраняется в БД и, если агент на связи,
   сразу же уходит ему.
4. Агент выполняет команду и присылает результат обратно
   ({"type": "result", "command_id": ..., "status": "done"|"error", "result": "..."}) —
   сервер обновляет БД и уведомляет владельца, который её запросил.
5. Агент также может прислать {"type": "voice_command", "text": "..."} —
   это транскрибированная голосовая команда (после локального wake word +
   распознавания речи на самой машине). Она проходит через ТОТ ЖЕ
   конвейер (eva_engine.process_eva_command), что и текстовые команды —
   включая обязательное подтверждение в Telegram для разрушительных
   действий, даже если распознанный голос попросил об этом напрямую.
"""
import base64
import json
from typing import Optional

from aiogram import Bot
from aiogram.types import BufferedInputFile
from aiohttp import web, WSMsgType

from config import EVA_AGENT_SECRET
from db import get_pending_eva_commands, update_eva_command_status, get_eva_command, can_use_eva
from eva_agent_registry import (
    register_agent, unregister_agent, push_command_to_agents, get_owner_id_for_agent,
)


async def eva_ws_handler(request: web.Request) -> web.WebSocketResponse:
    if not EVA_AGENT_SECRET:
        return web.Response(status=503, text="EVA_AGENT_SECRET не настроен на сервере")

    if request.headers.get("X-Eva-Agent-Secret") != EVA_AGENT_SECRET:
        return web.Response(status=403, text="Неверный секрет агента")

    owner_id_raw = request.headers.get("X-Eva-Owner-Id")
    if not owner_id_raw or not owner_id_raw.isdigit():
        return web.Response(status=403, text="Нужен корректный заголовок X-Eva-Owner-Id")
    owner_id = int(owner_id_raw)

    if not await can_use_eva(owner_id):
        return web.Response(status=403, text="У этого Telegram-аккаунта нет доступа к Еве")

    agent_id = request.headers.get("X-Eva-Agent-Id", "default")

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    register_agent(agent_id, ws, owner_id)
    print(f"🔌 Агент Евы подключился: {agent_id} (владелец {owner_id})", flush=True)

    bot: Bot = request.app["bot"]

    # Досылаем всё, что накопилось, пока агента не было на связи
    pending = await get_pending_eva_commands()
    for cmd in pending:
        if cmd.get("action_json"):
            await push_command_to_agents(cmd["id"], cmd["action_json"])

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await _handle_agent_message(msg.data, bot, agent_id, ws)
            elif msg.type == WSMsgType.ERROR:
                print(f"⚠️ Ошибка WS-соединения с агентом {agent_id}: {ws.exception()}", flush=True)
    finally:
        unregister_agent(agent_id)
        print(f"🔌 Агент Евы отключился: {agent_id}", flush=True)

    return ws


async def _handle_agent_message(raw: str, bot: Bot, agent_id: str, ws: web.WebSocketResponse) -> None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"⚠️ Агент прислал не-JSON: {raw[:200]}", flush=True)
        return

    msg_type = data.get("type")

    if msg_type == "result":
        await _handle_result_message(data, bot)
    elif msg_type == "voice_command":
        await _handle_voice_command_message(data, bot, agent_id)
    elif msg_type == "chat_result":
        from eva_engine import handle_chat_result
        await handle_chat_result(bot, data)
    elif msg_type == "confirm_request":
        from eva_engine import handle_confirm_request
        await handle_confirm_request(bot, agent_id, data)
    elif msg_type == "cloud_action":
        from eva_engine import handle_cloud_action
        await handle_cloud_action(ws, agent_id, data)
    # неизвестные типы просто игнорируем — вперёд совместимость


async def _handle_result_message(data: dict, bot: Bot) -> None:
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
    image_b64: Optional[str] = data.get("image_base64")

    try:
        if image_b64:
            image_bytes = base64.b64decode(image_b64)
            photo = BufferedInputFile(image_bytes, filename="screenshot.png")
            await bot.send_photo(
                command["requested_by"],
                photo=photo,
                caption=f"{icon} Ева выполнила команду «{command['command_text']}»",
            )
        else:
            await bot.send_message(
                command["requested_by"],
                f"{icon} Ева выполнила команду «{command['command_text']}»:\n{result}",
            )
    except Exception as e:
        print(f"⚠️ Не удалось уведомить о результате команды {command_id}: {e}", flush=True)


async def _handle_voice_command_message(data: dict, bot: Bot, agent_id: str) -> None:
    """Голосовая команда с агента (после локального wake word + STT).
    Проводим её через ТОТ ЖЕ конвейер, что и текстовые команды в Telegram —
    см. докстринг модуля про границу безопасности."""
    text = (data.get("text") or "").strip()
    if not text:
        return

    owner_id = get_owner_id_for_agent(agent_id)
    if owner_id is None:
        print(f"⚠️ Голосовая команда от неизвестного агента {agent_id}, игнорирую", flush=True)
        return

    # Импорт внутри функции — избегаем цикла на уровне модулей
    # (eva_engine.py импортирует push_command_to_agents из
    # eva_agent_registry, а не из eva_ws, так что реального цикла нет,
    # но держим импорт локально для ясности, что это специфично для
    # голосовой обработки).
    from eva_engine import process_eva_command
    from eva_agent_registry import speak_to_owner

    async def _reply(text: str) -> None:
        # Для голоса — сразу и в Telegram (для истории), и озвучиваем на ПК
        try:
            await bot.send_message(owner_id, text)
        except Exception as e:
            print(f"⚠️ Не удалось отправить голосовой ответ в Telegram: {e}", flush=True)
        await speak_to_owner(owner_id, text)

    await process_eva_command(bot, owner_id, text, _reply, source="voice")
