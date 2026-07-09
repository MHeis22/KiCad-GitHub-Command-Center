import os
import wx
from .model_exporter import render_supported


class Model3DSettingsDialog(wx.Dialog):
    """Dedicated menu for 3D STEP export and PCB image render settings.

    STEP export works on KiCad 7+. Image rendering requires KiCad 9.0+, so the
    render controls are disabled (with an explanatory tooltip) on older versions.
    """

    def __init__(self, parent, current_settings, kicad_version=""):
        super().__init__(parent, title="3D Model & Render Settings", size=(500, 620))
        self.settings = current_settings.copy()
        self.kicad_version = kicad_version
        self.render_ok = render_supported(kicad_version)

        vbox = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(self, label=(
            "Generate a 3D STEP model and/or a rendered image of the PCB when you\n"
            "commit. Files are written to /3d and /docs and included in the repo."
        ))
        intro.SetForegroundColour(wx.Colour(100, 100, 100))
        vbox.Add(intro, flag=wx.ALL, border=15)

        # --- STEP Export ---
        step_box = wx.StaticBox(self, label="3D STEP Model (Geometry)")
        step_sizer = wx.StaticBoxSizer(step_box, wx.VERTICAL)

        self.cb_step = wx.CheckBox(self, label="Export 3D STEP model to /3d on commit")
        self.cb_step.SetValue(self.settings.get('export_step', False))
        self.cb_step.SetToolTip("Runs 'kicad-cli pcb export step'. Works on KiCad 7+.")
        self.cb_step.Bind(wx.EVT_CHECKBOX, self.on_toggle)
        step_sizer.Add(self.cb_step, flag=wx.ALL, border=8)

        self.cb_subst = wx.CheckBox(self, label="Substitute similar 3D models when exact ones are missing")
        self.cb_subst.SetValue(self.settings.get('step_subst_models', True))
        step_sizer.Add(self.cb_subst, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=8)

        self.cb_nodnp = wx.CheckBox(self, label="Exclude Do-Not-Populate (DNP) components")
        self.cb_nodnp.SetValue(self.settings.get('step_no_dnp', False))
        step_sizer.Add(self.cb_nodnp, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=8)

        self.cb_boardonly = wx.CheckBox(self, label="Board only (exclude all components)")
        self.cb_boardonly.SetValue(self.settings.get('step_board_only', False))
        step_sizer.Add(self.cb_boardonly, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=8)

        vbox.Add(step_sizer, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=15)

        # --- Image Render ---
        img_label = "PCB Image Render (README)"
        if not self.render_ok:
            img_label += "  — Requires KiCad 9.0+"
        render_box = wx.StaticBox(self, label=img_label)
        render_sizer = wx.StaticBoxSizer(render_box, wx.VERTICAL)

        self.cb_render = wx.CheckBox(self, label="Render PCB image to /docs and embed in README")
        self.cb_render.SetValue(self.settings.get('render_image', False) and self.render_ok)
        self.cb_render.Bind(wx.EVT_CHECKBOX, self.on_toggle)
        render_sizer.Add(self.cb_render, flag=wx.ALL, border=8)

        self.cb_both_sides = wx.CheckBox(self, label="Render both top and bottom (two images)")
        self.cb_both_sides.SetValue(self.settings.get('render_both_sides', False))
        self.cb_both_sides.SetToolTip("Renders top and bottom views and embeds both in the README. Overrides the single 'View side' choice.")
        self.cb_both_sides.Bind(wx.EVT_CHECKBOX, self.on_toggle)
        render_sizer.Add(self.cb_both_sides, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=8)

        # Quality / side / background dropdowns
        grid = wx.FlexGridSizer(cols=2, vgap=8, hgap=10)
        grid.AddGrowableCol(1, 1)

        self.quality_choices = ["basic", "high"]
        current_quality = self.settings.get('render_quality', 'basic')
        self.ch_quality = wx.Choice(self, choices=["Basic (fast)", "High (ray-traced, slow)"])
        self.ch_quality.SetSelection(self.quality_choices.index(current_quality) if current_quality in self.quality_choices else 0)
        grid.Add(wx.StaticText(self, label="Quality:"), flag=wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.ch_quality, flag=wx.EXPAND)

        self.side_choices = ["top", "bottom", "left", "right", "front", "back"]
        current_side = self.settings.get('render_side', 'top')
        self.ch_side = wx.Choice(self, choices=self.side_choices)
        self.ch_side.SetSelection(self.side_choices.index(current_side) if current_side in self.side_choices else 0)
        grid.Add(wx.StaticText(self, label="View side:"), flag=wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.ch_side, flag=wx.EXPAND)

        self.bg_choices = ["opaque", "transparent"]
        current_bg = self.settings.get('render_background', 'opaque')
        self.ch_bg = wx.Choice(self, choices=self.bg_choices)
        self.ch_bg.SetSelection(self.bg_choices.index(current_bg) if current_bg in self.bg_choices else 0)
        grid.Add(wx.StaticText(self, label="Background:"), flag=wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.ch_bg, flag=wx.EXPAND)

        self.sc_width = wx.SpinCtrl(self, min=256, max=8192, initial=int(self.settings.get('render_width', 1600)))
        grid.Add(wx.StaticText(self, label="Render width (px):"), flag=wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.sc_width, flag=wx.EXPAND)

        self.sc_height = wx.SpinCtrl(self, min=256, max=8192, initial=int(self.settings.get('render_height', 1200)))
        grid.Add(wx.StaticText(self, label="Render height (px):"), flag=wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.sc_height, flag=wx.EXPAND)

        self.sc_readme_w = wx.SpinCtrl(self, min=100, max=2000, initial=int(self.settings.get('readme_image_width', 500)))
        grid.Add(wx.StaticText(self, label="README image width (px):"), flag=wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.sc_readme_w, flag=wx.EXPAND)

        render_sizer.Add(grid, flag=wx.EXPAND | wx.ALL, border=8)
        vbox.Add(render_sizer, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=15)

        # Buttons
        btn_sizer = wx.StdDialogButtonSizer()
        btn_ok = wx.Button(self, wx.ID_OK)
        btn_cancel = wx.Button(self, wx.ID_CANCEL)
        btn_sizer.AddButton(btn_ok)
        btn_sizer.AddButton(btn_cancel)
        btn_sizer.Realize()
        vbox.Add(btn_sizer, flag=wx.ALIGN_RIGHT | wx.BOTTOM | wx.RIGHT, border=10)

        self.SetSizer(vbox)
        self.CenterOnParent()
        self._sync_enabled_state()

    def on_toggle(self, event):
        self._sync_enabled_state()

    def _sync_enabled_state(self):
        """Grey out sub-options when their parent toggle is off, and lock the
        render section entirely on KiCad < 9."""
        step_on = self.cb_step.GetValue()
        for cb in (self.cb_subst, self.cb_nodnp, self.cb_boardonly):
            cb.Enable(step_on)

        # Render checkbox is disabled outright on unsupported KiCad versions.
        self.cb_render.Enable(self.render_ok)
        if not self.render_ok:
            self.cb_render.SetToolTip("Requires KiCad 9.0+. Your installed version does not provide 'kicad-cli pcb render'.")

        render_on = self.render_ok and self.cb_render.GetValue()
        for ctrl in (self.cb_both_sides, self.ch_quality, self.ch_bg,
                     self.sc_width, self.sc_height, self.sc_readme_w):
            ctrl.Enable(render_on)
        # The single-side choice is irrelevant when rendering both sides.
        self.ch_side.Enable(render_on and not self.cb_both_sides.GetValue())

    def get_settings(self):
        self.settings['export_step'] = self.cb_step.IsChecked()
        self.settings['step_subst_models'] = self.cb_subst.IsChecked()
        self.settings['step_no_dnp'] = self.cb_nodnp.IsChecked()
        self.settings['step_board_only'] = self.cb_boardonly.IsChecked()

        # Never persist render_image as True on an unsupported version.
        self.settings['render_image'] = self.cb_render.IsChecked() and self.render_ok
        self.settings['render_both_sides'] = self.cb_both_sides.IsChecked()
        self.settings['render_quality'] = self.quality_choices[self.ch_quality.GetSelection()]
        self.settings['render_side'] = self.ch_side.GetStringSelection()
        self.settings['render_background'] = self.ch_bg.GetStringSelection()
        self.settings['render_width'] = self.sc_width.GetValue()
        self.settings['render_height'] = self.sc_height.GetValue()
        self.settings['readme_image_width'] = self.sc_readme_w.GetValue()
        return self.settings


