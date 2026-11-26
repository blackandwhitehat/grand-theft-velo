#!/bin/bash
# Grand Theft Velo: one-line install for Raspberry Pi OS / Debian (64-bit).
#     curl -fsSL https://grandtheftvelo.com/install.sh | bash
# Fetches the project and runs its installer. Run it again later to update.
set -euo pipefail
[ "$(id -u)" -ne 0 ] || { echo "Run this as your normal user, not root. It asks for sudo when needed."; exit 1; }
command -v git >/dev/null || { echo "== installing git"; sudo apt-get update -q; sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q git; }
DIR="${GTV_DIR:-$HOME/grand-theft-velo}"
if [ -d "$DIR/.git" ]; then
  echo "== updating $DIR"; git -C "$DIR" pull --ff-only
else
  echo "== downloading to $DIR"; git clone https://github.com/blackandwhitehat/grand-theft-velo.git "$DIR"
fi
cd "$DIR"
exec ./install.sh
