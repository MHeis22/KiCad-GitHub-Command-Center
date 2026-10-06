import os
import wx
import wx.lib.scrolledpanel
from .merge_resolver import MINE, THEIRS, BOTH

_LABELS = {BOTH: "Combine both", MINE: "Keep mine", THEIRS: "Take the server's"}
_BADGE = {BOTH: "COMBINED", MINE: "MINE", THEIRS: "SERVER'S", None: "CHOOSE"}
_KIND_TEXT = {'design': "KiCad design file", 'readme': "README text", 'other': "file"}

# One color per side, used for the badges, buttons and legend: mine blue,
# the server's orange, combined green. (light theme, dark theme)
_COLOURS = {MINE: ((205, 225, 255), (40, 80, 150)),
            THEIRS: ((255, 215, 170), (150, 85, 20)),
            BOTH: ((200, 240, 200), (35, 115, 55)),
            None: ((235, 235, 235), (80, 80, 80))}


def _is_dark():
    try:
        return wx.SystemSettings.GetAppearance().IsDark()
    except AttributeError:  # wxPython < 4.1
        return wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW).GetLuminance() < 0.5


def _colour(side):
    light, dark = _COLOURS[side]
    return wx.Colour(*(dark if _is_dark() else light))


def _text_colour():
    return wx.WHITE if _is_dark() else wx.BLACK


def _badge(parent, side, width=90):
    b = wx.StaticText(parent, label=_BADGE[side], size=(width, -1),
                      style=wx.ALIGN_CENTRE_HORIZONTAL | wx.ST_NO_AUTORESIZE)
    _paint_badge(b, side)
    return b


def _paint_badge(badge, side):
    badge.SetLabel(_BADGE[side])
    badge.SetBackgroundColour(_colour(side))
    badge.SetForegroundColour(_text_colour())
    font = badge.GetFont()
    font.SetWeight(wx.FONTWEIGHT_BOLD)
    badge.SetFont(font)
    badge.Refresh()


class ConflictDialog(wx.Dialog):
    """Asks, per file that both sides changed, which version to keep.

    Only files that need a decision get a row: KiCad design files changed on
    both sides (always, since a clean text merge can still be a wrong design)
    and other files git can't merge. Generated outputs and view settings are
    listed as handled automatically."""

    def __init__(self, parent, items, upstream, ahead, behind, previous=None):
        super().__init__(parent, title="Merge Server Changes", size=(640, 560),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.rows = []
        self.badges = []
        previous = previous or {}
        v = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(self, label=(
            f"You have {ahead} commit(s) the server doesn't have, and {upstream} has {behind} you don't. "
            "Both sides changed the files below. Choose which version to keep for each."))
        intro.Wrap(600)
        v.Add(intro, flag=wx.ALL, border=12)

        legend = wx.BoxSizer(wx.HORIZONTAL)
        for side, text in ((MINE, "your version"), (THEIRS, "the server's version"), (BOTH, "both combined")):
            legend.Add(_badge(self, side), flag=wx.ALIGN_CENTER_VERTICAL)
            legend.Add(wx.StaticText(self, label=" " + text), flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=14)
        v.Add(legend, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=12)

        panel = wx.lib.scrolledpanel.ScrolledPanel(self)
        grid = wx.FlexGridSizer(cols=3, vgap=6, hgap=12)
        grid.AddGrowableCol(0, 1)
        for item in (i for i in items if i.needs_choice):
            name = wx.StaticText(panel, label=item.path)
            what = _KIND_TEXT.get(item.kind, "file") + (
                ": both sides changed the same lines" if item.conflict else ": changed on both sides, no overlap")
            name.SetToolTip(what)
            labels = [_LABELS[o] for o in item.options]
            choice = wx.Choice(panel, choices=labels)
            if previous.get(item.path) in item.options:
                choice.SetSelection(item.options.index(previous[item.path]))
            elif BOTH in item.options:
                choice.SetSelection(0)  # no overlap: combining keeps both sides' work
            choice.Bind(wx.EVT_CHOICE, self._update_ok)
            col = wx.BoxSizer(wx.VERTICAL)
            col.Add(name)
            hint = wx.StaticText(panel, label=what)
            hint.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
            col.Add(hint)
            badge = _badge(panel, None)
            grid.Add(col, flag=wx.EXPAND | wx.ALIGN_CENTER_VERTICAL)
            grid.Add(choice, flag=wx.ALIGN_CENTER_VERTICAL)
            grid.Add(badge, flag=wx.ALIGN_CENTER_VERTICAL)
            self.rows.append((item, choice))
            self.badges.append(badge)
        panel.SetSizer(grid)
        panel.SetupScrolling(scroll_x=False)
        v.Add(panel, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=12)

        quick = wx.BoxSizer(wx.HORIZONTAL)
        for label, side in (("All: keep mine", MINE), ("All: take the server's", THEIRS)):
            b = wx.Button(self, label=label)
            b.SetBackgroundColour(_colour(side))
            b.SetForegroundColour(_text_colour())
            b.Bind(wx.EVT_BUTTON, lambda e, s=side: self._set_all(s))
            quick.Add(b, flag=wx.RIGHT, border=6)
        v.Add(quick, flag=wx.ALL, border=12)

        auto = [i for i in items if not i.needs_choice]
        if auto:
            gen = [i.path for i in auto if i.kind in ('generated', 'readme')]
            other = [i.path for i in auto if i.kind not in ('generated', 'readme')]
            text = []
            if gen:
                text.append("Rebuilt from the merged design at your next commit: " + ", ".join(gen))
            if other:
                text.append("Kept as yours (view settings) or merged automatically: " + ", ".join(other))
            st = wx.StaticText(self, label="\n".join(text))
            st.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
            st.Wrap(600)
            v.Add(st, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=12)

        note = wx.StaticText(self, label=(
            "'Keep mine' drops the server's changes to that file, 'Take the server's' drops yours; both "
            "stay in the history. Combined files are checked by loading them in KiCad, and the merge is "
            "refused if two parts would end up with the same reference. Nothing changes until every "
            "check passes.\n\n"
            "Save your work in the PCB and Schematic editors first. After merging, the PCB editor still "
            "shows the board as it was: close it without saving and open it again."))
        note.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        note.Wrap(600)
        v.Add(note, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=12)

        btns = wx.StdDialogButtonSizer()
        self.btn_ok = wx.Button(self, wx.ID_OK, "Merge")
        btns.AddButton(self.btn_ok)
        btns.AddButton(wx.Button(self, wx.ID_CANCEL))
        btns.Realize()
        v.Add(btns, flag=wx.ALIGN_RIGHT | wx.ALL, border=10)

        self.SetSizer(v)
        self.CenterOnParent()
        self._update_ok()

    def _set_all(self, side):
        for item, choice in self.rows:
            choice.SetSelection(item.options.index(side))
        self._update_ok()

    def _update_ok(self, event=None):
        for (item, c), badge in zip(self.rows, self.badges):
            sel = c.GetSelection()
            _paint_badge(badge, item.options[sel] if sel != wx.NOT_FOUND else None)
        self.btn_ok.Enable(all(c.GetSelection() != wx.NOT_FOUND for _, c in self.rows))

    def get_choices(self):
        return {item.path: item.options[c.GetSelection()] for item, c in self.rows}


def project_sheet_locks(project_dir):
    """Lock files of schematics the Schematic Editor has open in this project."""
    out = []
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d != '.git']
        out += [os.path.join(root, f) for f in files
                if f.startswith('~') and f.endswith('.kicad_sch.lck') and not f.startswith('~tmp_git_old_')]
    return out
