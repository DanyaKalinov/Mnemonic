import asyncio
import os
import tempfile
from zoneinfo import ZoneInfo
from aiogram import Router, types
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)
import PyPDF2
from dotenv import load_dotenv
from timezonefinder import TimezoneFinder

from config import ADMIN_ID
from db import (
    add_user, user_exists, get_user, set_user_status, update_user_settings,
    add_reminder, get_user_reminders, snooze_reminder, delete_reminder,
    get_user_memories, delete_memory,
    list_users, get_stats,
    set_user_role, is_owner, add_eva_command,
    can_use_eva, set_eva_access, list_owner_ids, list_eva_access_users,
)
from reminder_parser import parse_reminder_text
from memory import add_memory
from ai_core import get_ai_response, describe_image, transcribe_voice
from eva_ws import is_agent_connected, push_command_to_agents

load_dotenv()
# TimezoneFinder держит в памяти геоданные — создаём один раз на процесс,
# а не при каждом сообщении с локацией.
_tz_finder = TimezoneFinder()
router = Router()

# ----- Текст с текущим уровнем доступа (боту / полным функциям Евы) -----
def _eva_access_line(user: dict) -> str:
    if user["role"] == "owner":
        return "🤖 Доступ к полным функциям (Ева): ✅ есть (владелец)"
    if user["eva_access"] == "granted":
        return "🤖 Доступ к полным функциям (Ева): ✅ есть"
    if user["eva_access"] == "pending":
        return "🤖 Доступ к полным функциям (Ева): ⏳ заявка на рассмотрении"
    return "🤖 Доступ к полным функциям (Ева): ❌ нет — запросить: /eva_request"


# ----- Уведомление админа о новой/повторной заявке -----
async def notify_admin_new_request(bot, user_id: int, tg_user: types.User) -> None:
    username_display = f"@{tg_user.username}" if tg_user.username else tg_user.full_name
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{user_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{user_id}"),
    ]])
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🆕 Новая заявка на доступ к боту:\n{username_display} (ID: {user_id})",
            reply_markup=keyboard,
        )
    except Exception as e:
        print(f"Не удалось уведомить администратора о заявке {user_id}: {e}")

# ----- Команда /start -----
@router.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.first_name or "Друг"

    if user_id != ADMIN_ID:
        user = await get_user(user_id)
        if user is None:
            await add_user(user_id)
            print(f"🆕 Новая заявка на доступ: {username} (ID: {user_id})")
            await notify_admin_new_request(message.bot, user_id, message.from_user)
            await message.answer(
                "📨 Заявка на доступ отправлена администратору.\n"
                "Как только её одобрят, я сам тебе напишу — тогда и начнём."
            )
            return
        elif user["status"] == "pending":
            await message.answer("⏳ Твоя заявка уже на рассмотрении. Дождись решения администратора.")
            return
        elif user["status"] == "rejected":
            await set_user_status(user_id, "pending")
            print(f"🔁 Повторная заявка на доступ: {username} (ID: {user_id})")
            await notify_admin_new_request(message.bot, user_id, message.from_user)
            await message.answer("📨 Заявка отправлена администратору повторно.")
            return
    else:
        if not await user_exists(user_id):
            await add_user(user_id, status="approved", role="owner")

    user = await get_user(user_id)
    access_line = _eva_access_line(user)

    welcome_text = (
        f"🧠 Привет, {username}!\n\n"
        "Я — твой **Второй Мозг**, часть экосистемы **Евы**. Я запоминаю всё, что ты мне пишешь,\n"
        "и могу найти это по смыслу даже через месяцы.\n\n"
        "📌 **Просто напиши мне что угодно** — я сохраню это в память.\n"
        "🔍 **Спроси меня о чём‑то** — я найду ответ среди твоих записей.\n"
        "📄 **Отправь PDF** — я прочитаю и запомню его содержание.\n"
        "🎤 **Отправь голосовое** — я распознаю речь и сохраню текст.\n"
        "🖼 **Отправь фото** — я опишу его и распознаю текст, если есть.\n\n"
        "⏰ **Напоминания**: напиши «Напомни мне [текст] через 30 минут»\n"
        "🔁 Можно и повторяющиеся: «Напомни каждый день пить витамины в 9:00»\n\n"
        "🌍 **Сначала настрой часовой пояс** — отправь /timezone, иначе "
        "напоминания будут приходить по UTC, а не по твоему местному времени.\n\n"
        f"{access_line}\n\n"
        "Для списка всех команд отправь /help, для проверки статуса — /status"
    )
    await message.answer(welcome_text)

