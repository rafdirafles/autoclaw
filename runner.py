#!/usr/bin/env python3
"""
AutoClaw Services Runner — background launcher with auto-restart.

Starts router (port 31000) and dashboard (port 31001) as background
processes. Auto-restarts on crash. Can run with pythonw (no console window)
to stay running even after terminal is closed.

Usage:
  # Foreground (see logs):
  python runner.py

  # Background (no window, stays running after terminal close):
  pythonw runner.py --daemon

  # Stop services:
  python runner.py --stop

  # Status:
  python runner.py --status
"""

import subprocess
import sys
import os
import time
import socket
from pathlib import Path

# ═══════════════════════════════════════════════════════════════
# CONFIG — all scripts in the same directory now
# ═══════════════════════════════════════════════════════════════

_DIR = Path(__file__).parent
ROUTER_SCRIPT = _DIR / "router.py"
DASH_SCRIPT = _DIR / "dashboard.py"

ROUTER_PORT = 31000
DASH_PORT = 31001

PID_FILE = _DIR / "autoclaw_runner.pid"
LOG_FILE = _DIR / "autoclaw_runner.log"

SERVICES = [
    {
        "name": "router",
        "script": ROUTER_SCRIPT,
        "port": ROUTER_PORT,
        "cmd": [sys.executable, str(ROUTER_SCRIPT)],
    },
    {
        "name": "dashboard",
        "script": DASH_SCRIPT,
        "port": DASH_PORT,
        "cmd": [sys.executable, str(DASH_SCRIPT)],
    },
]

MAX_RESTARTS = 50
RESTART_DELAY = 3
SERVICE_LOG = _DIR / "service.log"


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def is_pid_alive(pid):
    """Check if a process is running (Windows-compatible)."""
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True
            )
            return str(pid) in result.stdout
        else:
            os.kill(int(pid), 0)
            return True
    except (ProcessLookupError, ValueError, OSError):
        return False


