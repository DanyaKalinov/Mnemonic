import asyncio
import os
from dotenv import load_dotenv
from google import genai
from google.genai import types
from memory import search_memory

load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
client = genai.Client(api_key=GOOGLE_API_KEY)

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


async def summarize_for_digest(entries: list[str]) -> str:
    """Делает краткое саммари списка заметок для ежедневного дайджеста."""
    if not GOOGLE_API_KEY or not entries:
        return ""
    joined = "\n".join(f"- {e}" for e in entries)
    prompt = (
        "Вот заметки, которые пользователь сохранил за последний день:\n"
        f"{joined}\n\n"
        "Сделай короткое дружелюбное саммари в 3-5 предложениях: "
        "о чём в целом были записи, есть ли что-то, что стоит не забыть."
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