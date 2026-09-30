#!/bin/bash
# Builds "MathSnap Dock.app" with PyInstaller and wraps it in a DMG (macOS only).
# Usage: pip install pyinstaller PyQt6 pillow google-genai "pynput>=1.7.7" && ./build_dmg.sh
set -euo pipefail
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"
APP="MathSnap Dock"
OUT="dist/MathSnapDock-$(uname -m).dmg"

"$PYTHON" -m PyInstaller --noconfirm --clean --windowed --name "$APP" \
    --osx-bundle-identifier org.mathsnap.dock \
    --hidden-import pynput.keyboard._darwin --hidden-import pynput.mouse._darwin \
    --collect-submodules google.genai \
    mathsnap_dock.py

STAGE="$(mktemp -d)"
cp -R "dist/$APP.app" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$OUT"
hdiutil create -volname "$APP" -srcfolder "$STAGE" -ov -format UDZO "$OUT"
rm -rf "$STAGE"
echo "Built $OUT"
