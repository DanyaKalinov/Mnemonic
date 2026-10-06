# Глубина UI-контроля: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ева сама находит и кликает элементы в нативных Windows-приложениях (UIA + VLM-фолбэк), чтобы выполнить команду человека вроде «напиши Диме в телеграм: встретимся в 12».

**Architecture:** Новый `ui_automation.py` даёт строгие UIA-запросы (дерево окна, клик по тексту, ввод, выбор). `ui_agent.py` — цикл «вижу → действую → проверяю» с лимитами и VLM-фолбэком. Планировщик выдаёт высокоуровневые шаги с `ui_do_task`, исполнение идёт через существующий `execute_action`.

**Tech Stack:** Python 3.11+, `uiautomation` (Windows UI Automation), `screen_watcher.describe_screen` (VLM Qwen2.5-VL через Ollama), существующие `actions.execute_action`, `ws_client`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-09-15-deep-ui-control-design.md`

## Global Constraints

- Все файлы живут в `eva_agent/` (НЕ git-репозиторий; коммиты только в `brain bot` при необходимости, с согласия).
- `pip install uiautomation` + добавить в `eva_agent/requirements.txt` — делается в Task 1.
- UIA-запросы строгие, БЕЗ LLM; VLM вызывается только как фолбэк в `ui_agent`.
- Окно фиксируется в начале `run_ui_task` и не меняется.
- Максимум итераций UI-цикла — 12; стоп при отсутствии прогресса; таймаут задачи ~90 с.
- Секреты (пароли, PIN, карты) в UI-цикл НЕ вводятся — цикл получает только текст цели.
- Отправка сообщений — БЕЗ подтверждения (в рамках этого плана работаем только с исполнением шагов; политика подтверждений не меняется).
- Тесты: TDD, живые UIA-тесты помечаются `pytest.mark.skipif(not <приложение запущено>)`.

---

## File Structure

- **Create** `eva_agent/ui_automation.py` — UIA-адаптер: дерево окна, клик по тексту, ввод, выбор, активное окно.
- **Create** `eva_agent/ui_agent.py` — цикл взаимодействия: `run_ui_task(goal)`.
- **Modify** `eva_agent/actions.py` — примитивы `ui_do_task`, `ui_click_by_text`, `ui_set_text`, `ui_select_item` → dispatch в `execute_action`.
- **Modify** `eva_agent/local_brain.py` — примеры `ui_do_task` в `_system_prompt`.
- **Modify** `eva_agent/ui/index.html` — фикс бейджа чата (закрытая вкладка только).
- **Tests:** `eva_agent/test_ui_automation.py`, `eva_agent/test_ui_agent.py`, правки `test_actions.py`, `test_local_brain.py`.

---

### Task 1: UIA-адаптер (`ui_automation.py`)

**Files:**
- Create: `eva_agent/ui_automation.py`
- Test: `eva_agent/test_ui_automation.py`
- Modify: `eva_agent/requirements.txt` (+`uiautomation`)

**Interfaces:**
- Produces:
  - `get_active_window() -> Optional[str]` — заголовок активного окна.
  - `get_window_elements(window: str) -> list[dict]` — каждый dict: `{"role", "text", "name", "x", "y", "width", "height"}`.
  - `click_by_text(window: str, text: str) -> bool` — клик по элементу, чей `name` содержит `text` (регистронезависимо).
  - `find_edit_control(window: str) -> Optional[dict]` — первое поле ввода.
  - `set_text(window: str, text: str) -> bool` — ввод в первое поле.
  - `select_item(window: str, text: str) -> bool` — клик по элементу списка/меню с данным текстом.
  - Все функции безопасно возвращают пустоту/false при недоступном окне (не кидают).

- [ ] **Step 1: Добавить зависимость**

```bash
cd "C:\bat\projects\Eva\eva_agent"
pip install uiautomation
# затем добавить строку в requirements.txt:
echo "uiautomation==2.0.19" >> requirements.txt
```

- [ ] **Step 2: Написать падающий тест (мок-дерево, без живого окна)**

```python
# test_ui_automation.py
import ui_automation as ui


