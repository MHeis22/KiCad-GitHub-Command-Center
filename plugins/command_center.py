import wx
import wx.lib.buttons as wxbuttons
import os
import re
import glob
import json
import subprocess
import pcbnew
import webbrowser
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime


# On Windows, native wx.Button honours background colour, so we keep the native
# control (the original look). macOS ignores background colour on native buttons,
# so there we use an owner-drawn GenButton with theme-aware colours instead.
_IS_MAC = wx.Platform == '__WXMAC__'


def _is_dark_mode():
    bg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)
    return (int(bg.Red()) + int(bg.Green()) + int(bg.Blue())) < 384


def _btn_text_colour():
    return wx.Colour(255, 255, 255) if _is_dark_mode() else wx.Colour(0, 0, 0)


def _theme_colour(light: tuple, dark: tuple) -> wx.Colour:
    rgb = dark if _is_dark_mode() else light
    return wx.Colour(*rgb)


def _action_bg(light: tuple, dark: tuple) -> wx.Colour:
    """Background for a coloured action button. Windows keeps the flat light
    fill; macOS adapts the fill to light/dark theme."""
    return _theme_colour(light, dark) if _IS_MAC else wx.Colour(*light)


def _action_text_colour() -> wx.Colour:
    """Text colour for a coloured action button. Windows keeps black text on the
    light fill (original look); macOS adapts for dark mode."""
    return _btn_text_colour() if _IS_MAC else wx.Colour(0, 0, 0)


def _make_action_button(parent, label, light=None, dark=None, size=(-1, 40)):
    """Creates a coloured action button that matches the platform convention:
    native wx.Button on Windows, owner-drawn GenButton on macOS. Pass light/dark
    RGB tuples to fill it now, or omit them for buttons coloured later (commit/push)."""
    if _IS_MAC:
        btn = wxbuttons.GenButton(parent, label=label, size=size)
    else:
        btn = wx.Button(parent, label=label, size=size)
    if light is not None:
        btn.SetBackgroundColour(_action_bg(light, dark if dark else light))
        btn.SetForegroundColour(_action_text_colour())
    return btn

from .utils import CREATE_NO_WINDOW, load_settings, save_settings, get_last_target, save_last_target
from .ui_dialogs import SettingsDialog, CommitDialog, Model3DSettingsDialog
from .diff_engine import DiffEngine
from .diff_window import DiffWindow
from .readme_generator import ReadmeGenerator
from .bom_generator import BOMGenerator
from .jlcpcb_exporter import JLCPCBExporter
from .model_exporter import Model3DExporter
from .schematic_exporter import SchematicExporter
from .jlcpcb_rules import set_jlcpcb_constraints

