import wx
import os
import subprocess
import pcbnew
import re
import webbrowser
import json
from .diff_engine import DiffEngine
from .diff_window import DiffWindow

# Fix for Windows: prevents the plugin from popping up CMD windows or hanging
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

def get_settings_path():
    """Returns the path for the global plugin settings file."""
    return os.path.expanduser('~/.kicad_git_diff_settings.json')

def load_settings():
    """Loads settings from the user's home directory."""
    try:
        with open(get_settings_path(), 'r') as f:
            return json.load(f)
    except Exception:
        # Default settings if file doesn't exist
        return {'include_kicad_version': True}

def save_settings(settings):
    """Saves settings to the user's home directory."""
    try:
        with open(get_settings_path(), 'w') as f:
            json.dump(settings, f)
    except Exception as e:
        print(f"Error saving settings: {e}")

def is_git_installed():
    """Checks if git is available on the system PATH."""
    try:
        git_cmd = "git.exe" if os.name == "nt" else "git"
        subprocess.run([git_cmd, "--version"], capture_output=True, check=True, creationflags=CREATE_NO_WINDOW)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False

class SettingsDialog(wx.Dialog):
    def __init__(self, parent, current_settings):
        super().__init__(parent, title="Settings", size=(450, 250)) # Slightly taller for the new option
        self.settings = current_settings.copy()
        
        vbox = wx.BoxSizer(wx.VERTICAL)
        
        # KiCad Version toggle
        self.cb_kicad_version = wx.CheckBox(self, label="Automatically append KiCad Version to commit messages")
        self.cb_kicad_version.SetValue(self.settings.get('include_kicad_version', True))
        vbox.Add(self.cb_kicad_version, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.TOP, border=15)
        
        # Auto-Readme toggle
        self.cb_readme = wx.CheckBox(self, label="Automatically update README.md with hardware summary")
        self.cb_readme.SetValue(self.settings.get('auto_readme', False))
        self.cb_readme.SetToolTip("Generates a sticky footer in your README with BOM and board stats.")
        vbox.Add(self.cb_readme, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=15)

        # Silent Pull toggle
        self.cb_silent_pull = wx.CheckBox(self, label="Auto-Pull text files before pushing (Silent Pull)")
        self.cb_silent_pull.SetValue(self.settings.get('silent_pull', False))
        self.cb_silent_pull.SetToolTip("Automatically pulls remote changes to safe text files (README.md, .csv) before pushing.\nAborts pulling if remote KiCad schematic or PCB changes are detected.")
        vbox.Add(self.cb_silent_pull, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=15)
        
        btn_sizer = wx.StdDialogButtonSizer()
        btn_ok = wx.Button(self, wx.ID_OK)
        btn_cancel = wx.Button(self, wx.ID_CANCEL)
        btn_sizer.AddButton(btn_ok)
        btn_sizer.AddButton(btn_cancel)
        btn_sizer.Realize()
        
        vbox.Add(btn_sizer, flag=wx.ALIGN_RIGHT|wx.BOTTOM|wx.RIGHT, border=10)
        self.SetSizer(vbox)
        self.CenterOnParent()
        
    def get_settings(self):
        self.settings['include_kicad_version'] = self.cb_kicad_version.IsChecked()
        self.settings['auto_readme'] = self.cb_readme.IsChecked()
        self.settings['silent_pull'] = self.cb_silent_pull.IsChecked()
        return self.settings

