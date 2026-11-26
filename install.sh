#!/bin/bash
# Grand Theft Velo installer for Raspberry Pi OS / Debian (64-bit). Run as your normal user, from the repo:
#     ./install.sh
# It installs packages, builds the Remote Play library and the bridge, and starts the web page on port 8080.
set -euo pipefail
cd "$(dirname "$0")"
REPO="$(pwd)"; CHIAKI="${CHIAKI_DIR:-$HOME/.local/share/gtv/chiaki-ng}"; CHIAKI_REV=43728af
[ "$(id -u)" -ne 0 ] || { echo "Run this as your normal user, not root. It asks for sudo when needed."; exit 1; }

echo "== 1/5 packages"
sudo apt-get update -q
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q git build-essential cmake ninja-build pkg-config protobuf-compiler python3-protobuf \
  libopus-dev libssl-dev libjson-c-dev libminiupnpc-dev libevent-dev libnghttp2-dev libspeexdsp-dev libavcodec-dev libavutil-dev libavformat-dev \
  libswscale-dev libssh2-1-dev libidn2-dev libpsl-dev libbrotli-dev libzstd-dev zlib1g-dev \
  bluez python3-aiohttp python3-bleak python3-evdev python3-gpiozero

echo "== 2/5 Remote Play library (chiaki-ng, about 10 minutes on a Pi 4)"
if [ ! -f "$CHIAKI/build/lib/libchiaki.a" ]; then
  mkdir -p "$(dirname "$CHIAKI")"
  [ -d "$CHIAKI" ] || git clone -q --recurse-submodules https://github.com/streetpea/chiaki-ng.git "$CHIAKI"
  git -C "$CHIAKI" checkout -q "$CHIAKI_REV" && git -C "$CHIAKI" submodule update -q --init --recursive
  cmake -S "$CHIAKI" -B "$CHIAKI/build" -G Ninja -DCMAKE_BUILD_TYPE=Release -DCHIAKI_ENABLE_GUI=OFF -DCHIAKI_ENABLE_TESTS=OFF -DCHIAKI_ENABLE_CLI=OFF \
    -DCHIAKI_ENABLE_SETSU=OFF -DCHIAKI_ENABLE_STEAM_SHORTCUT=OFF -DCHIAKI_ENABLE_STEAMDECK_NATIVE=OFF -DCHIAKI_ENABLE_FFMPEG_DECODER=ON \
    -DCHIAKI_USE_SYSTEM_NANOPB=OFF -DCHIAKI_USE_SYSTEM_JERASURE=OFF -DCHIAKI_USE_SYSTEM_CURL=OFF -DCMAKE_POLICY_VERSION_MINIMUM=3.5 >/dev/null
  ninja -C "$CHIAKI/build"
fi

echo "== 3/5 bridge"
for t in bridge register; do CHIAKI_DIR="$CHIAKI" native/build-linux.sh "native/$t" "native/$t.c"; done

if [ -n "${GTV_BUILD_ONLY:-}" ]; then echo "Built. GTV_BUILD_ONLY is set, so the service was not installed."; exit 0; fi

echo "== 4/5 permissions and Bluetooth"
sudo usermod -aG input,bluetooth,gpio "$USER" 2>/dev/null || sudo usermod -aG input,bluetooth "$USER"
sudo rfkill unblock bluetooth 2>/dev/null || true
sudo systemctl enable --now bluetooth
# keep the Bluetooth radio on across reboots
sudo sed -i 's/^#\?AutoEnable=.*/AutoEnable=true/' /etc/bluetooth/main.conf 2>/dev/null || true

echo "== 5/5 service"
sudo tee /etc/systemd/system/gtv.service >/dev/null <<UNIT
[Unit]
Description=Grand Theft Velo
After=network-online.target bluetooth.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$REPO
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -m gtv
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable gtv >/dev/null
sudo systemctl restart gtv
IP=$(hostname -I | awk '{print $1}')
echo
echo "Done. Open http://$IP:8080/ (or http://$(hostname).local:8080/) on a phone or computer on the same network."
