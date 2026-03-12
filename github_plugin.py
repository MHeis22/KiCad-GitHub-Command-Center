import wx
import os
import subprocess
import pcbnew
import re
from .diff_engine import DiffEngine
from .diff_window import DiffWindow

# Fix for Windows: prevents the plugin from popping up CMD windows or hanging
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

class CommitDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title="Commit Changes", size=(400, 280))
        
        vbox = wx.BoxSizer(wx.VERTICAL)
        
        # Commit Message
        vbox.Add(wx.StaticText(self, label="Commit Message:"), flag=wx.LEFT|wx.TOP, border=10)
        self.tc_msg = wx.TextCtrl(self, style=wx.TE_MULTILINE)
        vbox.Add(self.tc_msg, proportion=1, flag=wx.EXPAND|wx.LEFT|wx.RIGHT|wx.TOP, border=10)
        
        # Branch Name
        vbox.Add(wx.StaticText(self, label="Target Branch (Leave empty to stay on current):"), flag=wx.LEFT|wx.TOP, border=10)
        self.tc_branch = wx.TextCtrl(self)
        vbox.Add(self.tc_branch, flag=wx.EXPAND|wx.LEFT|wx.RIGHT|wx.BOTTOM, border=10)
        
        # Buttons
        btn_sizer = wx.StdDialogButtonSizer()
        btn_ok = wx.Button(self, wx.ID_OK)
        btn_cancel = wx.Button(self, wx.ID_CANCEL)
        btn_sizer.AddButton(btn_ok)
        btn_sizer.AddButton(btn_cancel)
        btn_sizer.Realize()
        
        vbox.Add(btn_sizer, flag=wx.ALIGN_RIGHT|wx.BOTTOM|wx.RIGHT, border=10)
        self.SetSizer(vbox)

    def get_message(self):
        return self.tc_msg.GetValue().strip()

    def get_branch(self):
        return self.tc_branch.GetValue().strip()