class CommitDialog(wx.Dialog):
    def __init__(self, parent, changed_files, kicad_version="Unknown KiCad Version", include_version=True):
        super().__init__(parent, title="Commit Changes", size=(450, 480))
        self.changed_files = changed_files
        self.kicad_version = kicad_version
        self.include_version = include_version
        
        vbox = wx.BoxSizer(wx.VERTICAL)
        
        # --- File Selection Menu ---
        vbox.Add(wx.StaticText(self, label="Files to Commit (Uncheck to exclude):"), flag=wx.LEFT|wx.TOP, border=10)
        self.clb_files = wx.CheckListBox(self, choices=self.changed_files)
        # Default all turned on
        for i in range(len(self.changed_files)):
            self.clb_files.Check(i, True)
        vbox.Add(self.clb_files, proportion=1, flag=wx.EXPAND|wx.LEFT|wx.RIGHT|wx.TOP, border=10)
        
        # --- Commit Details ---
        vbox.Add(wx.StaticText(self, label="Commit Message:"), flag=wx.LEFT|wx.TOP, border=10)
        self.tc_msg = wx.TextCtrl(self, style=wx.TE_MULTILINE)
        vbox.Add(self.tc_msg, proportion=1, flag=wx.EXPAND|wx.LEFT|wx.RIGHT|wx.TOP, border=10)
        
        vbox.Add(wx.StaticText(self, label="Target Branch (Leave empty to stay on current):"), flag=wx.LEFT|wx.TOP, border=10)
        self.tc_branch = wx.TextCtrl(self)
        vbox.Add(self.tc_branch, flag=wx.EXPAND|wx.ALL, border=10)

        btn_sizer = wx.StdDialogButtonSizer()
        btn_ok = wx.Button(self, wx.ID_OK)
        btn_cancel = wx.Button(self, wx.ID_CANCEL)
        btn_sizer.AddButton(btn_ok)
        btn_sizer.AddButton(btn_cancel)
        btn_sizer.Realize()
        
        vbox.Add(btn_sizer, flag=wx.ALIGN_RIGHT|wx.BOTTOM|wx.RIGHT, border=10)
        self.SetSizer(vbox)

    def get_selected_files(self):
        return [self.changed_files[i] for i in range(self.clb_files.GetCount()) if self.clb_files.IsChecked(i)]

    def get_message(self):
        msg = self.tc_msg.GetValue().strip()
        # Append KiCad version if global setting is enabled
        if self.include_version and self.kicad_version and self.kicad_version != "Unknown KiCad Version":
            if msg:
                msg += f"\n\n[KiCad Version: {self.kicad_version}]"
            else:
                msg = f"[KiCad Version: {self.kicad_version}]"
        return msg

    def get_branch(self):
        return self.tc_branch.GetValue().strip()

