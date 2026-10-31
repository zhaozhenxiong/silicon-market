#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p output/bin
native_arch="$(uname -m)"
swiftc -O -parse-as-library -target "${native_arch}-apple-macos14.2" \
  -framework AppKit -framework ScreenCaptureKit -framework ApplicationServices \
  -framework ImageIO -framework UniformTypeIdentifiers \
  automation/native_mobile.swift -o output/bin/native-mobile
# Stable local executable path for macOS permission settings. No privilege gain.
codesign --force --sign - output/bin/native-mobile >/dev/null 2>&1
printf '%s\n' 'Built output/bin/native-mobile; no permissions requested or UI operated.'
