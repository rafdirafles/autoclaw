#!/usr/bin/env python3
"""
AutoClaw Dashboard — Web UI for managing AutoClaw proxy.

Runs on localhost:31001. Provides browser-based access to:
- Token Health monitoring (refresh status, expiry countdowns)
- Models overview
- Register accounts
- Test API
- Check balance
- View/edit accounts
- Manage emails
- Settings

Usage:
  python dashboard.py
  python dashboard.py --port 31001
"""

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn
import requests

# Fix Windows cp1252 charmap codec error
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

_DIR = Path(__file__).parent
CONFIG_FILE = _DIR / "config.json"
ACCOUNTS_FILE = _DIR / "accounts.json"
TOKENS_FILE = _DIR / "tokens.txt"
EMAIL_FILE = _DIR / "email.txt"
REGISTER_SCRIPT = _DIR / "register.py"

ROUTER_HOST = "localhost"
ROUTER_PORT = 31000

# --- File cache (avoid re-reading 130KB JSON on every request) ---
_accounts_cache = {"mtime": 0, "data": None}
_emails_cache = {"mtime": 0, "data": None}

APP_ID = "100003"
APP_KEY = "38d2391985e2369a5fb8227d8e6cd5e5"
BASE_URL = "https://autoglm-api.autoglm.ai"
PROXY_URL = f"{BASE_URL}/autoclaw-proxy/proxy/autoclaw/chat/completions"

MODELS = {
    "1": {"id": "openrouter_glm-5.2", "name": "GLM-5.2", "cost": "~3 pts", "context": "128K", "tier": "Best"},
    "2": {"id": "zai_glm-5-turbo", "name": "GLM-5-Turbo", "cost": "1 pt", "context": "128K", "tier": "Cheapest"},
    "3": {"id": "zai_auto", "name": "DeepSeek-V4", "cost": "~7 pts", "context": "128K", "tier": "Expensive"},
}


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def load_config():
    defaults = {
        "password": "",
        "batch_size": 5,
        "email_file": "email.txt",
        "accounts_file": "accounts.json",
        "tokens_file": "tokens.txt",
    }
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            defaults.update(cfg)
        except Exception:
            pass
    return defaults


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def load_accounts():
    """Load accounts with mtime-based file cache. Returns list copy."""
    try:
        mtime = ACCOUNTS_FILE.stat().st_mtime
        if _accounts_cache["mtime"] != mtime or _accounts_cache["data"] is None:
            raw = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
            _accounts_cache["mtime"] = mtime
            _accounts_cache["data"] = raw
        return _accounts_cache["data"]  # direct ref (read-only callers)
    except Exception:
        return []


def save_accounts(accounts):
    ACCOUNTS_FILE.write_text(json.dumps(accounts, indent=2, ensure_ascii=False), encoding="utf-8")
    tokens = [acc.get("access_token", "") for acc in accounts if acc.get("access_token")]
    TOKENS_FILE.write_text("\n".join(tokens) + "\n" if tokens else "", encoding="utf-8")
    # Invalidate cache
    _accounts_cache["mtime"] = 0
    _accounts_cache["data"] = None


def load_emails():
    """Load emails with mtime-based file cache."""
    try:
        mtime = EMAIL_FILE.stat().st_mtime
        if _emails_cache["mtime"] != mtime or _emails_cache["data"] is None:
            raw = [l.strip() for l in EMAIL_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
            _emails_cache["mtime"] = mtime
            _emails_cache["data"] = raw
        return _emails_cache["data"]
    except Exception:
        return []


def generate_sign(timestamp):
    raw = f"{APP_ID}&{timestamp}&{APP_KEY}"
    return hashlib.md5(raw.encode()).hexdigest()


def get_auth_headers():
    ts = str(int(time.time()))
    return {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://autoclaw.z.ai",
        "referer": "https://autoclaw.z.ai/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "x-auth-appid": APP_ID,
        "x-auth-timestamp": ts,
        "x-auth-sign": generate_sign(ts),
        "x-product": "autoclaw",
        "x-version": "1.10.0",
        "x-tm": "web",
        "x-channel": "official",
        "x-client-type": "web",
        "x-trace-id": str(uuid.uuid4()),
        "x-lang": "zh-CN",
    }


def get_wallet_balance(token):
    try:
        if token.startswith("Bearer "):
            token = token[7:]
        url = f"{BASE_URL}/agent-assetmgr/api/v2/wallets?biz_app_id=autoclaw"
        headers = get_auth_headers()
        headers["authorization"] = f"Bearer {token}"
        resp = requests.get(url, headers=headers, timeout=15)
        data = resp.json()
        return data.get("data", {}).get("total_balance", "N/A")
    except Exception:
        return "N/A"


def test_chat(token, model_id, prompt="Hello! What model are you? Reply in 1 sentence."):
    try:
        if token.startswith("Bearer "):
            token = token[7:]
        headers = get_auth_headers()
        headers["x-authorization"] = f"Bearer {token}"
        headers["x-request-model"] = model_id
        headers["x-request-id"] = str(uuid.uuid4())
        body = {
            "model": "x",
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }
        resp = requests.post(PROXY_URL, json=body, headers=headers, timeout=60, stream=True)
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}: {resp.text[:300]}"
        full_content = ""
        for line in resp.iter_lines(decode_unicode=True):
            if line and line.startswith("data:"):
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    choices = chunk.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        if "content" in delta:
                            full_content += delta["content"]
                except json.JSONDecodeError:
                    continue
        return full_content or "(empty response)", None
    except Exception as e:
        return None, str(e)


