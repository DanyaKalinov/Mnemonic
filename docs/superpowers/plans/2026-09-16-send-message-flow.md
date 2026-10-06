# Поток «отправить сообщение»: уточнение, открытие приложения, поиск контакта — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development for каждый шаг (RED→GREEN→CHECKPOINT). Задачи идут по порядку; каждая проверяема.

**Goal:** Ева на просьбу написать сообщение спрашивает «куда», если адресат не назван; открывает нужное приложение/сайт; ищет контакт/чат; отправляет.

**Architecture:** Подход A из спека: новая логика целиком на desktop-агенте. Двухходовка через одно «незакрытое намерение» (`pending_intent`) в памяти агента + пере-планирование на каждый ход бес-состоятельным мозгом (local_brain qwen2.5vl). `run_ui_task` учится открывать адресат и искать контакт (строгий UIA → встроенный поиск приложения → честный `stuck`). Сервер-бот не меняется.

**Tech Stack:** Python 3, uiautomation, pywinauto (не трогаем), Ollama/qwen2.5vl, pytest (TDD).

**Spec:** `C:\bat\projects\Eva\brain bot\docs\superpowers\specs\2026-09-16-send-message-flow-design.md`

## Global Constraints

- **Всё на агенте** (`eva_agent/`); `brain bot/` не меняем (сервер просто ретранслирует chat/chat_result).
- **Честность статусов:** `done` только при реальном действии; «не нашла чат» → `stuck`, ничего никуда не отправлено.
- **Подтверждение отправки — никогда** (решение владельца). Guard вместо конфирма: композер существует перед Enter.
- **Секреты:** stop-list `_looks_secret` применяется и к тексту сообщения (payload) — фича с секретом → `error`.
- **Русский язык** в ответах/логах пользователя (Ева говорит по-русски).
- **No git:** вместо commit — шаг CHECKPOINT (full-suite зелёный + одна строка в итоговом отчёте).
- **Тесты:** из `eva_agent`: `python -m pytest test_<mod>.py -q -x`. При Bash-блокировке — через `!` в поле ввода Claude Code.
- **Пользовательские промпт-тексты мозгу** не ломаем: валидный JSON, kind из whitelist.

---

## Task 1: Модуль `pending_intent` (память намерения)

**Files:**
- Create: `eva_agent/pending_intent.py`
- Test: `eva_agent/test_pending_intent.py`

**Interfaces:**
- Produces: `PendingIntent` (dataclass: `prefix: str`, `text: str`); `store(prefix, text) -> None`; `get() -> Optional[PendingIntent]`; `clear() -> None`; `interpret_response(msg: str) -> str` (одно из `"cancel" | "replace" | "answer"`); `merge(pending, answer) -> str`.

- [ ] **Step 1: failing tests**

```python
# test_pending_intent.py
import pytest
import pending_intent as pi


def test_store_get_clear():
    pi.clear()
    assert pi.get() is None
    pi.store("напиши", "привет")
    got = pi.get()
    assert got is not None and got.prefix == "напиши" and got.text == "привет"
    pi.clear()
    assert pi.get() is None


def test_interpret_destination_answer():
    assert pi.interpret_response("в телеграм Диме") == "answer"


def test_interpret_fresh_command_is_replace():
    assert pi.interpret_response("напиши в вк Ане привет") == "replace"


def test_interpret_cancel():
    assert pi.interpret_response("отмена") == "cancel"
    assert pi.interpret_response("Не надо") == "cancel"


def test_merge_builds_goal():
    pi.clear()
    pi.store("напиши", "привет")
    assert pi.merge(pi.get(), "в телеграм Диме") == "напиши в телеграм Диме: привет"
```

- [ ] **Step 2: verify RED**

Run: `python -m pytest test_pending_intent.py -q -x`
Expected: FAIL — `ModuleNotFoundError: No module named 'pending_intent'`.

- [ ] **Step 3: minimal implementation**

```python
# pending_intent.py
import re
from dataclasses import dataclass
from typing import Optional

_VERB_RE = re.compile(r"^(напиш|отправ|сообщи|введи|открой|запусти|найди|сделай)", re.IGNORECASE)
_CANCEL_RE = re.compile(r"^(отмен|не надо|cancel)", re.IGNORECASE)


@dataclass
class PendingIntent:
    prefix: str
    text: str


_pending: Optional[PendingIntent] = None


def store(prefix: str, text: str) -> None:
    global _pending
    _pending = PendingIntent(prefix=prefix, text=text)


def get() -> Optional[PendingIntent]:
    return _pending


def clear() -> None:
    global _pending
    _pending = None


def interpret_response(msg: str) -> str:
    """Ответ на вопрос «куда написать?»: cancel / replace (новая команда) / answer."""
    if _CANCEL_RE.search((msg or "").strip()):
        return "cancel"
    if _VERB_RE.match((msg or "").strip()):
        return "replace"
    return "answer"


def merge(pending: Optional[PendingIntent], answer: str) -> str:
    if pending is None:
        return answer
    return f"{pending.prefix} {answer.strip()}: {pending.text}"
```

