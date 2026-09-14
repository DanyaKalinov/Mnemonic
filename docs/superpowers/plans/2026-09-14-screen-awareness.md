# Непрерывное осознание экрана — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать агенту постоянную двухъярусную картину экрана: дешёвый реалтайм-слой (активное окно + дифф кадров), семантика по требованию, watch-правила «следить за конкретным» с уведомлениями в Telegram и приложение.

**Architecture:** Два яруса. Ярус 1 — фоновый поток `screen_watcher.ScreenWatcher`: кадр раз в секунду → перцептивный хэш/дифф + активное окно через win32gui (без LLM). Ярус 2 — `describe_screen()`: подпись экрана из кэша с TTL, иначе живой взгляд через существующий `actions.look_at_screen`. `watchers.py` хранит правила (JSON), оценивает их на события яруса 1, уведомляет через новый серверный тип `agent_notify` (Telegram) и локальный push в интерфейс.

**Tech Stack:** Python (asyncio), pywinauto/win32gui, Pillow (ImageGrab), Ollama (qwen2.5vl:7b), pytest, aiogram/aiohttp на сервере.

**Spec:** `docs/superpowers/specs/2026-09-14-screen-awareness-design.md`

## Global Constraints

- Тайминги по умолчанию (из spec §6): `frame_interval=1.0с`, `caption_ttl=12с`, `caption_refresh_s=45с`, `watch_cooldown_s=300с`.
- Кадры экрана **только в RAM**, никогда на диск; не покидают ПК.
- VLM (ярус 2) вызывается **только** по требованию/refresh — не на каждый кадр.
- В `ws_client.py` импорты `from actions import execute_action, list_window_titles` — по имени. В тестах мокать `"ws_client.<имя>"`, а не `"actions.<имя>"`.
- OCR **не входит** в объём: change-правила работают через VLM-описание; без Ollama они ждут, appear/disappear работают без VLM.
- Агентский код (`eva_agent/`) и серверный (`brain bot/`) — два разных каталога; коммиты в `brain bot` (единственный git-репозиторий). Файлы `eva_agent/` не коммитятся (не git).

---

### Task 1: config + хэш/дифф кадра + ScreenState + кэш подписи

**Files:**
- Modify: `eva_agent/config.py:50-67` (новые поля в `__init__`), `:78-90` (`as_dict`), `:92-116` (`update`)
- Create: `eva_agent/screen_watcher.py` (часть: константы, `_fit_frame`, `_frame_hash`, `_diff_magnitude`, `ScreenState`)
- Test: `eva_agent/test_config.py`, `eva_agent/test_screen_watcher.py` (часть)

**Interfaces:**
- Produces: `config.screen_frame_interval: float`, `config.caption_ttl: float`, `config.caption_refresh_s: float`, `config.watch_cooldown_s: float`; `screen_watcher._fit_frame(img, max_w=320) -> Image`, `screen_watcher._frame_hash(img, size=16) -> int`, `screen_watcher._diff_magnitude(h1, h2) -> int`, `screen_watcher.FRAME_CHANGE_THRESHOLD = 16`, `screen_watcher.ScreenState` (dataclass с `active_window`, `last_change_ts`, `last_magnitude`, `caption`, `caption_ts`, `mode`, `events` deque).

- [ ] **Step 1: Тест конфига — новые поля**

`eva_agent/test_config.py`:
```python
def test_screen_awareness_defaults(monkeypatch):
    monkeypatch.delenv("EVA_SCREEN_FRAME_INTERVAL", raising=False)
    monkeypatch.delenv("EVA_CAPTION_TTL", raising=False)
    monkeypatch.delenv("EVA_CAPTION_REFRESH_S", raising=False)
    monkeypatch.delenv("EVA_WATCH_COOLDOWN_S", raising=False)
    from config import AgentConfig
    c = AgentConfig()
    assert c.screen_frame_interval == 1.0
    assert c.caption_ttl == 12.0
    assert c.caption_refresh_s == 45.0
    assert c.watch_cooldown_s == 300.0
```

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `cd "C:\bat\projects\Eva\eva_agent" && python -m pytest test_config.py -q`
Expected: FAIL — `AttributeError: 'AgentConfig' object has no attribute 'screen_frame_interval'`

- [ ] **Step 3: Поля конфига**

В `config.py`, после `self.ollama_url` (строка 66) добавить:
```python
        # Непрерывное осознание экрана (двухъярусная картина)
        self.screen_frame_interval: float = float(os.getenv("EVA_SCREEN_FRAME_INTERVAL", "1.0"))
        self.caption_ttl: float = float(os.getenv("EVA_CAPTION_TTL", "12.0"))
        self.caption_refresh_s: float = float(os.getenv("EVA_CAPTION_REFRESH_S", "45.0"))
        self.watch_cooldown_s: float = float(os.getenv("EVA_WATCH_COOLDOWN_S", "300.0"))
```
В `as_dict()` добавить четыре ключа; в `update()` добавить в `mapping`:
```python
            "screen_frame_interval": "EVA_SCREEN_FRAME_INTERVAL",
            "caption_ttl": "EVA_CAPTION_TTL",
            "caption_refresh_s": "EVA_CAPTION_REFRESH_S",
            "watch_cooldown_s": "EVA_WATCH_COOLDOWN_S",
```

- [ ] **Step 4: Тест хэша — синтетические кадры**

`eva_agent/test_screen_watcher.py`:
```python
from PIL import Image
import screen_watcher as sw


def _solid(color):
    return Image.new("L", (320, 180), color)


def _two_halfs(left, right):
    img = Image.new("L", (320, 180))
    for x in range(160):
        for y in range(180):
            img.putpixel((x, y), left)
    for x in range(160, 320):
        for y in range(180):
            img.putpixel((x, y), right)
    return img


def _vertical_stripes(dark, light, width=40):
    """Вертикальные полосы: перепады по X, которые dHash видит (в отличие
    от вертикального сплошного раздела — dHash сравнивает пиксель со
    следующим ВДОЛЬ строки, поэтому однотонная вертикаль бит не даёт)."""
    img = Image.new("L", (320, 180))
    for x in range(320):
        for y in range(180):
            img.putpixel((x, y), light if (x // width) % 2 else dark)
    return img


def test_identical_frames_have_zero_diff():
    a = sw._frame_hash(_solid(100))
    b = sw._frame_hash(_solid(100))
    assert sw._diff_magnitude(a, b) == 0


def test_different_layout_has_high_diff():
    a = sw._frame_hash(_solid(100))
    b = sw._frame_hash(_vertical_stripes(0, 255))
    assert sw._diff_magnitude(a, b) >= sw.FRAME_CHANGE_THRESHOLD


def test_fit_frame_scales_down_wide_images():
    wide = Image.new("L", (2560, 1440), 0)
    out = sw._fit_frame(wide)
    assert out.width <= 320 and out.height == 180


def test_small_perturbation_below_threshold():
    # Кадр с полосами реально несёт биты dHash (не однотонный) — иначе тест
    # проходил бы тривиально (diff 0), как с вертикальным сплошным разделом.
    assert sw._frame_hash(_vertical_stripes(0, 255)) != 0
    img = _vertical_stripes(0, 255)
    img.putpixel((0, 0), 128)  # один пиксель из 28800 отличается
    a = sw._frame_hash(_vertical_stripes(0, 255))
    b = sw._frame_hash(img)
    assert sw._diff_magnitude(a, b) < sw.FRAME_CHANGE_THRESHOLD
```