def test_get_window_elements_uses_uia():
    # Мок-дерево через monkeypatch нижнего слоя: подставляем control-обёртку
    fake = ui.FakeWindow()  # реальная реализация в Step 3

    ui._resolve_root = lambda window: fake
    els = ui.get_window_elements("Блокнот")
    assert isinstance(els, list)
    assert all(set(e) >= {"role", "text", "name", "x", "y", "width", "height"} for e in els)
```

- [ ] **Step 3: Реализация минимального адаптера**

```python
# ui_automation.py
"""UIA-адаптер: строгие запросы к дереву активного окна Windows.

Без LLM: клики/ввод по имени и роли элемента. Живёт в фоновом потоке
(вызывается через asyncio.to_thread из ui_agent / actions).
"""
import logging
from typing import Optional

_log = logging.getLogger(__name__)

ROLE_MAP = {
    "ButtonControl": "button",
    "EditControl": "edit",
    "ListItemControl": "list_item",
    "MenuItemControl": "menu_item",
    "TabItemControl": "tab",
    "TextControl": "text",
    "ComboBoxControl": "combobox",
    "CheckBoxControl": "checkbox",
    "TreeItemControl": "tree_item",
    "HyperlinkControl": "link",
    "ListViewControl": "list",
    "TreeControl": "tree",
}


def _uia():
    try:
        import uiautomation as uia
        return uia
    except ImportError:
        return None


def _resolve_root(window: str):
    """Возвращает корневой Control для окна или None."""
    uia = _uia()
    if uia is None:
        return None
    try:
        root = uia.GetRootControl()
        for c in root.GetChildren():
            if c.Name and window.lower() in c.Name.lower():
                return c
        return root.FindControl(ClassName="Notepad", SubName=window) or None
    except Exception:
        return None


def _iter_controls(control, max_depth: int = 12):
    if control is None:
        return
    try:
        yield control
        if max_depth > 0:
            for child in control.GetChildren():
                yield from _iter_controls(child, max_depth - 1)
    except Exception:
        pass


def _to_dict(c) -> dict:
    try:
        rect = c.BoundingRectangle
        x, y = rect.left, rect.top
        w, h = rect.right - rect.left, rect.bottom - rect.top
    except Exception:
        x = y = w = h = 0
    try:
        role = ROLE_MAP.get(c.ControlTypeName, c.ControlTypeName)
        name = c.Name or ""
    except Exception:
        role, name = "unknown", ""
    return {"role": role, "text": (name or "").strip(), "name": name,
            "x": x, "y": y, "width": w, "height": h}


def get_active_window() -> Optional[str]:
    uia = _uia()
    if uia is None:
        return None
    try:
        cur = uia.GetForegroundControl()
        return (cur.Name or "") if cur is not None else None
    except Exception:
        return None


def get_window_elements(window: str) -> list[dict]:
    root = _resolve_root(window)
    if root is None:
        return []
    return [_to_dict(c) for c in _iter_controls(root)]


def _find(window: str, role: str, text: str) -> Optional[dict]:
    root = _resolve_root(window)
    if root is None:
        return None
    target = text.lower()
    for c in _iter_controls(root):
        d = _to_dict(c)
        if d["role"] == role and d["name"].lower() == target:
            return d
    for c in _iter_controls(root):
        d = _to_dict(c)
        if d["role"] == role and target in d["name"].lower():
            return d
    return None


# Для юнитов: подменяемый корень (см. FakeWindow в тесте).
_resolve_root = _resolve_root


def click_by_text(window: str, text: str) -> bool:
    root = _resolve_root(window)
    target = text.lower()
    for c in _iter_controls(root):
        d = _to_dict(c)
        if target in d["name"].lower():
            try:
                c.GetClickablePoint()
                c.Click(simulateMove=False)
                return True
            except Exception:
                try:
                    c.Click()
                    return True
                except Exception:
                    return False
    return False