- [ ] **Step 4: verify GREEN**

Run: `python -m pytest test_pending_intent.py -q -x`
Expected: PASS (5 passed).

- [ ] **Step 5: CHECKPOINT** — suite `python -m pytest test_pending_intent.py -q` зелёный; отчёт: «pending_intent готов».

---

## Task 2: Интеграция pending в chat-цикл (`ws_client`)

**Files:**
- Modify: `eva_agent/ws_client.py`
- Test: `eva_agent/test_ws_client_handlers.py`

**Interfaces:**
- Consumes: `pending_intent.get/clear/interpret_response/merge/store` (Task 1).
- Produces: `resolve_plan` обрабатывает `kind="need_destination"` → `("chat_result", payload)`. `_handle_chat` применяет pending до `build_plan` и сохраняет `need_destination`-pending после планирования.

- [ ] **Step 1: failing tests**

```python
# в test_ws_client_handlers.py добавить (json уже импортирован в файле)

def _patch_env(monkeypatch):
    """Общая заготовка для _handle_chat: мозг готов, экран не трогаем."""
    import ws_client
    monkeypatch.setattr(ws_client, "ensure_ollama_ready", lambda: (True, "готов"))
    monkeypatch.setattr(ws_client, "get_screen_state",
                        lambda: {"active_window": {"title": "Блокнот"}, "mode": "normal"})


def test_resolve_need_destination_is_chat_result():
    plan = {"kind": "need_destination", "text": "Куда написать?"}
    msg_type, payload = resolve_plan(plan, message_id=9, origin="telegram")
    assert msg_type == "chat_result"
    assert payload["kind"] == "need_destination"


def test_handle_chat_merges_pending_answer(monkeypatch):
    import ws_client
    import pending_intent as pi
    pi.clear()
    pi.store("напиши", "привет")
    _patch_env(monkeypatch)
    seen = {}

    def fake_build_plan(text, memory_context):
        seen["text"] = text
        return {"kind": "answer", "text": "Отправила"}

    monkeypatch.setattr(ws_client, "build_plan", fake_build_plan)
    ws = FakeWs()
    asyncio.run(ws_client._handle_chat(ws, {"message_id": 1, "text": "в телеграм Диме", "origin": "app"}))
    assert seen["text"].startswith("напиши в телеграм Диме: привет")
    assert pi.get() is None, "после слияния pending чистится"


def test_handle_chat_cancels_pending(monkeypatch):
    import ws_client
    import pending_intent as pi
    pi.clear()
    pi.store("напиши", "привет")
    _patch_env(monkeypatch)
    called = {"n": 0}

    def fake_build_plan(text, memory_context):
        called["n"] += 1
        return {"kind": "answer", "text": "не должно планировать"}

    monkeypatch.setattr(ws_client, "build_plan", fake_build_plan)
    ws = FakeWs()
    asyncio.run(ws_client._handle_chat(ws, {"message_id": 1, "text": "отмена", "origin": "telegram"}))
    assert called["n"] == 0, "отмена не планирует заново"
    assert pi.get() is None
    assert json.loads(ws.sent[-1])["text"] == "Отменила."


def test_handle_chat_stores_pending_on_need_destination(monkeypatch):
    import ws_client
    import pending_intent as pi
    pi.clear()
    _patch_env(monkeypatch)

    def fake_build_plan(text, memory_context):
        return {"kind": "need_destination",
                "text": "Куда написать? Например: в телеграм Диме",
                "pending": {"prefix": "напиши", "text": "привет"}}

    monkeypatch.setattr(ws_client, "build_plan", fake_build_plan)
    ws = FakeWs()
    asyncio.run(ws_client._handle_chat(ws, {"message_id": 1, "text": "напиши привет", "origin": "telegram"}))
    got = pi.get()
    assert got is not None and got.text == "привет"
```

- [ ] **Step 2: verify RED** — Run: `python -m pytest test_ws_client_handlers.py -q -x`
Expected: FAIL — `need_destination` не в whitelist `resolve_plan` и pending не применяется.

- [ ] **Step 3: minimal implementation** (в `ws_client.py`)

Сверху: `import pending_intent`

В `resolve_plan` добавить ветку (после `kind == "answer"`):

```python
    if kind == "need_destination":
        return "chat_result", prepare_chat_payload(message_id, plan)
```

