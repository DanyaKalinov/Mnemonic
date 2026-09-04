import asyncio
import json
import os
from dotenv import load_dotenv
from google import genai
from google.genai import types
from groq import Groq
from memory import search_memory

load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
client = genai.Client(api_key=GOOGLE_API_KEY)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

async def get_ai_response(user_id: int, user_question: str) -> str:
    if not GOOGLE_API_KEY:
        return "⚠️ Ошибка: не настроен API-ключ Google Gemini."

    remembered_facts = await search_memory(user_id, user_question, k=3)

    if remembered_facts:
        memory_context = "\n".join([f"- {fact}" for fact in remembered_facts])
        system_prompt = (
            "Ты — 'Второй Мозг', персональный ИИ-помощник. Вот факты о пользователе, которые ты уже знаешь:\n"
            f"{memory_context}\n\n"
            "Используй эти факты, чтобы дать персонализированный ответ. "
            "Если факты не относятся к вопросу — просто проигнорируй их."
        )
    else:
        system_prompt = (
            "Ты — 'Второй Мозг', дружелюбный ИИ-ассистент. "
            "Пока у тебя нет сохранённых фактов об этом пользователе, "
            "но ты можешь дать полезный общий совет."
        )

    try:
        # generate_content — синхронный блокирующий сетевой вызов, уводим
        # его в отдельный поток, чтобы не подвешивать event loop бота
        # на время ожидания ответа от Gemini (иначе зависли бы все
        # остальные пользователи и обработка входящих вебхуков).
        response = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-flash-lite-latest",
            contents=[
                types.Content(
                    role="user",
                    parts=[types.Part(text=f"{system_prompt}\n\nВопрос пользователя: {user_question}")]
                )
            ]
        )
        return response.text
    except Exception as e:
        print(f"❌ Ошибка Gemini: {e}")
        return "😵 Извини, я завис. Попробуй ещё раз позже."


async def describe_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """Описывает фото и распознаёт текст на нём (скриншоты, визитки, доски и т.п.)."""
    if not GOOGLE_API_KEY:
        return "⚠️ Ошибка: не настроен API-ключ Google Gemini."
    prompt = (
        "Опиши, что на этом изображении, и если на нём есть текст — "
        "перепиши его полностью. Ответ дай кратко и по делу, на русском."
    )
    try:
        response = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-flash-lite-latest",
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part(text=prompt),
                        types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    ],
                )
            ],
        )
        return response.text
    except Exception as e:
        print(f"❌ Ошибка анализа изображения: {e}")
        return "😵 Не смог разобрать изображение."


async def summarize_for_digest(entries: list[dict], today_str: str) -> str:
    """Делает краткое саммари списка заметок для ежедневного дайджеста.

    entries — список {"text": ..., "created_at_local": "<дата и время в поясе пользователя>"}.
    today_str — сегодняшняя дата в поясе пользователя (например, "29 августа 2026").

    Явно передаём Gemini дату каждой заметки и сегодняшнюю дату — иначе
    модель дословно повторяет относительные слова из исходного текста
    («завтра», «сегодня»), которые были верны в момент написания заметки,
    но могли устареть к моменту отправки дайджеста (например, заметка
    написана вчера со словом «завтра» — та же заметка «завтра» уже
    относится к сегодняшнему дню, а не к дню, когда она писалась).
    """
    if not GOOGLE_API_KEY or not entries:
        return ""
    joined = "\n".join(f"- [{e['created_at_local']}] {e['text']}" for e in entries)
    prompt = (
        f"Сегодня {today_str}. Вот заметки пользователя за последние сутки, "
        "каждая с датой и временем, когда она была написана:\n"
        f"{joined}\n\n"
        "Сделай короткое дружелюбное саммари в 3-5 предложениях: о чём в целом "
        "были записи, есть ли что-то, что стоит не забыть. "
        "ВАЖНО: если в заметке есть относительные слова о времени "
        "(«сегодня», «завтра», «через час» и т.п.) — пересчитай их "
        "относительно сегодняшней даты и даты/времени самой заметки, "
        "а не повторяй дословно. Например, если заметка написана вчера "
        "и в ней было слово «завтра», в саммари это уже день, который "
        "наступил сегодня — так и напиши."
    )
    try:
        response = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-flash-lite-latest",
            contents=[prompt],
        )
        return response.text
    except Exception as e:
        print(f"❌ Ошибка составления дайджеста: {e}")
        return ""


async def transcribe_voice(file_path: str) -> str:
    """Распознаёт голосовое сообщение через Groq (Whisper Large v3 Turbo).

    Groq SDK синхронный — сам сетевой вызов уводим в отдельный поток,
    как и остальные блокирующие вызовы в этом файле.
    """
    if not groq_client:
        return "⚠️ Не настроен ключ Groq (GROQ_API_KEY)."

    def _sync_transcribe() -> str:
        with open(file_path, "rb") as f:
            result = groq_client.audio.transcriptions.create(
                file=f,
                model="whisper-large-v3-turbo",
                language="ru",
            )
        return result.text

    try:
        text = await asyncio.to_thread(_sync_transcribe)
        return text.strip() or "Не удалось распознать речь."
    except Exception as e:
        print(f"❌ Ошибка Groq (распознавание голоса): {e}")
        return "😵 Не смог разобрать голосовое."


