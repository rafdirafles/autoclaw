#!/usr/bin/env python3
"""
AutoClaw Router — OpenAI-compatible proxy for AutoClaw (Z.ai/Zhipu) LLM API.

Listens on localhost:31000. Reads access tokens from tokens.txt (or accounts.json),
rotates them round-robin, and forwards chat completion requests to the AutoClaw
backend (autoglm-api.autoglm.ai).

Includes a built-in 2-layer auto-refresh daemon thread (see refresh.py).

Endpoints:
  GET  /v1/models              — list available models
  POST /v1/chat/completions    — chat completion (stream + non-stream)
  GET  /health                 — health check + token stats
  GET  /refresh-status         — auto-refresh thread status
  POST /refresh-now            — trigger manual refresh cycle

Usage:
  python router.py
  python router.py --port 31000
"""

import hashlib
import json
import os
import sys
import time
import uuid
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib import request as urlrequest
from urllib import error as urlerror
from pathlib import Path

# Fix Windows cp1252 charmap codec error (same fix as refresh.py)
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Import the refresh module (same directory)
from refresh import (
    AutoRefreshThread, refresh_all_accounts, refresh_account,
    load_accounts, save_accounts, generate_sign, get_auth_headers,
    APP_ID, APP_KEY, BASE_URL, ACCOUNTS_FILE, TOKENS_FILE,
)

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

HOST = "localhost"
PORT = 31000

