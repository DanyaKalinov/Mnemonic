"""
Разбор текста команды Евы в структурированное действие для desktop-агента.

Как и reminder_parser.py — чистая функция без зависимостей от Telegram/БД,
поэтому легко покрывается юнит-тестами напрямую.

Поддерживаемые действия:
- open_app    — «открой блокнот», «запусти калькулятор»
- create_file — «создай файл notes.txt с текстом купить молоко»
- read_file   — «прочитай файл notes.txt», «покажи файл notes.txt»
- delete_file — «удали файл notes.txt»

create_file и delete_file помечены как requires_confirmation=True —
именно эти два меняют/удаляют файлы на ПК, поэтому перед реальным
выполнением сервер должен спросить подтверждение владельца в Telegram.
"""
import re
from dataclasses import dataclass, field
from typing import Optional

FILENAME_PATTERN = re.compile(r'[\w\-]+\.\w+')
CONTENT_PATTERN = re.compile(
    r'(?:с\s+текстом|с\s+содержимым|текст[:\s])\s*(.+)$', re.IGNORECASE,
)

USAGE_ERROR = (
    "⚠️ Не понял команду. Примеры:\n"
    "• /eva открой блокнот\n"
    "• /eva создай файл notes.txt с текстом купить молоко\n"
    "• /eva прочитай файл notes.txt\n"
    "• /eva удали файл notes.txt"
)
NO_FILENAME_ERROR = "⚠️ Не нашёл имя файла (нужно расширение, например notes.txt)."


@dataclass
class EvaCommandParseResult:
    action: Optional[str] = None  # open_app | create_file | read_file | delete_file
    params: dict = field(default_factory=dict)
    requires_confirmation: bool = False
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def parse_eva_command(text: str) -> EvaCommandParseResult:
    normalized = text.strip().lower()

    # Порядок проверок важен: сначала самые специфичные (файловые действия
    # с явным словом "файл"), потом общий "открой/запусти" для приложений —
    # иначе "открой файл X" ошибочно распознается как открытие приложения.
    if re.search(r'\bудали(ть)?\b', normalized) and re.search(r'\bфайл\w*\b', normalized):
        filename_match = FILENAME_PATTERN.search(text)
        if not filename_match:
            return EvaCommandParseResult(error=NO_FILENAME_ERROR)
        return EvaCommandParseResult(
            action="delete_file",
            params={"path": filename_match.group(0)},
            requires_confirmation=True,
        )

    if re.search(r'\b(создай|сделай)\b', normalized) and re.search(r'\bфайл\w*\b', normalized):
        filename_match = FILENAME_PATTERN.search(text)
        if not filename_match:
            return EvaCommandParseResult(error=NO_FILENAME_ERROR)
        content_match = CONTENT_PATTERN.search(text)
        content = content_match.group(1).strip() if content_match else ""
        return EvaCommandParseResult(
            action="create_file",
            params={"path": filename_match.group(0), "content": content},
            requires_confirmation=True,
        )

    if re.search(r'\b(прочитай|покажи|открой)\b', normalized) and re.search(r'\bфайл\w*\b', normalized):
        filename_match = FILENAME_PATTERN.search(text)
        if not filename_match:
            return EvaCommandParseResult(error=NO_FILENAME_ERROR)
        return EvaCommandParseResult(
            action="read_file",
            params={"path": filename_match.group(0)},
            requires_confirmation=False,
        )

    if re.search(r'\b(открой|запусти)\b', normalized):
        # Всё, что осталось после триггерного слова — имя приложения
        app_name = re.sub(r'^.*?\b(открой|запусти)\b\s*', '', text, count=1, flags=re.IGNORECASE).strip()
        if not app_name:
            return EvaCommandParseResult(error="⚠️ Какое приложение открыть? Например: /eva открой блокнот")
        return EvaCommandParseResult(
            action="open_app",
            params={"app": app_name},
            requires_confirmation=False,
        )

    return EvaCommandParseResult(error=USAGE_ERROR)
