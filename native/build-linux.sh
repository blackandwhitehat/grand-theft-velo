#!/bin/sh
# Builds one native tool on Linux against a built chiaki-ng checkout: build-linux.sh <output> <source.c>
set -e
C="${CHIAKI_DIR:-$HOME/.local/share/gtv/chiaki-ng}"; OUT="${1:-bridge}"; SRC="${2:-$OUT.c}"
cc -O2 -o "$OUT" "$SRC" -I"$C/lib/include" -I"$C/build/lib/include" -I"$C/build/lib" \
  "$C/build/lib/libchiaki.a" "$C/build/third-party/libjerasure.a" "$C/build/third-party/libgf_complete.a" \
  "$C/build/third-party/nanopb/libprotobuf-nanopb.a" "$C/build/third-party/curl/lib/libcurl.a" \
  $(pkg-config --libs opus openssl json-c miniupnpc libevent libevent_pthreads libnghttp2 libavcodec libavutil libswscale speexdsp zlib libssh2 libidn2 libpsl libbrotlidec libzstd) \
  -lpthread -lm
