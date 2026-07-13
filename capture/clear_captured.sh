#!/data/data/com.termux/files/usr/bin/bash
# Deletes every finalized capture file (keeps the currently-active
# session untouched, so capture keeps running without interruption).
# Usage: ./clear_captured.sh <fly-app-url> <token>
# Example: ./clear_captured.sh https://polymarket-lifecycle-capture.fly.dev 1234567890123456

set -e

BASE_URL="${1:?Usage: $0 <fly-app-url> <token>}"
TOKEN="${2:?Usage: $0 <fly-app-url> <token>}"

echo "Fetching current status..."
STATUS_JSON=$(curl -s "${BASE_URL}/status?token=${TOKEN}")

LATEST=$(curl -s "${BASE_URL}/latest?token=${TOKEN}" | python3 -c "import json,sys; print(json.load(sys.stdin)['latest_file'])")
echo "Active file (will be kept): ${LATEST}"

FILES=$(echo "$STATUS_JSON" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for f in data['files']:
    print(f['name'])
")

DELETED_COUNT=0
for f in $FILES; do
    if [ "$f" == "$LATEST" ]; then
        continue
    fi
    echo "Deleting $f ..."
    curl -s -X DELETE "${BASE_URL}/${f}?token=${TOKEN}" -o /dev/null
    DELETED_COUNT=$((DELETED_COUNT + 1))
done

echo ""
echo "Done. Deleted ${DELETED_COUNT} file(s). Active session left untouched."
curl -s "${BASE_URL}/status?token=${TOKEN}"
