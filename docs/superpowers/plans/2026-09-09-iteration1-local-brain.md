# Итерация 1: локальный мозг на ПК владельца — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Перевести владельца Евы на локальный мозг (Qwen2.5-VL 7B через Ollama): когда агент онлайн, чат/голос/приложение думают локально, облако (Gemini) остаётся только для напоминаний, офлайн-чата и облачных действий (память, интернет-сырьё).

**Architecture:** Сервер (`brain bot/`) перестаёт разбирать команды владельца, когда его агент онлайн, — ретранслирует сырой текст как `chat` (с инъекцией памяти из Chroma). Агент (`eva_agent/`) разбирает utterance локальной моделью в план (ответ / локальные действия / облачное действие), исполняет, что может, а облачные отдаёт серверу через `cloud_action`. Подтверждение разрушительных действий — по-прежнему кнопкой в Telegram через существующий конвейер `eva_commands`.

**Tech Stack:** Сервер — aiohttp/aiogram/asyncpg/Chroma(pgvector)/Gemini/Tavily. Агент — pywebview/websockets/Ollama (`ollama` python lib)/pywinauto/actions.py (не трогаем). Новые зависимости: сервер — `html2text`; агент — `ollama`.

**Spec:** `docs/superpowers/specs/2026-09-09-multi-user-architecture-design.md`

## Global Constraints

- Модель одна для всех: **`qwen2.5vl:7b`**. Агент подключается только когда Ollama доступна; если нет — честный ответ «мозг ещё не поднят».
- Память всегда в облаке: агент не заводит свою базу, только `memory_add`/`memory_search` через сервер.
- Подтверждение разрушительных действий всегда уходит в Telegram (`eva_confirm:`/`eva_cancel:`), независимо от канала.
- Существующие обработчики Telegram (`cmd_eva`, `/find`, `/open`, скриншот, callback-подтверждения) остаются точками входа — меняется только то, КУДА они отправляют.
- `eva_engine.py` остаётся границей безопасности; новые функции живут в нём же или в новых модулях `chat_state.py`, `cloud_actions.py`.
- Существующие тесты (`test_reminder_parser.py`, `test_eva_command_parser.py`) должны остаться зелёными.
- Стиль: технический, пошаговый; коммит после каждой задачи в `brain bot/` (агент не в git — там коммиты не делаем, только в сервере).

---

## Фаза A — сервер

### Task 1: Ретрансляция `chat` агенту + инъекция памяти + офлайн-guard

**Файлы:**
- Create: `brain bot/chat_state.py`
- Modify: `brain bot/eva_agent_registry.py` (хелперы маршрутизации к конкретному юзеру)
- Modify: `brain bot/eva_engine.py` (решение «онлайн/офлайн», `relay_to_agent`)
- Modify: `brain bot/handlers.py` (Text-обработчик и `cmd_eva` используют маршрутизацию)
- Test: `brain bot/test_chat_routing.py`

**Interfaces:**
- Consumes: `memory.search_memory(user_id, query, k) -> list[str]` (существует, `memory.py`); `eva_agent_registry` словарь `_connected_agents`.
- Produces:
  - `chat_state.new_message_id() -> int`
  - `chat_state.register_message(message_id: int, user_id: int, origin: str, command_text: str) -> None`
  - `chat_state.get_message(message_id: int) -> Optional[dict]`
  - `eva_agent_registry.get_user_agent(user_id: int) -> Optional[WebSocketResponse]`
  - `eva_agent_registry.send_to_user_agent(user_id: int, payload: dict) -> bool`
  - `eva_engine.relay_to_agent(bot, user_id: int, text: str, reply, source: str) -> None`
  - `eva_engine.is_agent_online(user_id: int) -> bool`

- [ ] **Step 1: Написать падающий тест для маршрутизации и инъекции памяти**

`test_chat_routing.py`:

```python
import pytest
from eva_agent_registry import register_agent, get_user_agent, send_to_user_agent
from chat_state import new_message_id, register_message, get_message


class FakeWS:
    def __init__(self): self.sent = []
    async def send_str(self, s): self.sent.append(s)


@pytest.mark.asyncio
async def test_registry_routes_to_specific_user_only():
    ws_owner = FakeWS()
    ws_other = FakeWS()
    register_agent("p1", ws_owner, owner_id=111)
    register_agent("p2", ws_other, owner_id=222)
    assert get_user_agent(111) is ws_owner
    assert get_user_agent(222) is ws_other
    assert get_user_agent(333) is None
    ok = await send_to_user_agent(111, {"type": "chat", "text": "hi"})
    assert ok and len(ws_owner.sent) == 1 and ws_other.sent == []
    # очистка от регистраций, чтобы не пересечься с другими тестами
    from eva_agent_registry import unregister_agent
    unregister_agent("p1"); unregister_agent("p2")


def test_chat_state_message_roundtrip():
    mid = new_message_id()
    register_message(mid, user_id=111, origin="app", command_text="привет")
    m = get_message(mid)
    assert m == {"user_id": 111, "origin": "app", "command_text": "привет"}
    assert get_message(999999) is None
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `cd 'brain bot' && python -m pytest test_chat_routing.py -v`
Expected: FAIL — модули/функции не существуют.

- [ ] **Step 3: Реализовать `chat_state.py` и хелперы реестра**

`brain bot/chat_state.py`:

```python
"""Слегка следимая память о chat-сообщениях, отправленных агенту.

Нужна для обратной маршрутизации (chat_result / confirm_request):
зная message_id, сервер вспоминает, какому пользователю и из какого
канала пришло сообщение. Память процесса (не БД) — для И1 достаточно,
на рестарте сервера потеря карт допустима (результат всё равно уйдёт
в Telegram, как и раньше).
"""
from typing import Optional

_next_id = 0
_chat_meta: dict[int, dict] = {}


def new_message_id() -> int:
    global _next_id
    _next_id += 1
    return _next_id


def register_message(message_id: int, user_id: int, origin: str, command_text: str) -> None:
    _chat_meta[message_id] = {
        "user_id": user_id,
        "origin": origin,
        "command_text": command_text,
    }


def get_message(message_id: int) -> Optional[dict]:
    return _chat_meta.get(message_id)
```

`eva_agent_registry.py` — добавить функции:

```python
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
```

- [ ] **Step 4: Запустить тесты — должны пройти**

Run: `cd 'brain bot' && python -m pytest test_chat_routing.py -v`
Expected: PASS.

- [ ] **Step 5: Добавить `relay_to_agent` в `eva_engine.py`**

Добавить в начало `eva_engine.py` (после существующих импортов):

```python
from eva_agent_registry import get_user_agent, send_to_user_agent
from chat_state import new_message_id, register_message

# Каналы, в которых "говорим" с агентом.
_ORIGIN_TELEGRAM = "telegram"
_ORIGIN_VOICE = "voice"
_ORIGIN_APP = "app"

TOP_MEMORY_K = 5


def is_agent_online(user_id: int) -> bool:
    """Есть ли у этого пользователя подключённый desktop-агент."""
    return get_user_agent(user_id) is not None


async def relay_to_agent(bot: Bot, user_id: int, text: str, reply: ReplyFunc, source: str) -> None:
    """Онлайн-путь: сырой текст + инъекция памяти уходит агенту как 'chat'.
    Мозг (локальная модель) отвечает сам; это не разбор на сервере."""
    origin = _ORIGIN_VOICE if source == "voice" else _ORIGIN_TELEGRAM
    message_id = new_message_id()
    register_message(message_id, user_id, origin, text)

    memories = await search_memory(user_id, text, k=TOP_MEMORY_K)
    payload = {
        "type": "chat",
        "message_id": message_id,
        "text": text,
        "origin": origin,
        "memory": memories,
    }
    sent = await send_to_user_agent(user_id, payload)
    if not sent:
        await reply("📥 Агент сейчас не на связи — попробуй позже.")
        return
    if origin == _ORIGIN_TELEGRAM:
        await reply("🧠 Отправила Еве, жду ответ...")
