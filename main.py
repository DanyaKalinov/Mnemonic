import asyncio
import html
import logging
import os
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from config import BOT_TOKEN, ADMIN_ID
from handlers import router
from db import (
    init_db, close_db,
    get_due_reminders, mark_reminder_sent, reschedule_reminder,
    list_approved_users, get_recent_memories, set_last_digest_at,
    get_user, get_users_with_task_reminders, get_user_tasks,
    claim_task_reminder_run, disable_task_reminders,
)
from ai_core import summarize_for_digest
from access_control import ApprovalMiddleware
from error_handler import register_error_handler
from eva_ws import eva_ws_handler
# notify_agent_reminder объявлена в eva_agent_registry (раньше ошибочно импортировалась из eva_ws).
from eva_agent_registry import notify_agent_reminder
from task_reminder_parser import next_run_from_config
from bot_commands import set_default_commands, sync_commands_for_user
# FSM-состояния хранятся в PostgreSQL и переживают деплой (вместо MemoryStorage)
from fsm_storage import PostgresStorage

logging.basicConfig(level=logging.INFO)

# === ЗАГРУЗКА НАСТРОЕК ===
# Секрет вебхука — придумай любую длинную случайную строку и сохрани в .env.
# Telegram будет присылать её в заголовке X-Telegram-Bot-Api-Secret-Token,
# так мы отличаем настоящие апдейты от Telegram от чужих запросов.
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")
if not WEBHOOK_SECRET:
    raise ValueError("❌ TELEGRAM_WEBHOOK_SECRET не задан в .env")

WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")

# Публичный адрес сервиса. На Render эта переменная выставляется автоматически
# для веб-сервисов (например, https://my-bot.onrender.com) — руками задавать
# не нужно. Для локального теста / другого хостинга можно переопределить.
BASE_WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("BASE_WEBHOOK_URL")
if not BASE_WEBHOOK_URL:
    raise ValueError(
        "❌ Не удалось определить публичный адрес сервиса. "
        "Задай BASE_WEBHOOK_URL в .env (например, https://my-bot.onrender.com)."
    )

# Render сам назначает порт через переменную PORT (по умолчанию 10000).
WEB_SERVER_HOST = os.getenv("WEB_SERVER_HOST", "0.0.0.0")
WEB_SERVER_PORT = int(os.getenv("PORT", "10000"))


# === ФОНОВАЯ ЗАДАЧА ДЛЯ НАПОМИНАНИЙ ===
async def check_reminders(bot: Bot):
    while True:
        due_reminders = await get_due_reminders()
        for reminder in due_reminders:
            user_id = reminder['user_id']
            text = reminder['text']
            recurrence = reminder.get('recurrence')
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Сделано", callback_data=f"remdone:{reminder['id']}"),
                InlineKeyboardButton(text="⏰ Отложить на час", callback_data=f"remsnooze:{reminder['id']}"),
            ]])
            try:
                await bot.send_message(user_id, f"⏰ Напоминание: {text}", reply_markup=keyboard)
                # Дублируем напоминание подключённому desktop-агенту — он покажет
                # тост и проиграет звук на ПК владельца. Если агент офлайн,
                # notify_agent_reminder вернёт False — просто пропускаем.
                await notify_agent_reminder(user_id, text)
                if recurrence:
                    # Повторяющееся — переносим на следующий срок, не помечаем отправленным насовсем
                    await reschedule_reminder(reminder['id'], recurrence)
                else:
                    await mark_reminder_sent(reminder['id'])
            except Exception as e:
                print(f"Ошибка отправки напоминания {reminder['id']}: {e}")
        await asyncio.sleep(30)



# === ФОНОВАЯ ЗАДАЧА: НАПОМИНАНИЯ О ЗАДАЧАХ ===
# Раз в минуту смотрит пользователей с включёнными напоминаниями и шлёт компактный список
# задач «в работе», когда наступил next_run_at. Настройки: users.settings["task_reminders"].
TASK_REMINDER_TICK_SECONDS = 60
TASK_REMINDER_MAX_LATE = timedelta(hours=3)   # слишком опоздавшее (после простоя) не шлём
TASK_REMINDER_MAX_LINES = 15