- [ ] **Step 5: Запустить — убедиться, что падает**

Run: `python -m pytest test_screen_watcher.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'screen_watcher'`

- [ ] **Step 6: Реализовать хэш/дифф**

`eva_agent/screen_watcher.py` (начало файла):
```python
"""Двухъярусная картина экрана (непрерывное осознание).

Ярус 1 (дёшево, всегда): фоновый поток захватывает уменьшенный кадр ~раз в
секунду и сравнивает перцептивные хэши; отслеживает активное окно через
win32gui. Без LLM.
Ярус 2 (дорого, по требованию): describe_screen() — подпись экрана; свежая
из кэша, иначе живой взгляд через actions.look_at_screen.
"""
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

from config import config

FRAME_CHANGE_THRESHOLD = 16  # хэмминг dHash 16x16 — сколько бит считаем «изменением»
CAPTION_DEFAULT_QUESTION = "Что открыто на экране и в каком порядке?"


@dataclass
class ScreenState:
    """Снимок текущей картины экрана. Хранится только в памяти."""
    active_window: dict = field(default_factory=dict)   # {title, class, hwnd, rect}
    last_change_ts: Optional[float] = None
    last_magnitude: int = 0
    caption: str = ""                                   # подпись яруса 2
    caption_ts: Optional[float] = None
    caption_question: str = ""
    mode: str = "normal"                                # normal | locked
    events: deque = field(default_factory=lambda: deque(maxlen=64))


def _fit_frame(img, max_w: int = 320):
    """Уменьшает кадр до ширины max_w (пропорционально)."""
    if img.width > max_w:
        h = int(img.height * max_w / img.width)
        img = img.resize((max_w, h))
    return img


def _frame_hash(img, size: int = 16) -> int:
    """dHash по серому: >= смещение пикселя по строке даёт бит 1."""
    g = img.convert("L").resize((size + 1, size))
    px = list(g.getdata())
    h = 0
    for row in range(size):
        for col in range(size):
            h <<= 1
            if px[row * (size + 1) + col] > px[row * (size + 1) + col + 1]:
                h |= 1
    return h


def _diff_magnitude(h1: int, h2: int) -> int:
    """Биты Хэмминга между двумя dHash."""
    return bin(h1 ^ h2).count("1")
```

- [ ] **Step 7: Запустить — тесты зелёные**

Run: `python -m pytest test_screen_watcher.py test_config.py -q`
Expected: PASS (5 тестов)

- [ ] **Step 8: Commit**

```bash
git add config.py  # только если config.py в git (уточнить: git ls-files eva_agent)
```
Файлы `eva_agent/` НЕ в git — коммит пропускается для них. Коммит серверных изменений (`brain bot/`) — в конце фичи.

---

### Task 2: активное окно + захват кадра + детект блокировки

**Files:**
- Modify: `eva_agent/screen_watcher.py` (добавить `_capture_frame`, `_is_locked_frame`, `_active_window_info`)
- Test: `eva_agent/test_screen_watcher.py`

**Interfaces:**
- Consumes: `_fit_frame`, `_frame_hash` из Task 1.
- Produces: `screen_watcher._capture_frame() -> tuple[Optional[Image], Optional[str]]` (img, error), `screen_watcher._is_locked_frame(img) -> bool`, `screen_watcher._active_window_info() -> dict` (`{title, class, hwnd, rect}`).

- [ ] **Step 1: Тесты захвата/окна**

```python
def test_is_locked_frame_detects_black(monkeypatch):
    black = Image.new("L", (320, 180), 0)
    assert sw._is_locked_frame(black)


def test_is_locked_frame_false_on_content(monkeypatch):
    img = Image.new("L", (320, 180), 128)
    assert not sw._is_locked_frame(img)


def test_active_window_info(monkeypatch):
    fake = {"GetForegroundWindow": lambda: 5,
            "GetWindowText": lambda h: "Блокнот",
            "GetClassName": lambda h: "Notepad",
            "GetWindowRect": lambda h: (0, 0, 800, 600)}
    monkeypatch.setattr("win32gui", _FakeWin32(fake))
    info = sw._active_window_info()
    assert info["title"] == "Блокнот" and info["class"] == "Notepad" and info["rect"] == [0, 0, 800, 600]
```
Хелпер для мока (в начале тестового файла):
```python
class _FakeWin32:
    def __init__(self, funcs): self._f = funcs
    def __getattr__(self, name):
        if name in self._f:
            return self._f[name]
        raise AttributeError(name)
```
(win32gui недоступен/не установлен — мокается целиком на уровне модуля через `monkeypatch.setattr("screen_watcher.win32gui", ...)` с `sys.modules` подменой; фактически `_active_window_info` импортирует win32gui внутри функции, поэтому мокаем атрибут модуля `screen_watcher.win32gui`.)

- [ ] **Step 2: Запустить — падает**

Run: `python -m pytest test_screen_watcher.py -q`
Expected: FAIL — `AttributeError: ... _is_locked_frame`

- [ ] **Step 3: Реализация**

Добавить в `screen_watcher.py`:
```python
def _capture_frame():
    """Скриншот → уменьшенный кадр. Возвращает (img|None, error|None)."""
    try:
        from PIL import ImageGrab
        return _fit_frame(ImageGrab.grab()), None
    except Exception as e:
        return None, str(e)


def _is_locked_frame(img) -> bool:
    """Чёрный/тёмный экран — заблокирован или пуст. Однотонный яркий кадр
    (например, пустой белый документ) замком не считается."""
    try:
        small = img.convert("L").resize((8, 8))
        vals = list(small.getdata())
        avg = sum(vals) / len(vals)
        spread = max(vals) - min(vals)
        return avg < 12 or (spread < 6 and avg < 60)
    except Exception:
        return True


def _active_window_info() -> dict:
    """Активное окно через win32gui: title, class, hwnd, rect."""
    try:
        import win32gui
        hwnd = win32gui.GetForegroundWindow()
        return {
            "title": win32gui.GetWindowText(hwnd),
            "class": win32gui.GetClassName(hwnd),
            "hwnd": hwnd,
            "rect": list(win32gui.GetWindowRect(hwnd)),
        }
    except Exception:
        return {}
```
Тест на `_active_window_info` мокает `screen_watcher.win32gui`; из-за `import win32gui` внутри функции мок через `monkeypatch.setitem(sys.modules, "win32gui", fake)` или `monkeypatch.setattr("screen_watcher.win32gui", fake)` (после того как win32gui уже импортирован в screen_watcher). Положите перед вызовом `monkeypatch.setitem(sys.modules, "win32gui", fake)`.

