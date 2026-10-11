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
    if re.search(r'тих(?:ие|ий)\s+час\w*\s*(?:выключ\w*|отключ\w*)', work) or \
       re.search(r'(?:выключ\w*|отключ\w*)\s+тих(?:ие|ий)\s+час', work):
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
    # Голое «выключи / отключи / стоп» (в режиме ожидания расписания) тоже считаем выключением.
    if re.fullmatch(r'(?:выключ\w*|отключ\w*|стоп|хватит)[.!]?', work) or \
       re.search(r'(?:выключ\w*|отключ\w*|останов\w*|прекрат\w*)\s+(?:все\s+)?напоминани\w*.*задач', work) or \
       re.search(r'(?:не|хватит)\s+напомин\w*.*задач', work):
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
        r'(понедельник\w*|вторник\w*|сред[ауы]\w*|четверг\w*|пятниц\w*|суббот\w*|воскресень\w*)'
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
        return quiet_start <= current < quiet_end

    # Тихие часы ПЕРЕХОДЯТ через полночь (22:00-08:00)
    return current >= quiet_start or current < quiet_end


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
        # Прибавляем в UTC: арифметика по локальному времени «ломается» при переходе на летнее/зимнее время
        next_run = (now_utc + interval).astimezone(tz)

        # Если попали в тихие часы — сдвигаем на конец
        if quiet_start and quiet_end and _is_in_quiet_hours(next_run, quiet_start, quiet_end):
            # Конец тихих часов — это момент, когда они заканчиваются
            # Если переход через полночь (22:00-08:00), то конец — это 08:00 следующего дня
            if quiet_end <= quiet_start:
                # 22:00-08:00 → конец в 08:00 следующего дня
                quiet_end_dt = next_run.replace(
                    hour=quiet_end.hour, minute=quiet_end.minute, second=0, microsecond=0
                )
                if quiet_end_dt <= next_run:
                    quiet_end_dt += timedelta(days=1)
                next_run = quiet_end_dt
            else:
                # Обычный диапазон (09:00-18:00) — сдвигаем на конец диапазона
                next_run = next_run.replace(
                    hour=quiet_end.hour, minute=quiet_end.minute, second=0, microsecond=0
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


# --- Распознавание намерения «настроить напоминания о задачах» ---

_INTENT_PATTERNS = [
    # «напоминай мне о (моих) задачах ...»
    r'напомин\w*\s+(?:мне\s+)?(?:про|о|об)\s+(?:моих\s+|всех\s+|своих\s+|текущих\s+)?задач',
    # «выключи напоминания о задачах», «не напоминай о задачах», «хватит напоминать о задачах»
    r'(?:выключ\w*|отключ\w*|останов\w*|прекрат\w*)\s+(?:все\s+)?напоминани\w*.*задач',
    r'(?:не|хватит)\s+напомин\w*.*задач',
    # тихие часы
    r'тих(?:ие|ий)\s+час',
    r'не\s+беспоко\w+\s+(?:с\s+)?\d',
]


def is_task_reminder_intent(text: str) -> bool:
    """
    True, если фраза — про настройку напоминаний О ЗАДАЧАХ (а не обычное «напомни позвонить маме»).
    Намеренно узкое правило: лучше пропустить редкую формулировку, чем сломать обычные напоминания.
    """
    if not text:
        return False
    work = text.strip().lower()
    return any(re.search(pat, work) for pat in _INTENT_PATTERNS)


# --- Конфиг напоминаний (хранится в users.settings["task_reminders"]) ---

WEEKDAY_PHRASES = [
    "каждый понедельник", "каждый вторник", "каждую среду", "каждый четверг",
    "каждую пятницу", "каждую субботу", "каждое воскресенье",
]


def _t_to_dict(t: Optional[time]) -> Optional[dict]:
    return {"hour": t.hour, "minute": t.minute} if t else None


def _dict_to_t(d: Optional[dict]) -> Optional[time]:
    if not d:
        return None
    try:
        return time(int(d["hour"]), int(d["minute"]))
    except (KeyError, ValueError, TypeError):
        return None


def _fmt_t(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Склонение: 1 час, 2 часа, 5 часов."""
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def _describe_schedule(cfg: dict) -> str:
    kind = cfg.get("kind")
    if kind == "interval":
        m = int(cfg.get("interval_minutes") or 0)
        if m % 60 == 0:
            h = m // 60
            return "каждый час" if h == 1 else f"каждые {h} {_plural(h, 'час', 'часа', 'часов')}"
        return f"каждые {m} {_plural(m, 'минуту', 'минуты', 'минут')}"
    t = _dict_to_t(cfg.get("time")) or time(9, 0)
    if kind == "daily":
        return f"каждый день в {_fmt_t(t)}"
    if kind == "weekly":
        wd = cfg.get("weekday")
        day = WEEKDAY_PHRASES[wd] if wd is not None and 0 <= wd <= 6 else "каждую неделю"
        return f"{day} в {_fmt_t(t)}"
    return "по расписанию"


def default_task_reminders_config() -> dict:
    return {
        "enabled": False, "kind": None, "interval_minutes": None, "time": None, "weekday": None,
        "quiet_enabled": True,
        "quiet_start": _t_to_dict(DEFAULT_QUIET_START),
        "quiet_end": _t_to_dict(DEFAULT_QUIET_END),
        "next_run_at": None,
    }


def next_run_from_config(cfg: dict, tz_name: str, now_utc: datetime) -> datetime:
    """Следующий запуск (UTC) по сохранённому конфигу. Используется и обработчиком, и фоновым циклом."""
    schedule = TaskReminderSchedule(
        kind=cfg["kind"],
        interval_minutes=cfg.get("interval_minutes"),
        time=_dict_to_t(cfg.get("time")),
        weekday=cfg.get("weekday"),
    )
    quiet_start = quiet_end = None
    # Тихие часы действуют только на интервальные напоминания: время daily/weekly пользователь задал сам.
    if cfg.get("quiet_enabled", True) and cfg["kind"] == "interval":
        quiet_start = _dict_to_t(cfg.get("quiet_start")) or DEFAULT_QUIET_START
        quiet_end = _dict_to_t(cfg.get("quiet_end")) or DEFAULT_QUIET_END
    return compute_next_run(schedule, tz_name, now_utc, quiet_start, quiet_end)


def build_task_reminders_config(
    existing: Optional[dict], result: TaskReminderParseResult, tz_name: str, now_utc: datetime
) -> dict:
    """
    Накладывает результат разбора на ТЕКУЩИЙ конфиг, ничего не затирая:
    смена тихих часов не сбрасывает расписание, «тихие часы выключи» не выключает напоминания.
    """
    cfg = {**default_task_reminders_config(), **(existing or {})}

    if result.action == "set":
        cfg.update(
            enabled=True, kind=result.kind, interval_minutes=result.interval_minutes,
            time=_t_to_dict(result.time), weekday=result.weekday,
        )
    elif result.action == "disable":
        cfg["enabled"] = False
    elif result.action == "quiet_set":
        cfg.update(
            quiet_enabled=True,
            quiet_start=_t_to_dict(result.quiet_start),
            quiet_end=_t_to_dict(result.quiet_end),
        )
    elif result.action == "quiet_off":
        cfg["quiet_enabled"] = False

    if cfg["enabled"] and cfg.get("kind"):
        cfg["next_run_at"] = next_run_from_config(cfg, tz_name, now_utc).isoformat()
    else:
        cfg["next_run_at"] = None
    return cfg


def _human_when(next_run_utc: datetime, tz_name: str, now_utc: datetime) -> str:
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    local = next_run_utc.astimezone(tz)
    today = now_utc.astimezone(tz).date()
    if local.date() == today:
        day = "сегодня"
    elif (local.date() - today).days == 1:
        day = "завтра"
    else:
        day = local.strftime("%d.%m")
    return f"{day} в {local.strftime('%H:%M')}"


def describe_task_reminders(cfg: dict, tz_name: str, action: str = "status", now_utc: Optional[datetime] = None) -> str:
    """Текст (HTML) для пользователя: статус или подтверждение после изменения настройки."""
    now_utc = now_utc or datetime.now(timezone.utc)
    enabled = bool(cfg.get("enabled")) and bool(cfg.get("kind"))
    quiet_on = cfg.get("quiet_enabled", True)
    qs = _dict_to_t(cfg.get("quiet_start")) or DEFAULT_QUIET_START
    qe = _dict_to_t(cfg.get("quiet_end")) or DEFAULT_QUIET_END
    quiet_txt = f"{_fmt_t(qs)}–{_fmt_t(qe)}"

    lines = []
    if action == "disable":
        lines.append("🔕 Напоминания о задачах выключены.")
    elif action == "quiet_off":
        lines.append("🌙 Тихие часы выключены.")
    elif action == "quiet_set":
        lines.append(f"🌙 Тихие часы: {quiet_txt}.")

    if enabled:
        sched = _describe_schedule(cfg)
        if action in ("set", "status"):
            head = "🔔 Буду напоминать" if action == "set" else "🔔 <b>Напоминания о задачах включены:</b>"
            extra = f", кроме {quiet_txt}" if (quiet_on and cfg["kind"] == "interval") else ""
            lines.append(f"{head} {sched}{extra}.")
        else:
            lines.append(f"Расписание не изменилось: {sched}.")
        nxt = cfg.get("next_run_at")
        if nxt:
            try:
                lines.append("Ближайшее — " + _human_when(datetime.fromisoformat(nxt), tz_name, now_utc) + ".")
            except ValueError:
                pass
        if action == "status":
            lines.append(f"Тихие часы: {quiet_txt}" if quiet_on else "Тихие часы выключены")
    elif action in ("quiet_set", "quiet_off"):
        lines.append("Напоминания о задачах пока выключены — напиши расписание, например «каждые 2 часа».")
    elif action == "status":
        lines.append("🔔 <b>Напоминания о задачах выключены.</b>")

    if tz_name == "UTC":
        lines.append("\n🌍 Часовой пояс не настроен (UTC). Отправь /timezone, чтобы время было местным.")
    return "\n".join(lines)
