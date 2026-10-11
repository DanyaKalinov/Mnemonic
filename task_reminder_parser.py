"""
Парсинг настроек периодических напоминаний о задачах.

Чистые функции без зависимостей от Telegram/БД — легко тестировать.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, time, timezone
from typing import Optional

# --- Константы ---

MIN_INTERVAL_MINUTES = 30
MAX_INTERVAL_MINUTES = 24 * 60  # 24 часа
DEFAULT_QUIET_START = time(22, 0)   # 22:00
DEFAULT_QUIET_END = time(8, 0)      # 08:00


# --- Dataclasses ---

@dataclass
class TaskReminderParseResult:
    """Результат парсинга настроек напоминаний о задачах."""
    action: str  # "set" | "disable" | "quiet_set" | "quiet_off"
    kind: Optional[str] = None  # "interval" | "daily" | "weekly"
    interval_minutes: Optional[int] = None  # для interval
    time: Optional[time] = None  # для daily/weekly (локальное время)
    weekday: Optional[int] = None  # 0-6 для weekly (понедельник=0)
    quiet_enabled: Optional[bool] = None  # для quiet_set/quiet_off
    quiet_start: Optional[time] = None
    quiet_end: Optional[time] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class TaskReminderSchedule:
    """Сконвертированное расписание для сохранения в БД."""
    kind: str  # "interval" | "daily" | "weekly"
    interval_minutes: Optional[int] = None  # для interval
    time: Optional[time] = None  # для daily/weekly (локальное время)
    weekday: Optional[int] = None  # 0-6 для weekly (понедельник=0)


# --- Сообщения об ошибках ---

ERROR_INTERVAL_TOO_SHORT = (
    "⏱ Минимальный интервал — 30 минут. "
    "Попробуй: «каждые 30 минут», «каждый час»."
)

ERROR_INTERVAL_TOO_LONG = (
    "📅 Для интервалов больше 24 часов используй расписание. "
    "Например: «каждый день в 9:00» или «каждую неделю в понедельник в 10:00»."
)

ERROR_EXAMPLES = (
    "📝 <b>Примеры:</b>\n"
    "• «каждые 2 часа»\n"
    "• «каждый день в 9:00»\n"
    "• «каждую неделю в понедельник в 10:00»\n"
    "• «тихие часы с 23:00 до 7:00»\n"
    "• «выключи напоминания о задачах»"
)


# --- Парсинг ---

def _parse_time(hour_str: Optional[str], minute_str: Optional[str] = None) -> Optional[time]:
    """Парсит время из строк вида '9', '9:00', '09.30'."""
    if not hour_str:
        return None
    try:
        hour = int(hour_str)
        minute = int(minute_str) if minute_str else 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour, minute)
    except (ValueError, TypeError):
        pass
    return None


def _extract_weekday(word: str) -> Optional[int]:
    """Извлекает номер дня недели из слова."""
    word_lower = word.lower()
    # Порядок важен: более специфичные паттерны первыми
    weekday_map = [
        (r'понедельник', 0),
        (r'вторник', 1),
        (r'сред', 2),  # среда, среду
        (r'четверг', 3),
        (r'пятниц', 4),  # пятница, пятницу
        (r'суббот', 5),  # суббота, субботу
        (r'воскресень', 6),  # воскресенье, воскресенья
    ]
    for pattern, idx in weekday_map:
        if re.search(pattern, word_lower):
            return idx
    return None


def parse_task_reminder_text(text: str) -> TaskReminderParseResult:
    """
    Разбирает текст пользователя в настройки напоминаний о задачах.

    Поддерживаемые форматы:
    - Интервалы: «каждые 2 часа», «каждый час», «каждые 30 минут», «каждые полчаса»
    - Ежедневно: «каждый день в 9:00» (время опционально, по умолчанию 9:00)
    - Еженедельно: «каждую неделю в понедельник в 10:00», «по понедельникам в 10:00»
    - Отключение: «выключи/отключи напоминания о задачах», «не напоминай о задачах»
    - Тихие часы: «тихие часы с 23:00 до 7:00», «не беспокой с 23 до 7»

    Args:
        text: Текст от пользователя

    Returns:
        TaskReminderParseResult с action и параметрами или error
    """
    if not text or not text.strip():
        return TaskReminderParseResult(
            action="error",
            error="Напиши расписание или «выключи напоминания».\n\n" + ERROR_EXAMPLES
        )

    work = text.strip().lower()

    # --- Сначала проверяем выключение тихих часов (до общих паттернов отключения) ---
    if re.search(r'тих(?:ие|ий)\s+час(?:ы|а)?\s*(?:выключи|отключи)', work) or \
       re.search(r'(?:выключи|отключи)\s+тих(?:ие|ий)\s+час', work):
        return TaskReminderParseResult(action="quiet_off")

    # --- Проверяем установку тихих часов ---
    quiet_match = re.search(
        r'тих(?:ие|ий)\s+час(?:ы|а)?\s*(?:с\s+)?(\d{1,2})(?:[:.](\d{2}))?\s*(?:до|-|–)\s*(\d{1,2})(?:[:.](\d{2}))?',
        work
    )
    if not quiet_match:
        quiet_match = re.search(
            r'не\s+беспоко[йя]\s+(?:с\s+)?(\d{1,2})(?:[:.](\d{2}))?\s*(?:до|-|–)\s*(\d{1,2})(?:[:.](\d{2}))?',
            work
        )
    if not quiet_match:
        quiet_match = re.search(
            r'не\s+беспоко[йя]\s+(?:с\s+)?(\d{1,2})\s+(?:до|-|–)\s+(\d{1,2})',
            work
        )

    if quiet_match:
        groups = quiet_match.groups()
        start = _parse_time(groups[0], groups[1] if len(groups) > 1 else None)
        end = _parse_time(groups[2] if len(groups) > 2 else None, groups[3] if len(groups) > 3 else None)
        if start and end:
            return TaskReminderParseResult(
                action="quiet_set",
                quiet_enabled=True,
                quiet_start=start,
                quiet_end=end,
            )

    # --- Проверяем отключение напоминаний о задачах ---
    if re.search(r'выключи\s+напоминани[яе]?\s+о\s+задач', work) or \
       re.search(r'отключи\s+напоминани[яе]?\s+о\s+задач', work) or \
       re.search(r'не\s+напомина[йя]\s+о\s+задач', work):
        return TaskReminderParseResult(action="disable")

    # --- Проверяем интервалы (порядок важен: более специфичные первыми) ---

    # "каждые полчаса"
    if re.search(r'кажд(?:ые|ый)\s+полчас', work):
        return TaskReminderParseResult(
            action="set",
            kind="interval",
            interval_minutes=30,
        )

    # "каждые N час/часа/часов" или "каждый час"
    interval_hour_match = re.search(r'кажд(?:ые|ый|ую|ое)\s+(?:(\d+)\s+)?час', work)
    if interval_hour_match:
        num_str = interval_hour_match.group(1)
        num = int(num_str) if num_str else 1
        interval_minutes = num * 60
        if interval_minutes < MIN_INTERVAL_MINUTES:
            return TaskReminderParseResult(action="error", error=ERROR_INTERVAL_TOO_SHORT)
        if interval_minutes > MAX_INTERVAL_MINUTES:
            return TaskReminderParseResult(action="error", error=ERROR_INTERVAL_TOO_LONG)
        return TaskReminderParseResult(
            action="set",
            kind="interval",
            interval_minutes=interval_minutes,
        )

    # "каждые N минут/минуты"
    interval_min_match = re.search(r'кажд(?:ые|ый|ую|ое)\s+(\d+)\s+минут', work)
    if interval_min_match:
        num = int(interval_min_match.group(1))
        interval_minutes = num
        if interval_minutes < MIN_INTERVAL_MINUTES:
            return TaskReminderParseResult(action="error", error=ERROR_INTERVAL_TOO_SHORT)
        if interval_minutes > MAX_INTERVAL_MINUTES:
            return TaskReminderParseResult(action="error", error=ERROR_INTERVAL_TOO_LONG)
        return TaskReminderParseResult(
            action="set",
            kind="interval",
            interval_minutes=interval_minutes,
        )

    # --- Проверяем еженедельное расписание ---

    # "каждую неделю в понедельник" или "каждый понедельник"
    weekly_match = re.search(
        r'(?:кажд(?:ый|ую|ое)\s+(?:неделю\s+в\s+)?)?'  # опционально "каждую неделю в"
        r'(понедельник|вторник|сред[ау]|четверг|пятниц[ау]|суббот[ау]|воскресень[ея])'
        r'(?:\s+в\s+(\d{1,2})(?:[:.](\d{2}))?)?',
        work
    )
    if not weekly_match:
        # "по понедельникам"
        weekly_match = re.search(
            r'по\s+(понедельникам|вторникам|средам|четвергам|пятницам|субботам|воскресеньям)'
            r'(?:\s+в\s+(\d{1,2})(?:[:.](\d{2}))?)?',
            work
        )

    if weekly_match:
        day_word = weekly_match.group(1)
        weekday = _extract_weekday(day_word)
        if weekday is not None:
            hour_str = weekly_match.group(2) if len(weekly_match.groups()) >= 2 else None
            minute_str = weekly_match.group(3) if len(weekly_match.groups()) >= 3 else None
            t = _parse_time(hour_str, minute_str) if hour_str else time(9, 0)
            if t:
                return TaskReminderParseResult(
                    action="set",
                    kind="weekly",
                    weekday=weekday,
                    time=t,
                )

    # --- Проверяем ежедневное расписание ---
    daily_match = re.search(
        r'кажд(?:ый|ую|ое)\s+день(?:\s+в\s+(\d{1,2})(?:[:.](\d{2}))?)?',
        work
    )
    if daily_match:
        hour_str = daily_match.group(1)
        minute_str = daily_match.group(2)
        t = _parse_time(hour_str or "9", minute_str or "0")
        if t:
            return TaskReminderParseResult(
                action="set",
                kind="daily",
                time=t,
            )

    # Ничего не распознали
    return TaskReminderParseResult(
        action="error",
        error="Не понял расписание. Напиши: «каждые 2 часа», «каждый день в 9:00» и т.п.\n\n" + ERROR_EXAMPLES
    )


# --- Расчёт следующего запуска ---

def _time_to_datetime(t: time, tz, base_date: datetime) -> datetime:
    """Конвертирует time в aware datetime в нужном часовом поясе."""
    return base_date.replace(
        hour=t.hour, minute=t.minute, second=t.second, microsecond=0
    ).astimezone(tz)


def _is_in_quiet_hours(dt: datetime, quiet_start: time, quiet_end: time) -> bool:
    """Проверяет, попадает ли datetime в тихие часы."""
    current = dt.time()

    # Тихие часы НЕ переходят через полночь
    if quiet_start <= quiet_end:
        return quiet_start <= current <= quiet_end

    # Тихие часы ПЕРЕХОДЯТ через полночь (22:00-08:00)
    return current >= quiet_start or current <= quiet_end


def compute_next_run(
    cfg: TaskReminderSchedule,
    tz_name: str,
    now_utc: datetime,
    quiet_start: Optional[time] = None,
    quiet_end: Optional[time] = None,
) -> datetime:
    """
    Вычисляет следующее время запуска напоминания.

    Args:
        cfg: Расписание (kind, interval_minutes, time, weekday)
        tz_name: Имя часового пояса (IANA, например "Europe/Moscow")
        now_utc: Текущее время в UTC
        quiet_start: Начало тихих часов (если есть)
        quiet_end: Конец тихих часов (если есть)

    Returns:
        Следующее время запуска в UTC
    """
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    now_local = now_utc.astimezone(tz)

    # --- Interval: now + interval ---
    if cfg.kind == "interval":
        interval = timedelta(minutes=cfg.interval_minutes or 60)
        next_run = now_local + interval

        # Если попали в тихие часы — сдвигаем на конец
        if quiet_start and quiet_end and _is_in_quiet_hours(next_run, quiet_start, quiet_end):
            # Конец тихих часов — это момент, когда они заканчиваются
            # Если переход через полночь (22:00-08:00), то конец — это 08:00 следующего дня
            if quiet_end <= quiet_start:
                # 22:00-08:00 → конец в 08:00 следующего дня
                quiet_end_dt = next_run.replace(
                    hour=quiet_end.hour, minute=quiet_end.minute
                )
                if quiet_end_dt <= next_run:
                    quiet_end_dt += timedelta(days=1)
                next_run = quiet_end_dt
            else:
                # Обычный диапазон (09:00-18:00) — сдвигаем на конец диапазона
                next_run = next_run.replace(
                    hour=quiet_end.hour, minute=quiet_end.minute
                )
                # Если конец уже прошёл сегодня — на завтра
                if next_run <= now_local:
                    next_run += timedelta(days=1)

        return next_run.astimezone(timezone.utc)

    # --- Daily: ближайшее вхождение указанного времени ---
    if cfg.kind == "daily":
        target_time = cfg.time or time(9, 0)
        today_target = _time_to_datetime(target_time, tz, now_local)

        if today_target > now_local:
            # Сегодня ещё будет — сегодня
            next_run = today_target
        else:
            # Сегодня уже прошло — завтра
            next_run = today_target + timedelta(days=1)

        return next_run.astimezone(timezone.utc)

    # --- Weekly: ближайшее вхождение указанного дня и времени ---
    if cfg.kind == "weekly":
        target_weekday = cfg.weekday if cfg.weekday is not None else 0
        target_time = cfg.time or time(9, 0)

        # Считаем номер недели (понедельник = 0)
        current_weekday = now_local.weekday()

        # Дней до целевого дня
        days_ahead = target_weekday - current_weekday
        if days_ahead < 0:
            days_ahead += 7
        elif days_ahead == 0:
            # Сегодня тот же день — проверим время
            today_target = _time_to_datetime(target_time, tz, now_local)
            if today_target > now_local:
                days_ahead = 0
            else:
                days_ahead = 7  # Следующая неделя

        next_run = (now_local + timedelta(days=days_ahead)).replace(
            hour=target_time.hour, minute=target_time.minute, second=0, microsecond=0
        )

        return next_run.astimezone(timezone.utc)

    # Неизвестный kind — возвращаем now как fallback
    return now_utc
