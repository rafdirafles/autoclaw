#!/usr/bin/env python3
"""
AutoClaw Account Registration — Google OAuth via CloakBrowser stealth automation.

Features:
- Headless stealth Chromium via CloakBrowser (anti-detect)
- Concurrent batch registration (configurable batch_size, default 5)
- Google OAuth login → intercept tokens → save to accounts.json + tokens.txt
- New accounts get ~2000 bonus points
- Supports per-email passwords (email:password format in email.txt)

Usage:
  python register.py              # Register all emails from email.txt
  python register.py --count 5    # Register only 5 accounts
  python register.py --headless   # Run headless (default: True)

No interactive CLI menu — pure batch mode for automation.
"""

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid

import requests
from cloakbrowser import launch_async

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(_DIR, "config.json")

APP_ID = "100003"
APP_KEY = "38d2391985e2369a5fb8227d8e6cd5e5"
BASE_URL = "https://autoglm-api.autoglm.ai"
PROXY_URL = f"{BASE_URL}/autoclaw-proxy/proxy/autoclaw/chat/completions"
REDIRECT_URI = f"{BASE_URL}/userapi/oauth/google/callback"

MODELS = {
    "1": {"id": "openrouter_glm-5.2", "name": "GLM-5.2 (Best)", "cost": "~3 pts"},
    "2": {"id": "zai_glm-5-turbo", "name": "GLM-5-Turbo (Cheapest)", "cost": "1 pt"},
    "3": {"id": "zai_auto", "name": "Auto/DeepSeek-V4 (Expensive)", "cost": "~7 pts"},
}


