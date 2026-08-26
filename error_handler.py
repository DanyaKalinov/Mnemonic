"""
Глобальный перехватчик необработанных ошибок.

Без этого любое неожиданное исключение в хендлере просто тонет в логах
Render — бот молчит, а узнаёшь о проблеме только когда кто-то напишет
"бот не отвечает". Здесь любая необработанная ошибка сразу:
1) печатается в лог (как и раньше — для отладки на сервере),
2) и дополнительно улетает тебе как админу в личку в Telegram.
"""
import html
import traceback

from aiogram import Bot, Dispatcher
from aiogram.types import ErrorEvent

from config import ADMIN_ID

# Лимит одного сообщения в Telegram — 4096 символов. Оставляем запас
# под обвязку текста (заголовок, теги <code>).
MAX_TRACEBACK_LENGTH = 3500


def register_error_handler(dp: Dispatcher) -> None:
    @dp.error()
    async def global_error_handler(event: ErrorEvent, bot: Bot) -> bool:
        tb_text = "".join(
            traceback.format_exception(
                type(event.exception), event.exception, event.exception.__traceback__
            )
        )
        print(f"❌ Необработанная ошибка:\n{tb_text}", flush=True)

        short_tb = html.escape(tb_text[-MAX_TRACEBACK_LENGTH:])
        alert_text = f"🚨 <b>Необработанная ошибка в боте</b>\n\n<code>{short_tb}</code>"

        try:
            await bot.send_message(ADMIN_ID, alert_text, parse_mode="HTML")
        except Exception as notify_error:
            # Если даже алерт не смог уйти (например, сообщение всё ещё
            # слишком длинное или Telegram недоступен) — не роняем бота
            # из-за этого, просто логируем и продолжаем работать.
            print(f"⚠️ Не удалось отправить алерт админу: {notify_error}", flush=True)

        # True — сообщаем aiogram, что ошибку обработали, не нужно
        # прокидывать её дальше и ронять процесс.
        return True