- [ ] **Step 4: Запустить — зелёные**

Run: `python -m pytest test_screen_watcher.py -q`
Expected: PASS

---

### Task 3: ScreenWatcher — поток, singleton, публичное API

**Files:**
- Modify: `eva_agent/screen_watcher.py` (класс `ScreenWatcher`, `start_screen_watcher`, `get_screen_state`, `describe_screen`, `_refresh_caption`)
- Test: `eva_agent/test_screen_watcher.py`

**Interfaces:**
- Consumes: `_capture_frame`, `_is_locked_frame`, `_active_window_info`, `_frame_hash`, `_diff_magnitude`, `ScreenState`, `config.caption_ttl/caption_refresh_s/screen_frame_interval`.
- Produces:
  - `ScreenWatcher.start()/stop()`; `ScreenWatcher.get_state() -> dict`; `ScreenWatcher.describe_screen(question: str) -> str`; `ScreenWatcher.on_change(cb: Callable[[str], None])`;
  - модульные `start_screen_watcher() -> ScreenWatcher`, `get_screen_state() -> dict`, `describe_screen(question: str = CAPTION_DEFAULT_QUESTION) -> str`.

- [ ] **Step 1: Тесты цикла и API**

```python
def test_loop_records_change_and_active_window(monkeypatch):
    from screen_watcher import ScreenWatcher
    watcher = ScreenWatcher()
    watcher._frame_capture = lambda: (Image.new("L", (320, 180), 100), None)  # стабильный кадр
    watcher._is_locked = lambda img: False
    watcher._compute = lambda img: (0b0000000000000000_0000000000000000, sw.FRAME_CHANGE_THRESHOLD)
    watcher._win_info = lambda: {"title": "Блокнот", "class": "Notepad", "hwnd": 1, "rect": [0, 0, 1, 1]}
    events = []
    watcher.on_change(lambda e: events.append(e))
    watcher._process_once()   # первый прогон: хэш None → запишет, сменит фокус
    watcher._process_once()   # второй прогон: той же кадр → изменений нет
    state = watcher.get_state()
    assert state["active_window"]["title"] == "Блокнот"
    assert "focus" in events
    assert state["recent_events"][-1]["t"] == "focus"


def test_describe_screen_uses_cache_when_fresh(monkeypatch):
    from screen_watcher import ScreenWatcher
    watcher = ScreenWatcher()
    # ВАЖНО: писать в watcher.state напрямую — get_state() возвращает копию
    # (новый dict), запись в неё не дойдёт до self.state.
    watcher.state.caption = "старая подпись"
    watcher.state.caption_ts = time.time()  # свежая
    calls = []
    watcher._refresh_caption = lambda q: calls.append(q) or "новое"
    out = watcher.describe_screen("вопрос")
    assert out == "старая подпись" and calls == []


def test_describe_screen_live_when_stale(monkeypatch):
    from screen_watcher import ScreenWatcher
    watcher = ScreenWatcher()
    watcher.state.caption_ts = time.time() - 999  # протухла
    watcher._refresh_caption = lambda q: "живой взгляд"
    assert watcher.describe_screen("вопрос") == "живой взгляд"
```

Примечание: чтобы тесты не трогали реальный экран, `ScreenWatcher._process_once` (вытащенный из `_loop` шаг) должен быть отдельным методом, который использует инжектируемые `_frame_capture/_is_locked/_compute/_win_info`. Конструктор принимает эти хуки с дефолтами = реальные функции.

- [ ] **Step 2: Запустить — падает**

Run: `python -m pytest test_screen_watcher.py -q`
Expected: FAIL — `ImportError: cannot import name 'ScreenWatcher'`

- [ ] **Step 3: Класс и loop**

Добавить в `screen_watcher.py`:
```python
class ScreenWatcher:
    def __init__(self, frame_capture=None, is_locked=None, compute=None, win_info=None):
        self.state = ScreenState()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._callbacks: list[Callable[[str], None]] = []
        self._last_hash: Optional[int] = None
        self._last_focus_key: Optional[tuple] = None
        self._last_refresh_ts: Optional[float] = None
        self._refresh_lock = threading.Lock()
        # инжектируемые хуки (для тестов)
        from PIL import Image
        self._frame_capture = frame_capture or _capture_frame
        self._is_locked = is_locked or _is_locked_frame
        self._compute = compute or (lambda img: (_frame_hash(img), None))
        self._win_info = win_info or _active_window_info

    def on_change(self, callback: Callable[[str], None]) -> None:
        self._callbacks.append(callback)

    def _emit(self, event: str) -> None:
        for cb in self._callbacks:
            try:
                cb(event)
            except Exception:
                pass

    def _process_once(self) -> None:
        img, err = self._frame_capture()
        if err or img is None:
            return
        locked = self._is_locked(img)
        if locked:
            self.state.mode = "locked"
            return
        if self.state.mode == "locked":  # разблокировался
            self.state.mode = "normal"
            self._emit("unlock")
        h, _mag = self._compute(img)
        if self._last_hash is None:
            self._last_hash = h
        else:
            mag = _diff_magnitude(self._last_hash, h)
            self._last_hash = h
            if mag >= FRAME_CHANGE_THRESHOLD:
                self.state.last_change_ts = time.time()
                self.state.last_magnitude = mag
                self.state.events.append({"t": "change", "ts": self.state.last_change_ts, "mag": mag})
                self._emit("change")
        focus = self._win_info()
        key = (focus.get("title"), focus.get("class"))
        if key and key[0] and key != self._last_focus_key:
            self._last_focus_key = key
            self.state.active_window = focus
            self.state.events.append({"t": "focus", "ts": time.time(), "title": focus.get("title")})
            self._emit("focus")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._process_once()
                if (self._last_refresh_ts is None
                        or time.time() - self._last_refresh_ts >= config.caption_refresh_s):
                    self._last_refresh_ts = time.time()
                    self.describe_screen(CAPTION_DEFAULT_QUESTION)
            except Exception:
                pass
            time.sleep(config.screen_frame_interval)

    def start(self) -> "ScreenWatcher":
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="screen-watcher")
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def get_state(self) -> dict:
        """Публичный снимок для мозга/UI — без скриншота и VLM."""
        return {
            "active_window": self.state.active_window,
            "last_change_ts": self.state.last_change_ts,
            "last_magnitude": self.state.last_magnitude,
            "mode": self.state.mode,
            "caption": self.state.caption,
            "caption_ts": self.state.caption_ts,
            "recent_events": list(self.state.events)[-20:],
        }

    def describe_screen(self, question: str = CAPTION_DEFAULT_QUESTION) -> str:
        """Ярус 2: свежая подпись из кэша, иначе живой взгляд (VLM)."""
        now = time.time()
        if self.state.caption_ts is not None and now - self.state.caption_ts <= config.caption_ttl:
            return self.state.caption
        return self._refresh_caption(question)

    def _refresh_caption(self, question: str) -> str:
        with self._refresh_lock:
            import actions
            status, text = actions.look_at_screen(question)
            if status == "done" and text:
                self.state.caption = text
                self.state.caption_ts = time.time()
                self.state.caption_question = question
            return text


_watcher: Optional[ScreenWatcher] = None


def start_screen_watcher() -> ScreenWatcher:
    global _watcher
    if _watcher is None:
        _watcher = ScreenWatcher().start()
    return _watcher


def get_screen_state() -> dict:
    if _watcher is None:
        return {"active_window": {}, "mode": "normal", "caption": ""}
    return _watcher.get_state()


def describe_screen(question: str = CAPTION_DEFAULT_QUESTION) -> str:
    return start_screen_watcher().describe_screen(question)
```

