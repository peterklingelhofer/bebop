#!/usr/bin/env bash
# Build NNLS-Chroma Vamp plugin (provides chordino + nnls-chroma + tuning) for arm64.
# Outputs ~/Library/Audio/Plug-Ins/Vamp/nnls-chroma.dylib which is required by
# autochord and chord-extractor.
#
# Prerequisites:
#   - cmake, boost (brew install cmake boost)
#   - macOS arm64
#
# Usage:
#   ./vendor/build_nnls_chroma.sh

set -euo pipefail

# ensure brew tools (cmake, boost) are on PATH even when invoked from a non-login shell
if [ -x /opt/homebrew/bin/brew ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
elif [ -x /usr/local/bin/brew ]; then
    eval "$(/usr/local/bin/brew shellenv)"
fi

VENDOR_DIR="$(cd "$(dirname "$0")" && pwd)"
SDK_DIR="$VENDOR_DIR/vamp-plugin-sdk"
NNLS_DIR="$VENDOR_DIR/nnls-chroma"

# locate boost headers — handle both x86_64 brew (/usr/local) and arm64 brew (/opt/homebrew)
if [ -d /opt/homebrew/include/boost ]; then
    BOOST_INC=/opt/homebrew/include
elif [ -d /usr/local/include/boost ]; then
    BOOST_INC=/usr/local/include
else
    echo "boost headers not found. Install with: brew install boost"
    exit 1
fi
echo "using boost headers from $BOOST_INC"

echo "==> Building Vamp SDK as arm64..."
cd "$SDK_DIR"
mkdir -p build && cd build
# the upstream CMakeLists.txt has a stray set_target_properties outside the
# example-plugins conditional, so we have to enable examples to build cleanly
cmake .. -DCMAKE_OSX_ARCHITECTURES=arm64 -DVAMPSDK_BUILD_EXAMPLE_PLUGINS=ON > /dev/null
cmake --build . -j 8 > /dev/null
echo "    -> built libvamp-sdk.a"

echo "==> Building NNLS-Chroma as arm64..."
cd "$NNLS_DIR"
# patch boost::iostreams (which would need linking against x86_64 libboost_iostreams)
# to plain std::ifstream — already applied to chromamethods.cpp, idempotent
if grep -q 'iostreams::stream<iostreams::file_source>' chromamethods.cpp 2>/dev/null; then
    sed -i.bak \
      -e 's|iostreams::stream<iostreams::file_source> chordDictFile(chordDictFilename);|std::ifstream chordDictFile(chordDictFilename.c_str());|' \
      -e 's|#include <boost/iostreams/device/file.hpp>||' \
      -e 's|#include <boost/iostreams/stream.hpp>||' \
      chromamethods.cpp
fi

# write the macOS Makefile pointing at our vendored SDK
cat > Makefile.osx <<EOF

VAMP_SDK_DIR = ../vamp-plugin-sdk/build
BOOST_INCLUDE = $BOOST_INC

ARCHFLAGS ?= -arch arm64 -mmacosx-version-min=11.0
OPTFLAGS  ?= -O3 -ffast-math
PLUGIN_EXT = .dylib

CXXFLAGS  += -I\$(BOOST_INCLUDE) -I../vamp-plugin-sdk
LDFLAGS += \$(ARCHFLAGS) -dynamiclib -install_name \$(PLUGIN) \$(VAMP_SDK_DIR)/libvamp-sdk.a -exported_symbols_list vamp-plugin.list -framework Accelerate

include Makefile.inc
EOF

make -f Makefile.osx > /dev/null
echo "    -> built nnls-chroma.dylib"

echo "==> Installing to ~/Library/Audio/Plug-Ins/Vamp/..."
mkdir -p "$HOME/Library/Audio/Plug-Ins/Vamp"
cp nnls-chroma.dylib "$HOME/Library/Audio/Plug-Ins/Vamp/"
cp *.cat *.n3 "$HOME/Library/Audio/Plug-Ins/Vamp/" 2>/dev/null || true

echo
echo "Done. To verify:"
echo "  .venv-extras/bin/python -c \"import vamp; print(vamp.list_plugins())\""
echo "Should list: ['nnls-chroma:chordino', 'nnls-chroma:nnls-chroma', 'nnls-chroma:tuning']"
