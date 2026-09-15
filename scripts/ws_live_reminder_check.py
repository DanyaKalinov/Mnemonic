"""Живой прогон уведомлений В ОДНОМ ПРОЦЕССЕ.

Проблема среды: процессы в изолированных сетевых пространствах — два python
(сервер и агент) не видят друг друга через 127.0.0.1. Внутри одного
asyncio-процесса loopback работает.

Поэтому здесь: поднимаем мини-WS-сервер (сравнимо с /ws/eva), затем в том же
event loop запускаем НАСТОЯЩИЙ ws_client.run_forever() — он подключается к
нашему серверу, тот шлёт {"type":"reminder",...} — и срабатывает настоящий
_handle_reminder: тост Windows + звук + запись в чат + озвучка.

Прод не затрагивается. Это полный путь «сервер → агент → уведомление ПК»,
только сервер заменён локальной мини-копией вместо Render.

Запуск (секрет и URL берутся из eva_agent/.env, URL переопределяем на себя):
    cd eva_agent && python ../brain\ bot/scripts/ws_live_reminder_check.py --port 9000
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Скрипт лежит в brain bot/scripts — поднимаемся на уровень Eva/
# и добавляем eva_agent в import-путь, чтобы импортировать ws_client/config.
EVA_AGENT_DIR = Path(__file__).resolve().parents[2] / "eva_agent"
sys.path.insert(0, str(EVA_AGENT_DIR))

# --- env ДО импорта ws_client/config ---
PORT = 0


def main() -> int:
    global PORT
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--interval", type=int, default=8, help="сек между напоминаниями")
    parser.add_argument("--text", default="Тест: вынести мусор и покормить кота")
    args = parser.parse_args()
    PORT = args.port

    from dotenv import load_dotenv
    load_dotenv(EVA_AGENT_DIR / ".env")  # читает eva_agent/.env, не перезаписывает уже заданное
    os.environ["EVA_SERVER_WS_URL"] = f"ws://127.0.0.1:{PORT}/ws/eva"

    import ws_client  # noqa: E402 — после установки env

    asyncio.run(run(args, ws_client))
    return 0


async def ws_handler(request):
    """Приём агента по протоколу /ws/eva: проверка секрета, регистрация."""
    import aiohttp
    from ws_client import config
    if request.headers.get("X-Eva-Agent-Secret") != config.agent_secret:
        return aiohttp.web.Response(status=403, text="Неверный секрет агента")
    owner = request.headers.get("X-Eva-Owner-Id", "?")
    agent_id = request.headers.get("X-Eva-Agent-Id", "default")
    ws = aiohttp.web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    _agents[agent_id] = ws
    print(f"[{ts()}] 🔌 Агент подключился: {agent_id} (владелец {owner})", flush=True)
    try:
        async for msg in ws:
            if msg.type == aiohttp.web.WSMsgType.ERROR:
                print(f"[{ts()}] ⚠️ Ошибка WS: {ws.exception()}", flush=True)
    finally:
        _agents.pop(agent_id, None)
        print(f"[{ts()}] 🔌 Агент отключился: {agent_id}", flush=True)
    return ws


_agents: dict = {}


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


async def run(args, ws_client) -> None:
    # 1) Мини-WS-сервер (протокол /ws/eva).
    from aiohttp import web
    app = web.Application()
    app.router.add_get("/ws/eva", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT)
    await site.start()
    print(f"[{ts()}] Стенд слушает ws://127.0.0.1:{PORT}/ws/eva", flush=True)

    # 2) Настоящий клиент-агент в том же event loop.
    client_task = asyncio.create_task(ws_client.run_forever())

    # 3) Рассылка напоминаний подключённым агентам.
    text = args.text
    n = 0
    try:
        while True:
            await asyncio.sleep(args.interval)
            n += 1
            payload = json.dumps({"type": "reminder", "text": f"{text} ({n})"})
            if not _agents:
                print(f"[{ts()}] ⏰ reminder готов, агентов нет на связи", flush=True)
                continue
            for agent_id, ws in list(_agents.items()):
                try:
                    await ws.send_str(payload)
                    print(f"[{ts()}] ⏰ → агент {agent_id}: {payload}", flush=True)
                except Exception as e:
                    print(f"[{ts()}] ⚠️ Не отправлено {agent_id}: {e}", flush=True)
    finally:
        client_task.cancel()
        await runner.cleanup()


if __name__ == "__main__":
    sys.exit(main())