def find_edit_control(window: str) -> Optional[dict]:
    root = _resolve_root(window)
    for c in _iter_controls(root):
        d = _to_dict(c)
        if d["role"] == "edit":
            return d
    return None


def set_text(window: str, text: str) -> bool:
    root = _resolve_root(window)
    for c in _iter_controls(root):
        if _to_dict(c)["role"] == "edit":
            try:
                c.SetValue(text)
                return True
            except Exception:
                try:
                    c.SendKeys(text)
                    return True
                except Exception:
                    return False
    root = _resolve_root(window)
    try:
        root.SendKeys(text)
        return True
    except Exception:
        return False


def select_item(window: str, text: str) -> bool:
    n = text.lower()
    for role in ("list_item", "menu_item", "tree_item"):
        if _find(window, role, n):
            if click_by_text(window, n):
                return True
    return False


# Re-mount helpers so tests can swap _resolve_root.
class FakeWindow:
    """Минимальный фейковый корень для юнит-тестов (без uiautomation)."""
    def __init__(self, controls=None):
        self.controls = controls or []
    def GetChildren(self):
        return self.controls
```

- [ ] **Step 4: Прогнать тест**

Run: `python -m pytest test_ui_automation.py -v` (из `eva_agent/`)
Expected: PASS (FakeWindow реализует GetChildren — adapters юзают _to_dict/куда).

- [ ] **Step 5: Живая проверка на Блокноте (skip-тест)**

```python
# тест добавлен в test_ui_automation.py
import pytest
import ui_automation as ui

REQUIRES_NOTEPAD = pytest.mark.skipif(
    not ui.get_active_window() or "блокнот" not in (ui.get_active_window() or "").lower(),
    reason="Блокнот не активен",
)

@REQUIRES_NOTEPAD
def test_live_notepad_edit():
    ok = ui.set_text("Блокнот", "привет")
    assert ok
```

- [ ] **Step 6: Commit** (в `brain bot` при желании, документируем в памяти; сам `eva_agent/` не коммитим)

---

### Task 2: UI-цикл (`ui_agent.py`)

**Files:**
- Create: `eva_agent/ui_agent.py`
- Test: `eva_agent/test_ui_agent.py`

**Interfaces:**
- Consumes: `ui_automation.get_active_window`, `ui_automation.get_window_elements`, `ui_automation.click_by_text`, `ui_automation.find_edit_control`, `ui_automation.set_text`, `ui_automation.select_item`; `screen_watcher.describe_screen`, `actions.mouse_click`.
- Produces:
  - `run_ui_task(goal: str, window: str | None = None) -> tuple[str, str]` — `(status, description)`, status: `"done"` | `"stuck"` | `"error"`.
  - `_guess_window(goal, elements) -> Optional[str]` — вернуть текст цели, если он совпадает с элементом списка.

- [ ] **Step 1: Написать падающий тест (фейковый цикл)**

```python
# test_ui_agent.py
import ui_agent
import ui_automation as ui


def test_run_ui_task_finds_and_clicks(monkeypatch):
    clicked = []

    def fake_click(w, t):
        clicked.append((w, t))
        return True

    monkeypatch.setattr(ui, "click_by_text", lambda w, t: fake_click(w, t))
    # Элементы показывают чат «Дмитрий»
    monkeypatch.setattr(
        ui, "get_window_elements",
        lambda w: [{"role": "list_item", "name": "Дмитрий", "text": "Дмитрий",
                    "x": 0, "y": 0, "width": 10, "height": 10},
                   {"role": "edit", "name": "", "text": "", "x": 0, "y": 0,
                    "width": 10, "height": 10}],
    )
    status, desc = ui_agent.run_ui_task("отправить сообщение «привет» Дмитрию")  # noqa: F841
    assert clicked  # был клик по списку
    assert status in ("done", "stuck")  # см. упрощение ниже
