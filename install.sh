#!/usr/bin/env bash
# install.sh — Install Mint Migration Tool as a desktop app
# Run with: bash install.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ""
echo "─────────────────────────────────────────"
echo "  🐧  Mint Migration Tool — Installer"
echo "─────────────────────────────────────────"
echo ""

# ── Check dependencies ────────────────────────────────────────────────────────
echo "Checking dependencies..."
MISSING=()
python3 -c "import gi" 2>/dev/null || MISSING+=("python3-gi")
python3 -c "import gi; gi.require_version('Gtk','4.0')" 2>/dev/null || MISSING+=("gir1.2-gtk-4.0")
python3 -c "import gi; gi.require_version('Adw','1')" 2>/dev/null || MISSING+=("gir1.2-adw-1")

if [ ${#MISSING[@]} -gt 0 ]; then
    echo "Installing missing packages: ${MISSING[*]}"
    sudo apt-get install -y "${MISSING[@]}"
else
    echo "  ✔  All dependencies present"
fi

# ── Install app ───────────────────────────────────────────────────────────────
echo ""
echo "Installing app..."

# Copy script to /usr/local/bin
sudo cp "$SCRIPT_DIR/mint_migrate_gui.py" /usr/local/bin/mint-migrate
sudo chmod +x /usr/local/bin/mint-migrate
echo "  ✔  Installed to /usr/local/bin/mint-migrate"

# Install icon (multiple sizes via scaling, or just the PNG)
sudo mkdir -p /usr/share/icons/hicolor/128x128/apps
sudo cp "$SCRIPT_DIR/mint-migrate.png" /usr/share/icons/hicolor/128x128/apps/mint-migrate.png
sudo gtk-update-icon-cache /usr/share/icons/hicolor/ 2>/dev/null || true
echo "  ✔  Icon installed"

# Install .desktop file
sudo cp "$SCRIPT_DIR/mint-migrate.desktop" /usr/share/applications/mint-migrate.desktop
sudo update-desktop-database /usr/share/applications/ 2>/dev/null || true
echo "  ✔  Desktop entry installed"

echo ""
echo "─────────────────────────────────────────"
echo "  ✅  Done! Find 'Mint Migration Tool'"
echo "      in your application menu."
echo "─────────────────────────────────────────"
echo ""
