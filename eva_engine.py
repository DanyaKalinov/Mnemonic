"""
Общая логика обработки команд Евы.

Раньше вся эта логика жила внутри cmd_eva в handlers.py и была доступна
только из Telegram. Теперь она вынесена сюда, чтобы теми же самыми
правилами (разбор шагов, AI-фолбэк, обязательное подтверждение
разрушительных действий через Telegram) пользовались ОБА источника
команд — текст в Telegram и голос с desktop-агента.

Это и есть граница безопасности: даже если команда пришла голосом (а
голос физически может услышать кто угодно рядом с ПК, не только
владелец), разрушительные действия всё равно требуют явного тапа по
кнопке в аутентифицированном Telegram-аккаунте владельца — посторонний
голос не может, например, удалить файл или выключить ПК без этого шага.
"""
import json
import re
from typing import Awaitable, Callable, Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from ai_core import interpret_eva_command, ask_web, CHAT_HISTORY_LIMIT, CHAT_MESSAGE_MAX_CHARS
from db import add_eva_command, add_chat_message, get_recent_chat_messages
from eva_agent_registry import push_command_to_agents, get_user_agent, send_to_user_agent, speak_to_owner
from eva_command_parser import parse_eva_command, split_into_subcommands
import chat_state
from chat_state import new_message_id, register_message
from memory import search_memory

ReplyFunc = Callable[[str], Awaitable[None]]

# Каналы, в которых "говорим" с агентом.
_ORIGIN_TELEGRAM = "telegram"
_ORIGIN_VOICE = "voice"
_ORIGIN_APP = "app"

TOP_MEMORY_K = 5


def is_agent_online(user_id: int) -> bool:
    """Есть ли у этого пользователя подключённый desktop-агент."""
    return get_user_agent(user_id) is not None


async def _load_history_for_agent(user_id: int) -> list[dict]:
    """История диалога для локального мозга: последние реплики из базы сервера."""
    try:
        rows = await get_recent_chat_messages(user_id, limit=CHAT_HISTORY_LIMIT)
    except Exception as e:
        print(f"⚠️ Не удалось загрузить историю для агента: {e}", flush=True)
        return []
    return [{"role": r["role"], "text": r["text"][:CHAT_MESSAGE_MAX_CHARS]} for r in rows]


async def _save_chat_message(user_id: int, role: str, text: str) -> None:
    """Сохраняет реплику в историю. Сбой записи не должен ломать ответ пользователю."""
    if not text or not text.strip():
        return
    try:
        await add_chat_message(user_id, role, text)
    except Exception as e:
        print(f"⚠️ Не удалось сохранить реплику в историю: {e}", flush=True)


async def relay_to_agent(bot: Bot, user_id: int, text: str, reply: ReplyFunc, source: str) -> None:
    """Онлайн-путь: сырой текст + память + история диалога уходят агенту как 'chat'.
    Мозг (локальная модель) отвечает сам; это не разбор на сервере."""
    origin = _ORIGIN_VOICE if source == "voice" else _ORIGIN_TELEGRAM
    message_id = new_message_id()
    register_message(message_id, user_id, origin, text)

    memories = await search_memory(user_id, text, k=TOP_MEMORY_K)
    # История берётся ДО сохранения текущей реплики, иначе вопрос попадёт в неё дважды
    history = await _load_history_for_agent(user_id)
    payload = {
        "type": "chat",
        "message_id": message_id,
        "text": text,
        "origin": origin,
        "memory": memories,
        "history": history,
    }
    # Вопрос сохраняем до отправки, чтобы порядок реплик в истории был верным
    await _save_chat_message(user_id, "user", text)
    sent = await send_to_user_agent(user_id, payload)
    if not sent:
        await reply("📥 Агент сейчас не на связи — попробуй позже.")
        return
    if origin == _ORIGIN_TELEGRAM:
        await reply("🧠 Отправила Еве, жду ответ...")

