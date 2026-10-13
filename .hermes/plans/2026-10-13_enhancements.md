# Thrash — 10 Enhancements Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Turn Thrash from a working control panel into complete, adoptable software by adding a test suite, a CLI, idle auto-unload, RAM-guard, log viewer, chat model-picker, doctor, notifications, graceful drain, and one-command install.

**Architecture:** All Python logic lives in a new shared `core.py` (config, lane control, status, doctor checks) so the server, the CLI, and the TTL reaper all call the same functions. The HTTP server stays thin (JSON + SSE proxy). Web additions are new DOM nodes + JS, additive only, in `web/panel.html` and `web/uzu-chat.html`. Tests use Python's stdlib `unittest` (no new deps) and drive real subprocesses against stub lanes.

**Tech Stack:** Python 3.9+ stdlib (unittest, urllib, subprocess, threading), bash (lane scripts, install.sh), Swift (menu-bar, existing), HTML/JS (panel + chat). No new dependencies — this is a hard constraint.

**Repo:** `~/projects/thrash` (branch `feat/enhancements` off `main`)

---

## Ground Rules (every implementer MUST follow)

1. **Read the file you're editing first** (`read_file`). Never overwrite from memory.
2. **TDD** for every Python task: write the failing test in `tests/`, run it, watch it fail, implement, watch it pass.
3. **No new dependencies.** Stdlib only. `unittest`, not pytest.
4. **Commit after each task** with a conventional-commit message.
5. **Never delete** user files; quarantine to `~/.hermes/quarantine/` if ever needed.
6. **Preserve existing behavior** — all existing endpoints/tests must keep passing.
7. Run the full suite (`python3 -m unittest discover -s tests -v`) before every commit; it must stay green.
8. Style: match the existing server (stdlib, no f-string overkill, `# noqa: BLE001` where broad excepts are intentional).

---

## Progress

- [x] T1 test infra + config/lane tests — commit 787b110, reviewed, 24 tests
- [x] T5 config new optional fields — commit 48d94f6 + 7d75571 (review fixes), reviewed
- [x] T1b test fixes (blocking-start stub, ramtoggle round-trip, feature-block validation) — commit b8c851f, 42 tests green, no orphans
- [ ] T2 extract core.py + refactor server  ← IN PROGRESS
- [ ] T3–T14 pending

---

## Dependency Graph (execution waves)

```
W1 (parallel, disjoint files):
  T1  tests/ infra: helpers + config + toggle/lane-control tests
  T5  config.py + config.example.json (new optional fields, defaulted)

W2 (needs T1+T5):  T2  core.py extraction + refactor server + config/cli tests
W3 (needs T2):
  T3  server.py: /api/logs + /api/config
  T7  panel.html: log viewer + RAM projection bar
  T8  uzu-chat.html: model picker
  T10 lanes/uzu-9b-lane.sh + reaper.py: idle auto-unload (TTL)
W4 (needs T2):
  T4  cli.py: thrash CLI + tests
  T6  RAM-projection guard in core.py + tests
W5 (needs T2):
  T9  doctor.py + tests
  T11 notifications.py (macOS osascript) + tests
W6 (needs T2):
  T12 graceful drain in core.py + stop paths + tests
W7 (last):       T13 install.sh + docs + smoke
W8 (final):      integration review
```

**File-ownership is disjoint within each wave** — no two parallel children edit the same file.

---

## TASKS

### Task 1: Test infrastructure + config + lane-control tests

**Objective:** Stand up `tests/` with a working harness and cover `config.py` + the lane-control contract (`script_lane_on`, `run`, `toggle`) using stub lane scripts and a stub ramtoggle.

**Files:**
- Create: `tests/__init__.py` (empty)
- Create: `tests/helpers.py`
- Create: `tests/test_config.py`
- Create: `tests/test_lanes.py`

**Details:**