- [ ] **Step 4: Запустить — зелёные**

Run: `python -m pytest test_screen_watcher.py -q`
Expected: PASS

---

### Task 4: watchers — правила, JSON-хранилище, чистая оценка

**Files:**
- Create: `eva_agent/watchers.py` (часть: `WatchRule`, `WatchStore`, `_target_matches`, `evaluate_rules`)
- Test: `eva_agent/test_watchers.py`

**Interfaces:**
- Produces: `watchers.WatchRule` (target, when, what, capture, id, created, cooldown_until), `watchers.WatchStore(path)` c `.load()/.save()/.add(rule)/.remove(target)/.list()`, `watchers._target_matches(state: dict, target: str, capture: str) -> bool`, `watchers.evaluate_rules(rules: list[WatchRule], event: str, state: dict, memory: dict, now: float) -> tuple[list[WatchRule], dict]`.

- [ ] **Step 1: Тесты**

```python
import time
from watchers import WatchRule, WatchStore, _target_matches, evaluate_rules, RULES_PATH


def rule(**kw):
    base = {"target": "курс валют", "when": "change", "what": "курс обновился", "capture": "title"}
    base.update(kw)
    return WatchRule(**base)


def test_target_matches_title_and_class():
    st = {"active_window": {"title": "Блокнот — курс валют", "class": "Chrome_WidgetWin_1"}}
    assert _target_matches(st, "курс валют", "title")
    assert _target_matches(st, "валю", "title")          # подстрока
    assert not _target_matches(st, "ютуб", "title")
    assert _target_matches({"active_window": {"class": "Notepad", "title": "x"}}, "notepad", "title")


def test_change_fires_only_when_caption_differs():
    st = {"active_window": {"title": "курс валют", "class": "Chrome"},
          "mode": "normal", "caption": "курс 90.5"}
    fired1, mem1 = evaluate_rules([rule()], "change", st, {}, 0.0)
    assert fired1 == []                                  # первый раз — это снапшот, не событие
    fired2, mem2 = evaluate_rules([rule()], "change", st, mem1, 0.0)
    assert fired2 == []
    st2 = {**st, "caption": "курс 91.2"}
    fired3, mem3 = evaluate_rules([rule()], "change", st2, mem2, 0.0)
    assert len(fired3) == 1 and fired3[0].target == "курс валют"
```

- [ ] **Step 2: Запустить — падает**

Run: `python -m pytest test_watchers.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'watchers'`

- [ ] **Step 3: Реализация**

```python
"""Наблюдатели «следить за конкретным»: правила, оценка, уведомление."""
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

RULES_PATH = Path(__file__).parent / "screen_watch_rules.json"


@dataclass
class WatchRule:
    target: str
    when: str = "change"          # change | appear | disappear
    what: str = ""
    capture: str = "title"        # title | screen
    id: str = ""
    created: str = ""
    cooldown_until: Optional[float] = None


class WatchStore:
    def __init__(self, path=RULES_PATH):
        self.path = path

    def load(self) -> list[WatchRule]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8")) or []
        except Exception:
            return []
        return [WatchRule(**{k: r.get(k) for k in ("target", "when", "what", "capture", "id", "created", "cooldown_until") if k in r})
                for r in raw]

    def save(self, rules: list[WatchRule]) -> None:
        self.path.write_text(json.dumps([asdict(r) for r in rules], ensure_ascii=False, indent=2), encoding="utf-8")

    def add(self, rules: list[WatchRule], target: str, when: str, what: str) -> WatchRule:
        r = WatchRule(target=target, when=when, what=what,
                      id=uuid.uuid4().hex[:8], created=datetime.now().isoformat(timespec="seconds"))
        rules.append(r)
        self.save(rules)
        return r

    def remove(self, rules: list[WatchRule], target: str) -> bool:
        before = len(rules)
        rules[:] = [r for r in rules if r.target != target]
        if len(rules) != before:
            self.save(rules)
        return len(rules) != before


def _target_matches(state: dict, target: str, capture: str) -> bool:
    aw = state.get("active_window") or {}
    hay = " ".join([aw.get("title", ""), aw.get("class", "")]).lower()
    needle = (target or "").strip().lower()
    return bool(needle) and needle in hay


def evaluate_rules(rules: list[WatchRule], event: str, state: dict, memory: dict, now: float):
    """Чистая функция: по событию яруса 1 (change/focus) возвращает
    (сработавшие правила, обновлённый memory). memory = {id: last_caption}.

    Маппинг событий из яруса 1: when='change' → событие 'change';
    'appear' → 'focus' (окно с target появилось/стало активным);
    'disappear' → 'focus'/'change' при отсутствии target-окна. Cooldown
    проверяется по now. 'change' требует, чтобы подпись отличалась от
    последнего снапшота; первый снапшот — это база, не событие.
    """
    fired = []
    for r in rules:
        expected = "change" if r.when == "change" else ("focus" if r.when == "appear" else "disappear")
        if event != expected and not (r.when == "disappear" and event in ("focus", "change")):
            continue
        if r.cooldown_until and now < r.cooldown_until:
            continue
        if r.when == "disappear":
            titles = " ".join(state.get("window_titles", [])).lower()
            if (r.target or "").lower() in titles:
                continue
            if not _target_matches(state, r.target, r.capture):
                fired.append(r)
            continue
        if not _target_matches(state, r.target, r.capture):
            if r.when == "appear":
                continue
        if r.when == "change":
            last = memory.get(r.id)
            cap = state.get("caption", "") or ""
            if not cap:
                continue
            if last is None:
                memory[r.id] = cap  # первый снапшот — база, не событие
                continue
            if cap == last:
                continue
            memory[r.id] = cap
            fired.append(r)
        else:  # appear
            fired.append(r)
    return fired, memory
```

