# Perplexity Research MCP for Claude Desktop

A small, single-file Python MCP server that lets **Claude Desktop** search the web and run
**deep research with Perplexity**, without hitting Claude Desktop's tool-call timeout.

- **Local, nothing to deploy.** Claude Desktop starts the server itself over stdio.
- **Long research does not time out.** Research jobs run in Perplexity's
  [background mode](https://docs.perplexity.ai/docs/agent-api/background-mode). If a job is
  not finished within ~45 s, the tool returns a job ID and Claude collects the result later.
- **Current Perplexity APIs.** Built on the Agent API (presets `fast` … `xhigh`) and the
  Search API, not the legacy Sonar models.
- **Answers with numbered sources and the real cost** of every query.
- **Small and readable.** One file (~380 lines), two dependencies (`mcp`, `httpx`),
  tests with a mocked API.

## Tools

| Tool | What it does | Approx. cost |
|---|---|---|
| `perplexity_web_results` | Raw ranked results (title, URL, snippet, date) from the Search API. Claude summarizes them itself. Up to 5 queries at once, deduplicated. | cheapest |
| `perplexity_search` | Written answer with sources, Agent API presets `fast` / `low` (default) / `medium` | ~$0.002–0.01 |
| `perplexity_research` | Deep research in the background, `depth`: `medium` / `high` (default) / `xhigh` | ~$0.01 / $0.07 / $0.16+ |
| `perplexity_result` | Status or result of a background job by ID | free |
| `perplexity_cancel` | Cancels a running job | free |

Costs are rough estimates based on [Perplexity pricing](https://docs.perplexity.ai/docs/getting-started/pricing)
(October 2026). The actual cost is appended to every answer.

Search tools support filters: `recency` (`hour` … `year`), `domains` (allow list, or deny list
with a `-` prefix), `after_date` / `before_date` (`MM/DD/YYYY`). `perplexity_web_results` also
takes `languages` and `country`.

## How it differs from the official Perplexity MCP server

Perplexity maintains an official server,
[perplexityai/modelcontextprotocol](https://github.com/perplexityai/modelcontextprotocol),
plus a hosted remote server. Use the official one if you want a widely used, maintained package.
This project is an alternative if you want:

- **deep research that survives Claude Desktop's tool-call timeout** (background jobs +
  `perplexity_result` instead of one long call),
- **a short Python codebase** you can read in a few minutes and adapt,
- **the query cost shown in every answer.**

## Setup

Requirements: Python 3.10+, Claude Desktop, a Perplexity API key.

### 1. Install

```bash
git clone https://github.com/<your-account>/perplexity-research-mcp.git
cd perplexity-research-mcp
python -m venv .venv
```

macOS / Linux:

```bash
.venv/bin/pip install -r requirements.txt
```

Windows (PowerShell):

```powershell
.venv\Scripts\pip install -r requirements.txt
```

### 2. Add your API key

Create a key at <https://console.perplexity.ai> (and add credit). Copy `.env.example` to `.env`
and fill in the key:

```
PERPLEXITY_API_KEY=pplx-...
```

`.env` is in `.gitignore`. Never commit it. If the key leaks, revoke it in the console.

### 3. Register the server in Claude Desktop

Open the config file (Claude Desktop → Settings → Developer → Edit Config):

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Add the server with **absolute paths** to the virtualenv's Python and to `server.py`.

macOS / Linux:

```json
{
  "mcpServers": {
    "perplexity": {
      "command": "/Users/you/perplexity-research-mcp/.venv/bin/python",
      "args": ["/Users/you/perplexity-research-mcp/server.py"]
    }
  }
}
```

Windows:

```json
{
  "mcpServers": {
    "perplexity": {
      "command": "C:\\Users\\you\\perplexity-research-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\you\\perplexity-research-mcp\\server.py"]
    }
  }
}
```

If the file already has an `mcpServers` section, add the `perplexity` entry to it.

The key is read from `.env` next to `server.py`, so it does not have to be in the Claude config.
You can also pass it through `"env": {"PERPLEXITY_API_KEY": "..."}` in the config instead.

### 4. Restart Claude Desktop

Quit Claude Desktop completely (including the tray / menu bar icon) and start it again.
Under Settings → Developer, the `perplexity` server should show as running.

### Try it

```
Use perplexity_search with preset fast: what will the weather be in London tomorrow?
List the sources and the query cost.
```

```
Use perplexity_research with depth medium: what were the most important EV news of the
last month? If it returns a job ID, collect the result with perplexity_result.
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PERPLEXITY_API_KEY` | (required) | API key, from the environment or `.env` |
| `PPLX_WAIT_SECONDS` | `45` | How long one tool call waits before returning a job ID |

## Troubleshooting

- **The server does not appear:** check the absolute paths in the config and restart Claude
  Desktop fully.
- **Logs:** `mcp-server-perplexity.log` in Claude Desktop's log folder
  (macOS: `~/Library/Logs/Claude/`, Windows: `%APPDATA%\Claude\logs\`).
  The server never logs the API key or the query text.
- **401:** the key is wrong or revoked. **402:** add credit. **429:** rate limit, try again later.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The tests mock the Perplexity API with [respx](https://lundberg.github.io/respx/), so they
make no real calls and cost nothing.

## Privacy

Queries are sent to Perplexity and handled under
[Perplexity's API policies](https://www.perplexity.ai/hub/legal). Nothing is stored locally.

## License

[MIT](LICENSE)