def _build_task_reminder_text(tasks: list[dict]) -> str:
    pending = sorted((t for t in tasks if t.get("status") == "pending"), key=lambda t: t["user_seq"])
    done = len(tasks) - len(pending)
    lines = [f"⏳ #{t['user_seq']} — {html.escape(t['title'])}" for t in pending[:TASK_REMINDER_MAX_LINES]]
    if len(pending) > TASK_REMINDER_MAX_LINES:
        lines.append(f"…и ещё {len(pending) - TASK_REMINDER_MAX_LINES}")
    return (
        "🔔 <b>Напоминание о задачах</b>\n"
        f"В работе: {len(pending)} | Готово: {done}\n\n" + "\n".join(lines)
    )


async def _task_reminders_tick(bot: Bot) -> None:
    now = datetime.now(timezone.utc)
    for row in await get_users_with_task_reminders():
        user_id = row["user_id"]
        settings = row["settings"] or {}
        if isinstance(settings, str):
            settings = json.loads(settings)
        cfg = settings.get("task_reminders") or {}
        old_next = cfg.get("next_run_at")
        if not cfg.get("kind") or not old_next:
            continue
        try:
            due_at = datetime.fromisoformat(old_next)
        except ValueError:
            continue
        if due_at > now:
            continue

        # Сначала «занимаем» запуск и сдвигаем next_run_at (от СЕЙЧАС, без догоняющих рассылок),
        # потом отправляем — так при сбое не будет бесконечных повторов.
        tz_name = settings.get("timezone") or "UTC"
        new_next = next_run_from_config(cfg, tz_name, now).isoformat()
        if not await claim_task_reminder_run(user_id, old_next, new_next):
            continue  # настройку уже поменяли или другой экземпляр бота успел раньше

        if now - due_at > TASK_REMINDER_MAX_LATE:
            continue  # бот долго не работал — устаревшее напоминание не шлём

        tasks = await get_user_tasks(user_id)
        if not any(t.get("status") == "pending" for t in tasks):
            continue  # нечего напоминать

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📊 Открыть дашборд", callback_data="taskdash_refresh:all"),
        ]])
        try:
            await bot.send_message(
                user_id, _build_task_reminder_text(tasks), reply_markup=keyboard, parse_mode="HTML",
            )
        except TelegramForbiddenError:
            logging.warning("Пользователь %s заблокировал бота — выключаю напоминания о задачах", user_id)
            await disable_task_reminders(user_id)
        except Exception as e:
            logging.exception("Не удалось отправить напоминание о задачах %s: %s", user_id, e)


async def task_reminders_loop(bot: Bot):
    while True:
        try:
            await _task_reminders_tick(bot)
        except Exception as e:  # цикл не должен умирать из-за одной ошибки
            logging.exception("Ошибка цикла напоминаний о задачах: %s", e)
        await asyncio.sleep(TASK_REMINDER_TICK_SECONDS)


# === ФОНОВАЯ ЗАДАЧА ДЛЯ ЕЖЕДНЕВНОГО ДАЙДЖЕСТА ===
# Проверяет раз в 10 минут: для каждого одобренного пользователя — сейчас
# ли у него 9:00 УТРА ПО ЕГО СОБСТВЕННОМУ часовому поясу (не по времени
# сервера — Render работает в UTC, а пользователи могут быть в любом
# поясе), и не отправляли ли ему дайджест уже сегодня (по его же дате).
async def send_daily_digest(bot: Bot):
    while True:
        user_ids = await list_approved_users()
        for user_id in user_ids:
            user = await get_user(user_id)
            if user is None:
                continue

            tz_name = (user["settings"] or {}).get("timezone") or "UTC"
            try:
                tz = ZoneInfo(tz_name)
            except Exception:
                tz = ZoneInfo("UTC")

            local_now = datetime.now(timezone.utc).astimezone(tz)
            if local_now.hour != 9:
                continue

            last_digest_at = user.get("last_digest_at")
            if last_digest_at is not None:
                last_local_date = last_digest_at.astimezone(tz).date()
                if last_local_date == local_now.date():
                    continue  # сегодня уже отправляли

            entries = await get_recent_memories(user_id, hours=24)
            if not entries:
                await set_last_digest_at(user_id)  # чтобы не проверять его снова весь час
                continue

            digest_entries = [
                {
                    "text": e["text"],
                    "created_at_local": e["created_at"].astimezone(tz).strftime("%d.%m.%Y %H:%M"),
                }
                for e in entries
            ]
            today_str = local_now.strftime("%d.%m.%Y")
            summary = await summarize_for_digest(digest_entries, today_str)
            if summary:
                try:
                    await bot.send_message(user_id, f"📰 Дайджест за день:\n\n{summary}")
                    await set_last_digest_at(user_id)
                except Exception as e:
                    print(f"Не удалось отправить дайджест {user_id}: {e}")
        await asyncio.sleep(600)