# ----- Обработка нажатий на кнопки "Одобрить"/"Отклонить" -----
@router.callback_query(lambda c: c.data and (c.data.startswith("approve:") or c.data.startswith("reject:")))
async def handle_approval_decision(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только администратор может это делать.", show_alert=True)
        return

    action, user_id_str = callback.data.split(":")
    target_user_id = int(user_id_str)

    if action == "approve":
        await set_user_status(target_user_id, "approved")
        await callback.message.edit_text(callback.message.text + "\n\n✅ Одобрено")
        try:
            await callback.bot.send_message(
                target_user_id,
                "🎉 Твою заявку одобрили! Напиши /start, чтобы начать пользоваться ботом."
            )
        except Exception as e:
            print(f"Не удалось уведомить пользователя {target_user_id} об одобрении: {e}")
    else:
        await set_user_status(target_user_id, "rejected")
        await callback.message.edit_text(callback.message.text + "\n\n❌ Отклонено")
        try:
            await callback.bot.send_message(
                target_user_id,
                "😔 Заявку отклонили. Можешь запросить доступ снова, написав /start."
            )
        except Exception as e:
            print(f"Не удалось уведомить пользователя {target_user_id} об отклонении: {e}")

    await callback.answer()

# ----- Кнопки под напоминанием: "Сделано" / "Отложить на час" -----
@router.callback_query(lambda c: c.data and c.data.startswith("remdone:"))
async def handle_reminder_done(callback: CallbackQuery):
    await callback.message.edit_text(callback.message.text + "\n\n✅ Отмечено как сделано")
    await callback.answer()

@router.callback_query(lambda c: c.data and c.data.startswith("remsnooze:"))
async def handle_reminder_snooze(callback: CallbackQuery):
    reminder_id = int(callback.data.split(":")[1])
    await snooze_reminder(reminder_id, minutes=60)
    await callback.message.edit_text(callback.message.text + "\n\n⏰ Отложено на час")
    await callback.answer()

# ----- Команда /timezone (определение часового пояса по геолокации) -----
@router.message(Command("timezone"))
async def cmd_timezone(message: Message):
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Поделиться локацией", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        "Нажми на кнопку ниже, чтобы поделиться геопозицией — я определю твой часовой "
        "пояс и буду присылать напоминания по твоему местному времени, а не по UTC.",
        reply_markup=keyboard,
    )

