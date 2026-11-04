#!/usr/bin/env bash
# (Re)start the background capture as a systemd user service.
#   ./restart_capture.sh            restart now
#   ./restart_capture.sh --after-match   restart once the current match has been written to the workbook
cd "$(dirname "$0")"
D=$(pwd)
LOG="$D/Captures/capture.log"

if [ "$1" = "--after-match" ]; then
    # The newest match's own line: every read (the 20 min clean-ups too) prints "not added" for matches left to type
    # in by hand, and a match is often read once the next has begun - that restarted mid-match (2026-11-03 23:07)
    MATCH=$(ls -d "$D"/Captures/20*/ 2>/dev/null | sort | tail -1 | xargs -r basename)
    if ! grep -qE "  $MATCH: (added|not added)" "$LOG"; then
        tail -n0 -F "$LOG" | grep -m1 -E "  $MATCH: (added|not added)|is open in Calc" >/dev/null
        sleep 5
    fi
fi

systemctl --user stop apex-stat-capture 2>/dev/null
# A unit that crashed on the way out stays "failed" and blocks starting a new one with the same name
systemctl --user reset-failed apex-stat-capture 2>/dev/null
systemd-run --user --unit=apex-stat-capture --working-directory="$D" -p Restart=on-failure \
    -p StandardOutput=append:"$LOG" -p StandardError=append:"$LOG" "$D/.venv/bin/python" "$D/Stat_Capture.py"
