import os
import wx
import wx.lib.scrolledpanel
from .model_exporter import render_supported, step_silkscreen_supported


class _WrapText(wx.StaticText):
    """Grey help text that re-wraps to its current width, so long notes are
    never clipped on narrow windows or with large system fonts."""

    def __init__(self, parent, text):
        # NO_AUTORESIZE: otherwise every SetLabel snaps the control back to the
        # unwrapped width, and the wrap undoes itself.
        super().__init__(parent, label=text, style=wx.ST_NO_AUTORESIZE)
        self._text = text
        self._width = 0
        self.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        self.SetMinSize((50, -1))  # let the sizer decide the width, not the unwrapped text
        self.Bind(wx.EVT_SIZE, self._on_size)

    def _on_size(self, event):
        event.Skip()
        w = event.GetSize().width
        if w > 20 and w != self._width:
            self._width = w
            self.SetLabel(self._text)
            self.Wrap(w)
            self.SetMinSize((50, self.GetBestSize().height))
            wx.CallAfter(self._relayout)

    def _relayout(self):
        if self:
            self.GetParent().Layout()


def _note(parent, text):
    return _WrapText(parent, text)


def _choice(parent, labels, values, current):
    ch = wx.Choice(parent, choices=labels)
    ch.SetSelection(values.index(current) if current in values else 0)
    return ch