`tests/helpers.py` must provide:
- `make_lane_script(dir, port, name)` — writes an executable bash script that supports `start`/`stop`/`status`, where `status` prints `UP on :<port>` if a marker file exists else `DOWN`, and `start` creates the marker, `stop` removes it. (This lets tests drive lane on/off with no real model.)
- `make_ramtoggle_stub(dir)` — writes an executable script that on `--json` prints `{"memory":{...},"lanes":[{...}]}` and on `on|off <id>` prints `toggled <id>`.
- `write_config(dir, overrides)` — writes a `config.local.json` into a temp `config/` dir pointing `ramtoggle` + `lanes` at the stubs, and returns the dir. Tests import `config` with `CONFIG_DIR`/`LOCAL` monkeypatched via `config_mod.CONFIG_DIR` and `config_mod.LOCAL`.

`tests/test_config.py` — cover: default load (example only, no local) has required keys; deep-merge keeps `server.host` when local overrides only `server.port`; `_expand` leaves `~` in labels but expands `~` in `ramtoggle`/`script`; validation raises SystemExit on missing `server.host`; minimal lane entry (id+port+script) gets label/impact defaults.

`tests/test_lanes.py` — import the lane functions from `thrash_server` (guard the `__main__` server start behind `if __name__` — it already is, so import is safe). Cover: `script_lane_on` returns True after stub `start`, False after `stop`; `run()` returns (rc, stdout, stderr) split; `toggle()` on a script lane with a missing script returns `(False, ...)` mentioning the error, not a crash; `toggle()` unknown id returns `(False, "unknown lane...")`.

**Verify:** `python3 -m unittest discover -s tests -v` — all pass. Commit:
`test: add test harness + config and lane-control coverage`

---

### Task 2: Extract shared core.py + refactor server onto it

**Objective:** Move all non-HTTP logic (config access, `run`, `script_lane_on`, `ramtoggle_status`, `get_status`, `toggle`, `uzu_proxy_port`) into `server/core.py`, and make `thrash_server.py` import from it. CLI, doctor, reaper, and tests all build on `core`. **Behavior must be identical.**

**Files:**
- Create: `server/core.py`
- Modify: `server/thrash_server.py` (replace inline functions with `from core import ...`)
- Create: `tests/test_core.py` (assert `core.get_status()` shape + `core.toggle()` behavior via stubs, mirroring T1)

**Details:**
- `core.py` exposes: `CFG`, `PORT`, `HOST`, `RAMTOGGLE`, `SCRIPT_LANES`, `UZU_PROXY_LANE`, `run()`, `script_lane_on()`, `ramtoggle_status()`, `get_status()`, `toggle()`, `uzu_proxy_port()`, plus `known_lane_ids()`.
- Move the module-level config load (`CFG = config_mod.load()` etc.) into `core.py`.
- `thrash_server.py` keeps only: `_load_asset`, `PANEL_HTML`/`UZU_CHAT_HTML`, the `Handler` class, and `__main__`. It imports everything else from `core`. Add `sys.path.insert(0, dirname)` then `import core` — but since `core` also does `import config`, both live in `server/`, so `sys.path` already includes it.
- Do NOT change any HTTP response shape.

**Verify:** server boots (`python3 server/thrash_server.py &`, curl `/api/health` + `/api/status`, kill it) and full suite green. Commit:
`refactor: extract shared core.py (server/cli/doctor/reaper share it)`

---

### Task 3: Server endpoints — /api/logs and /api/config

**Objective:** Add two GET endpoints the panel's log viewer and the frontends need.

**Files:**
- Modify: `server/thrash_server.py`
- Create: `tests/test_server_endpoints.py`