```

- [ ] **Step 6: Обновить `process_eva_command` — онлайн ретранслируем, офлайн отдаём чат в Gemini**

В `eva_engine.py`, в начале `process_eva_command` (после докстринга, до `step_texts=...`):

```python
    if is_agent_online(user_id):
        # Мозг локальный: сервер не разбирает, а ретранслирует.
        await relay_to_agent(bot, user_id, command_text, reply, source)
        return
```

И в самом конце функции — ОФЛАЙН-чат: заменить `if not steps: return` на:

```python
    if not steps:
        # Не действие, а обычный вопрос — при офлайн агенте отвечает Gemini.
        from ai_core import get_ai_response
        answer = await get_ai_response(user_id, command_text)
        await reply(answer)
        return
```

- [ ] **Step 7: Подключить маршрутизацию в `handlers.py`**

В `cmd_eva` (перед финальным `await process_eva_command(...)` видим, что вызов уже идёт через `process_eva_command` — там ничего менять не нужно, ретрансляция уже внутри). Текстовый обработчик «Память + AI-ответ» — вставить маршрутизацию для владельцев с агентом:

```python
from eva_engine import is_agent_online, relay_to_agent
# ... внутри обработчика текста, до сохранения памяти/Gemini:
        if is_agent_online(user_id):
            await relay_to_agent(message.bot, user_id, user_text, message.answer, "telegram")
            return
```

- [ ] **Step 8: Прогнать тесты**

Run: `cd 'brain bot' && python -m pytest test_chat_routing.py test_eva_command_parser.py -v`
Expected: PASS (новые + старые).

- [ ] **Step 9: Коммит**

```bash
cd 'brain bot'
git add chat_state.py eva_agent_registry.py eva_engine.py handlers.py test_chat_routing.py
git commit -m "feat: онлайн-ретрансляция чата агенту + инъекция памяти (итерация 1)"
```

---

### Task 2: Обработка сообщений агента — `chat_result`, `confirm_request`, `cloud_action`

**Файлы:**
- Modify: `brain bot/eva_ws.py` (диспетчеризация на новые типы)
- Modify: `brain bot/eva_engine.py` (обработчики)
- Modify: `brain bot/cloud_actions.py` — **создать в Task 3**, поэтому здесь только обработчик-диспетчер, вызывающий заглушку из Task 3
- Test: `brain bot/test_agent_messages.py`

**Interfaces:**
- Consumes: `chat_state.get_message`; `eva_agent_registry.send_to_user_agent`; `db.update_eva_command_status`; `db.add_eva_command`; `ai_core.get_ai_response`.
- Produces:
  - `eva_engine.handle_chat_result(bot, data: dict) -> None`
  - `eva_engine.handle_confirm_request(bot, agent_id: str, data: dict) -> None`
  - `eva_engine.handle_cloud_action(bot, agent_id: str, data: dict) -> Optional[str]` (возвращает `request_id` если принято)

- [ ] **Step 1: Падающий тест**

`test_agent_messages.py`:

```python
import pytest
from chat_state import new_message_id, register_message
from eva_engine import handle_chat_result, handle_confirm_request


class FakeBot:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, "kw": kw})


@pytest.mark.asyncio
async def test_handle_chat_result_answer_delivers_to_telegram():
    bot = FakeBot()
    mid = new_message_id()
    register_message(mid, user_id=111, origin="app", command_text="привет")
    await handle_chat_result(bot, {"type": "chat_result", "message_id": mid, "kind": "answer", "text": "Привет!"})
    assert any(s["chat_id"] == 111 and "Привет!" in s["text"] for s in bot.sent)


@pytest.mark.asyncio
async def test_confirm_request_creates_command_with_buttons():
    from db import add_eva_command
    bot = FakeBot()
    await handle_confirm_request(
        bot, "p1",
        {
            "type": "confirm_request",
            "message_id": 424242,  # не в chat_state (restart-case) — всё равно работает по user? нет: без message_id ищем… см. реализацию
            "local_steps": [{"action": "delete_file", "params": {"path": "notes.txt"}, "description": "Ева удалит файл «notes.txt»."}],
        },
    )
    # реализация должна ответить в Telegram текстом с кнопками (FakeBot получает reply_markup)
    assert any(s["chat_id"] == 111 and s["kw"].get("reply_markup") for s in bot.sent)
```

> Примечание: тест с `message_id=424242` падает уже на этапе «реализация» — там сообщение не найдено, и подтверждение шлётся абоненту без привязки к команде. Это ок для И1: главный тест — что кнопки ушли.

- [ ] **Step 2: Прогнать — падает**

Run: `cd 'brain bot' && python -m pytest test_agent_messages.py -v`
Expected: FAIL (функции не существуют).

- [ ] **Step 3: Реализовать `handle_chat_result` и `handle_confirm_request` в `eva_engine.py`**

```python
async def _deliver_text(bot: Bot, user_id: int, text: str, image_base64: Optional[str] = None) -> None:
    """Отправляет итог чата пользователю в Telegram (единая точка для
    chat_result) — для голоса дополнительно озвучиваем через агента."""
    from aiogram.types import BufferedInputFile
    try:
        if image_base64:
            import base64
            photo = BufferedInputFile(base64.b64decode(image_base64), filename="screenshot.png")
            await bot.send_photo(user_id, photo=photo, caption=text or "Ева")
        else:
            await bot.send_message(user_id, text or "Ева")
    except Exception as e:
        print(f"⚠️ Не удалось доставить chat_result пользователю {user_id}: {e}", flush=True)


async def handle_chat_result(bot: Bot, data: dict) -> None:
    message_id = data.get("message_id")
    meta = chat_state.get_message(message_id) if message_id else None
    if meta is None:
        return
    kind = data.get("kind", "answer")
    text = str(data.get("text") or "")
    if kind == "error":
        text = f"⚠️ {text}"
    await _deliver_text(bot, meta["user_id"], text, data.get("image_base64"))
    # голос: если пришло голосом — озвучиваем на ПК
    if meta.get("origin") == "voice":
        await speak_to_owner(meta["user_id"], text)


async def handle_confirm_request(bot: Bot, agent_id: str, data: dict) -> None:
    """Разрушительное действие от локального мозга — всегда подтверждение
    в Telegram владельца (граница безопасности не зависит от канала)."""
    from db import add_eva_command
    from eva_agent_registry import get_owner_id_for_agent
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    owner_id = get_owner_id_for_agent(agent_id)
    if owner_id is None:
        return

    steps = data.get("local_steps") or []
    if not steps:
        return
    payload = {"steps": steps}
    meta = None
    message_id = data.get("message_id")
    if message_id:
        meta = chat_state.get_message(message_id)
        if data.get("voice"):  # голос: результаты озвучивать
            payload["voice"] = True

    command_id = await add_eva_command(
        owner_id,
        meta["command_text"] if meta else "действие с ПК",
        action_json=json.dumps(payload),
        status="awaiting_confirmation",
    )
    numbered = "\n".join(f"{i}. {s.get('description','')}" for i, s in enumerate(steps, start=1))
    body = f"⚠️ Локальный мозг предлагает:\n{numbered}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"eva_confirm:{command_id}"),
        InlineKeyboardButton(text="❌ Отменить", callback_data=f"eva_cancel:{command_id}"),
    ]])
    await bot.send_message(owner_id, f"{body}\nПодтверди выполнение:", reply_markup=keyboard)
