#!/bin/sh
set -eu

APP_PATH="${1:-/Applications/Codex.app}"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PATCH_SCRIPT="$PROJECT_ROOT/scripts/patch_codex_app_extended_history.js"
NODE_PATH="$APP_PATH/Contents/Resources/node"
ASAR_PATH="$APP_PATH/Contents/Resources/app.asar"
LABEL="com.am700.codex-history-repair.patch"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs"
OUT_LOG="$LOG_DIR/codex-history-repair-patch.log"
ERR_LOG="$LOG_DIR/codex-history-repair-patch.err.log"

if [ ! -x "$NODE_PATH" ]; then
  echo "Codex bundled node not found or not executable: $NODE_PATH" >&2
  exit 1
fi

if [ ! -f "$PATCH_SCRIPT" ]; then
  echo "Patch script not found: $PATCH_SCRIPT" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
      <string>$NODE_PATH</string>
      <string>$PATCH_SCRIPT</string>
      <string>$APP_PATH</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>WatchPaths</key>
    <array>
      <string>$ASAR_PATH</string>
    </array>
    <key>StandardOutPath</key>
    <string>$OUT_LOG</string>
    <key>StandardErrorPath</key>
    <string>$ERR_LOG</string>
  </dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "Installed LaunchAgent: $PLIST_PATH"
echo "Logs:"
echo "  $OUT_LOG"
echo "  $ERR_LOG"