- [ ] **Step 4: Test disappear + cooldown + store**

```python
def test_disappear_fires_when_window_gone():
    st = {"window_titles": [], "active_window": {"title": "другое окно"}}
    fired, _ = evaluate_rules([rule(when="disappear")], "focus", st, {}, 0.0)
    assert len(fired) == 1


def test_disappear_silent_while_window_present():
    st = {"window_titles": ["курс валют"], "active_window": {"title": "курс валют"}}
    fired, _ = evaluate_rules([rule(when="disappear")], "focus", st, {}, 0.0)
    assert fired == []


def test_change_cooldown_blocks_repeat():
    r = rule()
    st = {"active_window": {"title": "курс валют"}, "caption": "курс 95"}
    _, mem = evaluate_rules([r], "change", st, {}, 0.0)
    st2 = {**st, "caption": "курс 96"}
    fired, _ = evaluate_rules([r], "change", st2, mem, 0.0)
    assert fired and fired[0] is r
    r.cooldown_until = 100.0
    st3 = {**st, "caption": "курс 97"}
    fired2, _ = evaluate_rules([r], "change", st3, mem, 50.0)
    assert fired2 == []


def test_store_round_trip(tmp_path):
    store = WatchStore(tmp_path / "rules.json")
    rules = []
    store.add(rules, "ютуб", "appear", "открылся ютуб")
    rules2 = store.load()
    assert rules2[0].target == "ютуб" and rules2[0].when == "appear"
    assert store.remove(store.load(), "ютуб")
```

- [ ] **Step 5: Запустить — зелёные**

Run: `python -m pytest test_watchers.py -q`
Expected: PASS

---

### Task 5: WatchManager — поток оценки + регион-взгляд + уведомление (клиент)

**Files:**
- Modify: `eva_agent/watchers.py` (добавить `_describe_window`, `WatchManager`, `start_watch_manager`)
- Modify: `eva_agent/ws_client.py` (добавить `notify_owner`)
- Test: `eva_agent/test_watchers.py`, `eva_agent/test_ws_client_handlers.py`

**Interfaces:**
- Consumes: `evaluate_rules`, `WatchStore`, `WatchRule` (Task 4); `screen_watcher.describe_screen/get_screen_state` (Task 3); `config.watch_cooldown_s`.
- Produces:
  - `watchers.WatchManager(screen, store=None, notify=None, worker_delay=1.0)` с `.on_event(event)`, `.start()`, `.stop()`;
  - `watchers.start_watch_manager(screen) -> WatchManager`;
  - `ws_client.notify_owner(text: str) -> None` — UI-push + `agent_notify` на сервер.

- [ ] **Step 1: Тест менеджера (оценка + cooldown записывается)**

```python
import time
from watchers import WatchManager, WatchStore
from config import config


def make_screen(active_title, caption, titles):
    class S:
        def get_state(self):
            return {"active_window": {"title": active_title, "class": "Chrome"},
                    "mode": "normal", "caption": caption,
                    "window_titles": titles, "recent_events": []}
        def describe_screen(self, q): return caption
    return S()


def test_manager_appear_fires_notification(tmp_path):
    store = WatchStore(tmp_path / "r.json")
    rules = []
    store.add(rules, "ютуб", "appear", "открылся ютуб")
    screen = make_screen("ютуб — главная", "", [])
    notified = []
    m = WatchManager(screen, store=store, notify=lambda t: notified.append(t))
    m.on_event("focus")
    m._drain()
    assert notified == ["открылся ютуб"]


def test_manager_silent_when_window_absent(tmp_path):
    store = WatchStore(tmp_path / "r2.json")
    rules = []
    store.add(rules, "ютуб", "appear", "открылся ютуб")
    screen = make_screen("Блокнот", "", [])
    notified = []
    m = WatchManager(screen, store=store, notify=lambda t: notified.append(t))
    m.on_event("focus")
    m._drain()
    assert notified == []
```
_(Менеджер в тестах запускается без потока — для оценки вызывается `_drain()` напрямую. Хук `store_text` — тестовый доступ к последнему состоянию.)_

- [ ] **Step 2: Запустить — падает**

Run: `python -m pytest test_watchers.py -q`
Expected: FAIL — `Cannot import WatchManager`

- [ ] **Step 3: Реализация менеджера**

Добавить в `watchers.py`:
```python
def _describe_window(rect, question="Что видно в этом окне? Опиши кратко.") -> str:
    """Селективный взгляд на region окна (для change-правил)."""
    try:
        from PIL import ImageGrab
        import base64, io
        img = ImageGrab.grab(bbox=tuple(rect))
        img = img.convert("RGB")
        if img.width > 640:
            h = int(img.height * 640 / img.width)
            img = img.resize((640, h))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        import actions
        return actions._ask_model_about_image(question, base64.b64encode(buf.getvalue()).decode("ascii"))
    except Exception:
        return ""


class WatchManager:
    def __init__(self, screen, store=None, notify=None, worker_delay=1.0):
        self.screen = screen
        self.store = store or WatchStore()
        self.notify = notify or (lambda t: None)
        self.worker_delay = worker_delay
        self._rules = self.store.load()
        self._memory: dict = {}
        self._events = deque()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def on_event(self, event: str) -> None:
        self._events.append(event)

    def _drain(self) -> list[WatchRule]:
        fired_all = []
        while self._events:
            event = self._events.popleft()
            self._rules = self.store.load()  # свежие правила с диска
            state = self.screen.get_state()
            if event in ("change", "focus"):
                state["window_titles"] = _all_window_titles()
            # change-правила: свежая подпись активного окна
            need_caption = any(r.when == "change" for r in self._rules)
            if need_caption:
                aw = state.get("active_window") or {}
                if aw.get("rect"):
                    cap = _describe_window(aw["rect"])
                    if cap:
                        state["caption"] = cap
            fired, self._memory = evaluate_rules(self._rules, event, state, self._memory, time.time())
            for r in fired:
                r.cooldown_until = time.time() + config.watch_cooldown_s
                self._rules = [rr if rr.id != r.id else r for rr in self._rules]
                self.store.save(self._rules)
                self.notify(r.what or f"Заметил: {r.target} ({r.when})")
                fired_all.append(r)
        return fired_all

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._drain()
            except Exception:
                pass
            time.sleep(self.worker_delay)

    def start(self) -> "WatchManager":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="watch-manager")
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()


def _all_window_titles() -> list[str]:
    try:
        import actions
        return actions.list_window_titles()
    except Exception:
        return []


def start_watch_manager(screen) -> WatchManager:
    """Единственный общий менеджер (по процессу): и старт приложения, и
    действие start_watch пользуются одним экземпляром — так notify из
    app.py применяется к тому же менеджеру, что подписан на события."""
    global _shared_manager
    if _shared_manager is None:
        m = WatchManager(screen)
        screen.on_change(m.on_event)
        m.start()
        _shared_manager = m
    return _shared_manager
```
Импорты в начало `watchers.py`: `from collections import deque`, `import threading`; модульную глобальность `_shared_manager: Optional[WatchManager] = None` объявить рядом с `RULES_PATH`.

