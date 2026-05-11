# Build, install, and validate the bebop AUv2 plugin.
#
# Stack:
#   - C++23 source (BebopAU.cpp) extending ausdk::AUEffectBase
#   - Linked against vendored Apple/AudioUnitSDK static lib
#   - Bundled as a .component, installed to ~/Library/Audio/Plug-Ins/Components/
#   - Validated via auval -strict (the same battery Logic uses internally)
#
# `make`        — full pipeline: build → sign → install → validate
# `make build`  — just compile + bundle
# `make sdk`    — build/refresh the AudioUnitSDK static lib
# `make clean`  — wipe build artifacts (does NOT touch the vendored SDK)

PLUGIN_NAME      := BebopAU
BUNDLE_NAME      := bebop.component
SWIFT_MODULE     := BebopAU

AU_TYPE          := aufx
AU_SUBTYPE       := BBOP
AU_MANUFACTURER  := Bbop

BUILD_DIR        := build
BUNDLE_DIR       := $(BUILD_DIR)/$(BUNDLE_NAME)
CONTENTS_DIR     := $(BUNDLE_DIR)/Contents
MACOS_DIR        := $(CONTENTS_DIR)/MacOS
INSTALL_DIR      := $(HOME)/Library/Audio/Plug-Ins/Components

VENDOR_DIR       := vendor/AudioUnitSDK
SDK_BUILD_DIR    := $(BUILD_DIR)/sdk
SDK_LIB          := $(SDK_BUILD_DIR)/Build/Products/Release/libAudioUnitSDK.a
SDK_INCLUDE      := $(VENDOR_DIR)/include

# bebop-rs static lib — embeds the Python interpreter and the bebop pipeline
BEBOP_RS_DIR     := ../bebop-rs
BEBOP_RS_LIB     := $(BEBOP_RS_DIR)/target/release/libbebop_rs.a
BEBOP_RS_INCLUDE := $(BEBOP_RS_DIR)/include

# Locate libpython at build time (same Python PyO3 linked against). Makefile
# expansion runs `python -c ...`, so this works during `make` invocation.
PY_PREFIX        := $(shell $(BEBOP_RS_DIR)/../.venv/bin/python3 -c 'import sys; print(sys.base_prefix)')
PY_LIB_DIR       := $(PY_PREFIX)/lib
PY_LIB_NAME      := python3.12

SRC              := Sources/BebopAU.cpp
INFO_PLIST       := Resources/Info.plist

CXX_FLAGS        := \
	-std=c++23 \
	-target arm64-apple-macos11.0 \
	-O2 \
	-stdlib=libc++ \
	-fvisibility=hidden \
	-I$(SDK_INCLUDE) \
	-I$(BEBOP_RS_INCLUDE)

LD_FLAGS         := \
	-target arm64-apple-macos11.0 \
	-shared \
	-stdlib=libc++ \
	-Wl,-install_name,@rpath/$(PLUGIN_NAME) \
	-framework AudioToolbox \
	-framework AudioUnit \
	-framework CoreAudio \
	-framework CoreFoundation \
	-framework CoreMIDI \
	-framework Security \
	-framework SystemConfiguration \
	$(SDK_LIB) \
	$(BEBOP_RS_LIB) \
	-L$(PY_LIB_DIR) -l$(PY_LIB_NAME) \
	-Wl,-rpath,$(PY_LIB_DIR)

.PHONY: all build sdk bundle sign install validate test clean

all: install validate

# Build the AUSDK static lib (one-time per checkout / SDK update)
$(SDK_LIB):
	xcodebuild -project $(VENDOR_DIR)/AudioUnitSDK.xcodeproj \
		-scheme AudioUnitSDK -configuration Release \
		-derivedDataPath $(SDK_BUILD_DIR) \
		ARCHS=arm64 ONLY_ACTIVE_ARCH=NO 2>&1 | tail -20

sdk: $(SDK_LIB)

# Build the bebop-rs static lib (Phase A core + C FFI surface).
# Cargo handles its own up-to-date checks, so this rule always runs cargo
# but cargo no-ops if nothing has changed.
.PHONY: bebop_rs_lib
bebop_rs_lib:
	cd $(BEBOP_RS_DIR) && cargo build --release

$(BEBOP_RS_LIB): bebop_rs_lib

# Compile + bundle (no install)
build: bundle sign