# Местоимения-указатели: если шаг целиком состоит из "открой её/его/это"
# после шага search_file — заменяем на open_folder с тем же запросом.
_PRONOUN_RE = re.compile(
    r'^(?:открой(?:те)?|запусти|покажи)\s+(?:её|ее|его|это|ею|нее|неё|них|их)\s*$',
    re.IGNORECASE,
)


async def _resolve_step(step_text: str):
    """Разбирает ОДИН шаг команды: сначала жёсткий шаблон, если не подошёл —
    свободный разбор через Gemini. Возвращает (action, params,
    requires_confirmation, error). Если error не None — action будет None."""
    parsed = parse_eva_command(step_text)
    if parsed.ok:
        return parsed.action, parsed.params, parsed.requires_confirmation, None

    ai_result = await interpret_eva_command(step_text)
    if not ai_result.get("action"):
        return None, {}, False, (ai_result.get("error") or parsed.error)
    return ai_result["action"], ai_result["params"], ai_result.get("requires_confirmation", False), None


def describe_eva_action(action: str, params: dict) -> str:
    if action == "create_file":
        return f"Ева создаст файл «{params.get('path')}»."
    if action == "delete_file":
        return f"Ева удалит файл «{params.get('path')}»."
    if action == "edit_file":
        verb = "допишет в" if params.get("mode") == "append" else "перезапишет"
        return f"Ева {verb} файл «{params.get('path')}»."
    if action == "copy_file":
        return f"Ева скопирует «{params.get('src')}» в «{params.get('dst')}»."
    if action == "move_file":
        return f"Ева перенесёт «{params.get('src')}» в «{params.get('dst')}» (исходный файл исчезнет оттуда, где был)."
    if action == "power_action":
        labels = {"shutdown": "выключит компьютер", "restart": "перезагрузит компьютер", "lock": "заблокирует компьютер"}
        return f"Ева {labels.get(params.get('action'), 'выполнит действие с питанием ПК')}."
    if action == "close_app":
        return f"Ева закроет окно «{params.get('window')}» (несохранённые данные внутри могут быть потеряны)."
    if action == "open_file":
        return f"Ева откроет файл «{params.get('path', '?')}»."
    if action == "open_folder":
        return f"Ева откроет папку «{params.get('name', '?')}»."
    if action == "mouse_move":
        return f"Ева переместит курсор в ({params.get('x', '?')}, {params.get('y', '?')})."
    if action == "mouse_click":
        label = {"left": "левой", "right": "правой", "middle": "средней"}.get(params.get("button"), params.get("button", "?"))
        return f"Ева кликнет {label} кнопкой мыши в ({params.get('x', '?')}, {params.get('y', '?')})."
    if action == "mouse_scroll":
        return f"Ева прокрутит {params.get('direction', '?')} на {params.get('amount', '?')}."
    if action == "mouse_drag":
        return (f"Ева перетащит элемент мышью из ({params.get('x_start', '?')}, {params.get('y_start', '?')}) "
                f"в ({params.get('x_end', '?')}, {params.get('y_end', '?')}).")
    if action == "set_default_browser":
        return f"Ева будет открывать сайты и поиск в браузере «{params.get('browser', '?')}»."
    return "Это действие изменит данные или состояние ПК."


