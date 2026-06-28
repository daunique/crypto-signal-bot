#!/data/data/com.termux/files/usr/bin/bash
# ═══════════════════════════════════════════════════════════
# SignalBot — GitHub Push Script for Termux
# Run: bash deploy.sh
# ═══════════════════════════════════════════════════════════

set -e  # stop on any error

# ── CONFIG — edit these two lines ──────────────────────────
GITHUB_USER="daunique"
REPO_NAME="crypto-signal-bot"
# ───────────────────────────────────────────────────────────

REPO_URL="https://github.com/${GITHUB_USER}/${REPO_NAME}.git"
WORK_DIR="$HOME/signalbot"

echo ""
echo "╔═══════════════════════════════════════╗"
echo "║   SignalBot — Termux Deploy Script    ║"
echo "╚═══════════════════════════════════════╝"
echo ""

# ── Step 1: Install git if missing ─────────────────────────
if ! command -v git &> /dev/null; then
  echo "📦 Installing git..."
  pkg install -y git
fi

# ── Step 2: Set git identity (required for commits) ────────
git config --global user.email "bot@signalbot.local" 2>/dev/null || true
git config --global user.name  "SignalBot Deploy"    2>/dev/null || true

# ── Step 3: Ask for GitHub token ───────────────────────────
echo "🔑 Enter your GitHub Personal Access Token (PAT)"
echo "   (Get one at: GitHub → Settings → Developer Settings → PAT → Fine-grained)"
echo "   Needs: Contents = Read & Write on repo ${REPO_NAME}"
echo ""
read -s -p "Token: " GH_TOKEN
echo ""

if [ -z "$GH_TOKEN" ]; then
  echo "❌ No token entered. Exiting."
  exit 1
fi

AUTHED_URL="https://${GITHUB_USER}:${GH_TOKEN}@github.com/${GITHUB_USER}/${REPO_NAME}.git"

# ── Step 4: Extract zip if present ─────────────────────────
ZIP_PATH="$HOME/storage/downloads/btcbot.zip"
if [ -f "$ZIP_PATH" ]; then
  echo "📦 Found btcbot.zip in Downloads — extracting..."
  pkg install -y unzip 2>/dev/null || true
  mkdir -p "$WORK_DIR"
  unzip -o "$ZIP_PATH" -d "$HOME/extracted_bot"

  # Handle zip structure (may have btcbot/ subfolder inside)
  if [ -d "$HOME/extracted_bot/btcbot" ]; then
    cp -r "$HOME/extracted_bot/btcbot/." "$WORK_DIR/"
  else
    cp -r "$HOME/extracted_bot/." "$WORK_DIR/"
  fi
  rm -rf "$HOME/extracted_bot"
  echo "✅ Extracted to $WORK_DIR"
else
  echo "ℹ️  No zip found at $ZIP_PATH"
  echo "   Using existing files in $WORK_DIR"
  if [ ! -d "$WORK_DIR" ]; then
    echo "❌ No project directory found at $WORK_DIR. Download btcbot.zip first."
    exit 1
  fi
fi

# ── Step 5: Init or update git repo ────────────────────────
cd "$WORK_DIR"

if [ ! -d ".git" ]; then
  echo "🔧 Initialising git repository..."
  git init
  git remote add origin "$AUTHED_URL"
else
  echo "🔧 Updating remote URL with token..."
  git remote set-url origin "$AUTHED_URL"
fi

# ── Step 6: Stage all files ─────────────────────────────────
echo ""
echo "📁 Files to be pushed:"
ls -la
echo ""

git add -A

# ── Step 7: Commit ──────────────────────────────────────────
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
git commit -m "Deploy: $TIMESTAMP" 2>/dev/null || {
  echo "ℹ️  Nothing new to commit — already up to date"
}

# ── Step 8: Push ────────────────────────────────────────────
echo ""
echo "🚀 Pushing to GitHub..."
git push -u origin main --force 2>/dev/null || \
git push -u origin master --force 2>/dev/null || {
  echo ""
  echo "⚠️  Push failed. Trying to set upstream branch..."
  BRANCH=$(git rev-parse --abbrev-ref HEAD)
  git push -u origin "$BRANCH" --force
}

# ── Done ────────────────────────────────────────────────────
echo ""
echo "╔═══════════════════════════════════════╗"
echo "║   ✅ Push complete!                   ║"
echo "╚═══════════════════════════════════════╝"
echo ""
echo "🔗 Repo:  https://github.com/${GITHUB_USER}/${REPO_NAME}"
echo ""
echo "If deploying on Render: check your Render dashboard for auto-deploy."
echo ""
echo "If deploying on Fly.io: Fly does NOT auto-deploy on git push."
echo "  Run from this folder on a machine with flyctl installed:"
echo "    flyctl deploy"
echo ""
echo "⚠️  Make sure these are set as env vars (Render) or secrets (Fly):"
echo "   LIMITLESS_TOKEN_ID"
echo "   LIMITLESS_TOKEN_SECRET"
echo "   LIMITLESS_PRIVATE_KEY"
echo ""
