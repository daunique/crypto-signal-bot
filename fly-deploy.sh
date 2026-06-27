#!/data/data/com.termux/files/usr/bin/bash
# ═══════════════════════════════════════════════════════════
# SignalBot — Fly.io Deploy Script for Termux
# Run: bash fly-deploy.sh
#
# This pushes the CURRENT directory (your existing git repo) to Fly.
# It assumes you already ran `bash deploy.sh` at least once, so a git
# repo with origin set to GitHub already exists in $WORK_DIR — Fly
# deploys straight from local files, not from GitHub, so this script
# does not touch your GitHub remote at all.
# ═══════════════════════════════════════════════════════════

set -e

WORK_DIR="$HOME/signalbot"
APP_NAME="btcbot-candle-oracle"   # must match fly.toml's `app =` line

echo ""
echo "╔═══════════════════════════════════════╗"
echo "║   SignalBot — Fly.io Deploy (Termux)  ║"
echo "╚═══════════════════════════════════════╝"
echo ""

# ── Step 1: Make sure flyctl is installed ──────────────────
if ! command -v flyctl &> /dev/null; then
  echo "📦 flyctl not found — installing..."
  curl -L https://fly.io/install.sh | sh
  export FLYCTL_INSTALL="$HOME/.fly"
  export PATH="$FLYCTL_INSTALL/bin:$PATH"
  echo ""
  echo "ℹ️  Add this to your ~/.bashrc so flyctl is on PATH next time:"
  echo '   export FLYCTL_INSTALL="$HOME/.fly"'
  echo '   export PATH="$FLYCTL_INSTALL/bin:$PATH"'
  echo ""
fi

# ── Step 2: cd into the project ─────────────────────────────
if [ ! -d "$WORK_DIR" ]; then
  echo "❌ No project directory found at $WORK_DIR."
  echo "   Run deploy.sh first to extract/place the project there, or"
  echo "   edit WORK_DIR at the top of this script."
  exit 1
fi
cd "$WORK_DIR"

if [ ! -f "fly.toml" ]; then
  echo "❌ No fly.toml found in $WORK_DIR."
  echo "   Make sure Dockerfile, fly.toml, and .dockerignore are present"
  echo "   before deploying."
  exit 1
fi

# ── Step 3: Log in (only prompts if not already authed) ────
echo "🔑 Checking Fly.io auth..."
flyctl auth whoami &> /dev/null || flyctl auth login

# ── Step 4: First-time app creation vs. redeploy ────────────
if ! flyctl status --app "$APP_NAME" &> /dev/null; then
  echo ""
  echo "🆕 App '$APP_NAME' not found on Fly — launching for the first time."
  echo "   This reads fly.toml as-is (answer 'no' if it asks to overwrite it)."
  flyctl launch --copy-config --name "$APP_NAME" --no-deploy --yes

  echo ""
  echo "⚠️  Before deploying, set your secrets (one-time):"
  echo "   flyctl secrets set SECRET_KEY=\$(openssl rand -hex 32) --app $APP_NAME"
  echo "   flyctl secrets set DATABASE_URL=postgresql://... --app $APP_NAME"
  echo "   flyctl secrets set LIMITLESS_TOKEN_ID=... --app $APP_NAME"
  echo "   flyctl secrets set LIMITLESS_TOKEN_SECRET=... --app $APP_NAME"
  echo "   flyctl secrets set LIMITLESS_PRIVATE_KEY=... --app $APP_NAME"
  echo "   flyctl secrets set LIMITLESS_SMART_WALLET=... --app $APP_NAME"
  echo "   flyctl secrets set TELEGRAM_BOT_TOKEN=... --app $APP_NAME"
  echo "   flyctl secrets set TELEGRAM_CHAT_ID=... --app $APP_NAME"
  echo ""
  read -p "Press Enter once secrets are set to continue with deploy, or Ctrl+C to stop and set them now... "
fi

# ── Step 5: Deploy ───────────────────────────────────────────
echo ""
echo "🚀 Deploying to Fly.io..."
flyctl deploy --app "$APP_NAME"

# ── Step 6: Sanity check — exactly one machine, always on ──
echo ""
echo "🔎 Verifying machine count (should be exactly 1)..."
flyctl machines list --app "$APP_NAME"
echo ""
echo "⚠️  If you see more than one machine, scale down immediately:"
echo "   flyctl machines list --app $APP_NAME"
echo "   flyctl machines destroy <extra-machine-id> --app $APP_NAME"
echo "   (Duplicate machines = duplicate signals + duplicate live orders.)"

# ── Done ────────────────────────────────────────────────────
echo ""
echo "╔═══════════════════════════════════════╗"
echo "║   ✅ Deploy complete!                 ║"
echo "╚═══════════════════════════════════════╝"
echo ""
echo "🔗 App:   https://${APP_NAME}.fly.dev"
echo "📜 Logs:  flyctl logs --app $APP_NAME"
echo ""
