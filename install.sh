#!/usr/bin/env bash
# ==============================================================================
# All-in-One Media Downloader Bot - One-Click Installer & Runner
# Fully Rootless, User-Space Compatible, Portable & Open-Source
# Author: Mohammad Yousef Morovajnia (@EINDRAL)
# ==============================================================================

set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}"
echo "╔═══════════════════════════════════════════════════════════╗"
echo "║       🚀 All-in-One Social & Music Downloader Bot        ║"
echo "║          Telegram Media Downloader (Pyrogram/MTProto)     ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo -e "${NC}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 1. Check Python 3
echo -e "${BLUE}[1/5] Checking Python installation...${NC}"
if command -v python3 >/dev/null 2>&1; then
    PY_VER=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
    echo -e "${GREEN}✓ Found Python $PY_VER${NC}"
else
    echo -e "${RED}✗ Python 3 is not installed. Please install Python 3.9+ first.${NC}"
    exit 1
fi

# 2. Check or Create Virtual Environment
echo -e "${BLUE}[2/5] Setting up isolated Python virtual environment...${NC}"
if [ ! -d "venv" ]; then
    python3 -m venv venv || {
        echo -e "${YELLOW}Warning: 'python3 -m venv' failed. Trying virtualenv...${NC}"
        virtualenv venv || {
            echo -e "${RED}✗ Could not create venv. Make sure python3-venv is available.${NC}"
            exit 1
        }
    }
    echo -e "${GREEN}✓ Virtual environment created under ./venv${NC}"
else
    echo -e "${GREEN}✓ Virtual environment already exists.${NC}"
fi

VENV_PY="$SCRIPT_DIR/venv/bin/python"

# 3. Install Dependencies
echo -e "${BLUE}[3/5] Installing dependencies from requirements.txt...${NC}"
"$VENV_PY" -m pip install --upgrade pip setuptools wheel >/dev/null 2>&1 || true
"$VENV_PY" -m pip install -r requirements.txt
echo -e "${GREEN}✓ All dependencies installed successfully.${NC}"

# 4. Interactive Configuration (.env)
echo -e "${BLUE}[4/5] Checking configuration (.env)...${NC}"
if [ ! -f ".env" ]; then
    echo -e "${YELLOW}No .env file found. Let's configure your bot!${NC}"
    echo ""
    read -rp "👉 Enter your Telegram Bot Token (from @BotFather): " USER_BOT_TOKEN
    read -rp "👉 Enter your Numeric Telegram Admin ID (e.g. 1429926943): " USER_ADMIN_ID

    cat <<EOF > .env
BOT_TOKEN=${USER_BOT_TOKEN}
ADMIN_ID=${USER_ADMIN_ID}
DOWNLOAD_DIR=downloads
EOF
    echo -e "${GREEN}✓ .env created successfully!${NC}"
else
    echo -e "${GREEN}✓ .env configuration found.${NC}"
fi

# Ensure downloads directory exists
mkdir -p downloads

# 5. Launch Option
echo -e "${BLUE}[5/5] Ready to launch!${NC}"
echo -e "${GREEN}Setup completed successfully!${NC}"
echo ""
echo "How would you like to run the bot?"
echo "1) Run in foreground now (Interactive logs)"
echo "2) Run in background (nohup / daemon)"
echo "3) Exit setup"
read -rp "Select option [1-3]: " LAUNCH_CHOICE

case "$LAUNCH_CHOICE" in
    1)
        echo -e "${GREEN}Starting bot in foreground... Press Ctrl+C to stop.${NC}"
        "$VENV_PY" bot.py
        ;;
    2)
        nohup "$VENV_PY" bot.py > bot.log 2>&1 &
        BOT_PID=$!
        echo -e "${GREEN}✓ Bot started in background! PID: $BOT_PID${NC}"
        echo -e "${BLUE}ℹ You can monitor logs with: tail -f bot.log${NC}"
        ;;
    *)
        echo -e "${YELLOW}You can start the bot anytime with:${NC}"
        echo "  ./venv/bin/python bot.py"
        ;;
esac
