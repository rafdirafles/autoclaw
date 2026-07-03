#!/usr/bin/env python3
"""
AutoClaw Auto-Refresh Module — unified token refresh (laptop-off-safe).

Unified cycle (every 20 hours, ALL accounts):
  L1: Refresh access_token using current refresh_token.
      POST /userapi/v1/refresh
      Payload: {"source_id":"autoclaw","device_id":"<did>","refresh_token":"<current_refresh_token>"}
      Response: {"code":0,"data":{"access_token":"NEW","refresh_token":"SAME","refresh":false}}

  L2: Rotate refresh_token if its JWT expiry is < 5 days away.
      Same endpoint, but use the fresh access_token AS refresh_token:
      Payload: {"source_id":"autoclaw","device_id":"<did>","refresh_token":"<fresh_access_token>"}
      Response: {"code":0,"data":{"access_token":"NEW","refresh_token":"NEW ROTATED","refresh":true}}

On every router startup the cycle runs immediately — so if the laptop was off,
tokens are refreshed right away. L2 checks actual JWT expiry (not a day counter),
so it rotates exactly when needed, regardless of downtime.

The "refresh":true flag means the refresh_token was rotated (30-day expiry reset).
This creates an infinite chain — never needs Google re-login.
"""

import hashlib
import json
import os
import sys
import time
import threading
import uuid
from datetime import datetime, timedelta

import requests

# Fix Windows cp1252 charmap codec error
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ═══════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════

APP_ID = "100003"
APP_KEY = "38d2391985e2369a5fb8227d8e6cd5e5"
BASE_URL = "https://autoglm-api.autoglm.ai"
REFRESH_ENDPOINT = f"{BASE_URL}/userapi/v1/refresh"

# Refresh intervals
AT_REFRESH_INTERVAL = 20 * 3600       # 20 hours — unified cycle runs L1 always, L2 if needed
RT_ROTATE_THRESHOLD = 5 * 24 * 3600   # 5 days — rotate RT if less than this remaining

# File paths (relative to this module)
_DIR = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = os.path.join(_DIR, "accounts.json")
TOKENS_FILE = os.path.join(_DIR, "tokens.txt")


# ═══════════════════════════════════════════════════════════════
# SHARED API HELPERS
# ═══════════════════════════════════════════════════════════════

def generate_sign(timestamp):
    """Generate MD5 signature for AutoClaw API auth."""
    raw = f"{APP_ID}&{timestamp}&{APP_KEY}"
    return hashlib.md5(raw.encode()).hexdigest()


def get_auth_headers(access_token=None):
    """Build standard auth headers. If access_token given, add authorization."""
    ts = str(int(time.time()))
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://autoclaw.z.ai",
        "referer": "https://autoclaw.z.ai/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
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
    if access_token:
        # Ensure "Bearer " prefix
        if not access_token.startswith("Bearer "):
            access_token = f"Bearer {access_token}"
        headers["authorization"] = access_token
    return headers


# ═══════════════════════════════════════════════════════════════
# DATA I/O
# ═══════════════════════════════════════════════════════════════

def load_accounts():
    """Load accounts from accounts.json."""
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_accounts(accounts):
    """Save accounts to accounts.json + update tokens.txt."""
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2, ensure_ascii=False)
    # Update tokens.txt (one access_token per line, with "Bearer " prefix)
    with open(TOKENS_FILE, "w", encoding="utf-8") as f:
        for acc in accounts:
            token = acc.get("access_token", "")
            if token:
                f.write(f"{token}\n")


# ═══════════════════════════════════════════════════════════════
# REFRESH LOGIC
# ═══════════════════════════════════════════════════════════════