> `prepare_chat_payload` уносит только `kind`+`text` — поле `pending` из плана
> **не** уходит на сервер/в чат (оно целиком остаётся в памяти агента через
> `pending_intent.store`; серверу нужен лишь вопрос текстом). Проверка уже есть
> в тесте: `payload["kind"] == "need_destination"`, полю `pending` в payload быть
> не должно.

В `_handle_chat` — вставить pending-блок **сразу после** вычисления `line`
(после `except Exception: line = ""`, строка ~138), **до** `ensure_ollama_ready`,
чтобы отмена и слияние работали без обращения к мозгу:

```python
    pending = pending_intent.get()
    eff_text = text
    if pending is not None:
        resp = pending_intent.interpret_response(text)
        if resp == "cancel":
            pending_intent.clear()
            cancel_payload = {"type": "chat_result", "message_id": message_id,
                              "kind": "answer", "text": "Отменила."}
            await ws.send(json.dumps(cancel_payload))
            if origin == "voice":
                _speak_if_possible("Отменила.")
            return
        if resp == "replace":
            pending_intent.clear()
        else:
            eff_text = pending_intent.merge(pending, text)
            pending_intent.clear()
            _log(f"✍️ Продолжаю по заготовке: {eff_text}")
```

В `build_plan`-вызове заменить `text + line` → `eff_text + line` (строка ~156).
И после `_resolve_local_plan(...)` (строка ~157), до `resolve_plan`:

```python
    if plan.get("kind") == "need_destination":
        _p = plan.get("pending") or {}
        pending_intent.store(prefix=_p.get("prefix", "напиши"), text=_p.get("text", ""))
```

(Сохраняем ИТОГОВЫЙ план — после возможных look-перепланов.)

- [ ] **Step 4: verify GREEN** — Run: `python -m pytest test_ws_client_handlers.py -q -x`
Expected: PASS (4 новых + старые зелёные).

- [ ] **Step 5: CHECKPOINT** — suite `python -m pytest test_ws_client_handlers.py -q` зелёный.

---

## Task 3: local_brain — kind `need_destination` + детектор без-адресата

**Files:**
- Modify: `eva_agent/local_brain.py`
- Test: `eva_agent/test_local_brain.py`

**Interfaces:**
- Produces: `_DEST_KEYWORDS: tuple[str, ...]`; `_no_destination_send(text: str) -> Optional[str]` (вернёт текст сообщения, если это send-запрос без адресата, иначе None); `parse_plan` принимает `need_destination`; `build_plan` переопределяет план на `need_destination` при срабатывании детектора.

- [ ] **Step 1: failing tests**

```python
# в test_local_brain.py добавить
from local_brain import parse_plan, build_plan, _no_destination_send


def test_parse_plan_accepts_need_destination():
    plan = parse_plan('{"kind": "need_destination", "text": "Куда?", "pending": {"prefix": "напиши", "text": "привет"}}')
    assert plan["kind"] == "need_destination"
    assert plan["pending"]["text"] == "привет"


def test_no_destination_send_detects():
    assert _no_destination_send("напиши привет") == "привет"
    assert _no_destination_send("напиши привет (на экране сейчас активно: Блокнот)") == "привет"
    assert _no_destination_send("отправь сообщение «встретимся в 12»") == "встретимся в 12"


def test_no_destination_send_none_with_dest():
    assert _no_destination_send("напиши Диме в телеграм: привет") is None
    assert _no_destination_send("привет") is None


def test_build_plan_overrides_to_need_destination(monkeypatch):
    def fake_call_model(text, memory_context):
        # модель ошиблась: планирует ui_do_task в активном окне
        return '{"kind": "actions", "steps": [{"action": "ui_do_task", "params": {"goal": "отправить сообщение привет"}}]}'

    monkeypatch.setattr("local_brain._call_model", fake_call_model)
    plan = build_plan("напиши привет", "")
    assert plan["kind"] == "need_destination"
    assert plan["pending"]["text"] == "привет"
```

- [ ] **Step 2: verify RED** — Run: `python -m pytest test_local_brain.py -q -x`
Expected: FAIL — kind `need_destination` отклоняется; `_no_destination_send` не существует; override нет.

- [ ] **Step 3: minimal implementation** (в `local_brain.py`)

```python
_SEND_WORDS = ("напиш", "отправ", "сообщ", "смс")
_DEST_KEYWORDS = ("телеграм", "telegram", "вотсап", "whatsapp", "ватсап",
                  "вк", "вконтакте", "дискорд", "discord", "браузер",
                  "почт", "gmail", "mail", "мессендж")


def _extract_message(text: str) -> Optional[str]:
    """Текст сообщения из просьбы: кавычки, либо после «:», либо после глагола."""
    m = re.search(r"[«\"']([^»\"']+)[»\"']", text)
    if m:
        return m.group(1).strip()
    for sep in (":", "—", "-"):
        if sep in text:
            return text.split(sep, 1)[1].strip()
    parts = text.split()
    return " ".join(parts[1:]) if len(parts) > 1 else None


def _no_destination_send(text: str) -> Optional[str]:
    clean = re.split(r"\s*\(на экране", text)[0]
    gl = clean.lower()
    if not any(w in gl for w in _SEND_WORDS):
        return None
    if any(d in gl for d in _DEST_KEYWORDS):
        return None
    msg = _extract_message(clean) or ""
    return msg or None
```

