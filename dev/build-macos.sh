#!/bin/sh
# builds rptest against the locally built chiaki-ng library
set -e
R="$(cd "$(dirname "$0")/.." && pwd)"; C="${CHIAKI_DIR:-$R/chiaki-ng}"; P="${OPUS_PREFIX:-$R/prefix}"; O="${BREW_OPT:-/opt/homebrew/opt}"
cc -O2 -o "${1:-rptest}" "${2:-rptest.c}" -I$C/lib/include -I$C/build/lib/include -I$C/build/lib -I$O/openssl@3/include \
  $C/build/lib/libchiaki.a $C/build/third-party/libjerasure.a $C/build/third-party/libgf_complete.a $C/build/third-party/nanopb/libprotobuf-nanopb.a \
  $C/build/_deps/miniupnpc-build/libminiupnpc.a $C/build/_deps/json-c-build/libjson-c.a $C/build/third-party/curl/lib/libcurl.a $P/lib/libopus.a \
  $O/openssl@3/lib/libssl.a $O/openssl@3/lib/libcrypto.a $O/libevent/lib/libevent_core.a $O/libevent/lib/libevent_pthreads.a $O/libnghttp2/lib/libnghttp2.a \
  -lz -lpthread -lm -framework CoreFoundation -framework SystemConfiguration -framework Security -framework CoreServices