def load_config():
    defaults = {
        "password": "",
        "batch_size": 5,
        "email_file": "email.txt",
        "accounts_file": "accounts.json",
        "tokens_file": "tokens.txt",
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            defaults.update(cfg)
        except Exception:
            pass
    return defaults


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ═══════════════════════════════════════════════════════════════
# API HELPERS
# ═══════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════
# DATA I/O
# ═══════════════════════════════════════════════════════════════

def load_accounts():
    config = load_config()
    accounts_file = os.path.join(_DIR, config.get("accounts_file", "accounts.json"))
    try:
        with open(accounts_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_accounts(accounts):
    config = load_config()
    accounts_file = os.path.join(_DIR, config.get("accounts_file", "accounts.json"))
    tokens_file = os.path.join(_DIR, config.get("tokens_file", "tokens.txt"))
    with open(accounts_file, "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2, ensure_ascii=False)
    with open(tokens_file, "w", encoding="utf-8") as f:
        for acc in accounts:
            token = acc.get("access_token", "")
            if token:
                f.write(f"{token}\n")


# ═══════════════════════════════════════════════════════════════
# API FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def get_google_oauth_url(device_id):
    """Get Google OAuth URL from AutoClaw API."""
    url = f"{BASE_URL}/userapi/overseasv1/google-oauth-url"
    body = {
        "device_id": device_id,
        "source_id": "web",
        "navigate_uri": REDIRECT_URI,
        "client_type": "web",
    }
    resp = requests.post(url, json=body, headers=get_auth_headers(), timeout=15)
    data = resp.json()
    if data.get("code") == 0:
        return data["data"]["oauth_url"], data["data"]["state"]
    return None, None


def get_wallet_balance(access_token):
    """Get wallet balance for a token."""
    url = f"{BASE_URL}/agent-assetmgr/api/v2/wallets?biz_app_id=autoclaw"
    headers = get_auth_headers()
    if access_token.startswith("Bearer "):
        access_token = access_token[7:]
    headers["authorization"] = f"Bearer {access_token}"
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        data = resp.json()
        if data.get("code") == 0:
            return data["data"].get("total_balance", "N/A")
    except Exception:
        pass
    return "N/A"


def test_chat_silent(access_token, model="openrouter_glm-5.2", prompt="Hello"):
    """Test chat completion silently (no streaming output)."""
    if access_token.startswith("Bearer "):
        access_token = access_token[7:]
    ts = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-Authorization": f"Bearer {access_token}",
        "X-Request-Id": str(uuid.uuid4()),
        "X-Request-Model": model,
        "X-Auth-Appid": APP_ID,
        "X-Auth-Timestamp": ts,
        "X-Auth-Sign": generate_sign(ts),
        "X-Product": "autoclaw",
        "X-Version": "1.10.0",
        "X-Tm": "web",
        "X-Trace-Id": str(uuid.uuid4()),
    }
    body = {
        "model": "x",
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "temperature": 0.7,
    }
    try:
        resp = requests.post(PROXY_URL, json=body, headers=headers, stream=True, timeout=15)
        if resp.status_code != 200:
            return None, resp.text
        full = ""
        for line in resp.iter_lines():
            if line:
                line = line.decode("utf-8")
                if line.startswith("data: "):
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta", {})
                        full += delta.get("content", "")
                    except Exception:
                        pass
        return full if full else None, None
    except Exception as e:
        return None, str(e)


# ═══════════════════════════════════════════════════════════════
# REGISTER FUNCTION
# ═══════════════════════════════════════════════════════════════

async def register_autoclaw(email, password, browser):
    """Register AutoClaw account via Google OAuth using CloakBrowser."""
    device_id = str(uuid.uuid4())
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    )
    page = await context.new_page()

    tokens = {"access_token": None, "refresh_token": None}

    async def handle_response(response):
        try:
            url = response.url
            # Intercept token responses from multiple possible endpoints
            if any(ep in url for ep in [
                "/userapi/v1/refresh",
                "/userapi/overseasv1/google-oauth-login",
                "/userapi/oauth/google/callback",
                "/userapi/v1/oauth",
                "/userapi/v1/google",
            ]):
                try:
                    body = await response.json()
                    if body.get("code") == 0 and body.get("data", {}).get("access_token"):
                        tokens["access_token"] = body["data"]["access_token"]
                        tokens["refresh_token"] = body["data"].get("refresh_token", "")
                        print(f"\033[32m  [✓] Token intercepted!\033[0m")
                except Exception:
                    pass  # Not JSON or empty body — skip
            # Broad catch: any response body containing access_token
            if not tokens["access_token"] and response.status == 200:
                try:
                    body = await response.json()
                    data = body.get("data", body)
                    at = data.get("access_token") if isinstance(data, dict) else None
                    if at and at.startswith("eyJ"):
                        tokens["access_token"] = at
                        tokens["refresh_token"] = data.get("refresh_token", "")
                        print(f"\033[32m  [✓] Token intercepted (broad match)!\033[0m")
                except Exception:
                    pass
        except Exception:
            pass

    page.on("response", handle_response)

    try:
        print(f"\n\033[36m  ┌─ Processing: {email}\033[0m")
        print(f"  │  Device ID: {device_id[:20]}...")

        # Step 1: Get OAuth URL
        print(f"  ├─ Getting OAuth URL...")
        oauth_url, state = get_google_oauth_url(device_id)
        if not oauth_url:
            print(f"\033[31m  └─ ✖ Failed to get OAuth URL\033[0m")
            return None

        # Step 2: Google OAuth
        print(f"  ├─ Opening Google login...")
        await page.goto(oauth_url)
        await asyncio.sleep(2)

        # Step 3: Enter email
        print(f"  ├─ Entering email...")
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(2)
        email_input = page.locator('#identifierId')
        if not await email_input.is_visible(timeout=3000):
            email_input = page.locator('input[type="email"]')
        if not await email_input.is_visible(timeout=3000):
            email_input = page.locator('input[name="identifier"]')
        await email_input.click()
        await email_input.type(email, delay=50)
        await asyncio.sleep(1)
        next_btn = page.locator('#identifierNext')
        if await next_btn.is_visible(timeout=3000):
            await next_btn.click()
        else:
            await page.keyboard.press("Enter")
        await asyncio.sleep(3)

        # Step 4: Enter password
        print(f"  ├─ Entering password...")
        await page.wait_for_selector('input[type="password"]', timeout=10000)
        await asyncio.sleep(1)
        password_input = page.locator('input[type="password"]')
        await password_input.click()
        await password_input.type(password, delay=50)
        await asyncio.sleep(1)
        next_btn = page.locator('#passwordNext')
        if await next_btn.is_visible(timeout=3000):
            await next_btn.click()
        else:
            await page.keyboard.press("Enter")
        await asyncio.sleep(3)

        # Step 5: Handle workspace terms
        await asyncio.sleep(2)
        if "workspacetermsofservice" in page.url or "speedbump" in page.url:
            print(f"  ├─ Accepting terms...")
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1)
            try:
                btn = page.locator('button:has-text("I understand"), button:has-text("Saya mengerti"), input[type="submit"]').first
                await btn.wait_for(state="visible", timeout=5000)
                await btn.click()
                await asyncio.sleep(3)
            except Exception:
                pass

        # Step 6: Handle consent
        try:
            continue_btn = page.locator('button:has-text("Lanjutkan"), button:has-text("Continue"), button:has-text("Allow")')
            await continue_btn.first.wait_for(state="visible", timeout=15000)
            print(f"  ├─ Clicking consent...")
            await continue_btn.first.click()
            await asyncio.sleep(3)
        except Exception:
            pass

        # Step 7: Wait redirect
        print(f"  ├─ Waiting for redirect...")
        try:
            await page.wait_for_url("**/autoclaw.z.ai/**", timeout=30000)
        except Exception:
            pass

        # Step 8: Wait for token
        print(f"  ├─ Waiting for token...")
        for _ in range(15):
            if tokens["access_token"]:
                break
            await asyncio.sleep(1)

        # Fallback 1: Extract auth code from URL and exchange via API
        if not tokens["access_token"]:
            current_url = page.url
            if "code=" in current_url:
                print(f"  ├─ Found OAuth code in URL, exchanging...")
                try:
                    from urllib.parse import urlparse, parse_qs
                    parsed = urlparse(current_url)
                    params = parse_qs(parsed.query)
                    auth_code = params.get("code", [None])[0]
                    if auth_code:
                        exchange_url = f"{BASE_URL}/userapi/oauth/google/callback"
                        resp = requests.get(exchange_url, params={"code": auth_code}, timeout=15)
                        data = resp.json()
                        if data.get("code") == 0 and data.get("data", {}).get("access_token"):
                            tokens["access_token"] = data["data"]["access_token"]
                            tokens["refresh_token"] = data["data"].get("refresh_token", "")
                            print(f"\033[32m  [✓] Token exchanged from auth code!\033[0m")
                except Exception as e:
                    print(f"  ├─ Code exchange failed: {e}")

        # Fallback 2: localStorage
        if not tokens["access_token"]:
            print(f"  ├─ Trying localStorage...")
            await asyncio.sleep(3)
            storage_data = await page.evaluate('''() => {
                let result = {};
                for (let i = 0; i < localStorage.length; i++) {
                    let key = localStorage.key(i);
                    let val = localStorage.getItem(key);
                    if (val && (val.includes("eyJ") || key.toLowerCase().includes("token"))) {
                        result[key] = val;
                    }
                }
                return result;
            }''')
            if storage_data:
                for key, val in storage_data.items():
                    if "Bearer" in val or val.startswith("eyJ"):
                        if "refresh" in key.lower():
                            tokens["refresh_token"] = val
                        else:
                            tokens["access_token"] = val

        if tokens["access_token"]:
            access_token = tokens["access_token"]
            refresh_tok = tokens["refresh_token"] or ""
            # Ensure "Bearer " prefix
            if not access_token.startswith("Bearer "):
                access_token = f"Bearer {access_token}"
            if refresh_tok and not refresh_tok.startswith("Bearer "):
                refresh_tok = f"Bearer {refresh_tok}"
            balance = get_wallet_balance(access_token)

            print(f"\033[32m  ├─ ✓ Registered successfully!\033[0m")
            print(f"\033[32m  ├─ ✓ Balance: {balance} points\033[0m")

            accounts = load_accounts()
            accounts.append({
                "email": email,
                "device_id": device_id,
                "access_token": access_token,
                "refresh_token": refresh_tok,
                "balance": balance,
                "registered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            save_accounts(accounts)
            print(f"\033[32m  └─ ✓ Saved!\033[0m")

            # Remove from email.txt
            config = load_config()
            email_file = os.path.join(_DIR, config.get("email_file", "email.txt"))
            if os.path.exists(email_file):
                with open(email_file, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                with open(email_file, "w", encoding="utf-8") as f:
                    for line in lines:
                        if line.strip().split(":")[0] != email:
                            f.write(line)

            return access_token
        else:
            print(f"\033[31m  └─ ✖ Failed to get tokens\033[0m")
            print(f"       URL: {page.url}")
            return None

    except Exception as e:
        print(f"\033[31m  └─ ✖ Error: {e}\033[0m")
        return None
    finally:
        await context.close()


# ═══════════════════════════════════════════════════════════════
# BATCH REGISTER (main entry point)
# ═══════════════════════════════════════════════════════════════

async def batch_register(count=None, headless=True):
    """
    Register accounts from email.txt in batches.
    
    Args:
        count: Number of accounts to register (None = all in email.txt)
        headless: Run browser headless (default True)
    """
    config = load_config()
    password = config.get("password", "")
    batch_size = config.get("batch_size", 5)
    email_file = os.path.join(_DIR, config.get("email_file", "email.txt"))

    if not os.path.exists(email_file):
        print(f"\033[31m[!] {email_file} not found!\033[0m")
        return

    with open(email_file, "r", encoding="utf-8") as f:
        raw_lines = [line.strip() for line in f if line.strip() and "@" in line]

    # Parse email:password format
    emails = []
    email_passwords = {}
    for line in raw_lines:
        if ":" in line:
            parts = line.split(":", 1)
            email = parts[0].strip()
            pw = parts[1].strip()
            if pw:
                email_passwords[email] = pw
        else:
            email = line
        emails.append(email)

    if not emails:
        print(f"\033[31m[!] No emails in {email_file}\033[0m")
        return

    if count:
        emails = emails[:count]

    print(f"\n033[36m{'═' * 55}\033[0m")
    print(f"\033[36m  📝 AUTO REGISTER — {len(emails)} accounts\033[0m")
    print(f"\033[36m{'═' * 55}\033[0m")
    print(f"  Emails found  : {len(emails)}")
    print(f"  Batch size    : {batch_size}")
    print(f"  Headless      : {headless}")
    print(f"  Per-email pwd : {len(email_passwords)} emails with individual password")
    if not email_passwords:
        print(f"  Global pwd    : {password}")
    print()

    browsers = []
    for i in range(0, len(emails), batch_size):
        batch = emails[i:i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (len(emails) + batch_size - 1) // batch_size
        print(f"\n\033[36m  ═══ Batch {batch_num}/{total_batches} ({len(batch)} accounts) ═══\033[0m")

        tasks = []
        for email in batch:
            browser = await launch_async(headless=headless)
            browsers.append(browser)
            email_pw = email_passwords.get(email, password)
            task = register_autoclaw(email, email_pw, browser)
            tasks.append(task)

        await asyncio.gather(*tasks)

        for b in browsers:
            try:
                await b.close()
            except Exception:
                pass
        browsers = []

        if i + batch_size < len(emails):
            await asyncio.sleep(3)

    accounts = load_accounts()
    print(f"\n\033[32m  ✓ Done! Total accounts: {len(accounts)}\033[0m")


# ═══════════════════════════════════════════════════════════════
# CLI ENTRY
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AutoClaw Account Registration")
    parser.add_argument("--count", type=int, default=None, help="Number of accounts to register (default: all)")
    parser.add_argument("--no-headless", action="store_true", help="Show browser window (default: headless)")
    args = parser.parse_args()

    asyncio.run(batch_register(count=args.count, headless=not args.no_headless))
