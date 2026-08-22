import asyncio
import os
import tempfile
import re
import dateparser
from datetime import datetime
from aiogram import Router, types
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
import PyPDF2
import requests
from dotenv import load_dotenv

from config import ADMIN_ID
from db import (
    add_user, user_exists, get_user, set_user_status,
    add_reminder, get_user_reminders, snooze_reminder,
    get_user_memories, delete_memory,
    list_users, get_stats,
)
from memory import add_memory
from ai_core import get_ai_response, describe_image

load_dotenv()
router = Router()

# ----- Вспомогательная функция для распознавания голоса (Yandex SpeechKit) -----
async def recognize_speech(file_path: str) -> str:
    api_key = os.getenv("YANDEX_SPEECHKIT_KEY")
    if not api_key:
        return "⚠️ Не настроен ключ SpeechKit."
    url = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
    headers = {"Authorization": f"Api-Key {api_key}"}
    with open(file_path, "rb") as f:
        audio_data = f.read()
    params = {"lang": "ru-RU", "format": "oggopus"}
    try:
        response = await asyncio.to_thread(
            requests.post, url, headers=headers, params=params, data=audio_data
        )
        response.raise_for_status()
        result = response.json()
        return result.get("result", "Не удалось распознать речь.")
    except Exception as e:
        print(f"Ошибка SpeechKit: {e}")
        return "😵 Не смог разобрать голосовое."

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
            await add_user(user_id, status="approved")

    welcome_text = (
        f"🧠 Привет, {username}!\n\n"
        "Я — твой **Второй Мозг**. Я запоминаю всё, что ты мне пишешь,\n"
        "и могу найти это по смыслу даже через месяцы.\n\n"
        "📌 **Просто напиши мне что угодно** — я сохраню это в память.\n"
        "🔍 **Спроси меня о чём‑то** — я найду ответ среди твоих записей.\n"
        "📄 **Отправь PDF** — я прочитаю и запомню его содержание.\n"
        "🎤 **Отправь голосовое** — я распознаю речь и сохраню текст.\n"
        "🖼 **Отправь фото** — я опишу его и распознаю текст, если есть.\n\n"
        "⏰ **Напоминания**: напиши «Напомни мне [текст] через 30 минут»\n"
        "🔁 Можно и повторяющиеся: «Напомни каждый день пить витамины в 9:00»\n\n"
        "Для списка всех команд отправь /help"
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

# ----- Команда /help -----
@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "🤖 **Что я умею:**\n\n"
        "📝 /start — Показать приветствие\n"
        "❓ /help — Показать справку\n"
        "📋 /reminders — Список активных напоминаний\n"
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
        "• «Напомни каждую неделю [текст]»\n\n"
        "⚡️ Все данные хранятся только у тебя в персональной базе."
    )
    if message.from_user.id == ADMIN_ID:
        help_text += (
            "\n\n👑 **Команды администратора:**\n"
            "👥 /users — Список пользователей\n"
            "🚫 /revoke <id> — Отозвать доступ у пользователя\n"
            "📊 /stats — Статистика бота"
        )
    await message.answer(help_text)

# ----- Команда /reminders (список активных напоминаний) -----
@router.message(Command("reminders"))
async def cmd_reminders(message: Message):
    user_id = message.from_user.id
    reminders = await get_user_reminders(user_id)
    active = [r for r in reminders if not r['is_sent']]
    if not active:
        await message.answer("📭 У тебя нет активных напоминаний.")
        return
    lines = []
    for r in active:
        suffix = " 🔁" if r.get('recurrence') else ""
        lines.append(f"• {r['text']} — {r['notify_at']}{suffix}")
    await message.answer("📋 Активные напоминания:\n" + "\n".join(lines))

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

# ----- Обработка текста (запоминание + AI-ответ) -----
@router.message(lambda msg: msg.text is not None)
async def handle_text(message: Message):
    if message.text.startswith('/'):
        await message.answer("❓ Неизвестная команда. Напиши /help.")
        return

    user_id = message.from_user.id
    user_text = message.text

    lower_text = user_text.lower()
    if lower_text.startswith("напомни") or lower_text.startswith("напомни мне"):
        await handle_reminder_command(message)
        return

    await add_memory(user_id, user_text)

    await message.answer("🧠 Думаю...")
    answer = await get_ai_response(user_id, user_text)
    await message.answer(answer)