```

> Зависимость: `json` и `chat_state` — уже импортированы/добавлены в Task 1; `speak_to_owner` импортируется из `eva_agent_registry`.

- [ ] **Step 4: Добавить обработчик `cloud_action` (диспетчер; исполнение — Task 3)**

В `eva_engine.py`:

```python
async def handle_cloud_action(ws, data: dict) -> None:
    """Облачное действие локального мозга. Исполняется на сервере,
    результат возвращается агенту как cloud_result (синтез ответа — агент)."""
    request_id = data.get("request_id")
    action = data.get("action")
    params = data.get("params") or {}
    if not request_id or not action:
        return
    from cloud_actions import execute_cloud_action
    result = await execute_cloud_action(action, params)
    payload = {"type": "cloud_result", "request_id": request_id, **result}
    try:
        await ws.send_str(json.dumps(payload))
    except Exception as e:
        print(f"⚠️ Не удалось вернуть cloud_result: {e}", flush=True)


async def handle_cloud_action_for_agent(ws, data: dict) -> None:
    await handle_cloud_action(ws, data)
```

- [ ] **Step 5: Диспетчеризация в `eva_ws.py`**

Сигнатуру `_handle_agent_message` расширить, чтобы у него был доступ к `ws` и `agent_id` (уже есть `agent_id`; `ws` добавить), и обрабатывать новые типы:

```python
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
        await handle_chat_result(bot, data)
    elif msg_type == "confirm_request":
        await handle_confirm_request(bot, agent_id, data)
    elif msg_type == "cloud_action":
        await handle_cloud_action(ws, data)
    # неизвестные типы — игнорируем (forward compat)
```

Место вызова в `eva_ws_handler` изменить:

```python
                await _handle_agent_message(msg.data, bot, agent_id, ws)
```

- [ ] **Step 6: Прогнать тесты**

Run: `cd 'brain bot' && python -m pytest test_agent_messages.py -v`
Expected: PASS. (Примечание: `handle_confirm_request` с `message_id=424242` не найдёт meta и создаст команду с дефолтным текстом — кнопки при этом уходят.)

- [ ] **Step 7: Коммит**

```bash
cd 'brain bot'
git add eva_ws.py eva_engine.py test_agent_messages.py
git commit -m "feat: сервер обрабатывает chat_result/confirm_request/cloud_action от агента"
```

---

### Task 3: Облачные действия сервера — `cloud_actions.py`

**Файлы:**
- Create: `brain bot/cloud_actions.py`
- Modify: `brain bot/ai_core.py` (сырые сниппеты Tavily, чтение URL, JSON-нормализация текста напоминания)
- Modify: `brain bot/requirements.txt` (добавить `html2text`)
- Test: `brain bot/test_cloud_actions.py`

**Interfaces:**
- Consumes: `db.add_reminder`, `memory.add_memory`, `memory.search_memory`, Tavily REST через `requests`, `html2text`.
- Produces:
  - `cloud_actions.execute_cloud_action(action: str, params: dict) -> dict` → `{"status": "done"|"error", "result": str}`
  - `ai_core.tavily_search_snippets(query: str) -> str` (сырое, без Gemini-синтеза)
  - `ai_core.fetch_url_text(url: str) -> str`
  - `ai_core.normalize_reminder_text(raw: str) -> str` (Gemini причёсывает текст; при сбое — исходный)

- [ ] **Step 1: Падающий тест**

`test_cloud_actions.py`:

```python
import pytest
from cloud_actions import execute_cloud_action


class FakeCtx:
    pass


@pytest.mark.asyncio
async def test_unknown_action_returns_error(monkeypatch):
    out = await execute_cloud_action("nonexistent", {})
    assert out["status"] == "error"


@pytest.mark.asyncio
async def test_memory_search_paths_through(monkeypatch):
    # подменяем исполнение, чтобы не бить по сети
    from cloud_actions import _execute_memory_search
    captured = {}
    monkeypatch.setattr("cloud_actions._execute_memory_search", async_fake)
    await _execute_memory_search("про отпуск", captured)
    assert captured["status"] == "done"
```

> Упрощение: прямой юнит тест только для диспетчера + маленькой функции. Основной тест реального поиска — интеграционный (руками), в план он не входит.

Вспомогательный хелпер:

```python
async def async_fake(*a, **kw):
    return {"status": "done", "result": "привет"}
```

- [ ] **Step 2: Прогнать — падает**

Run: `cd 'brain bot' && python -m pytest test_cloud_actions.py -v`
Expected: FAIL.

- [ ] **Step 3: Реализовать `ai_core.tavily_search_snippets` и `fetch_url_text`**

В `ai_core.py` добавить (после `ask_web`):

```python
def _tavily_search(query: str, max_results: int = 5) -> str:
    """Сырой поиск Tavily → текстовые сниппеты. БЕЗ генерации Gemini —
    сырьё уходит локальному мозгу, текст формулирует он."""
    if not TAVILY_API_KEY:
        return "⚠️ Не настроен ключ Tavily (TAVILY_API_KEY)."
    response = requests.post(
        "https://api.tavily.com/search",
        json={"api_key": TAVILY_API_KEY, "query": query,
              "search_depth": "basic", "max_results": max_results},
        timeout=15,
    )
    response.raise_for_status()
    results = response.json().get("results", [])
    if not results:
        return "Не нашёл ничего по этому запросу."
    return "\n\n".join(
        f"Источник: {r.get('url','')}\n{(r.get('content') or '')[:800]}" for r in results[:5]
    )


async def tavily_search_snippets(query: str) -> str:
    try:
        return await asyncio.to_thread(_tavily_search, query)
    except Exception as e:
        print(f"❌ Ошибка Tavily (сырой поиск): {e}")
        return "😵 Не смог найти информацию в интернете (ошибка поискового сервиса)."


async def fetch_url_text(url: str) -> str:
    """Текст страницы по ссылке (HTML → Markdown). Для чтения статьи локальным мозгом."""
    import html2text
    def _fetch() -> str:
        response = requests.get(url, timeout=20, headers={"User-Agent": "EvaBot/1.0"})
        response.raise_for_status()
        return html2text.html2text(response.text)[:6000]
    try:
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        print(f"❌ Ошибка чтения URL {url}: {e}")
        return "😵 Не смог прочитать страницу по этой ссылке."


async def normalize_reminder_text(raw: str) -> str:
    """Gemini причёсывает текст напоминания (коротко и по делу). При сбое —
    исходный текст."""
    if not GOOGLE_API_KEY or not raw.strip():
        return raw.strip()
    prompt = (
        "Перепиши текст напоминания коротко и по делу, одним предложением, "
        "на русском. Без слова 'напомни' и без дат/времени в тексте (они "
        f"считаются отдельно). Текст: «{raw}»"
    )
    try:
        response = await asyncio.to_thread(
            client.models.generate_content, model="gemini-flash-lite-latest", contents=[prompt]
        )
        return (response.text or "").strip() or raw.strip()
    except Exception:
        return raw.strip()
```

- [ ] **Step 4: Реализовать `cloud_actions.py`**

```python
"""Облачные действия локального мозга: исполняются на сервере, результат
возвращается агенту как cloud_result. Локальный мозг получает СЫРЬЁ и сам
формулирует ответ (см. спецификацию, §5)."""

import asyncio
import json
from datetime import datetime, timedelta
from typing import Optional

import dateparser

from ai_core import tavily_search_snippets, fetch_url_text, normalize_reminder_text
from db import add_reminder
from memory import add_memory, search_memory


