"""
Меню команд Telegram (кнопка со списком команд рядом со строкой ввода).

Список команд у каждого пользователя разный — зависит от его уровня
доступа (обычный / владелец Евы / админ бота). Поэтому вместо одного
глобального set_my_commands используется отдельная область (scope) на
каждый чат: Telegram позволяет задать команды персонально для конкретного
chat_id, и они перекрывают команды по умолчанию именно в этом чате.
"""
from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from config import ADMIN_ID

# Видно всем, включая ещё не одобренных пользователей — безопасный минимум,
# ничего не раскрывает и не требует прав.
DEFAULT_COMMANDS = [
    BotCommand(command="start", description="Начать / показать приветствие"),
    BotCommand(command="help", description="Список команд"),
]

# Добавляется одобренным пользователям бота
BASE_COMMANDS = DEFAULT_COMMANDS + [
    BotCommand(command="status", description="Мой уровень доступа"),
    BotCommand(command="dashboard", description="Дашборд задач To-Do"),
    BotCommand(command="memory", description="Последние записи в памяти"),
    BotCommand(command="digest", description="Дайджест памяти за сутки"),
    BotCommand(command="reminders", description="Список напоминаний"),
    BotCommand(command="timezone", description="Настроить часовой пояс"),
    BotCommand(command="eva_request", description="Запросить доступ к Еве"),
]

# Добавляется тем, у кого есть доступ к Еве (owner или eva_access=granted)
EVA_COMMANDS = [
    BotCommand(command="eva", description="Отдать команду Еве (файлы, сайты, ПК)"),
    BotCommand(command="screenshot", description="Сделать скриншот экрана"),
    BotCommand(command="find", description="Найти файл или папку на компьютере"),
    BotCommand(command="open", description="Открыть файл или папку"),
    BotCommand(command="eva_dashboard", description="Панель управления Евой (кнопки)"),
    BotCommand(command="agent_status", description="Подключён ли агент Евы"),
]

# Добавляется только владельцам (role=owner) — управление доступом к Еве
EVA_OWNER_COMMANDS = [
    BotCommand(command="eva_users", description="Кто имеет доступ к Еве"),
    BotCommand(command="eva_revoke", description="Отозвать доступ к Еве"),
]

# Добавляется только ADMIN_ID — управление доступом к самому боту
ADMIN_COMMANDS = [
    BotCommand(command="users", description="Список пользователей бота"),
    BotCommand(command="revoke", description="Отозвать доступ к боту"),
    BotCommand(command="stats", description="Статистика бота"),
]


async def sync_commands_for_user(bot: Bot, user_id: int, user: dict | None) -> None:
    """Выставляет персональное меню команд для конкретного чата,
    исходя из текущего статуса/роли пользователя в БД."""
    if user is None or user.get("status") != "approved":
        # Не одобрен — сбрасываем на дефолтную область, ничего лишнего не показываем
        try:
            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=user_id))
        except Exception:
            pass
        return

    commands = list(BASE_COMMANDS)
    role = user.get("role")
    eva_access = user.get("eva_access")

    if role == "owner" or eva_access == "granted":
        commands += EVA_COMMANDS
    if role == "owner":
        commands += EVA_OWNER_COMMANDS
    if user_id == ADMIN_ID:
        commands += ADMIN_COMMANDS

    await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=user_id))


async def set_default_commands(bot: Bot) -> None:
    """Минимальное меню для всех остальных (неодобренных/незнакомых чатов)."""
    await bot.set_my_commands(DEFAULT_COMMANDS, scope=BotCommandScopeDefault())