В `parse_plan`: в whitelist kind-ов добавить `"need_destination"`; провалидировать `pending` (dict с `prefix`/`text`), иначе kind падает в `error`.

В `build_plan` (после `parse_plan(raw)`):

```python
    plan = parse_plan(raw)
    content = _no_destination_send(text)
    if content is not None:
        return {"kind": "need_destination",
                "text": "Куда написать? Назови приложение или сайт — например: в телеграм Диме.",
                "pending": {"prefix": "напиши", "text": content}}
    return plan
```

Промпт: в `_system_prompt` добавить правило строкой и пример:

```python
        '- «напиши привет» (не сказано куда) -> {"kind": "need_destination", '
        '"text": "Куда написать?", "pending": {"prefix": "напиши", "text": "привет"}}\n'
```

и в правила: `"- Если просят написать/отправить сообщение и НЕ названо, куда ('в телеграм', 'в вк'...) — kind=need_destination с текстом сообщения в pending.\n"`

- [ ] **Step 4: verify GREEN** — Run: `python -m pytest test_local_brain.py -q -x`
Expected: PASS (4 новых + старые).

- [ ] **Step 5: CHECKPOINT** — suite зелёный.

---

## Task 4: `ui_automation.find_search_edit`

**Files:**
- Modify: `eva_agent/ui_automation.py`
- Test: `eva_agent/test_ui_automation.py`

**Interfaces:**
- Produces: `find_search_edit(window: str) -> Optional[dict]` — верхний edit, чьё имя похоже на поиск (`_SEARCH_LIKE`), иначе самый верхний по y; `None`, если edits нет.

- [ ] **Step 1: failing tests**

```python
# в test_ui_automation.py добавить (паттерн FakeEdit уже есть в файле)
def test_find_search_edit_prefers_named_search():
    class E:
        ControlTypeName = "EditControl"
        def __init__(self, name, y):
            self._n, self._y = name, y
        @property
        def Name(self):
            return self._n
        @property
        def BoundingRectangle(self):
            return type("R", (), {"left": 0, "top": self._y, "right": 100, "bottom": self._y + 20})()

    search = E("Поиск", 50)
    other = E("", 30)
    ui._resolve_root = lambda window: ui.FakeWindow(controls=[search, other])
    d = ui.find_search_edit("Тест")
    assert d is not None and d["name"] == "Поиск"


def test_find_search_edit_takes_topmost_when_unnamed():
    class E:
        ControlTypeName = "EditControl"
        def __init__(self, y):
            self._y = y
        @property
        def Name(self):
            return ""
        @property
        def BoundingRectangle(self):
            return type("R", (), {"left": 0, "top": self._y, "right": 100, "bottom": self._y + 20})()

    ui._resolve_root = lambda window: ui.FakeWindow(controls=[E(80), E(20)])
    d = ui.find_search_edit("Тест")
    assert d is not None and d["y"] == 20


def test_find_search_edit_none_without_edits():
    ui._resolve_root = lambda window: ui.FakeWindow()
    assert ui.find_search_edit("Тест") is None
```

- [ ] **Step 2: verify RED** — Run: `python -m pytest test_ui_automation.py -q -x`
Expected: FAIL — `AttributeError: module 'ui_automation' has no attribute 'find_search_edit'`.

- [ ] **Step 3: minimal implementation**

```python
def find_search_edit(window: str) -> Optional[dict]:
    """Поле поиска (верхнее/поисковое) — инверсия find_message_input для
    шага «найти контакт». Сначала edit, похожий на поиск, иначе верхний."""
    root = _resolve_root(window)
    edits = [_to_dict(c) for c in _iter_controls(root)
             if _to_dict(c)["role"] == "edit"]
    if not edits:
        return None
    for e in edits:
        nm = (e["name"] or "").strip().lower()
        if any(s in nm for s in _SEARCH_LIKE):
            return e
    return min(edits, key=lambda e: e["y"])
```

- [ ] **Step 4: verify GREEN** — Run: `python -m pytest test_ui_automation.py -q -x`
Expected: PASS (3 новых + старые).

- [ ] **Step 5: CHECKPOINT** — suite зелёный.

---

## Task 5: `ui_agent._parse_send_goal` (адресат + контакт + текст)