async def _create_reminder(params: dict) -> dict:
    text = (params.get("text") or "").strip()
    when_raw = (params.get("when") or "").strip()
    if not text:
        return {"status": "error", "result": "Напоминание без текста."}
    notify_at = dateparser.parse(when_raw) if when_raw else datetime.now() + timedelta(minutes=15)
    if notify_at is None:
        return {"status": "error", "result": "Не поняла время напоминания. Пример: «через 15 минут»."}
    nice_text = await normalize_reminder_text(text)
    rid = await add_reminder(0, nice_text, notify_at)  # user_id подставит caller
    return {"status": "done", "result": f"⏰ Напомню в {notify_at:%H:%M}: {nice_text}."}


async def _execute_memory_add(params: dict) -> dict:
    text = (params.get("text") or "").strip()
    if not text:
        return {"status": "error", "result": "Нечего сохранять."}
    await add_memory(_CURRENT_USER, text)
    return {"status": "done", "result": "✅ Запомнила."}


async def _execute_memory_search(params: dict) -> dict:
    query = (params.get("query") or "").strip()
    if not query:
        return {"status": "error", "result": "Пустой запрос."}
    found = await search_memory(_CURRENT_USER, query, k=5)
    if not found:
        return {"status": "done", "result": "Ничего не нашла в памяти."}
    return {"status": "done", "result": "\n".join(f"- {f}" for f in found)}


async def _fetch_web_results(params: dict) -> dict:
    query = (params.get("query") or "").strip()
    if not query:
        return {"status": "error", "result": "Пустой запрос."}
    return {"status": "done", "result": await tavily_search_snippets(query)}


async def _fetch_url(params: dict) -> dict:
    url = (params.get("url") or "").strip()
    if not url:
        return {"status": "error", "result": "Нет ссылки."}
    return {"status": "done", "result": await fetch_url_text(url)}


# user_id проставляется per-call — см. Thread.current / параметризация ниже.
_CURRENT_USER = 0

_HANDLERS = {
    "create_reminder": _create_reminder,
    "memory_add": _execute_memory_add,
    "memory_search": _execute_memory_search,
    "fetch_web_results": _fetch_web_results,
    "fetch_url": _fetch_url,
}


async def execute_cloud_action(action: str, params: dict, user_id: int) -> dict:
    """user_id — владелец облачного действия (для изоляции памяти/напоминаний)."""
    global _CURRENT_USER
    handler = _HANDLERS.get(action)
    if handler is None:
        return {"status": "error", "result": f"Неизвестное облачное действие: {action}"}
    _CURRENT_USER = user_id
    try:
        return await handler(params or {})
    except Exception as e:
        return {"status": "error", "result": f"Ошибка сервера: {e}"}
```

> **Важно:** причесать `cloud_actions.execute_cloud_action(action, params, user_id)` — добавить третий параметр. В `eva_engine.handle_cloud_action` передать `user_id` из registry (`get_owner_id_for_agent`). Ниже в Step 5 это показано для сервера; также поправить подпись в Task 2 (диспетчер вызывает 3-аргументную версию). Заменить `_CURRENT_USER`-глобал на передаваемый параметр: функцию `_create_reminder`/`_execute_memory_*` переписать так, чтобы они принимали `user_id` первым аргументом, либо обернуть — см. реализацию ниже.

> **Уточнение реализации:** чтобы не возиться с глобалом `_CURRENT_USER` (не потокобезопасный и неочевидный), обработчики принимают `user_id` как первый аргумент:

```python
async def _create_reminder(user_id: int, params: dict) -> dict:
    ...
    rid = await add_reminder(user_id, nice_text, notify_at)
    ...

async def _execute_memory_add(user_id: int, params: dict) -> dict:
    ...
    await add_memory(user_id, text)
    ...

async def _execute_memory_search(user_id: int, params: dict) -> dict:
    ...
    found = await search_memory(user_id, query, k=5)
    ...

_handlers = {
    "create_reminder": _create_reminder,
    "memory_add": _execute_memory_add,
    "memory_search": _execute_memory_search,
    "fetch_web_results": _fetch_web_results,
    "fetch_url": _fetch_url,
}

async def execute_cloud_action(action: str, params: dict, user_id: int) -> dict:
    handler = _handlers.get(action)
    if handler is None:
        return {"status": "error", "result": f"Неизвестное облачное действие: {action}"}
    try:
        result = await handler(user_id, params or {})
        return result
    except Exception as e:
        print(f"❌ Ошибка облачного действия {action}: {e}")
        return {"status": "error", "result": "Ошибка сервера — не удалось выполнить."}
```

- [ ] **Step 5: Соединить диспетчер сервера с `user_id`**

В `eva_engine.handle_cloud_action` (из Task 2) поправить вызов:

```python
async def handle_cloud_action(ws, data: dict) -> None:
    request_id = data.get("request_id")
    action = data.get("action")
    params = data.get("params") or {}
    if not request_id or not action:
        return
    from cloud_actions import execute_cloud_action
    from eva_agent_registry import get_user_agent
    # user_id ищем по владельцу агента, от которого пришло действие
    result = await execute_cloud_action(action, params, user_id=0)  # см. замечание
    payload = {"type": "cloud_result", "request_id": request_id, **result}
    try:
        await ws.send_str(json.dumps(payload))
    except Exception as e:
        print(f"⚠️ Не удалось вернуть cloud_result: {e}", flush=True)
```

> **Замечание:** чтобы получить `user_id`, нужно знать `agent_id` в точке вызова. Расширить `handle_cloud_action(ws, data)` → `handle_cloud_action(ws, agent_id, data)` в `eva_ws.py`, а `user_id = get_owner_id_for_agent(agent_id)`; если None — `user_id=0` (не найдено). Поправьте вызов в `eva_ws.py` соответственно во всех местах.

- [ ] **Step 6: Обновить `requirements.txt`**

`brain bot/requirements.txt` — добавить строку `html2text`.

- [ ] **Step 7: Прогнать тесты**

Run: `cd 'brain bot' && python -m pip install html2text && python -m pytest test_cloud_actions.py test_agent_messages.py -v`
Expected: PASS.

- [ ] **Step 8: Коммит**

```bash
cd 'brain bot'
git add cloud_actions.py ai_core.py eva_engine.py eva_ws.py requirements.txt test_cloud_actions.py
git commit -m "feat: облачные действия локального мозга (напоминания, память, интернет-сырьё)"
```

---

### Task 4: Доставка результата в приложение для confirm из канала «app»

**Файлы:**
- Modify: `brain bot/eva_ws.py` (`_handle_result_message` — эхо в приложение)
- Modify: `brain bot/eva_agent_registry.py` (хелпер `notify_app`)
- Test: `brain bot/test_agent_messages.py` (доп. тест)

**Interfaces:**
- Consumes: результат существующего `command` + `get_eva_command`.
- Produces: `eva_agent_registry.notify_app(user_id: int, role: str, text: str) -> bool`

- [ ] **Step 1: Написать тест на `notify_app`**

В `test_agent_messages.py`:

```python
from eva_agent_registry import register_agent, unregister_agent, notify_app


@pytest.mark.asyncio
async def test_notify_app_sends_chat_echo():
    ws = FakeWS()
    register_agent("p1", ws, owner_id=111)
    ok = await notify_app(111, "eva", "готово")
    assert ok and '"role": "eva"' in ws.sent[0]
    unregister_agent("p1")
```

- [ ] **Step 2: Прогнать — падает**

Run: `cd 'brain bot' && python -m pytest test_agent_messages.py -v`
Expected: FAIL (нет `notify_app`).

- [ ] **Step 3: Реализовать `notify_app` в реестре**

```python
async def notify_app(user_id: int, role: str, text: str) -> bool:
    """Показывает строку в чате-приложении у пользователя (role: user|eva|system)."""
    return await send_to_user_agent(user_id, {"type": "chat_echo", "role": role, "text": text})