class CommandCenterDialog(wx.Dialog):
    def __init__(self, parent, project_dir):
        super().__init__(parent, title="GitHub Command Center", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.project_dir = project_dir
        self.git_cmd = "git.exe" if os.name == "nt" else "git"
        self.engine = DiffEngine(self.project_dir)
        self.kicad_version = self.engine.get_kicad_version()
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
        box_status = wx.StaticBox(self.scroll_panel, label="Status & Comparison")
        sizer_status = wx.StaticBoxSizer(box_status, wx.VERTICAL)
        
        self.status_lbl = wx.StaticText(self.scroll_panel, label="Checking status...\n")
        sizer_status.Add(self.status_lbl, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)
        
        target_sizer = wx.BoxSizer(wx.HORIZONTAL)
        target_sizer.Add(wx.StaticText(self.scroll_panel, label="Compare against:"), flag=wx.ALIGN_CENTER_VERTICAL|wx.RIGHT, border=5)
        
        targets = self.engine.get_git_targets()
        if not targets:
            targets = ["HEAD"]
            
        self.cb_targets = wx.ComboBox(self.scroll_panel, choices=targets, style=wx.CB_READONLY)
        self.cb_targets.SetSelection(0)
        self.cb_targets.Bind(wx.EVT_COMBOBOX, self.on_target_change)
        target_sizer.Add(self.cb_targets, proportion=1, flag=wx.EXPAND)
        
        sizer_status.Add(target_sizer, flag=wx.EXPAND | wx.ALL, border=5)
        self.scroll_vbox.Add(sizer_status, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        # ==========================================
        # GROUP 2: Review & Validation
        # ==========================================
        box_review = wx.StaticBox(self.scroll_panel, label="Review & Validation")
        sizer_review = wx.StaticBoxSizer(box_review, wx.VERTICAL)
        
        btn_diff = wx.Button(self.scroll_panel, label="View Local Changes (Visual Diff)", size=(-1, 40))
        btn_diff.SetBackgroundColour(wx.Colour(220, 240, 255)) # Light Blue (Primary)
        btn_diff.Bind(wx.EVT_BUTTON, self.on_diff)
        
        btn_diff_all = wx.Button(self.scroll_panel, label="View All Files (Including Unchanged)", size=(-1, 40))
        btn_diff_all.Bind(wx.EVT_BUTTON, self.on_diff_all)
        
        self.cb_drc = wx.CheckBox(self.scroll_panel, label="Run DRC Checks (Shows DRC violations as diffs)")
        self.cb_drc.SetToolTip("Executes KiCad's design rules checker and compares violations.")
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
        
        btn_commit = wx.Button(self.scroll_panel, label="Save Snapshot (Quick Commit)", size=(-1, 40))
        btn_commit.SetBackgroundColour(wx.Colour(220, 255, 220)) # Light Green (Safe)
        btn_commit.Bind(wx.EVT_BUTTON, self.on_commit)
        
        btn_switch = wx.Button(self.scroll_panel, label="Switch Working Branch", size=(-1, 40))
        btn_switch.Bind(wx.EVT_BUTTON, self.on_switch_branch)
        
        stash_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_stash = wx.Button(self.scroll_panel, label="Stash Local Changes", size=(-1, 40))
        btn_pop = wx.Button(self.scroll_panel, label="Pop Last Stash", size=(-1, 40))
        btn_stash.Bind(wx.EVT_BUTTON, self.on_stash)
        btn_pop.Bind(wx.EVT_BUTTON, self.on_pop)
        stash_sizer.Add(btn_stash, proportion=1, flag=wx.RIGHT, border=2)
        stash_sizer.Add(btn_pop, proportion=1, flag=wx.LEFT, border=2)

        sizer_local.Add(btn_commit, flag=wx.EXPAND | wx.ALL, border=5)
        sizer_local.Add(btn_switch, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)
        sizer_local.Add(stash_sizer, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)
        self.scroll_vbox.Add(sizer_local, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        # ==========================================
        # GROUP 4: Remote / Sync
        # ==========================================
        box_remote = wx.StaticBox(self.scroll_panel, label="Remote & Sync")
        sizer_remote = wx.StaticBoxSizer(box_remote, wx.VERTICAL)

        btn_push = wx.Button(self.scroll_panel, label="Push Changes to GitHub", size=(-1, 40))
        btn_push.SetBackgroundColour(wx.Colour(255, 240, 200)) # Light Orange (Network)
        btn_push.Bind(wx.EVT_BUTTON, self.on_push)
        
        btn_github = wx.Button(self.scroll_panel, label="Open GitHub Page", size=(-1, 40))
        btn_github.Bind(wx.EVT_BUTTON, self.on_open_github)
        
        btn_sync = wx.Button(self.scroll_panel, label="Download from Server (Force Sync)", size=(-1, 40))
        btn_sync.SetBackgroundColour(wx.Colour(255, 200, 200)) # Light Red (Destructive local)
        btn_sync.Bind(wx.EVT_BUTTON, self.on_force_sync)

        sizer_remote.Add(btn_push, flag=wx.EXPAND | wx.ALL, border=5)
        sizer_remote.Add(btn_github, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)
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
        st_help.SetForegroundColour(wx.Colour(100, 100, 100))
        help_sizer.Add(st_help, flag=wx.ALL, border=5)
        self.scroll_vbox.Add(help_sizer, flag=wx.EXPAND | wx.ALL, border=10)
        
        self.scroll_panel.SetSizer(self.scroll_vbox)
        
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

    def _check_and_prompt_git_encoding(self, force_prompt=False):
        """
        Git quotes paths with non-ASCII chars by default causing octal escapes (e.g., \303\266).
        This breaks subprocess file operations. Prompt to apply `git config core.quotePath false`.
        """
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
                
                # If it's not explicitly false, Git encodes files with 'ö', 'ä', etc.
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
        """Builds the Setup UI section if git is not initialized."""
        setup_box = wx.StaticBox(self.scroll_panel, label="New Project Setup")
        self.setup_section_container = wx.StaticBoxSizer(setup_box, wx.VERTICAL)
        
        btn_setup = wx.Button(self.scroll_panel, label="Initialize & Link to GitHub")
        btn_setup.SetBackgroundColour(wx.Colour(200, 255, 200))
        btn_setup.Bind(wx.EVT_BUTTON, self.on_setup_repo)
        
        self.setup_section_container.Add(btn_setup, flag=wx.EXPAND | wx.ALL, border=5)

    def on_settings(self, event):
        dlg = SettingsDialog(self, self.settings)
        if dlg.ShowModal() == wx.ID_OK:
            self.settings = dlg.get_settings()
            save_settings(self.settings)
        dlg.Destroy()

    def on_target_change(self, event):
        self.update_git_status()

    def update_git_status(self):
        if not os.path.isdir(os.path.join(self.project_dir, ".git")):
            self.status_lbl.SetLabel("Status: Not a Git repository.")
            return
        try:
            res_curr = subprocess.run([self.git_cmd, "-C", self.project_dir, "branch", "--show-current"], 
                                      capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            curr_branch = res_curr.stdout.strip() or "Detached HEAD"

            target_raw = self.cb_targets.GetStringSelection()
            actual_target = target_raw.split(' ')[0] if ' ' in target_raw else target_raw
            
            status_dict = self.engine.get_git_status(target=actual_target)
            changes = len(status_dict)
            
            status_text = f"Working Branch: '{curr_branch}'\n"
            if changes > 0:
                status_text += f"Status: {changes} changes relative to {actual_target}."
            else:
                status_text += f"Status: Workspace identical to {actual_target}."
                
            self.status_lbl.SetLabel(status_text)
        except:
            self.status_lbl.SetLabel("Status: Git Error.")

    def create_default_gitignore(self):
        gitignore_path = os.path.join(self.project_dir, ".gitignore")
        if not os.path.exists(gitignore_path):
            content = (
                "# KiCad modern backups (KiCad 7+)\n"
                "*-backups/\n\n"
                "# KiCad legacy backups and autosaves\n"
                "*.bak\n*.kicad_pcb-bak\n*.kicad_sch-bak\n*.kicad_pro-bak\n"
                "*-save.pro\n*-save.kicad_pcb\n*-save.kicad_sch\n"
                "*_autosave-*\n_autosave-*\n\n"
                "# KiCad caches\nfp-info-cache\n\n"
                "# Generated files\n*.bck\n*.kicad_pcb-shl\npython_environment/\n\n"
                "# OS files\n.DS_Store\nThumbs.db\n"
            )
            with open(gitignore_path, "w") as f:
                f.write(content)

    def on_setup_repo(self, event):
        dlg = wx.TextEntryDialog(self, 
            "Paste your GitHub Repository URL:", 
            "Link to GitHub")
        
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

                wx.MessageBox("Project linked to GitHub successfully!", "Success")
                
                # --- UI Refresh Logic ---
                if self.setup_section_container:
                    # Destroy widgets to perfectly reclaim layout space and prevent overlap bugs
                    for item in self.setup_section_container.GetChildren():
                        if item.IsWindow():
                            item.GetWindow().Destroy()
                    box = self.setup_section_container.GetStaticBox()
                    if box:
                        box.Destroy()
                    
                    self.scroll_vbox.Detach(self.setup_section_container)
                    self.setup_section_container = None
                    
                    # Force thorough recursive layout refreshes
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
                
            # Verify and fix encoding right after repo initialization 
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

    def on_diff(self, event):
        wx.BeginBusyCursor()
        try:
            selected_target = self.cb_targets.GetStringSelection()
            run_checks = self.cb_drc.GetValue()
            
            diffs, summary = self.engine.render_all_diffs(show_unchanged=False, compare_target=selected_target, run_drc=run_checks)
            if not diffs:
                wx.MessageBox(f"No local changes detected against {selected_target}.", "Info")
            else:
                win = DiffWindow(diffs, summary, target_name=selected_target, kicad_version=self.kicad_version)
                win.Show()
        finally:
            if wx.IsBusy(): wx.EndBusyCursor()

    def on_diff_all(self, event):
        wx.BeginBusyCursor()
        try:
            selected_target = self.cb_targets.GetStringSelection()
            run_checks = self.cb_drc.GetValue()
            
            diffs, summary = self.engine.render_all_diffs(show_unchanged=True, compare_target=selected_target, run_drc=run_checks)
            if not diffs:
                wx.MessageBox(f"No schematic or PCB files found to render.", "Info")
            else:
                win = DiffWindow(diffs, summary, target_name=selected_target, kicad_version=self.kicad_version)
                win.Show()
        finally:
            if wx.IsBusy(): wx.EndBusyCursor()

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
                self.perform_atomic_overwrite(target)
        dlg.Destroy()

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

    def on_commit(self, event):
        if not os.path.isdir(os.path.join(self.project_dir, ".git")):
            subprocess.run([self.git_cmd, "-C", self.project_dir, "init"], check=True, creationflags=CREATE_NO_WINDOW)
            
            if not os.path.exists(os.path.join(self.project_dir, ".gitignore")):
                create_gi = wx.MessageBox("Create a default .gitignore file for KiCad?", "Create .gitignore?", wx.YES_NO | wx.ICON_QUESTION)
                if create_gi == wx.YES:
                    self.create_default_gitignore()

        if self.settings.get('auto_readme', False):
            try:
                from .readme_generator import ReadmeGenerator
                rg = ReadmeGenerator(self.project_dir, self.engine)
                rg.update_readme(self.kicad_version)
            except Exception as e:
                print(f"Failed to autogenerate README: {e}")
        
        status_dict = self.engine.get_git_status(target="HEAD")
        changed_files = list(status_dict.keys())
        
        # Guard against unhandled octal escapes in filenames before throwing it to the Commit Dialog
        if any('\\' in f for f in changed_files):
            wx.MessageBox("Escaped filenames detected (e.g. \\303). Let's fix your Git encoding first so the commit doesn't crash.", "Encoding Issue", wx.ICON_WARNING)
            if self._check_and_prompt_git_encoding(force_prompt=True):
                # Fetch clean unquoted filenames
                status_dict = self.engine.get_git_status(target="HEAD")
                changed_files = list(status_dict.keys())
            else:
                return # Abort if they didn't fix it

        if not changed_files:
            wx.MessageBox("No changes detected. Workspace is clean.", "Info")
            return

        # Fetch setting for auto-appending KiCad version
        include_version = self.settings.get('include_kicad_version', True)

        dlg = CommitDialog(self, changed_files, kicad_version=self.kicad_version, include_version=include_version)
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
        wx.BeginBusyCursor()
        try:
            res_br = subprocess.run([self.git_cmd, "-C", self.project_dir, "branch", "--show-current"], 
                                    capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            branch = res_br.stdout.strip()
            
            # --- SILENT PULL LOGIC ---
            if self.settings.get('silent_pull', False):
                # Fetch remote updates without merging yet
                subprocess.run([self.git_cmd, "-C", self.project_dir, "fetch", "origin", branch], creationflags=CREATE_NO_WINDOW)
                
                # Check what files are ahead on the remote server
                res_diff = subprocess.run([self.git_cmd, "-C", self.project_dir, "diff", f"HEAD..origin/{branch}", "--name-only"],
                                          capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
                
                changed_files = [f.strip() for f in res_diff.stdout.split('\n') if f.strip()]
                
                if changed_files:
                    # Check if any dangerous KiCad files are in the remote changes
                    dangerous_exts = ('.kicad_pcb', '.kicad_sch', '.kicad_pro', '.kicad_prl')
                    has_dangerous = any(f.endswith(dangerous_exts) for f in changed_files)
                    
                    if not has_dangerous:
                        # It's safe! Pull the text files using rebase (to cleanly stack local commits on top)
                        subprocess.run([self.git_cmd, "-C", self.project_dir, "pull", "--rebase", "origin", branch], creationflags=CREATE_NO_WINDOW)
                    else:
                        print("Silent pull aborted: Remote KiCad changes detected. Letting push fail normally.")
            # -------------------------

            res = subprocess.run([self.git_cmd, "-C", self.project_dir, "push", "-u", "origin", branch], 
                                 capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            
            if res.returncode == 0:
                wx.MessageBox(f"Successfully pushed branch '{branch}' to GitHub.", "Success")
            else:
                wx.MessageBox(f"Push Failed:\n{res.stderr.strip()}", "Error")
        finally:
            if wx.IsBusy(): wx.EndBusyCursor()

    def on_open_github(self, event):
        if not os.path.isdir(os.path.join(self.project_dir, ".git")):
            wx.MessageBox("No Git repo found. Please initialize and link first.", "Error")
            return
            
        try:
            res = subprocess.run([self.git_cmd, "-C", self.project_dir, "remote", "get-url", "origin"], 
                                 capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            if res.returncode == 0:
                url = res.stdout.strip()
                if url.startswith("git@github.com:"):
                    url = url.replace("git@github.com:", "https://github.com/")
                if url.endswith(".git"):
                    url = url[:-4]
                    
                webbrowser.open(url)
            else:
                wx.MessageBox("No remote 'origin' found. Have you linked your project to GitHub?", "Error")
        except Exception as e:
            wx.MessageBox(f"Failed to open GitHub: {e}", "Error")

    def on_close(self, event):
        self.Destroy()

class GithubActionPlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "GitHub Command Center"
        self.category = "Tool"
        self.description = "Visual Diff & Force Sync"
        self.show_toolbar_button = True 
        self.icon_file_name = os.path.join(os.path.dirname(__file__), 'icon.png')

    def Run(self):
        if not is_git_installed():
            wx.MessageBox("Git is not installed or not in PATH.", "Git Dependency Missing", wx.ICON_ERROR)
            return

        board = pcbnew.GetBoard()
        path = board.GetFileName()
        if not path:
            wx.MessageBox("Save the board first.")
            return
            
        dlg = CommandCenterDialog(None, os.path.dirname(path))
        dlg.ShowModal()