async def get_router_status():
    """Check if router is running and get its health (non-blocking)."""
    def _fetch():
        try:
            resp = requests.get(f"http://{ROUTER_HOST}:{ROUTER_PORT}/health", timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                uptime = data.get("uptime_seconds", 0)
                hours = uptime // 3600
                minutes = (uptime % 3600) // 60
                return {
                    "running": True,
                    "port": ROUTER_PORT,
                    "uptime": f"{hours}h {minutes}m",
                    "tokens": data.get("tokens", {}),
                    "models": data.get("models", []),
                }
        except Exception:
            pass
        return {"running": False, "port": ROUTER_PORT, "uptime": "—", "tokens": {}, "models": []}
    return await asyncio.to_thread(_fetch)


async def get_refresh_status():
    """Get auto-refresh status from router (non-blocking)."""
    def _fetch():
        try:
            resp = requests.get(f"http://{ROUTER_HOST}:{ROUTER_PORT}/refresh-status", timeout=3)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {"running": False}
    return await asyncio.to_thread(_fetch)


# ═══════════════════════════════════════════════════════════════
# FASTAPI APP
# ═══════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app):
    print(f"[Dashboard] Ready at http://localhost:31001")
    yield

app = FastAPI(title="AutoClaw Dashboard", lifespan=lifespan)


# ---- API Routes ----

@app.get("/api/config")
async def api_get_config():
    return load_config()


@app.post("/api/config")
async def api_save_config(request: Request):
    data = await request.json()
    cfg = load_config()
    cfg.update(data)
    save_config(cfg)
    return {"ok": True}


@app.get("/api/accounts")
async def api_get_accounts():
    accounts = load_accounts()
    return {"accounts": accounts, "total": len(accounts)}


@app.get("/api/emails")
async def api_get_emails():
    emails = load_emails()
    parsed = []
    for line in emails:
        if ":" in line:
            parts = line.split(":", 1)
            parsed.append({"email": parts[0].strip(), "has_password": bool(parts[1].strip())})
        else:
            parsed.append({"email": line, "has_password": False})
    return {"emails": parsed, "total": len(parsed)}


@app.post("/api/emails/upload")
async def api_upload_emails(file: UploadFile = File(...)):
    content = await file.read()
    text = content.decode("utf-8")
    EMAIL_FILE.write_text(text, encoding="utf-8")
    emails = [l.strip() for l in text.splitlines() if l.strip()]
    return {"ok": True, "total": len(emails)}


@app.post("/api/emails/add")
async def api_add_email(request: Request):
    data = await request.json()
    entry = data.get("email", "").strip()
    if not entry:
        return {"ok": False, "error": "Email required"}
    emails = load_emails()
    email_part = entry.split(":", 1)[0].strip() if ":" in entry else entry
    existing_emails = [e.split(":", 1)[0].strip() if ":" in e else e for e in emails]
    if email_part not in existing_emails:
        emails.append(entry)
        EMAIL_FILE.write_text("\n".join(emails) + "\n", encoding="utf-8")
    return {"ok": True, "total": len(emails)}


@app.post("/api/emails/delete")
async def api_delete_email(request: Request):
    data = await request.json()
    email = data.get("email", "").strip()
    emails = load_emails()
    email_part = email.split(":", 1)[0].strip() if ":" in email else email
    new_emails = []
    for e in emails:
        e_part = e.split(":", 1)[0].strip() if ":" in e else e
        if e_part != email_part:
            new_emails.append(e)
    EMAIL_FILE.write_text("\n".join(new_emails) + "\n" if new_emails else "", encoding="utf-8")
    return {"ok": True, "total": len(new_emails)}


@app.post("/api/balance")
async def api_check_balance():
    accounts = load_accounts()
    sem = asyncio.Semaphore(5)  # max 5 concurrent balance checks

    async def _check(acc):
        token = acc.get("access_token", "")
        async with sem:
            balance = await asyncio.to_thread(get_wallet_balance, token)
        acc["balance"] = balance
        return {"email": acc.get("email", "N/A"), "balance": balance}

    results = await asyncio.gather(*[_check(acc) for acc in accounts])
    save_accounts(accounts)
    total = sum(r["balance"] for r in results if isinstance(r["balance"], (int, float)))
    return {"results": results, "total_points": total, "total_accounts": len(accounts)}


@app.post("/api/test")
async def api_test_chat(request: Request):
    data = await request.json()
    token = data.get("token", "")
    model_key = data.get("model", "1")
    prompt = data.get("prompt", "Hello! What model are you? Reply in 1 sentence.")
    model_info = MODELS.get(model_key, MODELS["1"])
    model_id = model_info["id"]
    result, error = await asyncio.to_thread(test_chat, token, model_id, prompt)
    if error:
        return {"ok": False, "error": error, "model": model_info["name"]}
    return {"ok": True, "response": result, "model": model_info["name"]}


@app.get("/api/tokens")
async def api_get_tokens():
    accounts = load_accounts()
    tokens = [acc.get("access_token", "") for acc in accounts if acc.get("access_token")]
    return {"tokens": tokens, "total": len(tokens)}


@app.get("/api/stats")
async def api_stats():
    accounts = load_accounts()
    emails = load_emails()
    total_balance = sum(acc.get("balance", 0) for acc in accounts if isinstance(acc.get("balance"), (int, float)))
    active = sum(1 for acc in accounts if acc.get("access_token"))
    router, refresh = await asyncio.gather(get_router_status(), get_refresh_status())
    return {
        "accounts": len(accounts),
        "active_tokens": active,
        "emails": len(emails),
        "total_points": total_balance,
        "router": router,
        "refresh": refresh,
    }


@app.get("/api/router-status")
async def api_router_status():
    return await get_router_status()


@app.get("/api/refresh-status")
async def api_refresh_status():
    return await get_refresh_status()


@app.post("/api/refresh-now")
async def api_refresh_now():
    """Trigger manual refresh via router (non-blocking)."""
    def _do():
        try:
            resp = requests.post(f"http://{ROUTER_HOST}:{ROUTER_PORT}/refresh-now", timeout=120)
            if resp.status_code == 200:
                return resp.json()
            return {"ok": False, "error": f"Router returned {resp.status_code}"}
        except requests.exceptions.ConnectionError:
            return {"ok": False, "error": "Router not running. Start it first."}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    return await asyncio.to_thread(_do)


@app.post("/api/register")
async def api_register(request: Request):
    data = await request.json()
    count = data.get("count", 1)
    password = data.get("password", "")
    if password:
        cfg = load_config()
        cfg["password"] = password
        save_config(cfg)
    return {
        "ok": True,
        "message": (
            f"Registration needs browser interaction (Google OAuth).\n"
            f"To register {count} account(s), run in terminal:\n\n"
            f"cd {_DIR}\n"
            f"python register.py --count {count}\n\n"
            f"Password is pre-saved in config.json."
        ),
    }


# ---- Main HTML page ----

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTML_PAGE


# ═══════════════════════════════════════════════════════════════
# HTML FRONTEND — Modern Dark Theme Dashboard
# ═══════════════════════════════════════════════════════════════

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>⚡ AutoClaw Dashboard</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

:root {
    --bg: #0d1117;
    --bg-elev: #161b22;
    --bg-elev2: #21262d;
    --border: #30363d;
    --border-light: #484f58;
    --text: #e6edf3;
    --text-muted: #8b949e;
    --text-dim: #6e7681;
    --accent: #58a6ff;
    --accent-hover: #79b8ff;
    --green: #3fb950;
    --green-bg: #1a4731;
    --red: #f85149;
    --red-bg: #4a1e1e;
    --yellow: #d29922;
    --yellow-bg: #4a3a1e;
    --purple: #d2a8ff;
    --purple-bg: #2d1b4e;
    --blue: #1f6feb;
    --shadow: 0 3px 12px rgba(0,0,0,0.4);
    --shadow-sm: 0 1px 4px rgba(0,0,0,0.3);
    --radius: 12px;
    --radius-sm: 8px;
    --transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
    font-family: 'Inter', system-ui, -apple-system, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    line-height: 1.5;
}

/* ── Header ── */
.header {
    background: linear-gradient(135deg, #161b22 0%, #0d1117 100%);
    padding: 16px 28px;
    border-bottom: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 12px;
    position: sticky;
    top: 0;
    z-index: 100;
    backdrop-filter: blur(12px);
}
.header-left { display: flex; align-items: center; gap: 16px; }
.header h1 { font-size: 1.35rem; font-weight: 700; color: var(--accent); letter-spacing: -0.5px; }

.header-stats { display: flex; gap: 12px; flex-wrap: wrap; }
.stat-card {
    background: var(--bg-elev2);
    padding: 8px 18px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--border);
    text-align: center;
    min-width: 70px;
    transition: var(--transition);
}
.stat-card:hover { border-color: var(--border-light); transform: translateY(-1px); }
.stat-card .val { font-size: 1.3rem; font-weight: 700; color: var(--green); }
.stat-card .label { font-size: 0.7rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }

/* ── Router Status Widget ── */
.router-widget {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 14px;
    border-radius: 20px;
    font-size: 0.8rem;
    font-weight: 500;
    transition: var(--transition);
}
.router-widget.online {
    background: var(--green-bg);
    color: var(--green);
    border: 1px solid rgba(63, 185, 80, 0.3);
}
.router-widget.offline {
    background: var(--red-bg);
    color: var(--red);
    border: 1px solid rgba(248, 81, 73, 0.3);
}
.router-widget .dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: currentColor;
    animation: pulse 2s ease-in-out infinite;
}
@keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
}