**Files:**
- Modify: `eva_agent/ui_agent.py`
- Test: `eva_agent/test_ui_agent.py`

**Interfaces:**
- Produces: `_DEST_MAP: dict[str, tuple[str, str]]`; `_dest_phrase(goal: str) -> Optional[str]` (фраза-адресат из goal, напр. «в вк», или None); `_parse_send_goal(goal: str) -> tuple[Optional[str], Optional[str], Optional[str]]` = `(dest_phrase, contact, payload)`.
- Consumes: `ui_agent._extract_payload` (существующий), `_click_targets` (существующий).

- [ ] **Step 1: failing tests**

```python
# в test_ui_agent.py добавить
from ui_agent import _parse_send_goal, _dest_phrase


def test_parse_send_goal_full():
    dest, contact, payload = _parse_send_goal("напиши в вк Ане: привет")
    assert dest == "в вк"
    assert contact == "Ане"
    assert payload == "привет"


def test_parse_send_goal_telegram():
    dest, contact, payload = _parse_send_goal("напиши Диме в телеграм: встретимся в 12")
    assert dest == "в телеграм"
    assert contact == "Диме"
    assert payload == "встретимся в 12"


def test_parse_send_goal_no_destination():
    dest, contact, payload = _parse_send_goal("напиши привет")
    assert dest is None


def test_dest_phrase_detects():
    assert _dest_phrase("напиши в вк Ане привет") == "в вк"
    assert _dest_phrase("напиши Диме в телеграм: привет") == "в телеграм"
    assert _dest_phrase("напиши привет") is None
```

- [ ] **Step 2: verify RED** — Run: `python -m pytest test_ui_agent.py -q -x`
Expected: FAIL — 4 новых: функции не существуют.

- [ ] **Step 3: minimal implementation** (в `ui_agent.py`)

```python
# Адресат «куда писать»: цель-фраза -> (способ, цель). app=приложение, site=сайт.
_DEST_MAP = {
    "телеграм": ("app", "telegram"), "telegram": ("app", "telegram"),
    "вотсап": ("app_or_web", "whatsapp"), "whatsapp": ("app_or_web", "whatsapp"), "ватсап": ("app_or_web", "whatsapp"),
    "вк": ("site", "https://vk.com"), "вконтакте": ("site", "https://vk.com"),
    "дискорд": ("app_or_web", "discord"), "discord": ("app_or_web", "discord"),
}
_DEST_PREP_RE = re.compile(r"\b(в|на)\b")


def _dest_phrase(goal: str) -> Optional[str]:
    """Фраза-адресат из цели («в вк», «в телеграм») или None, если адресата нет."""
    gl = goal.lower()
    for key in _DEST_MAP:
        if key in gl:
            idx = gl.find(key)
            pre = gl[:idx]
            starts = [m.start() for m in _DEST_PREP_RE.finditer(pre)]
            start = starts[-1] if starts else 0
            return goal[start:idx + len(key)].strip() or None
    return None


def _parse_send_goal(goal: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    dest = _dest_phrase(goal)
    payload = _extract_payload(goal)
    contact = None
    stop = {"в", "на", "и", "что", "как", "где"} | set(_DEST_MAP) | {"напиши", "написать"}
    for w in _click_targets(goal):
        if w.lower() in stop or (payload and w == payload):
            continue
        contact = w
        break
    return dest, contact, payload
```

(contact = первое заглавное слово `_click_targets` минус стоп-слова/адресаты/payload.)

- [ ] **Step 4: verify GREEN** — Run: `python -m pytest test_ui_agent.py -q -x`
Expected: PASS (4 новых + старые). Если контакт «Ане»/«Диме» не попадает в `_click_targets` (слово с большой буквы есть) — ок; иначе подправить контакт-поиск под тест.

- [ ] **Step 5: CHECKPOINT** — suite зелёный.

---

## Task 6: `ui_agent` — открыть адресат, если окна нет

**Files:**
- Modify: `eva_agent/ui_agent.py`
- Test: `eva_agent/test_ui_agent.py`

**Interfaces:**
- Produces: `_open_dest(dest_phrase: str) -> tuple[bool, str]` (открыть app/site; `(ok, note)`); `_ensure_dest_window(goal: str, win: Optional[str]) -> tuple[Optional[str], str]` (окно для работы; открывает при отсутствии; `(title|None, note)`).

- [ ] **Step 1: failing tests**

