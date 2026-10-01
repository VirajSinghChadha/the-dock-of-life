#!/bin/bash
# Builds "MathSnap Dock.app" with PyInstaller and wraps it in a DMG (macOS only).
# Usage: pip install pyinstaller PyQt6 pillow google-genai "pynput>=1.7.7" && ./build_dmg.sh
set -euo pipefail
cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"
PYTHON="${PYTHON:-python3}"
APP="MathSnap Dock"
OUT_NAME="MathSnapDock-$(uname -m).dmg"

# Build in a plain local temp directory, never in-place. If this project folder
# lives under iCloud Drive (Desktop/Documents with iCloud sync on) or certain
# other synced/managed locations, macOS stamps freshly written files with extra
# attributes (observed: com.apple.macl) that `codesign` refuses to seal. That
# leaves a *broken* ad-hoc signature — one that claims sealed resources it
# doesn't actually have — which makes Gatekeeper report "app is damaged" for
# anyone who opens the DMG, and is not something `xattr -d` can clean up after
# the fact. A scratch dir under $TMPDIR has no such attributes.
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cp "mathsnap_dock.py" "$WORK/"
cd "$WORK"

"$PYTHON" -m PyInstaller --noconfirm --clean --windowed --name "$APP" \
    --osx-bundle-identifier org.mathsnap.dock \
    --hidden-import pynput.keyboard._darwin --hidden-import pynput.mouse._darwin \
    --collect-submodules google.genai \
    mathsnap_dock.py

codesign --force --deep --sign - --identifier org.mathsnap.dock "dist/$APP.app"
codesign --verify --deep --strict "dist/$APP.app"
echo "Signed ad-hoc and verified."

STAGE="$(mktemp -d)"
cp -R "dist/$APP.app" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "$APP" -srcfolder "$STAGE" -ov -format UDZO "$OUT_NAME"
rm -rf "$STAGE"

mkdir -p "$PROJECT_DIR/dist"
cp "$OUT_NAME" "$PROJECT_DIR/dist/$OUT_NAME"
echo "Built $PROJECT_DIR/dist/$OUT_NAME"