# Действия, которые агент реально умеет выполнять (должно совпадать с тем,
# что понимает eva_command_parser.py и eva_agent/actions.py). Модели строго
# запрещено придумывать действие вне этого списка — она либо выбирает
# что-то отсюда, либо честно отвечает, что не поняла команду.
_EVA_ACTIONS_SCHEMA = """
- open_app: {"app": "название программы"}
- open_website: {"url": "адрес сайта"}
- create_file: {"path": "имя файла", "content": "текст"}
- read_file: {"path": "имя файла"}
- delete_file: {"path": "имя файла"}
- edit_file: {"path": "имя файла", "mode": "append" или "overwrite", "content": "текст"}
- copy_file: {"src": "откуда", "dst": "куда"}
- move_file: {"src": "откуда", "dst": "куда"}
- list_files: {"folder": "название папки, например 'загрузки' или 'документы'"}
- search_file: {"query": "что искать"}
- screenshot: {}
- set_volume: {"direction": "up"/"down"/"mute"/"unmute"} или {"level": число от 0 до 100}
- power_action: {"action": "shutdown"/"restart"/"lock"}
- type_text: {"text": "что напечатать в активном окне"}
- press_key: {"keys": "сочетание клавиш, например 'ctrl+s' или 'enter'"}
- web_search: {"query": "что искать", "engine": "google"/"yandex"/"bing" (необязательно, по умолчанию google), "browser": "chrome"/"opera"/"firefox"/"edge" (необязательно, по умолчанию браузер по умолчанию)}
- ui_click: {"window": "название окна программы", "control": "название кнопки или пункта меню"}
- ui_list_controls: {"window": "название окна программы"}
- ask_web: {"query": "вопрос, на который нужен АКТУАЛЬНЫЙ ответ из интернета текстом (не открытие браузера, а именно текстовый ответ)"}
"""

# Действия, которые меняют/удаляют данные или влияют на работу ПК целиком —
# для них ВСЕГДА нужно подтверждение, даже если модель сама так не подумала.
_DESTRUCTIVE_ACTIONS = {"create_file", "delete_file", "edit_file", "copy_file", "move_file", "power_action"}


async def interpret_eva_command(text: str) -> dict:
    """Свободный разбор команды Еве через Gemini — подключается как запасной
    вариант, когда жёсткие шаблоны eva_command_parser.py не распознали
    фразу. Модель обязана выбрать action СТРОГО из списка выше или вернуть
    action: null, если команда не про один из этих сценариев — это не
    произвольное выполнение чего угодно, а классификация в рамках уже
    реализованного набора безопасных действий.

    Возвращает dict: {"action": str|None, "params": dict, "error": str|None}
    """
    if not GOOGLE_API_KEY:
        return {"action": None, "params": {}, "error": "⚠️ Не настроен API-ключ Google Gemini."}

    prompt = (
        "Ты помогаешь разобрать команду голосового ассистента Евы в строго "
        "структурированный JSON. Вот список действий, которые реально умеет "
        f"выполнять агент, и их параметры:\n{_EVA_ACTIONS_SCHEMA}\n"
        f"Команда пользователя: «{text}»\n\n"
        "Ответь ТОЛЬКО валидным JSON без пояснений и без markdown-разметки, "
        'строго в виде {"action": "...", "params": {...}}. '
        "Если команда явно не про одно из перечисленных действий — верни "
        '{"action": null, "params": {}}. Никогда не придумывай действие, '
        "которого нет в списке."
    )
    try:
        response = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-flash-lite-latest",
            contents=[prompt],
        )
        raw = response.text.strip()
        # На случай, если модель всё же обернёт ответ в ```json ... ``` — снимаем разметку
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw[4:] if raw.lower().startswith("json") else raw

        data = json.loads(raw)
        action = data.get("action")
        params = data.get("params", {}) or {}

        if action is None:
            return {"action": None, "params": {}, "error": None}

        known_actions = {line.split(":")[0].lstrip("- ").strip() for line in _EVA_ACTIONS_SCHEMA.strip().split("\n")}
        if action not in known_actions:
            return {"action": None, "params": {}, "error": None}

        return {
            "action": action,
            "params": params,
            "requires_confirmation": action in _DESTRUCTIVE_ACTIONS,
            "error": None,
        }
    except Exception as e:
        print(f"❌ Ошибка AI-разбора команды Евы: {e}")
        return {"action": None, "params": {}, "error": "😵 Не смог разобрать команду."}


async def ask_web(query: str) -> str:
    """Отвечает текстом с АКТУАЛЬНЫМИ данными из интернета — в отличие от
    обычного get_ai_response (который знает только память пользователя и
    "замороженные" знания модели), здесь подключён встроенный инструмент
    Google Search у самой Gemini — модель реально ищет и опирается на
    свежие результаты, а не только на то, что запомнила при обучении.

    Выполняется целиком на сервере — desktop-агент тут не участвует,
    ответ уходит владельцу прямо в Telegram.
    """
    if not GOOGLE_API_KEY:
        return "⚠️ Не настроен API-ключ Google Gemini."
    try:
        response = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-flash-lite-latest",
            contents=[query],
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
        return response.text or "Не нашёл ничего конкретного по этому запросу."
    except Exception as e:
        print(f"❌ Ошибка веб-поиска Gemini (ask_web): {e}")
        return "😵 Не смог найти информацию в интернете."