- [ ] **Step 4: `ws_client.notify_owner`**

В `ws_client.py`, после `submit_chat` (строка ~377) добавить:
```python
def notify_owner(text: str) -> None:
    """Уведомление владельцу: в UI приложения + в Telegram через сервер
    (тип agent_notify, обрабатывает eva_ws на сервере)."""
    push_ui_message("eva", f"🗔 {text}")
    if _loop is None or _current_ws is None:
        return

    async def _send():
        try:
            await _current_ws.send(json.dumps({"type": "agent_notify", "text": text}))
        except Exception as e:
            _log(f"⚠️ Не удалось отправить уведомление: {e}", "error")

    asyncio.run_coroutine_threadsafe(_send(), _loop)
```

- [ ] **Step 5: Тест notify_owner**

`test_ws_client_handlers.py`:
```python
def test_notify_owner_pushes_ui(monkeypatch):
    import ws_client
    pushed = []
    monkeypatch.setattr(ws_client, "push_ui_message", lambda role, t: pushed.append((role, t)))
    monkeypatch.setattr(ws_client, "_loop", None)
    monkeypatch.setattr(ws_client, "_current_ws", None)
    ws_client.notify_owner("курс обновился")
    assert any("курс" in t for _r, t in pushed)
```
_(С async-отправкой — тестировать через прямой вызов корутины в отдельном тесте асинка; здесь достаточно UI-ветки.)_

- [ ] **Step 6: Запустить — зелёные**

Run: `python -m pytest test_watchers.py test_ws_client_handlers.py -q`
Expected: PASS

---

### Task 6: сервер — agent_notify → Telegram (brain bot)

**Files:**
- Modify: `brain bot/eva_ws.py:93-108` (в `_handle_agent_message` добавить ветку `agent_notify`)
- Test: `brain bot/test_agent_messages.py`

**Interfaces:**
- Consumes: `get_owner_id_for_agent` и `notify_app` из `eva_agent_registry` (уже импортированы в `eva_ws.py:35-37`).

- [ ] **Step 1: Тест**

`test_agent_messages.py` (добавить рядом с существующими):
```python
@pytest.mark.asyncio
async def test_agent_notify_sends_telegram_to_owner(monkeypatch):
    from eva_ws import _handle_agent_message
    from eva_agent_registry import register_agent, unregister_agent
    from chat_state import new_message_id
    bot = FakeBot()
    ws = FakeWS()
    register_agent("p1", ws, owner_id=111)
    try:
        await _handle_agent_message(
            '{"type": "agent_notify", "text": "курс доллара обновился"}', bot, "p1", ws
        )
        assert any(s["chat_id"] == 111 and "курс доллара" in s["text"] for s in bot.sent)
    finally:
        unregister_agent("p1")
```

- [ ] **Step 2: Запустить — падает**

Run: `cd "C:\bat\projects\Eva\brain bot" && python -m pytest test_agent_messages.py -q`
Expected: FAIL — тест не найдет уведомление (ветки нет)

- [ ] **Step 3: Реализация**

В `eva_ws.py`, после ветки `chat` (строка ~125, перед комментарием «неизвестные типы») добавить:
```python
    elif msg_type == "agent_notify":
        from eva_agent_registry import notify_app
        text = data.get("text", "")
        owner_id = get_owner_id_for_agent(agent_id)
        if owner_id is None or not text:
            return
        try:
            await bot.send_message(owner_id, f"🗔 Ева заметила: {text}")
        except Exception as e:
            print(f"⚠️ Не удалось отправить agent_notify в Telegram: {e}", flush=True)
        await notify_app(owner_id, "eva", text)
```

- [ ] **Step 4: Запустить — зелёные**

Run: `python -m pytest test_agent_messages.py -q`
Expected: PASS (включая старые тесты)

- [ ] **Step 5: Commit**

```bash
git add eva_ws.py test_agent_messages.py
git commit -m "feat(server): agent_notify — уведомления агента владельцу в Telegram и приложение"
```

---

### Task 7: actions.py — get_screen_state + watchdog-действия

**Files:**
- Modify: `eva_agent/actions.py` (добавить `get_screen_state()`, `start_watch()`, `stop_watch()`, `list_watches()`; расширить `execute_action`)
- Test: `eva_agent/test_actions_ui.py` (или новый `test_actions_screen.py`)

**Interfaces:**
- Consumes: `screen_watcher.get_screen_state` (Task 3), `watchers.WatchStore`/`start_watch_manager`/`WatchManager` (Task 4-5), `config`.
- Produces: `actions.execute_action("get_screen_state"|"start_watch"|"stop_watch"|"list_watches", params) -> (status, text, None)`.

- [ ] **Step 1: Тесты**

`eva_agent/test_actions_screen.py`:
```python
import actions


def test_execute_get_screen_state(monkeypatch):
    fake = {"active_window": {"title": "Блокнот"}, "mode": "normal"}
    monkeypatch.setattr("actions.get_screen_state", lambda: ("done", f"Активно: Блокнот"))
    status, text, img = actions.execute_action("get_screen_state", {})
    assert status == "done" and "Блокнот" in text and img is None


def test_execute_start_watch(monkeypatch):
    calls = {}
    monkeypatch.setattr("actions.start_watch",
                        lambda p: (calls.update(p), ("done", "Наблюдатель создан"))[1])
    status, text, img = actions.execute_action("start_watch", {"target": "ютуб", "when": "appear", "what": "открылся ютуб"})
    assert status == "done" and calls == {"target": "ютуб", "when": "appear", "what": "открылся ютуб"}
```

- [ ] **Step 2: Запустить — падает**

Run: `python -m pytest test_actions_screen.py -q`
Expected: FAIL — `Unknown action: get_screen_state`

- [ ] **Step 3: Реализация**