/* ── Navigation ── */
.nav {
    display: flex;
    gap: 4px;
    padding: 12px 28px;
    background: var(--bg-elev);
    border-bottom: 1px solid var(--border);
    overflow-x: auto;
    scrollbar-width: thin;
}
.nav button {
    background: transparent;
    color: var(--text-muted);
    border: 1px solid transparent;
    padding: 9px 18px;
    border-radius: var(--radius-sm);
    cursor: pointer;
    font-size: 0.85rem;
    font-weight: 500;
    transition: var(--transition);
    white-space: nowrap;
    font-family: inherit;
}
.nav button:hover { background: var(--bg-elev2); color: var(--text); }
.nav button.active {
    background: var(--blue);
    color: #fff;
    border-color: var(--blue);
    box-shadow: 0 2px 8px rgba(31, 111, 235, 0.3);
}

/* ── Content ── */
.content { padding: 28px; max-width: 1300px; margin: 0 auto; }
.panel { display: none; animation: fadeIn 0.3s ease; }
.panel.active { display: block; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }

/* ── Cards ── */
.card {
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 24px;
    margin-bottom: 20px;
    box-shadow: var(--shadow-sm);
    transition: var(--transition);
}
.card:hover { box-shadow: var(--shadow); }
.card h2 {
    color: var(--accent);
    margin-bottom: 16px;
    font-size: 1.1rem;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 8px;
}