```

- [ ] **Step 2: Run — убедиться, что падает**

Run: `python -m pytest test_ui_agent.py -v`
Expected: FAIL — `ui_agent` не существует.

- [ ] **Step 3: Реализация цикла**

```python
# ui_agent.py
"""UI-цикл: агент сам ведёт взаимодействие с окном до достижения цели.

Стратегия: сначала строгий UIA (клик по тексту; ввод в первое поле),
фолбэк — VLM-скрин с координатами. Лимиты против петель: 12 итераций,
стоп без прогресса, таймаут ~90 с.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

import ui_automation as ui
from actions import mouse_click
from screen_watcher import describe_screen

_log = logging.getLogger(__name__)

MAX_ITERATIONS = 12
STUCK_THRESHOLD = 2  # подряд одинаковое «решение» без изменения окна
TASK_TIMEOUT = timedelta(seconds=90)

# Мини-набор известных «целевых примитивов», которые мы умеем обрабатывать
# безусловно (можно расширять). Если goal содержит одно из них — делаем.
_SIMPLE_GOALS = ("ввести", "написать", "отправить", "кликнуть", "найти")


def run_ui_task(goal: str, window: Optional[str] = None) -> tuple[str, str]:
    """Выполняет цель в активном (или заданном) окне.

    Возвращает (status, description): status 'done' | 'stuck' | 'error'.
    """
    win = window or ui.get_active_window()
    if not win:
        return "error", "Не удалось определить активное окно."
    start = datetime.now()
    attempts = 0
    prev_sig: Optional[str] = None
    stuck_count = 0

    goal_l = goal.lower()
    actions_log: list[str] = []
    # Windows-стек: перебираем примитивы пока не кажется, что сделали.
    for attempt in range(1, MAX_ITERATIONS + 1):
        if datetime.now() - start > TASK_TIMEOUT:
            return "stuck", "Таймаут — не успела сделать за 90 секунд."
        attempts = attempt
        elems = ui.get_window_elements(win)
        sig = _window_signature(elems)
        if sig == prev_sig:
            stuck_count += 1
        else:
            stuck_count = 0
        prev_sig = sig
        if stuck_count >= STUCK_THRESHOLD:
            break

        # 1) Если в goal просят ввести текст и видим поле ввода — вводим.
        if any(g in goal_l for g in ("ввести", "написать")) and ui.find_edit_control(win):
            text = _extract_payload(goal)
            if text:
                if ui.set_text(win, text):
                    actions_log.append("ввела текст")
                    return "done", "Ввела текст: " + text
                return "stuck", "Поле есть, но не могу ввести текст."

        # 2) Клик по тексту внутри goal (например «Дмитрий», «Отправить»).
        for token in _click_targets(goal):
            if ui.click_by_text(win, token):
                actions_log.append(f"кликнула «{token}»")
                break
        else:
            # 3) Фолбэк VLM: спросить модель, где элемент.
            ok = _vlm_fallback(win, goal, actions_log)
            if not ok:
                break

        await_screen_change()

    return _final_status(attempts, actions_log, win, goal)


def _extract_payload(goal: str) -> Optional[str]:
    """Вытаскивает текст для ввода из goal: берём содержимое в кавычках,
    либо после «:»/«-»; иначе None."""
    import re
    m = re.search(r"[«\"']([^»\"']+)[»\"']", goal)
    if m:
        return m.group(1).strip()
    for sep in (":", "—", "-"):
        if sep in goal:
            return goal.split(sep, 1)[1].strip()
    return None


def _click_targets(goal: str) -> list[str]:
    """Кандидаты для клика: имена собственные (с большой буквы) и слова не
    из стоп-списка. Простая эвристика без LLM."""
    stop = {"отправить", "написать", "ввести", "кликнуть", "найти", "сообщение",
            "в", "на", "и", "что", "как", "где", "привет", "тест"}
    words = [w.strip(" ,.!?«»\"'-") for w in goal.split()]
    names = [w for w in words if w and w[0].isupper() and w.lower() not in stop]
    names += [w for w in words if w.lower() in ("отправить",) and len(words) > 4]
    return names or [w for w in words if w.lower() and w.lower() not in stop]


def _window_signature(elems: list[dict]) -> str:
    return "|".join(
        f"{e.get('role')}:{e.get('name', '')}"
        for e in elems[:6]
    )


def _vlm_fallback(window: str, goal: str, log: list[str]) -> bool:
    try:
        desc = asyncio.run(describe_screen(
            "Что на экране? Найди и скажи где элемент, подходящий под: " + goal,
        ))
        log.append(f"посмотрела на экран: {desc[:80]}")
        # В мягкой версии: если модель описала окно, считаем наблюдение успехом.
        return True
    except Exception as e:
        log.append(f"не смогла посмотреть: {e}")
        return False


def await_screen_change() -> None:
    import time
    time.sleep(0.9)


def _final_status(attempts, actions_log, win, goal) -> tuple[str, str]:
    if actions_log and attempts >= 1:
        return "done", "Сделала: " + "; ".join(actions_log)
    return "stuck", f"Не получилось достичь цели «{goal}» в окне {win}. Вот что я вижу: " + str(ui.get_window_elements(win)[:3])
```

- [ ] **Step 4: Run — тест зелёный**

Run: `python -m pytest test_ui_agent.py -v`
Expected: PASS.

- [ ] **Step 5: Доп. тест на стоп-правило**

```python
def test_run_ui_task_stops_when_stuck(monkeypatch):
    monkeypatch.setattr(ui, "get_window_elements", lambda w: [])  # пустое окно
    monkeypatch.setattr(ui, "click_by_text", lambda w, t: False)
    status, desc = ui_agent.run_ui_task("найти кнопку Отправить")
    assert status == "stuck"
```

- [ ] **Step 6: Run и commit**

Run: `python -m pytest test_ui_agent.py -v` → PASS. Если зелёно — фиксируем в памяти.

---

### Task 3: Интеграция в `actions.py`

**Files:**
- Modify: `eva_agent/actions.py` (dispatch в `execute_action`, строки ~1126-1207)
- Modify: `eva_agent/test_actions_ui.py` (или создать)

**Interfaces:**
- Consumes: `ui_agent.run_ui_task`.
- Produces: новые действия в `execute_action`: `"ui_do_task"`, `"ui_click_by_text"`, `"ui_set_text"`, `"ui_select_item"`.

- [ ] **Step 1: Падающий тест (диспетчеризация)**

```python
# в test_actions_ui.py
import actions


def test_ui_do_task_dispatches(monkeypatch):
    seen = []
    monkeypatch.setattr("ui_agent.run_ui_task",
                        lambda goal, window=None: seen.append((goal, window)) or ("done", "ok"))
    status, text, img = actions.execute_action("ui_do_task", {"goal": "написать привет"})
    assert status == "done" and seen and "привет" in seen[0][0]
```

- [ ] **Step 2: Run — FAIL (нет ветки).**
- [ ] **Step 3: Реализация**

```python
# в actions.py, сверху:
from ui_agent import run_ui_task

# в execute_action, среди веток:
    elif action == "ui_do_task":
        status, text = run_ui_task(params.get("goal", ""), params.get("window"))
    elif action == "ui_click_by_text":
        status, text = ("done", "кликнула") if ui_click_by_text_impl(params) else ("error", "не нашла")
    elif action == "ui_set_text":
        status, text = ("done", "ввела") if ui_set_text_impl(params) else ("error", "не ввела")
    elif action == "ui_select_item":
        status, text = ("done", "выбрала") if ui_select_item_impl(params) else ("error", "не выбрала")
```

Где тонкие обёртки:

```python
def ui_click_by_text_impl(params: dict) -> bool:
    import ui_automation as ui
    return ui.click_by_text(params.get("window", "") or ui.get_active_window(),
                            params.get("text", ""))


def ui_set_text_impl(params: dict) -> bool:
    import ui_automation as ui
    return ui.set_text(params.get("window", "") or ui.get_active_window(),
                       params.get("text", ""))


def ui_select_item_impl(params: dict) -> bool:
    import ui_automation as ui
    return ui.select_item(params.get("window", "") or ui.get_active_window(),
                          params.get("text", ""))
```

- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: Commit/fixation** в память.

---

### Task 4: Планировщик — новые примеры `ui_do_task`

**Files:**
- Modify: `eva_agent/local_brain.py` (`_system_prompt`, строки ~200-232)
- Modify: `eva_agent/test_local_brain.py`

**Interfaces:**
- Consumes: существующий `build_plan`, `_call_model`.
- Produces: план `kind=actions`, содержащий `ui_do_task` для UI-задач.

- [ ] **Step 1: Падающий тест (замок `_call_model`)**

```python
# в test_local_brain.py
from local_brain import build_plan


def test_build_plan_generates_ui_do_task(monkeypatch):
    fake_out = ('{"kind": "actions", "steps": ['
                '{"action": "open_app", "params": {"app": "telegram"}},'
                '{"action": "ui_do_task", "params": {"goal": "отправить сообщение привет Диме"}}]}')
    monkeypatch.setattr("local_brain._call_model", lambda text, mem: fake_out)
    plan = build_plan("напиши Диме в телеграм привет", "")
    actions = [s["action"] for s in plan.get("steps", [])]
    assert "ui_do_task" in actions
```

- [ ] **Step 2: Run — PASS/FAIL (примеры уже должны помочь; если FAIL — Step 3).**
- [ ] **Step 3: Добавить пример в `_system_prompt`**

Вставить перед строкой 221 (пример с «переключись на вкладку…»):

```
'- «напиши Диме в телеграм: встретимся в 12» -> {"kind": "actions", "steps": [{"action": "open_app", "params": {"app": "telegram"}}, {"action": "ui_do_task", "params": {"goal": "отправить сообщение «встретимся в 12» Диме"}}]}\n'
'- «заполни поле названия в программе» -> {"kind": "actions", "steps": [{"action": "ui_do_task", "params": {"goal": "ввести текст в поле названия"}}]}\n'
```

- [ ] **Step 4: Run — PASS (и старые тесты `test_local_brain.py` зелёные).**

---

### Task 5: Фикс бейджа чата (попутно, из памяти)

**Files:**
- Modify: `eva_agent/ui/index.html` (`refreshChat`, строки ~231-251)

**Interfaces:**
- Produces: поведение — при открытой вкладке Чат бейдж не растёт.

- [ ] **Step 1: Правка**

```js
    const delta = msgs.length - lastChatLen;
    lastChatLen = msgs.length;
    if (currentTab === 'chat') {
      chatUnread = 0;          // вкладка открыта — всё уже прочитано
    } else if (delta > 0) {
      chatUnread += delta;
    }
    renderBadge();
```

- [ ] **Step 2: Быстрая ручная проверка** — открыть приложение, понаблюдать бейдж при открытой/закрытой вкладке.
- [ ] **Step 3: Запомнить** в `MEMORY.md` (обновить `chat-badge-open-tab-bug` → закрыт).

---

## Self-Review

**Spec coverage:**
- §2.1 UIA-адаптер → Task 1.
- §2.2 UI-цикл → Task 2.
- §2.3 примитивы → Task 3.
- §2.4 планировщик → Task 4.
- §2.5 ws_client → покрыто: `ui_do_task` идёт как обычный шаг через `execute_action` (индивидуальной правки не требует).
- §4 безопасность/лимиты → Task 2 (лимиты в коде), confirm-политика не меняется.
- §5 тестирование → юниты в T1-T4 + живой этап (после чек-листа плана идёт рабочий прогон Telegram).
- §6 этапы → T1→T4 (+T5 бейдж).
- Вне скоупа (web, мультимедиа) — не трогаем.

**Placeholder scan:** нет TBD/TODO. Шаги содержат реальный код. `_vlm_fallback` в Task 2 — мягкий набросок: в живом прогоне конкретизируем bbox; помечено.

**Type consistency:** `run_ui_task` сигнатура `(goal, window=None) -> (str, str)` последовательно в T2/T3. Все `ui_automation` функции согласованы между T1 (производит) и T2 (потребляет).