class SettingsDialog(wx.Dialog):
    """All plugin settings, in tabs. Everything is stored with the project
    (committed, so everyone who clones it generates the same files) except the
    options marked 'this computer only'.

    Image rendering needs KiCad 9.0+, so the render controls are disabled
    (with an explanatory tooltip) on older versions."""

    VIEW_LABELS = ["Top", "Bottom", "Top and bottom (two images)", "Left", "Right", "Front", "Back"]
    VIEW_VALUES = ["top", "bottom", "both", "left", "right", "front", "back"]

    def __init__(self, parent, current_settings, kicad_version=""):
        super().__init__(parent, title="Settings", size=(560, 640),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.settings = current_settings.copy()
        self.render_ok = render_supported(kicad_version)
        s = self.settings

        vbox = wx.BoxSizer(wx.VERTICAL)
        nb = wx.Notebook(self)
        nb.AddPage(self._general_page(nb, s), "General")
        nb.AddPage(self._outputs_page(nb, s, kicad_version), "Outputs")
        nb.AddPage(self._readme_page(nb, s), "README")
        vbox.Add(nb, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        vbox.Add(_note(self, "Settings are saved with this project and shared through git, "
                             "except those marked 'this computer only'."),
                 flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=12)

        btn_sizer = wx.StdDialogButtonSizer()
        btn_sizer.AddButton(wx.Button(self, wx.ID_OK))
        btn_sizer.AddButton(wx.Button(self, wx.ID_CANCEL))
        btn_sizer.Realize()
        vbox.Add(btn_sizer, flag=wx.ALIGN_RIGHT | wx.ALL, border=10)

        self.SetSizer(vbox)
        self.SetMinSize((520, 520))
        self.CenterOnParent()
        self._sync_enabled_state()

    # ----- pages -------------------------------------------------------------

    def _general_page(self, nb, s):
        p = wx.Panel(nb)
        v = wx.BoxSizer(wx.VERTICAL)

        box = wx.StaticBoxSizer(wx.VERTICAL, p, "Commits (this computer only)")
        sb = box.GetStaticBox()
        self.cb_kicad_version = wx.CheckBox(sb, label="Append the KiCad version to commit messages")
        self.cb_kicad_version.SetValue(s.get('include_kicad_version', True))
        box.Add(self.cb_kicad_version, flag=wx.ALL, border=8)
        self.cb_silent_pull = wx.CheckBox(sb, label="Pull changed text files before pushing (Silent Pull)")
        self.cb_silent_pull.SetValue(s.get('silent_pull', False))
        self.cb_silent_pull.SetToolTip("Automatically pulls remote changes to safe text files (README.md, .csv) before pushing.\n"
                                       "Aborts if remote schematic or PCB changes are detected.")
        box.Add(self.cb_silent_pull, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=8)
        v.Add(box, flag=wx.EXPAND | wx.ALL, border=10)

        p.SetSizer(v)
        return p

    def _outputs_page(self, nb, s, kicad_version):
        p = wx.lib.scrolledpanel.ScrolledPanel(nb)
        p.SetupScrolling(scroll_x=False)
        v = wx.BoxSizer(wx.VERTICAL)

        when = wx.BoxSizer(wx.HORIZONTAL)
        when.Add(wx.StaticText(p, label="Generate outputs:"), flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=8)
        self.ch_when = _choice(p, ["On every commit", "Only with the 'Generate Project Files' button"],
                               [False, True], bool(s.get('manual_file_generation', False)))
        self.ch_when.SetToolTip("Applies to everything on this page, the README and the BOMs.")
        when.Add(self.ch_when, proportion=1)
        v.Add(when, flag=wx.EXPAND | wx.ALL, border=10)

        # Gerbers
        box = wx.StaticBoxSizer(wx.VERTICAL, p, "Fabrication")
        sb = box.GetStaticBox()
        self.cb_gerbers = wx.CheckBox(sb, label="Gerber and drill files (production/gerbers.zip)")
        self.cb_gerbers.SetValue(s.get('generate_gerbers_zip', False))
        self.cb_gerbers.SetToolTip("Uses the conventions JLCPCB asks for (Protel file extensions, one merged drill file),\n"
                                   "which PCBWay, OSH Park, Aisler and most other fabs accept as well.")
        box.Add(self.cb_gerbers, flag=wx.ALL, border=8)
        box.Add(_note(sb, "Usually only needed when you are close to ordering boards."),
                flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=8)

        self.cb_bom = wx.CheckBox(sb, label="Bill of materials (production/)")
        self.cb_bom.SetValue(s.get('generate_bom', bool(s.get('generate_bom_eng') or s.get('generate_bom_dist'))))
        box.Add(self.cb_bom, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=8)
        box.Add(_note(sb, "The BOM files, part-number field, parts and columns are chosen in "
                          "the BOM window each time it runs."),
                flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=8)
        v.Add(box, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        # STEP
        box = wx.StaticBoxSizer(wx.VERTICAL, p, "3D STEP model")
        sb = box.GetStaticBox()
        self.cb_step = wx.CheckBox(sb, label="Export a STEP model to /3d")
        self.cb_step.SetValue(s.get('export_step', False))
        self.cb_step.Bind(wx.EVT_CHECKBOX, self.on_toggle)
        box.Add(self.cb_step, flag=wx.ALL, border=8)
        self.cb_subst = wx.CheckBox(sb, label="Substitute similar 3D models when exact ones are missing")
        self.cb_subst.SetValue(s.get('step_subst_models', True))
        self.cb_nodnp = wx.CheckBox(sb, label="Exclude Do-Not-Populate (DNP) components")
        self.cb_nodnp.SetValue(s.get('step_no_dnp', False))
        self.cb_boardonly = wx.CheckBox(sb, label="Board only (no components)")
        self.cb_boardonly.SetValue(s.get('step_board_only', False))
        self.silk_ok = step_silkscreen_supported(kicad_version)
        self.cb_silk = wx.CheckBox(sb, label="Include silkscreen and solder mask"
                                   + ("" if self.silk_ok else "  (requires KiCad 9.0+)"))
        self.cb_silk.SetValue(s.get('step_silkscreen', False) and self.silk_ok)
        self.cb_silk.SetToolTip("Adds the silkscreen, and the solder mask it sits on, as flat faces on the board surface.")
        for cb in (self.cb_subst, self.cb_nodnp, self.cb_boardonly, self.cb_silk):
            box.Add(cb, flag=wx.LEFT | wx.BOTTOM, border=28)
        v.Add(box, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        # Render
        label = "PCB image" + ("" if self.render_ok else "  (requires KiCad 9.0+)")
        box = wx.StaticBoxSizer(wx.VERTICAL, p, label)
        sb = box.GetStaticBox()
        self.cb_render = wx.CheckBox(sb, label="Render the board to /docs")
        self.cb_render.SetValue(s.get('render_image', False) and self.render_ok)
        self.cb_render.Bind(wx.EVT_CHECKBOX, self.on_toggle)
        box.Add(self.cb_render, flag=wx.ALL, border=8)

        grid = wx.FlexGridSizer(cols=2, vgap=6, hgap=10)
        grid.AddGrowableCol(1, 1)
        view = 'both' if s.get('render_both_sides', False) else s.get('render_side', 'top')
        self.ch_view = _choice(sb, self.VIEW_LABELS, self.VIEW_VALUES, view)
        self.ch_quality = _choice(sb, ["Basic (fast)", "High (ray-traced, slow)"], ["basic", "high"],
                                  s.get('render_quality', 'basic'))
        self.ch_bg = _choice(sb, ["Opaque", "Transparent"], ["opaque", "transparent"],
                             s.get('render_background', 'opaque'))
        for text, ctrl in (("View:", self.ch_view), ("Quality:", self.ch_quality), ("Background:", self.ch_bg)):
            grid.Add(wx.StaticText(sb, label=text), flag=wx.ALIGN_CENTER_VERTICAL)
            grid.Add(ctrl, flag=wx.EXPAND)
        box.Add(grid, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=28)

        self.cb_dims = wx.CheckBox(sb, label="Also make a dimensioned top-view drawing")
        self.cb_dims.SetValue(s.get('render_dimensions', False))
        self.cb_dims.SetToolTip("Board width/height, corner radius, mounting-hole diameters and edge distances,\n"
                                "read straight from the board.")
        box.Add(self.cb_dims, flag=wx.LEFT | wx.BOTTOM, border=28)

        size = wx.BoxSizer(wx.HORIZONTAL)
        size.Add(wx.StaticText(sb, label="Image size (px):"), flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=8)
        self.sc_width = wx.SpinCtrl(sb, min=256, max=8192, initial=int(s.get('render_width', 1600)), size=(80, -1))
        self.sc_height = wx.SpinCtrl(sb, min=256, max=8192, initial=int(s.get('render_height', 1200)), size=(80, -1))
        size.Add(self.sc_width)
        size.Add(wx.StaticText(sb, label=" × "), flag=wx.ALIGN_CENTER_VERTICAL)
        size.Add(self.sc_height)
        box.Add(size, flag=wx.LEFT | wx.BOTTOM, border=28)
        v.Add(box, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        # Schematic
        box = wx.StaticBoxSizer(wx.VERTICAL, p, "Schematic image")
        sb = box.GetStaticBox()
        self.cb_schematic = wx.CheckBox(sb, label="Export the schematic to /docs (one SVG per sheet)")
        self.cb_schematic.SetValue(s.get('export_schematic', False))
        self.cb_schematic.Bind(wx.EVT_CHECKBOX, self.on_toggle)
        box.Add(self.cb_schematic, flag=wx.ALL, border=8)
        self.cb_sch_bw = wx.CheckBox(sb, label="Black and white")
        self.cb_sch_bw.SetValue(s.get('schematic_bw', False))
        self.cb_sch_nosheet = wx.CheckBox(sb, label="Without the drawing sheet (border and title block)")
        self.cb_sch_nosheet.SetValue(s.get('schematic_no_sheet', False))
        for cb in (self.cb_sch_bw, self.cb_sch_nosheet):
            box.Add(cb, flag=wx.LEFT | wx.BOTTOM, border=28)
        v.Add(box, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        p.SetSizer(v)
        return p

    def _readme_page(self, nb, s):
        p = wx.Panel(nb)
        v = wx.BoxSizer(wx.VERTICAL)

        self.cb_readme = wx.CheckBox(p, label="Keep a hardware summary in README.md")
        self.cb_readme.SetValue(s.get('auto_readme', False))
        self.cb_readme.SetToolTip("Maintains a section in the README with board stats and the BOM.")
        self.cb_readme.Bind(wx.EVT_CHECKBOX, self.on_toggle)
        v.Add(self.cb_readme, flag=wx.ALL, border=12)

        self.cb_readme_drc = wx.CheckBox(p, label="Include the DRC result (runs a design rules check)")
        self.cb_readme_drc.SetValue(s.get('readme_drc', False))
        v.Add(self.cb_readme_drc, flag=wx.LEFT | wx.BOTTOM, border=32)

        box = wx.StaticBoxSizer(wx.VERTICAL, p, "Embedded images")
        sb = box.GetStaticBox()
        box.Add(_note(sb, "Board and schematic images from the Outputs tab are added "
                          "to the README whenever they are generated."), flag=wx.EXPAND | wx.ALL, border=8)
        grid = wx.FlexGridSizer(cols=2, vgap=6, hgap=10)
        self.sc_readme_w = wx.SpinCtrl(sb, min=100, max=2000, initial=int(s.get('readme_image_width', 500)))
        self.sc_sch_w = wx.SpinCtrl(sb, min=100, max=2000, initial=int(s.get('readme_schematic_width', 700)))
        for text, ctrl in (("Board image width (px):", self.sc_readme_w),
                           ("Schematic width (px):", self.sc_sch_w)):
            grid.Add(wx.StaticText(sb, label=text), flag=wx.ALIGN_CENTER_VERTICAL)
            grid.Add(ctrl)
        box.Add(grid, flag=wx.ALL, border=8)
        v.Add(box, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=12)

        box = wx.StaticBoxSizer(wx.VERTICAL, p, "Part links in the BOM table")
        sb = box.GetStaticBox()
        grid = wx.FlexGridSizer(cols=2, vgap=6, hgap=10)
        grid.AddGrowableCol(1, 1)
        engines = ["Octopart", "ComponentSearchEngine"]
        self.ch_engine = _choice(sb, engines, engines, s.get('search_engine', 'Octopart'))
        currencies = ["USD", "EUR", "GBP", "CAD", "AUD", "JPY"]
        self.ch_currency = _choice(sb, currencies, currencies, s.get('currency', 'USD'))
        for text, ctrl in (("Search site:", self.ch_engine), ("Octopart currency:", self.ch_currency)):
            grid.Add(wx.StaticText(sb, label=text), flag=wx.ALIGN_CENTER_VERTICAL)
            grid.Add(ctrl, flag=wx.EXPAND)
        box.Add(grid, flag=wx.EXPAND | wx.ALL, border=8)
        v.Add(box, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=12)

        p.SetSizer(v)
        return p

    # ----- state -------------------------------------------------------------

    def on_toggle(self, event):
        self._sync_enabled_state()

    def _sync_enabled_state(self):
        """Grey out sub-options whose parent toggle is off."""
        step_on = self.cb_step.GetValue()
        for c in (self.cb_subst, self.cb_nodnp, self.cb_boardonly):
            c.Enable(step_on)
        self.cb_silk.Enable(step_on and self.silk_ok)

        self.cb_render.Enable(self.render_ok)
        if not self.render_ok:
            self.cb_render.SetToolTip("Requires KiCad 9.0+. Your installed version does not provide 'kicad-cli pcb render'.")
        render_on = self.render_ok and self.cb_render.GetValue()
        for c in (self.ch_view, self.ch_quality, self.ch_bg, self.cb_dims, self.sc_width, self.sc_height):
            c.Enable(render_on)

        sch_on = self.cb_schematic.GetValue()
        for c in (self.cb_sch_bw, self.cb_sch_nosheet):
            c.Enable(sch_on)

        self.cb_readme_drc.Enable(self.cb_readme.GetValue())

    def get_settings(self):
        s = self.settings
        s['include_kicad_version'] = self.cb_kicad_version.IsChecked()
        s['silent_pull'] = self.cb_silent_pull.IsChecked()

        s['manual_file_generation'] = self.ch_when.GetSelection() == 1
        s['generate_gerbers_zip'] = self.cb_gerbers.IsChecked()

        s['export_step'] = self.cb_step.IsChecked()
        s['step_subst_models'] = self.cb_subst.IsChecked()
        s['step_no_dnp'] = self.cb_nodnp.IsChecked()
        s['step_board_only'] = self.cb_boardonly.IsChecked()
        s['step_silkscreen'] = self.cb_silk.IsChecked() and self.silk_ok

        # Never persist render options as on for an unsupported KiCad version.
        s['render_image'] = self.cb_render.IsChecked() and self.render_ok
        s['render_dimensions'] = self.cb_dims.IsChecked() and self.render_ok
        view = self.VIEW_VALUES[self.ch_view.GetSelection()]
        s['render_both_sides'] = view == 'both'
        s['render_side'] = 'top' if view == 'both' else view
        s['render_quality'] = ["basic", "high"][self.ch_quality.GetSelection()]
        s['render_background'] = ["opaque", "transparent"][self.ch_bg.GetSelection()]
        s['render_width'] = self.sc_width.GetValue()
        s['render_height'] = self.sc_height.GetValue()

        s['export_schematic'] = self.cb_schematic.IsChecked()
        s['schematic_bw'] = self.cb_sch_bw.IsChecked()
        s['schematic_no_sheet'] = self.cb_sch_nosheet.IsChecked()

        s['auto_readme'] = self.cb_readme.IsChecked()
        s['readme_drc'] = self.cb_readme_drc.IsChecked()
        s['readme_image_width'] = self.sc_readme_w.GetValue()
        s['readme_schematic_width'] = self.sc_sch_w.GetValue()

        s['generate_bom'] = self.cb_bom.IsChecked()
        s['search_engine'] = self.ch_engine.GetStringSelection()
        s['currency'] = self.ch_currency.GetStringSelection()
        return s


class CommitDialog(wx.Dialog):
    # Maps a git status code to (badge text, colour). git diff --name-status
    # yields M/A/D/T/R/C; porcelain yields '??' for untracked files.
    @staticmethod
    def _classify(code):
        if code in ('A', '??'):
            return ("＋ NEW", wx.Colour(30, 140, 30))
        if code in ('M', 'T'):
            return ("● MOD", wx.Colour(200, 120, 0))
        if code == 'D':
            return ("－ DEL", wx.Colour(200, 40, 40))
        if code and code.startswith('R'):
            return ("→ REN", wx.Colour(40, 90, 200))
        return ("• ?", wx.Colour(120, 120, 120))

    def __init__(self, parent, changed_files, kicad_version="", include_version=True,
                 file_statuses=None, project_dir=None):
        super().__init__(parent, title="Commit Changes", size=(560, 500),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)

        self.changed_files = list(changed_files)
        self.kicad_version = kicad_version
        self.include_version = include_version
        self.file_statuses = file_statuses or {}
        self.project_dir = project_dir

        # filename -> checkbox, filename -> (badge, checkbox, ignore_btn)
        self.file_checks = {}
        self.file_rows = {}

        vbox = wx.BoxSizer(wx.VERTICAL)

        # Branch selection
        branch_box = wx.BoxSizer(wx.HORIZONTAL)
        branch_box.Add(wx.StaticText(self, label="New Branch (optional):"), flag=wx.ALIGN_CENTER_VERTICAL|wx.RIGHT, border=5)
        self.tc_branch = wx.TextCtrl(self)
        branch_box.Add(self.tc_branch, proportion=1)
        vbox.Add(branch_box, flag=wx.EXPAND | wx.ALL, border=10)

        # Commit message
        vbox.Add(wx.StaticText(self, label="Commit Message:"), flag=wx.LEFT | wx.TOP, border=10)
        self.tc_msg = wx.TextCtrl(self, style=wx.TE_MULTILINE)
        vbox.Add(self.tc_msg, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        # File selection (custom rows: status badge + checkbox + ignore button)
        vbox.Add(wx.StaticText(self, label="Select files to commit:"), flag=wx.LEFT, border=10)

        self.file_panel = wx.ScrolledWindow(self, style=wx.VSCROLL | wx.HSCROLL)
        self.file_panel.SetScrollRate(10, 10)
        self.file_sizer = wx.FlexGridSizer(cols=3, vgap=4, hgap=8)
        self.file_sizer.AddGrowableCol(1, 1)

        try:
            ignore_bmp = wx.ArtProvider.GetBitmap(wx.ART_DELETE, wx.ART_BUTTON, (16, 16))
        except Exception:
            ignore_bmp = wx.NullBitmap

        for fname in self.changed_files:
            code = self.file_statuses.get(fname, '')
            badge_text, badge_colour = self._classify(code)

            badge = wx.StaticText(self.file_panel, label=badge_text, size=(60, -1))
            badge.SetForegroundColour(badge_colour)
            badge_font = badge.GetFont()
            badge_font.SetWeight(wx.FONTWEIGHT_BOLD)
            badge.SetFont(badge_font)

            cb = wx.CheckBox(self.file_panel, label=fname)
            cb.SetValue(True)  # Check all by default

            if ignore_bmp and ignore_bmp.IsOk():
                btn_ignore = wx.BitmapButton(self.file_panel, bitmap=ignore_bmp, style=wx.BU_EXACTFIT)
            else:
                btn_ignore = wx.Button(self.file_panel, label="Ignore", style=wx.BU_EXACTFIT)
            btn_ignore.SetToolTip("Add to .gitignore and remove from this commit")
            btn_ignore.Bind(wx.EVT_BUTTON, lambda evt, f=fname: self._on_ignore(f))
            if not self.project_dir:
                btn_ignore.Disable()

            self.file_sizer.Add(badge, flag=wx.ALIGN_CENTER_VERTICAL | wx.LEFT, border=6)
            self.file_sizer.Add(cb, flag=wx.ALIGN_CENTER_VERTICAL | wx.EXPAND)
            self.file_sizer.Add(btn_ignore, flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=6)

            self.file_checks[fname] = cb
            self.file_rows[fname] = (badge, cb, btn_ignore)

        self.file_panel.SetSizer(self.file_sizer)
        self.file_panel.FitInside()
        vbox.Add(self.file_panel, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        # Buttons
        btn_sizer = wx.StdDialogButtonSizer()
        btn_ok = wx.Button(self, wx.ID_OK, label="Commit")
        btn_cancel = wx.Button(self, wx.ID_CANCEL)
        btn_sizer.AddButton(btn_ok)
        btn_sizer.AddButton(btn_cancel)
        btn_sizer.Realize()
        vbox.Add(btn_sizer, flag=wx.ALIGN_RIGHT|wx.BOTTOM|wx.RIGHT, border=10)

        self.SetSizer(vbox)
        self.CenterOnParent()

    def _add_to_gitignore(self, filename):
        """Appends the file to the project .gitignore (creating it if needed).
        Returns True if newly added, False if it was already present."""
        path = os.path.join(self.project_dir, ".gitignore")
        entry = filename.replace(os.sep, '/')

        existing_lines = []
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                existing_lines = f.read().splitlines()

        if entry in [l.strip() for l in existing_lines]:
            return False

        needs_leading_nl = bool(existing_lines) and existing_lines[-1].strip() != ""
        with open(path, 'a', encoding='utf-8') as f:
            if needs_leading_nl:
                f.write("\n")
            f.write(entry + "\n")
        return True

    def _on_ignore(self, filename):
        if not self.project_dir:
            return
        try:
            self._add_to_gitignore(filename)
        except Exception as e:
            wx.MessageBox(f"Could not update .gitignore:\n{e}", "Error", wx.ICON_ERROR)
            return

        # Remove the row so the file is excluded from this commit.
        rec = self.file_rows.pop(filename, None)
        self.file_checks.pop(filename, None)
        if rec:
            for widget in rec:
                self.file_sizer.Detach(widget)
                widget.Destroy()
            self.file_panel.Layout()
            self.file_panel.FitInside()

    def get_message(self):
        msg = self.tc_msg.GetValue().strip()
        if self.include_version and self.kicad_version and msg:
            msg += f"\n\n[KiCad Version: {self.kicad_version}]"
        return msg

    def get_branch(self):
        return self.tc_branch.GetValue().strip()

    def get_selected_files(self):
        return [f for f, cb in self.file_checks.items() if cb.IsChecked()]

class BundlePreviewDialog(wx.Dialog):
    """Shows what 'Bundle Libraries into Project' will copy before anything is
    changed. `rescan(include_stock)` must return a fresh BundlePlan; it is called
    again when the 'include stock parts' box is toggled."""

    def __init__(self, parent, plan, rescan, include_stock=False, intro_extra=None):
        super().__init__(parent, title="Bundle Libraries into Project", size=(720, 640),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.plan = plan
        self.rescan = rescan

        vbox = wx.BoxSizer(wx.VERTICAL)
        intro = wx.StaticText(self, label=(
            "Copies the symbols, footprints and 3D models this project uses from your personal\n"
            f"libraries into /libs and relinks the design to the '{plan.nickname}' project library,\n"
            "so teammates can open the project without your libraries."
        ))
        intro.SetForegroundColour(wx.Colour(100, 100, 100))
        vbox.Add(intro, flag=wx.ALL, border=15)
        if intro_extra:
            extra = wx.StaticText(self, label=intro_extra)
            extra.SetForegroundColour(wx.Colour(170, 90, 0))
            vbox.Add(extra, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=15)

        self.list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.BORDER_SUNKEN)
        self.list.InsertColumn(0, "Type", width=90)
        self.list.InsertColumn(1, "Item", width=300)
        self.list.InsertColumn(2, "Becomes / Source", width=290)
        vbox.Add(self.list, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=15)

        self.summary = wx.StaticText(self, label="")
        vbox.Add(self.summary, flag=wx.ALL, border=15)

        self.warn = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 90))
        vbox.Add(self.warn, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=15)

        self.cb_stock = wx.CheckBox(self, label="Include stock KiCad parts too")
        self.cb_stock.SetToolTip("Stock libraries ship with KiCad, so teammates already have them. "
                                 "Enable only if you want the project fully self-contained.")
        self.cb_stock.SetValue(include_stock)
        self.cb_stock.Bind(wx.EVT_CHECKBOX, self.on_stock_toggle)
        vbox.Add(self.cb_stock, flag=wx.ALL, border=15)

        note = wx.StaticText(self, label=(
            "The Schematic Editor must be closed. The board is saved automatically;\n"
            "afterwards, reopen the project so KiCad loads the new library tables."
        ))
        note.SetForegroundColour(wx.Colour(170, 90, 0))
        vbox.Add(note, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=15)

        btn_sizer = wx.StdDialogButtonSizer()
        self.btn_ok = wx.Button(self, wx.ID_OK, label="Bundle")
        btn_sizer.AddButton(self.btn_ok)
        btn_sizer.AddButton(wx.Button(self, wx.ID_CANCEL))
        btn_sizer.Realize()
        vbox.Add(btn_sizer, flag=wx.ALIGN_RIGHT | wx.BOTTOM | wx.RIGHT, border=10)

        self.SetSizer(vbox)
        self.CenterOnParent()
        self._populate()

    def _populate(self):
        p = self.plan
        self.list.DeleteAllItems()
        rows = []
        for (lib, name), i in sorted(p.symbols.items()):
            rows.append(("Symbol", f"{lib}:{name}", f"{p.nickname}:{i['new_name']}"))
        for (lib, name), i in sorted(p.footprints.items()):
            origin = "already in project library" if i.get('exists') else i['origin']
            rows.append(("Footprint", f"{lib}:{name}", f"{p.nickname}:{i['new_name']}  ({origin})"))
        for raw, i in sorted(p.models.items()):
            rows.append(("3D model", os.path.basename(i['src']), f"libs/3dmodels/{i['dest_name']}"))
        for old, new in sorted(p.field_remaps.items()):
            rows.append(("Fp. link", old, f"{new}  (already bundled)"))
        for r in rows:
            idx = self.list.InsertItem(self.list.GetItemCount(), r[0])
            self.list.SetItem(idx, 1, r[1])
            self.list.SetItem(idx, 2, r[2])

        stock = p.skipped_stock
        text = (f"{len(p.symbols)} symbols, {len(p.footprints)} footprints, {len(p.models)} 3D models to bundle.")
        if p.field_remaps:
            text += f"  {len(p.field_remaps)} stale footprint link(s) to fix."
        if stock['symbols'] or stock['footprints']:
            text += f"  Stock parts left linked to KiCad libraries: {stock['symbols']} symbols, {stock['footprints']} footprints."
        if p.is_empty():
            text = "Nothing to bundle: every part already comes from a stock or project library."
        self.summary.SetLabel(text)
        self.summary.Wrap(680)
        self.warn.SetValue("\n".join(p.warnings) if p.warnings else "No warnings.")
        self.btn_ok.Enable(not p.is_empty())
        self.Layout()

    def on_stock_toggle(self, event):
        wx.BeginBusyCursor()
        try:
            self.plan = self.rescan(self.cb_stock.GetValue())
        finally:
            wx.EndBusyCursor()
        self._populate()

    def include_stock(self):
        return self.cb_stock.GetValue()


def wait_until_done(parent, title, message, still_waiting, extra_button=None, interval_ms=500):
    """Shows `message` without blocking the rest of KiCad and returns once
    still_waiting() turns False (True), or when the user cancels (False).

    A modal dialog would disable every KiCad window, so the user couldn't do
    the very thing being waited for (e.g. close the Schematic Editor). This
    runs its own event loop instead, like ShowModal() but without disabling
    other windows. `parent` is disabled meanwhile so it can't be re-entered.

    extra_button: optional (label, callback). The callback returns True to
    finish, False to cancel, or None to keep waiting."""
    if not still_waiting():
        return True
    dlg = wx.Dialog(parent, title=title, style=wx.CAPTION | wx.CLOSE_BOX | wx.STAY_ON_TOP)
    v = wx.BoxSizer(wx.VERTICAL)
    v.Add(wx.StaticText(dlg, label=message), flag=wx.ALL, border=15)
    row = wx.BoxSizer(wx.HORIZONTAL)
    row.AddStretchSpacer()
    btn_extra = None
    if extra_button:
        btn_extra = wx.Button(dlg, label=extra_button[0])
        row.Add(btn_extra, flag=wx.RIGHT, border=8)
    btn_cancel = wx.Button(dlg, wx.ID_CANCEL, "Cancel")
    row.Add(btn_cancel)
    v.Add(row, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=15)
    dlg.SetSizerAndFit(v)
    dlg.CenterOnParent()

    loop = wx.GUIEventLoop()
    result = {}

    def finish(value):
        if 'value' not in result:
            result['value'] = value
            if loop.IsRunning():
                loop.Exit()

    def on_timer(event):
        if not still_waiting():
            finish(True)

    def on_extra(event):
        outcome = extra_button[1]()
        if outcome is not None:
            finish(outcome)
        elif not still_waiting():
            finish(True)

    timer = wx.Timer(dlg)
    dlg.Bind(wx.EVT_TIMER, on_timer, timer)
    btn_cancel.Bind(wx.EVT_BUTTON, lambda e: finish(False))
    dlg.Bind(wx.EVT_CLOSE, lambda e: finish(False))
    if btn_extra:
        btn_extra.Bind(wx.EVT_BUTTON, on_extra)

    if parent:
        parent.Disable()
    try:
        dlg.Show()
        timer.Start(interval_ms)
        if 'value' not in result:
            loop.Run()
    finally:
        timer.Stop()
        dlg.Destroy()
        if parent:
            parent.Enable()
            parent.Raise()
    return result.get('value', False)
