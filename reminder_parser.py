"""
Разбор текста напоминания в структурированные данные.

Специально вынесено из handlers.py в отдельный модуль без зависимостей
от Telegram/БД — это чистая функция (одинаковый вход всегда даёт
одинаковый выход, без побочных эффектов), поэтому её легко покрыть
юнит-тестами напрямую, без запуска бота и без моков сообщений Telegram.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import dateparser

TRIGGER_PATTERN = re.compile(
    r'^(напомни мне|напомнить|напомни|напоминай мне|напоминай)\s*',
    re.IGNORECASE,
)
RECURRENCE_PATTERN = re.compile(
    r'кажд(?:ый|ую|ое)\s+'
    r'(день|недел[юи]|месяц|понедельник|вторник|сред[ау]|четверг|пятниц[ау]|суббот[ау]|воскресень[ея])',
    re.IGNORECASE,
)
DELTA_PATTERN = re.compile(
    r'через\s+(\d+)\s+(минут\w*|час\w*|дн\w*|день)', re.IGNORECASE,
)

MONTH_NAMES = (
    r'январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]\w*|июн\w*|июл\w*|'
    r'август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*'
)
# Порядок альтернатив важен: более специфичные (длинные) варианты — раньше,
# иначе регулярка захватит только часть фразы и оставит "хвост" в тексте задачи.
DATE_PATTERN = re.compile(
    rf'(?:завтра|сегодня)\s+в\s+\d{{1,2}}:\d{{2}}'
    rf'|\d{{1,2}}\s+(?:{MONTH_NAMES})(?:\s+в\s+\d{{1,2}}:\d{{2}})?'
    rf'|завтра|сегодня|в\s+\d{{1,2}}:\d{{2}}',
    re.IGNORECASE,
)

NO_TEXT_ERROR = (
    "⚠️ Напиши, что именно напомнить. Например: «Напомни мне позвонить маме через 30 минут» "
    "или «Напоминай каждый вторник отложить деньги»."
)
NO_TIME_ERROR = (
    "⏰ Не могу понять, когда напомнить. Попробуй:\n"
    "• «Напомни мне [текст] через 30 минут»\n"
    "• «Напомни [текст] завтра в 15:00»\n"
    "• «Напомни [текст] 30 сентября»\n"
    "• «Напоминай каждый день [текст] в 9:00»"
)


@dataclass
class ReminderParseResult:
    reminder_text: Optional[str] = None
    remind_at: Optional[datetime] = None  # всегда в UTC — так хранится в БД
    recurrence: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def parse_reminder_text(
    text: str, tz_name: str = "UTC", now: Optional[datetime] = None
) -> ReminderParseResult:
    """
    Разбирает фразу пользователя в текст задачи + время (UTC) + признак повтора.

    tz_name — часовой пояс пользователя (IANA-строка вроде "Europe/Istanbul").
    Именно в нём интерпретируются фразы вроде "в 15:00" или "30 сентября" —
    иначе бот считал бы, что пользователь имеет в виду 15:00 по UTC, а не
    по своему местному времени, и напоминание срабатывало бы не тогда,
    когда ожидается.

    now можно передать явно (для предсказуемых тестов), иначе берётся
    текущее время. Служебные фразы (повтор, время) ищутся в ЛЮБОМ месте
    сообщения, а не только в начале — порядок слов в русском свободный.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
        tz_name = "UTC"

    working_text = TRIGGER_PATTERN.sub('', text, count=1).strip()

    recurrence = None
    recurrence_match = RECURRENCE_PATTERN.search(working_text)
    if recurrence_match:
        word = recurrence_match.group(1).lower()
        if word.startswith('день'):
            recurrence = 'daily'
        elif word.startswith('недел'):
            recurrence = 'weekly'
        elif word.startswith('месяц'):
            recurrence = 'monthly'
        else:
            recurrence = 'weekly'  # конкретный день недели — считаем еженедельным
        working_text = (working_text[:recurrence_match.start()] + working_text[recurrence_match.end():]).strip()

    remind_at = None
    delta_match = DELTA_PATTERN.search(working_text)
    if delta_match:
        # "через N минут/часов/дней" не зависит от часового пояса —
        # это просто смещение от текущего момента.
        amount = int(delta_match.group(1))
        unit = delta_match.group(2).lower()
        if unit.startswith('минут'):
            delta = timedelta(minutes=amount)
        elif unit.startswith('час'):
            delta = timedelta(hours=amount)
        else:
            delta = timedelta(days=amount)
        remind_at = now + delta
        working_text = (working_text[:delta_match.start()] + working_text[delta_match.end():]).strip()

    if remind_at is None:
        date_match = DATE_PATTERN.search(working_text)
        if date_match:
            local_now_naive = now.astimezone(tz).replace(tzinfo=None)
            parsed = dateparser.parse(
                date_match.group(0),
                settings={
                    'PREFER_DATES_FROM': 'future',
                    'RETURN_AS_TIMEZONE_AWARE': True,
                    'TIMEZONE': tz_name,
                    'TO_TIMEZONE': 'UTC',
                    'RELATIVE_BASE': local_now_naive,
                },
            )
            if parsed:
                remind_at = parsed
            working_text = (working_text[:date_match.start()] + working_text[date_match.end():]).strip()

    reminder_text = working_text.strip(' ,.-')

    if not reminder_text:
        return ReminderParseResult(error=NO_TEXT_ERROR)

    if remind_at is None:
        if recurrence:
            remind_at = now
        else:
            return ReminderParseResult(error=NO_TIME_ERROR)

    return ReminderParseResult(reminder_text=reminder_text, remind_at=remind_at, recurrence=recurrence)