/* ── Buttons ── */
.btn {
    background: #238636;
    color: #fff;
    border: none;
    padding: 10px 20px;
    border-radius: var(--radius-sm);
    cursor: pointer;
    font-size: 0.85rem;
    font-weight: 500;
    transition: var(--transition);
    font-family: inherit;
    display: inline-flex;
    align-items: center;
    gap: 6px;
}
.btn:hover { background: #2ea043; transform: translateY(-1px); box-shadow: var(--shadow-sm); }
.btn:active { transform: translateY(0); }
.btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
.btn-blue { background: var(--blue); }
.btn-blue:hover { background: #388bfd; }
.btn-red { background: #da3633; }
.btn-red:hover { background: var(--red); }
.btn-purple { background: #6e40c9; }
.btn-purple:hover { background: #8957e5; }
.btn-sm { padding: 6px 12px; font-size: 0.78rem; }

/* ── Forms ── */
input, textarea, select {
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    padding: 10px 14px;
    border-radius: var(--radius-sm);
    font-size: 0.88rem;
    width: 100%;
    font-family: inherit;
    transition: var(--transition);
}
input:focus, textarea:focus, select:focus {
    outline: none;
    border-color: var(--accent);
    box-shadow: 0 0 0 3px rgba(88, 166, 255, 0.15);
}
label { display: block; margin-bottom: 6px; color: var(--text-muted); font-size: 0.8rem; font-weight: 500; }
.form-group { margin-bottom: 16px; }

/* ── Tables ── */
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 12px 14px; border-bottom: 1px solid var(--border); }
th {
    color: var(--text-muted);
    font-size: 0.72rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    cursor: pointer;
    user-select: none;
    transition: var(--transition);
}
th:hover { color: var(--accent); }
th.sort-asc::after { content: ' ↑'; color: var(--accent); }
th.sort-desc::after { content: ' ↓'; color: var(--accent); }
td { font-size: 0.88rem; }
tr:hover { background: rgba(255,255,255,0.02); }

/* ── Badges ── */
.badge {
    padding: 3px 10px;
    border-radius: 20px;
    font-size: 0.72rem;
    font-weight: 600;
    display: inline-block;
}
.badge-green { background: var(--green-bg); color: var(--green); }
.badge-red { background: var(--red-bg); color: var(--red); }
.badge-yellow { background: var(--yellow-bg); color: var(--yellow); }
.badge-blue { background: #0c2d6b; color: var(--accent); }
.badge-purple { background: var(--purple-bg); color: var(--purple); }

/* ── Alerts ── */
.alert {
    padding: 12px 16px;
    border-radius: var(--radius-sm);
    margin-bottom: 16px;
    font-size: 0.85rem;
    border: 1px solid;
}
.alert-info { background: rgba(88, 166, 255, 0.08); color: var(--accent); border-color: rgba(88, 166, 255, 0.2); }
.alert-success { background: rgba(63, 185, 80, 0.08); color: var(--green); border-color: rgba(63, 185, 80, 0.2); }
.alert-error { background: rgba(248, 81, 73, 0.08); color: var(--red); border-color: rgba(248, 81, 73, 0.2); }

/* ── Code Block ── */
.code-block {
    background: var(--bg);
    border: 1px solid var(--border);
    padding: 14px;
    border-radius: var(--radius-sm);
    font-family: 'JetBrains Mono', 'Consolas', monospace;
    font-size: 0.82rem;
    white-space: pre-wrap;
    color: var(--purple);
    margin-top: 10px;
    overflow-x: auto;
}

/* ── Spinner ── */
.spinner {
    display: inline-block;
    width: 14px; height: 14px;
    border: 2px solid rgba(255,255,255,0.2);
    border-top-color: #fff;
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ── Token Health Panel ── */
.health-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 16px;
    margin-top: 16px;
}
.health-card {
    background: var(--bg-elev2);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 16px;
    transition: var(--transition);
}
.health-card:hover { border-color: var(--border-light); }
.health-card .email { font-weight: 600; font-size: 0.85rem; margin-bottom: 8px; word-break: break-all; }
.health-card .row { display: flex; justify-content: space-between; font-size: 0.78rem; margin: 4px 0; }
.health-card .row .key { color: var(--text-muted); }
.health-card .row .val { font-weight: 500; }
.countdown-bar {
    height: 4px;
    border-radius: 2px;
    margin-top: 8px;
    overflow: hidden;
    background: var(--border);
}
.countdown-bar .fill { height: 100%; border-radius: 2px; transition: width 0.5s ease; }

/* ── Models Panel ── */
.models-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 16px;
}
.model-card {
    background: var(--bg-elev2);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 20px;
    transition: var(--transition);
}
.model-card:hover { border-color: var(--accent); transform: translateY(-2px); box-shadow: var(--shadow-sm); }
.model-card .name { font-size: 1.1rem; font-weight: 600; margin-bottom: 6px; }
.model-card .upstream { font-family: monospace; font-size: 0.78rem; color: var(--text-muted); margin-bottom: 10px; }
.model-card .info { display: flex; gap: 8px; flex-wrap: wrap; }

/* ── Email List ── */
.email-list { max-height: 350px; overflow-y: auto; }
.email-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 14px;
    background: var(--bg-elev2);
    border-radius: var(--radius-sm);
    margin-bottom: 6px;
    transition: var(--transition);
}
.email-item:hover { border-color: var(--border-light); }

/* ── Misc ── */
.token-text { font-family: monospace; font-size: 0.72rem; color: var(--text-dim); word-break: break-all; max-width: 250px; }
.flex { display: flex; gap: 10px; align-items: center; }
.flex-1 { flex: 1; }
.text-muted { color: var(--text-muted); }
.text-sm { font-size: 0.8rem; }
.mt-2 { margin-top: 16px; }
.mt-1 { margin-top: 8px; }

/* ── Responsive ── */
@media (max-width: 768px) {
    .header { padding: 12px 16px; flex-direction: column; align-items: flex-start; }
    .header-stats { width: 100%; justify-content: space-between; }
    .stat-card { padding: 6px 10px; min-width: 55px; }
    .stat-card .val { font-size: 1rem; }
    .nav { padding: 8px 16px; }
    .nav button { padding: 8px 12px; font-size: 0.78rem; }
    .content { padding: 16px; }
    .card { padding: 16px; }
    .health-grid { grid-template-columns: 1fr; }
    .models-grid { grid-template-columns: 1fr; }
    table { font-size: 0.78rem; }
    th, td { padding: 8px; }
}

/* Scrollbar */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--border-light); }
</style>
</head>
<body>

<!-- ── HEADER ── -->
<div class="header">
    <div class="header-left">
        <h1>⚡ AutoClaw</h1>
        <div id="router-widget" class="router-widget offline">
            <span class="dot"></span>
            <span id="router-text">Checking...</span>
        </div>
    </div>
    <div class="header-stats">
        <div class="stat-card"><div class="val" id="stat-accounts">0</div><div class="label">Accounts</div></div>
        <div class="stat-card"><div class="val" id="stat-tokens">0</div><div class="label">Tokens</div></div>
        <div class="stat-card"><div class="val" id="stat-emails">0</div><div class="label">Emails</div></div>
        <div class="stat-card"><div class="val" id="stat-points">0</div><div class="label">Points</div></div>
    </div>
</div>

<!-- ── NAV ── -->
<div class="nav">
    <button class="active" onclick="showPanel('health', this)">💓 Token Health</button>
    <button onclick="showPanel('models', this)">🤖 Models</button>
    <button onclick="showPanel('register', this)">📝 Register</button>
    <button onclick="showPanel('test', this)">🧪 Test API</button>
    <button onclick="showPanel('balance', this)">💰 Balance</button>
    <button onclick="showPanel('accounts', this)">📋 Accounts</button>
    <button onclick="showPanel('emails', this)">📧 Emails</button>
    <button onclick="showPanel('settings', this)">⚙️ Settings</button>
</div>

<div class="content">

<!-- ── TOKEN HEALTH PANEL ── -->
<div class="panel active" id="panel-health">
    <div class="card">
        <h2>💓 Token Health Monitor</h2>
        <div class="flex" style="margin-bottom: 16px; flex-wrap: wrap;">
            <button class="btn btn-blue" onclick="refreshAllNow()" id="btn-refresh-all">🔄 Refresh All Now</button>
            <button class="btn btn-sm" onclick="loadHealth()">↻ Reload</button>
            <span id="refresh-status-text" class="text-muted text-sm"></span>
        </div>
        <div id="refresh-info" style="margin-bottom: 16px;"></div>
        <div id="health-grid" class="health-grid"></div>
    </div>
</div>

<!-- ── MODELS PANEL ── -->
<div class="panel" id="panel-models">
    <div class="card">
        <h2>🤖 Available Models</h2>
        <p class="text-muted text-sm" style="margin-bottom: 16px;">Model is set via X-Request-Model header. The body "model" field is ignored upstream.</p>
        <div class="models-grid" id="models-grid"></div>
    </div>
</div>

<!-- ── REGISTER PANEL ── -->
<div class="panel" id="panel-register">
    <div class="card">
        <h2>📝 Register New Accounts</h2>
        <div class="alert alert-info">
            Registration uses Google OAuth via CloakBrowser (stealth, headless).
            Each new account gets ~2000 bonus points.
        </div>
        <div class="form-group">
            <label>Google Password (shared by all emails without individual password)</label>
            <input type="password" id="reg-password" placeholder="Enter Google account password">
        </div>
        <div class="form-group">
            <label>Number of accounts to register</label>
            <input type="number" id="reg-count" value="1" min="1" max="100">
        </div>
        <button class="btn btn-blue" onclick="startRegister()" id="btn-register">Start Registration</button>
        <div id="register-result" class="mt-2"></div>
    </div>
</div>

<!-- ── TEST API PANEL ── -->
<div class="panel" id="panel-test">
    <div class="card">
        <h2>🧪 Test API</h2>
        <div class="form-group">
            <label>Select Account</label>
            <select id="test-account" onchange="loadToken()"></select>
        </div>
        <div class="form-group">
            <label>Or paste token manually</label>
            <input type="text" id="test-token" placeholder="Bearer eyJ... (optional)">
        </div>
        <div class="form-group">
            <label>Model</label>
            <select id="test-model">
                <option value="1">GLM-5.2 (Best, ~3 pts)</option>
                <option value="2">GLM-5-Turbo (Cheapest, 1 pt)</option>
                <option value="3">DeepSeek-V4 (Expensive, ~7 pts)</option>
            </select>
        </div>
        <div class="form-group">
            <label>Prompt</label>
            <textarea id="test-prompt" rows="3">Hello! What model are you? Reply in 1 sentence.</textarea>
        </div>
        <button class="btn" onclick="runTest()" id="btn-test">Send Request</button>
        <div id="test-result" class="mt-2"></div>
    </div>
</div>

<!-- ── BALANCE PANEL ── -->
<div class="panel" id="panel-balance">
    <div class="card">
        <h2>💰 Check Balance</h2>
        <button class="btn" onclick="checkBalance()" id="btn-balance">Refresh All Balances</button>
        <div id="balance-result" class="mt-2"></div>
    </div>
</div>

<!-- ── ACCOUNTS PANEL ── -->
<div class="panel" id="panel-accounts">
    <div class="card">
        <h2>📋 Saved Accounts</h2>
        <button class="btn btn-blue btn-sm" onclick="loadAccounts()">↻ Refresh</button>
        <div id="accounts-table" class="mt-2"></div>
    </div>
</div>

<!-- ── EMAILS PANEL ── -->
<div class="panel" id="panel-emails">
    <div class="card">
        <h2>📧 Email List</h2>
        <div class="alert alert-info">
            Format: <code>email@gmail.com:password</code> — per-email password (OK if different!).<br>
            Or just <code>email@gmail.com</code> — uses global password from Settings.
        </div>
        <div class="form-group">
            <label>Add Email (email:password or just email)</label>
            <div class="flex">
                <input type="text" id="email-input" placeholder="user@gmail.com:mypassword" class="flex-1">
                <button class="btn btn-sm" onclick="addEmail()">Add</button>
            </div>
        </div>
        <div class="form-group">
            <label>Or upload email.txt</label>
            <input type="file" id="email-file" accept=".txt" onchange="uploadEmails(event)">
        </div>
        <div id="email-list" class="email-list mt-2"></div>
    </div>
</div>

<!-- ── SETTINGS PANEL ── -->
<div class="panel" id="panel-settings">
    <div class="card">
        <h2>⚙️ Settings (config.json)</h2>
        <div class="form-group">
            <label>Password (global Google password)</label>
            <input type="text" id="cfg-password" placeholder="Google password">
        </div>
        <div class="form-group">
            <label>Batch Size (concurrent registrations)</label>
            <input type="number" id="cfg-batch" value="5" min="1" max="20">
        </div>
        <div class="form-group">
            <label>Email File</label>
            <input type="text" id="cfg-emailfile" value="email.txt">
        </div>
        <button class="btn" onclick="saveSettings()">Save Settings</button>
        <div id="settings-result" class="mt-2"></div>
    </div>
</div>

</div>

<script>
let accounts = [];
let sortCol = null;
let sortDir = 1;

// ── Navigation ──
function showPanel(name, btn) {
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav button').forEach(b => b.classList.remove('active'));
    document.getElementById('panel-' + name).classList.add('active');
    if (btn) btn.classList.add('active');
    if (name === 'health') loadHealth();
    if (name === 'models') loadModels();
    if (name === 'accounts') loadAccounts();
    if (name === 'emails') loadEmails();
    if (name === 'test') loadAccountsForTest();
    if (name === 'settings') loadSettings();
}

// ── Stats + Router Status ──
let _statsController = null;
async function loadStats() {
    try {
        if (_statsController) _statsController.abort();
        _statsController = new AbortController();
        const r = await fetch('/api/stats', {signal: _statsController.signal});
        const d = await r.json();
        document.getElementById('stat-accounts').textContent = d.accounts;
        document.getElementById('stat-tokens').textContent = d.active_tokens;
        document.getElementById('stat-emails').textContent = d.emails;
        document.getElementById('stat-points').textContent = d.total_points.toLocaleString();

        // Router widget
        const widget = document.getElementById('router-widget');
        const text = document.getElementById('router-text');
        if (d.router && d.router.running) {
            widget.className = 'router-widget online';
            text.textContent = `Router ● :${d.router.port} · ${d.router.uptime}`;
        } else {
            widget.className = 'router-widget offline';
            text.textContent = 'Router Offline';
        }
    } catch(e) { if (e.name !== 'AbortError') console.error('Stats error:', e); }
}

// ── Token Health ──
async function loadHealth() {
    const grid = document.getElementById('health-grid');
    grid.innerHTML = '<div class="text-muted"><span class="spinner"></span> Loading...</div>';

    // Get refresh status
    try {
        const rs = await fetch('/api/refresh-status');
        const rsData = await rs.json();
        const infoDiv = document.getElementById('refresh-info');
        if (rsData.running) {
            infoDiv.innerHTML = `<div class="alert alert-success">
                <strong>Auto-Refresh: Running</strong> &nbsp;|&nbsp;
                Last: ${rsData.last_refresh || '—'} &nbsp;|&nbsp;
                Next: ${rsData.next_refresh || '—'} &nbsp;|&nbsp;
                Interval: ${rsData.interval_hours}h
            </div>`;
        } else {
            infoDiv.innerHTML = `<div class="alert alert-error">
                <strong>Auto-Refresh: Not running</strong> — Start the router to enable auto-refresh.
            </div>`;
        }
        document.getElementById('refresh-status-text').textContent = rsData.running ? 'Auto-refresh active' : 'Auto-refresh inactive';
    } catch(e) {
        document.getElementById('refresh-info').innerHTML = '<div class="alert alert-error">Router not running — cannot get refresh status.</div>';
    }

    // Load accounts for health display
    try {
        const r = await fetch('/api/accounts');
        const d = await r.json();
        accounts = d.accounts;
        if (d.total === 0) {
            grid.innerHTML = '<div class="alert alert-info">No accounts yet. Register first!</div>';
            return;
        }

        let html = '';
        const now = new Date();
        accounts.forEach((acc, i) => {
            const lastRefresh = acc.last_at_refresh || acc.registered_at || '';
            const lastRTRotation = acc.last_rt_rotation || '';
            let atHoursLeft = '—', rtDaysLeft = '—';
            let atPct = 0, rtPct = 0;
            let atColor = 'var(--green)', rtColor = 'var(--green)';

            if (lastRefresh) {
                const dt = new Date(lastRefresh);
                const hoursSince = (now - dt) / 3600000;
                atHoursLeft = Math.max(0, 24 - hoursSince).toFixed(1) + 'h';
                atPct = Math.max(0, Math.min(100, (1 - hoursSince/24) * 100));
                if (atPct < 25) atColor = 'var(--red)';
                else if (atPct < 50) atColor = 'var(--yellow)';
            }
            if (lastRTRotation) {
                const dt = new Date(lastRTRotation);
                const daysSince = (now - dt) / 86400000;
                rtDaysLeft = Math.max(0, 30 - daysSince).toFixed(1) + 'd';
                rtPct = Math.max(0, Math.min(100, (1 - daysSince/30) * 100));
                if (rtPct < 25) rtColor = 'var(--red)';
                else if (rtPct < 50) rtColor = 'var(--yellow)';
            }

            const balance = acc.balance || '?';
            const balBadge = balance !== 'N/A' && balance !== '?' ?
                `<span class="badge badge-green">${balance}</span>` :
                `<span class="badge badge-red">${balance}</span>`;

            html += `<div class="health-card">
                <div class="email">${acc.email || 'N/A'}</div>
                <div class="row"><span class="key">Balance</span><span class="val">${balBadge}</span></div>
                <div class="row"><span class="key">AT Refresh</span><span class="val">${lastRefresh || 'Never'}</span></div>
                <div class="row"><span class="key">AT Expires</span><span class="val" style="color:${atColor}">${atHoursLeft}</span></div>
                <div class="countdown-bar"><div class="fill" style="width:${atPct}%;background:${atColor}"></div></div>
                <div class="row mt-1"><span class="key">RT Rotated</span><span class="val">${lastRTRotation || 'Never'}</span></div>
                <div class="row"><span class="key">RT Expires</span><span class="val" style="color:${rtColor}">${rtDaysLeft}</span></div>
                <div class="countdown-bar"><div class="fill" style="width:${rtPct}%;background:${rtColor}"></div></div>
            </div>`;
        });
        grid.innerHTML = html;
    } catch(e) {
        grid.innerHTML = '<div class="alert alert-error">Failed to load health data.</div>';
    }
}

async function refreshAllNow() {
    const btn = document.getElementById('btn-refresh-all');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Refreshing...';
    try {
        const r = await fetch('/api/refresh-now', {method: 'POST'});
        const d = await r.json();
        if (d.ok) {
            btn.innerHTML = '✓ Done!';
            setTimeout(() => { btn.innerHTML = '🔄 Refresh All Now'; btn.disabled = false; }, 2000);
            loadHealth();
            loadStats();
        } else {
            btn.innerHTML = '🔄 Refresh All Now';
            btn.disabled = false;
            alert('Refresh failed: ' + (d.error || 'Unknown error'));
        }
    } catch(e) {
        btn.innerHTML = '🔄 Refresh All Now';
        btn.disabled = false;
        alert('Refresh failed: Router not reachable');
    }
}

// ── Models ──
function loadModels() {
    const grid = document.getElementById('models-grid');
    const models = [
        {id: 'glm-5.2', name: 'GLM-5.2', upstream: 'openrouter_glm-5.2', cost: '~3 pts', tier: 'Best', context: '128K'},
        {id: 'glm-5-turbo', name: 'GLM-5-Turbo', upstream: 'zai_glm-5-turbo', cost: '1 pt', tier: 'Cheapest', context: '128K'},
        {id: 'deepseek-v4', name: 'DeepSeek-V4', upstream: 'zai_auto', cost: '~7 pts', tier: 'Expensive', context: '128K'},
    ];
    let html = '';
    models.forEach(m => {
        const tierBadge = m.tier === 'Best' ? 'badge-purple' : m.tier === 'Cheapest' ? 'badge-green' : 'badge-yellow';
        html += `<div class="model-card">
            <div class="name">${m.name}</div>
            <div class="upstream">upstream: ${m.upstream}</div>
            <div class="info">
                <span class="badge ${tierBadge}">${m.tier}</span>
                <span class="badge badge-blue">${m.cost}</span>
                <span class="badge badge-green">${m.context}</span>
            </div>
        </div>`;
    });
    grid.innerHTML = html;
}

// ── Register ──
async function startRegister() {
    const pw = document.getElementById('reg-password').value;
    const count = document.getElementById('reg-count').value;
    const btn = document.getElementById('btn-register');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Processing...';
    const r = await fetch('/api/register', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({password: pw, count: parseInt(count)})
    });
    const d = await r.json();
    btn.disabled = false;
    btn.textContent = 'Start Registration';
    const result = document.getElementById('register-result');
    if (d.ok) {
        result.innerHTML = '<div class="alert alert-info">' + d.message.replace(/\n/g, '<br>') + '</div>';
    } else {
        result.innerHTML = '<div class="alert alert-error">Error: ' + d.error + '</div>';
    }
    loadStats();
}