class CommandCenterDialog(wx.Dialog):
    def __init__(self, parent, project_dir):
        super().__init__(parent, title="Git Command Center", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.project_dir = project_dir
        self.git_cmd = "git.exe" if os.name == "nt" else "git"
        self.engine = DiffEngine(self.project_dir)
        # Guards for the background status refresh: a monotonic token discards
        # stale results when refreshes overlap, and _alive suppresses UI updates
        # that land after the dialog has been closed.
        self._status_token = 0
        self._alive = True
        # `kicad-cli --version` is slow to cold-start on Windows and isn't needed
        # for the first paint, so warm it up in the background. Reads of the
        # `kicad_version` property block only if that probe hasn't finished yet.
        self._kicad_version = None
        self._kicad_version_lock = threading.Lock()
        threading.Thread(target=self._warm_kicad_version, daemon=True).start()
        self.settings = load_settings()
        
        self.main_panel = wx.Panel(self)
        self.outer_vbox = wx.BoxSizer(wx.VERTICAL)
        
        # Added ScrolledWindow to ensure it scales perfectly on any monitor size
        self.scroll_panel = wx.ScrolledWindow(self.main_panel)
        self.scroll_panel.SetScrollRate(10, 10)
        self.scroll_vbox = wx.BoxSizer(wx.VERTICAL)
        
        # --- Header ---
        header = wx.StaticText(self.scroll_panel, label="Git Hardware Control")
        header_font = header.GetFont()
        header_font.SetWeight(wx.FONTWEIGHT_BOLD)
        header_font.SetPointSize(12)
        header.SetFont(header_font)
        self.scroll_vbox.Add(header, flag=wx.ALIGN_CENTER | wx.TOP | wx.BOTTOM, border=15)
        
        # --- Dynamic Setup Section ---
        self.setup_section_container = None
        if not os.path.isdir(os.path.join(self.project_dir, ".git")):
            self.create_setup_ui()
            self.scroll_vbox.Add(self.setup_section_container, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        # ==========================================
        # GROUP 1: Status & Comparison
        # ==========================================
        box_status = wx.StaticBox(self.scroll_panel, label="Status and Comparison")
        sizer_status = wx.StaticBoxSizer(box_status, wx.VERTICAL)
        
        self.status_lbl = wx.StaticText(self.scroll_panel, label="Checking status...\n")
        sizer_status.Add(self.status_lbl, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)
        
        target_sizer = wx.BoxSizer(wx.HORIZONTAL)
        target_sizer.Add(wx.StaticText(self.scroll_panel, label="Compare against:"), flag=wx.ALIGN_CENTER_VERTICAL|wx.RIGHT, border=5)
        
        targets = self.engine.get_git_targets()
        if not targets:
            targets = ["HEAD"]
            
        self.cb_targets = wx.ComboBox(self.scroll_panel, choices=targets, style=wx.CB_READONLY)
        # Restore last used target for this project
        last_target = get_last_target(self.project_dir)
        if last_target and last_target in targets:
            self.cb_targets.SetSelection(targets.index(last_target))
        else:
            self.cb_targets.SetSelection(0)
        self.cb_targets.Bind(wx.EVT_COMBOBOX, self.on_target_change)
        target_sizer.Add(self.cb_targets, proportion=1, flag=wx.EXPAND)
        
        sizer_status.Add(target_sizer, flag=wx.EXPAND | wx.ALL, border=5)
        self.scroll_vbox.Add(sizer_status, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        # ==========================================
        # GROUP 2: Review & Validation
        # ==========================================
        box_review = wx.StaticBox(self.scroll_panel, label="Review and Validation")
        sizer_review = wx.StaticBoxSizer(box_review, wx.VERTICAL)
        
        btn_diff = _make_action_button(self.scroll_panel, "View Local Changes (Visual Diff)", (220, 240, 255), (40, 90, 160))
        btn_diff.Bind(wx.EVT_BUTTON, self.on_diff)
        
        btn_diff_all = wx.Button(self.scroll_panel, label="View All Files (Including Unchanged)", size=(-1, 40))
        btn_diff_all.Bind(wx.EVT_BUTTON, self.on_diff_all)
        
        self.cb_drc = wx.CheckBox(self.scroll_panel, label="Run DRC Checks (Shows violations as diffs)")
        self.cb_drc.SetToolTip("Executes KiCad's design rules checker on PCB files and compares violations.")
        self.cb_drc.SetValue(False)

        sizer_review.Add(btn_diff, flag=wx.EXPAND | wx.ALL, border=5)
        sizer_review.Add(btn_diff_all, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)
        sizer_review.Add(self.cb_drc, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)
        self.scroll_vbox.Add(sizer_review, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        # ==========================================
        # GROUP 3: Local Operations
        # ==========================================
        box_local = wx.StaticBox(self.scroll_panel, label="Local Workspace")
        sizer_local = wx.StaticBoxSizer(box_local, wx.VERTICAL)
        
        self.btn_commit = _make_action_button(self.scroll_panel, "Save Snapshot (Quick Commit)")
        self.btn_commit.Bind(wx.EVT_BUTTON, self.on_commit)
        
        btn_switch = wx.Button(self.scroll_panel, label="Switch Working Branch", size=(-1, 40))
        btn_switch.Bind(wx.EVT_BUTTON, self.on_switch_branch)

        stash_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_stash = wx.Button(self.scroll_panel, label="Stash Local Changes", size=(-1, 40))
        btn_pop = wx.Button(self.scroll_panel, label="Pop Last Stash", size=(-1, 40))
        btn_stash.Bind(wx.EVT_BUTTON, self.on_stash)
        btn_pop.Bind(wx.EVT_BUTTON, self.on_pop)
        stash_sizer.Add(btn_stash, proportion=1, flag=wx.RIGHT, border=2)
        stash_sizer.Add(btn_pop, proportion=1, flag=wx.LEFT, border=2)

        btn_tag = wx.Button(self.scroll_panel, label="Create Version Tag (v1.0.0)", size=(-1, 40))
        btn_tag.Bind(wx.EVT_BUTTON, self.on_create_tag)
        btn_tag.SetToolTip("Create a semantic version tag on the current commit (e.g. v1.0.0). Remember to push tags separately.")

        sizer_local.Add(self.btn_commit, flag=wx.EXPAND | wx.ALL, border=5)
        sizer_local.Add(btn_switch, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)
        sizer_local.Add(stash_sizer, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)
        sizer_local.Add(btn_tag, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)

        # --- JLCPCB Constraints Enforcer ---
        btn_jlc_rules = _make_action_button(self.scroll_panel, "Set JLCPCB Safe Constraints (Free Tier)", (230, 230, 250), (90, 70, 160))
        btn_jlc_rules.Bind(wx.EVT_BUTTON, self.on_set_jlc_constraints)
        sizer_local.Add(btn_jlc_rules, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)

        # JLCPCB Gerber generation toggle
        self.cb_gerbers = wx.CheckBox(self.scroll_panel, label="Auto-Generate JLCPCB Gerbers on Commit")
        self.cb_gerbers.SetValue(self.settings.get('generate_gerbers_zip', False))
        self.cb_gerbers.Bind(wx.EVT_CHECKBOX, self.on_gerber_toggle)
        sizer_local.Add(self.cb_gerbers, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)

        # --- 3D Model & Render Settings ---
        btn_3d = _make_action_button(self.scroll_panel, "3D Model and Render Settings...", (230, 230, 250), (90, 70, 160))
        btn_3d.SetToolTip("Configure STEP model export and PCB image rendering generated on commit.")
        btn_3d.Bind(wx.EVT_BUTTON, self.on_3d_settings)
        sizer_local.Add(btn_3d, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)

        # --- Manual file generation (hidden unless enabled in Settings) ---
        self.btn_gen = _make_action_button(self.scroll_panel, "Generate Project Files", (255, 235, 200), (150, 110, 30))
        self.btn_gen.SetToolTip("Run all enabled file generation now (BOMs, Gerbers, 3D model, renders, schematic, README) without committing.")
        self.btn_gen.Bind(wx.EVT_BUTTON, self.on_generate_files)
        sizer_local.Add(self.btn_gen, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)

        self.scroll_vbox.Add(sizer_local, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        # ==========================================
        # GROUP 4: Remote / Sync
        # ==========================================
        box_remote = wx.StaticBox(self.scroll_panel, label="Remote and Sync")
        sizer_remote = wx.StaticBoxSizer(box_remote, wx.VERTICAL)

        self.btn_push = _make_action_button(self.scroll_panel, "Push Changes to Remote")
        self.btn_push.Bind(wx.EVT_BUTTON, self.on_push)

        btn_remote = wx.Button(self.scroll_panel, label="Open Remote Web Page", size=(-1, 40))
        btn_remote.Bind(wx.EVT_BUTTON, self.on_open_remote)

        btn_sync = _make_action_button(self.scroll_panel, "Download from Server (Force Sync)", (255, 200, 200), (160, 50, 50))
        btn_sync.Bind(wx.EVT_BUTTON, self.on_force_sync)

        sizer_remote.Add(self.btn_push, flag=wx.EXPAND | wx.ALL, border=5)
        sizer_remote.Add(btn_remote, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)
        sizer_remote.Add(btn_sync, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)
        self.scroll_vbox.Add(sizer_remote, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        # --- Help Text ---
        help_box = wx.StaticBox(self.scroll_panel, label="Force Sync Instructions")
        help_sizer = wx.StaticBoxSizer(help_box, wx.VERTICAL)
        help_text = (
            "TO SEE CHANGES AFTER FORCE SYNC/SWITCH/POP:\n"
            "1. Run 'Download from Server (Force Sync)'.\n"
            "2. Close your PCB and Schematic editor.\n"
            "3. If KiCad asks to save, select 'DISCARD CHANGES'.\n"
            "4. Re-open the file to see the loaded version."
        )
        st_help = wx.StaticText(self.scroll_panel, label=help_text)
        st_help.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        help_sizer.Add(st_help, flag=wx.ALL, border=5)
        self.scroll_vbox.Add(help_sizer, flag=wx.EXPAND | wx.ALL, border=10)
        
        self.scroll_panel.SetSizer(self.scroll_vbox)
        # Hide the manual-generation button up front unless the setting is on, so
        # it doesn't reserve layout space or appear in automatic mode.
        self._update_gen_button_visibility()

        # --- Persistent Bottom Bar ---
        bottom_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_settings = wx.Button(self.main_panel, label="⚙ Settings")
        btn_settings.Bind(wx.EVT_BUTTON, self.on_settings)
        btn_close = wx.Button(self.main_panel, label="Close")
        btn_close.Bind(wx.EVT_BUTTON, self.on_close)
        
        bottom_sizer.Add(btn_settings, flag=wx.LEFT, border=15)
        bottom_sizer.AddStretchSpacer()
        bottom_sizer.Add(btn_close, flag=wx.RIGHT, border=15)
        
        self.outer_vbox.Add(self.scroll_panel, proportion=1, flag=wx.EXPAND | wx.ALL, border=0)
        self.outer_vbox.Add(bottom_sizer, flag=wx.EXPAND | wx.BOTTOM | wx.TOP, border=15)
        self.main_panel.SetSizer(self.outer_vbox)
        
        # Calculate optimal size dynamically based on environment
        best_scroll_size = self.scroll_vbox.GetMinSize()
        display_rect = wx.GetClientDisplayRect()
        max_height = int(display_rect.height * 0.85)
        
        target_width = max(550, best_scroll_size.width + 60)
        target_height = min(best_scroll_size.height + 120, max_height)
        
        self.SetMinSize((500, 400)) # Guarantee UI doesn't become squished/unusable
        self.SetSize((target_width, target_height))
        self.CenterOnScreen()
        self.Layout()

        self.update_git_status()
        self._check_and_prompt_git_encoding()
        threading.Thread(target=self._check_for_updates, daemon=True).start()

    def _warm_kicad_version(self):
        """Background probe for the KiCad version (see __init__)."""
        with self._kicad_version_lock:
            if self._kicad_version is None:
                self._kicad_version = self.engine.get_kicad_version()

    @property
    def kicad_version(self):
        """The installed KiCad version string. Returns the warmed-up value, or
        computes it on first access if the background probe hasn't finished."""
        with self._kicad_version_lock:
            if self._kicad_version is None:
                self._kicad_version = self.engine.get_kicad_version()
            return self._kicad_version

    def _check_for_updates(self):
        try:
            metadata_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "metadata.json")
            with open(metadata_path, "r") as f:
                metadata = json.load(f)
            versions = metadata.get("versions", [])
            current = versions[0].get("version", "0.0.0") if versions else "0.0.0"

            api_url = "https://api.github.com/repos/MHeis22/KiCad-GitHub-Command-Center/releases/latest"
            req = urllib.request.Request(api_url, headers={"User-Agent": "KiCad-GitHub-Command-Center"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())

            latest_tag = data.get("tag_name", "").lstrip("v")
            release_url = data.get("html_url", "")

            def _parse(v):
                try:
                    return tuple(int(x) for x in v.split("."))
                except Exception:
                    return (0, 0, 0)

            if _parse(latest_tag) > _parse(current):
                wx.CallAfter(self._show_update_prompt, current, latest_tag, release_url)
        except Exception:
            pass  # silently ignore network errors on startup

    def _show_update_prompt(self, current, latest, release_url):
        msg = (
            f"A new version of GitHub Command Center is available!\n\n"
            f"  Installed: v{current}\n"
            f"  Latest:    v{latest}\n\n"
            "Would you like to open the release page?"
        )
        dlg = wx.MessageDialog(self, msg, "Update Available", wx.YES_NO | wx.ICON_INFORMATION)
        if dlg.ShowModal() == wx.ID_YES and release_url:
            webbrowser.open(release_url)
        dlg.Destroy()

    def on_set_jlc_constraints(self, event):
        set_jlcpcb_constraints(self)

    def on_gerber_toggle(self, event):
        self.settings['generate_gerbers_zip'] = self.cb_gerbers.GetValue()
        save_settings(self.settings)

    def on_3d_settings(self, event):
        dlg = Model3DSettingsDialog(self, self.settings, kicad_version=self.kicad_version)
        if dlg.ShowModal() == wx.ID_OK:
            self.settings = dlg.get_settings()
            save_settings(self.settings)
        dlg.Destroy()

    def _check_and_prompt_git_encoding(self, force_prompt=False):
        if not os.path.isdir(os.path.join(self.project_dir, ".git")):
            return False
            
        try:
            status_dict = self.engine.get_git_status(target="HEAD")
            has_escaped_files = any('\\' in f for f in status_dict.keys())
            
            has_non_ascii = any(ord(c) > 127 for c in self.project_dir)
            if not has_non_ascii:
                for f in os.listdir(self.project_dir):
                    if any(ord(c) > 127 for c in f):
                        has_non_ascii = True
                        break

            if has_non_ascii or has_escaped_files or force_prompt:
                res = subprocess.run([self.git_cmd, "-C", self.project_dir, "config", "--get", "core.quotePath"], 
                                     capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
                
                if res.stdout.strip() != "false":
                    msg = ("Special characters (like ö, ä, å) were detected in your project path or files.\n\n"
                           "Git by default escapes these characters (e.g. '\\303\\266'), which will cause operations like Commit to fail.\n\n"
                           "Would you like to automatically configure Git to handle these characters correctly?")
                    
                    dlg = wx.MessageDialog(self, msg, "Fix Git Character Encoding", wx.YES_NO | wx.ICON_WARNING)
                    result = dlg.ShowModal()
                    dlg.Destroy()
                    
                    if result == wx.ID_YES:
                        subprocess.run([self.git_cmd, "-C", self.project_dir, "config", "core.quotePath", "false"], creationflags=CREATE_NO_WINDOW)
                        wx.MessageBox("Git encoding fixed! Filenames will now display correctly.", "Success")
                        self.update_git_status()
                        return True
        except Exception as e:
            print(f"Error checking git encoding: {e}")
            
        return False

    def create_setup_ui(self):
        setup_box = wx.StaticBox(self.scroll_panel, label="New Project Setup")
        self.setup_section_container = wx.StaticBoxSizer(setup_box, wx.VERTICAL)
        
        btn_setup = _make_action_button(self.scroll_panel, "Initialize and Link to Remote", (200, 255, 200), (30, 130, 60), size=wx.DefaultSize)
        btn_setup.Bind(wx.EVT_BUTTON, self.on_setup_repo)
        
        self.setup_section_container.Add(btn_setup, flag=wx.EXPAND | wx.ALL, border=5)

    def on_settings(self, event):
        dlg = SettingsDialog(self, self.settings)
        if dlg.ShowModal() == wx.ID_OK:
            self.settings = dlg.get_settings()
            save_settings(self.settings)
            
            # Sync local checkbox with newly saved setting
            self.cb_gerbers.SetValue(self.settings.get('generate_gerbers_zip', False))
            # Show/hide the manual-generation button to match the new mode.
            self._update_gen_button_visibility()
        dlg.Destroy()

    def on_target_change(self, event):
        save_last_target(self.project_dir, self.cb_targets.GetStringSelection())
        self.update_git_status()

    def update_git_status(self):
        if not os.path.isdir(os.path.join(self.project_dir, ".git")):
            self.status_lbl.SetLabel("Status: Not a Git repository.")
            if hasattr(self, 'btn_commit'):
                self.btn_commit.SetBackgroundColour(_action_bg((240, 240, 240), (70, 70, 70)))
                self.btn_push.SetBackgroundColour(_action_bg((240, 240, 240), (70, 70, 70)))
                self.btn_commit.SetForegroundColour(_action_text_colour())
                self.btn_push.SetForegroundColour(_action_text_colour())
            return

        # The git work here (status plus reorder-noise filtering, which shells out
        # once per changed KiCad file) is too slow to run on the UI thread every
        # refresh, so compute it in the background and apply via CallAfter. The
        # token discards results from a superseded refresh.
        target_raw = self.cb_targets.GetStringSelection()
        self.status_lbl.SetLabel("Checking status...")
        self._status_token += 1
        token = self._status_token
        threading.Thread(target=self._compute_git_status,
                         args=(target_raw, token), daemon=True).start()

    def _compute_git_status(self, target_raw, token):
        """Background worker for update_git_status(): does only git/subprocess
        work, then hands the result to _apply_git_status on the main thread."""
        try:
            # Single call gives us branch name, ahead/behind, and porcelain status
            res_sb = subprocess.run([self.git_cmd, "-C", self.project_dir, "status", "-sb"],
                                    capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            first_line = res_sb.stdout.split('\n')[0] if res_sb.stdout else ''
            branch_match = re.match(r'^## (\S+?)(?:\.\.\.|\s|$)', first_line)
            curr_branch = branch_match.group(1) if branch_match else "Detached HEAD"
            is_ahead = "[ahead" in first_line

            actual_target = target_raw.split(' ')[0] if ' ' in target_raw else target_raw

            # Ignore KiCad re-serialization (reorder) noise so a cosmetic Ctrl+S
            # doesn't show up as a committable change.
            status_dict = self.engine.filter_reorder_noise(
                self.engine.get_git_status(target=actual_target), target=actual_target)
            changes = len(status_dict)

            # Reuse status_dict when target is HEAD; otherwise get HEAD status separately
            if actual_target == "HEAD":
                head_status = status_dict
            else:
                head_status = self.engine.filter_reorder_noise(
                    self.engine.get_git_status(target="HEAD"), target="HEAD")
            uncommitted_changes = len(head_status) > 0

            wx.CallAfter(self._apply_git_status, token, {
                'curr_branch': curr_branch, 'actual_target': actual_target,
                'changes': changes, 'is_ahead': is_ahead,
                'uncommitted_changes': uncommitted_changes,
            }, None)
        except Exception as e:
            wx.CallAfter(self._apply_git_status, token, None, str(e))

    def _apply_git_status(self, token, data, error):
        """Main-thread UI update for update_git_status(). Ignores superseded
        refreshes and no-ops if the dialog has already been closed."""
        if token != self._status_token or not self._alive:
            return
        try:
            if error is not None:
                self.status_lbl.SetLabel(f"Status: Git Error. {error}")
                return

            status_text = f"Working Branch: '{data['curr_branch']}'\n"
            if data['changes'] > 0:
                status_text += f"Status: {data['changes']} changes relative to {data['actual_target']}."
            else:
                status_text += f"Status: Workspace identical to {data['actual_target']}."
            self.status_lbl.SetLabel(status_text)

            if hasattr(self, 'btn_commit'):
                commit_font = self.btn_commit.GetFont()
                if data['uncommitted_changes']:
                    self.btn_commit.SetBackgroundColour(_action_bg((150, 255, 150), (30, 150, 60)))
                    commit_font.SetWeight(wx.FONTWEIGHT_BOLD)
                else:
                    self.btn_commit.SetBackgroundColour(_action_bg((230, 245, 230), (40, 90, 40)))
                    commit_font.SetWeight(wx.FONTWEIGHT_NORMAL)

                self.btn_commit.SetForegroundColour(_action_text_colour())
                self.btn_commit.SetFont(commit_font)

                push_font = self.btn_push.GetFont()
                if data['is_ahead']:
                    self.btn_push.SetBackgroundColour(_action_bg((255, 180, 100), (180, 100, 20)))
                    push_font.SetWeight(wx.FONTWEIGHT_BOLD)
                else:
                    self.btn_push.SetBackgroundColour(_action_bg((255, 240, 220), (120, 80, 30)))
                    push_font.SetWeight(wx.FONTWEIGHT_NORMAL)

                self.btn_push.SetForegroundColour(_action_text_colour())
                self.btn_push.SetFont(push_font)

                self.btn_commit.Refresh()
                self.btn_push.Refresh()
        except RuntimeError:
            pass  # dialog was destroyed between the CallAfter and now

    def create_default_gitignore(self):
        gitignore_path = os.path.join(self.project_dir, ".gitignore")
        if not os.path.exists(gitignore_path):
            content = (
                "# KiCad modern backups (KiCad 7+)\n"
                "*-backups/\n\n"
                "# KiCad lock files\n"
                "*.lck\n"
                "~*.lck\n\n"
                "# KiCad legacy backups and autosaves\n"
                "*.bak\n*.kicad_pcb-bak\n*.kicad_sch-bak\n*.kicad_pro-bak\n"
                "*-save.pro\n*-save.kicad_pcb\n*-save.kicad_sch\n"
                "*_autosave-*\n_autosave-*\n\n"
                "# KiCad caches\n"
                "fp-info-cache\n\n"
                "# Plugin Temporary Files\n"
                "tmp_git_old_*\n\n"
                "# IDE & History Folders\n"
                ".history/\n"
                ".history_trim/\n\n"
                "# Generated files\n"
                "*.bck\n*.kicad_pcb-shl\npython_environment/\n\n"
                "# OS files\n.DS_Store\nThumbs.db\n"
            )
            with open(gitignore_path, "w") as f:
                f.write(content)

    def on_setup_repo(self, event):
        dlg = wx.TextEntryDialog(self, 
            "Paste your Git Repository URL (GitHub, GitLab, etc. (for local repositories, just press Save Snapshot/Quick Commit)):",
            "Link to Remote")
        
        if dlg.ShowModal() == wx.ID_OK:
            url = dlg.GetValue().strip()
            if not url: 
                dlg.Destroy()
                return

            wx.BeginBusyCursor()
            try:
                if not os.path.isdir(os.path.join(self.project_dir, ".git")):
                    subprocess.run([self.git_cmd, "-C", self.project_dir, "init"], check=True, creationflags=CREATE_NO_WINDOW)
                
                if not os.path.exists(os.path.join(self.project_dir, ".gitignore")):
                    if wx.IsBusy(): wx.EndBusyCursor()
                    create_gi = wx.MessageBox("Create a default .gitignore file for KiCad?", "Create .gitignore?", wx.YES_NO | wx.ICON_QUESTION)
                    if create_gi == wx.YES:
                        self.create_default_gitignore()
                    wx.BeginBusyCursor()
                
                res_rem = subprocess.run([self.git_cmd, "-C", self.project_dir, "remote", "add", "origin", url], 
                                         capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
                
                if res_rem.returncode != 0:
                    subprocess.run([self.git_cmd, "-C", self.project_dir, "remote", "set-url", "origin", url], creationflags=CREATE_NO_WINDOW)

                wx.MessageBox("Project linked to remote successfully!", "Success")
                
                if self.setup_section_container:
                    for item in self.setup_section_container.GetChildren():
                        if item.IsWindow():
                            item.GetWindow().Destroy()
                    box = self.setup_section_container.GetStaticBox()
                    if box:
                        box.Destroy()
                    
                    self.scroll_vbox.Detach(self.setup_section_container)
                    self.setup_section_container = None
                    
                    self.scroll_vbox.Layout()
                    self.scroll_panel.FitInside()
                    self.main_panel.Layout()
                    self.Layout()
                    self.Refresh()
                    self.Update()

                self.update_git_status()
                
                new_targets = self.engine.get_git_targets()
                if new_targets:
                    self.cb_targets.SetItems(new_targets)
                    self.cb_targets.SetSelection(0)
                        
            except Exception as e:
                wx.MessageBox(f"Failed to setup repository: {e}", "Error", wx.ICON_ERROR)
            finally:
                if wx.IsBusy(): wx.EndBusyCursor()
                
            self._check_and_prompt_git_encoding()
        dlg.Destroy()

    def on_switch_branch(self, event):
        if not os.path.isdir(os.path.join(self.project_dir, ".git")):
            wx.MessageBox("No Git repo found.", "Error")
            return
            
        wx.BeginBusyCursor()
        try:
            res = subprocess.run([self.git_cmd, "-C", self.project_dir, "branch", "--format=%(refname:short)"],
                                 capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            branches = [b.strip() for b in res.stdout.split('\n') if b.strip()]
            
            res_curr = subprocess.run([self.git_cmd, "-C", self.project_dir, "branch", "--show-current"], 
                                      capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            curr = res_curr.stdout.strip()
        finally:
            if wx.IsBusy(): wx.EndBusyCursor()
            
        if not branches:
            wx.MessageBox("No branches found.", "Error")
            return
            
        dlg = wx.SingleChoiceDialog(self, "Select branch to switch to:", "Switch Branch", branches)
        if curr in branches:
            dlg.SetSelection(branches.index(curr))
            
        if dlg.ShowModal() == wx.ID_OK:
            selected = dlg.GetStringSelection()
            if selected != curr:
                wx.BeginBusyCursor()
                try:
                    res_switch = subprocess.run([self.git_cmd, "-C", self.project_dir, "checkout", selected], 
                                                capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
                    if res_switch.returncode != 0:
                        wx.MessageBox(f"Checkout Failed.\n\n{res_switch.stderr}", "Git Error", wx.ICON_ERROR)
                    else:
                        wx.MessageBox(f"Switched to branch '{selected}'.", "Success")
                        self.update_git_status()
                        # Refresh the compare list so it reflects the new branch
                        # context, preserving the current selection if still valid.
                        new_targets = self.engine.get_git_targets()
                        if new_targets:
                            prev = self.cb_targets.GetStringSelection()
                            self.cb_targets.SetItems(new_targets)
                            self.cb_targets.SetSelection(
                                new_targets.index(prev) if prev in new_targets else 0)
                finally:
                    if wx.IsBusy(): wx.EndBusyCursor()
        dlg.Destroy()

    def on_stash(self, event):
        if not os.path.isdir(os.path.join(self.project_dir, ".git")):
            wx.MessageBox("No Git repo found.", "Error")
            return
            
        wx.BeginBusyCursor()
        try:
            res = subprocess.run([self.git_cmd, "-C", self.project_dir, "stash"], 
                                 capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            if res.returncode == 0:
                wx.MessageBox(f"Stash successful:\n{res.stdout.strip()}", "Success")
                self.update_git_status()
            else:
                wx.MessageBox(f"Stash failed:\n{res.stderr or res.stdout}", "Git Error", wx.ICON_ERROR)
        finally:
            if wx.IsBusy(): wx.EndBusyCursor()

    def on_pop(self, event):
        if not os.path.isdir(os.path.join(self.project_dir, ".git")):
            wx.MessageBox("No Git repo found.", "Error")
            return

        # Show stash contents before popping so the user knows what they're restoring
        list_res = subprocess.run([self.git_cmd, "-C", self.project_dir, "stash", "list"],
                                  capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
        show_res = subprocess.run([self.git_cmd, "-C", self.project_dir, "stash", "show", "--stat"],
                                  capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)

        stash_list = list_res.stdout.strip()
        stash_stat = show_res.stdout.strip()

        if not stash_list:
            wx.MessageBox("No stashes found.", "Info")
            return

        preview = stash_list
        if stash_stat:
            preview += f"\n\nTop stash changes:\n{stash_stat}"

        confirm_dlg = wx.MessageDialog(
            self, f"{preview}\n\nPop the top stash?",
            "Stash Contents", wx.YES_NO | wx.ICON_QUESTION
        )
        should_pop = confirm_dlg.ShowModal() == wx.ID_YES
        confirm_dlg.Destroy()

        if not should_pop:
            return

        wx.BeginBusyCursor()
        try:
            res = subprocess.run([self.git_cmd, "-C", self.project_dir, "stash", "pop"],
                                 capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            if res.returncode == 0:
                wx.MessageBox(f"Stash popped successfully:\n{res.stdout.strip()}", "Success")
                self.update_git_status()
            else:
                wx.MessageBox(f"Stash pop failed:\n{res.stderr.strip()}", "Git Error", wx.ICON_ERROR)
        finally:
            if wx.IsBusy(): wx.EndBusyCursor()

    def on_create_tag(self, event):
        if not os.path.isdir(os.path.join(self.project_dir, ".git")):
            wx.MessageBox("No Git repository found.", "Error", wx.ICON_ERROR)
            return

        # Fetch existing version tags to suggest the next patch number
        res = subprocess.run(
            [self.git_cmd, "-C", self.project_dir, "tag", "--sort=-version:refname", "-l", "v*"],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW
        )
        tags = [t.strip() for t in res.stdout.split('\n') if t.strip()]

        suggested = "v1.0.0"
        if tags:
            import re as _re
            m = _re.match(r'v?(\d+)\.(\d+)\.(\d+)', tags[0])
            if m:
                major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
                suggested = f"v{major}.{minor}.{patch + 1}"

        existing_summary = ", ".join(tags[:5]) if tags else "None"
        dlg = wx.TextEntryDialog(
            self,
            f"Enter a semantic version tag (e.g. {suggested}):\n\nRecent tags: {existing_summary}",
            "Create Version Tag",
            suggested
        )
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        tag_name = dlg.GetValue().strip()
        dlg.Destroy()

        if not tag_name:
            return

        msg_dlg = wx.TextEntryDialog(
            self,
            "Optional annotation message (leave blank for a lightweight tag):",
            "Tag Message",
            f"Release {tag_name}"
        )
        annotation = ""
        if msg_dlg.ShowModal() == wx.ID_OK:
            annotation = msg_dlg.GetValue().strip()
        msg_dlg.Destroy()

        wx.BeginBusyCursor()
        try:
            if annotation:
                cmd = [self.git_cmd, "-C", self.project_dir, "tag", "-a", tag_name, "-m", annotation]
            else:
                cmd = [self.git_cmd, "-C", self.project_dir, "tag", tag_name]
            result = subprocess.run(cmd, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            if result.returncode == 0:
                wx.MessageBox(
                    f"Tag '{tag_name}' created successfully.\n\n"
                    "To share it, push tags to remote:\n  git push origin --tags",
                    "Tag Created"
                )
            else:
                wx.MessageBox(f"Tag creation failed:\n{result.stderr.strip()}", "Git Error", wx.ICON_ERROR)
        finally:
            if wx.IsBusy():
                wx.EndBusyCursor()

    def _make_progress_callback(self):
        def progress(current, total, fname):
            self.status_lbl.SetLabel(f"Processing {current}/{total}: {fname}")
            self.status_lbl.Refresh()
            wx.SafeYield()
        return progress

    def on_diff(self, event):
        wx.BeginBusyCursor()
        try:
            selected_target = self.cb_targets.GetStringSelection()
            run_checks = self.cb_drc.GetValue()

            diffs, summary = self.engine.render_all_diffs(
                show_unchanged=False, compare_target=selected_target,
                run_drc=run_checks, progress_callback=self._make_progress_callback()
            )
            if not diffs:
                wx.MessageBox(f"No local changes detected against {selected_target}.", "Info")
            else:
                win = DiffWindow(diffs, summary, target_name=selected_target, kicad_version=self.kicad_version)
                win.Show()
        finally:
            if wx.IsBusy(): wx.EndBusyCursor()
            self.update_git_status()

    def on_diff_all(self, event):
        wx.BeginBusyCursor()
        try:
            selected_target = self.cb_targets.GetStringSelection()
            run_checks = self.cb_drc.GetValue()

            diffs, summary = self.engine.render_all_diffs(
                show_unchanged=True, compare_target=selected_target,
                run_drc=run_checks, progress_callback=self._make_progress_callback()
            )
            if not diffs:
                wx.MessageBox(f"No schematic or PCB files found to render.", "Info")
            else:
                win = DiffWindow(diffs, summary, target_name=selected_target, kicad_version=self.kicad_version)
                win.Show()
        finally:
            if wx.IsBusy(): wx.EndBusyCursor()
            self.update_git_status()

    def on_force_sync(self, event):
        if not os.path.isdir(os.path.join(self.project_dir, ".git")):
            wx.MessageBox("No Git repo found.", "Error")
            return

        res_br = subprocess.run([self.git_cmd, "-C", self.project_dir, "branch", "--show-current"], 
                                capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
        curr = res_br.stdout.strip() or "main"
        
        dlg = wx.TextEntryDialog(self, "Enter branch to download (e.g. origin/main):", "Force Download", f"origin/{curr}")
        if dlg.ShowModal() == wx.ID_OK:
            target = dlg.GetValue().strip()
            if target:
                warn_msg = (
                    f"WARNING: You are about to force sync from '{target}'.\n\n"
                    "This will PERMANENTLY OVERWRITE your local workspace.\n"
                    "All uncommitted changes and new untracked files will be DESTROYED.\n\n"
                    "Are you absolutely sure you want to continue?"
                )
                warn_dlg = wx.MessageDialog(self, warn_msg, "Destructive Action Warning", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING)
                result = warn_dlg.ShowModal()
                warn_dlg.Destroy()
                
                if result == wx.ID_YES:
                    # Offer a backup branch before destroying local state
                    backup_dlg = wx.MessageDialog(
                        self,
                        "Would you like to save your current local state to a backup branch first?\n\n"
                        "This commits ALL current work — including uncommitted and untracked\n"
                        "files — onto a new branch so it can be recovered after the sync.",
                        "Create Backup Branch?",
                        wx.YES_NO | wx.ICON_QUESTION
                    )
                    want_backup = backup_dlg.ShowModal() == wx.ID_YES
                    backup_dlg.Destroy()

                    if want_backup:
                        wx.BeginBusyCursor()
                        try:
                            backup_name, err = self._create_backup_snapshot()
                        finally:
                            if wx.IsBusy(): wx.EndBusyCursor()

                        if err:
                            proceed_dlg = wx.MessageDialog(
                                self,
                                f"Could not create a backup:\n\n{err}\n\n"
                                "Continue with the force sync anyway? Your uncommitted and "
                                "untracked work WILL be permanently lost.",
                                "Backup Failed", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING
                            )
                            go_on = proceed_dlg.ShowModal() == wx.ID_YES
                            proceed_dlg.Destroy()
                            if not go_on:
                                dlg.Destroy()
                                return
                        else:
                            wx.MessageBox(
                                f"Backup branch '{backup_name}' created with all your current "
                                "work (committed, uncommitted, and untracked).\n\n"
                                f"To recover it later, run:\n  git checkout {backup_name}",
                                "Backup Created"
                            )

                    self.perform_atomic_overwrite(target)
        dlg.Destroy()

    def _create_backup_snapshot(self):
        """Saves the FULL current working state — committed history plus any
        uncommitted and untracked files — onto a new 'backup_<timestamp>' branch,
        then returns to the original branch. A bare 'git branch' only bookmarks
        HEAD, so it would NOT protect the uncommitted/untracked work that the
        subsequent 'reset --hard' + 'clean -fd' destroys; committing it onto the
        backup branch actually does.

        Returns (backup_branch_name, None) on success, or (None, error_message).
        """
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_name = f"backup_{stamp}"

        def run(args):
            return subprocess.run([self.git_cmd, "-C", self.project_dir] + args,
                                  capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)

        # Where to return to afterwards: the branch name, or the commit SHA if
        # we're on a detached HEAD.
        original = run(["symbolic-ref", "--quiet", "--short", "HEAD"]).stdout.strip()
        if not original:
            original = run(["rev-parse", "HEAD"]).stdout.strip()
        if not original:
            return None, "Could not determine the current branch or commit."

        dirty = bool(run(["status", "--porcelain"]).stdout.strip())

        created = run(["checkout", "-b", backup_name])
        if created.returncode != 0:
            return None, (created.stderr.strip() or created.stdout.strip()
                          or "Could not create the backup branch.")

        if dirty:
            run(["add", "-A"])
            committed = run(["commit", "-m", f"Backup before force sync ({stamp})"])
            if committed.returncode != 0:
                # Roll back the half-made backup and abort, so we never sync
                # believing the work was saved when it wasn't.
                run(["checkout", "-f", original])
                run(["branch", "-D", backup_name])
                return None, ("Could not commit your changes to the backup branch:\n"
                              + (committed.stderr.strip() or committed.stdout.strip()
                                 or "unknown error")
                              + "\n\n(Is git user.name / user.email configured?)")

        back = run(["checkout", original])
        if back.returncode != 0:
            return None, (f"Work was saved to '{backup_name}', but returning to "
                          f"'{original}' failed:\n{back.stderr.strip()}")

        return backup_name, None

    def perform_atomic_overwrite(self, remote_ref):
        wx.BeginBusyCursor()
        try:
            subprocess.run([self.git_cmd, "-C", self.project_dir, "fetch", "origin"], creationflags=CREATE_NO_WINDOW)
            subprocess.run([self.git_cmd, "-C", self.project_dir, "reset", "--hard", remote_ref], 
                                 capture_output=True, text=True, check=True, creationflags=CREATE_NO_WINDOW)
            subprocess.run([self.git_cmd, "-C", self.project_dir, "clean", "-fd"], creationflags=CREATE_NO_WINDOW)

            pcbnew.Refresh()
            wx.MessageBox("SUCCESS!\n\nLocal files updated. Remember to 'Discard Changes' if KiCad prompts you.", "Sync Complete")
            self.update_git_status()
        except Exception as e:
            wx.MessageBox(f"Sync Failed: {e}", "Git Error")
        finally:
            if wx.IsBusy(): wx.EndBusyCursor()

    def _set_status(self, msg):
        """Immediately paints a status message (used as the generation progress
        callback, mirroring the live feedback the push flow shows)."""
        self.status_lbl.SetLabel(f"Status: {msg}")
        self.status_lbl.Refresh()
        self.status_lbl.Update()
        wx.SafeYield()

    def _generate_extra_files(self, progress=None, force=False):
        """Runs every enabled 'extra file' generator (3D STEP, PCB render,
        schematic SVG, README, BOMs, JLCPCB gerbers), writing outputs into the
        project. Each generator is guarded independently so one failure doesn't
        block the rest. `progress` is an optional callback taking a status string.

        The heavy generators are each a separate 'kicad-cli' subprocess (3D STEP
        export, PCB render, schematic SVG) with no shared state, so they run
        concurrently in a thread pool for a large wall-clock win. README (needs
        the fresh images), BOMs, and gerbers run afterwards on the main thread —
        gerbers in particular MUST stay on the main thread because they call the
        pcbnew plotting API, which is not thread-safe.

        When `force` is True (the manual "Generate Project Files" button), every
        enabled generator runs regardless of git state. When False (automatic on
        commit), each board/schematic artifact is generated only if its source
        changed OR its output file is missing — so a cosmetic Ctrl+S doesn't churn
        the files, but enabling a feature still produces its output the first
        time even if the source is committed/unchanged."""
        def step(msg):
            if progress:
                progress(msg)

        pcb_files = glob.glob(os.path.join(self.project_dir, "*.kicad_pcb"))
        pcb_file = pcb_files[0] if pcb_files else None

        export_step = self.settings.get('export_step', False)
        render_image = self.settings.get('render_image', False)
        gerbers_on = self.settings.get('generate_gerbers_zip', False)
        auto_readme = self.settings.get('auto_readme', False)

        # Only pay for the PCB change-check when a PCB-derived artifact needs it.
        need_pcb_flag = pcb_file is not None and (export_step or render_image or gerbers_on)
        pcb_updated = self.engine.file_content_changed(pcb_file, target="HEAD") if need_pcb_flag else False

        # Assemble the independent kicad-cli jobs. Each entry is
        # key -> (human label, zero-arg callable returning its result).
        # A board artifact runs when forced, when the PCB genuinely changed, or
        # when its output doesn't exist yet (first-time enable).
        jobs = {}
        if (export_step or render_image) and pcb_file:
            model_exporter = Model3DExporter(self.project_dir, self.settings, self.kicad_version)
            if export_step and (force or pcb_updated or not model_exporter.step_output_exists()):
                jobs['step'] = ("3D STEP model", model_exporter.export_step)
            if render_image and (force or pcb_updated or not model_exporter.renders_exist()):
                jobs['render'] = ("PCB render", model_exporter.render_images)
            if (render_image and self.settings.get('render_dimensions', False)
                    and (force or pcb_updated or not model_exporter.dimensioned_exists('top'))):
                jobs['dimensioned'] = ("Dimensioned drawing", lambda: model_exporter.render_dimensioned('top'))
            if not jobs and (export_step or render_image):
                print("GitHub Command Center: PCB unchanged and outputs present - skipping STEP/render generation.")

        if self.settings.get('export_schematic', False):
            sch_files = glob.glob(os.path.join(self.project_dir, "*.kicad_sch"))
            if sch_files:
                sch_exporter = SchematicExporter(self.project_dir, self.settings, self.kicad_version)
                sch_updated = any(self.engine.file_content_changed(s, target="HEAD") for s in sch_files)
                # Also export when the SVG doesn't exist yet, so enabling the
                # feature on an unchanged/committed schematic still produces it
                # (the change gate alone would skip it forever otherwise).
                if force or sch_updated or not sch_exporter.output_exists():
                    jobs['schematic'] = ("Schematic image", sch_exporter.export_svgs)
                else:
                    print("GitHub Command Center: Schematic unchanged and already exported - skipping.")

        # The README DRC status is another independent kicad-cli call, so run it
        # in the pool and hand the result to the README step instead of blocking
        # on it. Only worthwhile when the README will actually be regenerated.
        rg = None
        drc_status = None
        if auto_readme and self.settings.get('readme_drc', False) and pcb_file:
            rg = ReadmeGenerator(self.project_dir, self.settings)
            jobs['drc'] = ("DRC check", lambda: rg._get_drc_status(pcb_file))

        board_images = None
        schematic_images = None
        dim_image = None
        errors = []

        # BOMs (pure Python) and gerbers (pcbnew) run on the MAIN thread. Gerbers
        # MUST stay there — the pcbnew plotting API isn't thread-safe — but they
        # share no files or state with the kicad-cli jobs, so we run them WHILE
        # the pool's subprocesses execute to overlap the two.
        def do_bom_and_gerbers():
            try:
                step("Generating BOMs...")
                BOMGenerator(self.project_dir, self.settings).generate_boms()

                # Gerbers derive from the board, so skip them when the PCB only
                # has reorder noise — unless forced or the zip doesn't exist yet.
                if gerbers_on:
                    gerber_zip = os.path.join(self.project_dir, "production", "gerbers.zip")
                    if force or pcb_updated or not os.path.exists(gerber_zip):
                        step("Generating JLCPCB gerbers...")
                        board = pcbnew.GetBoard()
                        if board:
                            JLCPCBExporter(board).generate_zip(self.project_dir, zip_filename="gerbers")
                    else:
                        print("GitHub Command Center: PCB unchanged and gerbers present - skipping gerber generation.")
            except Exception as e:
                errors.append(f"BOM/Gerbers: {e}")

        # --- Run the subprocess generators concurrently ---
        # as_completed() iterates on THIS (main) thread, so the progress callback
        # and result collection stay on the main thread; only the blocking
        # kicad-cli calls run in the worker threads.
        if jobs:
            step(f"Generating {len(jobs)} file set(s) in parallel...")
            with ThreadPoolExecutor(max_workers=min(len(jobs), (os.cpu_count() or 4))) as executor:
                future_map = {executor.submit(fn): (key, label) for key, (label, fn) in jobs.items()}
                # Overlap: main-thread pcbnew gerbers + BOM run while the pool works.
                do_bom_and_gerbers()
                done = 0
                for future in as_completed(future_map):
                    key, label = future_map[future]
                    done += 1
                    try:
                        result = future.result()
                        if key == 'render':
                            board_images = result
                        elif key == 'schematic':
                            schematic_images = result
                        elif key == 'drc':
                            drc_status = result
                        elif key == 'dimensioned':
                            dim_image = result
                    except Exception as e:
                        errors.append(f"{label}: {e}")
                    step(f"{label} finished ({done}/{len(jobs)})")
        else:
            # No kicad-cli work this run; still generate BOMs/gerbers.
            do_bom_and_gerbers()

        # The dimensioned drawing is embedded alongside the board render(s).
        if dim_image:
            board_images = (board_images or []) + [dim_image]

        # --- README last (needs the fresh images + DRC result) ---
        if auto_readme or board_images or schematic_images:
            try:
                step("Updating README...")
                if rg is None:
                    rg = ReadmeGenerator(self.project_dir, self.settings)
                rg.update_readme(self.kicad_version, board_images=board_images,
                                 schematic_images=schematic_images, drc_status=drc_status)
            except Exception as e:
                errors.append(f"README: {e}")

        # Surface any failures together, once, on the main thread.
        if errors:
            wx.MessageBox("Some file generation steps failed:\n\n- " + "\n- ".join(errors),
                          "Generation Warnings", wx.ICON_WARNING)

    def on_generate_files(self, event):
        """Manual-mode trigger for the 'Generate Project Files' button. Runs the
        same generation pipeline as a commit would, with live status feedback,
        but without committing so the user can review the results first.

        force=True: everything the user has enabled is (re)generated, even when
        nothing changed — that's the whole point of clicking the button."""
        self.btn_gen.Disable()
        self.status_lbl.SetLabel("Status: Generating project files...")
        wx.BeginBusyCursor()
        try:
            self._generate_extra_files(progress=self._set_status, force=True)
            wx.MessageBox("Project files generated. Review and commit them when ready.",
                          "Generation Complete")
        finally:
            if wx.IsBusy(): wx.EndBusyCursor()
            self.btn_gen.Enable()
            self.update_git_status()

    def _update_gen_button_visibility(self):
        """Shows the 'Generate Project Files' button only in manual mode; in the
        default automatic mode it stays hidden (generation happens on commit)."""
        manual = self.settings.get('manual_file_generation', False)
        self.btn_gen.Show(manual)
        self.scroll_vbox.Layout()
        self.scroll_panel.FitInside()
        self.main_panel.Layout()

    def on_commit(self, event):
        if not os.path.isdir(os.path.join(self.project_dir, ".git")):
            subprocess.run([self.git_cmd, "-C", self.project_dir, "init"], check=True, creationflags=CREATE_NO_WINDOW)
            
            if not os.path.exists(os.path.join(self.project_dir, ".gitignore")):
                create_gi = wx.MessageBox("Create a default .gitignore file for KiCad?", "Create .gitignore?", wx.YES_NO | wx.ICON_QUESTION)
                if create_gi == wx.YES:
                    self.create_default_gitignore()

        # Extra file generation (3D STEP, renders, schematic, README, BOMs,
        # gerbers) normally runs here as part of committing. When the user opts
        # into manual mode it's driven by the "Generate Project Files" button
        # instead, so skip it and just commit whatever is already on disk.
        if not self.settings.get('manual_file_generation', False):
            wx.BeginBusyCursor()
            try:
                self._generate_extra_files(progress=self._set_status)
            finally:
                if wx.IsBusy(): wx.EndBusyCursor()

        # Filter out KiCad re-serialization (reorder) noise so a cosmetic Ctrl+S
        # isn't offered as a committable change.
        status_dict = self.engine.filter_reorder_noise(
            self.engine.get_git_status(target="HEAD"), target="HEAD")
        changed_files = list(status_dict.keys())

        if any('\\' in f for f in changed_files):
            wx.MessageBox("Escaped filenames detected (e.g. \\303). Let's fix your Git encoding first so the commit doesn't crash.", "Encoding Issue", wx.ICON_WARNING)
            if self._check_and_prompt_git_encoding(force_prompt=True):
                status_dict = self.engine.filter_reorder_noise(
                    self.engine.get_git_status(target="HEAD"), target="HEAD")
                changed_files = list(status_dict.keys())
            else:
                return

        if not changed_files:
            wx.MessageBox("No real changes detected (only KiCad re-serialization noise, if any). Workspace is clean.", "Info")
            return

        include_version = self.settings.get('include_kicad_version', True)

        dlg = CommitDialog(self, changed_files, kicad_version=self.kicad_version,
                           include_version=include_version, file_statuses=status_dict,
                           project_dir=self.project_dir)
        if dlg.ShowModal() == wx.ID_OK:
            msg = dlg.get_message()
            branch = dlg.get_branch()
            selected_files = dlg.get_selected_files()
            
            if not msg:
                wx.MessageBox("Commit message cannot be empty.", "Error")
                dlg.Destroy()
                return
                
            if not selected_files:
                wx.MessageBox("No files selected to commit.", "Error")
                dlg.Destroy()
                return

            try:
                if branch:
                    res_branch = subprocess.run([self.git_cmd, "-C", self.project_dir, "checkout", "-b", branch], 
                                                capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
                    if res_branch.returncode != 0:
                        subprocess.run([self.git_cmd, "-C", self.project_dir, "checkout", branch], creationflags=CREATE_NO_WINDOW)

                subprocess.run([self.git_cmd, "-C", self.project_dir, "reset"], creationflags=CREATE_NO_WINDOW)
                
                cmd_add = [self.git_cmd, "-C", self.project_dir, "add", "--"] + selected_files
                subprocess.run(cmd_add, check=True, creationflags=CREATE_NO_WINDOW)
                
                res_commit = subprocess.run([self.git_cmd, "-C", self.project_dir, "commit", "-m", msg], 
                                            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
                
                if res_commit.returncode != 0:
                    wx.MessageBox(f"Commit failed:\n{res_commit.stderr}", "Git Error", wx.ICON_ERROR)
                else:
                    wx.MessageBox("Committed successfully.", "Success")
                
                self.update_git_status()
                
                new_targets = self.engine.get_git_targets()
                if new_targets:
                    self.cb_targets.SetItems(new_targets)
                        
            except Exception as e:
                wx.MessageBox(f"Git operation failed: {e}", "Error", wx.ICON_ERROR)
        
        dlg.Destroy()

    def on_push(self, event):
        """1. The Trigger: Runs on the main thread, updates UI, starts background work."""
        if not os.path.isdir(os.path.join(self.project_dir, ".git")):
            wx.MessageBox("No Git repo found. Please initialize and link first.", "Error")
            return
            
        # Disable the button so the user doesn't click it twice
        self.btn_push.Disable()
        self.status_lbl.SetLabel("Status: Pushing to Remote (Please wait)...")
        wx.BeginBusyCursor()
        
        # Fire and forget the background thread
        thread = threading.Thread(target=self._push_worker)
        thread.start()

    def _push_worker(self):
        """2. The Worker: Runs in the background, DOES NOT touch wx elements directly."""
        success = False
        message = ""
        
        try:
            res_br = subprocess.run([self.git_cmd, "-C", self.project_dir, "branch", "--show-current"], 
                                    capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            branch = res_br.stdout.strip()
            
            if self.settings.get('silent_pull', False):
                subprocess.run([self.git_cmd, "-C", self.project_dir, "fetch", "origin", branch], creationflags=CREATE_NO_WINDOW)
                res_diff = subprocess.run([self.git_cmd, "-C", self.project_dir, "diff", f"HEAD..origin/{branch}", "--name-only"],
                                          capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
                changed_files = [f.strip() for f in res_diff.stdout.split('\n') if f.strip()]
                
                if changed_files:
                    dangerous_exts = ('.kicad_pcb', '.kicad_sch', '.kicad_pro', '.kicad_prl')
                    has_dangerous = any(f.endswith(dangerous_exts) for f in changed_files)
                    if not has_dangerous:
                        subprocess.run([self.git_cmd, "-C", self.project_dir, "pull", "--rebase", "-X", "ours", "origin", branch], 
                                       creationflags=CREATE_NO_WINDOW)
                    else:
                        print("Silent pull aborted: Remote KiCad changes detected.")

            res = subprocess.run([self.git_cmd, "-C", self.project_dir, "push", "-u", "origin", branch],
                                 capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)

            if res.returncode == 0:
                success = True
                message = f"Successfully pushed branch '{branch}' to Remote."

                # A plain branch push leaves local tags behind, so version tags
                # created via "Create Version Tag" never reached the remote.
                # Publish them too (no-op when there are none).
                res_tags = subprocess.run([self.git_cmd, "-C", self.project_dir, "push", "origin", "--tags"],
                                          capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
                if res_tags.returncode == 0:
                    if "new tag" in (res_tags.stderr or ""):
                        message += "\nVersion tags pushed."
                else:
                    message += f"\n\n(Branch pushed, but pushing tags failed:\n{res_tags.stderr.strip()})"
            else:
                success = False
                message = f"Push Failed:\n{res.stderr.strip()}"
                
        except Exception as e:
            success = False
            message = f"An unexpected error occurred: {e}"
            
        # Safely pass the results back to the main thread
        wx.CallAfter(self._push_complete, success, message)

    def _push_complete(self, success, message):
        """3. The Callback: Runs on the main thread, updates the UI based on results."""
        if wx.IsBusy(): 
            wx.EndBusyCursor()
            
        self.btn_push.Enable()
        
        if success:
            wx.MessageBox(message, "Success")
        else:
            wx.MessageBox(message, "Error", wx.ICON_ERROR)
            
        self.update_git_status()

    def on_open_remote(self, event):
        if not os.path.isdir(os.path.join(self.project_dir, ".git")):
            wx.MessageBox("No Git repo found. Please initialize and link first.", "Error")
            return
            
        try:
            res = subprocess.run([self.git_cmd, "-C", self.project_dir, "remote", "get-url", "origin"], 
                                 capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            if res.returncode == 0:
                url = res.stdout.strip()
                
                # Format common Git SSH URLs to HTTPS so they open correctly in a web browser
                if url.startswith("git@"):
                    # git@gitlab.com:user/repo.git -> https://gitlab.com/user/repo.git
                    url = "https://" + url[4:].replace(":", "/")
                elif url.startswith("ssh://git@"):
                    # ssh://git@bitbucket.org/user/repo.git -> https://bitbucket.org/user/repo.git
                    url = "https://" + url[10:]
                    
                if url.endswith(".git"):
                    url = url[:-4]
                    
                webbrowser.open(url)
            else:
                wx.MessageBox("No remote 'origin' found. Have you linked your project to a remote server?", "Error")
        except Exception as e:
            wx.MessageBox(f"Failed to open remote URL: {e}", "Error")

    def on_close(self, event):
        self._alive = False
        self.Destroy()