# ----- Обработка геолокации -----
@router.message(lambda msg: msg.location is not None)
async def handle_location(message: Message):
    tz_name = _tz_finder.timezone_at(
        lat=message.location.latitude, lng=message.location.longitude
    )
    if not tz_name:
        await message.answer(
            "😕 Не смог определить часовой пояс по этой точке. Попробуй ещё раз через /timezone.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    user = await get_user(message.from_user.id)
    settings = (user["settings"] if user else {}) or {}
    settings["timezone"] = tz_name
    await update_user_settings(message.from_user.id, settings)

    await message.answer(
        f"✅ Часовой пояс определён: {tz_name}. Теперь напоминания будут приходить по этому времени.",
        reply_markup=ReplyKeyboardRemove(),
    )

# ----- Команда /help -----
@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "🤖 **Что я умею:**\n\n"
        "📝 /start — Показать приветствие\n"
        "❓ /help — Показать справку\n"
        "📊 /status — Проверить уровень доступа (бот / Ева)\n"
        "📋 /reminders — Список активных напоминаний (можно отменять)\n"
        "🌍 /timezone — Настроить часовой пояс по геолокации\n"
        "🗂 /memory — Последние записи в памяти (можно удалять)\n\n"
        "🧠 **Просто напиши текст** — я запомню его\n"
        "🔎 **Задай вопрос** — я найду ответ среди твоих записей\n"
        "📄 **Отправь PDF** — я прочитаю и запомню его содержание\n"
        "🎤 **Отправь голосовое** — я распознаю речь и сохраню текст\n"
        "🖼 **Отправь фото** — я опишу его и распознаю текст, если есть\n\n"
        "⏰ **Напоминания:**\n"
        "• «Напомни мне [текст] через 30 минут»\n"
        "• «Напомни [текст] завтра в 15:00»\n"
        "🔁 **Повторяющиеся:**\n"
        "• «Напомни каждый день [текст] в 9:00»\n"
        "• «Напоминай каждый вторник [текст]»\n\n"
        "⚡️ Все данные хранятся только у тебя в персональной базе."
    )
    if message.from_user.id == ADMIN_ID:
        help_text += (
            "\n\n👑 **Команды администратора:**\n"
            "👥 /users — Список пользователей\n"
            "🚫 /revoke <id> — Отозвать доступ у пользователя\n"
            "📊 /stats — Статистика бота"
        )
    if await is_owner(message.from_user.id):
        help_text += (
            "\n\n🤖 **Команды Евы (владелец):**\n"
            "🎛 /eva <команда> — Отдать команду desktop-агенту\n"
            "📡 /agent_status — Подключён ли desktop-агент\n"
            "👥 /eva_users — Кто имеет доступ к Еве\n"
            "🚫 /eva_revoke <id> — Отозвать доступ к Еве"
        )
    elif await can_use_eva(message.from_user.id):
        help_text += (
            "\n\n🤖 **Ева (доступ выдан):**\n"
            "🎛 /eva <команда> — Отдать команду desktop-агенту\n"
            "📡 /agent_status — Подключён ли desktop-агент"
        )
    else:
        help_text += (
            "\n\n🤖 **Ева** — голосовой ИИ-помощник с доступом к ПК.\n"
            "🙋 /eva_request — Запросить доступ у владельца"
        )
    await message.answer(help_text)

# ----- Команда /status — уровень доступа пользователя -----
@router.message(Command("status"))
async def cmd_status(message: Message):
    user = await get_user(message.from_user.id)
    if user is None:
        await message.answer("Похоже, ты ещё не зарегистрирован. Отправь /start.")
        return

    bot_access_map = {
        "approved": "✅ одобрен",
        "pending": "⏳ заявка на рассмотрении",
        "rejected": "❌ отклонён — можно подать заявку снова через /start",
    }
    bot_access_line = f"📋 Доступ к боту: {bot_access_map.get(user['status'], user['status'])}"
    access_line = _eva_access_line(user)

    await message.answer(f"{bot_access_line}\n{access_line}")

# ----- Команда /reminders (список активных напоминаний + удаление) -----
@router.message(Command("reminders"))
async def cmd_reminders(message: Message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    tz_name = ((user["settings"] if user else {}) or {}).get("timezone") or "UTC"

    reminders = await get_user_reminders(user_id)
    active = [r for r in reminders if not r['is_sent']]
    if not active:
        await message.answer("📭 У тебя нет активных напоминаний.")
        return
    await message.answer("📋 Активные напоминания:")
    for r in active:
        suffix = " 🔁" if r.get('recurrence') else ""
        local_time = r['notify_at'].astimezone(ZoneInfo(tz_name))
        when = local_time.strftime('%d.%m.%Y %H:%M')
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Отменить", callback_data=f"delrem:{r['id']}"),
        ]])
        await message.answer(f"• {r['text']} — {when}{suffix}", reply_markup=keyboard)

# ----- Обработка нажатия "Отменить" в /reminders -----
@router.callback_query(lambda c: c.data and c.data.startswith("delrem:"))
async def handle_delete_reminder(callback: CallbackQuery):
    reminder_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    deleted = await delete_reminder(reminder_id, user_id)
    if deleted:
        await callback.message.edit_text("🗑 Напоминание отменено.")
    else:
        await callback.message.edit_text("⚠️ Напоминание уже отменено или не найдено.")
    await callback.answer()