В `actions.py` (рядом с `look_at_screen`) добавить:
```python
# ----- Осознание экрана: состояние + наблюдатели -----
def get_screen_state() -> tuple[str, str]:
    """Мгновенная картина экрана (без скриншота и VLM)."""
    from screen_watcher import get_screen_state as _gs
    st = _gs()
    aw = st.get("active_window") or {}
    title = aw.get("title") or "—"
    mode = st.get("mode", "normal")
    if mode == "locked":
        return "done", "Экран заблокирован или выключен."
    recent = st.get("recent_events") or []
    changes = [e for e in recent if e.get("t") == "change"]
    line = f"Активно на экране: {title}."
    if changes:
        line += f" Недавно менялось (последнее {len(changes)} изменений за {len(recent)} событий)."
    return "done", line


def start_watch(params: dict) -> tuple[str, str]:
    from watchers import WatchStore, start_watch_manager
    store = WatchStore()
    rules = store.load()
    target = (params.get("target") or "").strip()
    if not target:
        return "error", "Не указано, за чем следить (target)."
    when = params.get("when") or "change"
    if when not in ("change", "appear", "disappear"):
        return "error", f"Неизвестный режим when={when}. Нужен change|appear|disappear."
    what = params.get("what") or f"[{when}] {target}"
    r = store.add(rules, target, when, what)
    try:
        from screen_watcher import start_screen_watcher
        start_watch_manager(start_screen_watcher())
    except Exception:
        pass  # менеджер поднимется при старте приложения — ок
    return "done", f"Слежу за «{target}» ({when}). Сообщу: {what}"


def stop_watch(params: dict) -> tuple[str, str]:
    from watchers import WatchStore
    store = WatchStore()
    target = (params.get("target") or "").strip()
    if store.remove(store.load(), target):
        return "done", f"Наблюдение за «{target}» остановлено."
    return "done", f"Наблюдение за «{target}» не найдено."


def list_watches(params: dict) -> tuple[str, str]:
    from watchers import WatchStore
    rules = WatchStore().load()
    if not rules:
        return "done", "Активных наблюдений нет."
    lines = [f"- {r.target} ({r.when}): {r.what}" for r in rules]
    return "done", "Наблюдаю за:\n" + "\n".join(lines)
```
В `execute_action`, перед `else:` (после ветки `look_at_screen`, строка ~1143) добавить ветки:
```python
    elif action == "get_screen_state":
        status, text = get_screen_state()
    elif action == "start_watch":
        status, text = start_watch(params)
    elif action == "stop_watch":
        status, text = stop_watch(params)
    elif action == "list_watches":
        status, text = list_watches(params)
```
(имена функций совпадают с моками в тестах — `actions.get_screen_state` и т.д.)

- [ ] **Step 4: Запустить — зелёные**

Run: `python -m pytest test_actions_screen.py test_actions_ui.py -q`
Expected: PASS

---

### Task 8: local_brain — whitelist + примеры

**Files:**
- Modify: `eva_agent/local_brain.py:38-69` (`_LOCAL_ACTIONS`), `:209-217` (примеры), возможно `:218-229` (правила)
- Test: `eva_agent/test_local_brain.py`

- [ ] **Step 1: Тест whitelist**

Добавить в `test_local_brain.py`:
```python
def test_screen_actions_in_whitelist():
    import local_brain as lb
    for name in ("get_screen_state", "start_watch", "stop_watch", "list_watches"):
        assert f"- {name}: " in lb._LOCAL_ACTIONS, f"{name} отсутствует в whitelist"


def test_screen_example_in_prompt():
    import local_brain as lb
    assert "get_screen_state" in lb._system_prompt(""")
```

- [ ] **Step 2: Запустить — падает**

Run: `python -m pytest test_local_brain.py -q`
Expected: FAIL

- [ ] **Step 3: Реализация**

В `_LOCAL_ACTIONS` добавить строки (после `look_at_screen`, строка 41):
```python
- get_screen_state: {} — спросить: что сейчас активно на экране (окно, режим), мгновенно, без скриншота
- start_watch: {"target": "курс валют", "when": "change|appear|disappear", "what": "что сообщить"} — следить за окном и уведомить при событии
- stop_watch: {"target": "курс валют"} — перестать следить
- list_watches: {} — показать активные наблюдения
```
В `_system_prompt` примеры (после строки 217):
```python
"- «что сейчас открыто?» -> {"kind": "actions", "steps": [{"action": "get_screen_state", "params": {}, "description": "Посмотрю, что сейчас активно на экране"}]}\n"
"- «следи за курсом доллара на вкладке» -> {"kind": "actions", "steps": [{"action": "start_watch", "params": {"target": "курс доллара", "when": "change", "what": "курс доллара обновился"}, "description": "Начну следить за курсом доллара"}]}\n"
```
В список правил (после строки 229) добавить:
```python
        "- Если человек просит «смотреть/следить за чем-то постоянно» — start_watch "
        "(when change/appear/disappear), а не разовое look_at_screen.\n"
```

- [ ] **Step 4: Запустить — зелёные (все тесты local_brain + regression)**

Run: `python -m pytest test_local_brain.py -q`
Expected: PASS

---

### Task 9: ws_client — кэш-взгляд + инъекция окна в промпт

**Files:**
- Modify: `eva_agent/ws_client.py` (в `_resolve_local_plan` — кэш-путь; в `_handle_chat` — инъекция строки окна)
- Test: `eva_agent/test_ws_client_handlers.py`, `eva_agent/test_ws_client.py`

**Interfaces:**
- Consumes: `screen_watcher.describe_screen` (Task 3), `screen_watcher.get_screen_state` (Task 3). Требует выноса уже существующих в `_handle_chat`/`_resolve_local_plan` локальных импортов `from local_brain import build_plan, ensure_ollama_ready` наверх модуля — иначе их нельзя замокать как `ws_client.build_plan` (gotcha из памяти: by-name импорты).

- [ ] **Step 1: Тесты**

`test_ws_client_handlers.py`:
```python
import pytest


@pytest.mark.asyncio
async def test_look_at_screen_uses_cached_describe(monkeypatch):
    import ws_client

    seen = []
    async def fake_describe(question):
        seen.append(question)
        return "Свежая подпись"

    monkeypatch.setattr(ws_client, "describe_screen", fake_describe)
    monkeypatch.setattr(ws_client, "list_window_titles", lambda: ["Блокнот"])

    async def fake_replan(text, memory):
        assert "Свежая подпись" in text
        return {"kind": "answer", "text": "готово"}

    monkeypatch.setattr(ws_client, "build_plan", fake_replan)

    plan = {"kind": "actions", "steps": [{"action": "look_at_screen", "params": {"question": "Что?"}}]}
    out = await ws_client._resolve_local_plan(plan, "текст", "")
    assert out["kind"] == "answer"
    assert seen  # describe_screen реально вызван, а не execute_action('look_at_screen')


@pytest.mark.asyncio
async def test_handle_chat_injects_active_window(monkeypatch):
    import ws_client

    seen = {}
    def capture_built(text, memory):
        seen["text"] = text
        return {"kind": "answer", "text": "Привет"}

    monkeypatch.setattr(ws_client, "build_plan", capture_built)
    monkeypatch.setattr(ws_client, "ensure_ollama_ready", lambda: (True, "ок"))
    monkeypatch.setattr("ws_client.get_screen_state",
                        lambda: {"active_window": {"title": "Блокнот"}, "mode": "normal"})
    ws = FakeWS()
    await ws_client._handle_chat(ws, {"message_id": 1, "text": "привет", "origin": "app"})
    assert "Блокнот" in seen["text"]
```

