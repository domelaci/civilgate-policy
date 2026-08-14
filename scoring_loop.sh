#!/bin/bash
cd /home/domelaci/projects/automate/civilgate
while true; do
    if [ $(stat -c%s scoring.log 2>/dev/null || echo 0) -gt 5242880 ]; then
        mv -f scoring.log scoring.log.1 && > scoring.log
    fi
    echo "=== $(date -u +"%Y-%m-%d %H:%M:%S UTC") ===" >> scoring.log
    venv/bin/python eu_backfill.py --score-only --score-limit 500 >> scoring.log 2>&1
    PENDING=$(venv/bin/python3 -c "import sqlite3; c=sqlite3.connect('policies.db'); print(c.execute('SELECT COUNT(*) FROM policies WHERE summary IS NULL AND score_failed=0').fetchone()[0])")
    echo "Pending: $PENDING" >> scoring.log
    [ "$PENDING" -eq 0 ] && { echo "All done!" >> scoring.log; break; }
    sleep 5
done
