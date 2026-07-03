# AutoClaw CHANGELOG

## 2026-07-04 — Unified Refresh + Auto-Reload + Non-Blocking Manual Refresh

### Changed
- **Merged L1 + L2 into single unified cycle** (every 20h, both layers in one pass)
- **L2 RT rotation: JWT-expiry-based** (rotate if < 5 days remaining) — replaces old 25-day counter
- Old `RT_ROTATION_INTERVAL = 25 * 24 * 3600` → removed
- New `RT_ROTATE_THRESHOLD = 5 * 24 * 3600` — rotate RT if less than 5 days left
- New `_get_token_expiry()` helper — decodes JWT to check actual RT expiry
- `refresh_account()` rewritten: L1 always runs, L2 conditional on JWT expiry
- On startup: immediate cycle runs → handles any downtime (laptop off scenario)
- Updated docstrings (refresh.py header, AutoRefreshThread, router.py startup msg)

### Fixed
- **router.py encoding fix**: added `sys.stdout.reconfigure(encoding="utf-8")` — was missing, caused charmap crash on auto-refresh thread
- **Auto-reload after refresh**: AutoRefreshThread now calls `token_mgr.reload()` via `on_cycle_complete` callback after each cycle — router previously held stale tokens in memory
- **Manual refresh-now non-blocking**: was synchronous (timeout on 123 accounts ~2min), now spawns background thread + responds instantly

### Verified
- Ad-hoc: 8/8 checks PASS (code, health, refresh, LLM, manual refresh-now, callback wiring)
- 123/123 tokens valid, 0 expired
- LLM call: 200 OK
- Manual refresh-now: instant response (non-blocking)