```python
# в test_ui_agent.py добавить
from ui_agent import _open_dest, _ensure_dest_window
import ui_automation as _ui
import actions as _act


def test_open_dest_site(monkeypatch):
    opened = {}
    monkeypatch.setattr(_act, "open_website", lambda url: (opened.setdefault("s", url) or "done", "ок"))
    ok, _note = _open_dest("в вк")
    assert ok and opened.get("s") == "https://vk.com"


def test_ensure_dest_window_opens_when_missing(monkeypatch):
    import ui_agent
    opened = []
    seen = []
    monkeypatch.setattr(ui_agent, "_parse_send_goal",
                        lambda goal: ("в вк", "Ане", "привет"))
    monkeypatch.setattr(_ui, "get_active_window", lambda: None)
    monkeypatch.setattr(_ui, "list_windows", lambda: [])

    def fake_open_app(app):
        seen.append(app)
        opened.append("Telegram (11905)")
        return "done", "Открыл"

    monkeypatch.setattr(_act, "open_app", fake_open_app)
    # первая попытка pick — окна нет; дальше «появилось» -> открыли -> pick видит окно
    def fake_pick(goal, windows, active):
        if opened:
            return "Telegram (11905)"
        return None

    monkeypatch.setattr(ui_agent, "_pick_ui_window", fake_pick)
    win, note = _ensure_dest_window("напиши в вк Ане: привет", None)
    assert seen == ["telegram"], "адресат «в вк» -> без приложения, фолбэк не нужен"
    assert win == "Telegram (11905)"
```

(В тесте `_parse_send_goal` и `_pick_ui_window` замоканы, чтобы проверять именно «окно
не открыто → открыть → работать в новом»; сам парсер уже покрыт в Task 5. `open_app`
дюжится как «открыло»; `_open_dest` подхватывает адресат по ключу «вк» → открывает сайт,
а тест-хвост про попытку app оставлен для самопроверки связки `_open_dest`-арп.)

- [ ] **Step 2: verify RED** — Run: `python -m pytest test_ui_agent.py -q -x`
Expected: FAIL — `_open_dest`/`_ensure_dest_window` нет.

- [ ] **Step 3: minimal implementation** (в `ui_agent.py`; `import time` уже есть)

```python
def _open_dest(dest_phrase: str) -> tuple[bool, str]:
    import actions as _act  # лениво: не тянем pywinauto/старт-меню при импорте
    gl = (dest_phrase or "").lower()
    for key, (mode, target) in _DEST_MAP.items():
        if key in gl:
            if mode == "site":
                return _act.open_website(target)
            ok, note = _act.open_app(target)
            if ok == "done" or mode == "app":
                return ok == "done", note
            # app_or_web: приложения нет -> открываем web-версию
            web = {"whatsapp": "https://web.whatsapp.com",
                   "discord": "https://discord.com"}.get(target)
            if web:
                return _act.open_website(web)
            return False, note
    return False, f"Не понимаю адресат «{dest_phrase}»."


def _ensure_dest_window(goal: str, win: Optional[str]) -> tuple[Optional[str], str]:
    dest_phrase, _contact, _payload = _parse_send_goal(goal)
    if not dest_phrase:
        return win, ""
    if not win and _has_window_hint(goal):
        win = _pick_ui_window(goal, ui.list_windows(), ui.get_active_window())
    if win:
        return win, ""
    ok, note = _open_dest(dest_phrase)
    if not ok:
        return None, note
    time.sleep(1.5)  # дать приложению/браузеру открыться
    return _pick_ui_window(goal, ui.list_windows(), ui.get_active_window()), ""
```

Подключить в `run_ui_task` вместо текущего блока определения окна (строки ~80-87):

```python
    win = window
    if not win:
        win = ui.get_active_window() if not _has_window_hint(goal) else \
              _pick_ui_window(goal, ui.list_windows(), ui.get_active_window())
        if not win and _parse_send_goal(goal)[0]:
            win, open_note = _ensure_dest_window(goal, None)
            if not win:
                return "stuck", f"Не удалось открыть место для сообщения: {open_note}"
    if not win:
        return "error", "Не удалось определить активное окно."
```

- [ ] **Step 4: verify GREEN** — Run: `python -m pytest test_ui_agent.py -q -x`
Expected: PASS (2 новых + старые). Внимание: старый `test_run_ui_task_picks_telegram_window_when_active_is_terminal` должен остаться зелёным — окно берётся до enter-логики.

- [ ] **Step 5: CHECKPOINT** — suite зелёный.

---

## Task 7: `ui_agent` — поиск контакта и честная отправка

**Files:**
- Modify: `eva_agent/ui_agent.py`
- Test: `eva_agent/test_ui_agent.py`

**Interfaces:**
- Consumes: `ui.find_search_edit` (Task 4), `ui.click_by_text`, `ui.set_text`, `ui.send_enter`, `ui.find_message_input`, `_parse_send_goal` (Task 5).
- Produces: поведение send-цели: найти контакт (клик/поиск) → проверить композер → ввести payload → Enter → `done`; композера нет → `stuck` без отправки.

- [ ] **Step 1: failing tests**

