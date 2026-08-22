import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
YANDEX_SPEECHKIT_KEY = os.getenv("YANDEX_SPEECHKIT_KEY")  # опционально

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

if not YANDEX_SPEECHKIT_KEY:
    print("⚠️ Предупреждение: YANDEX_SPEECHKIT_KEY не задан — голосовые не будут работать.")