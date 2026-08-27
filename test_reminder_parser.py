"""
Юнит-тесты разбора напоминаний.

Запуск:  python test_reminder_parser.py
Или через встроенный раннер:  python -m unittest test_reminder_parser
Зависимостей, кроме тех, что уже в requirements.txt, не требует.
"""
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from reminder_parser import parse_reminder_text, NO_TEXT_ERROR, NO_TIME_ERROR

# Фиксированная точка отсчёта — чтобы тесты были воспроизводимы и не
# зависели от того, в какой момент их реально запустили.
NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


class TestReminderParser(unittest.TestCase):

    # --- Базовый случай: "через N минут/часов" ---
    def test_through_minutes(self):
        result = parse_reminder_text("Напомни мне позвонить маме через 30 минут", now=NOW)
        self.assertTrue(result.ok)
        self.assertEqual(result.reminder_text, "позвонить маме")
        self.assertEqual(result.remind_at, NOW + timedelta(minutes=30))
        self.assertIsNone(result.recurrence)

    def test_through_hours(self):
        result = parse_reminder_text("Напомни выключить духовку через 2 часа", now=NOW)
        self.assertTrue(result.ok)
        self.assertEqual(result.reminder_text, "выключить духовку")
        self.assertEqual(result.remind_at, NOW + timedelta(hours=2))

    # --- Регрессионный тест на баг со скриншота: время ПЕРЕД задачей ---
    def test_time_before_task_regression(self):
        result = parse_reminder_text("Напомни сегодня в 23:00 заказать вб", now=NOW)
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.reminder_text, "заказать вб")
        self.assertEqual(result.remind_at.hour, 23)
        self.assertEqual(result.remind_at.minute, 0)

    # --- Регрессионный тест на баг со скриншота: слово "напоминай" + день недели ---
    def test_napominay_with_weekday_regression(self):
        result = parse_reminder_text("Напоминай каждый вторник отложить деньги", now=NOW)
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.reminder_text, "отложить деньги")
        self.assertEqual(result.recurrence, "weekly")

    def test_napominay_daily_with_time(self):
        result = parse_reminder_text("Напоминай каждый день пить воду в 9:00", now=NOW)
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.reminder_text, "пить воду")
        self.assertEqual(result.recurrence, "daily")
        self.assertEqual(result.remind_at.hour, 9)

    def test_recurrence_monthly(self):
        result = parse_reminder_text("Напомни каждый месяц оплатить интернет", now=NOW)
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.recurrence, "monthly")
        self.assertEqual(result.reminder_text, "оплатить интернет")

    # --- Ошибки: нет текста задачи ---
    def test_empty_task_after_trigger(self):
        result = parse_reminder_text("Напомни", now=NOW)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, NO_TEXT_ERROR)

    def test_empty_task_with_mne(self):
        result = parse_reminder_text("Напомни мне", now=NOW)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, NO_TEXT_ERROR)

    # --- Ошибка: есть текст, но непонятно когда ---
    def test_no_time_specified(self):
        result = parse_reminder_text("Напомни купить хлеб", now=NOW)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, NO_TIME_ERROR)

    # --- "завтра в HH:MM" ---
    def test_tomorrow_with_time(self):
        result = parse_reminder_text("Напомни завтра в 10:30 сходить к врачу", now=NOW)
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.reminder_text, "сходить к врачу")
        self.assertEqual(result.remind_at.day, (NOW + timedelta(days=1)).day)
        self.assertEqual(result.remind_at.hour, 10)
        self.assertEqual(result.remind_at.minute, 30)

    # --- Явная календарная дата: "30 сентября" (регрессия со скриншота) ---
    def test_explicit_month_date_regression(self):
        result = parse_reminder_text("Напомни 30 сентября что скоро на учебу", now=NOW)
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.reminder_text, "что скоро на учебу")
        self.assertEqual(result.remind_at.month, 9)
        self.assertEqual(result.remind_at.day, 30)

    # --- Часовой пояс: "сегодня в 23:00" по Стамбулу должно сработать
    # именно в 23:00 по Стамбулу, а не по UTC (регрессия со скриншота) ---
    def test_timezone_conversion_regression(self):
        result = parse_reminder_text(
            "Напомни сегодня в 23:00 заказать вб", tz_name="Europe/Istanbul", now=NOW
        )
        self.assertTrue(result.ok, msg=result.error)
        local_time = result.remind_at.astimezone(ZoneInfo("Europe/Istanbul"))
        self.assertEqual(local_time.hour, 23)
        # Стамбул — UTC+3, значит в UTC должно быть на 3 часа меньше
        self.assertEqual(result.remind_at.hour, 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)