#!/usr/bin/env bash
# Thrash — one-command install / uninstall.
#
#   ./install.sh                 preview the plan (no changes) — safe to run
#   ./install.sh --yes           apply (or set THRASH_ASSUME_YES=1)
#   ./install.sh --uninstall     stop services and trash the launchd plists
#   ./install.sh --help          usage
#
# Every mutating step is gated behind --yes / THRASH_ASSUME_YES=1. Without it
# the script prints what it would do and exits 0 without touching anything.
# Re-running with --yes is idempotent: config is never clobbered, and the
# launchd labels are booted out and bootstrapped again cleanly.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

LABEL_SERVER="com.cole.thrash"
LABEL_MENUBAR="com.cole.thrash-menubar"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
PLIST_SERVER="$LAUNCH_AGENTS/$LABEL_SERVER.plist"
PLIST_MENUBAR="$LAUNCH_AGENTS/$LABEL_MENUBAR.plist"
LOG_DIR="$HOME/.hermes/logs"
TRASH_DIR="$HOME/.Trash/hermes"

ASSUME_YES="${THRASH_ASSUME_YES:-0}"
UNINSTALL=0

usage() {
    cat << 'EOF'
Thrash installer

Usage:
  ./install.sh [--yes]        Install (or re-sync): config, menu-bar build, launchd
  ./install.sh --uninstall    Stop services, move launchd plists to ~/.Trash/hermes
  ./install.sh --help         This help

Install steps (idempotent):
  1. config/config.example.json -> config/config.local.json (never overwrites)
  2. build the menu-bar app     (skipped if swiftc is missing)
  3. install launchd agents     (com.cole.thrash, com.cole.thrash-menubar)

Without --yes or THRASH_ASSUME_YES=1 the script only prints the plan — no files
are written, no services are (re)started.

Environment:
  THRASH_ASSUME_YES=1          same as --yes (non-interactive installs)

After installing, open http://127.0.0.1:8790 and run ./thrash status.
EOF
}

# ---- arg parsing ------------------------------------------------------------

while [ $# -gt 0 ]; do
    case "$1" in
        --yes|-y)           ASSUME_YES=1 ;;
        --uninstall|uninstall) UNINSTALL=1 ;;
        --help|-h|help)     usage; exit 0 ;;
        *) echo "install.sh: unknown argument: $1" >&2; echo >&2; usage >&2; exit 2 ;;
    esac
    shift
done

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }

# launchd label -> live? (read-only check, always runs)
label_running() {
    launchctl list "$1" >/dev/null 2>&1
}

# ---- uninstall ---------------------------------------------------------------

do_uninstall() {
    local acted=0
    if [ "$ASSUME_YES" != "1" ]; then
        say "Uninstall plan (nothing changed — re-run with --yes to apply):"
        say "  - bootout launchd labels: $LABEL_SERVER, $LABEL_MENUBAR"
        say "  - move to $TRASH_DIR:"
        [ -f "$PLIST_SERVER" ]  && say "      $PLIST_SERVER"
        [ -f "$PLIST_MENUBAR" ] && say "      $PLIST_MENUBAR"
        [ -f "$PLIST_SERVER" ] || [ -f "$PLIST_MENUBAR" ] || say "      (no plists found)"
        say ""
        say "  Left in place (delete by hand):"
        say "      config/config.local.json"
        say "      $HOME/.local/bin/Thrash, /Applications/Thrash.app"
        exit 0
    fi

    for label in "$LABEL_SERVER" "$LABEL_MENUBAR"; do
        if label_running "$label"; then
            launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
            say "  booted out $label"
            acted=1
        else
            say "  $label not loaded — nothing to bootout"
        fi
    done

    mkdir -p "$TRASH_DIR"
    for plist in "$PLIST_SERVER" "$PLIST_MENUBAR"; do
        if [ -f "$plist" ]; then
            local dest="$TRASH_DIR/$(basename "$plist")"
            # never clobber a previously quarantined plist
            if [ -e "$dest" ]; then
                dest="$TRASH_DIR/$(basename "$plist").$(date +%s)"
            fi
            mv "$plist" "$dest"
            say "  moved $(basename "$plist") -> $dest"
            acted=1
        else
            say "  $(basename "$plist") not present"
        fi
    done

    say ""
    if [ "$acted" = "1" ]; then
        say "Uninstalled. Plists are in $TRASH_DIR (nothing was deleted)."
    else
        say "Nothing to uninstall."
    fi
    say "To fully remove, also delete by hand:"
    say "  config/config.local.json"
    say "  $HOME/.local/bin/Thrash and /Applications/Thrash.app"
    exit 0
}

