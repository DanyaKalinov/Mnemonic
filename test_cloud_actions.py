import pytest
from cloud_actions import execute_cloud_action


@pytest.mark.asyncio
async def test_unknown_action_returns_error():
    out = await execute_cloud_action("nonexistent", {}, user_id=111)
    assert out["status"] == "error"


@pytest.mark.asyncio
async def test_memory_search_without_db(monkeypatch):
    """Test that memory_search handler accepts params correctly."""
    async def fake_search(user_id, query, k):
        return ["запись 1", "запись 2"]
    monkeypatch.setattr("cloud_actions.search_memory", fake_search)

    result = await execute_cloud_action("memory_search", {"query": "тест"}, user_id=111)
    assert result["status"] == "done"
    assert "запись 1" in result["result"]


@pytest.mark.asyncio
async def test_fetch_web_results_structure(monkeypatch):
    """Test that fetch_web_results calls tavily correctly."""
    async def fake_tavily(query):
        return "Сырые сниппеты из Tavily"
    monkeypatch.setattr("cloud_actions.tavily_search_snippets", fake_tavily)

    result = await execute_cloud_action("fetch_web_results", {"query": "курс доллара"}, user_id=111)
    assert result["status"] == "done"
    assert "Tavily" in result["result"]
