#!/bin/bash
# ============================================================
# 🚀 ONE-CLICK INSTALLER - QRIS Payment Gateway Bot
# Jalankan dengan:
#   bash <(curl -s https://raw.githubusercontent.com/xyzval/pay/main/install.sh)
#
# Script ini akan:
# 1. Install dependencies (Python, pip, dll)
# 2. Clone/update repository
# 3. Setup virtual environment
# 4. Install Python packages
# 5. Setup konfigurasi (.env)
# 6. Install & aktifkan systemd service (24/7 non-stop)
# ============================================================

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Config
REPO_URL="https://github.com/xyzval/pay.git"
INSTALL_DIR="$HOME/pay"
SERVICE_NAME="pay-bot"
PYTHON_MIN="3.10"

echo -e "${CYAN}"
echo "╔══════════════════════════════════════════════════╗"
echo "║   🚀 QRIS Payment Gateway - Auto Installer     ║"
echo "║   Install & Run 24/7 Non-Stop                   ║"
echo "╚══════════════════════════════════════════════════╝"
echo -e "${NC}"

# ========================
# FUNCTIONS
# ========================

log_info() { echo -e "${GREEN}[✓]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[!]${NC} $1"; }
log_error() { echo -e "${RED}[✗]${NC} $1"; }
log_step() { echo -e "\n${BLUE}▶ $1${NC}"; }

check_root() {
    if [ "$EUID" -eq 0 ]; then
        SUDO=""
    else
        SUDO="sudo"
        if ! command -v sudo &> /dev/null; then
            log_error "Script membutuhkan root atau sudo. Jalankan sebagai root."
            exit 1
        fi
    fi
}

# ========================
# STEP 1: System Dependencies
# ========================

log_step "Step 1/6: Menginstall system dependencies..."

check_root

# Detect OS
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$ID
else
    OS="unknown"
fi

case $OS in
    ubuntu|debian)
        log_info "Detected: $PRETTY_NAME"
        $SUDO apt-get update -qq
        $SUDO apt-get install -y -qq python3 python3-pip python3-venv git curl wget > /dev/null 2>&1
        ;;
    centos|rhel|fedora|rocky|alma*)
        log_info "Detected: $PRETTY_NAME"
        $SUDO yum install -y python3 python3-pip git curl wget > /dev/null 2>&1 || \
        $SUDO dnf install -y python3 python3-pip git curl wget > /dev/null 2>&1
        ;;
    arch|manjaro)
        log_info "Detected: $PRETTY_NAME"
        $SUDO pacman -Sy --noconfirm python python-pip git curl wget > /dev/null 2>&1
        ;;
    *)
        log_warn "OS tidak terdeteksi ($OS). Pastikan Python 3.10+, pip, dan git sudah terinstall."
        ;;
esac

# Verify Python
if command -v python3 &> /dev/null; then
    PYTHON_VER=$(python3 --version | cut -d' ' -f2)
    log_info "Python $PYTHON_VER terdeteksi"
else
    log_error "Python3 tidak ditemukan! Install manual: sudo apt install python3"
    exit 1
fi

# ========================
# STEP 2: Clone/Update Repository
# ========================

log_step "Step 2/6: Mengambil source code..."

if [ -d "$INSTALL_DIR/.git" ]; then
    log_info "Repository sudah ada, updating..."
    cd "$INSTALL_DIR"
    git pull --quiet origin main 2>/dev/null || git pull --quiet
else
    if [ -d "$INSTALL_DIR" ]; then
        log_warn "Folder $INSTALL_DIR ada tapi bukan git repo. Backup & re-clone..."
        mv "$INSTALL_DIR" "${INSTALL_DIR}_backup_$(date +%s)"
    fi
    git clone --quiet "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

log_info "Source code ready di $INSTALL_DIR"

# ========================
# STEP 3: Virtual Environment
# ========================

log_step "Step 3/6: Setup Python virtual environment..."

if [ ! -d "$INSTALL_DIR/venv" ]; then
    python3 -m venv "$INSTALL_DIR/venv"
    log_info "Virtual environment dibuat"
else
    log_info "Virtual environment sudah ada"
fi

source "$INSTALL_DIR/venv/bin/activate"

# ========================
# STEP 4: Install Python Packages
# ========================

log_step "Step 4/6: Menginstall Python packages..."

pip install --quiet --upgrade pip
pip install --quiet -r "$INSTALL_DIR/requirements.txt"

log_info "Semua package terinstall"

# ========================
# STEP 5: Konfigurasi .env
# ========================

log_step "Step 5/6: Setup konfigurasi..."

