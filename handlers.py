import asyncio
import os
import random
import tempfile
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo
from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
import PyPDF2
from dotenv import load_dotenv
from timezonefinder import TimezoneFinder

from config import ADMIN_ID
from db import (
    add_user, user_exists, get_user, set_user_status, update_user_settings,
    add_reminder, get_user_reminders, snooze_reminder, delete_reminder,
    get_user_memories, delete_memory, get_recent_memories,
    list_users, get_stats,
    set_user_role, is_owner, get_eva_command, update_eva_command_status,
    can_use_eva, set_eva_access, list_owner_ids, list_eva_access_users,
    get_user_tasks, toggle_task_status, add_single_task, add_tasks_batch, clear_completed_tasks
)
from reminder_parser import parse_reminder_text
from memory import add_memory
from ai_core import get_ai_response, describe_image, transcribe_voice, summarize_for_digest
from eva_agent_registry import is_agent_connected, push_command_to_agents
from eva_engine import process_eva_command
from bot_commands import sync_commands_for_user

load_dotenv()

_tz_finder = TimezoneFinder()
router = Router()

# ----- FSM Состояние для создания задач -----
class TaskCreationStates(StatesGroup):
    waiting_for_task_text = State()

# ----- Состояние ожидания ввода для Dashboard Евы -----
_pending_dashboard_input: dict[int, str] = {}

# Кнопки с мгновенным действием: callback-суффикс -> текст команды Еве
_DASHBOARD_DIRECT = {
    "screenshot": "сделай скриншот",
    "vol_up": "сделай громче",
    "vol_down": "сделай тише",
    "vol_mute": "выключи звук",
    "list_files": "покажи файлы в загрузках",
    "power_lock": "заблокируй компьютер",
    "power_shutdown": "выключи компьютер",
}

# Кнопки, которым нужен ввод: callback-суффикс -> (текст-подсказка, шаблон команды).
_DASHBOARD_INPUT = {
    "open_file": ("📄 Что открыть? Напиши путь или имя файла.", "открой файл {input}"),
    "search_file": ("🔍 Что найти на компьютере? Напиши название файла.", "найди файл {input}"),
    "open_website": ("🌐 Какой сайт открыть? Напиши адрес, например youtube.com.", "открой сайт {input}"),
    "web_search": ("🔎 Что найти в интернете?", "найди в интернете {input}"),
    "type_text": ("⌨️ Что напечатать в активном окне?", "напиши {input}"),
}


# ----- Хелперы для Дашборда Задач -----
def build_progress_bar(completed: int, total: int, length: int = 10) -> str:
    """Формирует текстовый прогресс-бар."""
    if total == 0:
        return "[░░░░░░░░░░] 0%"
    percent = completed / total
    filled_length = int(round(length * percent))
    bar = "█" * filled_length + "░" * (length - filled_length)
    return f"[{bar}] {int(percent * 100)}%"


def render_dashboard_text(tasks: list[dict], filter_type: str = "all") -> str:
    """Формирует текст сообщения дашборда задач."""
    total = len(tasks)
    completed_count = sum(1 for t in tasks if t.get('status') == 'completed')
    in_progress = total - completed_count

    progress_bar = build_progress_bar(completed_count, total)

    summary = (
        "📊 <b>Дашборд задач Евы</b>\n\n"
        f"🎯 <b>Прогресс:</b> {progress_bar}\n"
        f"📥 Всего: {total} | ⏳ В работе: {in_progress} | ✅ Готово: {completed_count}\n"
        "─────────────────"
    )

    filtered = tasks
    if filter_type == "pending":
        filtered = [t for t in tasks if t.get('status') == 'pending']
    elif filter_type == "completed":
        filtered = [t for t in tasks if t.get('status') == 'completed']

    if not filtered:
        return summary + "\n\n🎉 <i>Задач в этой категории нет!</i>"

    task_lines = []
    for task in filtered:
        is_done = task.get('status') == 'completed'
        status_icon = "✅" if is_done else "⏳"
        title = task['title']
        user_seq = task.get('user_seq', '?')

        if is_done:
            title = f"<s>{title}</s>"

        # Один стабильный номер на пользователя: #1, #2, #3...
        task_lines.append(f"{status_icon} #{user_seq} — {title}")

    return summary + "\n\n" + "\n".join(task_lines)


