# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
python3 mint_migrate_gui.py
```

**Dependencies** (GTK4 + libadwaita Python bindings):
```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1
```

## Installing as a desktop app

```bash
bash install.sh
```

This copies `mint_migrate_gui.py` to `/usr/local/bin/mint-migrate`, installs the icon, and registers the `.desktop` entry.

## Building the .deb package

The `.deb` is pre-built (`mint-migrate_1.0_all.deb`). To install it directly:
```bash
sudo dpkg -i mint-migrate_1.0_all.deb
```

## Architecture

Everything lives in a single file: `mint_migrate_gui.py`.

**GTK4/Adw structure:**
- `MintMigrateApp` (Adw.Application) → `MintMigrateWindow` (Adw.ApplicationWindow)
- The window holds an `Adw.ViewStack` with two pages: `BackupPage` and `RestorePage`
- Both pages use `Adw.PreferencesGroup` / `Adw.SwitchRow` / `Adw.ActionRow` for their settings UI

**Threading model:** All backup and restore operations run on a daemon thread (`threading.Thread`). All UI updates from those threads must go through `GLib.idle_add()` — the `LogWidget.log()` and `LogWidget.set_progress()` methods handle this automatically.

**`LogWidget`** is a shared composite widget (progress bar + status label + coloured `Gtk.TextView`) used by both pages. Log tags: `ok` (green), `warn` (yellow), `err` (red), `info` (blue), `section` (bold).

**Backup flow** (`BackupPage._thread`):
1. Builds a list of `(src_path, archive_path)` tuples from `DEFAULT_DIRS`, `DEFAULT_FILES`, and any extra paths entered by the user.
2. Opens a `zipfile.ZipFile` (or `_DummyZip` for dry runs) and calls `_add_to_zip()` for each item.
3. Optionally exports package list via `dpkg --get-selections`, dconf via `dconf dump /`, and copies system files (`/etc/hosts`, `/etc/fstab`, etc.) into `migration/system/…`.
4. Writes `RESTORE_INSTRUCTIONS.txt` into the archive.

**Restore flow** (`RestorePage._thread`):
- Home files are extracted directly from the zip to `~`.
- System files use `pkexec cp` (polkit) via the `sudo_cp()` inner function to write to `/etc/…` without a terminal.
- Package restore pipes the saved selections through `pkexec dpkg --set-selections` then `pkexec apt-get dselect-upgrade -y`.

**Exclusion logic:** `ALWAYS_EXCLUDE` paths are always skipped (`.cache`, `node_modules`, browser caches, etc.). `LARGE_SKIP_DEFAULTS` are `.local/share` subdirectories that default to OFF in the UI (Steam, flatpak, VirtualBox, etc.). The user can override both via the subfolder toggles.

**Known stub:** `Gio_FileFilterList()` at line 959 returns `None` — it exists only to avoid a `NameError`; `Gtk.FileDialog` doesn't require a `ListModel` for a single filter in GTK4.