// ── Test API ──
async function loadAccountsForTest() {
    const r = await fetch('/api/accounts');
    const d = await r.json();
    accounts = d.accounts;
    const sel = document.getElementById('test-account');
    sel.innerHTML = '<option value="">-- Select Account --</option>';
    accounts.forEach((acc, i) => {
        sel.innerHTML += `<option value="${i}">${acc.email || 'N/A'}</option>`;
    });
}

function loadToken() {
    const idx = document.getElementById('test-account').value;
    if (idx !== '') {
        document.getElementById('test-token').value = accounts[idx].access_token || '';
    }
}

async function runTest() {
    const token = document.getElementById('test-token').value.trim();
    const model = document.getElementById('test-model').value;
    const prompt = document.getElementById('test-prompt').value;
    const btn = document.getElementById('btn-test');
    if (!token) { alert('Please select an account or paste a token'); return; }
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Sending...';
    const r = await fetch('/api/test', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({token, model, prompt})
    });
    const d = await r.json();
    btn.disabled = false;
    btn.textContent = 'Send Request';
    const result = document.getElementById('test-result');
    if (d.ok) {
        result.innerHTML = `<div class="alert alert-success"><strong>${d.model}</strong></div><div class="code-block">${d.response}</div>`;
    } else {
        result.innerHTML = `<div class="alert alert-error"><strong>${d.model || ''}</strong><br>${d.error}</div>`;
    }
}

