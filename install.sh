#!/usr/bin/env bash
# Install memmon.
#
#   ./install.sh                    CLI only (~/.local/bin/memmon)
#   ./install.sh --sampler          + launchd sampler (1/min history for --report)
#   ./install.sh --menubar          + menu-bar app, launched at login
#   ./install.sh --gate             + PreToolUse hook so sessions back off
#   ./install.sh --sampler --menubar  both
#   ./install.sh --uninstall        remove everything except collected history
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="$HOME/.claude/memmon"
DEST="$DEST_DIR/memmon.py"
BIN_DIR="$HOME/.local/bin"
BIN="$BIN_DIR/memmon"
AGENTS="$HOME/Library/LaunchAgents"
PLIST="$AGENTS/dev.memmon.sampler.plist"
BAR_PLIST="$AGENTS/dev.memmon.bar.plist"
LABEL="dev.memmon.sampler"
BAR_LABEL="dev.memmon.bar"
APP="$DEST_DIR/MemmonBar.app"

SETTINGS="$HOME/.claude/settings.json"
GATE="$DEST_DIR/memmon-gate.sh"

WANT_SAMPLER=0; WANT_MENUBAR=0; WANT_GATE=0; UNINSTALL=0
for a in "$@"; do
  case "$a" in
    --sampler) WANT_SAMPLER=1 ;;
    --menubar) WANT_MENUBAR=1 ;;
    --gate) WANT_GATE=1 ;;
    --uninstall) UNINSTALL=1 ;;
    *) echo "unknown flag: $a" >&2; exit 2 ;;
  esac
done

