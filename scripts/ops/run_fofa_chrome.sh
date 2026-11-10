#!/usr/bin/env bash
# Run the FOFA CDP browser on the desktop when someone is logged in, else virtual.
set -euo pipefail

PROFILE="${HOME}/.config/google-chrome-cdp"
EXTENSION="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/extension"
CHROME=(
    /usr/bin/google-chrome
    --user-data-dir="$PROFILE"
    --remote-debugging-port=9222
    --remote-allow-origins=*
    --no-first-run
    --no-default-browser-check
    --disable-extensions-except="$EXTENSION"
    --load-extension="$EXTENSION"
    --disable-background-timer-throttling
    --disable-renderer-backgrounding
)

for display in :10.0 :0; do
    if DISPLAY="$display" XAUTHORITY="$HOME/.Xauthority" xdpyinfo >/dev/null 2>&1; then
        echo "[fofa-chrome] using desktop display $display"
        exec env DISPLAY="$display" XAUTHORITY="$HOME/.Xauthority" "${CHROME[@]}"
    fi
done

echo "[fofa-chrome] no desktop display; using a virtual display"
exec xvfb-run -a -s "-screen 0 1920x1080x24 -nolisten tcp" "${CHROME[@]}"
