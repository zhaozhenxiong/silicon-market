#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p output/bin
swiftc -O -framework Vision -framework ImageIO automation/ocr.swift -o output/bin/mobile-ocr
