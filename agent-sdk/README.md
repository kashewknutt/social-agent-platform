# agent-sdk

Shared runtime and HTTP control contract for social observation bots (Fleet Control plugins).

## Install

```powershell
git clone https://github.com/kashewknutt/agent-sdk.git
cd agent-sdk
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

Used by [social-agent-platform](https://github.com/kashewknutt/social-agent-platform) and bot plugins such as [instagram-bot](https://github.com/kashewknutt/instagram-bot).

## Control API (every bot)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness |
| GET | `/status` | Lifecycle, step, usage, artifacts |
| POST | `/run` | `{ "mode": "once" \| "daemon" }` |
| POST | `/pause` `/resume` `/stop` | Lifecycle |
| GET/PUT | `/direction` | Goals, hashtags, pillars |

## Package layout

- `agent_sdk.models` — status / direction / event schemas
- `agent_sdk.control` — `BotController` pause/stop/status runtime
- `agent_sdk.events` — JSONL run event stream
- `agent_sdk.api` — FastAPI router factory
- `agent_sdk.safety` — session caps and humanized delays
- `agent_sdk.llm` — Moonshot/Kimi client (fixed temperature=1)