class CommandCenterDialog(wx.Dialog):
    def __init__(self, parent, project_dir):
        super().__init__(parent, title="GitHub Command Center", size=(500, 720))
        self.project_dir = project_dir
        self.git_cmd = "git.exe" if os.name == "nt" else "git"
        
        # Instantiate engine early to fetch targets
        self.engine = DiffEngine(self.project_dir)
        
        panel = wx.Panel(self)
        main_vbox = wx.BoxSizer(wx.VERTICAL)
        
        # --- Header & Status ---
        header = wx.StaticText(panel, label="Git Hardware Control")
        header_font = header.GetFont()
        header_font.SetWeight(wx.FONTWEIGHT_BOLD)
        header_font.SetPointSize(12)
        header.SetFont(header_font)
        main_vbox.Add(header, flag=wx.ALIGN_CENTER | wx.TOP | wx.BOTTOM, border=15)

        self.status_lbl = wx.StaticText(panel, label="Checking status...\n")
        main_vbox.Add(self.status_lbl, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        
        # --- Compare Target Selector ---
        target_sizer = wx.BoxSizer(wx.HORIZONTAL)
        target_sizer.Add(wx.StaticText(panel, label="Compare against:"), flag=wx.ALIGN_CENTER_VERTICAL|wx.RIGHT, border=5)
        
        targets = self.engine.get_git_targets()
        if not targets:
            targets = ["HEAD"]
            
        self.cb_targets = wx.ComboBox(panel, choices=targets, style=wx.CB_READONLY)
        self.cb_targets.SetSelection(0)
        self.cb_targets.Bind(wx.EVT_COMBOBOX, self.on_target_change)
        
        target_sizer.Add(self.cb_targets, proportion=1, flag=wx.EXPAND)
        main_vbox.Add(target_sizer, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        
        # --- DRC / ERC Checkbox ---
        self.cb_drc = wx.CheckBox(panel, label="Run DRC / ERC Checks (May take longer)")
        self.cb_drc.SetToolTip("Executes KiCad's electrical and design rules checkers and compares violations.")
        self.cb_drc.SetValue(False) # Off by default to keep viewing snappy
        main_vbox.Add(self.cb_drc, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=15)

        # --- Action Buttons ---
        btn_diff = wx.Button(panel, label="View Local Changes (Visual Diff)", size=(-1, 40))
        btn_commit = wx.Button(panel, label="Save Snapshot (Quick Commit)", size=(-1, 40))
        btn_switch = wx.Button(panel, label="Switch Working Branch", size=(-1, 40))
        
        # Stash Buttons Sizer
        stash_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_stash = wx.Button(panel, label="Stash Local Changes", size=(-1, 40))
        btn_pop = wx.Button(panel, label="Pop Last Stash", size=(-1, 40))
        stash_sizer.Add(btn_stash, proportion=1, flag=wx.RIGHT, border=5)
        stash_sizer.Add(btn_pop, proportion=1, flag=wx.LEFT, border=5)
        
        btn_push = wx.Button(panel, label="Push Changes to GitHub", size=(-1, 40))
        
        btn_sync = wx.Button(panel, label="Download from Server (Force Sync)", size=(-1, 40))
        btn_sync.SetBackgroundColour(wx.Colour(230, 240, 255)) 
        
        btn_diff.Bind(wx.EVT_BUTTON, self.on_diff)
        btn_commit.Bind(wx.EVT_BUTTON, self.on_commit)
        btn_switch.Bind(wx.EVT_BUTTON, self.on_switch_branch)
        btn_stash.Bind(wx.EVT_BUTTON, self.on_stash)
        btn_pop.Bind(wx.EVT_BUTTON, self.on_pop)
        btn_push.Bind(wx.EVT_BUTTON, self.on_push)
        btn_sync.Bind(wx.EVT_BUTTON, self.on_force_sync)
        
        main_vbox.Add(btn_diff, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        main_vbox.Add(btn_commit, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        main_vbox.Add(btn_switch, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        main_vbox.Add(stash_sizer, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        main_vbox.Add(btn_push, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        main_vbox.Add(btn_sync, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, border=10)

        # --- Help Text ---
        help_box = wx.StaticBox(panel, label="Sync Instructions")
        help_sizer = wx.StaticBoxSizer(help_box, wx.VERTICAL)
        help_text = (
            "TO SEE CHANGES AFTER SYNC/SWITCH/POP:\n"
            "1. Run 'Download', 'Switch Branch', or 'Pop Stash'.\n"
            "2. Close your PCB/Schematic editor.\n"
            "3. If KiCad asks to save, select 'DISCARD CHANGES'.\n"
            "4. Re-open the file to see the loaded version."
        )
        st_help = wx.StaticText(panel, label=help_text)
        st_help.SetForegroundColour(wx.Colour(100, 100, 100))
        help_sizer.Add(st_help, flag=wx.ALL, border=5)
        main_vbox.Add(help_sizer, flag=wx.EXPAND | wx.ALL, border=15)
        
        btn_close = wx.Button(panel, label="Close")
        btn_close.Bind(wx.EVT_BUTTON, self.on_close)
        main_vbox.Add(btn_close, flag=wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, border=15)
        
        panel.SetSizer(main_vbox)
        self.update_git_status()

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
                        wx.MessageBox(f"Checkout Failed. Do you have uncommitted changes blocking the switch?\n\n{res_switch.stderr}", "Git Error", wx.ICON_ERROR)
                    else:
                        wx.MessageBox(f"Switched to branch '{selected}'.\n\nRemember to 'Discard Changes' if KiCad prompts you when reopening files.", "Success")
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
        except Exception as e:
            wx.MessageBox(f"Error: {e}", "Error", wx.ICON_ERROR)
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
                wx.MessageBox(f"Stash popped successfully:\n{res.stdout.strip()}\n\nRemember to 'Discard Changes' if KiCad prompts you.", "Success")
                self.update_git_status()
            else:
                error_msg = res.stderr.strip() or res.stdout.strip()
                wx.MessageBox(f"Stash pop failed:\n{error_msg}", "Git Error", wx.ICON_ERROR)
        except Exception as e:
            wx.MessageBox(f"Error: {e}", "Error", wx.ICON_ERROR)
        finally:
            if wx.IsBusy(): wx.EndBusyCursor()

    def on_diff(self, event):
        wx.BeginBusyCursor()
        try:
            selected_target = self.cb_targets.GetStringSelection()
            run_checks = self.cb_drc.GetValue()
            
            diffs, summary = self.engine.render_all_diffs(show_unchanged=False, compare_target=selected_target, run_drc_erc=run_checks)
            if not diffs:
                wx.MessageBox(f"No local changes detected against {selected_target}.", "Info")
            else:
                win = DiffWindow(diffs, summary, target_name=selected_target)
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
            msg = ("SUCCESS!\n\n"
                   "Local files updated to match " + remote_ref + ".\n\n"
                   "IMPORTANT: Close your PCB/Schematic editor now. "
                   "When prompted, select 'DISCARD CHANGES' to load the new version.")
            wx.MessageBox(msg, "Sync Complete")
            self.update_git_status()
        except subprocess.CalledProcessError as e:
            wx.MessageBox(f"Sync Failed:\n{e.stderr}", "Git Error")
        except Exception as e:
            wx.MessageBox(f"Error: {e}", "Error")
        finally:
            if wx.IsBusy(): wx.EndBusyCursor()

    def on_commit(self, event):
        dlg = CommitDialog(self)
        if dlg.ShowModal() == wx.ID_OK:
            msg = dlg.get_message()
            branch = dlg.get_branch()
            
            if not msg:
                wx.MessageBox("Commit message cannot be empty.", "Error")
                dlg.Destroy()
                return

            try:
                if not os.path.isdir(os.path.join(self.project_dir, ".git")):
                    subprocess.run([self.git_cmd, "-C", self.project_dir, "init"], check=True, creationflags=CREATE_NO_WINDOW)
                
                if branch:
                    if re.search(r'\s|~|\^|:|\?|\*|\[|\\|\.\.|@\{|^/|/$|\.$', branch):
                        wx.MessageBox(f"Invalid branch name format: '{branch}'", "Git Error", wx.ICON_ERROR)
                        dlg.Destroy()
                        return

                    res_check = subprocess.run([self.git_cmd, "-C", self.project_dir, "branch", "--format=%(refname:short)"], 
                                               capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
                    existing_branches = [b.strip() for b in res_check.stdout.split('\n') if b.strip()]

                    if branch in existing_branches:
                        res_branch = subprocess.run([self.git_cmd, "-C", self.project_dir, "checkout", branch], 
                                                    capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
                    else:
                        res_branch = subprocess.run([self.git_cmd, "-C", self.project_dir, "checkout", "-b", branch], 
                                                    capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
                    
                    if res_branch.returncode != 0:
                        wx.MessageBox(f"Failed to switch to branch:\n{res_branch.stderr}", "Git Error", wx.ICON_ERROR)
                        dlg.Destroy()
                        return

                subprocess.run([self.git_cmd, "-C", self.project_dir, "add", "."], check=True, creationflags=CREATE_NO_WINDOW)
                res_commit = subprocess.run([self.git_cmd, "-C", self.project_dir, "commit", "-m", msg], 
                                            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
                
                if res_commit.returncode != 0:
                    wx.MessageBox(f"Commit failed:\n{res_commit.stderr or res_commit.stdout}", "Git Error", wx.ICON_ERROR)
                else:
                    success_msg = f"Committed successfully on branch '{branch}'." if branch else "Committed successfully."
                    wx.MessageBox(success_msg, "Success")
                
                self.update_git_status()
                
                current_sel = self.cb_targets.GetStringSelection()
                new_targets = self.engine.get_git_targets()
                if new_targets:
                    self.cb_targets.SetItems(new_targets)
                    if current_sel in new_targets:
                        self.cb_targets.SetStringSelection(current_sel)
                    else:
                        self.cb_targets.SetSelection(0)
                        
            except Exception as e:
                wx.MessageBox(f"Git operation failed: {e}", "Error", wx.ICON_ERROR)
        
        dlg.Destroy()

    def on_push(self, event):
        wx.BeginBusyCursor()
        try:
            res_br = subprocess.run([self.git_cmd, "-C", self.project_dir, "branch", "--show-current"], 
                                    capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            branch = res_br.stdout.strip()
            if not branch:
                wx.MessageBox("Could not detect current branch. Are you in a detached HEAD state?", "Error")
                return

            res_rem = subprocess.run([self.git_cmd, "-C", self.project_dir, "remote"], 
                                     capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            if "origin" not in res_rem.stdout:
                wx.MessageBox("Remote 'origin' not found. Please add a remote using 'git remote add origin <url>'", "Error")
                return

            res = subprocess.run([self.git_cmd, "-C", self.project_dir, "push", "-u", "origin", branch], 
                                 capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            
            if res.returncode == 0:
                wx.MessageBox(f"Successfully pushed branch '{branch}' to GitHub.", "Success")
            else:
                error_msg = res.stderr.strip() or res.stdout.strip() or "Unknown Git error."
                wx.MessageBox(f"Push Failed:\n{error_msg}", "Error")
                
        except Exception as e:
            wx.MessageBox(f"An error occurred during push: {e}", "Error")
        finally:
            if wx.IsBusy(): wx.EndBusyCursor()

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
        board = pcbnew.GetBoard()
        path = board.GetFileName()
        if not path:
            wx.MessageBox("Save the board first.")
            return
        dlg = CommandCenterDialog(None, os.path.dirname(path))
        dlg.ShowModal()