# Boot out any agent that points at this tool, whatever it is labelled. Earlier
# builds used different labels; matching on the plist CONTENTS rather than a
# hardcoded list means an upgrade never leaves a second sampler running against
# the same history file, and needs no list to be kept up to date.
if [[ -d "$AGENTS" ]]; then
  for PL in "$AGENTS"/*.plist; do
    [[ -e "$PL" ]] || continue
    [[ "$PL" == "$PLIST" || "$PL" == "$BAR_PLIST" ]] && continue
    if grep -q "$DEST_DIR" "$PL" 2>/dev/null; then
      OLD=$(basename "$PL" .plist)
      launchctl bootout "gui/$(id -u)/$OLD" 2>/dev/null || true
      rm -f "$PL"
      echo "removed a previous agent: $OLD"
    fi
  done
fi

if [[ $UNINSTALL == 1 ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootout "gui/$(id -u)/$BAR_LABEL" 2>/dev/null || true
  pkill -f "MemmonBar.app/Contents/MacOS/MemmonBar" 2>/dev/null || true
  # Patch settings.json BEFORE deleting the script it points at: under
  # `set -e` a failure here would otherwise leave a dead hook wired in.
  if [[ -f "$SETTINGS" ]]; then
    cp "$SETTINGS" "$SETTINGS.bak.$(date +%Y%m%d%H%M%S)"
    /usr/bin/python3 - "$SETTINGS" <<'PY'
import json, sys
path = sys.argv[1]
with open(path) as fh:
    cfg = json.load(fh)
pre = cfg.get("hooks", {}).get("PreToolUse", [])
kept = [e for e in pre
        if not any("memmon-gate" in h.get("command", "")
                   for h in e.get("hooks", []))]
if len(kept) != len(pre):
    cfg["hooks"]["PreToolUse"] = kept
    with open(path, "w") as fh:
        json.dump(cfg, fh, indent=2)
    print("gate hook removed from settings.json")
PY
  fi
  rm -rf "$PLIST" "$BAR_PLIST" "$BIN" "$DEST" "$APP" \
         "$DEST_DIR/memmon-gate.sh" "$DEST_DIR/learned.zsh" "$DEST_DIR/paused.json"
  echo "memmon removed. History kept at $DEST_DIR/history.jsonl"
  exit 0
fi

# Preflight. Fail with a sentence that says what to do, not a stack trace three
# steps later on a machine that has never run this.
[[ "$(uname)" == "Darwin" ]] || {
  echo "memmon is macOS-only (it reads top/vm_stat/sysctl)." >&2; exit 1; }
MACOS_MAJOR=$(sw_vers -productVersion | cut -d. -f1)
if [[ "$MACOS_MAJOR" -lt 13 ]]; then
  echo "needs macOS 13+ (found $(sw_vers -productVersion)); the menu-bar app uses SwiftUI popovers" >&2
  exit 1
fi
[[ -x /usr/bin/python3 ]] || {
  echo "no /usr/bin/python3 — run: xcode-select --install" >&2; exit 1; }
if [[ ! -d "$HOME/.claude" ]]; then
  echo "note: ~/.claude not found — session attribution and the gate need Claude Code;"
  echo "      the RAM/swap monitor still works without it."
fi

mkdir -p "$DEST_DIR" "$BIN_DIR"
# Copy rather than symlink: the source may live in a git worktree that gets
# deleted, and the monitor has to keep working after that.
cp "$SRC_DIR/memmon.py" "$DEST"
chmod +x "$DEST"
cp "$SRC_DIR/memmon-gate.sh" "$GATE"
chmod +x "$GATE"
cp "$SRC_DIR/README.md" "$DEST_DIR/README.md" 2>/dev/null || true

cat > "$BIN" <<EOF
#!/usr/bin/env bash
exec /usr/bin/python3 "$DEST" "\$@"
EOF
chmod +x "$BIN"
echo "installed: $BIN -> $DEST"

if [[ $WANT_SAMPLER == 1 ]]; then
  mkdir -p "$AGENTS"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$DEST</string>
    <string>--log</string>
  </array>
  <key>StartInterval</key><integer>60</integer>
  <key>RunAtLoad</key><true/>
  <key>Nice</key><integer>10</integer>
  <key>LowPriorityIO</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardErrorPath</key><string>$DEST_DIR/sampler.err</string>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "sampler installed: 1 sample/min -> $DEST_DIR/history.jsonl"
fi

if [[ $WANT_MENUBAR == 1 ]]; then
  command -v swiftc >/dev/null || { echo "swiftc not found (needs Xcode CLT)"; exit 1; }
  echo "building menu-bar app…"
  rm -rf "$APP"
  mkdir -p "$APP/Contents/MacOS"
  swiftc -O -o "$APP/Contents/MacOS/MemmonBar" \
    "$SRC_DIR/MemmonBar.swift" -framework Cocoa
  # LSUIElement keeps it out of the Dock and the app switcher.
  cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>MemmonBar</string>
  <key>CFBundleDisplayName</key><string>memmon</string>
  <key>CFBundleIdentifier</key><string>dev.memmon.bar</string>
  <key>CFBundleExecutable</key><string>MemmonBar</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
</dict>
</plist>
EOF
  mkdir -p "$AGENTS"
  cat > "$BAR_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$BAR_LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$APP/Contents/MacOS/MemmonBar</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)/$BAR_LABEL" 2>/dev/null || true
  pkill -f "MemmonBar.app/Contents/MacOS/MemmonBar" 2>/dev/null || true
  # bootout and SIGTERM are both asynchronous; bootstrapping before the old
  # process has actually exited leaves two icons in the menu bar.
  for _ in $(seq 1 25); do
    pgrep -f "MemmonBar.app/Contents/MacOS/MemmonBar" >/dev/null || break
    sleep 0.2
  done
  pkill -9 -f "MemmonBar.app/Contents/MacOS/MemmonBar" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$BAR_PLIST"
  echo "menu-bar app installed and started (also launches at login)"
fi

if [[ $WANT_GATE == 1 ]]; then
  if [[ ! -f "$SETTINGS" ]]; then
    echo "no $SETTINGS — skipping the gate (everything else installed)"
    WANT_GATE=0
  fi
fi
if [[ $WANT_GATE == 1 ]]; then
  cp "$SETTINGS" "$SETTINGS.bak.$(date +%Y%m%d%H%M%S)"
  GATE="$GATE" /usr/bin/python3 - "$SETTINGS" <<'PY'
import json, os, sys
path, gate = sys.argv[1], os.environ["GATE"]
with open(path) as fh:
    cfg = json.load(fh)
hooks = cfg.setdefault("hooks", {}).setdefault("PreToolUse", [])
# Idempotent: never stack duplicates across re-installs.
for entry in hooks:
    for h in entry.get("hooks", []):
        if "memmon-gate" in h.get("command", ""):
            print("gate hook already present")
            raise SystemExit(0)
hooks.append({"matcher": "Bash",
              "hooks": [{"type": "command", "command": gate}]})
with open(path, "w") as fh:
    json.dump(cfg, fh, indent=2)
print("gate hook added to PreToolUse (matcher: Bash)")
PY
  echo "  mode: blocks only at CRITICAL (warns at DANGER/WATCH)"
  echo "  pause it:   memmon --off        (or --off 8h to auto-resume)"
  echo "  resume it:  memmon --on"
  echo "  remove it:  ./install.sh --uninstall"
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "note: $BIN_DIR is not on PATH — add it to your ~/.zshrc" ;;
esac

echo
echo "  memmon              live dashboard"
echo "  memmon --once       one snapshot"
echo "  memmon --report     per-app / per-worktree averages"
echo "  memmon --reap       list reclaimable orphans"
