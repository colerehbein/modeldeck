# Running Thrash at login (launchd)

**Use `./install.sh`** — it generates both launchd agents (`com.cole.thrash`
and `com.cole.thrash-menubar`) with your machine's paths, writes them to
`~/Library/LaunchAgents/`, and loads them. It's idempotent: re-running re-syncs
the plists and reloads cleanly.

```bash
./install.sh                 # preview the plan, no changes
./install.sh --yes           # apply
./install.sh --uninstall     # stop services, move plists to ~/.Trash/hermes
```

Logs go to `~/.hermes/logs/thrash*.log`. The plists the installer generates use
label `com.cole.thrash` (the server) and `com.cole.thrash-menubar` (the menu
bar).

The rest of this document is a manual reference if you'd rather hand-roll the
plists.

---

The control server is a long-running process. Start it at login with a launchd
agent.

**Recommended:** copy `config/config.example.json` to `config/config.local.json`
and edit it for machine-specific overrides (model paths, ports, the `ramtoggle`
path). The server starts with just the example config if you don't — a local
file is only needed to override the defaults.

```bash
mkdir -p ~/.hermes/logs      # launchd will not create the log directory
```

Save this as `~/Library/LaunchAgents/com.cole.thrash.plist`, editing the paths
for your machine, then:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.cole.thrash.plist
```

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.cole.thrash</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/YOU/projects/thrash/server/thrash_server.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/YOU/projects/thrash</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/YOU/.hermes/logs/thrash.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOU/.hermes/logs/thrash.err.log</string>
</dict>
</plist>
```

For the menu-bar app, add `/Applications/Thrash.app` to
**System Settings → General → Login Items**.