if [ ! -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    
    echo ""
    echo -e "${YELLOW}╔══════════════════════════════════════════════════╗${NC}"
    echo -e "${YELLOW}║  ⚠️  KONFIGURASI DIPERLUKAN                      ║${NC}"
    echo -e "${YELLOW}╚══════════════════════════════════════════════════╝${NC}"
    echo ""
    
    # Ask for BOT_TOKEN
    read -p "$(echo -e ${CYAN}Masukkan BOT_TOKEN dari @BotFather: ${NC})" BOT_TOKEN
    if [ -n "$BOT_TOKEN" ]; then
        sed -i "s|BOT_TOKEN=.*|BOT_TOKEN=$BOT_TOKEN|g" "$INSTALL_DIR/.env"
    fi
    
    # Ask for ADMIN_IDS
    read -p "$(echo -e ${CYAN}Masukkan ADMIN_IDS \(Telegram ID, pisah koma\): ${NC})" ADMIN_IDS
    if [ -n "$ADMIN_IDS" ]; then
        sed -i "s|ADMIN_IDS=.*|ADMIN_IDS=$ADMIN_IDS|g" "$INSTALL_DIR/.env"
    fi
    
    # Ask for SAWERIA_USERNAME
    read -p "$(echo -e ${CYAN}Masukkan SAWERIA_USERNAME \(default: nvatryn\): ${NC})" SAWERIA_USER
    if [ -n "$SAWERIA_USER" ]; then
        # Add SAWERIA_USERNAME if not exists
        if grep -q "SAWERIA_USERNAME" "$INSTALL_DIR/.env"; then
            sed -i "s|SAWERIA_USERNAME=.*|SAWERIA_USERNAME=$SAWERIA_USER|g" "$INSTALL_DIR/.env"
        else
            echo "SAWERIA_USERNAME=$SAWERIA_USER" >> "$INSTALL_DIR/.env"
        fi
    fi
    
    echo ""
    log_info "Konfigurasi disimpan di $INSTALL_DIR/.env"
    log_warn "Edit manual nanti: nano $INSTALL_DIR/.env"
else
    log_info "File .env sudah ada (tidak di-overwrite)"
fi

# ========================
# STEP 6: Setup Systemd Service (24/7)
# ========================

log_step "Step 6/6: Setup service 24/7 (systemd)..."

# Get current user info
CURRENT_USER=$(whoami)
PYTHON_PATH="$INSTALL_DIR/venv/bin/python"

# Create systemd service file
$SUDO tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null << EOF
[Unit]
Description=QRIS Payment Gateway Bot (24/7)
After=network.target network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$CURRENT_USER
Group=$CURRENT_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$PYTHON_PATH $INSTALL_DIR/bot.py
Restart=always
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=5

# Environment
Environment=PYTHONUNBUFFERED=1

# Security
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=$INSTALL_DIR

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$SERVICE_NAME

[Install]
WantedBy=multi-user.target
EOF

# Reload systemd & enable service
$SUDO systemctl daemon-reload
$SUDO systemctl enable ${SERVICE_NAME}.service
$SUDO systemctl restart ${SERVICE_NAME}.service

# Wait a moment and check status
sleep 2

if $SUDO systemctl is-active --quiet ${SERVICE_NAME}; then
    log_info "Service AKTIF dan berjalan! ✅"
else
    log_warn "Service gagal start. Cek log: sudo journalctl -u ${SERVICE_NAME} -f"
    log_warn "Pastikan BOT_TOKEN sudah benar di file .env"
fi

# ========================
# DONE!
# ========================

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  ✅ INSTALASI SELESAI!                           ║${NC}"
echo -e "${GREEN}║  Bot berjalan 24/7 non-stop.                     ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${CYAN}📍 Lokasi:${NC}     $INSTALL_DIR"
echo -e "${CYAN}📋 Service:${NC}    $SERVICE_NAME"
echo -e "${CYAN}🌐 API Docs:${NC}   http://$(hostname -I | awk '{print $1}' 2>/dev/null || echo 'SERVER_IP'):8000/docs"
echo -e "${CYAN}🔗 Webhook:${NC}    http://$(hostname -I | awk '{print $1}' 2>/dev/null || echo 'SERVER_IP'):8000/callback/saweria"
echo ""
echo -e "${YELLOW}📌 Perintah Berguna:${NC}"
echo -e "   ${BLUE}Cek status:${NC}     sudo systemctl status $SERVICE_NAME"
echo -e "   ${BLUE}Lihat log:${NC}      sudo journalctl -u $SERVICE_NAME -f"
echo -e "   ${BLUE}Restart:${NC}        sudo systemctl restart $SERVICE_NAME"
echo -e "   ${BLUE}Stop:${NC}           sudo systemctl stop $SERVICE_NAME"
echo -e "   ${BLUE}Edit config:${NC}    nano $INSTALL_DIR/.env"
echo -e "   ${BLUE}Update bot:${NC}     cd $INSTALL_DIR && git pull && sudo systemctl restart $SERVICE_NAME"
echo ""
echo -e "${GREEN}🎉 Bot Telegram + Payment API sudah hidup 24 jam!${NC}"
echo ""