**Details:**
- `GET /api/logs?id=<lane_id>&lines=200` → `{"id":..., "path": <resolved log path>, "tail": "<last N lines>"}`. Resolve the log path from the lane's `log` field if present, else from env-var convention `<LANE>_LOG`, else return 404 with a helpful message. Bound `lines` to [1, 5000]. Never follow symlinks outside the lane's own log; if the file doesn't exist return `{"tail":"", "exists":false}`.
- `GET /api/config` → `{"api_base": "http://HOST:PORT", "uzu_port": <uzu_proxy_port>, "panel_port": PORT, "lanes":[{id,label,port,kind,approx_mb}]}`. This lets the frontends stop hardcoding ports.
- Tests: use a stub lane with a `log` file written with known lines; assert `/api/logs` returns the tail and respects `lines`; assert `/api/config` reflects the test config's port. Start the server in a thread on an ephemeral port for these tests (bind port 0, read `srv.server_address[1]`).

**Verify:** full suite green; manual curl of both endpoints against a running server. Commit:
`feat(server): add /api/logs and /api/config endpoints`

---

### Task 4: CLI — `thrash`

**Objective:** A scriptable terminal interface so the tool is usable without the GUI (and from Hermes/cron).

**Files:**
- Create: `thrash` (executable python script at repo root, `#!/usr/bin/env python3`)
- Create: `tests/test_cli.py`

