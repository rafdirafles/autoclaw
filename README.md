# AutoClaw

OpenAI-compatible LLM proxy for [AutoClaw (Z.ai/Zhipu)](https://autoclaw.z.ai) API. Auto-registers accounts via Google OAuth, auto-refreshes tokens infinitely (unified L1+L2 cycle), and provides a beautiful web dashboard.

## Quick Start

### Windows (recommended)

```bash
# 1. Clone
git clone https://github.com/rafdirafles/autoclaw.git
cd autoclaw

# 2. Run setup
setup.bat

# 3. Add your emails to email.txt (format: email:password per line)

# 4. Register accounts
python register.py --count 5

# 5. Start services
start.bat
# Select [1] for background mode

# 6. Open dashboard
# http://localhost:31001
```

### Manual (any OS)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create config.json
echo {"password":"","batch_size":5,"email_file":"email.txt","accounts_file":"accounts.json","tokens_file":"tokens.txt"} > config.json

# 3. Set API key
export AUTOCLAW_API_KEY="your-secret-key"

# 4. Add emails to email.txt (format: email:password per line)

# 5. Register accounts
python register.py --count 5

# 6. Start router + dashboard
python runner.py

# 7. Open dashboard
# http://localhost:31001
```

## Architecture

```
autoclaw/
├── setup.bat         # First-time install (deps + config + API key)
├── start.bat         # Service manager menu (start/stop/status/register/refresh)
├── router.py         # Port 31000 — OpenAI-compatible proxy + auto-refresh daemon
├── dashboard.py      # Port 31001 — Web UI (FastAPI, dark theme)
├── register.py       # Account registration (CloakBrowser stealth, headless)
├── refresh.py        # Unified auto-refresh module (L1 + L2)
├── runner.py         # Service launcher with auto-restart
├── config.json       # Configuration (auto-created by setup.bat)
├── accounts.json     # Account data (auto-created, gitignored)
├── tokens.txt        # Access tokens (auto-created, gitignored)
├── email.txt         # Email list (user-provided, gitignored)
├── requirements.txt
└── .gitignore
```

## Components

### Router (`router.py`) — Port 31000

OpenAI-compatible API proxy with round-robin token rotation.

**Endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/models` | List available models |
| `POST` | `/v1/chat/completions` | Chat completion (stream + non-stream) |
| `GET` | `/health` | Health check + token stats |
| `GET` | `/refresh-status` | Auto-refresh thread status |
| `POST` | `/refresh-now` | Trigger manual refresh (non-blocking) |

**Features:**
- Round-robin token rotation across all accounts
- Auto-refresh daemon thread (starts on boot, runs every 20h)
- On-demand single-token refresh on 401 errors
- Auto-reload tokens after each refresh cycle (`on_cycle_complete` callback)
- 402 (points empty) → skip to next token automatically

### Auto-Refresh (`refresh.py`)

Unified cycle — **laptop-off-safe**:

**Layer 1 — Access Token Refresh (every 20h, always runs)**
```
POST /userapi/v1/refresh
Payload: {"source_id":"autoclaw","device_id":"<did>","refresh_token":"<current_refresh_token>"}
→ Returns new access_token, same refresh_token
```

**Layer 2 — Refresh Token Rotation (if RT expiry < 5 days)**
```
POST /userapi/v1/refresh
Payload: {"source_id":"autoclaw","device_id":"<did>","refresh_token":"<fresh_access_token>"}
→ Returns new access_token + new rotated refresh_token (refresh: true)
→ 30-day expiry reset — infinite chain, never needs Google re-login
```

**Why it's laptop-off-safe:**
- On startup: refresh cycle runs immediately (handles any downtime)
- L2 checks **actual JWT expiry** (not a day counter) — rotates exactly when needed
- Laptop off for days? No problem — next startup refreshes everything

### Dashboard (`dashboard.py`) — Port 31001

Modern dark theme web UI:

- **Overview**: Router status, token count, refresh status
- **Token Health**: Per-account cards with expiry countdowns, manual refresh button
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
- Supports per-email passwords (`email:password` format in email.txt)
- New accounts get ~2000 bonus points

### Runner (`runner.py`)

Starts router + dashboard with auto-restart on crash.

```bash
python runner.py              # foreground (see logs)
pythonw runner.py --daemon    # background (no window)
python runner.py --stop       # stop all services
python runner.py --status     # show status
```

## Models

| Model | Upstream | Cost | Notes |
|-------|----------|------|-------|
| `glm-5.2` | `openrouter_glm-5.2` | ~3 pts | Best quality |
| `glm-5-turbo` | `zai_glm-5-turbo` | 1 pt | Cheapest |
| `deepseek-v4` | `zai_auto` | ~7 pts | Most expensive |

## Usage

### Using the proxy (OpenAI-compatible)

```python
import openai

client = openai.OpenAI(
    base_url="http://localhost:31000/v1",
    api_key=os.environ.get("AUTOCLAW_API_KEY", "sk-change-me")
)

response = client.chat.completions.create(
    model="glm-5.2",
    messages=[{"role": "user", "content": "Hello!"}]
)
print(response.choices[0].message.content)
```

### Using with curl

```bash
curl http://localhost:31000/v1/chat/completions \
  -H "Authorization: Bearer $AUTOCLAW_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.2",
    "messages": [{"role": "user", "content": "Say OK"}],
    "max_tokens": 10
  }'
```

### Register accounts

```bash
python register.py --count 10     # register 10 accounts
python register.py                # register all emails in email.txt
python register.py --no-headless  # show browser (debug)
```

### Manual token refresh

```bash
# Via CLI
python refresh.py --once

# Via API (non-blocking)
curl -X POST http://localhost:31000/refresh-now

# Via dashboard
# Click "Refresh All Now" button
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTOCLAW_API_KEY` | `sk-change-me` | API key for router authentication |

Set it via `setup.bat` (Windows) or `export AUTOCLAW_API_KEY="your-key"` (Linux/Mac).

### config.json (auto-created)

```json
{
    "password": "",
    "batch_size": 5,
    "email_file": "email.txt",
    "accounts_file": "accounts.json",
    "tokens_file": "tokens.txt"
}
```

- `password`: Google account password (shared, for registration)
- `batch_size`: Concurrent registration count
- Per-email passwords: use `email:password` format in email.txt

## File Formats

**email.txt** — one per line:
```
user1@gmail.com:password123
user2@gmail.com
```

**accounts.json** — auto-generated, gitignored:
```json
[{
    "email": "user@gmail.com",
    "device_id": "uuid",
    "access_token": "Bearer eyJ...",
    "refresh_token": "Bearer eyJ...",
    "balance": 2300,
    "registered_at": "2026-07-01 12:00:00",
    "last_at_refresh": "2026-07-01 12:00:00",
    "last_rt_rotation": "2026-07-01 12:00:00"
}]
```

## Requirements

- Python 3.13+
- CloakBrowser v0.4.5+ (for account registration only)
- Windows recommended (git-bash). Linux/Mac works for router + dashboard.

## Security Notes

- `accounts.json`, `tokens.txt`, `email.txt`, `config.json` are gitignored
- Router API key uses environment variable (`AUTOCLAW_API_KEY`)
- Never commit your tokens or passwords

## License

MIT