def build_dashboard_keyboard(tasks: list[dict], filter_type: str = "all") -> InlineKeyboardMarkup:
    """Собирает клавиатуру для дашборда задач."""
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="• Все •" if filter_type == "all" else "Все",
            callback_data="taskdash_filter:all"
        ),
        InlineKeyboardButton(
            text="• ⏳ В работе •" if filter_type == "pending" else "⏳ В работе",
            callback_data="taskdash_filter:pending"
        ),
        InlineKeyboardButton(
            text="• ✅ Готово •" if filter_type == "completed" else "✅ Готово",
            callback_data="taskdash_filter:completed"
        )
    )

    filtered = tasks
    if filter_type == "pending":
        filtered = [t for t in tasks if t.get('status') == 'pending']
    elif filter_type == "completed":
        filtered = [t for t in tasks if t.get('status') == 'completed']

    for task in filtered:
        is_done = task.get('status') == 'completed'
        btn_icon = "↩️ Вернуть" if is_done else "✅ Сделать"
        user_seq = task.get('user_seq', '?')
        builder.row(
            InlineKeyboardButton(
                text=f"{btn_icon} #{user_seq}",
                # Новый префикс taskdash_t: использует user_seq вместо id
                callback_data=f"taskdash_t:{user_seq}:{filter_type}"
            )
        )

    builder.row(
        InlineKeyboardButton(text="➕ Новая задача", callback_data="taskdash_add"),
        InlineKeyboardButton(text="🔔 Напоминания", callback_data="taskdash_reminders"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"taskdash_refresh:{filter_type}")
    )

    builder.row(
        InlineKeyboardButton(text="🧹 Очистить готовые", callback_data="taskdash_clear"),
    )

    return builder.as_markup()