// ── Balance ──
async function checkBalance() {
    const btn = document.getElementById('btn-balance');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Checking...';
    const r = await fetch('/api/balance', {method: 'POST'});
    const d = await r.json();
    btn.disabled = false;
    btn.textContent = 'Refresh All Balances';
    const result = document.getElementById('balance-result');
    let html = `<div class="alert alert-success">Total: ${d.total_points.toLocaleString()} points (${d.total_accounts} accounts)</div>`;
    html += '<table><tr><th onclick="sortBalance(this,\'email\')">Email</th><th onclick="sortBalance(this,\'balance\')">Balance</th><th>Status</th></tr>';
    d.results.forEach(r => {
        const ok = r.balance !== 'N/A';
        html += `<tr><td>${r.email}</td><td>${r.balance}</td><td><span class="badge ${ok?'badge-green':'badge-red'}">${ok?'OK':'FAIL'}</span></td></tr>`;
    });
    html += '</table>';
    result.innerHTML = html;
    loadStats();
}

// ── Accounts ──
async function loadAccounts() {
    const r = await fetch('/api/accounts');
    const d = await r.json();
    const div = document.getElementById('accounts-table');
    if (d.total === 0) {
        div.innerHTML = '<div class="alert alert-info">No accounts yet. Register first!</div>';
        return;
    }
    let html = '<table><thead><tr>';
    html += '<th onclick="sortAccounts(0,\'num\')">#</th>';
    html += '<th onclick="sortAccounts(1,\'email\')">Email</th>';
    html += '<th onclick="sortAccounts(2,\'balance\')">Balance</th>';
    html += '<th>Token (preview)</th>';
    html += '<th onclick="sortAccounts(4,\'date\')">Date</th>';
    html += '</tr></thead><tbody id="accounts-tbody">';
    d.accounts.forEach((acc, i) => {
        const token = (acc.access_token || '').substring(0, 35) + '...';
        html += `<tr><td>${i+1}</td><td>${acc.email||'N/A'}</td><td>${acc.balance||'?'}</td><td class="token-text">${token}</td><td>${(acc.registered_at||'?').substring(0,10)}</td></tr>`;
    });
    html += '</tbody></table>';
    div.innerHTML = html;
}