**Details — subcommands:**
- `thrash status [--json]` — table of lanes (id, on/off, port, ~RAM) + memory line; `--json` emits `core.get_status()`.
- `thrash on <id>` / `thrash off <id>` — calls `core.toggle`; prints the message; exit 1 on failure.
- `thrash toggle <id>` — flip based on current state.
- `thrash logs <id> [--lines N]` — tail the lane log (reuse the `/api/logs` resolution logic — factor the path-resolution into `core.lane_log_path(id)` and `core.read_log_tail(id, lines)` in Task 3's core additions; CLI calls core directly, no HTTP needed).
- `thrash doctor` — runs the doctor checks from Task 9 (import `doctor`).
- The CLI must locate `core`/`config` regardless of cwd: insert `os.path.dirname(__file__)/server` onto `sys.path`.
- `thrash` with no args prints help + exits 2.

Tests: invoke via `subprocess` against stub config (set `THRASH_CONFIG_DIR` env or monkeypatch `config_mod.LOCAL`); assert `status --json` parses and matches core, `on`/`off` flip a stub lane, `logs` returns the tail, unknown id exits 1.

**Verify:** full suite green; run `./thrash status` live. Commit:
`feat(cli): add thrash command (status/on/off/toggle/logs/doctor)`

---

### Task 5: Config — new optional fields (TTL, log paths, doctor)

**Objective:** Add opt-in config fields the later tasks read, all with safe defaults so existing configs keep working.

**Files:**
- Modify: `server/config.py` (defaults + validation for new fields)
- Modify: `config/config.example.json` (document them)
- Modify: `tests/test_config.py` (new cases)

**Details — new optional top-level keys, all defaulted:**
- `"idle_unload": {"enabled": false, "ttl_minutes": 30, "lanes": ["*"]}` — `lanes` may be `["*"]` (all script lanes) or a list of ids.
- `"lanes[].log"` — optional explicit log path per lane.
- `"notifications": {"on_load": true, "on_low_memory": true, "low_memory_gb": 6}`.
- `"ram_guard": {"mode": "warn"}` — `"warn"` | `"block"`.
Validation: `idle_unload.ttl_minutes` int > 0 if present; `low_memory_gb` number > 0; `ram_guard.mode` in the allowed set. Missing → defaults, no error. Add a `core.idle_unload_cfg()` accessor returning the dict with defaults applied.

Tests: default config has `idle_unload.enabled == False`; explicit values survive merge; bad `ram_guard.mode` raises SystemExit.

**Verify:** full suite green. Commit:
`feat(config): add idle_unload, notifications, ram_guard, per-lane log`

---

### Task 6: RAM-projection guard

**Objective:** Before starting a lane, project `free - lane_cost` and warn or block per `ram_guard.mode`.

**Files:**
- Modify: `server/core.py` (add projection to `toggle()` start path)
- Create: `tests/test_ram_guard.py`

**Details:**
- Add `core.project_start(lane)` → `(projected_free_mb, would_thrash: bool)` using `get_status()["memory"]["free_mb"]` and the lane's `approx_mb`. `would_thrash` = projected < some floor (use 1500 MB) OR projected/total < 10%.
- In `toggle()` script-lane start: if `would_thrash` and mode == `block`, return `(False, "refused: starting <id> would leave <X>MB free (needs ~<cost>MB)")` and do NOT spawn. If mode == `warn` (default), spawn but prefix the message with a `WARNING:`.
- Keep the existing success message format otherwise.
- Tests: stub a lane with a huge `approx_mb` and low free memory (stub ramtoggle memory); assert `block` refuses and does not create the marker; `warn` proceeds and message contains WARNING; a small lane with plenty of RAM proceeds cleanly in both modes.

**Verify:** full suite green. Commit:
`feat(core): RAM-projection guard (warn/block before overloading)`

---

### Task 7: Panel — log viewer + RAM projection bar

**Objective:** Surface the new backend capabilities in the panel.

**Files:**
- Modify: `web/panel.html` (additive only — new button + drawer + a projection hint under the memory bar)

**Details:**
- Add a small **"logs"** button per lane card that opens a drawer/modal showing `GET /api/logs?id=<id>` output (monospace, scrollable, auto-ish). Reuse the existing `esc`/DOM-build style already in the file (no innerHTML with user data).
- Under the memory bar, when a lane is `loading`, show a projected post-start free amount: fetch `/api/config` once at load, and compute `free - approx_mb` for the pending lane, rendering "after start: ~X GB free" (green/red). Keep it simple and additive.
- Use `location.origin` for all fetches (already the pattern). No new deps.
- Manual verify: load the panel from a running server, open a lane's logs, see the projection hint. (Automated JS tests are out of scope — note this as manually verified.)

**Verify:** boot server, screenshot/inspect via browser that the logs drawer opens and projection renders. Commit:
`feat(panel): per-lane log viewer + RAM projection hint`

---

### Task 8: Chat — model picker

**Objective:** Let the chat talk to any loaded lane, not just the hardcoded uzu one.

**Files:**
- Modify: `web/uzu-chat.html`
- Modify: `server/thrash_server.py` (make `/api/uzu-chat` accept a `lane` or `port` in the body)
- Modify: `tests/test_server_endpoints.py` (proxy honors the requested port)

**Details:**
- `/api/uzu-chat` body may include `"port": <int>` (or `"lane": "<id>"`); if present and it resolves to a known script lane, proxy to that lane's port instead of `uzu_proxy_lane`. Default unchanged. Validate the port belongs to a configured lane (reject arbitrary ports → 400).
- Chat UI: a `<select>` populated from `/api/config` lanes (id + label), defaulting to the uzu lane. On send, include the chosen lane's id. Show which lane is active in the header. Disable lanes that aren't `on` (from `/api/status`), or just attempt and show the proxy error (the error path already surfaces it).
- Keep the existing streaming + tok/s logic intact.
- Verify manually in-browser: switch the picker, confirm the request targets the chosen lane (check the backend log or the response).

**Verify:** full suite green + manual browser check. Commit:
`feat(chat): pick any loaded lane, not just the uzu default`

---

### Task 9: `thrash doctor`

**Objective:** One command that diagnoses the whole setup.

**Files:**
- Create: `server/doctor.py`
- Create: `tests/test_doctor.py`

**Details — checks (each returns `{name, ok, detail}`):**
- config loads without error
- every script lane's `script` path exists and is executable
- every configured port is either free or owned by that lane (no collisions between two lanes)
- `ramtoggle` path exists (if configured)
- launchd plists present & loaded for the labels in `docs/launchd.md` (best-effort: check `~/Library/LaunchAgents/com.cole.thrash*.plist` exist; don't fail hard if launchctl unavailable)
- `uzu_cli` / `llama-server` referenced by lane env defaults exist (best-effort warn)
`core.run_doctor()` returns `(checks, all_ok)`. Exit 0 if all_ok else 1. Print a readable report (✓/✗ with detail). The CLI `doctor` subcommand (Task 4) calls this.

Tests: with stub config, a missing script → that check fails; duplicate port → collision check fails; all-good stub → all_ok True.

**Verify:** full suite green; `./thrash doctor` live. Commit:
`feat(doctor): setup diagnostics (config, scripts, ports, launchd)`

---

### Task 10: Idle auto-unload (TTL reaper)

**Objective:** Stop a lane after N minutes of no requests, so ad-hoc use can't leak RAM.

**Files:**
- Create: `server/reaper.py`
- Modify: `lanes/uzu-9b-lane.sh` (and note the pattern; other llama lanes already support `status`) — add a `last_request` touchfile write on any `/v1` hit is NOT possible without a proxy; instead the reaper polls lane `status` + checks request activity via the lane's own server. **Simplest robust approach:** the reaper checks, per enabled lane, whether it's `on` AND its port has had no new connections for `ttl_minutes` (use `lsof` timestamp is unreliable) — so instead track activity in-process: **the Thrash server already sees chat requests**; extend that. Add a `core.touch_activity(lane_id)` called from `/api/uzu-chat` and `/api/toggle` (on=true), storing last-active epoch in memory; the reaper thread stops any enabled lane whose `now - last_active > ttl` and which is currently `on`.
- Create: `tests/test_reaper.py`
- Modify: `server/thrash_server.py` (start the reaper thread if `idle_unload.enabled`; call `touch_activity` in toggle/chat)

**Details:**
- `reaper.Reaper(cfg)` with `start()` (daemon thread, checks every 60s) and `consider(lane_id, is_on)`; unit-test the decision logic without threads (`should_unload(lane_id, now, last_active, ttl)`).
- `core.ACTIVITY = {}` dict `lane_id -> epoch`; `core.touch_activity(id)` sets `now`; `core.last_active(id)` reads it. Toggle-on and chat both touch.
- Only script lanes participate; ramtoggle lanes are excluded (managed elsewhere).
- Config: `idle_unload.enabled/ttl_minutes/lanes` (Task 5). Default disabled — zero behavior change unless opted in.
- Tests: `should_unload` True when idle > ttl and on; False when recently active; False when lane not in enabled list; reaper integration with a stub lane: set activity in the past, run one `consider`, assert the stub's marker file is gone (stopped).

**Verify:** full suite green; with a stub lane, enable TTL=0-ish (or inject old activity) and watch it stop. Commit:
`feat(reaper): idle auto-unload (TTL) to stop RAM leaks`

---

### Task 11: Notifications (macOS)

**Objective:** Native notification when a lane finishes loading and when RAM goes low.

**Files:**
- Create: `server/notify.py`
- Create: `tests/test_notify.py`

**Details:**
- `notify.send(title, message)` — shells out to `osascript -e 'display notification ...'`. Must be **best-effort and never raise** (wrap in try/except, return bool). On non-macOS or failure, return False silently.
- Hook points (guarded by `notifications` config from Task 5, default on for load): in `toggle()` when a script lane start is spawned, schedule a background check that waits until `script_lane_on` is True (poll up to ~120s) then `notify.send("Thrash", "<id> is up")`. Low-memory: a lightweight watcher thread (only if `on_low_memory`) that every 60s checks free < `low_memory_gb` and notifies at most once per crossing.
- Tests: monkeypatch `subprocess.run`; assert `send` builds the right osascript argv and swallows errors; assert the load-notify scheduler calls send after the stub lane reports up (use a fast-poll test seam). Don't actually fire osascript in tests.

**Verify:** full suite green; manual: start a lane, see the notification (on macOS). Commit:
`feat(notify): macOS notifications on lane load + low memory`

---

### Task 12: Graceful drain on stop

**Objective:** Let in-flight requests finish before killing a lane.

**Files:**
- Modify: `server/core.py` (stop path)
- Create: `tests/test_drain.py`

**Details:**
- Add `core.active_requests` counter; increment around the `/api/uzu-chat` proxy streaming (in the server) and expose `core.begin_request(id)/end_request(id)`.
- In `toggle()` script-lane **stop**: instead of immediate `<script> stop`, first send SIGTERM to the lane's server process (the lane script's `stop` already does `pkill`; extend the contract: add a `drain` verb to lane scripts that sends SIGTERM and waits). To keep it simple and portable: the stop path calls `<script> stop` but the **lane scripts' stop** should prefer a graceful signal. Update `lanes/*.sh` `stop` to `pkill -TERM` (SIGTERM) rather than default SIGTERM-from-pkill (pkill already sends SIGTERM by default — so mostly this is: wait briefly, then SIGKILL as fallback). Concretely, in each lane script `stop`: `pkill -TERM -f "<anchored pattern>"; sleep 3; pkill -KILL -f "<anchored pattern>" 2>/dev/null || true`.
- In `core`, before stopping, if `core.active_requests.get(id,0) > 0`, wait up to `drain_timeout` (config `ram_guard`-adjacent, default 15s) for it to hit 0, then stop; if still active, stop anyway but note it.
- Tests: with the request counter > 0 and a stub that never decrements within the test timeout (use a tiny timeout via injected clock/seam), assert stop still eventually proceeds; with counter 0, stop is immediate. Stub lane's `stop` removes the marker as before.

**Verify:** full suite green; manual stop of a lane mid-chat completes the stream then stops. Commit:
`feat(core): graceful drain — finish in-flight requests before stop`

---

### Task 13: One-command install + docs

**Objective:** `./install.sh` sets up config, builds the app, installs launchd.

**Files:**
- Create: `install.sh`
- Modify: `README.md` (install section + feature list)
- Modify: `docs/launchd.md` (point at install.sh)

**Details — `install.sh`:**
- Idempotent. Steps: (1) `cp -n config/config.example.json config/config.local.json` (don't clobber existing); (2) build the Swift app via `menubar/build.sh` (skip gracefully if `swiftc` missing, with a message); (3) install launchd plists — generate them from templates using `$HOME` and the repo path, `launchctl bootstrap` the current GUI session; (4) print next steps.
- `--uninstall` flag: bootout the two labels, move plists to quarantine (never delete).
- Guard every mutating step behind a `--yes` or interactive confirm unless `THRASH_ASSUME_YES=1`.
- README: add an "Install" section near the top (`git clone && ./install.sh`), and update the feature bullets to mention CLI, TTL, doctor, notifications, log viewer, chat picker.

**Verify:** run `THRASH_ASSUME_YES=1 ./install.sh` in a way that doesn't disturb the *running* system — actually, test `--help` and a dry-run path; do a real bootstrap only of the (already-correct) labels, confirming idempotence (second run is a no-op). Commit:
`feat(install): one-command install/uninstall + docs`

---

### Task 14: Final integration review

**Objective:** Verify everything works together; no regressions; suite green; live system healthy.

**Checks:** full `unittest` suite green; `./thrash status`, `./thrash doctor`, panel loads with logs + projection, chat picker works, TTL off by default (no behavior change), install idempotent. Produce a short integration report.

---

## Risks / Trade-offs

- **TDD against subprocesses** is slower and slightly flaky-prone; mitigate with generous timeouts and stub scripts that are pure bash + marker files (no real models).
- **Refactor (Task 2)** is the riskiest — it must be behavior-preserving. Gate it behind the T1 tests + a live server smoke test.
- **TTL reaper** could surprise users by unloading a lane mid-thought; default OFF, and only unload lanes explicitly idle past TTL. Announce via notification.
- **Graceful drain** touches lane scripts' `stop` semantics; keep SIGTERM→wait→SIGKILL so nothing hangs.
- **Notifications** are macOS-only; degrade silently elsewhere.

## Open questions (defaulted, revisit if needed)

- TTL default when enabled: 30 min. Low-memory threshold: 6 GB. RAM floor for projection: 1.5 GB / 10%.