# ----- Команда /memory (список последних записей + удаление) -----
@router.message(Command("memory"))
async def cmd_memory(message: Message):
    user_id = message.from_user.id
    entries = await get_user_memories(user_id, limit=10)
    if not entries:
        await message.answer("📭 Пока пусто — ничего не сохранено в память.")
        return
    await message.answer("🗂 Последние записи в памяти:")
    for entry in entries:
        tag_label = f"[{entry['tag']}] " if entry.get('tag') else ""
        preview = entry['text'][:200]
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delmem:{entry['id']}"),
        ]])
        await message.answer(f"{tag_label}{preview}", reply_markup=keyboard)

# ----- Обработка нажатия "Удалить" в /memory -----
@router.callback_query(lambda c: c.data and c.data.startswith("delmem:"))
async def handle_delete_memory(callback: CallbackQuery):
    memory_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    deleted = await delete_memory(memory_id, user_id)
    if deleted:
        await callback.message.edit_text("🗑 Запись удалена.")
    else:
        await callback.message.edit_text("⚠️ Запись уже удалена или не найдена.")
    await callback.answer()

# ----- Админ: /users -----
@router.message(Command("users"))
async def cmd_users(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    users = await list_users(limit=50)
    if not users:
        await message.answer("Пока нет ни одного пользователя.")
        return
    status_emoji = {"approved": "✅", "pending": "⏳", "rejected": "❌"}
    lines = [
        f"{status_emoji.get(u['status'], '❔')} `{u['user_id']}` — {u['status']}"
        for u in users
    ]
    await message.answer("👥 Пользователи (последние 50):\n" + "\n".join(lines))

# ----- Админ: /revoke <id> -----
@router.message(Command("revoke"))
async def cmd_revoke(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /revoke <telegram_id>")
        return
    target_user_id = int(parts[1])
    await set_user_status(target_user_id, "rejected")
    await message.answer(f"🚫 Доступ у пользователя `{target_user_id}` отозван.")
    try:
        await message.bot.send_message(
            target_user_id,
            "🚫 Твой доступ к боту был отозван администратором. "
            "Можешь запросить его снова через /start."
        )
    except Exception as e:
        print(f"Не удалось уведомить пользователя {target_user_id} об отзыве доступа: {e}")

# ----- Админ: /stats -----
@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    stats = await get_stats()
    text = (
        "📊 **Статистика бота:**\n\n"
        f"👥 Всего пользователей: {stats['total_users']}\n"
        f"✅ Одобрено: {stats['approved_users']}\n"
        f"⏳ Ждут одобрения: {stats['pending_users']}\n"
        f"🗂 Записей в памяти: {stats['total_memories']}\n"
        f"⏰ Активных напоминаний: {stats['active_reminders']}"
    )
    await message.answer(text)

# ----- Ева: запрос доступа обычным пользователем -----
@router.message(Command("eva_request"))
async def cmd_eva_request(message: Message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    if user is None:
        return  # мидлварь и так не пропустит неодобренного, но на всякий случай
    if user["role"] == "owner" or user["eva_access"] == "granted":
        await message.answer("✅ У тебя уже есть доступ к Еве — просто используй /eva <команда>.")
        return
    if user["eva_access"] == "pending":
        await message.answer("⏳ Заявка уже отправлена, жди решения владельца.")
        return

    await set_eva_access(user_id, "pending")
    username = message.from_user.username or message.from_user.full_name

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Разрешить", callback_data=f"eva_grant:{user_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"eva_deny:{user_id}"),
    ]])
    owner_ids = await list_owner_ids()
    for owner_id in owner_ids:
        try:
            await message.bot.send_message(
                owner_id,
                f"🤖 Запрос доступа к Еве:\n@{username} (ID: {user_id})",
                reply_markup=keyboard,
            )
        except Exception as e:
            print(f"⚠️ Не удалось уведомить владельца {owner_id} о запросе Евы: {e}")

    await message.answer("📨 Заявка отправлена владельцу, жди решения.")

