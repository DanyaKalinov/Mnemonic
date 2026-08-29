import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")  # для распознавания голосовых (Whisper через Groq)

# Секрет, которым desktop-агент Евы подтверждает себя при подключении
# по WebSocket (тот же принцип, что и TELEGRAM_WEBHOOK_SECRET).
EVA_AGENT_SECRET = os.getenv("EVA_AGENT_SECRET")

# Telegram ID администратора — именно ему будут приходить заявки на доступ
# (узнать свой ID можно, например, у бота @userinfobot)
_admin_id_raw = os.getenv("ADMIN_ID")
if not _admin_id_raw:
    raise ValueError("❌ ADMIN_ID не найден в .env")
ADMIN_ID = int(_admin_id_raw)

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден в .env")
if not GOOGLE_API_KEY:
    raise ValueError("❌ GOOGLE_API_KEY не найден в .env")

if not GROQ_API_KEY:
    print("⚠️ Предупреждение: GROQ_API_KEY не задан — голосовые не будут работать.")
if not EVA_AGENT_SECRET:
    print("⚠️ Предупреждение: EVA_AGENT_SECRET не задан — desktop-агент Евы не сможет подключиться.")