from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import Message

from config import ADMIN_ID
from db import get_user


class ApprovalMiddleware(BaseMiddleware):
    """
    Пропускает дальше только одобренных пользователей и админа.
    /start обрабатывается отдельно (он и запускает саму заявку на доступ),
    поэтому его мидлварь не трогает.
    """

    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any],
    ) -> Any:
        if event.text and event.text.startswith("/start"):
            return await handler(event, data)

        user_id = event.from_user.id
        if user_id == ADMIN_ID:
            return await handler(event, data)

        user = await get_user(user_id)
        if user is None or user["status"] != "approved":
            await event.answer(
                "⏳ Доступ пока не одобрен. Напиши /start, чтобы отправить "
                "(или повторить) заявку администратору."
            )
            return

        return await handler(event, data)