# Running ModelDeck at login (launchd)

The control server is a long-running process. Start it at login with a launchd
agent. Save this as `~/Library/LaunchAgents/com.modeldeck.server.plist`, editing
the paths for your machine, then:

```bash
launchctl load ~/Library/LaunchAgents/com.modeldeck.server.plist
```

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.modeldeck.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/YOU/projects/modeldeck/server/modeldeck_server.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/YOU/.modeldeck/server.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOU/.modeldeck/server.log</string>
</dict>
</plist>
```

For the menu-bar app, add `/Applications/ModelDeck.app` to
**System Settings → General → Login Items**.