# API key for router validation (clients send Authorization: Bearer <key>)
# Try env var first, then .env file, then default
def _load_api_key():
    # 1. Environment variable
    key = os.environ.get("AUTOCLAW_API_KEY")
    if key:
        return key
    # 2. Local .env file (gitignored, for local dev)
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("AUTOCLAW_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    # 3. Default (must be changed)
    return "sk-change-me"

ROUTER_API_KEY = _load_api_key()

PROXY_PATH = "/autoclaw-proxy/proxy/autoclaw/chat/completions"

# Available models — model is set via X-Request-Model header, NOT body
MODELS = {
    "glm-5.2": {
        "id": "glm-5.2",
        "upstream_model": "openrouter_glm-5.2",
        "cost": "~3 pts",
        "context": "128K",
    },
    "glm-5-turbo": {
        "id": "glm-5-turbo",
        "upstream_model": "zai_glm-5-turbo",
        "cost": "1 pt",
        "context": "128K",
    },
    "deepseek-v4": {
        "id": "deepseek-v4",
        "upstream_model": "zai_auto",
        "cost": "~7 pts",
        "context": "128K",
    },
}

# ═══════════════════════════════════════════════════════════════
# TOKEN MANAGER — round-robin rotation with accounts.json support
# ═══════════════════════════════════════════════════════════════

class TokenManager:
    def __init__(self, tokens_file=None, accounts_file=None):
        self.tokens_file = tokens_file or TOKENS_FILE
        self.accounts_file = accounts_file or ACCOUNTS_FILE
        self.tokens = []
        self.index = 0
        self.failed = set()
        self._lock = threading.Lock()
        self.load()

    def load(self):
        """Load tokens from accounts.json (preferred) or tokens.txt."""
        # Try accounts.json first
        accounts = load_accounts()
        if accounts:
            self.tokens = []
            for acc in accounts:
                token = acc.get("access_token", "")
                if token:
                    # Strip "Bearer " prefix for internal use
                    if token.startswith("Bearer "):
                        token = token[7:]
                    self.tokens.append(token)
            print(f"[INFO] Loaded {len(self.tokens)} tokens from accounts.json")
            return

        # Fallback: tokens.txt
        path = Path(self.tokens_file)
        if not path.exists():
            print(f"[WARN] Token file not found: {path}")
            return
        with open(path, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        self.tokens = [t[7:] if t.startswith("Bearer ") else t for t in lines]
        print(f"[INFO] Loaded {len(self.tokens)} tokens from {path}")

    def reload(self):
        """Hot-reload tokens from file."""
        with self._lock:
            old_count = len(self.tokens)
            self.load()
            self.failed.clear()
            print(f"[INFO] Reloaded: {old_count} -> {len(self.tokens)} tokens")

    def get_next(self):
        """Get next valid token (round-robin, skip failed)."""
        with self._lock:
            if not self.tokens:
                return None
            n = len(self.tokens)
            for _ in range(n):
                idx = self.index % n
                self.index += 1
                if idx not in self.failed:
                    return self.tokens[idx]
            # All failed — reset
            self.failed.clear()
            return self.tokens[0] if self.tokens else None

    def mark_failed(self, token):
        """Mark a token as failed (auth error)."""
        with self._lock:
            if token in self.tokens:
                idx = self.tokens.index(token)
                self.failed.add(idx)
                print(f"[WARN] Token #{idx} marked failed (auth error)")

    def stats(self):
        return {
            "total": len(self.tokens),
            "active": len(self.tokens) - len(self.failed),
            "failed": len(self.failed),
        }


# ═══════════════════════════════════════════════════════════════
# AUTOCLAW API
# ═══════════════════════════════════════════════════════════════

def build_upstream_headers(token, upstream_model):
    """Build headers for AutoClaw API request."""
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
        "x-request-model": upstream_model,
        "x-request-id": str(uuid.uuid4()),
        "x-authorization": f"Bearer {token}",
    }


def call_autoclaw(token, upstream_model, body):
    """Forward request to AutoClaw API. Returns (response_bytes, status_code, headers)."""
    body["stream"] = True  # Force stream upstream (DeepSeek 500 on non-stream)

    url = BASE_URL + PROXY_PATH
    headers = build_upstream_headers(token, upstream_model)
    data = json.dumps(body).encode("utf-8")

    req = urlrequest.Request(url, data=data, headers=headers, method="POST")

    try:
        resp = urlrequest.urlopen(req, timeout=120)
        return resp.read(), resp.status, dict(resp.headers)
    except urlerror.HTTPError as e:
        return e.read(), e.code, dict(e.headers)
    except Exception as e:
        return json.dumps({"error": str(e)}).encode(), 500, {}


# ═══════════════════════════════════════════════════════════════
# ON-DEMAND SINGLE TOKEN REFRESH (401 handler)
# ═══════════════════════════════════════════════════════════════

def refresh_single_token(failed_token):
    """
    Called when a token gets 401 during a request.
    Find the account, refresh it, save, and reload tokens.
    Returns the new access_token or None.
    """
    # Normalize: strip "Bearer " if present
    if failed_token.startswith("Bearer "):
        failed_token = failed_token[7:]

    accounts = load_accounts()
    for acc in accounts:
        at = acc.get("access_token", "")
        if at.startswith("Bearer "):
            at = at[7:]
        if at == failed_token:
            print(f"[ON-DEMAND] Refreshing token for {acc.get('email', 'unknown')}...")
            updated = refresh_account(acc)
            if updated:
                save_accounts(accounts)
                token_mgr.reload()
                new_at = updated["access_token"]
                return new_at[7:] if new_at.startswith("Bearer ") else new_at
            break
    return None


# ═══════════════════════════════════════════════════════════════
# HTTP HANDLER
# ═══════════════════════════════════════════════════════════════

token_mgr = None
refresh_thread = None
start_time = time.time()


class RouterHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}")

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _check_auth(self):
        """Check if request has valid API key."""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            key = auth[7:]
        else:
            key = auth
        if key != ROUTER_API_KEY:
            self._json(401, {"error": {"message": "Invalid API key", "type": "invalid_request_error"}})
            return False
        return True

    def do_GET(self):
        if self.path == "/health":
            uptime = int(time.time() - start_time)
            self._json(200, {
                "status": "ok",
                "service": "autoclaw-router",
                "uptime_seconds": uptime,
                "tokens": token_mgr.stats(),
                "models": list(MODELS.keys()),
            })
            return

        if self.path == "/refresh-status":
            self._json(200, refresh_thread.status() if refresh_thread else {"running": False})
            return

        if self.path == "/v1/models":
            # Models endpoint is public (no auth needed for listing)
            data = {
                "object": "list",
                "data": [
                    {
                        "id": m["id"],
                        "object": "model",
                        "created": 1700000000,
                        "owned_by": "autoclaw",
                        "cost": m["cost"],
                        "context_window": m.get("context", "128K"),
                    }
                    for m in MODELS.values()
                ],
            }
            self._json(200, data)
            return

        self._json(404, {"error": {"message": "Not found", "type": "invalid_request"}})

    def do_POST(self):
        if self.path == "/refresh-now":
            # Manual trigger — non-blocking: kick off in background thread, respond immediately
            def _bg_refresh():
                try:
                    result = refresh_all_accounts()
                    token_mgr.reload()
                    print(f"[REFRESH-NOW] Complete: {result}")
                except Exception as e:
                    print(f"[REFRESH-NOW] Error: {e}")
            threading.Thread(target=_bg_refresh, daemon=True).start()
            self._json(200, {"ok": True, "message": "Refresh started in background. Check /refresh-status for progress."})
            return

        if self.path != "/v1/chat/completions":
            self._json(404, {"error": {"message": "Not found"}})
            return

        if not self._check_auth():
            return

        body = self._read_body()
        if body is None:
            self._json(400, {"error": {"message": "Invalid JSON body"}})
            return

        # Map model name
        requested_model = body.get("model", "glm-5.2")
        model_info = MODELS.get(requested_model)
        if not model_info:
            self._json(400, {"error": {"message": f"Unknown model: {requested_model}. Available: {list(MODELS.keys())}"}})
            return

        upstream_model = model_info["upstream_model"]
        client_wants_stream = body.get("stream", False)

        # Get token
        token = token_mgr.get_next()
        if not token:
            self._json(503, {"error": {"message": "No tokens available. Run register.py first."}})
            return

        # Forward to AutoClaw
        resp_bytes, status, resp_headers = call_autoclaw(token, upstream_model, body)

        # Check for auth errors — try refresh + retry
        if status in (401, 410):
            print(f"[AUTH-FAIL] Token got {status}, attempting on-demand refresh...")
            new_token = refresh_single_token(token)
            if new_token:
                token = new_token
                resp_bytes, status, resp_headers = call_autoclaw(token, upstream_model, body)
            else:
                token_mgr.mark_failed(token)
                # Try next token
                token = token_mgr.get_next()
                if token:
                    resp_bytes, status, resp_headers = call_autoclaw(token, upstream_model, body)

        # Check for 402 (points insufficient) — skip and try next token
        if status == 402:
            print(f"[POINTS-EMPTY] Token got 402 (points insufficient), trying next...")
            token_mgr.mark_failed(token)
            for _ in range(min(5, len(token_mgr.tokens) - 1)):
                token = token_mgr.get_next()
                if not token:
                    break
                resp_bytes, status, resp_headers = call_autoclaw(token, upstream_model, body)
                if status == 200:
                    break
                if status in (401, 410):
                    new_token = refresh_single_token(token)
                    if new_token:
                        token = new_token
                        resp_bytes, status, resp_headers = call_autoclaw(token, upstream_model, body)
                        if status == 200:
                            break
                if status == 402:
                    token_mgr.mark_failed(token)
                    continue
                break

        if status != 200:
            try:
                err = json.loads(resp_bytes)
            except Exception:
                err = {"error": {"message": resp_bytes.decode("utf-8", errors="replace")[:500]}}
            self._json(status, err)
            return

        # Stream or aggregate
        if client_wants_stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(resp_bytes)
            self.wfile.flush()
        else:
            aggregated = self._aggregate_sse(resp_bytes, requested_model)
            self._json(200, aggregated)

    def _aggregate_sse(self, raw_bytes, model_name):
        """Parse SSE stream and build a single OpenAI-compatible response."""
        text = raw_bytes.decode("utf-8", errors="replace")
        full_content = ""
        finish_reason = None
        usage = None

        for line in text.split("\n"):
            line = line.strip()
            if not line or not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            choices = chunk.get("choices", [])
            if choices:
                delta = choices[0].get("delta", {})
                if "content" in delta:
                    full_content += delta["content"]
                if choices[0].get("finish_reason"):
                    finish_reason = choices[0]["finish_reason"]

            if chunk.get("usage"):
                usage = chunk["usage"]

        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": full_content},
                    "finish_reason": finish_reason or "stop",
                }
            ],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    global token_mgr, refresh_thread

    import argparse
    parser = argparse.ArgumentParser(description="AutoClaw Router — OpenAI-compatible proxy")
    parser.add_argument("--port", type=int, default=PORT, help="Port to listen on")
    parser.add_argument("--host", type=str, default=HOST, help="Host to bind")
    parser.add_argument("--no-refresh", action="store_true", help="Disable auto-refresh thread")
    args = parser.parse_args()

    print("=" * 55)
    print("  AutoClaw Router — OpenAI-compatible proxy")
    print(f"  Listening: http://{args.host}:{args.port}")
    print(f"  Upstream:  {BASE_URL}")
    print(f"  Tokens:    {TOKENS_FILE} / {ACCOUNTS_FILE}")
    print("=" * 55)

    # Initialize token manager
    token_mgr = TokenManager()

    # Start auto-refresh daemon thread
    if not args.no_refresh:
        refresh_thread = AutoRefreshThread()
        refresh_thread.on_cycle_complete = token_mgr.reload
        refresh_thread.start()
        print("[OK] Auto-refresh thread started (unified cycle: every 20h, L1+L2)")
    else:
        print("[SKIP] Auto-refresh thread disabled")

    server = ThreadedHTTPServer((args.host, args.port), RouterHandler)
    print(f"\n[OK] Server started on {args.host}:{args.port}")
    print(f"  GET  /v1/models")
    print(f"  POST /v1/chat/completions")
    print(f"  GET  /health")
    print(f"  GET  /refresh-status")
    print(f"  POST /refresh-now")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[STOP] Server shutting down...")
        if refresh_thread:
            refresh_thread.stop()
        server.shutdown()


if __name__ == "__main__":
    main()