async def on_startup(bot: Bot):
    await init_db()
    webhook_url = f"{BASE_WEBHOOK_URL.rstrip('/')}{WEBHOOK_PATH}"
    await bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
    )
    print(f"🚀 Бот Ева запущен, webhook: {webhook_url}", flush=True)

    # Диагностика: сверяем с тем, что Telegram реально видит у себя.
    # Если тут url пустой — проблема на стороне Telegram/токена, а не в коде.
    info = await bot.get_webhook_info()
    print(f"🔍 Telegram видит webhook как: {info.url!r}", flush=True)
    if info.last_error_message:
        print(f"⚠️ Последняя ошибка доставки от Telegram: {info.last_error_message}", flush=True)


    # Меню команд Telegram (кнопка рядом со строкой ввода).
    # Дефолтное — минимальное, для всех незнакомых/неодобренных чатов.
    # Плюс сразу выставляем полное меню для ADMIN_ID — он же owner с бутстрапа.
    # Для остальных пользователей персональное меню выставляется в момент
    # их /start и при выдаче/отзыве доступа к Еве (см. handlers.py).
    #
    # Дополнительно на старте обновляем меню ВСЕМ одобренным пользователям:
    # так новые команды (например /screenshot, /find, /open) появляются у них
    # в меню сразу после деплоя, не дожидаясь их следующего /start.
    await set_default_commands(bot)
    approved_ids = await list_approved_users()
    for uid in approved_ids:
        try:
            user = await get_user(uid)
            await sync_commands_for_user(bot, uid, user)
        except Exception as e:
            print(f"⚠️ Не удалось обновить меню команд для {uid}: {e}", flush=True)
    # На всякий случай дублируем админа (он мог быть не в списке одобренных,
    # например на бутстрапе роль ещё не выставлена).
    admin_user = await get_user(ADMIN_ID)
    await sync_commands_for_user(bot, ADMIN_ID, admin_user)


async def on_shutdown(bot: Bot):
    # ВАЖНО: НЕ вызываем bot.delete_webhook() здесь. Render делает деплой
    # без даунтайма — сначала поднимает новый инстанс (он ставит вебхук
    # через on_startup), и только потом останавливает старый. Если бы
    # старый при остановке удалял вебхук — он бы стирал то, что новый
    # инстанс только что поставил, и бот переставал бы получать апдейты
    # после каждого редеплоя. delete_webhook нужен только при осознанном
    # выводе бота из эксплуатации, не при обычном перезапуске/деплое.
    await bot.session.close()
    await close_db()


async def health(request: web.Request) -> web.Response:
    # Лёгкий эндпоинт для внешней пинговалки (UptimeRobot и т.п.),
    # чтобы Render не усыплял сервис из-за 15 минут без запросов.
    return web.Response(text="ok")


# === ГЛАВНАЯ ФУНКЦИЯ ===
def main():
    bot = Bot(token=BOT_TOKEN)

    # Состояния FSM хранятся в Supabase: диалоги не сбрасываются при деплое
    dp = Dispatcher(storage=PostgresStorage())
    dp.message.middleware(ApprovalMiddleware())
    dp.include_router(router)
    register_error_handler(dp)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    async def start_reminders(bot: Bot):
        asyncio.create_task(check_reminders(bot))
        asyncio.create_task(send_daily_digest(bot))
        asyncio.create_task(task_reminders_loop(bot))

    dp.startup.register(start_reminders)

    app = web.Application()
    app["bot"] = bot  # нужен внутри eva_ws.py, чтобы слать уведомления владельцу
    app.router.add_get("/health", health)
    app.router.add_get("/ws/eva", eva_ws_handler)
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    ).register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)

    web.run_app(app, host=WEB_SERVER_HOST, port=WEB_SERVER_PORT)


if __name__ == "__main__":
    main()
