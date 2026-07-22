#!/usr/bin/env bash
set -euo pipefail
APP="${1:-crypto-signal-bot-kooj9a}"
fly status -a "$APP"
fly secrets list -a "$APP"
echo "--- Health ---"
curl -fsS "https://${APP}.fly.dev/health"
echo