# C++ source → Mach-O dylib
$(MACOS_DIR)/$(PLUGIN_NAME): $(SRC) $(SDK_LIB) $(BEBOP_RS_LIB)
	@mkdir -p $(MACOS_DIR)
	clang++ $(CXX_FLAGS) -c $(SRC) -o $(BUILD_DIR)/BebopAU.o
	clang++ $(LD_FLAGS) $(BUILD_DIR)/BebopAU.o -o $@

$(CONTENTS_DIR)/Info.plist: $(INFO_PLIST)
	@mkdir -p $(CONTENTS_DIR)
	cp $(INFO_PLIST) $@

bundle: $(MACOS_DIR)/$(PLUGIN_NAME) $(CONTENTS_DIR)/Info.plist

sign: bundle
	codesign --force --sign - --timestamp=none $(BUNDLE_DIR)
	@echo "[bebop-au] signed $(BUNDLE_DIR)"

install: build
	@mkdir -p $(INSTALL_DIR)
	rm -rf $(INSTALL_DIR)/$(BUNDLE_NAME)
	cp -R $(BUNDLE_DIR) $(INSTALL_DIR)/
	@echo "[bebop-au] installed to $(INSTALL_DIR)/$(BUNDLE_NAME)"
	-killall -9 AudioComponentRegistrar 2>/dev/null || true

validate:
	@echo "[bebop-au] arch -arm64 auval -strict -v $(AU_TYPE) $(AU_SUBTYPE) $(AU_MANUFACTURER)"
	arch -arm64 /usr/bin/auval -strict -v $(AU_TYPE) $(AU_SUBTYPE) $(AU_MANUFACTURER)

test: install test-rb test-engine test-v2midi test-params test-fixture test-vmidi test-host-sync

test-engine:
	swiftc Tests/au_engine_test.swift \
		-target arm64-apple-macos11.0 \
		-O \
		-framework AVFoundation \
		-framework AudioToolbox \
		-o $(BUILD_DIR)/au_engine_test
	$(BUILD_DIR)/au_engine_test

test-v2midi:
	swiftc Tests/au_v2_midi_test.swift \
		-target arm64-apple-macos11.0 \
		-O \
		-framework AudioToolbox \
		-framework CoreMIDI \
		-o $(BUILD_DIR)/au_v2_midi_test
	$(BUILD_DIR)/au_v2_midi_test

test-params:
	swiftc Tests/au_params_test.swift \
		-target arm64-apple-macos11.0 \
		-O \
		-framework AudioToolbox \
		-framework CoreMIDI \
		-o $(BUILD_DIR)/au_params_test
	$(BUILD_DIR)/au_params_test

test-fixture:
	swiftc Tests/au_fixture_test.swift \
		-target arm64-apple-macos11.0 \
		-O \
		-framework AudioToolbox \
		-framework CoreMIDI \
		-framework AudioUnit \
		-o $(BUILD_DIR)/au_fixture_test
	$(BUILD_DIR)/au_fixture_test

test-vmidi:
	swiftc Tests/au_virtual_midi_test.swift \
		-target arm64-apple-macos11.0 \
		-O \
		-framework AudioToolbox \
		-framework CoreMIDI \
		-o $(BUILD_DIR)/au_virtual_midi_test
	$(BUILD_DIR)/au_virtual_midi_test

# Host-simulator test — registers fake HostCallbacks providing a known
# transport rolling at fixed BPM, drives renders with synthesized audio,
# captures MIDI output, and asserts timestamps land on the rhythm
# template's beat offsets within tight tolerance. This is the
# objective ground-truth test for Rhythm Sync timing — no Logic needed
test-host-sync:
	swiftc Tests/au_host_sync_test.swift \
		-target arm64-apple-macos11.0 \
		-O \
		-framework AudioToolbox \
		-framework CoreMIDI \
		-framework AudioUnit \
		-o $(BUILD_DIR)/au_host_sync_test
	$(BUILD_DIR)/au_host_sync_test

# C++ unit tests — ring buffer, etc. Doesn't need the AU installed.
test-rb:
	clang++ -std=c++23 -target arm64-apple-macos11.0 -O2 -stdlib=libc++ \
		-Wall -Wextra \
		Tests/ring_buffer_test.cpp \
		-o $(BUILD_DIR)/ring_buffer_test
	$(BUILD_DIR)/ring_buffer_test

clean:
	rm -rf $(BUILD_DIR)
