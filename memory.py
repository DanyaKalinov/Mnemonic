import asyncio
import os

from dotenv import load_dotenv
from google import genai
from google.genai import types

from db import get_pool

load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
client = genai.Client(api_key=GOOGLE_API_KEY)

TAGS = ["идея", "задача", "контакт", "цитата", "факт", "фото", "другое"]


def _get_embedding_sync(text: str) -> list[float]:
    if not GOOGLE_API_KEY:
        raise ValueError("❌ GOOGLE_API_KEY не задан.")
    try:
        result = client.models.embed_content(
            model="gemini-embedding-2",
            contents=[text],
            # 768 измерений — чтобы совпадало со схемой колонки vector(768) в Supabase
            config=types.EmbedContentConfig(output_dimensionality=768),
        )
        return result.embeddings[0].values
    except Exception as e:
        print(f"❌ Ошибка эмбеддинга: {e}")
        return [0.0] * 768


def _classify_tag_sync(text: str) -> str:
    prompt = (
        "Определи категорию заметки одним словом строго из списка: "
        f"{', '.join(TAGS)}.\n"
        f"Заметка: {text[:500]}\n"
        "Ответь только одним словом из списка, без пояснений."
    )
    try:
        response = client.models.generate_content(
            model="gemini-flash-lite-latest",
            contents=[prompt],
        )
        guess = response.text.strip().lower()
        for tag in TAGS:
            if tag in guess:
                return tag
        return "другое"
    except Exception as e:
        print(f"❌ Ошибка определения тега: {e}")
        return "другое"


async def add_memory(user_id: int, text: str, tag: str | None = None) -> None:
    if not text.strip():
        return
    # Эмбеддинг и (если тег не передан явно, как для фото) классификация —
    # синхронные сетевые вызовы, уводим в отдельный поток.
    embedding = await asyncio.to_thread(_get_embedding_sync, text)
    if tag is None:
        tag = await asyncio.to_thread(_classify_tag_sync, text)
    async with get_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO memories (user_id, text, embedding, tag) VALUES ($1, $2, $3, $4)",
            user_id, text, embedding, tag,
        )
    print(f"✅ Память сохранена (user {user_id}, тег: {tag}): {text[:50]}...")


async def search_memory(user_id: int, query: str, k: int = 3) -> list[str]:
    embedding = await asyncio.to_thread(_get_embedding_sync, query)
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT text
            FROM memories
            WHERE user_id = $1
            ORDER BY embedding <=> $2
            LIMIT $3
            """,
            user_id, embedding, k,
        )
    return [row["text"] for row in rows]