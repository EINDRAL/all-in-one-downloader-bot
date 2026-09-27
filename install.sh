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
# Use --prefer-binary to avoid OOM killer when building heavy C++ wheels (e.g. rapidfuzz) on limited shared hosting
"$VENV_PY" -m pip install --prefer-binary -r requirements.txt
echo -e "${GREEN}✓ All dependencies installed successfully.${NC}"

# 4. Interactive Configuration (.env)
echo -e "${BLUE}[4/5] Checking configuration (.env)...${NC}"
if [ ! -f ".env" ]; then
    echo -e "${YELLOW}No .env file found. Let's configure your bot!${NC}"
    echo ""
    read -rp "👉 Enter your Telegram Bot Token (from @BotFather): " USER_BOT_TOKEN
    read -rp "👉 Enter your Numeric Telegram Admin ID (e.g. 1429926943): " USER_ADMIN_ID
    echo ""
    echo -e "${BLUE}Outbound Proxy & V2Ray configuration (Optional):${NC}"
    echo "  Supports: vless://, vmess://, trojan://, ss://, socks5://, or http://"
    echo "  (Recommended if hosting in Russia, Iran, or behind a firewall restricting YouTube)"
    read -rp "👉 Paste Proxy / V2Ray link (Press Enter to skip for direct connection): " USER_PROXY
    USER_PROXY=$(echo "$USER_PROXY" | tr -d '\r\n' | xargs)

    if [ -n "$USER_PROXY" ]; then
        # Check if it's a V2Ray link requiring Xray binary
        case "$USER_PROXY" in
            vless://*|vmess://*|trojan://*|ss://*)
                if ! command -v xray >/dev/null 2>&1 && [ ! -f "$HOME/.local/bin/xray" ] && [ ! -f "$SCRIPT_DIR/bin/xray" ]; then
                    echo -e "${YELLOW}Notice: Xray binary not detected. Downloading portable Xray Core...${NC}"
                    mkdir -p "$SCRIPT_DIR/bin"
                    "$VENV_PY" -c "
import urllib.request, zipfile, io, os
url = 'https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip'
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
try:
    with urllib.request.urlopen(req) as resp:
        with zipfile.ZipFile(io.BytesIO(resp.read())) as z:
            z.extract('xray', '$SCRIPT_DIR/bin')
            os.chmod('$SCRIPT_DIR/bin/xray', 0o755)
            print('✓ Xray Core installed successfully.')
except Exception as e:
    print(f'Warning: Could not auto-download Xray: {e}')
" || true
                fi
                ;;
        esac

        echo -e "${YELLOW}Verifying proxy connectivity...${NC}"
        if "$VENV_PY" proxy_manager.py "$USER_PROXY" >/dev/null 2>&1; then
            echo -e "${GREEN}✓ Proxy verified successfully!${NC}"
        else
            echo -e "${YELLOW}Notice: Proxy could not be verified right now. It will still be saved to .env.${NC}"
        fi
    fi

    cat <<EOF > .env
BOT_TOKEN=${USER_BOT_TOKEN}
ADMIN_ID=${USER_ADMIN_ID}
DOWNLOAD_DIR=downloads
$( [ -n "$USER_PROXY" ] && echo "PROXY=${USER_PROXY}" || echo "# PROXY=" )
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