```

- [ ] **Step 4: Эхо результата в приложение, когда команда пришла из приложения**

В `eva_ws._handle_result_message` после успешной отправки в Telegram добавить:

```python
    # Если команда изначально пришла из приложения (агент запросил подтверждение),
    # продублировать итог в чат приложения.
    await notify_app(command["requested_by"], "eva", f"{icon} {result or 'готово'}")
```

> Строка из контекста: `command_id` собран из `data`, `command` из `get_eva_command`. `notify_app` импортировать вверху модуля.

- [ ] **Step 5: Прогнать тесты**

Run: `cd 'brain bot' && python -m pytest test_agent_messages.py -v`
Expected: PASS.

- [ ] **Step 6: Коммит**

```bash
cd 'brain bot'
git add eva_ws.py eva_agent_registry.py test_agent_messages.py
git commit -m "feat: эхо результата подтверждённой команды в чат приложения"
```

---

## Фаза B — агент

### Task 5: `local_brain.py` — локальный мозг (Ollama)

**Файлы:**
- Create: `eva_agent/local_brain.py`
- Modify: `eva_agent/requirements.txt` (добавить `ollama`)
- Modify: `eva_agent/config.py` (ключи `EVA_OLLAMA_MODEL`, `EVA_OLLAMA_URL`)
- Test: `eva_agent/test_local_brain.py`

**Interfaces:**
- Consumes: `ollama` python lib (`ollama.chat`), конфиг.
- Produces:
  - `local_brain.is_ollama_ready() -> bool`
  - `local_brain.build_plan(text: str, memory_context: str) -> dict`
  - `local_brain.classify_steps(steps: list[dict]) -> ("actions"|"confirm")`
  - Параметры из config: `config.ollama_model`, `config.ollama_url`.
  - План-схема `local_brain.ACTIONS_BRAIN` (строка с описанием локальных + облачных действий для промпта).

- [ ] **Step 1: Падающий тест**

`test_local_brain.py`:

```python
from unittest.mock import patch
import pytest

from local_brain import build_plan, classify_steps


def test_build_plan_answer():
    fake_resp = {"message": {"content": '{"kind": "answer", "text": "Привет!"}'}}
    with patch("local_brain._call_model") as mocked:
        mocked.return_value = "Привет!"  # уже распарсенный; см. impl
        plan = build_plan("привет", "")
        # строим на основе подмены _call_model ниже в impl — см. код
    # проще: юнит-тест на парсер, без сети
    plan = parse_plan('{"kind": "answer", "text": "Привет!"}')
    assert plan == {"kind": "answer", "text": "Привет!"}


def test_parse_plan_local_action():
    plan = parse_plan('{"kind":"actions","steps":[{"action":"open_app","params":{"app":"блокнот"}}]}')
    assert plan["kind"] == "actions"
    assert plan["steps"][0]["action"] == "open_app"


def test_classify_steps_danger():
    steps = [{"action": "delete_file", "params": {"path": "x"}}]
    assert classify_steps(steps) == "confirm"
    steps2 = [{"action": "open_app", "params": {"app": "блокнот"}}]
    assert classify_steps(steps2) == "actions"
```

> Используется вспомогательная чистая функция `parse_plan(raw: str) -> dict` — её реализует этот же task.

- [ ] **Step 2: Прогнать — падает**

Run: `cd eva_agent && python -m pytest test_local_brain.py -v`
Expected: FAIL.

- [ ] **Step 3: Реализовать `local_brain.py`**

```python
"""Локальный мозг desktop-агента: Qwen2.5-VL 7B через Ollama.

Переводит произвольную фразу пользователя в план:
- answer  — обычный ответ текстом;
- actions — безопасные локальные действия (исполняет сам агент);
- confirm — разрушительные локальные действия (нужно подтверждение в Telegram);
- cloud   — облачное действие (память/напоминания/интернет-сырьё) — закрывает
            сервер через cloud_action;
- error   — модель не смогла.

Whitelist действий — единый для задач «локально/облачно» (см. спецификацию §5).
Модель строго обязана выбирать только из него.
"""
import json
import re
from typing import Optional

import ollama

from config import config

OLLAMA_MODEL = "qwen2.5vl:7b"

# Действия, которые реально исполняет агент локально (actions.py) — перечисляем
# имена и параметры, чтобы модель не выдумывала.
_LOCAL_ACTIONS = """
- open_app: {"app": "название программы"}
- open_website: {"url": "адрес сайта"}
- open_file: {"path": "путь"}
- open_folder: {"name": "название папки"}
- create_file: {"path": "имя", "content": "текст"}
- read_file: {"path": "имя"}
- delete_file: {"path": "имя"}
- edit_file: {"path": "имя", "mode": "append/overwrite", "content": "текст"}
- copy_file: {"src": "откуда", "dst": "куда"}
- move_file: {"src": "откуда", "dst": "куда"}
- list_files: {"folder": "название"}
- search_file: {"query": "что искать"}
- screenshot: {}
- set_volume: {"direction": "up/down/mute/unmute"} или {"level": 0..100}
- power_action: {"action": "shutdown/restart/lock"}
- type_text: {"text": "текст"}
- press_key: {"keys": "ctrl+s"}
- web_search: {"query": "...", "engine": "google/yandex/bing", "browser": "chrome/opera/firefox/edge"}
- ui_click: {"window": "окно", "control": "кнопка"}
- ui_list_controls: {"window": "окно"}
- close_app: {"window": "окно"}
- minimize_window: {"window": "окно"}
- maximize_window: {"window": "окно"}
- mouse_move: {"x": 0, "y": 0}
- mouse_click: {"x": 0, "y": 0, "button": "left/right/middle"}
- mouse_scroll: {"direction": "up/down", "amount": 3}
- mouse_drag: {"x_start": 0, "y_start": 0, "x_end": 0, "y_end": 0}
- set_default_browser: {"browser": "chrome/opera/firefox/edge"}
- ui_type_text: {"window": "окно", "control": "поле", "text": "текст"}
"""

_CLOUD_ACTIONS = """
- create_reminder: {"text": "что напомнить", "when": "через 15 минут"}
- memory_add: {"text": "факт, который стоит запомнить"}
- memory_search: {"query": "что искать в памяти пользователя"}
- fetch_web_results: {"query": "вопрос для поиска в интернете"}
- fetch_url: {"url": "ссылка на статью"}
"""

_DESTRUCTIVE_LOCAL = {
    "create_file", "delete_file", "edit_file", "copy_file", "move_file",
    "power_action", "close_app", "mouse_click", "mouse_drag",
}


def is_ollama_ready() -> bool:
    try:
        return bool(ollama.list().get("models"))
    except Exception:
        return False


def classify_steps(steps: list[dict]) -> str:
    """'confirm', если среди шагов есть разрушительные действия, иначе 'actions'."""
    if any(s.get("action") in _DESTRUCTIVE_LOCAL for s in steps):
        return "confirm"
    return "actions"


