#!/bin/bash
# ---------------------------------------------------------------------------
# Wrapper invoked by the launchd agent (com.financeproject.zackssync) every
# Sunday. launchd runs with a minimal environment, so we set an explicit PATH
# that includes the Anaconda Python (which has selenium/pandas/openpyxl) and
# the usual locations where Chrome / chromedriver live, then run the sync.
#
# All output is appended to zacks_sync.log with a timestamped header so you
# can confirm each weekly run actually happened.
# ---------------------------------------------------------------------------
set -u

export PATH="/opt/anaconda3/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

PROJECT_DIR="/Users/netanelnevo/Desktop/לימודים/שנה ג׳/מנהל עסקים/סמסטר ב׳/בסיסי נתונים/finance-project"
PYTHON="/opt/anaconda3/bin/python3"
LOG="$PROJECT_DIR/zacks_sync.log"

cd "$PROJECT_DIR" || { echo "cannot cd to project dir" >&2; exit 1; }

# Run headless in the background (no GUI session guaranteed under launchd).
export HEADLESS=1

{
  echo ""
  echo "==================================================================="
  echo "launchd run @ $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "==================================================================="
} >> "$LOG" 2>&1

"$PYTHON" zacks_sync.py >> "$LOG" 2>&1
status=$?

echo "exit status: $status @ $(date '+%Y-%m-%d %H:%M:%S %Z')" >> "$LOG" 2>&1
exit $status