- [ ] **Step 2: Запустить — падает**

Run: `python -m pytest test_ws_client_handlers.py -q`
Expected: FAIL — `ws_client.describe_screen` не существует / локальные импорты `build_plan` не мокаются

- [ ] **Step 3: Реализация**

В `ws_client.py`:
1) Вынести импорты `local_brain` наверх. Заменить строку 33 `from local_brain import classify_steps` на:
```python
from local_brain import build_plan, classify_steps, ensure_ollama_ready
```
и **удалить** локальные импорты внутри функций: в `_handle_chat` строка ~125 `from local_brain import build_plan, ensure_ollama_ready`, в `_resolve_local_plan` строка ~175 `from local_brain import build_plan`.

⚠️ Вынос импортов меняет мок-имена для уже существующих тестов в `test_ws_client_handlers.py`. Обновить их патчи (иначе `local_brain.build_plan` больше не достаёт до ссылки `ws_client.build_plan`):
- `test_handle_chat_replans_after_look_at_screen`: `monkeypatch.setattr("local_brain.ensure_ollama_ready", ...)` → `"ws_client.ensure_ollama_ready"`; `monkeypatch.setattr("local_brain.build_plan", fake_build_plan)` → `"ws_client.build_plan"`.
- `test_handle_chat_limits_look_loop`: то же самое для `ensure_ollama_ready` и `build_plan`.
2) Добавить импорт screen_watcher (после `from actions import ...`, строка 35):
```python
from screen_watcher import describe_screen, get_screen_state
```
3) В `_handle_chat` (после `memory_context = ...`, строка 129):
```python
    # Контекст для команд: активное окно из яруса 1 — без скриншота и VLM.
    try:
        _st = get_screen_state()
        _win = (_st.get("active_window") or {}).get("title")
        line = f"\n(на экране сейчас активно: {_win or 'ничего не видно'}; режим: {_st.get('mode', 'normal')})"
    except Exception:
        line = ""
```
и передать в `build_plan`: заменить `build_plan(text, memory_context)` (строка 147) на `build_plan(text + line, memory_context)`.

В `_resolve_local_plan`, заменить внутренний вызов `execute_action("look_at_screen", ...)` (строки 183-185) на:
```python
        for s in looks:
            question = (s.get("params") or {}).get("question", "Что открыто на экране?")
            seen = await asyncio.to_thread(describe_screen, question)
            _log(f"👀 Смотрю на экран: {seen[:120]}")
            seen_context.append(seen)
```

- [ ] **Step 4: Запустить — зелёные**

Run: `python -m pytest test_ws_client.py test_ws_client_handlers.py -q`
Expected: PASS

---

### Task 10: app.py — запуск watcher + менеджер + проводка notify

**Files:**
- Modify: `eva_agent/app.py` (`main()`)
- Test: `eva_agent/test_app_smoke.py` (лёгкий smoke)

**Interfaces:**
- Consumes: `screen_watcher.start_screen_watcher`, `watchers.start_watch_manager`, `ws_client.notify_owner`.

- [ ] **Step 1: Smoke-тест старта**

`eva_agent/test_app_smoke.py`:
```python
def test_start_screen_watcher_returns_running(monkeypatch):
    from screen_watcher import ScreenWatcher
    w = ScreenWatcher().start()
    assert w._thread is not None and w._thread.is_alive()
    w.stop()


def test_watch_manager_wiring(monkeypatch):
    from watchers import WatchManager
    from screen_watcher import ScreenWatcher
    screen = ScreenWatcher()
    m = WatchManager(screen, notify=lambda t: None)
    screen.on_change(m.on_event)
    assert screen._callbacks and screen._callbacks[0] is m.on_event
```
_(Дополнительно: не запускать реальный ImageGrab в smoke — конструктор ScreenWatcher использует хуки по умолчанию, поток стартует с daemon. Это ок для CI без монитора.)_

- [ ] **Step 2: Запустить — падает (нет модуля/импорта)**

Run: `python -m pytest test_app_smoke.py -q`
Expected: FAIL — неверные импорты (например `watch_manager`)

- [ ] **Step 3: Реализация**

В `app.py`, в `main()` после запуска фоновых мозговых потоков (после строки 72) добавить:
```python
    # Непрерывное осознание экрана: фоновая картина (ярус 1) + наблюдатели.
    from screen_watcher import start_screen_watcher
    from watchers import start_watch_manager
    watcher = start_screen_watcher()
    manager = start_watch_manager(watcher)
    manager.notify = ws_client.notify_owner
```

- [ ] **Step 4: Запустить — зелёные**

Run: `python -m pytest test_app_smoke.py -q`
Expected: PASS

- [ ] **Step 5: Полный прогон агентских тестов**

Run: `cd "C:\bat\projects\Eva\eva_agent" && python -m pytest -q`
Expected: PASS (все тесты: test_actions_ui, test_actions_screen, test_local_brain, test_screen_watcher, test_watchers, test_ws_client*, test_look_at_screen, test_config, test_app_smoke и предыдущие 49).

---

### Task 11: финальный коммит серверных файлов (документация + eva_ws уже закоммичен в Task 6)

**Files:**
- Modify: `brain bot/docs/superpowers/plans/2026-09-14-screen-awareness.md` (этот план), spec уже закоммичен (`0c06e6e`).

- [ ] **Step 1: Проверить статус**

Run: `cd "C:\bat\projects\Eva\brain bot" && git status`
Expected: только `eva_ws.py` (если не закоммичен) и `test_agent_messages.py`.

- [ ] **Step 2: Коммит плана**

```bash
git add docs/superpowers/plans/2026-09-14-screen-awareness.md
git commit -m "docs: план реализации непрерывного осознания экрана"
```

- [ ] **Step 3: Итог — что осталось вне кода**

Обновить память (файл `iteration1-local-brain-status.md` или новый memory) по итогам: фича реализована, нужен живой прогон (фоновое видео, `start_watch`, кэш-взгляд). Отметить, что агентские файлы не в git.