"""Тесты исправлений напоминаний о задачах (дополняют test_task_reminder_parser.py)."""
from datetime import datetime, time, timezone, timedelta
from zoneinfo import ZoneInfo

import pytest

from task_reminder_parser import (
    parse_task_reminder_text, is_task_reminder_intent, build_task_reminders_config,
    describe_task_reminders, next_run_from_config, compute_next_run, TaskReminderSchedule,
    _is_in_quiet_hours,
)

UTC = timezone.utc


# ---------- парсер ----------
@pytest.mark.parametrize("text,weekday,hh", [
    ("по понедельникам в 10:00", 0, 10),   # раньше время терялось и становилось 9:00
    ("по пятницам в 18:30", 4, 18),
    ("каждый понедельник в 10:00", 0, 10),
    ("каждую среду в 7", 2, 7),
])
def test_weekly_keeps_time(text, weekday, hh):
    r = parse_task_reminder_text(text)
    assert r.action == "set" and r.kind == "weekly"
    assert r.weekday == weekday and r.time.hour == hh


@pytest.mark.parametrize("text", [
    "выключи", "отключи", "стоп", "выключи напоминания о задачах",
    "выключи напоминания о моих задачах", "не напоминай мне о задачах", "хватит напоминать о задачах",
])
def test_disable_phrases(text):
    assert parse_task_reminder_text(text).action == "disable"


def test_quiet_off_variants():
    assert parse_task_reminder_text("выключи тихие часы").action == "quiet_off"
    assert parse_task_reminder_text("выключить тихие часы").action == "quiet_off"


# ---------- намерение (не должно перехватывать обычные напоминания) ----------
@pytest.mark.parametrize("text", [
    "напоминай мне о моих задачах каждые 2 часа",
    "Напоминай мне о задачах каждый день в 9:00",
    "выключи напоминания о задачах",
    "не напоминай мне о задачах",
    "тихие часы с 23:00 до 7:00",
    "не беспокой с 23 до 7",
])
def test_intent_true(text):
    assert is_task_reminder_intent(text)


@pytest.mark.parametrize("text", [
    "напомни мне позвонить маме через 30 минут",
    "напомни про задачу завтра в 15:00",
    "напоминай каждый день пить воду в 9:00",
    "какая задача по математике?",
    "добавь задачу купить хлеб",
    "",
])
def test_intent_false(text):
    assert not is_task_reminder_intent(text)


# ---------- тихие часы ----------
def test_quiet_end_is_exclusive():
    assert _is_in_quiet_hours(datetime(2026, 1, 1, 7, 59, tzinfo=UTC), time(22), time(8))
    assert not _is_in_quiet_hours(datetime(2026, 1, 1, 8, 0, tzinfo=UTC), time(22), time(8))
    assert _is_in_quiet_hours(datetime(2026, 1, 1, 22, 0, tzinfo=UTC), time(22), time(8))


def test_interval_shifts_out_of_quiet_hours_across_midnight():
    tz = "Europe/Istanbul"  # UTC+3
    # 21:00 местного + 2 часа = 23:00 (тихо) -> сдвиг на 08:00 следующего дня
    now = datetime(2026, 10, 11, 18, 0, tzinfo=UTC)  # 21:00 в Стамбуле
    sched = TaskReminderSchedule(kind="interval", interval_minutes=120)
    nxt = compute_next_run(sched, tz, now, time(22), time(8)).astimezone(ZoneInfo(tz))
    assert (nxt.day, nxt.hour, nxt.minute, nxt.second) == (12, 8, 0, 0)


def test_interval_outside_quiet_is_plain_offset():
    now = datetime(2026, 10, 11, 7, 0, tzinfo=UTC)  # 10:00 в Стамбуле
    sched = TaskReminderSchedule(kind="interval", interval_minutes=120)
    nxt = compute_next_run(sched, "Europe/Istanbul", now, time(22), time(8))
    assert nxt == now + timedelta(hours=2)


def test_interval_across_dst_is_real_two_hours():
    # 2026-03-29 02:00 -> 03:00 в Берлине (переход на летнее время)
    now = datetime(2026, 3, 29, 0, 30, tzinfo=UTC)  # 01:30 CET
    sched = TaskReminderSchedule(kind="interval", interval_minutes=120)
    nxt = compute_next_run(sched, "Europe/Berlin", now)
    assert nxt - now == timedelta(hours=2)