```python
# в test_ui_agent.py добавить. Общий фиксчер — «окно мессенджера с композером».
CHAT_ELEMS = [
    {"role": "edit", "name": "", "x": 10, "y": 300, "w": 400, "h": 60},
    {"role": "button", "name": "Дима", "x": 10, "y": 100, "w": 100, "h": 30},
]


def _fake_env(monkeypatch):
    monkeypatch.setattr(ui, "get_active_window", lambda: "Тест")
    monkeypatch.setattr(ui, "get_window_elements", lambda w: CHAT_ELEMS)
    monkeypatch.setattr(ui, "find_message_input",
                        lambda w: next((e for e in CHAT_ELEMS if e["role"] == "edit"), None))
    monkeypatch.setattr(ui_agent, "_vlm_fallback", lambda w, g, log: False)


def test_run_ui_task_finds_contact_click_then_sends(monkeypatch):
    """Контакт кликается строгим UIA -> композер -> отправка."""
    typed = []
    entered = []
    _fake_env(monkeypatch)
    monkeypatch.setattr(ui, "find_search_edit", lambda w: None)
    clicked = []
    monkeypatch.setattr(ui, "click_by_text",
                        lambda w, t: (clicked.append(t) or True))
    monkeypatch.setattr(ui, "set_text", lambda w, t: (typed.append(t) or True))
    monkeypatch.setattr(ui, "send_enter", lambda w: (entered.append(w) or True))
    status, _ = ui_agent.run_ui_task("напиши Диме в телеграм: встретимся в 12")
    assert status == "done"
    assert clicked == ["Диме"]
    assert "встретимся в 12" in typed
    assert entered


def test_run_ui_task_searches_contact_when_no_click(monkeypatch):
    """click не сработал -> поиск по имени + Enter, потом отправка."""
    typed = []
    _fake_env(monkeypatch)
    monkeypatch.setattr(ui, "click_by_text", lambda w, t: False)
    monkeypatch.setattr(ui, "find_search_edit", lambda w: {"role": "edit", "name": "Поиск", "y": 20})
    monkeypatch.setattr(ui, "set_text", lambda w, t: (typed.append(t) or True))
    monkeypatch.setattr(ui, "send_enter", lambda w: (typed.append("ENTER") or True))
    status, _ = ui_agent.run_ui_task("напиши в вк Ане: привет")
    assert status == "done"
    assert "Ане" in typed  # искала контакт
    assert "ENTER" in typed
    assert "привет" in typed


def test_run_ui_task_stuck_when_no_composer_after_search(monkeypatch):
    """Контакт не нашёлся (композера нет) -> stuck, никакой отправки текста."""
    sent = []
    _fake_env(monkeypatch)
    monkeypatch.setattr(ui, "find_message_input", lambda w: None)  # композер нет
    monkeypatch.setattr(ui, "click_by_text", lambda w, t: False)
    monkeypatch.setattr(ui, "find_search_edit", lambda w: {"role": "edit", "name": "Поиск", "y": 20})
    monkeypatch.setattr(ui, "set_text", lambda w, t: (sent.append(t) or True))
    monkeypatch.setattr(ui, "send_enter", lambda w: (sent.append("ENTER") or True))
    status, desc = ui_agent.run_ui_task("напиши Диме в телеграм: встретимся в 12")
    assert status == "stuck"
    assert "встретимся в 12" not in sent, "текст не должен уйти без чата"
```

Contingence: если `_click_targets(goal)` внутри `_parse_send_goal` ходит в живую
UIA (а не строковый разбор), добейте в `_fake_env`:
`monkeypatch.setattr(ui_agent, "_click_targets", lambda g: ["Диме", "Ане"])` —
контакт от парсера становится детерминированным. В остальном связка проверяется
целиком (парсер → поиск контакта → композер → отправка).

- [ ] **Step 2: verify RED** — Run: `python -m pytest test_ui_agent.py -q -x`
Expected: FAIL — 3 новых (текущий send-путь не ищет контакт).

- [ ] **Step 3: minimal implementation** — в `run_ui_task` расширить send-путь
(детали вставки — в «Место вставки» ниже). Новое поведение: send-цель с контактом
сначала ищет чат (`_locate_contact`), потом проверяет композер, потом вводит текст
и жмёт Enter; send-цель без контакта — сразу композер → ввод → Enter; не-send —
без изменений. Модуль-хелпер `_locate_contact` (объявить рядом с `_parse_send_goal`):

```python
def _locate_contact(win: str, contact: str, actions_log: list) -> bool:
    """Найти контакт в окне: строгий клик по имени, иначе встроенный поиск."""
    if ui.click_by_text(win, contact):
        actions_log.append(f"нашла контакт «{contact}»")
        return True
    search = ui.find_search_edit(win)
    if search:
        if ui.set_text(win, contact):
            actions_log.append(f"искала «{contact}» в поиске")
            if ui.send_enter(win):
                actions_log.append("открыла первый результат")
                return True
    return False
```

