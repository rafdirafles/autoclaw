# AutoClaw

OpenAI-compatible LLM proxy using Z.ai/AutoClaw API tokens. Auto-registers accounts via Google OAuth, auto-refreshes tokens infinitely (2-layer system), and provides a beautiful web dashboard.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Add emails to email.txt (format: email:password per line)

# 3. Register accounts
python register.py --count 5

# 4. Start router + dashboard
python runner.py

# 5. Open dashboard
# http://localhost:31001
```

## Architecture

```
autoclaw/
├── router.py        # Port 31000 — OpenAI-compatible proxy + auto-refresh daemon
├── dashboard.py     # Port 31001 — Web UI (FastAPI + modern dark theme)
├── register.py      # Account registration (CloakBrowser stealth, headless)
├── refresh.py       # 2-layer auto-refresh module
├── runner.py        # Service launcher with auto-restart
├── config.json      # Configuration
├── accounts.json    # Account data (email, tokens, balance, device_id)
├── tokens.txt       # Access tokens (one per line, "Bearer " prefix)
├── email.txt        # Email list (email:password format)
└── requirements.txt
```

## Components

### Router (`router.py`)
- OpenAI-compatible API proxy on `localhost:31000`
- Endpoints: `GET /v1/models`, `POST /v1/chat/completions`, `GET /health`
- Round-robin token rotation across all accounts
- Built-in auto-refresh daemon thread (starts on boot)
- On-demand single-token refresh on 401 errors
- Manual refresh: `POST /refresh-now`
- Refresh status: `GET /refresh-status`

### Auto-Refresh (`refresh.py`)
Two-layer infinite token refresh:

**Layer 1 — Access Token Refresh (every 20 hours)**
```
POST /userapi/v1/refresh
Payload: {"source_id":"autoclaw","device_id":"<did>","refresh_token":"<current_refresh_token>"}
→ Returns new access_token, same refresh_token
```

**Layer 2 — Refresh Token Rotation (every 25 days)**
```
POST /userapi/v1/refresh
Payload: {"source_id":"autoclaw","device_id":"<did>","refresh_token":"<current_access_token>"}
→ Returns new access_token + new rotated refresh_token (refresh: true)
→ 30-day expiry reset — infinite chain, never needs Google re-login
```

### Dashboard (`dashboard.py`)
- Modern dark theme web UI on `localhost:31001`
- **Token Health**: Per-account cards with expiry countdowns, refresh status, manual refresh button
- **Models**: Available models with cost/context info
- **Register**: Trigger batch registration
- **Test API**: Send test chat completions
- **Balance**: Check wallet balances for all accounts
- **Accounts**: Sortable account table
- **Emails**: Manage email list
- **Settings**: Edit config.json

### Register (`register.py`)
- Google OAuth registration via CloakBrowser (stealth, headless)
- Concurrent batch registration (default 5 at a time)
- Supports per-email passwords (email:password format)
- New accounts get ~2000 bonus points
- No interactive menu — pure batch mode

### Runner (`runner.py`)
- Starts router + dashboard with auto-restart on crash
- Foreground mode (see logs) or daemon mode (background, no window)
- Commands: `--daemon`, `--stop`, `--status`

## Models

| Model | Upstream | Cost | Notes |
|-------|----------|------|-------|
| `glm-5.2` | `openrouter_glm-5.2` | ~3 pts | Best quality |
| `glm-5-turbo` | `zai_glm-5-turbo` | 1 pt | Cheapest |
| `deepseek-v4` | `zai_auto` | ~7 pts | Most expensive |

## Usage Examples

### Using the proxy (OpenAI-compatible)
```python
import openai

client = openai.OpenAI(
    base_url="http://localhost:31000/v1",
    api_key="sk-autoclaw-router"
)

response = client.chat.completions.create(
    model="glm-5.2",
    messages=[{"role": "user", "content": "Hello!"}]
)
print(response.choices[0].message.content)
```

### Manual token refresh
```bash
python refresh.py --once
```

### Register accounts
```bash
python register.py --count 10
python register.py              # all emails in email.txt
python register.py --no-headless  # show browser
```

### Run as background daemon
```bash
pythonw runner.py --daemon
python runner.py --status
python runner.py --stop
```

## Configuration (`config.json`)
```json
{
    "password": "",
    "batch_size": 5,
    "email_file": "email.txt",
    "accounts_file": "accounts.json",
    "tokens_file": "tokens.txt"
}
```

## File Formats

**accounts.json**: Array of account objects:
```json
{
    "email": "user@gmail.com",
    "device_id": "uuid",
    "access_token": "Bearer eyJ...",
    "refresh_token": "Bearer eyJ...",
    "balance": 2300,
    "registered_at": "2026-07-01 12:00:00",
    "last_at_refresh": "2026-07-01 12:00:00",
    "last_rt_rotation": "2026-07-01 12:00:00"
}
```

**tokens.txt**: One access token per line (with `Bearer ` prefix)

**email.txt**: One email per line, format `email:password` or just `email`

## Requirements
- Python 3.13+
- Windows (git-bash) — uses `requests` not `aiohttp`
- CloakBrowser v0.4.5+ (binary at `~/.cloakbrowser/`)