async def process_eva_command(
    bot: Bot,
    user_id: int,
    command_text: str,
    reply: ReplyFunc,
    source: str = "telegram",
) -> None:
    """
    bot — экземпляр aiogram-бота (нужен для гарантированной отправки
        запроса подтверждения владельцу в Telegram, независимо от source).
    user_id — кому принадлежит команда (для истории и подтверждения).
    command_text — сырой текст команды (уже без "/eva " и т.п.).
    reply — функция немедленного текстового ответа туда, откуда пришла
        команда (message.answer для Telegram, либо обёртка над
        bot.send_message для голоса).
    source — "telegram" или "voice". Для голоса неразрушительные действия
        уходят агенту с пометкой voice=True — агент дополнительно озвучит
        результат вслух через TTS, а не просто выполнит молча.
    """
    # Онлайн-ретрансляция: локальный мозг думает, сервер только передаёт.
    if is_agent_online(user_id):
        await relay_to_agent(bot, user_id, command_text, reply, source)
        return

    step_texts = split_into_subcommands(command_text)
    steps = []
    descriptions = []
    any_confirmation_needed = False

    prev_action = None
    prev_query = None
    for i, step_text in enumerate(step_texts, start=1):
        # "найди папку X и открой её" — вторая часть ссылается на результат
        # первой (search_file). Превращаем её в open_folder с тем же запросом.
        if prev_action == "search_file" and prev_query and _PRONOUN_RE.match(step_text.strip()):
            action, params, requires_confirmation, error = "open_folder", {"name": prev_query}, False, None
        else:
            action, params, requires_confirmation, error = await _resolve_step(step_text)
        if error:
            prefix = f"Шаг {i} («{step_text}»): " if len(step_texts) > 1 else ""
            await reply(f"{prefix}{error}")
            return

        if action == "ask_web":
            # Не команда агенту, а прямой вопрос к Gemini с доступом в
            # интернет — отвечаем сразу текстом, без похода на ПК.
            answer = await ask_web(params.get("query", step_text))
            await reply(f"🌐 {answer}")
            continue

        steps.append({"action": action, "params": params})
        descriptions.append(describe_eva_action(action, params))
        any_confirmation_needed = any_confirmation_needed or requires_confirmation
        prev_action = action
        if action == "search_file":
            prev_query = params.get("query")

    if not steps:
        # Не действие, а обычный вопрос — при офлайн агенте отвечает Gemini.
        from ai_core import get_ai_response
        answer = await get_ai_response(user_id, command_text)
        await reply(answer)
        return

    payload: dict = {"steps": steps} if len(steps) > 1 else dict(steps[0])
    if source == "voice":
        payload["voice"] = True
    action_json = json.dumps(payload)

    if any_confirmation_needed:
        command_id = await add_eva_command(
            user_id, command_text, action_json=action_json, status="awaiting_confirmation",
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"eva_confirm:{command_id}"),
            InlineKeyboardButton(text="❌ Отменить", callback_data=f"eva_cancel:{command_id}"),
        ]])
        if len(descriptions) == 1:
            body = f"⚠️ {descriptions[0]}"
        else:
            numbered = "\n".join(f"{i}. {d}" for i, d in enumerate(descriptions, start=1))
            body = f"⚠️ Последовательность из {len(descriptions)} шагов:\n{numbered}"
        if source == "voice":
            body += "\n\n🎙 Команда пришла голосом — для разрушительных действий подтверждение в Telegram нужно всегда, даже с голоса."

        # ВАЖНО: подтверждение всегда уходит в Telegram владельцу через
        # bot.send_message, независимо от source — это и есть граница
        # безопасности, о которой сказано в докстринге модуля.
        await bot.send_message(user_id, f"{body}\nПодтверди выполнение:", reply_markup=keyboard)
        if source == "voice":
            await reply("Нужно подтверждение в Telegram, прежде чем я это сделаю.")
        return

    command_id = await add_eva_command(user_id, command_text, action_json=action_json)
    sent = await push_command_to_agents(command_id, action_json)

    if source != "voice":
        if sent:
            await reply(f"📤 Команда «{command_text}» отправлена Еве, жду результат...")
        else:
            await reply(
                f"📥 Команда «{command_text}» сохранена в очередь — "
                "desktop-агент сейчас не на связи, выполнится при подключении."
            )
    # Для голоса ничего дополнительно не отвечаем здесь — результат придёт
    # позже через WS "result", агент сам озвучит его локально (ws_client.py),
    # и он всё равно уйдёт в Telegram как обычно (eva_ws._handle_agent_message).