def test_daily_and_weekly_in_user_timezone():
    now = datetime(2026, 10, 11, 5, 0, tzinfo=UTC)  # 08:00 в Стамбуле, воскресенье
    d = compute_next_run(TaskReminderSchedule(kind="daily", time=time(9, 0)), "Europe/Istanbul", now)
    assert d == datetime(2026, 10, 11, 6, 0, tzinfo=UTC)
    w = compute_next_run(TaskReminderSchedule(kind="weekly", weekday=0, time=time(10, 0)), "Europe/Istanbul", now)
    assert w == datetime(2026, 10, 12, 7, 0, tzinfo=UTC)


# ---------- слияние конфига (главный баг 2b) ----------
NOW = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
TZ = "Europe/Istanbul"


def _set_every_2h():
    return build_task_reminders_config(None, parse_task_reminder_text("каждые 2 часа"), TZ, NOW)


def test_set_enables_and_sets_next_run():
    cfg = _set_every_2h()
    assert cfg["enabled"] and cfg["kind"] == "interval" and cfg["interval_minutes"] == 120
    assert cfg["next_run_at"]


def test_quiet_set_keeps_schedule():
    cfg = build_task_reminders_config(_set_every_2h(), parse_task_reminder_text("тихие часы с 23:00 до 7:00"), TZ, NOW)
    assert cfg["enabled"] and cfg["kind"] == "interval" and cfg["interval_minutes"] == 120
    assert cfg["quiet_start"] == {"hour": 23, "minute": 0} and cfg["quiet_end"] == {"hour": 7, "minute": 0}


def test_quiet_off_does_not_disable_reminders():
    cfg = build_task_reminders_config(_set_every_2h(), parse_task_reminder_text("выключи тихие часы"), TZ, NOW)
    assert cfg["enabled"] is True and cfg["quiet_enabled"] is False


def test_quiet_set_without_schedule_does_not_enable():
    cfg = build_task_reminders_config(None, parse_task_reminder_text("тихие часы с 23 до 7"), TZ, NOW)
    assert cfg["enabled"] is False and cfg["next_run_at"] is None


def test_new_schedule_keeps_custom_quiet_hours():
    cfg = build_task_reminders_config(None, parse_task_reminder_text("тихие часы с 23:00 до 7:00"), TZ, NOW)
    cfg = build_task_reminders_config(cfg, parse_task_reminder_text("каждые 3 часа"), TZ, NOW)
    assert cfg["quiet_start"] == {"hour": 23, "minute": 0} and cfg["interval_minutes"] == 180


def test_disable_turns_off_and_clears_next_run():
    cfg = build_task_reminders_config(_set_every_2h(), parse_task_reminder_text("выключи"), TZ, NOW)
    assert cfg["enabled"] is False and cfg["next_run_at"] is None


def test_switching_kind_clears_old_fields():
    cfg = build_task_reminders_config(_set_every_2h(), parse_task_reminder_text("каждый день в 9:00"), TZ, NOW)
    assert cfg["kind"] == "daily" and cfg["interval_minutes"] is None and cfg["time"] == {"hour": 9, "minute": 0}


def test_next_run_from_config_ignores_quiet_for_daily():
    cfg = build_task_reminders_config(None, parse_task_reminder_text("каждый день в 23:30"), TZ, NOW)
    nxt = next_run_from_config(cfg, TZ, NOW).astimezone(ZoneInfo(TZ))
    assert (nxt.hour, nxt.minute) == (23, 30)


# ---------- тексты ----------
def test_describe_set_interval():
    cfg = _set_every_2h()
    txt = describe_task_reminders(cfg, TZ, "set", NOW)
    assert "каждые 2 часа" in txt and "22:00–08:00" in txt and "Ближайшее" in txt


def test_describe_weekly_grammar():
    cfg = build_task_reminders_config(None, parse_task_reminder_text("по средам в 10:00"), TZ, NOW)
    assert "каждую среду в 10:00" in describe_task_reminders(cfg, TZ, "set", NOW)


def test_describe_warns_when_timezone_unset():
    assert "/timezone" in describe_task_reminders(_set_every_2h(), "UTC", "set", NOW)