def _do_refresh(access_token, refresh_token, device_id):
    """
    Call the /userapi/v1/refresh endpoint.
    Returns (new_access_token, new_refresh_token, rotated_bool) or (None, None, False) on failure.
    """
    headers = get_auth_headers(access_token)
    payload = {
        "source_id": "autoclaw",
        "device_id": device_id,
        "refresh_token": refresh_token,
    }
    try:
        resp = requests.post(REFRESH_ENDPOINT, json=payload, headers=headers, timeout=30)
        data = resp.json()
        if data.get("code") == 0 and data.get("data", {}).get("access_token"):
            d = data["data"]
            new_at = d["access_token"]
            new_rt = d.get("refresh_token", refresh_token)
            rotated = d.get("refresh", False)
            # Ensure "Bearer " prefix
            if not new_at.startswith("Bearer "):
                new_at = f"Bearer {new_at}"
            if not new_rt.startswith("Bearer "):
                new_rt = f"Bearer {new_rt}"
            return new_at, new_rt, rotated
        else:
            print(f"  [REFRESH ERROR] code={data.get('code')}, msg={data.get('message', data.get('msg', 'unknown'))}")
            return None, None, False
    except Exception as e:
        print(f"  [REFRESH EXCEPTION] {e}")
        return None, None, False


def _get_token_expiry(token):
    """Decode JWT and return expiry timestamp (int), or None on failure."""
    if not token:
        return None
    if token.startswith("Bearer "):
        token = token[7:]
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        import base64 as _b64
        payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
        data = json.loads(_b64.urlsafe_b64decode(payload))
        return data.get("exp")
    except Exception:
        return None


def refresh_account(account):
    """
    Refresh a single account — unified cycle (L1 + L2 as needed).
    
    L1: Always run. Use current refresh_token → get new access_token.
    L2: Only if RT expiry < 5 days remaining. Use fresh AT as RT → rotate RT.
    
    This is laptop-off-safe: on every startup the thread runs immediately,
    and L2 triggers based on actual RT expiry, not a day counter.
    
    Returns updated account dict, or None on total failure.
    """
    email = account.get("email", "unknown")
    device_id = account.get("device_id", "")
    access_token = account.get("access_token", "")
    refresh_token = account.get("refresh_token", "")
    
    if not device_id or not access_token or not refresh_token:
        print(f"  [SKIP] {email}: missing device_id/access_token/refresh_token")
        return None
    
    # ── Layer 1: Access token refresh (always) ──
    print(f"  [L1] Refreshing access token for {email}...")
    new_at, new_rt_same, _ = _do_refresh(access_token, refresh_token, device_id)
    
    if not new_at:
        print(f"  [L1 FAIL] {email}: Layer 1 refresh failed")
        return None
    
    account["access_token"] = new_at
    account["last_at_refresh"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"  [L1 OK] {email}: new access token acquired")
    
    # ── Layer 2: RT rotation — check actual JWT expiry, not day counter ──
    rt_to_check = new_rt_same if new_rt_same else refresh_token
    rt_exp = _get_token_expiry(rt_to_check)
    need_rotation = True
    
    if rt_exp:
        remaining = rt_exp - time.time()
        remaining_days = remaining / 86400
        if remaining > RT_ROTATE_THRESHOLD:
            need_rotation = False
            print(f"  [L2 SKIP] {email}: RT has {remaining_days:.1f} days left (> {RT_ROTATE_THRESHOLD/86400:.0f}d threshold)")
        else:
            print(f"  [L2] {email}: RT has only {remaining_days:.1f} days left — rotating...")
    else:
        print(f"  [L2] {email}: can't decode RT expiry — rotating as precaution")
    
    if need_rotation:
        # Use the FRESH access_token as the refresh_token in the payload
        at_clean = new_at[7:] if new_at.startswith("Bearer ") else new_at
        new_at_2, new_rt_rotated, rotated = _do_refresh(at_clean, at_clean, device_id)
        
        if new_at_2 and rotated:
            account["access_token"] = new_at_2
            account["refresh_token"] = new_rt_rotated
            account["last_rt_rotation"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"  [L2 OK] {email}: refresh token rotated (30-day expiry reset)")
        else:
            print(f"  [L2 FAIL] {email}: rotation failed — keeping L1 tokens (AT still valid)")
            account["refresh_token"] = new_rt_same if new_rt_same else refresh_token
    else:
        account["refresh_token"] = new_rt_same if new_rt_same else refresh_token
    
    return account


