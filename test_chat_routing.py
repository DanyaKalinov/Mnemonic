import pytest
from eva_agent_registry import register_agent, get_user_agent, send_to_user_agent, unregister_agent
from chat_state import new_message_id, register_message, get_message


class FakeWS:
    def __init__(self): self.sent = []
    async def send_str(self, s): self.sent.append(s)


@pytest.mark.asyncio
async def test_registry_routes_to_specific_user_only():
    ws_owner = FakeWS()
    ws_other = FakeWS()
    register_agent("p1", ws_owner, owner_id=111)
    register_agent("p2", ws_other, owner_id=222)
    assert get_user_agent(111) is ws_owner
    assert get_user_agent(222) is ws_other
    assert get_user_agent(333) is None
    ok = await send_to_user_agent(111, {"type": "chat", "text": "hi"})
    assert ok and len(ws_owner.sent) == 1 and ws_other.sent == []
    unregister_agent("p1")
    unregister_agent("p2")


def test_chat_state_message_roundtrip():
    mid = new_message_id()
    register_message(mid, user_id=111, origin="app", command_text="привет")
    m = get_message(mid)
    assert m == {"user_id": 111, "origin": "app", "command_text": "привет"}
    assert get_message(999999) is None