// ── Sortable tables ──
function sortAccounts(colIdx, type) {
    const tbody = document.getElementById('accounts-tbody');
    if (!tbody) return;
    const rows = Array.from(tbody.querySelectorAll('tr'));
    if (sortCol === colIdx) sortDir *= -1; else { sortCol = colIdx; sortDir = 1; }
    rows.sort((a, b) => {
        let va = a.children[colIdx].textContent, vb = b.children[colIdx].textContent;
        if (type === 'num' || type === 'balance') {
            va = parseFloat(va) || 0; vb = parseFloat(vb) || 0;
        }
        if (va < vb) return -1 * sortDir;
        if (va > vb) return 1 * sortDir;
        return 0;
    });
    rows.forEach(r => tbody.appendChild(r));
    // Update header indicators
    document.querySelectorAll('#accounts-table th').forEach((th, i) => {
        th.classList.remove('sort-asc', 'sort-desc');
        if (i === colIdx) th.classList.add(sortDir > 0 ? 'sort-asc' : 'sort-desc');
    });
}

// ── Emails ──
async function loadEmails() {
    const r = await fetch('/api/emails');
    const d = await r.json();
    const div = document.getElementById('email-list');
    if (d.total === 0) {
        div.innerHTML = '<div class="alert alert-info">No emails yet. Add some!</div>';
        return;
    }
    let html = '';
    d.emails.forEach(e => {
        const badge = e.has_password
            ? '<span class="badge badge-green" style="margin-left:8px;">pwd</span>'
            : '<span class="badge badge-yellow" style="margin-left:8px;">global</span>';
        html += `<div class="email-item"><span>${e.email}${badge}</span><button class="btn btn-red btn-sm" onclick="deleteEmail('${e.email}')">Del</button></div>`;
    });
    div.innerHTML = html;
}