**Место вставки — критично.** В `run_ui_task` сейчас:

```python
        in_field = any(g in goal_l for g in ("введ", "напиш"))
        target = ((ui.find_message_input(win) if _is_send_goal(goal)
                   else ui.find_edit_control(win)) if in_field else None)   # строка 119-120
        if target:
            ...  # старый путь: ввод + (Enter для send)
```

Если вставить send-блок ПОСЛЕ `if target:` — для send-цели композер уже найден и
старый Enter-путь сработает БЕЗ поиска контакта. Поэтому send-ветку вставляем
выше, сразу после `in_field = ...`, а старую строку `target = ...` оставляем для
не-send-целей:

```python
        in_field = any(g in goal_l for g in ("введ", "напиш"))
        if _is_send_goal(goal) and in_field:
            _dest, contact, payload = _parse_send_goal(goal)
            sent_payload = payload or _extract_payload(goal)
            if contact:
                if not _locate_contact(win, contact, actions_log):
                    return ("stuck",
                            f"Не нашла чат/контакт «{contact}» в окне {win}. "
                            f"Последнее: {'; '.join(actions_log[-2:]) or 'попытки не было'}")
                time.sleep(0.8)
                if ui.find_message_input(win) is None:
                    return "stuck", f"Открыла, но нет поля ввода сообщения для «{contact}»."
            if sent_payload:
                target = ui.find_message_input(win)
                if target is None:
                    return "stuck", "Нет поля ввода сообщения в этом окне."
                if ui.set_text(win, sent_payload):
                    actions_log.append(f"ввела текст: {sent_payload}")
                    if ui.send_enter(win):
                        actions_log.append("нажала Enter (отправка)")
                        return "done", "Сделала: " + "; ".join(actions_log)
                    return "stuck", f"Ввела текст, но Enter не нажался — сообщение не отправлено."
                return "stuck", "Поле есть, но не могу ввести текст."
        # НЕ-send: старый путь без изменений (введи текст в первое поле без Enter).
        target = ((ui.find_edit_control(win)) if in_field else None)
        if target:
            ...  # прежние строки 121-136, без send-ветки
```

И модуль-хелпер `_locate_contact`:

```python
def _locate_contact(win: str, contact: str, actions_log: list) -> bool:
    """Найти контакт в окне: строгий клик по имени, иначе встроенный поиск."""
    if ui.click_by_text(win, contact):
        actions_log.append(f"нашла контакт «{contact}»")
        return True
    search = ui.find_search_edit(win)
    if search:
        if ui.set_text(win, contact):
            actions_log.append(f"искала «{contact}» в поиске")
            if ui.send_enter(win):
                actions_log.append("открыла первый результат")
                return True
    return False
```

Так старый send-тест `sends_enter_after_typing_for_send_goal` (без контакта:
«напиши привет» / просто ввод+Enter) продолжает попадать в send-ветку с
`contact is None` → идёт сразу в ввод+Enter — поведение прежнее. А обычный
не-send ввод (`введи ...`) идёт в не-send-ветку — без Enter.

- [ ] **Step 4: verify GREEN** — Run: `python -m pytest test_ui_agent.py -q -x`
Expected: PASS (3 новых + весь старый набор, включая `sends_enter_after_typing_for_send_goal`, `types_without_enter_for_plain_input`).

- [ ] **Step 5: CHECKPOINT** — suite зелёный.

---

## Task 8: Полный прогон и живой E2E (ручной)

**Files:** — (без кода; запуски)

- [ ] **Step 1: полный suite**

Run (из `eva_agent`): `python -m pytest -q`
Expected: PASS, без сбоев (было 121 passed + новые: 5 pend + 4 ws + 4 brain + 3 uia + 4 parse + 2 open + 3 search = ~146).

- [ ] **Step 2: двухходовка живьём**

Telegram открыт (не в трее). В чат Еве:
1. «напиши привет» → Ева: «Куда написать? …»
2. «в телеграм Диме» → Ева открывает Telegram, ищет «Дима», открывает чат, отправляет «привет».
Глазами (Ruling 6): плашка «привет» в чате Димы реально появилась.

- [ ] **Step 3: полный запрос одним ходом**

«напиши Диме в телеграм: встретимся в 12» → без вопроса: открыть, найти, отправить. Проверить глазами.

- [ ] **Step 4: cancel**

«напиши в вк Ане: привет» → перед отправкой в чат: «отмена» → «Отменила.» и ничего не открывается.

- [ ] **Step 5: CHECKPOINT** — memory-обновление: новая запись `send-message-flow-implemented` + строка в `MEMORY.md`; итог в отчёте сессии.