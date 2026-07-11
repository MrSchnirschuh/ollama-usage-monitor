# Ollama Cloud Usage Monitor

Ein leichter Docker-Container, der den Ollama Cloud Usage von ollama.com/settings überwacht und vorhersagt, ob das Session-Limit vor dem Reset erreicht wird.

## Features

- **HTTP API** — `/usage`, `/usage/summary`, `/usage/history`, `/health`
- **Smart Prediction** — Berechnet Burn-Rate aus History und sagt voraus, ob Session-Limit vor Reset erreicht wird
- **Schwellen-Alarme** — Meldet bei 50%, 75%, 90% Session-Auslastung
- **Webhook-Integration** — Kann bei Schwellen oder Limit-Prognose einen Webhook feuern
- **Open WebUI-ready** — Einfach als Custom Tool oder per HTTP-Request einbindbar
- **History** — Speichert Nutzungsverlauf in /data (persistentes Volume)
- **Tier-Erkennung** — Erkennt free/basic/pro/max und setzt korrekte Limits

## Quick Start

```bash
# 1. Cookie holen: Browser → ollama.com/settings → DevTools → Application → Cookies → __Secure-session

# 2. Starten
docker run -d --name ollama-usage-monitor \
  -e OLLAMA_COOKIE="dein__secure-session_cookie" \
  -e OLLAMA_BASE_URL="http://192.168.4.9:30068" \
  -p 9462:9462 \
  -v ollama-usage-data:/data \
  mrschnirschuh/ollama-usage-monitor:latest

# 3. Testen
curl http://localhost:9462/usage/summary
```

## Docker Compose

```yaml
version: "3.8"
services:
  ollama-usage-monitor:
    build: .
    container_name: ollama-usage-monitor
    restart: unless-stopped
    ports:
      - "9462:9462"
    environment:
      - OLLAMA_COOKIE=${OLLAMA_COOKIE:?required}
      - REFRESH_INTERVAL=300
      - LOG_LEVEL=INFO
    volumes:
      - ollama-usage-data:/data

volumes:
  ollama-usage-data:
```

## Environment Variablen

| Variable | Default | Beschreibung |
|----------|---------|--------------|
| `OLLAMA_COOKIE` | — | (required) `__Secure-session` Cookie von ollama.com |
| `OLLAMA_BASE_URL` | `http://192.168.4.9:30068` | URL deiner lokalen Ollama-Instanz |
| `DEBOUNCE_SECONDS` | `30` | Min Sekunden zwischen Usage-Updates nach Proxy-Requests |
| `WEBHOOK_URL` | — | Optional: URL für Webhook-Benachrichtigungen |
| `WEBHOOK_ALERTS` | `session_50,session_75,session_90,hit_limit` | Welche Events einen Webhook auslösen |
| `HISTORY_PATH` | `/data/usage_history.json` | Pfad zur History-Datei |
| `LOG_LEVEL` | `INFO` | DEBUG, INFO, WARNING, ERROR |
| `PORT` | `9462` | Container-Port |
| `HOST` | `0.0.0.0` | Bind-Addresse |

## API Endpoints

### `GET /health`
Healthcheck (Status "ok" oder "no_cookie").

### `GET /usage`
Vollständige Usage-Daten + Analyse.

### `GET /usage/raw`
Nur die Rohdaten (ohne Analyse).

### `GET /usage/summary`
Menschlich lesbare Zusammenfassung.

### `GET /usage/history`
Letzte 20 Snapshots.

### `POST /usage/refresh`
Erzwingt ein sofortiges Refresh.

## Webhook-Format

```json
{
  "events": ["session_75", "hit_limit"],
  "analysis": { ... },
  "ts": "2026-07-11T14:30:00Z"
}
```

## Open WebUI Integration

Als Custom Tool:

```python
def get_usage():
    import requests
    resp = requests.get("http://ollama-usage-monitor:9462/usage/summary")
    return resp.json()["summary"]
```

Oder als Function in Open WebUI:
1. Function anlegen, die `GET /usage` aufruft
2. Ausgabe als Context-Prompt in die Chat-Nachricht einfügen
3. Bei Bedarf Modell-Wechsel vorschlagen

## Bauen

```bash
git clone <repo>
cd ollama-usage-monitor
docker build -t ollama-usage-monitor:latest .
```

## TrueNAS Integration

1. Image auf TrueNAS laden oder von Registry pullen
2. Custom App mit:
   - Image: `ollama-usage-monitor:latest`
   - Port: `9462:9462`
   - Env: `OLLAMA_COOKIE`
   - Volume: `/data` persistent
3. Starten und unter `http://truenas-ip:9462/usage` abfragen