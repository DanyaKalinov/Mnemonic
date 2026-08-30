"""
Разбор текста команды Евы в структурированное действие для desktop-агента.

Как и reminder_parser.py — чистая функция без сети/БД, поэтому легко
покрывается юнит-тестами напрямую. Если ни один шаблон не подошёл — это
не обязательно ошибка: handlers.py на этот случай подключает свободный
разбор через Gemini (ai_core.interpret_eva_command), чтобы понимать более
вольные формулировки в рамках того же набора разрешённых действий.

Поддерживаемые действия:
- open_app      — «открой блокнот», «запусти калькулятор»
- open_website  — «открой сайт youtube.com», «зайди на ютуб.com»
- create_file   — «создай файл notes.txt с текстом купить молоко»
- read_file     — «прочитай файл notes.txt»
- delete_file   — «удали файл notes.txt»                    [подтверждение]
- edit_file     — «допиши в файл X текст ...» / «перепиши файл X текст ...»  [подтверждение]
- copy_file     — «скопируй файл X в загрузки»               [подтверждение]
- move_file     — «перенеси файл X в документы»              [подтверждение]
- list_files    — «покажи файлы в загрузках»
- search_file   — «найди файл отчёт»
- screenshot    — «сделай скриншот»
- set_volume    — «сделай громче/тише», «выключи звук», «громкость на 50»
- power_action  — «выключи компьютер», «перезагрузи пк», «заблокируй компьютер»  [подтверждение]

requires_confirmation=True — действие меняет/удаляет данные или влияет на
работу ПК целиком, поэтому сервер сначала спрашивает владельца в Telegram.
"""
import re
from dataclasses import dataclass, field
from typing import Optional

FILENAME_PATTERN = re.compile(r'[^\s]+\.\w+')
CONTENT_PATTERN = re.compile(
    r'(?:с\s+текстом|с\s+содержимым|текст[:\s])\s*(.+)$', re.IGNORECASE,
)

USAGE_ERROR = (
    "⚠️ Не понял команду. Примеры:\n"
    "• /eva открой блокнот\n"
    "• /eva открой сайт youtube.com\n"
    "• /eva создай файл notes.txt с текстом купить молоко\n"
    "• /eva прочитай файл notes.txt\n"
    "• /eva удали файл notes.txt\n"
    "• /eva сделай скриншот\n"
    "• /eva покажи файлы в загрузках\n"
    "• /eva найди файл отчёт\n"
    "• /eva сделай громче\n"
    "• /eva заблокируй компьютер\n"
    "• /eva напиши купить молоко\n"
    "• /eva нажми ctrl+s"
)
NO_FILENAME_ERROR = "⚠️ Не нашёл имя файла (нужно расширение, например notes.txt)."


@dataclass
class EvaCommandParseResult:
    action: Optional[str] = None
    params: dict = field(default_factory=dict)
    requires_confirmation: bool = False
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


# ----- Питание ПК -----
POWER_SHUTDOWN = re.compile(r'\bвыключи\b.*\b(компьютер|пк|ноутбук)\b', re.IGNORECASE)
POWER_RESTART = re.compile(r'\bперезагруз\w*\b.*\b(компьютер|пк|ноутбук)\b', re.IGNORECASE)
POWER_LOCK = re.compile(r'\bзаблокируй\b.*\b(компьютер|пк|ноутбук|экран)\b', re.IGNORECASE)

# ----- Скриншот -----
SCREENSHOT_PATTERN = re.compile(r'\bскриншот\b|сфотографируй\s+экран|сними\s+экран', re.IGNORECASE)

# ----- Список файлов -----
LIST_FILES_PATTERN = re.compile(r'\b(?:покажи|список)\s+файл\w*\s+в\s+(.+)$', re.IGNORECASE)
LIST_FILES_PATTERN2 = re.compile(r'\bчто\s+в\s+папке\s+(.+)$', re.IGNORECASE)

# ----- Громкость -----
VOLUME_SET_PATTERN = re.compile(r'громкост\w*\s+на\s+(\d{1,3})', re.IGNORECASE)
VOLUME_MUTE_PATTERN = re.compile(r'выключи\s+звук|замьют', re.IGNORECASE)
VOLUME_UNMUTE_PATTERN = re.compile(r'включи\s+звук', re.IGNORECASE)
VOLUME_UP_PATTERN = re.compile(r'\bгромче\b|прибавь\s+звук|звук\s+погромче', re.IGNORECASE)
VOLUME_DOWN_PATTERN = re.compile(r'\bтише\b|убавь\s+звук|звук\s+потише', re.IGNORECASE)

