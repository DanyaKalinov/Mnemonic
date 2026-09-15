"""Локальный WS-стенд для живого прогона уведомлений.

Повторяет протокол серверного /ws/eva ровно настолько, чтобы desktop-агент
подключился как обычно (те же заголовки), и периодически шлёт ему
напоминание {"type": "reminder", "text": ...} — на клиенте это превращается
в тост Windows + звук + запись в чат + бейдж.

Только для локального теста. Прод (Supabase, бот, webhook) не затрагивается —
стенд не ходит в базу и ничего не пишет.

⚠️ В среде Claude Code процессы изолированы по сети друг от друга (стенд и
агент в разных процессах не видят 127.0.0.1). Для живого прогона из Claude
используй ws_live_reminder_check.py (всё в одном процессе). Этот стенд
подходит, когда агент запускается вручную, вне Claude.

Запуск:
    python scripts/ws_reminder_stub.py --port 9000 --secret <EVA_AGENT_SECRET>

Агент при этом должен быть нацелен на локальный URL, например:
    EVA_SERVER_WS_URL=ws://127.0.0.1:9000/ws/eva python app.py
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime

from aiohttp import web

# Зарегистрированные агенты: agent_id -> (ws, owner_id)
_agents: dict[str, tuple[web.WebSocketResponse, str]] = {}
_next_n = 0


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    global _next_n
    secret = request.app["secret"]
    if request.headers.get("X-Eva-Agent-Secret") != secret:
        return web.Response(status=403, text="Неверный секрет агента")

    owner_id = request.headers.get("X-Eva-Owner-Id", "?")
    agent_id = request.headers.get("X-Eva-Agent-Id", "default")
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    _agents[agent_id] = (ws, owner_id)
    print(f"[{ts()}] 🔌 Агент подключился: {agent_id} (владелец {owner_id})", flush=True)

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                # Логируем, что прислал агент, чтобы видеть трассировку пути.
                print(f"[{ts()}] ← агент {agent_id}: {msg.data[:200]}", flush=True)
            elif msg.type == web.WSMsgType.ERROR:
                print(f"[{ts()}] ⚠️ Ошибка WS агента {agent_id}: {ws.exception()}", flush=True)
    finally:
        _agents.pop(agent_id, None)
        print(f"[{ts()}] 🔌 Агент отключился: {agent_id}", flush=True)
    return ws


async def reminder_loop(app: web.Application) -> None:
    """Каждые `interval` секунд шлёт reminder всем подключённым агентам."""
    global _next_n
    text = app["reminder_text"]
    interval = app["interval"]
    while True:
        await asyncio.sleep(interval)
        _next_n += 1
        payload = {"type": "reminder", "text": f"{text} ({_next_n})"}
        if not _agents:
            print(f"[{ts()}] ⏰ reminder готов, но агентов нет на связи", flush=True)
            continue
        for agent_id, (ws, _owner) in list(_agents.items()):
            try:
                await ws.send_str(json.dumps(payload))
                print(f"[{ts()}] ⏰ → агент {agent_id}: {payload}", flush=True)
            except Exception as e:
                print(f"[{ts()}] ⚠️ Не удалось отправить {agent_id}: {e}", flush=True)
                _agents.pop(agent_id, None)


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def main() -> None:
    parser = argparse.ArgumentParser(description="Локальный WS-стенд для проверки уведомлений")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--secret", required=True, help="EVA_AGENT_SECRET, общий с агентом")
    parser.add_argument("--interval", type=int, default=10, help="секунд между напоминаниями")
    parser.add_argument("--text", default="Тестовое напоминание: вынести мусор")
    args = parser.parse_args()

    app = web.Application()
    app["secret"] = args.secret
    app["interval"] = args.interval
    app["reminder_text"] = args.text
    app.router.add_get("/ws/eva", ws_handler)
    app.on_startup.append(lambda app: asyncio.create_task(reminder_loop(app)))

    print(f"[{ts()}] Стенд готов: ws://127.0.0.1:{args.port}/ws/eva, напоминание каждуе "
          f"{args.interval} с", flush=True)
    web.run_app(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()