[ "$UNINSTALL" = "1" ] && do_uninstall

# ---- install plan ------------------------------------------------------------
# In preview mode this prints the plan and exits. In apply mode we skip
# straight to doing the work.

if [ "$ASSUME_YES" != "1" ]; then
    step "Thrash install"
    say "repo:    $REPO"
    say "home:    $HOME"
    say "mode:    preview (no changes)"

    step "1/3  config"
    if [ -f "$REPO/config/config.local.json" ]; then
        say "  found existing config/config.local.json — keeping it"
    else
        say "  would create config/config.local.json from config.example.json"
    fi

    step "2/3  menu-bar app"
    if command -v swiftc >/dev/null 2>&1; then
        say "  swiftc found — would run menubar/build.sh"
    else
        say "  swiftc not found — skipping menu-bar build"
        say "  (install Xcode command line tools: xcode-select --install)"
    fi

    step "3/3  launchd agents"
    say "  would write + load:"
    say "      $PLIST_SERVER"
    say "      $PLIST_MENUBAR"
    say "  logs:  $LOG_DIR"
    label_running "$LABEL_SERVER"  && say "  $LABEL_SERVER is currently loaded"  || say "  $LABEL_SERVER is not loaded"
    label_running "$LABEL_MENUBAR" && say "  $LABEL_MENUBAR is currently loaded" || say "  $LABEL_MENUBAR is not loaded"

    say ""
    say "Preview only — nothing was changed."
    say "Re-run with --yes (or THRASH_ASSUME_YES=1) to apply."
    exit 0
fi

# ---- apply -------------------------------------------------------------------

step "Thrash install"
say "repo:    $REPO"
say "home:    $HOME"
say "mode:    apply"

step "1/3  config"
if [ -f "$REPO/config/config.local.json" ]; then
    say "  keeping existing config/config.local.json"
else
    cp -n "$REPO/config/config.example.json" "$REPO/config/config.local.json"
    say "  created config/config.local.json — edit it for your machine:"
    say "      $EDITOR $REPO/config/config.local.json"
fi

step "2/3  menu-bar app"
if command -v swiftc >/dev/null 2>&1; then
    bash "$REPO/menubar/build.sh"
else
    say "  swiftc not found — skipping menu-bar build"
    say "  (install Xcode command line tools: xcode-select --install)"
fi

step "3/3  launchd agents"
mkdir -p "$LAUNCH_AGENTS" "$LOG_DIR"

# bootout first so a re-run replaces a stale registration, not stacks on it
launchctl bootout "gui/$(id -u)/$LABEL_SERVER"  2>/dev/null || true
launchctl bootout "gui/$(id -u)/$LABEL_MENUBAR" 2>/dev/null || true

cat > "$PLIST_SERVER" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL_SERVER</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>$REPO/server/thrash_server.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$REPO</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/thrash.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/thrash.err.log</string>
</dict>
</plist>
PLIST

cat > "$PLIST_MENUBAR" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL_MENUBAR</string>
    <key>ProgramArguments</key>
    <array>
        <string>$HOME/.local/bin/Thrash</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/thrash-menubar.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/thrash-menubar.err.log</string>
</dict>
</plist>
PLIST

say "  wrote $LABEL_SERVER.plist"
say "  wrote $LABEL_MENUBAR.plist"

launchctl bootstrap "gui/$(id -u)" "$PLIST_SERVER"
say "  bootstrapped $LABEL_SERVER"

if [ -x "$HOME/.local/bin/Thrash" ]; then
    launchctl bootstrap "gui/$(id -u)" "$PLIST_MENUBAR"
    say "  bootstrapped $LABEL_MENUBAR"
else
    say "  menu-bar binary missing — skipped $LABEL_MENUBAR"
    say "  (build it later with ./menubar/build.sh, then re-run ./install.sh --yes)"
fi

# ---- next steps ----------------------------------------------------------------

step "Done"
say "  panel:   http://127.0.0.1:8790"
say "  cli:     ./thrash status"
say "  logs:    $LOG_DIR/thrash.log"
say "  uninstall: ./install.sh --uninstall"
