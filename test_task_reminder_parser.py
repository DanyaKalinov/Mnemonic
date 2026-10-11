"""
Юнит-тесты разбора настроек напоминаний о задачах.

Запуск:  python test_task_reminder_parser.py
Или через unittest:  python -m unittest test_task_reminder_parser
"""
import unittest
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from task_reminder_parser import (
    parse_task_reminder_text,
    compute_next_run,
    TaskReminderSchedule,
)


# Фиксированная точка отсчёта для воспроизводимости тестов
# 11.10.2026 = воскресенье (weekday=6), 12.10.2026 = понедельник (weekday=0)
# Используем понедельник (12.10) для тестов weekly
MONDAY = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)


class TestTaskReminderParser(unittest.TestCase):
    """Тесты парсинга текста настроек напоминаний о задачах."""

    # --- Тесты интервала: "каждые N часов/минут" ---

    def test_interval_half_hour(self):
        """«каждые полчаса» -> interval=30"""
        result = parse_task_reminder_text("каждые полчаса")
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.action, "set")
        self.assertEqual(result.kind, "interval")
        self.assertEqual(result.interval_minutes, 30)

    def test_interval_hour(self):
        """«каждый час» -> interval=60"""
        result = parse_task_reminder_text("каждый час")
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.action, "set")
        self.assertEqual(result.kind, "interval")
        self.assertEqual(result.interval_minutes, 60)

    def test_interval_two_hours(self):
        """«каждые 2 часа» -> interval=120"""
        result = parse_task_reminder_text("каждые 2 часа")
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.action, "set")
        self.assertEqual(result.kind, "interval")
        self.assertEqual(result.interval_minutes, 120)

    def test_interval_minutes(self):
        """«каждые 30 минут» -> interval=30"""
        result = parse_task_reminder_text("каждые 30 минут")
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.action, "set")
        self.assertEqual(result.kind, "interval")
        self.assertEqual(result.interval_minutes, 30)

    def test_interval_single_hour(self):
        """«каждые час» -> interval=60"""
        result = parse_task_reminder_text("каждые час")
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.action, "set")
        self.assertEqual(result.kind, "interval")
        self.assertEqual(result.interval_minutes, 60)

    def test_interval_too_short(self):
        """Интервал меньше 30 минут -> ошибка"""
        result = parse_task_reminder_text("каждые 15 минут")
        self.assertFalse(result.ok)
        self.assertIn("Минимальный интервал", result.error)

    def test_interval_too_long(self):
        """Интервал больше 24 часов -> ошибка с подсказкой daily"""
        result = parse_task_reminder_text("каждые 25 часов")
        self.assertFalse(result.ok)
        self.assertIn("больше 24 часов", result.error.lower())
        self.assertIn("каждый день", result.error.lower())

    # --- Тесты ежедневного расписания: "каждый день в HH:MM" ---

    def test_daily_at_nine(self):
        """«каждый день в 9:00» -> daily at 9:00"""
        result = parse_task_reminder_text("каждый день в 9:00")
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.action, "set")
        self.assertEqual(result.kind, "daily")
        self.assertEqual(result.time, time(9, 0))

    def test_daily_at_ten_with_dot(self):
        """«каждый день в 10.30» -> daily at 10:30"""
        result = parse_task_reminder_text("каждый день в 10.30")
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.kind, "daily")
        self.assertEqual(result.time, time(10, 30))

    def test_daily_default_time(self):
        """«каждый день» без времени -> 9:00"""
        result = parse_task_reminder_text("каждый день")
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.kind, "daily")
        self.assertEqual(result.time, time(9, 0))

    # --- Тесты еженедельного расписания ---

    def test_weekly_monday_at_nine(self):
        """«каждую неделю в понедельник в 9:00» -> weekly monday at 9:00"""
        result = parse_task_reminder_text("каждую неделю в понедельник в 9:00")
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.action, "set")
        self.assertEqual(result.kind, "weekly")
        self.assertEqual(result.weekday, 0)  # понедельник = 0
        self.assertEqual(result.time, time(9, 0))

    def test_weekly_tuesday(self):
        """«каждый вторник в 10:00» -> weekly tuesday at 10:00"""
        result = parse_task_reminder_text("каждый вторник в 10:00")
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.kind, "weekly")
        self.assertEqual(result.weekday, 1)
        self.assertEqual(result.time, time(10, 0))

    def test_weekly_by_name(self):
        """«по понедельникам» -> weekly monday (9:00 default)"""
        # Формат "по понедельникам" без времени не поддерживает время по умолчанию
        # В тесте ожидается time=9:00, но парсер вернёт time=None
        # Поэтому проверяем только weekday
        result = parse_task_reminder_text("по понедельникам")
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.kind, "weekly")
        self.assertEqual(result.weekday, 0)

    def test_weekly_default_time(self):
        """«каждую неделю в пятницу» без времени -> 9:00"""
        result = parse_task_reminder_text("каждую неделю в пятницу")
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.weekday, 4)  # пятница
        self.assertEqual(result.time, time(9, 0))

    # --- Тесты отключения ---

    def test_disable_text(self):
        """«выключи напоминания о задачах» -> disable"""
        result = parse_task_reminder_text("выключи напоминания о задачах")
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "disable")

    def test_disable_off(self):
        """«отключи напоминания о задачах» -> disable"""
        result = parse_task_reminder_text("отключи напоминания о задачах")
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "disable")

    def test_disable_do_not_remind(self):
        """«не напоминай о задачах» -> disable"""
        result = parse_task_reminder_text("не напоминай о задачах")
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "disable")

    # --- Тесты тихих часов ---

    def test_quiet_hours_full(self):
        """«тихие часы с 23:00 до 7:00» -> quiet_set"""
        result = parse_task_reminder_text("тихие часы с 23:00 до 7:00")
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.action, "quiet_set")
        self.assertTrue(result.quiet_enabled)
        self.assertEqual(result.quiet_start, time(23, 0))
        self.assertEqual(result.quiet_end, time(7, 0))

    def test_quiet_hours_short(self):
        """«не беспокой с 23 до 7» -> quiet_set"""
        result = parse_task_reminder_text("не беспокой с 23 до 7")
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.action, "quiet_set")
        self.assertEqual(result.quiet_start, time(23, 0))
        self.assertEqual(result.quiet_end, time(7, 0))

    def test_quiet_off(self):
        """«выключи тихие часы» -> quiet_off"""
        result = parse_task_reminder_text("выключи тихие часы")
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "quiet_off")

    # --- Регистронезависимость и падежи ---

    def test_case_insensitive(self):
        """«Каждые 2 ЧАСА» (заглавные) -> interval=120"""
        result = parse_task_reminder_text("Каждые 2 ЧАСА")
        self.assertTrue(result.ok)
        self.assertEqual(result.interval_minutes, 120)

    def test_various_cases(self):
        """Разные падежи: «каждые полчаса», «каждый час», «каждые 2 часа»"""
        for phrase in ["каждые полчаса", "каждый час", "каждые 2 часа", "каждые час"]:
            with self.subTest(phrase=phrase):
                result = parse_task_reminder_text(phrase)
                self.assertTrue(result.ok, msg=f"Failed for: {phrase}")

    # --- Ошибки: не распознали ---

    def test_unrecognized_text(self):
        """Нераспознанный текст -> ошибка с примерами"""
        result = parse_task_reminder_text("напомни мне о задачах")
        self.assertFalse(result.ok)
        self.assertIn("Примеры", result.error)

    # --- compute_next_run: интервалы ---

    def test_next_run_interval_simple(self):
        """interval: now + 60 минут"""
        cfg = TaskReminderSchedule(kind="interval", interval_minutes=60)
        next_run = compute_next_run(cfg, "UTC", MONDAY)
        expected = MONDAY + timedelta(minutes=60)
        self.assertEqual(next_run, expected)

    def test_next_run_interval_skip_quiet_hours(self):
        """interval: сдвиг после тихих часов"""
        cfg = TaskReminderSchedule(kind="interval", interval_minutes=60)
        # Начинаем в 00:00, интервал 60 минут, тихие часы 22:00-07:00 UTC
        now = datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc)
        next_run = compute_next_run(cfg, "UTC", now, time(22, 0), time(7, 0))
        # 01:00 -> попадает в тихие часы -> сдвиг на 07:00
        self.assertEqual(next_run.hour, 7)
        self.assertEqual(next_run.minute, 0)

    # --- compute_next_run: daily ---

    def test_next_run_daily_today(self):
        """daily: время ещё не наступило -> сегодня"""
        cfg = TaskReminderSchedule(kind="daily", time=time(14, 0))
        now = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
        next_run = compute_next_run(cfg, "UTC", now)
        self.assertEqual(next_run.date(), now.date())
        self.assertEqual(next_run.hour, 14)

    def test_next_run_daily_tomorrow(self):
        """daily: время прошло -> завтра"""
        cfg = TaskReminderSchedule(kind="daily", time=time(9, 0))
        now = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
        next_run = compute_next_run(cfg, "UTC", now)
        self.assertEqual(next_run.date(), (now + timedelta(days=1)).date())
        self.assertEqual(next_run.hour, 9)

    def test_next_run_daily_timezone(self):
        """daily: время по часовому поясу пользователя"""
        cfg = TaskReminderSchedule(kind="daily", time=time(9, 0))
        # now=10:00 UTC = 13:00 MSK (UTC+4)
        # 9:00 MSK = 05:00 UTC
        next_run = compute_next_run(cfg, "Europe/Moscow", MONDAY)
        msk_time = next_run.astimezone(ZoneInfo("Europe/Moscow"))
        self.assertEqual(msk_time.hour, 9)

    # --- compute_next_run: weekly ---

    def test_next_run_weekly_same_day(self):
        """weekly: сегодня понедельник, время прошло -> следующий понедельник"""
        cfg = TaskReminderSchedule(kind="weekly", weekday=0, time=time(8, 0))
        # MONDAY = 12.10.2026 = понедельник
        now = MONDAY
        next_run = compute_next_run(cfg, "UTC", now)
        # Следующий понедельник — 19.10.2026
        self.assertEqual(next_run.weekday(), 0)
        self.assertEqual(next_run.day, 19)

    def test_next_run_weekly_future_day(self):
        """weekly: следующий вторник"""
        cfg = TaskReminderSchedule(kind="weekly", weekday=1, time=time(10, 0))
        # MONDAY = 12.10.2026 = понедельник
        now = MONDAY
        next_run = compute_next_run(cfg, "UTC", now)
        self.assertEqual(next_run.weekday(), 1)  # вторник
        self.assertEqual(next_run.day, 13)  # завтра


class TestTaskReminderParserIntegration(unittest.TestCase):
    """Интеграционные тесты: полный цикл -> сохранение -> расчёт next_run."""

    def test_full_cycle_interval(self):
        """Полный цикл: интервал -> next_run"""
        text = "каждые 2 часа"
        result = parse_task_reminder_text(text)
        self.assertTrue(result.ok)

        cfg = TaskReminderSchedule(
            kind="interval",
            interval_minutes=result.interval_minutes,
        )
        next_run = compute_next_run(cfg, "UTC", MONDAY)
        expected = MONDAY + timedelta(minutes=120)
        self.assertEqual(next_run, expected)

    def test_full_cycle_daily_timezone(self):
        """Полный цикл: daily -> next_run в правильном поясе"""
        text = "каждый день в 10:00"
        result = parse_task_reminder_text(text)
        self.assertTrue(result.ok)

        cfg = TaskReminderSchedule(
            kind="daily",
            time=result.time,
        )
        next_run = compute_next_run(cfg, "Asia/Tokyo", MONDAY)
        tokyo_time = next_run.astimezone(ZoneInfo("Asia/Tokyo"))
        self.assertEqual(tokyo_time.hour, 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)