import pytest
from chat_state import new_message_id, register_message
from eva_engine import handle_chat_result, handle_confirm_request
from eva_agent_registry import register_agent, unregister_agent, notify_app


class FakeBot:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, "kw": kw})
    async def send_photo(self, chat_id, photo, caption=None, **kw):
        self.sent.append({"chat_id": chat_id, "photo": photo, "caption": caption, "kw": kw})


@pytest.mark.asyncio
async def test_handle_chat_result_answer_delivers_to_telegram():
    bot = FakeBot()
    mid = new_message_id()
    register_message(mid, user_id=111, origin="app", command_text="привет")
    await handle_chat_result(bot, {"type": "chat_result", "message_id": mid, "kind": "answer", "text": "Привет!"})
    assert any(s["chat_id"] == 111 and "Привет!" in s["text"] for s in bot.sent)


@pytest.mark.asyncio
async def test_confirm_request_creates_command_with_buttons(monkeypatch):
    from eva_agent_registry import register_agent, unregister_agent
    bot = FakeBot()

    # Mock add_eva_command to avoid DB dependency
    async def fake_add_eva_command(*args, **kwargs):
        return 999
    monkeypatch.setattr("db.add_eva_command", fake_add_eva_command)

    register_agent("p1", None, owner_id=111)
    try:
        await handle_confirm_request(
            bot, "p1",
            {
                "type": "confirm_request",
                "message_id": 424242,
                "local_steps": [{"action": "delete_file", "params": {"path": "notes.txt"}, "description": "Ева удалит файл «notes.txt»."}],
            },
        )
        assert any(s["chat_id"] == 111 and s["kw"].get("reply_markup") for s in bot.sent)
    finally:
        unregister_agent("p1")


class FakeWS:
    def __init__(self):
        self.sent = []
    async def send_str(self, s):
        self.sent.append(s)


@pytest.mark.asyncio
async def test_notify_app_sends_chat_echo():
    ws = FakeWS()
    register_agent("p1", ws, owner_id=111)
    ok = await notify_app(111, "eva", "готово")
    assert ok and '"role": "eva"' in ws.sent[0]
    unregister_agent("p1")
