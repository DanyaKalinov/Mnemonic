"""Слегка следимая память о chat-сообщениях, отправленных агенту.

Нужна для обратной маршрутизации (chat_result / confirm_request):
зная message_id, сервер вспоминает, какому пользователю и из какого
канала пришло сообщение. Память процесса (не БД) — для И1 достаточно,
на рестарте сервера потеря карт допустима (результат всё равно уйдёт
в Telegram, как и раньше).
"""
from typing import Optional

_next_id = 0
_chat_meta: dict[int, dict] = {}


def new_message_id() -> int:
    global _next_id
    _next_id += 1
    return _next_id


def register_message(message_id: int, user_id: int, origin: str, command_text: str) -> None:
    _chat_meta[message_id] = {
        "user_id": user_id,
        "origin": origin,
        "command_text": command_text,
    }


def get_message(message_id: int) -> Optional[dict]:
    return _chat_meta.get(message_id)