def refresh_all_accounts():
    """
    Refresh ALL accounts in accounts.json.
    Returns dict with success/fail counts.
    """
    accounts = load_accounts()
    if not accounts:
        print("[REFRESH] No accounts to refresh")
        return {"total": 0, "success": 0, "failed": 0}
    
    print(f"\n{'═' * 55}")
    print(f"  Auto-Refresh Cycle — {len(accounts)} accounts")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═' * 55}")
    
    success = 0
    failed = 0
    
    for i, acc in enumerate(accounts):
        print(f"\n  [{i+1}/{len(accounts)}] Processing...")
        updated = refresh_account(acc)
        if updated:
            accounts[i] = updated
            success += 1
        else:
            failed += 1
    
    # Save all updated tokens
    save_accounts(accounts)
    
    print(f"\n{'─' * 55}")
    print(f"  Refresh complete: {success} success, {failed} failed")
    print(f"{'─' * 55}\n")
    
    return {"total": len(accounts), "success": success, "failed": failed}


# ═══════════════════════════════════════════════════════════════
# BACKGROUND AUTO-REFRESH THREAD
# ═══════════════════════════════════════════════════════════════

class AutoRefreshThread(threading.Thread):
    """
    Background daemon thread — unified refresh cycle (laptop-off-safe).
    
    1. Runs an immediate refresh on startup (handles any downtime)
    2. Schedules unified cycle every 20 hours
    3. Each cycle: L1 (AT refresh, always) + L2 (RT rotation if < 5 days left)
    """
    
    def __init__(self, interval=AT_REFRESH_INTERVAL):
        super().__init__(daemon=True, name="AutoClaw-Refresh")
        self.interval = interval
        self._stop_event = threading.Event()
        self.last_refresh_time = None
        self.last_refresh_result = None
        self.is_running = False
        self.next_refresh_time = None
        self.on_cycle_complete = None  # callback( → called after each cycle
    
    def run(self):
        """Main loop: refresh immediately, then sleep interval, repeat."""
        self.is_running = True
        print("[AUTO-REFRESH] Thread started — running initial refresh on startup...")
        
        # Initial refresh on startup
        self._do_cycle()
        
        while not self._stop_event.is_set():
            # Wait for interval (check stop event every 60s for responsiveness)
            waited = 0
            while waited < self.interval and not self._stop_event.is_set():
                self._stop_event.wait(60)
                waited += 60
            
            if self._stop_event.is_set():
                break
            
            self._do_cycle()
        
        self.is_running = False
        print("[AUTO-REFRESH] Thread stopped")
    
    def _do_cycle(self):
        """Run one refresh cycle."""
        self.last_refresh_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.last_refresh_result = refresh_all_accounts()
        except Exception as e:
            print(f"[AUTO-REFRESH] Error during cycle: {e}")
            self.last_refresh_result = {"total": 0, "success": 0, "failed": 0, "error": str(e)}
        self.next_refresh_time = (datetime.now() + timedelta(seconds=self.interval)).strftime("%Y-%m-%d %H:%M:%S")
        # Notify router to hot-reload tokens from accounts.json
        if self.on_cycle_complete:
            try:
                self.on_cycle_complete()
            except Exception as e:
                print(f"[AUTO-REFRESH] on_cycle_complete callback error: {e}")
    
    def stop(self):
        """Signal the thread to stop."""
        self._stop_event.set()
    
    def status(self):
        """Return current status dict for dashboard."""
        return {
            "running": self.is_running,
            "last_refresh": self.last_refresh_time,
            "next_refresh": self.next_refresh_time,
            "interval_hours": self.interval / 3600,
            "last_result": self.last_refresh_result,
        }


# ═══════════════════════════════════════════════════════════════
# CLI ENTRY (for manual / testing)
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AutoClaw Auto-Refresh Module")
    parser.add_argument("--once", action="store_true", help="Run one refresh cycle and exit")
    parser.add_argument("--daemon", action="store_true", help="Run as background daemon (refresh every 20h)")
    args = parser.parse_args()
    
    if args.once:
        result = refresh_all_accounts()
        print(f"\nResult: {result}")
    elif args.daemon:
        thread = AutoRefreshThread()
        thread.start()
        print("[DAEMON] Auto-refresh running in background. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            thread.stop()
            print("\n[DAEMON] Stopped.")
    else:
        # Default: run once
        result = refresh_all_accounts()
        print(f"\nResult: {result}")
