#!/usr/bin/env python3
"""
mint_migrate_gui.py — Linux Mint Migration Tool (GTK4 GUI)
Backup tab: backs up home folder, packages, dconf, system files into a zip.
Restore tab: opens a backup zip and restores everything on the new machine.
"""

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Pango

import os
import sys
import shutil
import threading
import zipfile
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime

# ── Constants ─────────────────────────────────────────────────────────────────

HOME = Path.home()

DEFAULT_DIRS = [
    ".config", ".local/share", ".ssh", ".gnupg",
    ".cinnamon", ".themes", ".icons", ".mozilla",
    "bin", ".local/bin",
]

DEFAULT_FILES = [
    ".bashrc", ".bash_aliases", ".bash_profile", ".profile",
    ".gitconfig", ".gitignore_global", ".vimrc", ".tmux.conf",
]

ALWAYS_EXCLUDE = [
    ".cache", "__pycache__", ".git", "node_modules",
    ".local/share/Trash",
    ".config/google-chrome/Default/Cache",
    ".config/chromium/Default/Cache",
]

LARGE_SKIP_DEFAULTS = {
    "FaithLife-Community", "ALVR-Launcher", "Trash",
    "flatpak", "Steam", "containers", "gnome-boxes", "VirtualBox",
}

# ── Shared helpers ────────────────────────────────────────────────────────────