# ----- Обработка напоминаний (внутренняя функция) -----
async def handle_reminder_command(message: Message):
    text = message.text
    user_id = message.from_user.id

    # Повторяющееся напоминание: "каждый день/каждую неделю/каждый месяц"
    recurrence = None
    recurrence_match = re.search(r'кажд(?:ый|ую|ое)\s+(день|недел[юи]|месяц)', text, re.IGNORECASE)
    if recurrence_match:
        unit = recurrence_match.group(1).lower()
        if unit.startswith('день'):
            recurrence = 'daily'
        elif unit.startswith('недел'):
            recurrence = 'weekly'
        elif unit.startswith('месяц'):
            recurrence = 'monthly'
        # вырезаем фразу про повтор, чтобы не мешала дальнейшему парсингу
        text = (text[:recurrence_match.start()] + text[recurrence_match.end():]).strip()

    # Шаблон: "через N минут/часов/дней"
    match = re.search(r'через\s+(\d+)\s+(минут|минуты|минуту|час|часа|часов|день|дня|дней)', text, re.IGNORECASE)
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        if unit.startswith('минут'):
            delta = amount * 60
        elif unit.startswith('час'):
            delta = amount * 3600
        elif unit.startswith('день') or unit.startswith('дн'):
            delta = amount * 86400
        else:
            await message.answer("⏰ Не могу понять единицу времени. Используй: минуты, часы, дни.")
            return
        remind_at = datetime.now().timestamp() + delta
        remind_at_str = datetime.fromtimestamp(remind_at).strftime('%Y-%m-%d %H:%M:%S')
        reminder_text = text[:match.start()].strip()
        if not reminder_text or reminder_text.lower() in ('напомни', 'напомни мне'):
            await message.answer("⚠️ Напиши, что именно напомнить. Например: «Напомни мне позвонить маме через 30 минут».")
            return
        await add_reminder(user_id, reminder_text, remind_at_str, recurrence)
        suffix = " 🔁 (повторяется)" if recurrence else ""
        await message.answer(f"✅ Напомню **{reminder_text}** {match.group(0)}{suffix}.")
        return

    # Альтернативный формат: "завтра в 15:00" или "в 15:00"
    match_date = re.search(r'(завтра|сегодня|в\s+\d{1,2}:\d{2})', text, re.IGNORECASE)
    if match_date:
        reminder_text = text[:match_date.start()].strip()
        if not reminder_text or reminder_text.lower() in ('напомни', 'напомни мне'):
            await message.answer("⚠️ Напиши, что именно напомнить.")
            return
        parsed = dateparser.parse(match_date.group(0), settings={'PREFER_DATES_FROM': 'future'})
        if parsed:
            remind_at_str = parsed.strftime('%Y-%m-%d %H:%M:%S')
            await add_reminder(user_id, reminder_text, remind_at_str, recurrence)
            suffix = " 🔁 (повторяется)" if recurrence else ""
            await message.answer(f"✅ Напомню **{reminder_text}** {parsed.strftime('%d.%m.%Y в %H:%M')}{suffix}.")
            return
        else:
            await message.answer("⏰ Не могу разобрать дату. Попробуй: «через 30 минут» или «завтра в 15:00».")
            return

    if recurrence and not match and not match_date:
        # Повтор указан, но конкретное время — нет: по умолчанию берём "завтра в это же время"
        remind_at_str = (datetime.now()).strftime('%Y-%m-%d %H:%M:%S')
        reminder_text = text.strip()
        if reminder_text.lower() in ('напомни', 'напомни мне', ''):
            await message.answer("⚠️ Напиши, что именно напомнить.")
            return
        await add_reminder(user_id, reminder_text, remind_at_str, recurrence)
        await message.answer(f"✅ Буду напоминать **{reminder_text}** 🔁, начиная с сегодня.")
        return

    await message.answer(
        "⏰ Формат напоминания:\n"
        "• «Напомни мне [текст] через 30 минут»\n"
        "• «Напомни [текст] завтра в 15:00»\n"
        "• «Напомни каждый день [текст] в 9:00»"
    )

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
    recognized_text = await recognize_speech(tmp_path)
    os.unlink(tmp_path)

    if "Не удалось" in recognized_text or "Не смог" in recognized_text:
        await message.answer(f"⚠️ {recognized_text}")
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