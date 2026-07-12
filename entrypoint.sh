#!/bin/sh
# Lets the run mode be switched via `fly secrets set BOT_MODE=live` (or dry-run /
# check) without rebuilding or redeploying the image — just restart the machine
# (`fly machine restart`) after changing it.
set -e
MODE="${BOT_MODE:-dry-run}"
echo "Starting momentum_bot.py in --${MODE} mode (set BOT_MODE to change)"
exec python3 momentum_bot.py "--${MODE}"
