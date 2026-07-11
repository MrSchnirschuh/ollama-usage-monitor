# Ollama Cloud Usage Monitor

A lightweight Docker container that acts as a **transparent proxy** for your local Ollama instance. After each API request, it fetches cloud usage data from ollama.com/settings and calculates a forecast: will your current session last until the reset?

**No polling** — usage is only fetched when you actually use Ollama.

## Features

- **Transparent Proxy** — All Ollama API requests pass through; cloud usage is fetched after each response
- **Burn-Rate Forecast** — Calculates from history whether the session limit will be reached before reset
- **Threshold Alerts** — Reports at 50%, 75%, 90%, 95% session usage (once per threshold)
- **Weekly Forecast** — Only active when you've used more than one session per week
- **Webhook Integration** — Can fire a webhook on thresholds or limit forecasts
- **History** — Stores usage history in /data (persistent volume)
- **Tier Detection** — Detects free/basic/pro/max and sets correct limits
- **Open WebUI ready** — Easy to integrate via HTTP request

## Quick Start

```bash
# 1. Get your cookie: Browser -> ollama.com/settings -> F12 -> Application -> Cookies -> __Secure-session

# 2. Start
docker run -d --name ollama-usage-monitor \
  -e OLLAMA_COOKIE="__Secure-session=YOUR_COOKIE" \
  -e OLLAMA_BASE_URL="http://localhost:11434" \
  -p 9462:9462 \
  -v ollama-usage-data:/data \
  mrschnirschuh/ollama-usage-monitor:latest

# 3. Test
curl http://localhost:9462/usage/summary
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_COOKIE` | — | **Required.** `__Secure-session` cookie from ollama.com |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | URL of your local Ollama instance |
| `DEBOUNCE_SECONDS` | `30` | Min seconds between usage updates after proxy requests |
| `WEBHOOK_URL` | — | Optional: URL for webhook notifications |
| `WEBHOOK_ALERTS` | `session_50,session_75,session_90,hit_limit` | Which events trigger a webhook |
| `HISTORY_PATH` | `/data/usage_history.json` | Path to history file |
| `LOG_LEVEL` | `INFO` | DEBUG, INFO, WARNING, ERROR |
| `PORT` | `9462` | Container port |
| `HOST` | `0.0.0.0` | Bind address |

## API Endpoints

### `GET /health`
Healthcheck. Returns status, last fetch time, and proxy target.

### `GET /usage`
Complete usage data + analysis + forecast.

### `GET /usage/raw`
Raw data only (no analysis).

### `GET /usage/summary`
Human-readable summary.

### `GET /usage/history`
Last 20 snapshots.

### `POST /usage/refresh`
Force an immediate refresh.

## TrueNAS Integration

### Installation as Custom App

1. **In TrueNAS: Apps -> Discover Apps -> Custom App**

2. **Container Image:**
   ```
   mrschnirschuh/ollama-usage-monitor:latest
   ```

3. **Port Forwarding:**
   | Container Port | Node Port |
   |---|---|
   | `9462` | `9462` (or any) |

4. **Environment Variables:**
   | Variable | Value |
   |---|---|
   | `OLLAMA_COOKIE` | `__Secure-session=your_cookie_value` |
   | `OLLAMA_BASE_URL` | `http://localhost:11434` |

5. **Persistent Volume:**
   - Mount `/data` to a host path or PVC (for usage history)

6. **Healthcheck** is built into the image (checks `/health` every 30s)

### After Starting

Your Ollama URL changes from `http://<your-ollama-ip>:11434` to `http://<truenas-ip>:9462`.

**In Open WebUI** set `http://<truenas-ip>:9462` as Ollama Base URL.
**In Hermes** run `hermes config set provider.ollama.base_url http://<truenas-ip>:9462`.

The container forwards everything to your real Ollama — you won't notice any difference, except that `/usage` and `/usage/summary` now deliver live cloud data.

### Updating the Cookie

When the cookie expires (after a few hours/days):

1. Get a fresh cookie from your browser (ollama.com/settings -> F12 -> Cookies)
2. In TrueNAS: App -> Edit -> Environment -> Update `OLLAMA_COOKIE`
3. Restart the app

## Docker Compose

```yaml
version: "3.8"
services:
  ollama-usage-monitor:
    image: mrschnirschuh/ollama-usage-monitor:latest
    container_name: ollama-usage-monitor
    restart: unless-stopped
    ports:
      - "9462:9462"
    environment:
      - OLLAMA_COOKIE=${OLLAMA_COOKIE:?required}
      - OLLAMA_BASE_URL=http://localhost:11434
      - DEBOUNCE_SECONDS=30
      - LOG_LEVEL=INFO
    volumes:
      - ollama-usage-data:/data

volumes:
  ollama-usage-data:
```

## Open WebUI Integration

As a custom tool:

```python
def get_usage():
    import requests
    resp = requests.get("http://ollama-usage-monitor:9462/usage/summary")
    return resp.json()["summary"]
```

Or as a Function in Open WebUI:
1. Create a function that calls `GET /usage`
2. Output as context prompt in the chat message
3. Suggest model switching when needed

## Building

```bash
git clone https://github.com/MrSchnirschuh/ollama-usage-monitor
cd ollama-usage-monitor
docker build -t ollama-usage-monitor:latest .
```

## Docker Hub

Image: `mrschnirschuh/ollama-usage-monitor:latest`