async function addEmail() {
    const email = document.getElementById('email-input').value.trim();
    if (!email) return;
    await fetch('/api/emails/add', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({email})
    });
    document.getElementById('email-input').value = '';
    loadEmails();
    loadStats();
}

async function deleteEmail(email) {
    await fetch('/api/emails/delete', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({email})
    });
    loadEmails();
    loadStats();
}

async function uploadEmails(event) {
    const file = event.target.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    await fetch('/api/emails/upload', {method: 'POST', body: fd});
    loadEmails();
    loadStats();
}

// ── Settings ──
async function loadSettings() {
    const r = await fetch('/api/config');
    const d = await r.json();
    document.getElementById('cfg-password').value = d.password || '';
    document.getElementById('cfg-batch').value = d.batch_size || 5;
    document.getElementById('cfg-emailfile').value = d.email_file || 'email.txt';
}

async function saveSettings() {
    const data = {
        password: document.getElementById('cfg-password').value,
        batch_size: parseInt(document.getElementById('cfg-batch').value),
        email_file: document.getElementById('cfg-emailfile').value,
    };
    await fetch('/api/config', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data)
    });
    document.getElementById('settings-result').innerHTML = '<div class="alert alert-success">Settings saved!</div>';
}

// ── Init ──
loadStats();
loadHealth();
setInterval(loadStats, 60000); // Auto-refresh stats every 60s
</script>
</body>
</html>
"""

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AutoClaw Dashboard")
    parser.add_argument("--port", type=int, default=31001)
    parser.add_argument("--host", type=str, default="localhost")
    args = parser.parse_args()

    print("=" * 55)
    print("  AutoClaw Dashboard")
    print(f"  http://{args.host}:{args.port}")
    print(f"  Dir: {_DIR}")
    print("=" * 55)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
