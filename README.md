# ModelDeck

A local control panel + macOS menu-bar app for on-demand local LLM lanes.
One switch per model — turn lanes on and off, watch RAM, and chat with the
fastest one directly — all from a single local surface.

Built for Apple Silicon Macs running llama.cpp and [uzu](https://github.com/trymirai/uzu)
side by side, but the lane system is generic: any executable exposing
`start`/`stop`/`status` can be a lane.

## What it does

- **Control panel** (`web/panel.html`) — a switch per lane, live RAM, one-click
  start/stop, and a link into the streaming chat.
- **Menu-bar app** (`menubar/ModelDeck.swift`) — the same lanes in your menu
  bar, with an at-a-glance free-memory readout.
- **Streaming chat** (`web/uzu-chat.html`) — a minimal chat that talks straight
  to the uzu lane with a tiny prompt, so you see full speculative-decoding
  throughput (no agent scaffolding in the prompt).
- **One JSON API** — the panel and menu bar share a single source of truth via
  the stdlib Python server.

## Architecture

```
server/modeldeck_server.py   stdlib HTTP control backend (the source of truth)
server/config.py             config loader
lanes/*.sh                   one start/stop/status script per model lane
web/panel.html               control panel UI
web/uzu-chat.html            streaming chat UI
menubar/ModelDeck.swift      native menu-bar app (AppKit)
menubar/build.sh             compiles the binary + .app bundle
config/config.example.json   every machine-specific value lives here
```

No dependencies beyond Python 3 stdlib for the server, and Swift for the menu
bar. The web assets are plain HTML/CSS/JS.

## Quick start

```bash
# 1. configure
cp config/config.example.json config/config.local.json
$EDITOR config/config.local.json   # set your model paths, ports, ramtoggle path

# 2. run the control server
./run.sh                          # serves on 127.0.0.1:8790

# 3. (optional) build + launch the menu-bar app
./menubar/build.sh
open -a ModelDeck
```

Then open <http://127.0.0.1:8790> for the panel.

## Configuration

Everything machine-specific lives in `config/config.example.json`, which is
copied to `config/config.local.json` (gitignored) and edited. Each lane declares
its port, approximate RAM cost, and the script that controls it:

```json
{
  "server":  { "host": "127.0.0.1", "port": 8790 },
  "ramtoggle": "~/.local/bin/ramtoggle",
  "lanes": [
    { "id": "qwen-35b", "port": 8099, "approx_mb": 18000,
      "script": "lanes/qwen-lane.sh", "kind": "script" }
  ]
}
```

Any value can also be overridden per-lane via environment variables in the lane
scripts (e.g. `QWEN35B_PORT`, `MODEL_GGUF`, `UZU_CLI`).

## Adding a lane

1. Write a `lanes/my-lane.sh` exposing `start` (blocking server), `stop`, and
   `status` (prints `UP`/`DOWN`).
2. Add an entry to `config/config.local.json` with an `id`, `port`, `approx_mb`,
   and the `script` path.
3. Restart the server. The lane appears in the panel and menu bar automatically.

## Boot persistence

See `docs/launchd.md` for a launchd template that starts the server at login.

## License

MIT — see `LICENSE`.