# ----- Ева: владелец разрешает/отклоняет запрос -----
@router.callback_query(lambda c: c.data and (c.data.startswith("eva_grant:") or c.data.startswith("eva_deny:")))
async def handle_eva_access_decision(callback: CallbackQuery):
    if not await is_owner(callback.from_user.id):
        await callback.answer("Только владелец может решать это.", show_alert=True)
        return

    action, user_id_str = callback.data.split(":")
    target_id = int(user_id_str)

    if action == "eva_grant":
        await set_eva_access(target_id, "granted")
        await callback.message.edit_text(callback.message.text + "\n\n✅ Доступ разрешён.")
        try:
            await callback.bot.send_message(target_id, "✅ Тебе разрешили доступ к Еве. Используй /eva <команда>.")
        except Exception:
            pass
    else:
        await set_eva_access(target_id, "none")
        await callback.message.edit_text(callback.message.text + "\n\n❌ Доступ отклонён.")
        try:
            await callback.bot.send_message(target_id, "❌ Доступ к Еве отклонён.")
        except Exception:
            pass
    await callback.answer()

# ----- Ева: владелец отзывает ранее выданный доступ -----
@router.message(Command("eva_revoke"))
async def cmd_eva_revoke(message: Message):
    if not await is_owner(message.from_user.id):
        await message.answer("⛔ Отзывать доступ к Еве может только владелец.")
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /eva_revoke <user_id>\nСписок ID — /eva_users")
        return
    target_id = int(parts[1])
    await set_eva_access(target_id, "none")
    await message.answer(f"🚫 Доступ к Еве отозван у пользователя {target_id}.")
    try:
        await message.bot.send_message(target_id, "🚫 Твой доступ к Еве отозван владельцем.")
    except Exception:
        pass

# ----- Ева: владелец смотрит, у кого есть доступ -----
@router.message(Command("eva_users"))
async def cmd_eva_users(message: Message):
    if not await is_owner(message.from_user.id):
        await message.answer("⛔ Эта команда доступна только владельцам.")
        return
    users = await list_eva_access_users()
    if not users:
        await message.answer("Пока никто, кроме владельца, не запрашивал доступ к Еве.")
        return
    lines = ["🤖 **Доступ к Еве:**\n"]
    icons = {"owner": "👑", "granted": "✅", "pending": "⏳"}
    for u in users:
        label = "owner" if u["role"] == "owner" else u["eva_access"]
        lines.append(f"{icons.get(label, '•')} {u['user_id']} — {label}")
    await message.answer("\n".join(lines))

# ----- Ева: /agent_status — подключён ли desktop-агент -----
@router.message(Command("agent_status"))
async def cmd_agent_status(message: Message):
    if not await can_use_eva(message.from_user.id):
        await message.answer("⛔ У тебя нет доступа к Еве. Запросить: /eva_request")
        return
    if is_agent_connected():
        await message.answer("🟢 Desktop-агент Евы на связи.")
    else:
        await message.answer(
            "🔴 Desktop-агент не подключён. Команда сохранится в очередь "
            "и выполнится, как только агент подключится."
        )

# ----- Ева: /eva <команда> — поставить задачу desktop-агенту -----
@router.message(Command("eva"))
async def cmd_eva(message: Message):
    if not await can_use_eva(message.from_user.id):
        await message.answer("⛔ У тебя нет доступа к Еве. Запросить: /eva_request")
        return

    command_text = message.text.partition(" ")[2].strip()
    if not command_text:
        await message.answer(
            "Использование: /eva <команда>\n"
            "Например: /eva открой блокнот"
        )
        return

    command_id = await add_eva_command(message.from_user.id, command_text)
    sent = await push_command_to_agents(command_id, command_text)

    if sent:
        await message.answer(f"📤 Команда «{command_text}» отправлена Еве, жду результат...")
    else:
        await message.answer(
            f"📥 Команда «{command_text}» сохранена в очередь — "
            "desktop-агент сейчас не на связи, выполнится при подключении."
        )

# ----- Обработка текста (запоминание + AI-ответ) -----
@router.message(lambda msg: msg.text is not None)
async def handle_text(message: Message):
    if message.text.startswith('/'):
        await message.answer("❓ Неизвестная команда. Напиши /help.")
        return

    user_id = message.from_user.id
    user_text = message.text

    lower_text = user_text.lower()
    if lower_text.startswith("напомни") or lower_text.startswith("напоминай"):
        await handle_reminder_command(message)
        return

    await add_memory(user_id, user_text)

    await message.answer("🧠 Думаю...")
    answer = await get_ai_response(user_id, user_text)
    await message.answer(answer)