# ----- Сайты (проверяются раньше общего "открой", т.к. тоже используют это слово) -----
OPEN_SITE_PATTERN = re.compile(r'(?:открой|зайди на|перейди на)\s+сайт\s+(.+)$', re.IGNORECASE)
OPEN_SITE_DOMAIN_PATTERN = re.compile(
    r'(?:открой|зайди на|перейди на)\s+([a-zA-Zа-яА-Я0-9\-]+\.[a-zA-Zа-яА-Я]{2,})', re.IGNORECASE,
)

# ----- Копирование / перемещение -----
COPY_FILE_PATTERN = re.compile(r'скопируй\s+файл\s+(\S+)\s+в\s+(.+)$', re.IGNORECASE)
MOVE_FILE_PATTERN = re.compile(r'(?:перенеси|перемести)\s+файл\s+(\S+)\s+в\s+(.+)$', re.IGNORECASE)

# ----- Редактирование -----
APPEND_FILE_PATTERN = re.compile(r'допиши\s+в\s+файл\s+(\S+)\s+текст[:\s]+(.+)$', re.IGNORECASE)
OVERWRITE_FILE_PATTERN = re.compile(r'перепиши\s+файл\s+(\S+)\s+текст[:\s]+(.+)$', re.IGNORECASE)

# ----- Поиск -----
SEARCH_FILE_PATTERN = re.compile(r'найди\s+файл\s+(.+)$', re.IGNORECASE)

# ----- Печать текста и горячие клавиши (базовое управление внутри программ) -----
# Печатает в то окно, которое сейчас в фокусе — команду логично слать
# СЛЕДОМ за "открой <программа>", когда окно уже открылось и в фокусе.
TYPE_TEXT_PATTERN = re.compile(r'(?:напиши|напечатай|введи)\s+(.+)$', re.IGNORECASE)
PRESS_KEY_PATTERN = re.compile(r'нажми\s+(.+)$', re.IGNORECASE)


# ----- Составные команды: "открой блокнот и напиши привет" -----
# Разбивает одну фразу на несколько последовательных шагов по словам
# "и" / "потом" / "затем", если они встречаются как отдельное слово
# (а не как часть другого слова, например "найди" не разобьётся).
STEP_SEPARATOR_PATTERN = re.compile(r'\s+(?:и\s+затем|затем|потом|и)\s+', re.IGNORECASE)


def split_into_subcommands(text: str) -> list[str]:
    parts = STEP_SEPARATOR_PATTERN.split(text)
    return [p.strip(" ,.;") for p in parts if p.strip(" ,.;")]