# ---------------------------------------------------------------------------
# Обратная доставка от агента — chat_result / confirm_request / cloud_action
# ---------------------------------------------------------------------------

async def _deliver_text(bot: Bot, user_id: int, text: str, image_base64: Optional[str] = None) -> None:
    """Отправляет итог чата пользователю в Telegram (единая точка для
    chat_result) — для голоса дополнительно озвучиваем через агента."""
    from aiogram.types import BufferedInputFile
    try:
        if image_base64:
            import base64
            photo = BufferedInputFile(base64.b64decode(image_base64), filename="screenshot.png")
            await bot.send_photo(user_id, photo=photo, caption=text or "Ева")
        else:
            await bot.send_message(user_id, text or "Ева")
    except Exception as e:
        print(f"⚠️ Не удалось доставить chat_result пользователю {user_id}: {e}", flush=True)


async def handle_chat_result(bot: Bot, data: dict) -> None:
    message_id = data.get("message_id")
    meta = chat_state.get_message(message_id) if message_id else None
    if meta is None:
        return
    kind = data.get("kind", "answer")
    text = str(data.get("text") or "")
    # Ответ Евы сохраняем в историю (ошибки — нет, это шум для модели)
    if kind != "error":
        await _save_chat_message(meta["user_id"], "assistant", text)
    if kind == "error":
        text = f"⚠️ {text}"
    await _deliver_text(bot, meta["user_id"], text, data.get("image_base64"))
    # голос: если пришло голосом — озвучиваем на ПК
    if meta.get("origin") == "voice":
        await speak_to_owner(meta["user_id"], text)


async def handle_confirm_request(bot: Bot, agent_id: str, data: dict) -> None:
    """Разрушительное действие от локального мозга — всегда подтверждение
    в Telegram владельца (граница безопасности не зависит от канала)."""
    from db import add_eva_command
    from eva_agent_registry import get_owner_id_for_agent
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    owner_id = get_owner_id_for_agent(agent_id)
    if owner_id is None:
        return

    steps = data.get("local_steps") or []
    if not steps:
        return
    payload = {"steps": steps}
    meta = None
    message_id = data.get("message_id")
    if message_id:
        meta = chat_state.get_message(message_id)
        if data.get("voice"):  # голос: результаты озвучивать
            payload["voice"] = True

    command_id = await add_eva_command(
        owner_id,
        meta["command_text"] if meta else "действие с ПК",
        action_json=json.dumps(payload),
        status="awaiting_confirmation",
    )
    numbered = "\n".join(f"{i}. {s.get('description','')}" for i, s in enumerate(steps, start=1))
    body = f"⚠️ Локальный мозг предлагает:\n{numbered}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"eva_confirm:{command_id}"),
        InlineKeyboardButton(text="❌ Отменить", callback_data=f"eva_cancel:{command_id}"),
    ]])
    await bot.send_message(owner_id, f"{body}\nПодтверди выполнение:", reply_markup=keyboard)


async def handle_cloud_action(ws, agent_id: str, data: dict) -> None:
    """Облачное действие локального мозга. Исполняется на сервере,
    результат возвращается агенту как cloud_result (синтез ответа — агент)."""
    request_id = data.get("request_id")
    action = data.get("action")
    params = data.get("params") or {}
    if not request_id or not action:
        return

    from eva_agent_registry import get_owner_id_for_agent
    user_id = get_owner_id_for_agent(agent_id)
    if user_id is None:
        user_id = 0  # fallback

    from cloud_actions import execute_cloud_action
    result = await execute_cloud_action(action, params, user_id)

    payload = {"type": "cloud_result", "request_id": request_id, **result}
    try:
        await ws.send_str(json.dumps(payload))
    except Exception as e:
        print(f"⚠️ Не удалось вернуть cloud_result: {e}", flush=True)