# ----- Текст с текущим уровнем доступа -----
def _eva_access_line(user: dict) -> str:
    if user["role"] == "owner":
        return "🤖 Доступ к полным функциям (Ева): ✅ есть (владелец)"
    if user["eva_access"] == "granted":
        return "🤖 Доступ к полным функциям (Ева): ✅ есть"
    if user["eva_access"] == "pending":
        return "🤖 Доступ к полным функциям (Ева): ⏳ заявка на рассмотрении"
    return "🤖 Доступ к полным функциям (Ева): ❌ нет — запросить: /eva_request"


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
    await sync_commands_for_user(message.bot, user_id, user)

    welcome_text = (
        f"🧠 Привет, {username}!\n\n"
        "Я — **Ева**, твой ИИ-ассистент. Я запоминаю всё, что ты мне пишешь,\n"
        "и могу найти это по смыслу даже через месяцы.\n\n"
        "📌 **Просто напиши мне что угодно** — я сохраню это в память.\n"
        "🔍 **Спроси меня о чём‑то** — я найду ответ среди твоих записей.\n"
        "📊 **Дашборд задач** — отправь /dashboard для управления задачами.\n"
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


# ----- Хэндлеры Дашборда Задач (To-Do) -----
# ----- Фразы Eva для действий с дашбордом -----
_DASHBOARD_OPEN_PHRASES = [
    "Загружаю твой список дел. Посмотрим, что у нас на сегодня!",
    "Дашборд задач на экране. Давай сверимся с планами.",
    "Сектора проверены, список обновлён. Вот твои актуальные задачи:",
    "Показываю текущий прогресс. Что планируем делать дальше?",
]

_DASHBOARD_REFRESH_PHRASES = _DASHBOARD_OPEN_PHRASES

_DASHBOARD_FILTER_PHRASES = {
    "pending": "Оставила только активные задачи. Сосредоточимся на них.",
    "completed": "Отличная работа! Вот список всего, что ты уже закрыл.",
    "all": "Показываю полный список задач — и готовые, и в процессе.",
}

_DASHBOARD_TASK_TOGGLE_PHRASES = {
    "complete": [
        "Готово! Отметила задачу выполненной. Так держать!",
        "Минус одна задача! Вычеркиваем.",
        "Зафиксировано! На шаг ближе к закрытию всех дел.",
    ],
    "uncomplete": [
        "Возвращаю задачу в список активных. Не страшно, доделаем!",
        "Поняла, статус сброшен обратно на \"В работе\".",
    ],
}


@router.message(Command("dashboard"))
@router.message(F.text == "📊 Дашборд задач")
async def show_dashboard_cmd(message: Message):
    tasks = await get_user_tasks(user_id=message.from_user.id)
    text = render_dashboard_text(tasks, "all")
    kb = build_dashboard_keyboard(tasks, "all")
    await message.answer(text=text, reply_markup=kb, parse_mode="HTML")
    # Отправляем фразу Евы отдельным сообщением
    await message.answer(random.choice(_DASHBOARD_OPEN_PHRASES))


@router.callback_query(F.data.startswith("taskdash_filter:"))
async def handle_dashboard_filter(callback: CallbackQuery):
    filter_type = callback.data.split(":")[1]
    tasks = await get_user_tasks(user_id=callback.from_user.id)

    text = render_dashboard_text(tasks, filter_type)
    kb = build_dashboard_keyboard(tasks, filter_type)

    await callback.message.edit_text(text=text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()
    # Отправляем фразу Евы отдельным сообщением
    phrase = _DASHBOARD_FILTER_PHRASES.get(filter_type, "Фильтр применён.")
    await callback.message.answer(phrase)


@router.callback_query(F.data.startswith("taskdash_t:"))
async def handle_task_toggle(callback: CallbackQuery):
    """
    Переключает статус задачи по user_seq.
    Новый формат: taskdash_t:<user_seq>:<filter_type>
    """
    parts = callback.data.split(":")
    user_seq = int(parts[1])
    filter_type = parts[2]
    user_id = callback.from_user.id

    new_status = await toggle_task_status(user_id=user_id, user_seq=user_seq)
    if new_status is None:
        await callback.answer("Задача не найдена!", show_alert=True)
        return

    tasks = await get_user_tasks(user_id=user_id)
    text = render_dashboard_text(tasks, filter_type)
    kb = build_dashboard_keyboard(tasks, filter_type)

    await callback.message.edit_text(text=text, reply_markup=kb, parse_mode="HTML")
    # Отправляем фразу Евы
    if new_status == "completed":
        phrase = random.choice(_DASHBOARD_TASK_TOGGLE_PHRASES["complete"])
    else:
        phrase = random.choice(_DASHBOARD_TASK_TOGGLE_PHRASES["uncomplete"])
    await callback.message.answer(phrase)


@router.callback_query(F.data.startswith("taskdash_toggle:"))
async def handle_task_toggle_legacy(callback: CallbackQuery):
    """
    Заглушка для старого формата callback_data (taskdash_toggle:<id>:<filter>).
    В чате остались старые сообщения с глобальным id в callback_data.
    Данные не меняем — только предупреждаем пользователя.
    """
    await callback.answer("Дашборд устарел, открой заново: /dashboard", show_alert=True)


@router.callback_query(F.data == "taskdash_clear")
async def handle_clear_completed(callback: CallbackQuery):
    deleted_count = await clear_completed_tasks(user_id=callback.from_user.id)

    tasks = await get_user_tasks(user_id=callback.from_user.id)
    text = render_dashboard_text(tasks, "all")
    kb = build_dashboard_keyboard(tasks, "all")

    await callback.message.edit_text(text=text, reply_markup=kb, parse_mode="HTML")
    await callback.answer(f"Удалено задач: {deleted_count}", show_alert=True)


@router.callback_query(F.data.startswith("taskdash_refresh:"))
async def handle_refresh(callback: CallbackQuery):
    filter_type = callback.data.split(":")[1]
    tasks = await get_user_tasks(user_id=callback.from_user.id)

    text = render_dashboard_text(tasks, filter_type)
    kb = build_dashboard_keyboard(tasks, filter_type)

    await callback.message.edit_text(text=text, reply_markup=kb, parse_mode="HTML")
    await callback.answer("Данные обновлены")
    # Отправляем фразу Евы отдельным сообщением
    await callback.message.answer(random.choice(_DASHBOARD_REFRESH_PHRASES))


@router.callback_query(F.data == "taskdash_reminders")
async def handle_task_reminders(callback: CallbackQuery):
    """Настройка напоминаний о задачах."""
    await callback.message.answer(
        "🔔 <b>Настройка напоминаний о задачах</b>\n\n"
        "Напиши, как часто напоминать о задачах. Например:\n"
        "• <code>каждый день в 9:00</code>\n"
        "• <code>каждый час</code>\n"
        "• <code>каждую неделю в понедельник в 10:00</code>\n\n"
        "Или напиши свой вариант расписания.",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "taskdash_add")
async def start_task_creation(callback: CallbackQuery, state: FSMContext):
    await state.set_state(TaskCreationStates.waiting_for_task_text)
    await callback.message.answer(
        "📝 <b>Введите новую задачу</b> (или несколько задач, каждую с новой строки):",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(TaskCreationStates.waiting_for_task_text)
async def handle_task_input(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    user_id = message.from_user.id

    if not text:
        await message.answer("Текст задачи не может быть пустым.")
        return

    if "\n" in text:
        task_titles = [t.strip() for t in text.split("\n") if t.strip()]
        if task_titles:
            await add_tasks_batch(user_id=user_id, tasks_titles=task_titles, interval_minutes=0)
    else:
        await add_single_task(user_id=user_id, title=text)

    await state.clear()

    tasks = await get_user_tasks(user_id=user_id)
    text_dashboard = render_dashboard_text(tasks, "all")
    kb_dashboard = build_dashboard_keyboard(tasks, "all")

    await message.answer(text=text_dashboard, reply_markup=kb_dashboard, parse_mode="HTML")


# ----- Обработка одобрений и остальной функционал -----
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
        updated_user = await get_user(target_user_id)
        await sync_commands_for_user(callback.bot, target_user_id, updated_user)
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


@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "🤖 **Что я умею:**\n\n"
        "📝 /start — Показать приветствие\n"
        "❓ /help — Показать справку\n"
        "📊 /status — Проверить уровень доступа (бот / Ева)\n"
        "📋 /dashboard — Дашборд задач (To-Do list)\n"
        "📋 /reminders — Список активных напоминаний\n"
        "🌍 /timezone — Настроить часовой пояс по геолокации\n"
        "🗂 /memory — Последние записи в памяти\n"
        "📰 /digest — Дайджест памяти за сутки\n\n"
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
            "📸 /screenshot — Сделать скриншот экрана\n"
            "🔍 /find <что> — Найти файл или папку на компьютере\n"
            "📂 /open <путь> — Открыть файл или папку\n"
            "🎛 /eva_dashboard — Панель управления Евой (кнопки)\n"
            "📡 /agent_status — Подключён ли desktop-агент\n"
            "👥 /eva_users — Кто имеет доступ к Еве\n"
            "🚫 /eva_revoke <id> — Отозвать доступ к Еве"
        )
    elif await can_use_eva(message.from_user.id):
        help_text += (
            "\n\n🤖 **Ева (доступ выдан):**\n"
            "🎛 /eva <команда> — Отдать команду desktop-агенту\n"
            "📸 /screenshot — Сделать скриншот экрана\n"
            "🔍 /find <что> — Найти файл или папку на компьютере\n"
            "📂 /open <путь> — Открыть файл или папку\n"
            "🎛 /eva_dashboard — Панель управления Евой (кнопки)\n"
            "📡 /agent_status — Подключён ли desktop-агент"
        )
    else:
        help_text += (
            "\n\n🤖 **Ева** — голосовой ИИ-помощник с доступом к ПК.\n"
            "🙋 /eva_request — Запросить доступ у владельца"
        )
    await message.answer(help_text)


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


@router.message(Command("digest"))
async def cmd_digest(message: Message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    tz_name = ((user["settings"] if user else {}) or {}).get("timezone") or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    entries = await get_recent_memories(user_id, hours=24)
    if not entries:
        await message.answer("📭 За последние сутки у тебя не было записей в памяти — дайджест составлять не из чего.")
        return

    digest_entries = [
        {
            "text": e["text"],
            "created_at_local": e["created_at"].astimezone(tz).strftime("%d.%m.%Y %H:%M"),
        }
        for e in entries
    ]
    today_str = datetime.now(timezone.utc).astimezone(tz).strftime("%d.%m.%Y")

    await message.answer("🧠 Собираю дайджест за сутки...")
    summary = await summarize_for_digest(digest_entries, today_str)
    if not summary:
        await message.answer("😵 Не смог составить дайджест — попробуй ещё раз чуть позже.")
        return
    await message.answer(f"📰 Дайджест за сутки:\n\n{summary}")


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


@router.message(Command("eva_request"))
async def cmd_eva_request(message: Message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    if user is None:
        return
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
        updated_user = await get_user(target_id)
        await sync_commands_for_user(callback.bot, target_id, updated_user)
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
    updated_user = await get_user(target_id)
    await sync_commands_for_user(message.bot, target_id, updated_user)
    await message.answer(f"🚫 Доступ к Еве отозван у пользователя {target_id}.")
    try:
        await message.bot.send_message(target_id, "🚫 Твой доступ к Еве отозван владельцем.")
    except Exception:
        pass


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


@router.message(Command("screenshot"))
async def cmd_screenshot(message: Message):
    if not await can_use_eva(message.from_user.id):
        await message.answer("⛔ У тебя нет доступа к Еве. Запросить: /eva_request")
        return
    await process_eva_command(message.bot, message.from_user.id, "сделай скриншот", message.answer)


@router.message(Command("find"))
async def cmd_find(message: Message):
    if not await can_use_eva(message.from_user.id):
        await message.answer("⛔ У тебя нет доступа к Еве. Запросить: /eva_request")
        return
    query = message.text.partition(" ")[2].strip()
    if not query:
        await message.answer("Укажи что искать: /find отчёт")
        return
    await process_eva_command(message.bot, message.from_user.id, f"найди файл {query}", message.answer)


@router.message(Command("open"))
async def cmd_open(message: Message):
    if not await can_use_eva(message.from_user.id):
        await message.answer("⛔ У тебя нет доступа к Еве. Запросить: /eva_request")
        return
    path = message.text.partition(" ")[2].strip()
    if not path:
        await message.answer("Укажи путь: /open файл report.pdf или /open папку загрузки")
        return
    if path.lower().startswith(("файл", "папк")):
        await process_eva_command(message.bot, message.from_user.id, f"открой {path}", message.answer)
    else:
        await process_eva_command(message.bot, message.from_user.id, f"открой файл {path}", message.answer)


@router.message(Command("eva"))
async def cmd_eva(message: Message):
    if not await can_use_eva(message.from_user.id):
        await message.answer("⛔ У тебя нет доступа к Еве. Запросить: /eva_request")
        return

    command_text = message.text.partition(" ")[2].strip()
    if not command_text:
        await message.answer(
            "Использование: /eva <команда>\n"
            "Например:\n"
            "• /eva открой блокнот\n"
            "• /eva открой сайт youtube.com\n"
            "• /eva сделай скриншот\n"
            "• /eva покажи файлы в загрузках"
        )
        return

    await process_eva_command(message.bot, message.from_user.id, command_text, message.answer)


@router.callback_query(lambda c: c.data and (c.data.startswith("eva_confirm:") or c.data.startswith("eva_cancel:")))
async def handle_eva_command_confirmation(callback: CallbackQuery):
    action, command_id_str = callback.data.split(":")
    command_id = int(command_id_str)

    command = await get_eva_command(command_id)
    if not command:
        await callback.answer("Команда не найдена.", show_alert=True)
        return

    if callback.from_user.id != command["requested_by"] and not await is_owner(callback.from_user.id):
        await callback.answer("Подтвердить может только автор команды или владелец.", show_alert=True)
        return

    if command["status"] != "awaiting_confirmation":
        await callback.answer("Эта команда уже обработана.", show_alert=True)
        return

    if action == "eva_confirm":
        await update_eva_command_status(command_id, "pending")
        sent = await push_command_to_agents(command_id, command["action_json"])
        suffix = "\n\n📤 Отправлено Еве." if sent else "\n\n📥 В очереди — агент сейчас не на связи."
        await callback.message.edit_text(callback.message.text + suffix)
    else:
        await update_eva_command_status(command_id, "cancelled")
        await callback.message.edit_text(callback.message.text + "\n\n❌ Отменено.")
    await callback.answer()


# ----- Dashboard: панель быстрых действий Евы на ПК -----
def _eva_dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📸 Скриншот", callback_data="evadash:screenshot"),
            InlineKeyboardButton(text="🔊 Громче", callback_data="evadash:vol_up"),
            InlineKeyboardButton(text="🔉 Тише", callback_data="evadash:vol_down"),
        ],
        [
            InlineKeyboardButton(text="🔇 Звук", callback_data="evadash:vol_mute"),
            InlineKeyboardButton(text="📁 Файлы", callback_data="evadash:list_files"),
            InlineKeyboardButton(text="🔍 Найти файл", callback_data="evadash:input:search_file"),
        ],
        [
            InlineKeyboardButton(text="📄 Открыть файл", callback_data="evadash:input:open_file"),
            InlineKeyboardButton(text="🌐 Сайт", callback_data="evadash:input:open_website"),
            InlineKeyboardButton(text="🔎 Поиск", callback_data="evadash:input:web_search"),
        ],
        [
            InlineKeyboardButton(text="⌨️ Напечатать", callback_data="evadash:input:type_text"),
            InlineKeyboardButton(text="🔒 Блокировка", callback_data="evadash:power_lock"),
            InlineKeyboardButton(text="🔌 Выключить", callback_data="evadash:power_shutdown"),
        ],
    ])


@router.message(Command("eva_dashboard"))
async def cmd_eva_dashboard(message: Message):
    if not await can_use_eva(message.from_user.id):
        await message.answer("⛔ У тебя нет доступа к Еве. Запросить: /eva_request")
        return
    await message.answer("📋 Панель управления Евой (Desktop):", reply_markup=_eva_dashboard_keyboard())


@router.callback_query(lambda c: c.data and c.data.startswith("evadash:"))
async def handle_eva_dashboard(callback: CallbackQuery):
    if not await can_use_eva(callback.from_user.id):
        await callback.answer("⛔ У тебя нет доступа к Еве.", show_alert=True)
        return

    action = callback.data[len("evadash:"):]

    if action.startswith("input:"):
        input_type = action[len("input:"):]
        prompt, _ = _DASHBOARD_INPUT[input_type]
        _pending_dashboard_input[callback.from_user.id] = input_type
        await callback.message.answer(prompt)
        await callback.answer()
        return

    command_text = _DASHBOARD_DIRECT.get(action)
    if not command_text:
        await callback.answer("Неизвестная кнопка.", show_alert=True)
        return

    await callback.answer()
    await process_eva_command(
        callback.bot, callback.from_user.id, command_text, callback.message.answer
    )


# ----- Обработка входящего текста -----
@router.message(lambda msg: msg.text is not None)
async def handle_text(message: Message, state: FSMContext):
    user_id = message.from_user.id

    # Проверяем, находится ли пользователь в состоянии добавления задачи FSM
    current_state = await state.get_state()
    if current_state == TaskCreationStates.waiting_for_task_text.state:
        await handle_task_input(message, state)
        return

    # Проверяем ожидание ввода для панели Евы
    pending_input = _pending_dashboard_input.pop(user_id, None)
    if pending_input:
        if message.text.startswith('/'):
            await message.answer("❌ Действие отменено — ожидание ввода сброшено.")
            return
        _, template = _DASHBOARD_INPUT[pending_input]
        command_text = template.format(input=message.text.strip())
        await process_eva_command(message.bot, user_id, command_text, message.answer)
        return

    if message.text.startswith('/'):
        await message.answer("❓ Неизвестная команда. Напиши /help.")
        return

    user_text = message.text

    lower_text = user_text.lower()
    if lower_text.startswith("напомни") or lower_text.startswith("напоминай"):
        await handle_reminder_command(message)
        return

    from eva_engine import is_agent_online, relay_to_agent
    if is_agent_online(user_id):
        await relay_to_agent(message.bot, user_id, user_text, message.answer, "telegram")
        return

    await add_memory(user_id, user_text)

    await message.answer("🧠 Думаю...")
    answer = await get_ai_response(user_id, user_text)
    await message.answer(answer)


# ----- Обработка напоминаний -----
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


# ----- Обработка файлов (PDF, Голосовые, Фото) -----
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


@router.message(lambda msg: msg.photo is not None)
async def handle_photo(message: Message):
    user_id = message.from_user.id
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