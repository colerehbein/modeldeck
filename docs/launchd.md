# Running Thrash at login (launchd)

The control server is a long-running process. Start it at login with a launchd
agent.

**Recommended:** copy `config/config.example.json` to `config/config.local.json`
and edit it for machine-specific overrides (model paths, ports, the `ramtoggle`
path). The server starts with just the example config if you don't — a local
file is only needed to override the defaults.

```bash
mkdir -p ~/.thrash          # launchd will not create the log directory
```

Save this as `~/Library/LaunchAgents/com.thrash.server.plist`, editing the
paths for your machine, then:

```bash
launchctl load ~/Library/LaunchAgents/com.thrash.server.plist
```

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.thrash.server</string>
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
    <string>/Users/YOU/.thrash/server.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOU/.thrash/server.log</string>
</dict>
</plist>
```

For the menu-bar app, add `/Applications/Thrash.app` to
**System Settings → General → Login Items**.
