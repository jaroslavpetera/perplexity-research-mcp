"""Tests without real API calls: Perplexity is mocked with respx."""

import httpx
import pytest
import respx

import server

AGENT = server.API_BASE
SEARCH = server.SEARCH_URL


@pytest.fixture(autouse=True)
def fake_key(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "test-key")
    monkeypatch.setattr(server, "POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(server, "WAIT_BUDGET_SECONDS", 0.5)


def completed(rid="resp_1", text="Answer [web:1]", cost=0.0123):
    return {
        "id": rid,
        "status": "completed",
        "output": [
            {
                "type": "search_results",
                "queries": ["q"],
                "results": [
                    {"id": 1, "title": "Source A", "url": "https://a.example"},
                    {"id": 2, "title": "Source B", "url": "https://b.example"},
                ],
            },
            {
                "type": "fetch_url_results",
                "content": [{"title": "Source A", "url": "https://a.example", "snippet": "..."}],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            },
        ],
        "usage": {"cost": {"currency": "USD", "total_cost": cost}},
    }


@respx.mock
async def test_search_formats_text_sources_and_cost():
    route = respx.post(AGENT).respond(200, json=completed())
    out = await server.perplexity_search("What is MCP?")

    assert out.startswith("Answer [web:1]")
    assert "[1] Source A - https://a.example" in out
    assert "[2] Source B - https://b.example" in out
    assert out.count("https://a.example") == 1  # deduplicated
    assert "0.0123 USD" in out

    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer test-key"
    body = httpx.Response(200, content=sent.content).json()
    assert body == {"preset": "low", "input": "What is MCP?", "background": True}


@respx.mock
async def test_search_filters_go_into_web_search_tool():
    route = respx.post(AGENT).respond(200, json=completed())
    await server.perplexity_search("q", recency="week", domains=["-reddit.com"])

    body = httpx.Response(200, content=route.calls.last.request.content).json()
    assert body["tools"][0] == {
        "type": "web_search",
        "filters": {"search_recency_filter": "week", "search_domain_filter": ["-reddit.com"]},
    }


async def test_invalid_inputs_are_rejected_without_calling_api():
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(AGENT)
        assert "preset" in await server.perplexity_search("q", preset="xhigh")
        assert "recency" in await server.perplexity_search("q", recency="decade")
        assert "Do not mix" in await server.perplexity_search("q", domains=["a.com", "-b.com"])
        assert "empty" in await server.perplexity_search("   ")
        assert "depth" in await server.perplexity_research("q", depth="ultra")
        assert not route.called


@respx.mock
async def test_research_polls_until_completed():
    respx.post(AGENT).respond(200, json={"id": "resp_9", "status": "queued"})
    respx.get(f"{AGENT}/resp_9").mock(
        side_effect=[
            httpx.Response(200, json={"id": "resp_9", "status": "in_progress"}),
            httpx.Response(200, json=completed("resp_9", "Done")),
        ]
    )
    out = await server.perplexity_research("Big question")
    assert out.startswith("Done")


@respx.mock
async def test_research_returns_id_when_still_running():
    respx.post(AGENT).respond(200, json={"id": "resp_7", "status": "queued"})
    respx.get(f"{AGENT}/resp_7").respond(200, json={"id": "resp_7", "status": "in_progress"})
    out = await server.perplexity_research("Big question")
    assert "resp_7" in out and "perplexity_result" in out


@respx.mock
async def test_result_and_failed_status():
    respx.get(f"{AGENT}/resp_5").respond(
        200, json={"id": "resp_5", "status": "failed", "error": {"message": "boom"}}
    )
    assert "failed: boom" in await server.perplexity_result("resp_5")


@respx.mock
async def test_cancel():
    respx.post(f"{AGENT}/resp_5/cancel").respond(200, json={"id": "resp_5", "status": "cancelling"})
    assert "being cancelled" in await server.perplexity_cancel("resp_5")


@pytest.mark.parametrize(
    "status, headers, expected",
    [
        (401, {}, "rejected the API key"),
        (402, {}, "insufficient credit"),
        (429, {"Retry-After": "12"}, "in 12 s"),
        (503, {}, "problems on its side"),
        (400, {}, "Perplexity API error (400)"),
    ],
)
@respx.mock
async def test_http_errors(status, headers, expected):
    respx.post(AGENT).respond(status, headers=headers, json={"error": "bad request"})
    assert expected in await server.perplexity_search("q")


@respx.mock
async def test_timeout():
    respx.post(AGENT).mock(side_effect=httpx.ReadTimeout("slow"))
    assert "did not respond in time" in await server.perplexity_search("q")


@respx.mock
async def test_invalid_json():
    respx.post(AGENT).respond(200, content=b"<html>oops</html>")
    assert "not valid JSON" in await server.perplexity_search("q")


async def test_missing_key(monkeypatch, tmp_path):
    monkeypatch.delenv("PERPLEXITY_API_KEY")
    monkeypatch.setattr(server, "ENV_FILE", tmp_path / ".env")
    assert "PERPLEXITY_API_KEY is missing" in await server.perplexity_search("q")


@respx.mock
async def test_key_from_env_file(monkeypatch, tmp_path):
    monkeypatch.delenv("PERPLEXITY_API_KEY")
    env = tmp_path / ".env"
    env.write_text('# comment\nPERPLEXITY_API_KEY="from-file"\n', encoding="utf-8")
    monkeypatch.setattr(server, "ENV_FILE", env)
    route = respx.post(AGENT).respond(200, json=completed())
    await server.perplexity_search("q")
    assert route.calls.last.request.headers["Authorization"] == "Bearer from-file"


@respx.mock
async def test_web_results_dedupes_and_builds_payload():
    route = respx.post(SEARCH).respond(
        200,
        json={
            "id": "s1",
            "results": [
                {"title": "A", "url": "https://a.example", "snippet": "snippet A", "date": "2026-01-02"},
                {"title": "A again", "url": "https://a.example", "snippet": "dup"},
                {"title": "B", "url": "https://b.example", "snippet": ""},
            ],
        },
    )
    out = await server.perplexity_web_results(
        ["q1", "q2"], max_results=5, recency="month", languages=["en"], country="us"
    )
    assert "[1] A (2026-01-02)\nhttps://a.example\nsnippet A" in out
    assert "[2] B\nhttps://b.example" in out
    assert "A again" not in out

    body = httpx.Response(200, content=route.calls.last.request.content).json()
    assert body == {
        "query": ["q1", "q2"],
        "max_results": 5,
        "max_tokens_per_page": 512,
        "search_recency_filter": "month",
        "search_language_filter": ["en"],
        "country": "US",
    }


@respx.mock
async def test_web_results_handles_grouped_results():
    respx.post(SEARCH).respond(
        200,
        json={"id": "s2", "results": [[{"title": "A", "url": "https://a.example", "snippet": "x"}],
                                      [{"title": "B", "url": "https://b.example", "snippet": "y"}]]},
    )
    out = await server.perplexity_web_results(["q1", "q2"])
    assert "https://a.example" in out and "https://b.example" in out


async def test_web_results_validation():
    assert "1 to 5" in await server.perplexity_web_results(["q"] * 6)
    assert "between 1 and 20" in await server.perplexity_web_results("q", max_results=50)