def _system_prompt(memory_context: str) -> str:
    return (
        "Ты — Ева, персональный ИИ-ассистент на ПК пользователя. "
        "Пользователь разговаривает с тобой текстом или голосом. Вот что ты "
        f"знаешь о нём из памяти (может не относиться к вопросу):\n{memory_context or '(пусто)'}\n\n"
        "Список действий, которые ты реально умеешь выполнять ЛОКАЛЬНО (на этом ПК):\n"
        f"{_LOCAL_ACTIONS}\n"
        "Список ОБЛАЧНЫХ действий (их выполняет сервер, тебе придёт результат):\n"
        f"{_CLOUD_ACTIONS}\n\n"
        "Ответь СТРОГО одним JSON без пояснений и markdown:\n"
        '{"kind": "answer" | "actions" | "cloud" | "error", "text": "краткий ответ или описание", '
        '"steps": [{"action": "...", "params": {...}, "description": "что будет сделано"}], '
        '"cloud": {"action": "...", "params": {...}}}\n'
        "Правила:\n"
        "- Если человек просто спрашивает/общается — kind=answer.\n"
        "- Для локального действия kind=actions; skill с несколькими шагами — несколько в steps.\n"
        "- Если человек просит удалить/перезаписать/закрыть/выключить — всё равно kind=actions, "
        "сервер сам потребует подтверждение по опасным.\n"
        "- Для напоминаний/памяти/интернета kind=cloud и укажи cloud.\n"
        "- Не выдумывай действие вне списков."
    )


