import asyncio
import os
import tempfile
from aiogram import Router, types
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
import PyPDF2
import requests
from dotenv import load_dotenv

from config import ADMIN_ID
from db import (
    add_user, user_exists, get_user, set_user_status,
    add_reminder, get_user_reminders, snooze_reminder, delete_reminder,
    get_user_memories, delete_memory,
    list_users, get_stats,
)
from reminder_parser import parse_reminder_text
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
        "📋 /reminders — Список активных напоминаний (можно отменять)\n"
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
    await message.answer(help_text)

# ----- Команда /reminders (список активных напоминаний + удаление) -----
@router.message(Command("reminders"))
async def cmd_reminders(message: Message):
    user_id = message.from_user.id
    reminders = await get_user_reminders(user_id)
    active = [r for r in reminders if not r['is_sent']]
    if not active:
        await message.answer("📭 У тебя нет активных напоминаний.")
        return
    await message.answer("📋 Активные напоминания:")
    for r in active:
        suffix = " 🔁" if r.get('recurrence') else ""
        when = r['notify_at'].strftime('%d.%m.%Y %H:%M')
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
    result = parse_reminder_text(message.text)

    if not result.ok:
        await message.answer(result.error)
        return

    await add_reminder(message.from_user.id, result.reminder_text, result.remind_at, result.recurrence)
    when_label = result.remind_at.strftime('%d.%m.%Y %H:%M')
    suffix = " 🔁 (повторяется)" if result.recurrence else ""
    await message.answer(f"✅ Напомню **{result.reminder_text}** {when_label}{suffix}.")

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