# ----- Обработка напоминаний (внутренняя функция) -----
async def handle_reminder_command(message: Message):
    try:
        await _parse_and_save_reminder(message)
    except Exception as e:
        print(f"❌ Ошибка сохранения напоминания: {e}")
        await message.answer(
            "😵 Не получилось сохранить напоминание — что-то пошло не так на моей стороне. "
            "Попробуй ещё раз чуть позже."
        )


async def _parse_and_save_reminder(message: Message):
    user = await get_user(message.from_user.id)
    tz_name = ((user["settings"] if user else {}) or {}).get("timezone") or "UTC"

    result = parse_reminder_text(message.text, tz_name=tz_name)

    if not result.ok:
        await message.answer(result.error)
        return

    await add_reminder(message.from_user.id, result.reminder_text, result.remind_at, result.recurrence)
    local_time = result.remind_at.astimezone(ZoneInfo(tz_name))
    when_label = local_time.strftime('%d.%m.%Y %H:%M')
    suffix = " 🔁 (повторяется)" if result.recurrence else ""
    tz_note = "" if tz_name != "UTC" else " (UTC — часовой пояс не настроен, отправь /timezone)"
    await message.answer(f"✅ Напомню **{result.reminder_text}** {when_label}{suffix}{tz_note}.")

# ----- Обработка PDF -----
@router.message(lambda msg: msg.document and msg.document.mime_type == "application/pdf")
async def handle_pdf(message: Message):
    user_id = message.from_user.id
    file_id = message.document.file_id
    file_name = message.document.file_name or "документ"

    file = await message.bot.get_file(file_id)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        await message.bot.download_file(file.file_path, tmp.name)
        tmp_path = tmp.name

    def _extract_pdf_text(path: str) -> str:
        with open(path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            text = ""
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
        return text

    try:
        text = await asyncio.to_thread(_extract_pdf_text, tmp_path)
        os.unlink(tmp_path)
    except Exception as e:
        await message.answer("❌ Не удалось прочитать PDF.")
        print(f"PDF error: {e}")
        return

    if not text.strip():
        await message.answer("⚠️ PDF пуст или содержит только картинки.")
        return

    memory_text = f"[PDF: {file_name}]\n{text[:2000]}"
    await add_memory(user_id, memory_text, tag="документ")
    await message.answer(f"✅ Прочитал PDF **{file_name}** (символов: {len(text)}), сохранил в память.")

# ----- Обработка голосовых -----
@router.message(lambda msg: msg.voice is not None)
async def handle_voice(message: Message):
    user_id = message.from_user.id
    file_id = message.voice.file_id

    file = await message.bot.get_file(file_id)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp:
        await message.bot.download_file(file.file_path, tmp.name)
        tmp_path = tmp.name

    await message.answer("🎤 Распознаю голосовое...")
    recognized_text = await transcribe_voice(tmp_path)
    os.unlink(tmp_path)

    if recognized_text.startswith("⚠️") or recognized_text.startswith("😵"):
        await message.answer(recognized_text)
        return

    memory_text = f"[Голосовое]\n{recognized_text}"
    await add_memory(user_id, memory_text, tag="голосовое")
    await message.answer(f"🗣️ Распознал:\n\n{recognized_text}\n\n✅ Сохранил в память.")

# ----- Обработка фото -----
@router.message(lambda msg: msg.photo is not None)
async def handle_photo(message: Message):
    user_id = message.from_user.id
    # Берём фото в максимальном разрешении (последнее в списке размеров)
    photo = message.photo[-1]

    file = await message.bot.get_file(photo.file_id)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
        await message.bot.download_file(file.file_path, tmp.name)
        tmp_path = tmp.name

    await message.answer("🖼 Смотрю на фото...")
    try:
        with open(tmp_path, "rb") as f:
            image_bytes = f.read()
    finally:
        os.unlink(tmp_path)

    description = await describe_image(image_bytes)

    memory_text = f"[Фото]\n{description}"
    await add_memory(user_id, memory_text, tag="фото")
    await message.answer(f"🖼 {description}\n\n✅ Сохранил в память.")