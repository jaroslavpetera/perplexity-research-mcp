"""Local MCP server that gives Claude Desktop access to the Perplexity APIs.

Claude Desktop launches this server itself over stdio; nothing is deployed.

Configuration:
  PERPLEXITY_API_KEY - key from https://console.perplexity.ai
                       Read from the environment, or from a .env file next to
                       this script (a line PERPLEXITY_API_KEY=...).
  PPLX_WAIT_SECONDS  - how long a tool call waits before returning a job ID
                       (default 45).

Note: stdout belongs to the MCP protocol, so logs go to stderr only
(Claude Desktop stores them in mcp-server-perplexity.log).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

API_BASE = "https://api.perplexity.ai/v1/agent"
SEARCH_URL = "https://api.perplexity.ai/search"
ENV_FILE = Path(__file__).with_name(".env")

# How long a single tool call waits before returning a job ID instead of the result.
# Claude Desktop cuts tool calls off after a while, so we stay below that limit.
WAIT_BUDGET_SECONDS = float(os.environ.get("PPLX_WAIT_SECONDS", "45"))
POLL_INTERVAL_SECONDS = 2.0
HTTP_TIMEOUT_SECONDS = 30.0
MAX_QUERY_CHARS = 8000

SEARCH_PRESETS = ("fast", "low", "medium")
RESEARCH_PRESETS = ("medium", "high", "xhigh")
RECENCY_VALUES = ("hour", "day", "week", "month", "year")
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "incomplete"}

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("perplexity-mcp")
# At INFO level httpx would log every URL; the key is not in it, but it is noise.
logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = FastMCP("perplexity")


class PerplexityError(Exception):
    """An error whose message can be shown to the user as is."""


def _load_api_key() -> str:
    key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    if not key and ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == "PERPLEXITY_API_KEY":
                key = value.strip().strip('"').strip("'")
                break
    if not key:
        raise PerplexityError(
            "PERPLEXITY_API_KEY is missing. Create a key at https://console.perplexity.ai "
            f"and save it in {ENV_FILE} as a line PERPLEXITY_API_KEY=..."
        )
    return key


def _error_message(resp: httpx.Response) -> str:
    status = resp.status_code
    if status == 401:
        return "Perplexity rejected the API key (401). Check PERPLEXITY_API_KEY in .env."
    if status == 402:
        return "Perplexity reports insufficient credit (402). Top up in the API console."
    if status == 429:
        retry = resp.headers.get("Retry-After")
        hint = f" Try again in {retry} s." if retry else " Try again shortly."
        return "Perplexity is rate limiting requests (429)." + hint
    if status >= 500:
        return f"Perplexity is having problems on its side ({status}). Try again shortly."
    try:
        detail = resp.json().get("error") or resp.text
    except ValueError:
        detail = resp.text
    return f"Perplexity API error ({status}): {str(detail)[:300]}"


async def _request(
    method: str, path: str = "", json: dict | None = None, base: str = API_BASE
) -> dict:
    headers = {"Authorization": f"Bearer {_load_api_key()}"}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.request(method, base + path, headers=headers, json=json)
    except httpx.TimeoutException:
        raise PerplexityError("Perplexity did not respond in time. Try again shortly.")
    except httpx.HTTPError as exc:
        raise PerplexityError(f"Could not connect to Perplexity ({type(exc).__name__}).")

    if resp.status_code != 200:
        log.warning("Perplexity %s %s -> %s", method, path or "/", resp.status_code)
        raise PerplexityError(_error_message(resp))
    try:
        return resp.json()
    except ValueError:
        raise PerplexityError("Perplexity returned a response that is not valid JSON.")


def _format_response(data: dict) -> str:
    """Builds the answer text and the list of sources from the `output` array."""
    texts: list[str] = []
    sources: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add_source(title: str | None, url: str | None) -> None:
        if url and url not in seen:
            seen.add(url)
            sources.append(((title or "").strip(), url))

    for item in data.get("output") or []:
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text" and part.get("text"):
                    texts.append(part["text"])
                for ann in part.get("annotations") or []:
                    add_source(ann.get("title"), ann.get("url"))
        elif kind == "search_results":
            for r in item.get("results") or []:
                add_source(r.get("title"), r.get("url"))
        elif kind == "fetch_url_results":
            for r in item.get("content") or []:
                add_source(r.get("title"), r.get("url"))

    text = "\n\n".join(texts).strip() or data.get("output_text") or "(Perplexity returned no text.)"
    if sources:
        lines = [f"[{i}] {title + ' - ' if title else ''}{url}" for i, (title, url) in enumerate(sources, 1)]
        text += "\n\nSources:\n" + "\n".join(lines)

    cost = ((data.get("usage") or {}).get("cost") or {}).get("total_cost")
    if isinstance(cost, (int, float)):
        text += f"\n\n(Query cost: {cost:.4f} USD)"
    return text


def _describe(data: dict) -> str:
    """The result of a finished job, or a message about its status."""
    status = data.get("status")
    rid = data.get("id", "?")
    if status == "completed":
        return _format_response(data)
    if status == "incomplete":
        return "The job stopped early; the result may be incomplete.\n\n" + _format_response(data)
    if status == "failed":
        err = data.get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return f"Job {rid} failed: {msg or 'unknown error'}"
    if status in ("cancelled", "cancelling"):
        return f"Job {rid} was cancelled."
    return (
        f"Job {rid} is still running (status: {status}). "
        f"Get the result with the perplexity_result tool, id={rid}."
    )


async def _wait(data: dict, budget: float) -> dict:
    """Waits for a background job to finish, at most `budget` seconds."""
    deadline = time.monotonic() + budget
    while data.get("status") not in TERMINAL_STATUSES and time.monotonic() < deadline:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        data = await _request("GET", f"/{data['id']}")
    return data


def _check_query(query: str) -> str:
    query = query.strip()
    if not query:
        raise PerplexityError("The query is empty.")
    if len(query) > MAX_QUERY_CHARS:
        raise PerplexityError(f"The query is too long (max {MAX_QUERY_CHARS} characters).")
    return query


def _web_search_tool(
    recency: str | None,
    domains: list[str] | None,
    after_date: str | None,
    before_date: str | None,
) -> dict | None:
    filters: dict = {}
    if recency:
        if recency not in RECENCY_VALUES:
            raise PerplexityError(f"recency must be one of: {', '.join(RECENCY_VALUES)}.")
        filters["search_recency_filter"] = recency
    if domains:
        if len(domains) > 20:
            raise PerplexityError("domains can have at most 20 entries.")
        excluded = [d.startswith("-") for d in domains]
        if any(excluded) and not all(excluded):
            raise PerplexityError("Do not mix allowed and excluded ('-') domains in domains.")
        filters["search_domain_filter"] = domains
    if after_date:
        filters["search_after_date_filter"] = after_date
    if before_date:
        filters["search_before_date_filter"] = before_date
    return {"type": "web_search", "filters": filters} if filters else None


async def _run(
    query: str,
    preset: str,
    budget: float,
    recency: str | None = None,
    domains: list[str] | None = None,
    after_date: str | None = None,
    before_date: str | None = None,
) -> str:
    payload: dict = {
        "preset": preset,
        "input": _check_query(query),
        "background": True,
    }
    tool = _web_search_tool(recency, domains, after_date, before_date)
    if tool:
        # Explicit tools replace the preset's tool set; filters only make sense with web_search.
        payload["tools"] = [tool, {"type": "fetch_url"}]

    started = time.monotonic()
    data = await _request("POST", json=payload)
    log.info("Job %s started (preset=%s)", data.get("id"), preset)
    data = await _wait(data, budget)
    log.info("Job %s: %s after %.0f s", data.get("id"), data.get("status"), time.monotonic() - started)
    return _describe(data)


@mcp.tool()
async def perplexity_web_results(
    query: str | list[str],
    max_results: int = 10,
    recency: str | None = None,
    domains: list[str] | None = None,
    languages: list[str] | None = None,
    country: str | None = None,
    after_date: str | None = None,
    before_date: str | None = None,
) -> str:
    """Raw search results (title, URL, snippet, date) without a written answer.

    The cheapest and fastest tool. Use it when you want to go through and summarize
    the sources yourself, or for a quick overview of what exists on a topic.

    query: one query, or a list of up to 5 queries (results are merged without duplicates).
    max_results: 1-20 per query.
    recency: hour, day, week, month, year.
    domains: up to 20 domains, either only allowed ones or only excluded ones with '-'.
    languages: ISO 639-1 language codes, e.g. ["en", "de"].
    country: ISO 3166-1 country code, e.g. "US".
    after_date / before_date: publication date in MM/DD/YYYY format.
    """
    try:
        queries = [query] if isinstance(query, str) else list(query)
        if not 1 <= len(queries) <= 5:
            raise PerplexityError("query must contain 1 to 5 queries.")
        queries = [_check_query(q) for q in queries]
        if not 1 <= max_results <= 20:
            raise PerplexityError("max_results must be between 1 and 20.")

        payload: dict = {
            "query": queries[0] if len(queries) == 1 else queries,
            "max_results": max_results,
            "max_tokens_per_page": 512,  # short snippets so results don't flood the conversation
        }
        tool = _web_search_tool(recency, domains, after_date, before_date)
        if tool:
            payload.update(tool["filters"])
        if languages:
            payload["search_language_filter"] = languages
        if country:
            payload["country"] = country.upper()

        data = await _request("POST", json=payload, base=SEARCH_URL)
    except PerplexityError as exc:
        return str(exc)

    # With multiple queries the API may group results per query (a list of lists); flatten.
    flat: list[dict] = []
    for r in data.get("results") or []:
        flat.extend(r if isinstance(r, list) else [r])

    seen: set[str] = set()
    blocks: list[str] = []
    for r in flat:
        url = r.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        date = r.get("date") or r.get("last_updated")
        head = f"[{len(blocks) + 1}] {r.get('title', '').strip()}"
        if date:
            head += f" ({date})"
        snippet = (r.get("snippet") or "").strip()
        blocks.append(f"{head}\n{url}" + (f"\n{snippet}" if snippet else ""))

    return "\n\n".join(blocks) if blocks else "No results found."


@mcp.tool()
async def perplexity_search(
    query: str,
    preset: str = "low",
    recency: str | None = None,
    domains: list[str] | None = None,
    after_date: str | None = None,
    before_date: str | None = None,
) -> str:
    """Web search through Perplexity; returns an answer with numbered sources.

    preset: fast (quickest), low (default, good balance), medium (more sources,
            multi-step lookups). For in-depth research use perplexity_research.
    recency: source age - hour, day, week, month, year.
    domains: up to 20 domains, either only allowed ones (["nasa.gov"]) or only
             excluded ones with a dash (["-reddit.com"]).
    after_date / before_date: publication date in MM/DD/YYYY format.

    If the query takes longer, returns a job ID; get the result with perplexity_result.
    """
    if preset not in SEARCH_PRESETS:
        return f"preset must be one of: {', '.join(SEARCH_PRESETS)}."
    try:
        return await _run(query, preset, WAIT_BUDGET_SECONDS, recency, domains, after_date, before_date)
    except PerplexityError as exc:
        return str(exc)


@mcp.tool()
async def perplexity_research(query: str, depth: str = "high") -> str:
    """In-depth research through Perplexity (dozens of sources, takes minutes, costs more).

    depth: medium (cheaper, about 0.01 USD), high (default, about 0.07 USD),
           xhigh (most thorough, can run code, about 0.16 USD or more).

    The job runs in the background at Perplexity. The tool waits a while; if the job
    is not done yet, it returns an ID. Then call perplexity_result with that ID until
    you get the result. Use for complex questions, not for simple fact lookups.
    """
    if depth not in RESEARCH_PRESETS:
        return f"depth must be one of: {', '.join(RESEARCH_PRESETS)}."
    try:
        return await _run(query, depth, WAIT_BUDGET_SECONDS)
    except PerplexityError as exc:
        return str(exc)


@mcp.tool()
async def perplexity_result(id: str) -> str:
    """Status or result of a job started with perplexity_search / perplexity_research.

    Waits a while for the job to finish. If it is still running, call it again.
    """
    try:
        data = await _request("GET", f"/{id.strip()}")
        return _describe(await _wait(data, WAIT_BUDGET_SECONDS))
    except PerplexityError as exc:
        return str(exc)


@mcp.tool()
async def perplexity_cancel(id: str) -> str:
    """Cancels a running job (e.g. when the research is no longer needed)."""
    try:
        data = await _request("POST", f"/{id.strip()}/cancel")
        return f"Job {id} is being cancelled (status: {data.get('status')})."
    except PerplexityError as exc:
        return str(exc)


if __name__ == "__main__":
    mcp.run()  # stdio
