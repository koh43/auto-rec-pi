#!/bin/bash
set -e

green()  { echo -e "\033[32m$1\033[0m"; }
blue()   { echo -e "\033[36m$1\033[0m"; }
red()    { echo -e "\033[31m$1\033[0m"; }

if [ "$(id -u)" -eq 0 ]; then
    red "Do not run this script as root."
    echo "Usage: ./install.sh"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(whoami)"

blue "=== auto-rec-pi installer ==="
echo

# --- 1. Enable SPI --------------------------------------------------------
blue "[1/7] Enabling SPI..."
sudo raspi-config nonint do_spi 0

# --- 2. System packages ---------------------------------------------------
blue "[2/7] Installing system packages..."
sudo apt update
sudo apt install -y \
    ladspa-sdk \
    invada-studio-plugins-ladspa \
    libportaudio2 \
    python3-pip \
    python3-rpi.gpio \
    python3-spidev \
    python3-numpy \
    python3-pil \
    python3-venv

# --- 3. Device tree overlay ------------------------------------------------
blue "[3/7] Configuring device tree overlay..."
CONFIG_FILE="/boot/firmware/config.txt"
if [ ! -f "$CONFIG_FILE" ]; then
    CONFIG_FILE="/boot/config.txt"
fi

if ! grep -q "dtoverlay=adau7002-simple" "$CONFIG_FILE"; then
    echo "dtoverlay=adau7002-simple" | sudo tee -a "$CONFIG_FILE"
    green "  Added dtoverlay=adau7002-simple to $CONFIG_FILE"
else
    green "  dtoverlay=adau7002-simple already present in $CONFIG_FILE"
fi

# --- 4. ALSA config -------------------------------------------------------
blue "[4/7] Installing ALSA configuration..."
if [ -f "$HOME/.asoundrc" ]; then
    BACKUP="$HOME/.asoundrc.bak.$(date +%Y%m%d%H%M%S)"
    cp "$HOME/.asoundrc" "$BACKUP"
    green "  Backed up existing .asoundrc to $BACKUP"
fi
cp "$SCRIPT_DIR/asoundrc" "$HOME/.asoundrc"
green "  Installed .asoundrc"

# --- 5. Python virtual environment ----------------------------------------
blue "[5/7] Setting up Python virtual environment..."
cd "$SCRIPT_DIR"
python3 -m venv --system-site-packages .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
green "  Python environment ready"

# --- 6. Create recordings directory ----------------------------------------
blue "[6/7] Creating recordings directory..."
mkdir -p "$SCRIPT_DIR/recs"
green "  Created recs/"

# --- 7. systemd service ---------------------------------------------------
blue "[7/7] Installing systemd service..."
sudo tee /etc/systemd/system/autorecord.service > /dev/null << EOF
[Unit]
Description=auto-rec-pi Sound-Activated Recorder
After=multi-user.target sound.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$SCRIPT_DIR/scripts
ExecStart=$SCRIPT_DIR/.venv/bin/python autorecord.py
Restart=on-failure
RestartSec=5
Environment=HOME=$HOME

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable autorecord.service
green "  Installed and enabled autorecord.service"

echo
green "=== Installation complete ==="
echo
blue "Next steps:"
echo "  1. Reboot to apply the device tree overlay:"
echo "       sudo reboot"
echo "  2. After reboot the recorder starts automatically."
echo "  3. Check status:"
echo "       sudo systemctl status autorecord.service"
echo "  4. View logs:"
echo "       journalctl -u autorecord.service -f"
echo
blue "To test the microphone before rebooting:"
echo "  arecord -Dmic_out -c2 -r48000 -fS32_LE -twav -d5 -Vstereo test.wav"
