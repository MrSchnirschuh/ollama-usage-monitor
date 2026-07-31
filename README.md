# ollama-usage-monitor

**Transparent proxy for Ollama** that sits between your client (Open WebUI, Hermes, etc.) and your local Ollama instance. It intercepts nothing by default — every `/api/chat` and `/api/generate` request passes through untouched.

But it also fetches your **ollama.com cloud usage** (session + weekly limits) and lets you check it via **chat keywords** or **HTTP endpoints** — without burning a single token.

```
Client  --/api/chat--> Usage Monitor (port 9462) --/api/chat--> Ollama (local)
                           |
                           | GET /api/usage  (every DEBOUNCE_SECONDS)
                           v
                      ollama.com (cloud API)
```

### What it tracks

| Field | Example | Meaning |
|-------|---------|---------|
| Tier | `pro` | Your ollama.com plan |
| Session | `16.6% (332/2000)` | Requests used this session (resets ~2h) |
| Weekly | `20.4% (1020/5000)` | Requests used this week (resets ~4d) |
| Burn rate | `15.88/h` | Average requests per hour |
| Models | `deepseek-v4-flash: 82.5%` | Breakdown by model |
| Recommendation | `ok` / `caution` / `limit` | Should you worry? |

## Chat Keywords

Send any of these as the **only content** of a chat message to any model via the proxy:

| Keyword | Description |
|---------|-------------|
| `usage` | Detailed usage data with model breakdown, burn rate, alerts |
| `summary` | Compact usage summary (tier, session, weekly, recommendation) |
| `history` | Last 20 usage snapshots |
| `refresh` | Force immediate refresh from ollama.com |
| `info` / `help` | Show all available commands and endpoints |

Keywords are **stripped from the chat history** — the model never sees them.

## HTTP Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check |
| `GET /usage` | Full usage data with analysis |
| `GET /usage/raw` | Raw usage data without analysis |
| `GET /usage/summary` | Human-readable summary |
| `GET /usage/history` | Last 20 usage snapshots |
| `GET /info` | Available commands and endpoints |
| `POST /usage/refresh` | Force refresh usage data |

## Quick Start

```yaml
services:
  ollama-usage-monitor:
    image: mrschnirschuh/ollama-usage-monitor:latest
    container_name: ollama-usage-monitor
    environment:
      - OLLAMA_COOKIE=your_ollama_com_cookie
      - OLLAMA_BASE_URL=http://host.docker.internal:11434
      - DEBOUNCE_SECONDS=30
      - LOG_LEVEL=INFO
    ports:
      - '9462:9462'
    restart: unless-stopped
    volumes:
      - ollama-usage-data:/data

volumes:
  ollama-usage-data:
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_COOKIE` | — | ollama.com session cookie (required) |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Upstream Ollama URL |
| `DEBOUNCE_SECONDS` | `30` | Min seconds between ollama.com fetches |
| `LOG_LEVEL` | `INFO` | Log level (DEBUG, INFO, WARNING, ERROR) |
| `PORT` | `9462` | Proxy listen port |
| `HISTORY_SIZE` | `20` | Number of snapshots to keep |