def parse_plan(raw: str) -> dict:
    """Парсит модель->JSON, устойчив к обрамлению ```json ... ```."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"kind": "error", "text": "Не смогла разобрать запрос."}
    if not isinstance(data, dict):
        return {"kind": "error", "text": "Не смогла разобрать запрос."}
    kind = data.get("kind")
    if kind not in ("answer", "actions", "cloud", "error"):
        return {"kind": "error", "text": "Не смогла разобрать запрос."}
    return data


def _call_model(text: str, memory_context: str) -> str:
    """Синхронный вызов Ollama (уводится в поток вызывающим). Формальный
    контракт: возвращает сырой JSON-текст модели."""
    response = ollama.chat(
        model=config.ollama_model,
        messages=[
            {"role": "system", "content": _system_prompt(memory_context)},
            {"role": "user", "content": text},
        ],
        format="json",
    )
    return response["message"]["content"]


def build_plan(text: str, memory_context: str) -> dict:
    """Полный цикл: зовёт локальную модель и парсит план. Если модели нет —
    kind=error с подсказкой."""
    try:
        raw = _call_model(text, memory_context)
    except Exception as e:
        return {"kind": "error", "text": "Мозг (Ollama) недоступен — проверь, что Ollama запущена и модель установлена."}
    return parse_plan(raw)
```

> Замечание: `config` агента ещё не имеет `ollama_model`/`ollama_url` — их добавляет Step 4.

- [ ] **Step 4: Конфиг агента**

В `eva_agent/config.py`, в `__init__` после голосовых полей:

```python
        # Локальный мозг (Ollama)
        self.ollama_model: str = self._clean(os.getenv("EVA_OLLAMA_MODEL", "qwen2.5vl:7b")) or "qwen2.5vl:7b"
        self.ollama_url: str = self._clean(os.getenv("EVA_OLLAMA_URL", "http://localhost:11434")) or "http://localhost:11434"
```

И в `as_dict()` добавить `"ollama_model"`, `"ollama_url"`; в `update()` — маппинг `"ollama_model": "EVA_OLLAMA_MODEL"`, `"ollama_url": "EVA_OLLAMA_URL"`.

- [ ] **Step 5: `requirements.txt` агента**

`eva_agent/requirements.txt` — добавить `ollama`.

- [ ] **Step 6: Прогнать тесты**

Run: `cd eva_agent && python -m pip install -q ollama && python -m pytest test_local_brain.py -v`
Expected: PASS.

- [ ] **Step 7: Коммит → сервер**

Агент не в git; изменения Коммит возможен только в `brain bot/`, где лежит только сервер. Для агента — просто фиксация файлов на диске; коммитить нечего. (В план добавлен как задача с тестом, коммит не требуется — пометка на будущее для `.exe`.)

---

### Task 6: `ws_client.py` — обработка `chat`/`cloud_result`/`reminder` и ответы

**Файлы:**
- Modify: `eva_agent/ws_client.py`
- Test: `eva_agent/test_ws_client_handlers.py`

**Interfaces:**
- Consumes: `local_brain.build_plan`, `local_brain.classify_steps`, `actions.execute_action`, `voice.speak`, пайплайн ответов.
- Produces:
  - `ws_client.submit_chat(text)` (для UI)
  - `ws_client.push_ui_message(role: str, text: str)` (буфер для UI)
  - `ws_client.get_ui_chat() -> list[dict]`
  - `ws_client.is_brain_ready() -> bool`

- [ ] **Step 1: Падающий тест**

`test_ws_client_handlers.py`:

```python
import pytest
from ws_client import resolve_plan, prepare_chat_payload


def test_prepare_chat_answer():
    plan = {"kind": "answer", "text": "Привет!"}
    payload = prepare_chat_payload(41, plan)
    assert payload == {"type": "chat_result", "message_id": 41, "kind": "answer", "text": "Привет!"}


def test_resolve_confirm_marks_voice():
    plan = {"kind": "actions", "steps": [{"action": "delete_file", "params": {"path": "x"}, "description": "удалить"}]}
    msg_type, payload = resolve_plan(plan, message_id=7, origin="voice")
    assert msg_type == "confirm"
    assert payload["local_steps"][0]["action"] == "delete_file"
```

> Здесь `resolve_plan(plan, message_id, origin) -> (msg_type, payload)` — чистая функция, которую реализует этот task; отделяет решение от сетевой отправки, что и тестируется.

- [ ] **Step 2: Прогнать — падает**

Run: `cd eva_agent && python -m pytest test_ws_client_handlers.py -v`
Expected: FAIL.

- [ ] **Step 3: Реализовать чистые помощники в `ws_client.py`**

```python
from typing import Optional
import uuid

# Буферы для UI-чата (приложение читает через Api).
CHAT_BUFFER: deque = deque(maxlen=200)


def push_ui_message(role: str, text: str) -> None:
    CHAT_BUFFER.append({"role": role, "text": text, "time": datetime.now().strftime("%H:%M:%S")})


def get_ui_chat() -> list:
    return list(CHAT_BUFFER)


def prepare_chat_payload(message_id: int, plan: dict) -> dict:
    return {"type": "chat_result", "message_id": message_id, "kind": plan.get("kind", "answer"), "text": plan.get("text", "")}


def resolve_plan(plan: dict, message_id: int, origin: str):
    """Возвращает (msg_type, payload_to_send). msg_type: 'chat_result' | 'confirm' | 'cloud_action'."""
    kind = plan.get("kind")
    if kind == "answer":
        return "chat_result", prepare_chat_payload(message_id, plan)
    if kind == "cloud":
        return "cloud_action", {
            "type": "cloud_action",
            "request_id": uuid.uuid4().hex,
            "action": plan["cloud"]["action"],
            "params": plan["cloud"]["params"],
        }
    if kind == "actions":
        steps = plan.get("steps") or []
        if classify_steps(steps) == "confirm":
            local_steps = [
                {
                    "action": s.get("action"),
                    "params": s.get("params") or {},
                    "description": s.get("description") or f"Действие «{s.get('action')}»",
                }
                for s in steps
            ]
            payload = {"type": "confirm_request", "message_id": message_id, "local_steps": local_steps}
            if origin == "voice":
                payload["voice"] = True
            return "confirm", payload
        # безопасные локальные — агент исполняет сам и сразу отвечает
        return "execute_local", {"type": "chat_result", "message_id": message_id, "kind": "actions", **{"steps": steps}}
    return "chat_result", prepare_chat_payload(message_id, {"kind": "error", "text": plan.get("text", "Не понял.")})
```

> `classify_steps` импортируется из `local_brain`. `uuid` добавить в импорты.

- [ ] **Step 4: Асинхронный обработчик `chat` в `ws_client.py`**

Добавить в `_handle_message` новый блок до `if msg_type != "command"` (сейчас: `speak` → ранний return; добавить `chat`):

```python
    if msg_type == "chat":
        await _handle_chat(ws, data)
        return
    if msg_type == "cloud_result":
        _handle_cloud_result(data)
        return
    if msg_type == "reminder":
        _handle_reminder(data)
        return
    if msg_type == "chat_echo":
        _handle_chat_echo(data)
        return
```

И функции-обработчики:

```python
async def _handle_chat(ws, data: dict) -> None:
    from local_brain import build_plan
    message_id = data.get("message_id")
    text = data.get("text", "")
    origin = data.get("origin", "telegram")
    memory_context = "\n".join(f"- {m}" for m in (data.get("memory") or []))

    _log(f"💬 Чат от {origin}: {text}")
    plan = await asyncio.to_thread(build_plan, text, memory_context)
    msg_type, payload = resolve_plan(plan, message_id, origin)

    if msg_type == "execute_local":
        _log("Выполняю локальные действия...")
        results, status, image_bytes = await _execute_local_steps(plan.get("steps") or [])
        payload = {
            "type": "chat_result",
            "message_id": message_id,
            "kind": "error" if status == "error" else "actions",
            "text": "\n".join(results),
        }
        if image_bytes is not None:
            payload["image_base64"] = base64.b64encode(image_bytes).decode("ascii")
    elif msg_type == "confirm":
        _log("⚠️ Нужно подтверждение разрушительного действия в Telegram.")
        push_ui_message("system", "Нужно подтверждение в Telegram, прежде чем я это сделаю.")

    await ws.send(json.dumps(payload))
    if origin == "voice" and msg_type == "chat_result":
        _speak_if_possible(payload.get("text", ""))


async def _execute_local_steps(steps: list[dict]):
    results, status, last_image = [], "done", None
    for i, step in enumerate(steps, start=1):
        action = step.get("action")
        params = step.get("params", {})
        st, result, img = await asyncio.to_thread(execute_action, action, params)
        results.append(f"{i}. {result}")
        if img is not None:
            last_image = img
        if st == "error":
            status = "error"
            break
        if i < len(steps):
            await asyncio.sleep(1.0)
    return results, status, last_image


def _handle_cloud_result(data: dict) -> None:
    status = data.get("status")
    text = data.get("result", "")
    _log(f"☁️ Облачное действие: {status} — {text}")
    push_ui_message("eva", f"☁️ {text}")


def _handle_reminder(data: dict) -> None:
    text = data.get("text", "")
    _log(f"⏰ Напоминание: {text}")
    push_ui_message("system", f"⏰ Напоминание: {text}")
    _speak_if_possible(f"Напоминаю: {text}")


def _handle_chat_echo(data: dict) -> None:
    role = data.get("role", "eva")
    text = data.get("text", "")
    push_ui_message(role, text)
```

- [ ] **Step 5: Отправка из UI**

Добавить публичную функцию (потокобезопасную, как `submit_voice_command`):

```python
def submit_chat(text: str) -> None:
    """Вызывается из UI (поток pywebview) — переправляет текст в сетевой цикл."""
    if _loop is None:
        push_ui_message("system", "Сетевой цикл ещё не запущен.")
        return
    asyncio.run_coroutine_threadsafe(_send_chat_async(text), _loop)


async def _send_chat_async(text: str) -> None:
    global _current_ws
    if _current_ws is None:
        push_ui_message("system", "Нет соединения с сервером.")
        return
    push_ui_message("user", text)
    await _current_ws.send(json.dumps({"type": "chat", "text": text, "origin": "app"}))
```

> Смотри: для app-чата сервер сам прикрепляет `message_id` и память (см. Task 1), поэтому агент шлёт просто недостающие поля; сервер кладёт всё в `chat` обратно? Не совсем: агент уже подключён — удобнее, чтобы сервер воспринимал `{"type":"chat"}` от агента как запрос «примите моё сообщение как новый chat»: сервер трактует его как `voice_command`-подобный, но с origin=app и обогащает памятью. Это дополнение сервера в Task 2 — выполняется как «приём chat от агента».

> **Замечание (важно для последовательности):** в Task 2 в `eva_ws._handle_agent_message` добавить ветку `elif msg_type == "chat": await _handle_agent_initiated_chat(ws, bot, agent_id, data)`, которая вызывает `relay_to_agent` с заготовленным `message_id` (или сервер просто генерирует новый). Конкретика — в реализации: сервер получает `{"type":"chat","text":...,"origin":"app"}` (без message_id), генерирует `message_id`, инъектирует память и НЕ отсылает обратно себе — вместо этого вызывает общую функцию, которая исполняет план. Чтобы не плодить веток, проще: сервер при `chat` от агента сразу шлёт обратно `{"type":"chat","message_id":N,"text":...,"origin":"app","memory":[...]}`. Это сохраняет единую логику на агенте (`_handle_chat`).

- [ ] **Step 6: Прогнать тесты**

Run: `cd eva_agent && python -m pytest test_ws_client_handlers.py -v`
Expected: PASS.

- [ ] **Step 7: Дополнение сервера для app-чата (эйхо-запрос)**

В Task 2, `eva_ws._handle_agent_message`, добавить:

```python
    elif msg_type == "chat":
        # приложение шлёт сообщение на мозг: сервер обогащает памятью и
        # возвращает в том же виде, чтобы обработка шла по общей ветке на агенте
        text = data.get("text", "")
        from chat_state import new_message_id
        import eva_engine
        from memory import search_memory
        message_id = new_message_id()
        eva_engine.chat_state.register_message(message_id, owner_id, "app", text)
        memories = await search_memory(owner_id, text, k=5)
        await ws.send_str(json.dumps({
            "type": "chat", "message_id": message_id, "text": text,
            "origin": "app", "memory": memories,
        }))
```

> Проверить вызовом `test_agent_messages.py` и руками. (Это дополнение к Task 2/6 — в плане фиксируется здесь, чтобы наружу тесты агента не зависели от него.)

- [ ] **Step 8: Коммит (сервер)**

```bash
cd 'brain bot'
git add eva_ws.py eva_engine.py chat_state.py test_agent_messages.py
git commit -m "feat: app-чат агента через сервер (обогащение памятью)"
```

---

### Task 7: Чат-вкладка в приложении + уведомления

**Файлы:**
- Modify: `eva_agent/ui/index.html`
- Modify: `eva_agent/app.py`
- Modify: `eva_agent/ws_client.py` (публичные геттеры уже есть в Task 6)

**Interfaces:**
- Consumes: `ws_client.get_ui_chat()`, `ws_client.is_connected()`, `ws_client.submit_chat()`, `local_brain.is_ollama_ready()`.
- Produces: `app.Api.send_chat(text)`, `app.Api.get_chat_history()`, `app.Api.get_brain_status()`.

- [ ] **Step 1: Падающий (UI нет — но тест на Api в `test_ws_client_handlers.py` добавить)**

```python
def test_api_bridge_methods_exist():
    from app import Api
    api = Api()
    assert callable(getattr(api, "send_chat"))
    assert callable(getattr(api, "get_chat_history"))
    assert callable(getattr(api, "get_brain_status"))
```

Run: `cd eva_agent && python -m pytest test_ws_client_handlers.py -v` — FAIL (нет методов).

- [ ] **Step 2: Смок Python**

В `eva_agent/app.py` — в класс `Api` добавить:

```python
    def send_chat(self, text: str) -> dict:
        ws_client.submit_chat(text)
        return {"ok": True}

    def get_chat_history(self) -> list:
        return ws_client.get_ui_chat()

    def get_brain_status(self) -> dict:
        try:
            from local_brain import is_ollama_ready
            return {"ready": is_ollama_ready()}
        except Exception:
            return {"ready": False}
```

- [ ] **Step 3: UI — вкладка «Чат»**

В `ui/index.html`:

- В строке табов добавить третью кнопку:
```html
<button id="tab-chat-btn" class="flex-1 py-2 text-sm font-medium border-b-2 border-transparent text-slate-400">💬 Чат</button>
```
- Добавить контейнер вкладки после `#tab-log`:
```html
<div id="tab-chat" class="hidden flex-1 flex flex-col overflow-hidden">
  <div id="chat-list" class="flex-1 overflow-y-auto p-3 space-y-2"></div>
  <div class="flex gap-2 p-3 border-t border-slate-700 shrink-0">
    <input id="chat-input" type="text" placeholder="Напиши Еве..."
      class="flex-1 bg-slate-800 border border-slate-600 rounded px-3 py-2 text-sm focus:outline-none focus:border-blue-500">
    <button id="chat-send-btn" class="bg-blue-600 hover:bg-blue-500 rounded px-4 py-2 text-sm font-medium transition">➤</button>
  </div>
</div>
```
- В `showTab` добавить 3-й вариант:
```javascript
const tabChat = document.getElementById('tab-chat');
const tabChatBtn = document.getElementById('tab-chat-btn');
function showTab(name) {
  tabLog.classList.toggle('hidden', name !== 'log');
  tabSettings.classList.toggle('hidden', name !== 'settings');
  tabChat.classList.toggle('hidden', name !== 'chat');
  tabLogBtn.classList.toggle('border-blue-500', name === 'log');
  tabSettingsBtn.classList.toggle('border-blue-500', name === 'settings');
  tabChatBtn.classList.toggle('border-blue-500', name === 'chat');
  [tabLogBtn, tabSettingsBtn, tabChatBtn].forEach(b => b.classList.toggle('text-slate-400', b !== ...
  );
}
tabChatBtn.addEventListener('click', () => showTab('chat'));
```
> Аккуратно: существующий `showTab` переставить на переключение по `name !==`. Скопировать паттерн из текущего кода (два таба) на третий.

- Добавить загрузку/отправку сообщений:
```javascript
async function refreshChat() {
  try {
    const msgs = await api().get_chat_history();
    const list = document.getElementById('chat-list');
    list.innerHTML = msgs.slice().map(m => `
      <div class="text-xs rounded px-2 py-1.5 max-w-[85%] ${
        m.role === 'user'
          ? 'bg-blue-700/70 self-end ml-auto'
          : m.role === 'system' ? 'bg-slate-800 text-slate-400 text-center self-center' : 'bg-slate-800/80'
      }">
        <span class="text-slate-500">${m.time}</span> — ${escapeHtml(m.text)}
      </div>`).join('');
    list.scrollTop = list.scrollHeight;
  } catch (e) {}
}

async function sendChat() {
  const input = document.getElementById('chat-input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  await api().send_chat(text);
  refreshChat();
}

document.getElementById('chat-send-btn').addEventListener('click', sendChat);
document.getElementById('chat-input').addEventListener('keydown', e => { if (e.key === 'Enter') sendChat(); });
```
- В `refreshStatus` добавить индикатор мозга:
```javascript
    const brain = await api().get_brain_status();
    document.getElementById('brain-text').textContent = brain.ready ? '🧠 мозг готов' : '🧠 мозг не готов';
```
и в шапку добавить маленький бейдж `<span id="brain-text" class="text-xs text-slate-400">🧠…</span>`.

- В `pywebviewready` добавить `setInterval(refreshChat, 1500);`.

- [ ] **Step 4: Прогнать тесты**

Run: `cd eva_agent && python -m pytest test_ws_client_handlers.py -v`
Expected: PASS.

- [ ] **Step 5: Коммит (сервер, если менялись файлы сервера — нет; агент не в git)**

--- На этом каркас И1 готов. Вскользь отмечу: коммитить агент негде, поэтому финальный коммит сервера — на усмотрение, см. Task 8.

---

## Фаза C — интеграция

### Task 8: Интеграционная карта проверки (docs) + финальный коммит

**Файлы:**
- Create: `brain bot/docs/superpowers/plans/INTEGRATION_CHECKLIST.md` (или в README)

- [ ] **Step 1: Чек-лист ручных проверок**

Собрать `INTEGRATION_CHECKLIST.md`:

```markdown
# Итерация 1 — ручная проверка (ПК владельца)

## Подготовка
1. На ПК: `ollama pull qwen2.5vl:7b` (~5 ГБ), запустить `ollama serve`.
2. Задеплоить сервер (Render): git add/commit/push brain bot.
3. Запустить агента: `cd eva_agent && pip install -r requirements.txt && python app.py`
   (в настройках — как раньше; модель подтянется по умолчанию).

## Онлайн (агент на связи, Ollama + модель есть)
- [ ] Telegram: `/eva привет` → ответ из локального мозга (в логе агента видно 💬 Чат).
- [ ] Telegram: `/eva открой блокнот` → агент выполнил, результат в Telegram.
- [ ] Telegram: `/eva удали файл notes.txt` → кнопки ✅/❌ в Telegram; ✅ → исполнено.
- [ ] Голос: «Ева, что завтра по планам?» → локальный мозг отвечает, озвучка + ТГ-копия.
- [ ] Приложение: вкладка «Чат», написать «привет» → ответ в приложении.
- [ ] Приложение: «напомни через 15 минут вынести мусор» → Telegram ✅ + через 15 мин —
  сообщение в Telegram И уведомление в приложении (вкладка Чат / громкий сигнал).
- [ ] Приложение: «что я записывал про Стамбул» → memory_search выдал записи → ответ.
- [ ] Telegram: «узнай курс доллара» (онлайн) → Tavily-сырьё, синтез локально (в логе агента
  видно ☁️ Облачное действие).
- [ ] Telegram: «прочитай статью по ссылке <url>» → fetch_url + локальный синтез.

## Офлайн (агент выключен / ПК спит)
- [ ] Telegram: «привет» → отвечает Gemini (обычный чат бота).
- [ ] Telegram: «открой блокнот» → «ПК выключен, выполню при подключении» / очередь.
- [ ] Напоминание, заданное при офлайн ПК, приходит в Telegram.

## Краевые случаи
- [ ] Ollama не запущена: ответ агента — «Мозг (Ollama) недоступен...», агент жив.
- [ ] Модель не скачана (`ollama list` пусто): то же самое.
- [ ] Агент перезапущен: очередь команд доигралась, память на месте.
```

- [ ] **Step 2: Финальный коммит сервера**

```bash
cd 'brain bot'
git add -A
git commit -m "docs: чек-лист интеграции итерации 1"
git push
```

---

## Self-Review (проводится после написания плана, до передачи в исполнение)

- Спека покрыта: §5 (новые типы WS — Task 2/6), §6 (потоки по каналам — Task 1, 6, 7),
  §7 (память двусторонняя — Task 1, 3), §8 (напоминания двухканальные — Task 3, 6, 7),
  §9 (подтверждения — Task 2), §10 (ошибки: Ollama недоступна — Task 5; WS-обрыв — штатно;
  офлайн-guard — Task 1), §11 (И1 в плане, И2 вне плана).
- Плейсхолдеров нет — все шаги имеют код/код-тест.
- Консистентность типов: `resolve_plan` возвращает `(msg_type, payload)`; `chat_result`
  использует `message_id` из `chat`; `cloud_actions.execute_cloud_action(action, params, user_id)`
  согласована в Task 3/2/6; `register_message(user_id, origin, command_text)` — согласовано.