def is_port_open(port, host="localhost"):
    """Check if a port is already in use."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect((host, port))
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def wait_for_port(port, timeout=15):
    """Wait for a service to start listening on its port."""
    start = time.time()
    while time.time() - start < timeout:
        if is_port_open(port):
            return True
        time.sleep(0.5)
    return False


# ═══════════════════════════════════════════════════════════════
# RUNNER (foreground with auto-restart)
# ═══════════════════════════════════════════════════════════════

def run_foreground():
    """Run both services with auto-restart. Blocks until interrupted."""
    log("=" * 55)
    log("AutoClaw Runner — starting services with auto-restart")
    log(f"Directory: {_DIR}")
    log(f"Router:    localhost:{ROUTER_PORT}")
    log(f"Dashboard: localhost:{DASH_PORT}")
    log("=" * 55)

    # Check if ports already in use
    for svc in SERVICES:
        if is_port_open(svc["port"]):
            log(f"[WARN] Port {svc['port']} ({svc['name']}) already in use — skipping start")
            svc["process"] = None
            svc["skip"] = True
        else:
            svc["skip"] = False
            svc["restarts"] = 0
            svc["process"] = None

    # Save PID
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    try:
        while True:
            for svc in SERVICES:
                if svc.get("skip"):
                    continue

                # Start if not running
                if svc["process"] is None or svc["process"].poll() is not None:
                    if svc["process"] is not None and svc["process"].poll() is not None:
                        exit_code = svc["process"].poll()
                        log(f"[CRASH] {svc['name']} exited (code {exit_code})")

                    if svc["restarts"] >= MAX_RESTARTS:
                        log(f"[FATAL] {svc['name']} exceeded max restarts ({MAX_RESTARTS}). Giving up.")
                        svc["skip"] = True
                        continue

                    if svc["restarts"] > 0:
                        log(f"[RESTART] {svc['name']} restart #{svc['restarts']} in {RESTART_DELAY}s...")
                        time.sleep(RESTART_DELAY)

                    svc["restarts"] += 1
                    log(f"[START] {svc['name']} (attempt {svc['restarts']})")

                    # Redirect child stdout/stderr to service.log (survives crashes)
                    log_fh = open(str(SERVICE_LOG), "a", encoding="utf-8")
                    log_fh.write(f"\n{'='*55}\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting {svc['name']}\n{'='*55}\n")
                    log_fh.flush()

                    svc["process"] = subprocess.Popen(
                        svc["cmd"],
                        stdout=log_fh,
                        stderr=subprocess.STDOUT,
                        cwd=str(_DIR),
                    )

                    # Wait for port
                    timeout_val = 30 if svc["name"] == "dashboard" else 15
                    if wait_for_port(svc["port"], timeout=timeout_val):
                        log(f"[OK] {svc['name']} listening on localhost:{svc['port']} (PID {svc['process'].pid})")
                    else:
                        log(f"[WARN] {svc['name']} started but port {svc['port']} not responding yet")

                    svc["last_start"] = time.time()

            # Reset restart counter if running stably
            for svc in SERVICES:
                if svc.get("skip") or svc.get("process") is None:
                    continue
                if svc["process"].poll() is None:
                    if time.time() - svc.get("last_start", 0) > 60 and svc["restarts"] > 1:
                        log(f"[STABLE] {svc['name']} ran for 60s+, resetting restart counter")
                        svc["restarts"] = 1

            time.sleep(2)

    except KeyboardInterrupt:
        log("\n[STOP] Interrupted, shutting down services...")
    finally:
        for svc in SERVICES:
            if svc.get("process") and svc["process"].poll() is None:
                svc["process"].terminate()
                try:
                    svc["process"].wait(timeout=5)
                    log(f"[STOP] {svc['name']} terminated")
                except subprocess.TimeoutExpired:
                    svc["process"].kill()
                    log(f"[KILL] {svc['name']} force killed")
        PID_FILE.unlink(missing_ok=True)
        log("[DONE] All services stopped")


# ═══════════════════════════════════════════════════════════════
# DAEMON (background, no window)
# ═══════════════════════════════════════════════════════════════

def start_daemon():
    """Start runner as background process (pythonw, no console window)."""
    if PID_FILE.exists():
        pid = PID_FILE.read_text().strip()
        if is_pid_alive(pid):
            print(f"[RUNNING] Runner already running (PID {pid})")
            return
        PID_FILE.unlink(missing_ok=True)

    # Launch with pythonw (no window)
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    if not Path(pythonw).exists():
        pythonw = sys.executable  # fallback

    proc = subprocess.Popen(
        [pythonw, str(Path(__file__).absolute()), "--foreground-bg"],
        stdout=open(os.devnull, "w"),
        stderr=open(os.devnull, "w"),
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS if os.name == "nt" else 0,
        cwd=str(_DIR),
    )

    # Wait for PID file
    time.sleep(5)
    if PID_FILE.exists():
        pid = PID_FILE.read_text().strip()
        print(f"[OK] Runner started in background (PID {pid})")
        print(f"     Router:    localhost:{ROUTER_PORT}")
        print(f"     Dashboard: localhost:{DASH_PORT}")
        print(f"     Log:       {LOG_FILE}")
    else:
        print(f"[WARN] Runner launched (PID {proc.pid}) but PID file not found")
        print(f"       Check log: {LOG_FILE}")
        if LOG_FILE.exists():
            lines = LOG_FILE.read_text(encoding="utf-8").splitlines()[-5:]
            for line in lines:
                print(f"       {line}")


def stop_daemon():
    """Stop all autoclaw services."""
    if PID_FILE.exists():
        pid = PID_FILE.read_text().strip()
        if is_pid_alive(pid):
            try:
                subprocess.run(["taskkill", "/F", "/PID", pid, "/T"],
                             capture_output=True, timeout=5)
                print(f"[STOP] Killed runner (PID {pid}) + children")
            except Exception:
                pass
            time.sleep(1)
        PID_FILE.unlink(missing_ok=True)

    # Kill any process on our ports
    for svc in SERVICES:
        if is_port_open(svc["port"]):
            try:
                if os.name == "nt":
                    result = subprocess.run(
                        ["netstat", "-ano"], capture_output=True, text=True
                    )
                    for line in result.stdout.splitlines():
                        if f":{svc['port']}" in line and "LISTENING" in line:
                            parts = line.split()
                            if parts:
                                port_pid = parts[-1]
                                subprocess.run(["taskkill", "/F", "/PID", port_pid],
                                             capture_output=True)
                                print(f"[KILL] Killed process on port {svc['port']} (PID {port_pid})")
            except Exception as e:
                print(f"[WARN] Could not kill port {svc['port']}: {e}")

    time.sleep(2)
    print("[DONE] Services stopped")


def status():
    """Show status of services."""
    print("=" * 50)
    print("  AutoClaw Services Status")
    print("=" * 50)

    if PID_FILE.exists():
        pid = PID_FILE.read_text().strip()
        if is_pid_alive(pid):
            print(f"  Runner:    RUNNING (PID {pid})")
        else:
            print(f"  Runner:    STOPPED (stale PID file)")
            PID_FILE.unlink(missing_ok=True)
    else:
        print(f"  Runner:    STOPPED")

    for svc in SERVICES:
        if is_port_open(svc["port"]):
            print(f"  {svc['name']:10s} RUNNING (localhost:{svc['port']})")
        else:
            print(f"  {svc['name']:10s} STOPPED (localhost:{svc['port']})")

    print("=" * 50)

    if LOG_FILE.exists():
        print("\n  Recent logs:")
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()[-5:]
        for line in lines:
            print(f"  {line}")


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AutoClaw Services Runner")
    parser.add_argument("--daemon", action="store_true", help="Start as background daemon")
    parser.add_argument("--stop", action="store_true", help="Stop all services")
    parser.add_argument("--status", action="store_true", help="Show service status")
    parser.add_argument("--foreground-bg", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.status:
        status()
    elif args.stop:
        stop_daemon()
    elif args.daemon:
        start_daemon()
    elif args.foreground_bg:
        run_foreground()
    else:
        run_foreground()