def human_size(num_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def get_dir_size_fast(path: Path) -> int:
    try:
        r = subprocess.run(["du", "-sb", str(path)],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0 or not r.stdout.strip():
            return 0
        return int(r.stdout.split()[0])
    except Exception:
        return 0


def should_exclude(path: Path, extra_excludes: set) -> bool:
    parts = path.parts
    s = str(path)
    for pat in ALWAYS_EXCLUDE:
        if "/" in pat:
            if pat in s:
                return True
        else:
            if pat in parts:
                return True
    for exc in extra_excludes:
        if exc in s:
            return True
    return False


def write_restore_instructions(zf):
    zf.writestr("RESTORE_INSTRUCTIONS.txt", """\
# Linux Mint Migration — Restore Instructions
# =============================================
# You can restore automatically using mint_migrate_gui.py (Restore tab)
# or follow the manual steps below.

## 1. Install the app on the new machine
    sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1
    python3 mint_migrate_gui.py   # then use the Restore tab

## Manual restore:

## 2. Extract the archive
    unzip mint-backup-<date>.zip -d ~/migration-restore

## 3. Restore home folder files
    cp -r migration-restore/home/. ~/

## 4. Restore dconf (desktop settings)
    dconf load / < migration-restore/migration/dconf-backup.txt

## 5. Restore packages
    sudo dpkg --set-selections < migration-restore/migration/packages.txt
    sudo apt-get dselect-upgrade -y

## 6. Restore system files (check before applying — UUIDs differ!)
    sudo cp migration-restore/migration/system/etc/hosts /etc/hosts
    # Do NOT restore fstab unless you've updated the UUIDs

## 7. Fix permissions
    chmod 700 ~/.ssh && chmod 600 ~/.ssh/*
    chmod 700 ~/.gnupg
""")


def _add_to_zip(zf, src: Path, arcname: str, stats: dict, extra_excludes: set):
    if not src.exists() or src.is_symlink():
        return
    if should_exclude(src, extra_excludes):
        return
    if src.is_file():
        try:
            zf.write(src, arcname)
            stats["files"] += 1
            stats["bytes"] += src.stat().st_size
        except (PermissionError, OSError):
            pass
    elif src.is_dir():
        for child in src.rglob("*"):
            if should_exclude(child, extra_excludes) or child.is_symlink():
                continue
            if child.is_file():
                try:
                    rel = child.relative_to(src)
                    zf.write(child, f"{arcname}/{rel}")
                    stats["files"] += 1
                    stats["bytes"] += child.stat().st_size
                except (PermissionError, OSError):
                    pass


class _DummyZip:
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def write(self, *a, **kw): pass
    def writestr(self, *a, **kw): pass


# ── Shared log/progress widget ────────────────────────────────────────────────

class LogWidget(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.set_margin_top(8)
        self.set_margin_bottom(8)
        self.set_margin_start(12)
        self.set_margin_end(12)

        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_show_text(True)
        self._progress_bar.set_text("Ready")
        self.append(self._progress_bar)

        self._status_label = Gtk.Label(label="")
        self._status_label.set_halign(Gtk.Align.START)
        self._status_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self._status_label.add_css_class("dim-label")
        self.append(self._status_label)

        log_scroll = Gtk.ScrolledWindow()
        log_scroll.set_min_content_height(200)
        log_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._log_scroll = log_scroll

        self._log_buffer = Gtk.TextBuffer()
        self._log_buffer.create_tag("ok",      foreground="#26a269")
        self._log_buffer.create_tag("warn",    foreground="#e5a50a")
        self._log_buffer.create_tag("err",     foreground="#c01c28")
        self._log_buffer.create_tag("info",    foreground="#1c71d8")
        self._log_buffer.create_tag("section", weight=Pango.Weight.BOLD)

        self._log_view = Gtk.TextView(buffer=self._log_buffer)
        self._log_view.set_editable(False)
        self._log_view.set_cursor_visible(False)
        self._log_view.set_monospace(True)
        self._log_view.set_margin_start(8)
        self._log_view.set_margin_end(8)
        self._log_view.set_margin_top(6)
        self._log_view.set_margin_bottom(6)
        log_scroll.set_child(self._log_view)
        self.append(log_scroll)

    def log(self, msg, tag=None):
        def _do():
            end = self._log_buffer.get_end_iter()
            if tag:
                self._log_buffer.insert_with_tags_by_name(end, msg + "\n", tag)
            else:
                self._log_buffer.insert(end, msg + "\n")
            adj = self._log_scroll.get_vadjustment()
            adj.set_value(adj.get_upper())
        GLib.idle_add(_do)

    def set_progress(self, fraction, text, status=""):
        def _do():
            self._progress_bar.set_fraction(min(fraction, 1.0))
            self._progress_bar.set_text(text)
            if status:
                self._status_label.set_text(status)
        GLib.idle_add(_do)

    def clear(self):
        GLib.idle_add(self._log_buffer.set_text, "")
        self.set_progress(0.0, "Ready", "")


# ── Backup tab ────────────────────────────────────────────────────────────────

class BackupPage(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._window = window
        self._running = False
        self._output_path = str(HOME)
        self._subfolder_rows = {}
        self._build_ui()
        GLib.idle_add(self._populate_subfolders)

    def _build_ui(self):
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        self.append(scroll)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_margin_top(20)
        content.set_margin_bottom(20)
        content.set_margin_start(24)
        content.set_margin_end(24)
        scroll.set_child(content)

        # Output folder
        folder_group = Adw.PreferencesGroup(title="Output Location")
        folder_group.set_description("Where to save the backup zip file")
        content.append(folder_group)

        self._folder_row = Adw.ActionRow(title="Save to folder")
        self._folder_row.set_subtitle(self._output_path)
        folder_btn = Gtk.Button(label="Choose…")
        folder_btn.set_valign(Gtk.Align.CENTER)
        folder_btn.add_css_class("suggested-action")
        folder_btn.connect("clicked", self._on_choose_folder)
        self._folder_row.add_suffix(folder_btn)
        folder_group.add(self._folder_row)

        # What to back up
        options_group = Adw.PreferencesGroup(title="What to Back Up")
        content.append(options_group)

        self._checks = {}
        for key, title, subtitle, default in [
            ("home_dirs", "Home folder & dotfiles",   "~/.config, .ssh, .gnupg, .cinnamon, dotfiles…",          True),
            ("packages",  "Installed packages",        "dpkg package list for reinstalling apps",                 True),
            ("dconf",     "Desktop settings (dconf)",  "Panel layout, applets, Cinnamon config",                  True),
            ("sys_hosts", "/etc/hosts & hostname",     "Custom host entries and machine name",                    True),
            ("sys_fstab", "/etc/fstab",                "Drive mounts — OFF by default, UUIDs differ on new hardware", False),
            ("sys_cron",  "/etc/crontab & timezone",   "Scheduled tasks and timezone setting",                    True),
        ]:
            row = Adw.SwitchRow(title=title, subtitle=subtitle)
            row.set_active(default)
            self._checks[key] = row
            options_group.add(row)

        # .local/share exclusions
        self._subfolder_group = Adw.PreferencesGroup(title="Exclude from .local/share")
        self._subfolder_group.set_description(
            "Toggle OFF to exclude from backup. Large reinstallable folders are off by default."
        )
        content.append(self._subfolder_group)

        self._subfolder_spinner = Gtk.Spinner()
        self._subfolder_spinner.set_spinning(True)
        self._subfolder_spinner.set_margin_top(8)
        self._subfolder_spinner.set_margin_bottom(8)
        self._subfolder_spinner.set_halign(Gtk.Align.CENTER)
        self._subfolder_group.add(self._subfolder_spinner)

        # Extra paths
        extra_group = Adw.PreferencesGroup(title="Extra Folders (optional)")
        extra_group.set_description("Space-separated paths relative to home, e.g.  Projects  morning-dashboard")
        content.append(extra_group)
        self._extra_entry = Adw.EntryRow(title="Additional paths")
        extra_group.add(self._extra_entry)

        # Progress & log
        prog_group = Adw.PreferencesGroup(title="Progress")
        content.append(prog_group)
        self._log_widget = LogWidget()
        prog_group.add(self._log_widget)

        # Buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        btn_box.set_halign(Gtk.Align.CENTER)
        btn_box.set_margin_top(8)
        content.append(btn_box)

        self._dry_btn = Gtk.Button(label="Dry Run")
        self._dry_btn.set_tooltip_text("Preview without writing anything")
        self._dry_btn.connect("clicked", lambda b: self._run(dry_run=True))
        btn_box.append(self._dry_btn)

        self._start_btn = Gtk.Button(label="Start Backup")
        self._start_btn.add_css_class("suggested-action")
        self._start_btn.connect("clicked", lambda b: self._run(dry_run=False))
        btn_box.append(self._start_btn)

    def _populate_subfolders(self):
        def _scan():
            local_share = HOME / ".local/share"
            rows = []
            if local_share.exists():
                for entry in sorted(local_share.iterdir(), key=lambda p: p.name.lower()):
                    if entry.is_dir() and not entry.name.startswith("."):
                        size = get_dir_size_fast(entry)
                        if size > 1024 * 1024:
                            rows.append((entry.name, size))
            GLib.idle_add(self._add_subfolder_rows, rows)
        threading.Thread(target=_scan, daemon=True).start()

    def _add_subfolder_rows(self, rows):
        self._subfolder_group.remove(self._subfolder_spinner)
        if not rows:
            self._subfolder_group.add(Adw.ActionRow(title="No large subfolders found"))
            return
        for name, size in rows:
            row = Adw.SwitchRow(title=name, subtitle=f"{human_size(size)} — toggle OFF to exclude")
            row.set_active(name not in LARGE_SKIP_DEFAULTS)
            self._subfolder_rows[name] = row
            self._subfolder_group.add(row)

    def _on_choose_folder(self, btn):
        dialog = Gtk.FileDialog(title="Choose output folder")
        dialog.select_folder(self._window, None, self._on_folder_chosen)

    def _on_folder_chosen(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
            if folder:
                self._output_path = folder.get_path()
                self._folder_row.set_subtitle(self._output_path)
        except GLib.Error:
            pass

    def _set_sensitive(self, v):
        GLib.idle_add(self._start_btn.set_sensitive, v)
        GLib.idle_add(self._dry_btn.set_sensitive, v)

    def _run(self, dry_run=False):
        if self._running:
            return
        self._running = True
        self._set_sensitive(False)
        self._log_widget.clear()

        excluded = {f".local/share/{n}" for n, r in self._subfolder_rows.items() if not r.get_active()}
        opts = {
            "output_path":  self._output_path,
            "home_dirs":    self._checks["home_dirs"].get_active(),
            "packages":     self._checks["packages"].get_active(),
            "dconf":        self._checks["dconf"].get_active(),
            "sys_hosts":    self._checks["sys_hosts"].get_active(),
            "sys_fstab":    self._checks["sys_fstab"].get_active(),
            "sys_cron":     self._checks["sys_cron"].get_active(),
            "extra":        self._extra_entry.get_text().strip().split() if self._extra_entry.get_text().strip() else [],
            "excluded":     excluded,
            "dry_run":      dry_run,
        }
        threading.Thread(target=self._thread, args=(opts,), daemon=True).start()

    def _thread(self, opts):
        dry = opts["dry_run"]
        excl = opts["excluded"]
        stats = {"files": 0, "bytes": 0}
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        zip_path = Path(opts["output_path"]) / f"mint-backup-{timestamp}.zip"
        log = self._log_widget.log
        prog = self._log_widget.set_progress

        log("─" * 52, "section")
        log("  🐧  Mint Migration Backup", "section")
        log(f"  {'DRY RUN — nothing written' if dry else f'Saving to: {zip_path}'}")
        if excl:
            log(f"  Excluding: {', '.join(p.split('/')[-1] for p in excl)}", "warn")
        log("─" * 52, "section")

        items = []
        if opts["home_dirs"]:
            for d in DEFAULT_DIRS:
                p = HOME / d
                if p.exists():
                    items.append((p, f"home/{d}"))
            for f in DEFAULT_FILES:
                p = HOME / f
                if p.exists():
                    items.append((p, f"home/{f}"))
        for extra in opts["extra"]:
            p = Path(extra).expanduser()
            if not p.is_absolute():
                p = HOME / extra
            if p.exists():
                items.append((p, f"home/extra/{p.name}"))

        total = len(items) + opts["packages"] * 2 + opts["dconf"] + opts["sys_hosts"] + opts["sys_fstab"] + opts["sys_cron"]
        done = [0]

        def step(label, status=""):
            done[0] += 1
            prog(done[0] / max(total, 1), label, status)

        def backup_etc(paths):
            for fp in paths:
                f = Path(fp)
                if f.exists():
                    if not dry:
                        try:
                            zf.write(f, f"migration/system{f}")
                            log(f"  ✔ {f}", "ok")
                            stats["files"] += 1
                        except (PermissionError, OSError):
                            log(f"  ⚠ Permission denied: {f}", "warn")
                    else:
                        log(f"  → Would back up {f}", "info")

        try:
            ZipCls = _DummyZip if dry else lambda p: zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED, compresslevel=6)
            with ZipCls(zip_path) as zf:

                if opts["home_dirs"] or opts["extra"]:
                    log("\n📁  Home folder & dotfiles", "section")
                    for src, arc in items:
                        label = str(src.relative_to(HOME)) if src.is_relative_to(HOME) else str(src)
                        log(f"  → {label}", "info")
                        step("Backing up files…", str(src))
                        if not dry:
                            _add_to_zip(zf, src, arc, stats, excl)

                if opts["packages"]:
                    log("\n📦  Installed packages", "section")
                    step("Exporting package list…")
                    if not dry:
                        try:
                            r = subprocess.run(["dpkg", "--get-selections"],
                                               capture_output=True, text=True, check=True)
                            zf.writestr("migration/packages.txt", r.stdout)
                            log(f"  ✔ {r.stdout.strip().count(chr(10)) + 1} packages exported", "ok")
                            stats["files"] += 1
                        except Exception as e:
                            log(f"  ⚠ dpkg failed: {e}", "warn")
                    else:
                        log("  → Would export dpkg package list", "info")

                    step("Backing up apt sources…")
                    if not dry:
                        src_p = Path("/etc/apt/sources.list")
                        if src_p.exists():
                            try:
                                zf.write(src_p, "migration/apt/sources.list")
                                log(f"  ✔ {src_p}", "ok")
                                stats["files"] += 1
                            except (PermissionError, OSError):
                                log(f"  ⚠ Permission denied: {src_p}", "warn")
                        sources_d = Path("/etc/apt/sources.list.d")
                        if sources_d.exists():
                            for f in sources_d.glob("*.list"):
                                try:
                                    zf.write(f, f"migration/apt/sources.list.d/{f.name}")
                                except (PermissionError, OSError):
                                    pass
                    else:
                        log("  → Would back up apt sources", "info")

                if opts["dconf"]:
                    log("\n🎨  dconf desktop settings", "section")
                    step("Exporting dconf…")
                    if not dry:
                        try:
                            r = subprocess.run(["dconf", "dump", "/"],
                                               capture_output=True, text=True, check=True)
                            zf.writestr("migration/dconf-backup.txt", r.stdout)
                            log("  ✔ dconf dump saved", "ok")
                            stats["files"] += 1
                        except Exception as e:
                            log(f"  ⚠ dconf failed: {e}", "warn")
                    else:
                        log("  → Would export dconf dump", "info")

                if opts["sys_hosts"] or opts["sys_fstab"] or opts["sys_cron"]:
                    log("\n⚙️   System config files", "section")

                if opts["sys_hosts"]:
                    step("Backing up hosts/hostname…")
                    backup_etc(["/etc/hosts", "/etc/hostname"])

                if opts["sys_fstab"]:
                    step("Backing up fstab…")
                    backup_etc(["/etc/fstab"])

                if opts["sys_cron"]:
                    step("Backing up crontab/timezone…")
                    backup_etc(["/etc/crontab", "/etc/timezone"])

                if not dry:
                    write_restore_instructions(zf)

            prog(1.0, "Complete!", "")
            log("\n" + "─" * 52, "section")
            if dry:
                log("  ✅  Dry run complete — no files written", "ok")
            else:
                zip_size = zip_path.stat().st_size
                log("  ✅  Backup complete!", "ok")
                log(f"  Files:  {stats['files']:,}")
                log(f"  Size:   {human_size(zip_size)}")
                log(f"  Saved:  {zip_path}", "ok")
                log("  ⚠  Keep this archive secure — it contains SSH/GPG keys.", "warn")
                GLib.idle_add(self._done_dialog, str(zip_path), human_size(zip_size))
            log("─" * 52, "section")

        except Exception as e:
            log(f"\n  ✘ Error: {e}", "err")
            prog(0.0, "Error", str(e))
        finally:
            self._running = False
            self._set_sensitive(True)

    def _done_dialog(self, path, size):
        d = Adw.AlertDialog(heading="Backup Complete ✅", body=f"Archive size: {size}\n\nSaved to:\n{path}")
        d.add_response("ok", "OK")
        d.present(self._window)


# ── Restore tab ───────────────────────────────────────────────────────────────

class RestorePage(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._window = window
        self._running = False
        self._zip_path = None
        self._zip_contents = {}   # section -> bool (present in zip)
        self._contents_rows = []  # ActionRows added to _contents_group
        self._build_ui()

    def _build_ui(self):
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        self.append(scroll)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_margin_top(20)
        content.set_margin_bottom(20)
        content.set_margin_start(24)
        content.set_margin_end(24)
        scroll.set_child(content)

        # Zip picker
        zip_group = Adw.PreferencesGroup(title="Backup Archive")
        zip_group.set_description("Select the mint-backup-*.zip file to restore from")
        content.append(zip_group)

        self._zip_row = Adw.ActionRow(title="No archive selected")
        self._zip_row.set_subtitle("Click Choose… to open a backup zip")
        zip_btn = Gtk.Button(label="Choose…")
        zip_btn.set_valign(Gtk.Align.CENTER)
        zip_btn.add_css_class("suggested-action")
        zip_btn.connect("clicked", self._on_choose_zip)
        self._zip_row.add_suffix(zip_btn)
        zip_group.add(self._zip_row)

        # Archive contents info
        self._contents_group = Adw.PreferencesGroup(title="Archive Contents")
        self._contents_group.set_description("What was found inside the backup")
        content.append(self._contents_group)

        self._contents_placeholder = Adw.ActionRow(title="Open an archive to inspect it")
        self._contents_group.add(self._contents_placeholder)

        # Restore options
        self._options_group = Adw.PreferencesGroup(title="What to Restore")
        self._options_group.set_description("Toggle off anything you don't want to restore")
        content.append(self._options_group)

        self._restore_checks = {}
        for key, title, subtitle, default in [
            ("home",     "Home folder & dotfiles",   "Restores files into your home directory",            True),
            ("packages", "Installed packages",        "Reinstalls all packages via dpkg + apt (needs sudo)", True),
            ("dconf",    "Desktop settings (dconf)",  "Restores panel layout, applets, Cinnamon config",    True),
            ("sys_hosts","Hosts & hostname",          "Restores /etc/hosts and /etc/hostname (needs sudo)", True),
            ("sys_fstab","fstab",                     "Drive mounts — OFF by default, UUIDs differ on new hardware", False),
            ("sys_cron", "Crontab & timezone",        "Restores /etc/crontab and /etc/timezone (needs sudo)", True),
        ]:
            row = Adw.SwitchRow(title=title, subtitle=subtitle)
            row.set_active(default)
            self._restore_checks[key] = row
            self._options_group.add(row)

        # Warnings box
        self._warnings_group = Adw.PreferencesGroup(title="⚠  Before You Restore")
        self._warnings_group.set_description(
            "• Home folder files will overwrite existing files\n"
            "• Package restore requires sudo and may take several minutes\n"
            "• Check /etc/fstab manually if you have custom drive mounts\n"
            "• SSH/GPG key permissions will be fixed automatically"
        )
        content.append(self._warnings_group)

        # Progress & log
        prog_group = Adw.PreferencesGroup(title="Progress")
        content.append(prog_group)
        self._log_widget = LogWidget()
        prog_group.add(self._log_widget)

        # Buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        btn_box.set_halign(Gtk.Align.CENTER)
        btn_box.set_margin_top(8)
        content.append(btn_box)

        self._dry_btn = Gtk.Button(label="Dry Run")
        self._dry_btn.set_tooltip_text("Preview restore without changing any files")
        self._dry_btn.set_sensitive(False)
        self._dry_btn.connect("clicked", lambda b: self._run(dry_run=True))
        btn_box.append(self._dry_btn)

        self._restore_btn = Gtk.Button(label="Restore")
        self._restore_btn.add_css_class("destructive-action")
        self._restore_btn.set_sensitive(False)
        self._restore_btn.set_tooltip_text("Restore files from the backup archive")
        self._restore_btn.connect("clicked", self._confirm_restore)
        btn_box.append(self._restore_btn)

    # ── Zip chooser ───────────────────────────────────────────────────────────

    def _on_choose_zip(self, btn):
        dialog = Gtk.FileDialog(title="Open backup archive")
        f = Gtk.FileFilter()
        f.set_name("Zip archives")
        f.add_pattern("*.zip")
        dialog.set_default_filter(f)
        dialog.open(self._window, None, self._on_zip_chosen)

    def _on_zip_chosen(self, dialog, result):
        try:
            file = dialog.open_finish(result)
            if file:
                self._zip_path = file.get_path()
                self._zip_row.set_title(Path(self._zip_path).name)
                self._zip_row.set_subtitle(self._zip_path)
                threading.Thread(target=self._inspect_zip, args=(self._zip_path,), daemon=True).start()
        except GLib.Error:
            pass

    def _inspect_zip(self, zip_path):
        contents = {
            "home": False, "packages": False, "dconf": False,
            "sys_hosts": False, "sys_fstab": False, "sys_cron": False,
        }
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                names = zf.namelist()
                if any(n.startswith("home/") for n in names):
                    contents["home"] = True
                if "migration/packages.txt" in names:
                    contents["packages"] = True
                if "migration/dconf-backup.txt" in names:
                    contents["dconf"] = True
                if any("system/etc/hosts" in n for n in names):
                    contents["sys_hosts"] = True
                if any("system/etc/fstab" in n for n in names):
                    contents["sys_fstab"] = True
                if any("system/etc/crontab" in n for n in names):
                    contents["sys_cron"] = True
        except Exception as e:
            GLib.idle_add(self._log_widget.log, f"  ✘ Could not read zip: {e}", "err")
            return
        GLib.idle_add(self._update_contents_ui, contents, zip_path)

    def _update_contents_ui(self, contents, zip_path):
        if zip_path != self._zip_path:
            return  # stale result from a superseded inspection

        self._zip_contents = contents

        # Remove previously added rows
        for row in self._contents_rows:
            self._contents_group.remove(row)
        self._contents_rows.clear()
        if hasattr(self, "_contents_placeholder"):
            try:
                self._contents_group.remove(self._contents_placeholder)
            except Exception:
                pass

        labels = {
            "home":      ("📁", "Home folder & dotfiles"),
            "packages":  ("📦", "Installed packages list"),
            "dconf":     ("🎨", "dconf desktop settings"),
            "sys_hosts": ("⚙️", "/etc/hosts & hostname"),
            "sys_fstab": ("⚙️", "/etc/fstab"),
            "sys_cron":  ("⚙️", "/etc/crontab & timezone"),
        }
        for key, (icon, label) in labels.items():
            present = contents.get(key, False)
            row = Adw.ActionRow(title=f"{icon}  {label}")
            row.set_subtitle("✔  Found in archive" if present else "✘  Not in archive")
            if not present:
                self._restore_checks[key].set_active(False)
                self._restore_checks[key].set_sensitive(False)
            else:
                self._restore_checks[key].set_sensitive(True)
            self._contents_group.add(row)
            self._contents_rows.append(row)

        self._dry_btn.set_sensitive(True)
        self._restore_btn.set_sensitive(True)

    # ── Restore ───────────────────────────────────────────────────────────────

    def _confirm_restore(self, btn):
        d = Adw.AlertDialog(
            heading="Restore from backup?",
            body="This will overwrite existing files in your home folder and may require sudo for system files. Continue?"
        )
        d.add_response("cancel", "Cancel")
        d.add_response("restore", "Restore")
        d.set_response_appearance("restore", Adw.ResponseAppearance.DESTRUCTIVE)
        d.connect("response", self._on_confirm_response)
        d.present(self._window)

    def _on_confirm_response(self, dialog, response):
        if response == "restore":
            self._run(dry_run=False)

    def _run(self, dry_run=False):
        if self._running or not self._zip_path:
            return
        self._running = True
        self._dry_btn.set_sensitive(False)
        self._restore_btn.set_sensitive(False)
        self._log_widget.clear()

        opts = {
            "zip_path":  self._zip_path,
            "dry_run":   dry_run,
            "home":      self._restore_checks["home"].get_active(),
            "packages":  self._restore_checks["packages"].get_active(),
            "dconf":     self._restore_checks["dconf"].get_active(),
            "sys_hosts": self._restore_checks["sys_hosts"].get_active(),
            "sys_fstab": self._restore_checks["sys_fstab"].get_active(),
            "sys_cron":  self._restore_checks["sys_cron"].get_active(),
        }
        threading.Thread(target=self._thread, args=(opts,), daemon=True).start()

    def _thread(self, opts):
        dry = opts["dry_run"]
        log = self._log_widget.log
        prog = self._log_widget.set_progress

        log("─" * 52, "section")
        log("  🐧  Mint Migration Restore", "section")
        log(f"  {'DRY RUN — nothing will be changed' if dry else 'Restoring from: ' + opts['zip_path']}")
        log("─" * 52, "section")

        steps = sum([opts["home"], opts["packages"], opts["dconf"],
                     opts["sys_hosts"], opts["sys_fstab"], opts["sys_cron"]])
        done = [0]

        def step(label, status=""):
            done[0] += 1
            prog(done[0] / max(steps, 1), label, status)

        def sudo_cp(src_in_zip, dest):
            if dry:
                log(f"  → Would restore {dest}", "info")
                return True
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(delete=False) as tmp:
                    tmp_path = tmp.name
                    tmp.write(zf.read(src_in_zip))
                r = subprocess.run(["pkexec", "cp", tmp_path, dest],
                                   capture_output=True, text=True)
                if r.returncode == 0:
                    log(f"  ✔ {dest}", "ok")
                    return True
                else:
                    log(f"  ✘ Failed to restore {dest}: {r.stderr.strip()}", "err")
                    return False
            except Exception as e:
                log(f"  ✘ Error restoring {dest}: {e}", "err")
                return False
            finally:
                if tmp_path is not None:
                    Path(tmp_path).unlink(missing_ok=True)

        try:
            with zipfile.ZipFile(opts["zip_path"], "r") as zf:
                names = set(zf.namelist())

                # ── Home folder ───────────────────────────────────────────────
                if opts["home"]:
                    log("\n📁  Restoring home folder", "section")
                    step("Restoring home folder…")
                    home_files = [n for n in names if n.startswith("home/")]
                    if not dry:
                        restored = 0
                        skipped = 0
                        for name in home_files:
                            rel = name[len("home/"):]
                            if not rel:
                                continue
                            dest = HOME / rel
                            try:
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                with zf.open(name) as src, open(dest, "wb") as dst:
                                    shutil.copyfileobj(src, dst)
                                restored += 1
                            except (PermissionError, OSError):
                                skipped += 1
                        log(f"  ✔ {restored:,} files restored, {skipped} skipped", "ok")

                        # Fix SSH/GPG permissions
                        ssh_dir = HOME / ".ssh"
                        gnupg_dir = HOME / ".gnupg"
                        if ssh_dir.exists():
                            ssh_dir.chmod(0o700)
                            for f in ssh_dir.iterdir():
                                try:
                                    f.chmod(0o600)
                                except Exception:
                                    pass
                            log("  ✔ SSH permissions fixed (700/600)", "ok")
                        if gnupg_dir.exists():
                            gnupg_dir.chmod(0o700)
                            log("  ✔ GPG permissions fixed (700)", "ok")
                    else:
                        log(f"  → Would restore {len(home_files):,} files to ~/", "info")

                # ── Packages ──────────────────────────────────────────────────
                if opts["packages"] and "migration/packages.txt" in names:
                    log("\n📦  Restoring packages", "section")
                    step("Restoring packages (this may take a while)…")
                    if not dry:
                        try:
                            pkg_data = zf.read("migration/packages.txt").decode()

                            log("  → Running dpkg --set-selections (needs sudo)…", "info")
                            r1 = subprocess.run(
                                ["pkexec", "dpkg", "--set-selections"],
                                input=pkg_data, capture_output=True, text=True
                            )
                            if r1.returncode == 0:
                                log("  ✔ Package selections set", "ok")
                            else:
                                log(f"  ⚠ dpkg --set-selections: {r1.stderr.strip()}", "warn")

                            log("  → Running apt-get dselect-upgrade (needs sudo)…", "info")
                            r2 = subprocess.run(
                                ["pkexec", "apt-get", "dselect-upgrade", "-y"],
                                capture_output=True, text=True
                            )
                            if r2.returncode == 0:
                                log("  ✔ Packages reinstalled", "ok")
                            else:
                                log(f"  ⚠ apt-get: {r2.stderr.strip()[:200]}", "warn")

                        except Exception as e:
                            log(f"  ✘ Package restore failed: {e}", "err")
                    else:
                        count = zf.read("migration/packages.txt").decode().count("\n")
                        log(f"  → Would reinstall {count:,} packages", "info")

                # ── dconf ─────────────────────────────────────────────────────
                if opts["dconf"] and "migration/dconf-backup.txt" in names:
                    log("\n🎨  Restoring dconf settings", "section")
                    step("Restoring dconf…")
                    if not dry:
                        try:
                            dconf_data = zf.read("migration/dconf-backup.txt").decode()
                            r = subprocess.run(
                                ["dconf", "load", "/"],
                                input=dconf_data, capture_output=True, text=True
                            )
                            if r.returncode == 0:
                                log("  ✔ dconf settings restored", "ok")
                            else:
                                log(f"  ⚠ dconf load: {r.stderr.strip()}", "warn")
                        except Exception as e:
                            log(f"  ✘ dconf restore failed: {e}", "err")
                    else:
                        log("  → Would restore dconf settings", "info")

                # ── System files ──────────────────────────────────────────────
                sys_map = {
                    "sys_hosts": [
                        ("migration/system/etc/hosts",    "/etc/hosts"),
                        ("migration/system/etc/hostname", "/etc/hostname"),
                    ],
                    "sys_fstab": [
                        ("migration/system/etc/fstab", "/etc/fstab"),
                    ],
                    "sys_cron": [
                        ("migration/system/etc/crontab",  "/etc/crontab"),
                        ("migration/system/etc/timezone",  "/etc/timezone"),
                    ],
                }
                section_labels = {
                    "sys_hosts": ("⚙️  Restoring hosts/hostname", "Restoring system files…"),
                    "sys_fstab": ("⚙️  Restoring fstab",          "Restoring fstab…"),
                    "sys_cron":  ("⚙️  Restoring crontab/timezone","Restoring crontab…"),
                }
                for key, file_pairs in sys_map.items():
                    if not opts[key]:
                        continue
                    sec_label, step_label = section_labels[key]
                    log(f"\n{sec_label}", "section")
                    step(step_label)
                    for zip_name, dest in file_pairs:
                        if zip_name in names:
                            sudo_cp(zip_name, dest)
                        else:
                            log(f"  ℹ  {zip_name} not in archive, skipping", "info")

            prog(1.0, "Complete!", "")
            log("\n" + "─" * 52, "section")
            if dry:
                log("  ✅  Dry run complete — nothing was changed", "ok")
            else:
                log("  ✅  Restore complete!", "ok")
                log("  ℹ  You may need to log out and back in for all changes to take effect.", "info")
                GLib.idle_add(self._done_dialog)
            log("─" * 52, "section")

        except Exception as e:
            log(f"\n  ✘ Error: {e}", "err")
            prog(0.0, "Error", str(e))
        finally:
            self._running = False
            GLib.idle_add(self._dry_btn.set_sensitive, True)
            GLib.idle_add(self._restore_btn.set_sensitive, True)

    def _done_dialog(self):
        d = Adw.AlertDialog(
            heading="Restore Complete ✅",
            body="All selected items have been restored.\n\nLog out and back in (or reboot) for all changes to take effect."
        )
        d.add_response("ok", "OK")
        d.present(self._window)


# ── Main window ───────────────────────────────────────────────────────────────

class MintMigrateApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="org.mintmigrate.backup")
        self.connect("activate", self.on_activate)

    def on_activate(self, app):
        win = MintMigrateWindow(application=app)
        win.present()


class MintMigrateWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title("Mint Migration Tool")
        self.set_default_size(660, 820)
        self.set_resizable(True)
        self._build_ui()

    def _build_ui(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(outer)

        # Header with tab switcher
        header = Adw.HeaderBar()
        self._view_switcher = Adw.ViewSwitcher()
        self._view_switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(self._view_switcher)
        outer.append(header)

        # View stack
        self._stack = Adw.ViewStack()
        self._view_switcher.set_stack(self._stack)

        backup_page = BackupPage(self)
        self._stack.add_titled_with_icon(backup_page, "backup", "Backup", "document-save-symbolic")

        restore_page = RestorePage(self)
        self._stack.add_titled_with_icon(restore_page, "restore", "Restore", "document-revert-symbolic")

        outer.append(self._stack)

        # Bottom bar on narrow windows
        bar = Adw.ViewSwitcherBar()
        bar.set_stack(self._stack)
        outer.append(bar)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = MintMigrateApp()
    app.run(sys.argv)
