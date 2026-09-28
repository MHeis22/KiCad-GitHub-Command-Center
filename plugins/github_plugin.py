import wx
import os
import pcbnew
from .utils import is_git_installed
from .command_center import CommandCenterDialog

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
            
        # Parent to the PCB editor so the modal dialog stays in front of it
        # (an ownerless modal can end up behind the editor on macOS, which
        # looks exactly like KiCad being frozen).
        parent = wx.FindWindowByName("PcbFrame")
        dlg = CommandCenterDialog(parent, os.path.dirname(path))
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()