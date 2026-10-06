import wx
import os
import pcbnew
from .utils import is_git_installed
from .command_center import CommandCenterDialog

class GithubActionPlugin(pcbnew.ActionPlugin):
    _open_dialog = None

    def defaults(self):
        self.name = "GitHub Command Center"
        self.category = "Tool"
        self.description = "Git version control, visual diffs and manufacturing outputs for this project"
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
            
        # Modeless, so KiCad stays usable while it's open (e.g. to close the
        # Schematic Editor when asked to). One window at a time: clicking the
        # button again brings the open one to the front.
        project_dir = os.path.dirname(path)
        open_dlg = GithubActionPlugin._open_dialog
        try:
            if open_dlg and open_dlg._alive:
                if os.path.normcase(open_dlg.project_dir) == os.path.normcase(project_dir):
                    open_dlg.Raise()
                    return
                open_dlg.Close()
        except RuntimeError:
            pass  # the previous window is already destroyed
        # Parented to the PCB editor so it stays in front of it (an ownerless
        # window can end up behind the editor on macOS).
        parent = wx.FindWindowByName("PcbFrame")
        dlg = CommandCenterDialog(parent, project_dir)
        GithubActionPlugin._open_dialog = dlg
        dlg.Show()