def parse_eva_command(text: str) -> EvaCommandParseResult:
    normalized = text.strip().lower()

    # --- Питание ПК (проверяем раньше всего — явные, узкие фразы) ---
    if POWER_SHUTDOWN.search(normalized):
        return EvaCommandParseResult(action="power_action", params={"action": "shutdown"}, requires_confirmation=True)
    if POWER_RESTART.search(normalized):
        return EvaCommandParseResult(action="power_action", params={"action": "restart"}, requires_confirmation=True)
    if POWER_LOCK.search(normalized):
        return EvaCommandParseResult(action="power_action", params={"action": "lock"}, requires_confirmation=True)

    # --- Скриншот ---
    if SCREENSHOT_PATTERN.search(normalized):
        return EvaCommandParseResult(action="screenshot", params={}, requires_confirmation=False)

    # --- Список файлов в папке ---
    m = LIST_FILES_PATTERN.search(text) or LIST_FILES_PATTERN2.search(text)
    if m:
        return EvaCommandParseResult(action="list_files", params={"folder": m.group(1).strip()}, requires_confirmation=False)

    # --- Громкость ---
    m = VOLUME_SET_PATTERN.search(normalized)
    if m:
        return EvaCommandParseResult(action="set_volume", params={"level": int(m.group(1))}, requires_confirmation=False)
    if VOLUME_MUTE_PATTERN.search(normalized):
        return EvaCommandParseResult(action="set_volume", params={"direction": "mute"}, requires_confirmation=False)
    if VOLUME_UNMUTE_PATTERN.search(normalized):
        return EvaCommandParseResult(action="set_volume", params={"direction": "unmute"}, requires_confirmation=False)
    if VOLUME_UP_PATTERN.search(normalized):
        return EvaCommandParseResult(action="set_volume", params={"direction": "up"}, requires_confirmation=False)
    if VOLUME_DOWN_PATTERN.search(normalized):
        return EvaCommandParseResult(action="set_volume", params={"direction": "down"}, requires_confirmation=False)

    # --- Сайты (раньше общего "открой", то же триггерное слово) ---
    m = OPEN_SITE_PATTERN.search(text)
    if m:
        return EvaCommandParseResult(action="open_website", params={"url": m.group(1).strip()}, requires_confirmation=False)
    m = OPEN_SITE_DOMAIN_PATTERN.search(text)
    if m:
        return EvaCommandParseResult(action="open_website", params={"url": m.group(1).strip()}, requires_confirmation=False)

    # --- Копирование / перемещение (раньше общих файловых действий) ---
    m = COPY_FILE_PATTERN.search(text)
    if m:
        return EvaCommandParseResult(
            action="copy_file", params={"src": m.group(1).strip(), "dst": m.group(2).strip()}, requires_confirmation=True,
        )
    m = MOVE_FILE_PATTERN.search(text)
    if m:
        return EvaCommandParseResult(
            action="move_file", params={"src": m.group(1).strip(), "dst": m.group(2).strip()}, requires_confirmation=True,
        )

    # --- Редактирование ---
    m = APPEND_FILE_PATTERN.search(text)
    if m:
        return EvaCommandParseResult(
            action="edit_file", params={"path": m.group(1).strip(), "mode": "append", "content": m.group(2).strip()},
            requires_confirmation=True,
        )
    m = OVERWRITE_FILE_PATTERN.search(text)
    if m:
        return EvaCommandParseResult(
            action="edit_file", params={"path": m.group(1).strip(), "mode": "overwrite", "content": m.group(2).strip()},
            requires_confirmation=True,
        )

    # --- Удаление / создание / чтение файла (файловые, специфичные) ---
    if re.search(r'\bудали(ть)?\b', normalized) and re.search(r'\bфайл\w*\b', normalized):
        filename_match = FILENAME_PATTERN.search(text)
        if not filename_match:
            return EvaCommandParseResult(error=NO_FILENAME_ERROR)
        return EvaCommandParseResult(action="delete_file", params={"path": filename_match.group(0)}, requires_confirmation=True)

    if re.search(r'\b(создай|сделай)\b', normalized) and re.search(r'\bфайл\w*\b', normalized):
        filename_match = FILENAME_PATTERN.search(text)
        if not filename_match:
            return EvaCommandParseResult(error=NO_FILENAME_ERROR)
        content_match = CONTENT_PATTERN.search(text)
        content = content_match.group(1).strip() if content_match else ""
        return EvaCommandParseResult(
            action="create_file", params={"path": filename_match.group(0), "content": content}, requires_confirmation=True,
        )

    if re.search(r'\bнайди\b', normalized) and re.search(r'\bфайл\w*\b', normalized):
        m = SEARCH_FILE_PATTERN.search(text)
        if m:
            return EvaCommandParseResult(action="search_file", params={"query": m.group(1).strip()}, requires_confirmation=False)

    if re.search(r'\b(прочитай|покажи|открой)\b', normalized) and re.search(r'\bфайл\w*\b', normalized):
        filename_match = FILENAME_PATTERN.search(text)
        if not filename_match:
            return EvaCommandParseResult(error=NO_FILENAME_ERROR)
        return EvaCommandParseResult(action="read_file", params={"path": filename_match.group(0)}, requires_confirmation=False)

    # --- Печать текста / горячие клавиши (проверяем ДО общего "открой") ---
    # Явно исключаем случаи со словом "файл" — это почти всегда относится
    # к файловым действиям выше ("напиши в файл X текст ..."), а не к
    # печати на экран.
    if "файл" not in normalized:
        m = TYPE_TEXT_PATTERN.search(text)
        if m:
            return EvaCommandParseResult(action="type_text", params={"text": m.group(1).strip()}, requires_confirmation=False)
        m = PRESS_KEY_PATTERN.search(text)
        if m:
            return EvaCommandParseResult(action="press_key", params={"keys": m.group(1).strip()}, requires_confirmation=False)

    # --- Общее открытие приложения (самый общий шаблон — проверяется последним) ---
    if re.search(r'\b(открой|запусти)\b', normalized):
        app_name = re.sub(r'^.*?\b(открой|запусти)\b\s*', '', text, count=1, flags=re.IGNORECASE).strip()
        if not app_name:
            return EvaCommandParseResult(error="⚠️ Какое приложение открыть? Например: /eva открой блокнот")
        return EvaCommandParseResult(action="open_app", params={"app": app_name}, requires_confirmation=False)

    return EvaCommandParseResult(error=USAGE_ERROR)