class SettingsDialog(wx.Dialog):
    def __init__(self, parent, current_settings):
        # Slightly reduced window height as we moved the gerbers toggle
        super().__init__(parent, title="Settings", size=(470, 480)) 
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

        # DRC Check in Readme
        self.cb_readme_drc = wx.CheckBox(self, label="Include DRC (Design Rules Check) status in README")
        self.cb_readme_drc.SetValue(self.settings.get('readme_drc', False))
        self.cb_readme_drc.SetToolTip("Runs a background DRC check on the PCB during commit to display error/warning counts.")
        vbox.Add(self.cb_readme_drc, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=15)

        # Silent Pull toggle
        self.cb_silent_pull = wx.CheckBox(self, label="Auto-Pull text files before pushing (Silent Pull)")
        self.cb_silent_pull.SetValue(self.settings.get('silent_pull', False))
        self.cb_silent_pull.SetToolTip("Automatically pulls remote changes to safe text files (README.md, .csv) before pushing.\nAborts pulling if remote KiCad schematic or PCB changes are detected.")
        vbox.Add(self.cb_silent_pull, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=15)

        # --- BOM Generation ---
        bom_box = wx.StaticBox(self, label="BOM Generation (Auto-run on Commit)")
        bom_sizer = wx.StaticBoxSizer(bom_box, wx.VERTICAL)
        
        self.cb_bom_dist = wx.CheckBox(self, label="Generate Distributor BOM (Qty, Ref, MPN)")
        self.cb_bom_dist.SetValue(self.settings.get('generate_bom_dist', False))
        self.cb_bom_dist.SetToolTip("Compact CSV containing only what automated distributor tools need.")
        bom_sizer.Add(self.cb_bom_dist, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        
        self.cb_bom_eng = wx.CheckBox(self, label="Generate Engineering BOM (Includes Value, Footprint and DNP components)")
        self.cb_bom_eng.SetValue(self.settings.get('generate_bom_eng', False))
        self.cb_bom_eng.SetToolTip("A more detailed CSV easier for human review.")
        bom_sizer.Add(self.cb_bom_eng, flag=wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, border=10)
        
        mpn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        mpn_sizer.Add(wx.StaticText(self, label="Custom MPN Field Name:"), flag=wx.ALIGN_CENTER_VERTICAL|wx.RIGHT, border=5)
        self.tc_mpn = wx.TextCtrl(self, value=self.settings.get('mpn_field_name', 'Manufacturer_Part_Number'))
        self.tc_mpn.SetToolTip("The exact property name used in your KiCad symbols for the part number (e.g., LCSC, MPN, Part Number).") 
        mpn_sizer.Add(self.tc_mpn, proportion=1)
        bom_sizer.Add(mpn_sizer, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        
        vbox.Add(bom_sizer, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=15)

        # --- Search Engine & Currency Selections ---
        engine_choices = ["Octopart", "ComponentSearchEngine"]
        current_engine = self.settings.get('search_engine', 'Octopart')
        
        vbox.Add(wx.StaticText(self, label="BOM Component Search Engine:"), flag=wx.LEFT | wx.TOP, border=15)
        self.cb_engine = wx.Choice(self, choices=engine_choices)
        self.cb_engine.SetSelection(engine_choices.index(current_engine) if current_engine in engine_choices else 0)
        vbox.Add(self.cb_engine, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=15)
        
        currency_choices = ["USD", "EUR", "GBP", "CAD", "AUD", "JPY"]
        current_currency = self.settings.get('currency', 'USD')
        
        vbox.Add(wx.StaticText(self, label="Octopart Currency:"), flag=wx.LEFT | wx.TOP, border=15)
        self.cb_currency = wx.Choice(self, choices=currency_choices)
        self.cb_currency.SetSelection(currency_choices.index(current_currency) if current_currency in currency_choices else 0)
        vbox.Add(self.cb_currency, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=15)
        
        # ------------------------------------------------
        
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
        self.settings['readme_drc'] = self.cb_readme_drc.IsChecked()
        self.settings['silent_pull'] = self.cb_silent_pull.IsChecked()
        
        self.settings['generate_bom_dist'] = self.cb_bom_dist.IsChecked()
        self.settings['generate_bom_eng'] = self.cb_bom_eng.IsChecked()
        self.settings['mpn_field_name'] = self.tc_mpn.GetValue().strip() or "MPN"
        
        # Capture the new dropdown settings
        self.settings['search_engine'] = self.cb_engine.GetStringSelection()
        self.settings['currency'] = self.cb_currency.GetStringSelection()
        
        return self.settings

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