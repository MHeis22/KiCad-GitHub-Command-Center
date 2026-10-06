import os
from collections import Counter

import wx


class BOMOptionsDialog(wx.Dialog):
    """Asked before each BOM run: which BOMs, which part-number field, which
    parts and columns. Pre-filled from the last run's choices."""

    def __init__(self, parent, generator, settings, options):
        super().__init__(parent, title="Bill of Materials",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.generator = generator
        self.fields = generator.available_fields()
        info = generator.summary()

        vbox = wx.BoxSizer(wx.VERTICAL)

        sch = os.path.basename(generator.root_schematic() or "")
        intro = wx.StaticText(self, label=(
            f"{info['total']} components found in {sch} (all sheets).\n"
            "The BOM is built with KiCad's own BOM exporter."))
        vbox.Add(intro, flag=wx.ALL, border=10)

        # --- Outputs
        box = wx.StaticBoxSizer(wx.VERTICAL, self, "Files to generate (in production/)")
        # First run (nothing chosen yet): offer both rather than an empty choice.
        first = not (settings.get('generate_bom_eng') or settings.get('generate_bom_dist'))
        self.cb_eng = wx.CheckBox(box.GetStaticBox(), label="Engineering BOM (all columns, for review)")
        self.cb_eng.SetValue(first or settings.get('generate_bom_eng', False))
        self.cb_dist = wx.CheckBox(box.GetStaticBox(), label="Distributor BOM (Qty, Reference, part number)")
        self.cb_dist.SetValue(first or settings.get('generate_bom_dist', False))
        box.Add(self.cb_eng, flag=wx.ALL, border=4)
        box.Add(self.cb_dist, flag=wx.ALL, border=4)

        mpn_row = wx.BoxSizer(wx.HORIZONTAL)
        mpn_row.Add(wx.StaticText(box.GetStaticBox(), label="Part number field:"),
                    flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=6)
        mpn = generator.guess_mpn_field()
        choices = self.fields if mpn in self.fields else [mpn] + self.fields
        self.ch_mpn = wx.Choice(box.GetStaticBox(), choices=choices)
        self.ch_mpn.SetStringSelection(mpn)
        self.ch_mpn.SetToolTip("The symbol field holding the manufacturer part number. "
                               "The distributor BOM only lists parts that have it.")
        self.ch_mpn.Bind(wx.EVT_CHOICE, self._on_mpn_change)
        mpn_row.Add(self.ch_mpn, proportion=1)
        box.Add(mpn_row, flag=wx.EXPAND | wx.ALL, border=4)
        vbox.Add(box, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        # --- Which parts
        box = wx.StaticBoxSizer(wx.VERTICAL, self, "Parts to include")
        self.cb_dnp = wx.CheckBox(box.GetStaticBox(), label=(
            f"Do-not-populate (DNP) parts, flagged in a DNP column ({info['dnp']} in design)"))
        self.cb_dnp.SetValue(options.get('include_dnp', True))
        self.cb_off = wx.CheckBox(box.GetStaticBox(), label=(
            f"Parts not placed on the PCB, e.g. panel connectors or modules "
            f"({info['off_board']} in design)"))
        self.cb_off.SetValue(options.get('include_off_board', True))
        self.cb_group = wx.CheckBox(box.GetStaticBox(), label="Group identical parts into one row")
        self.cb_group.SetValue(options.get('group_identical', True))
        for cb in (self.cb_dnp, self.cb_off, self.cb_group):
            box.Add(cb, flag=wx.ALL, border=4)
        if info['excluded_from_bom']:
            note = wx.StaticText(box.GetStaticBox(), label=(
                f"{info['excluded_from_bom']} symbols are marked 'Exclude from bill of materials' "
                "in KiCad and are always left out."))
            note.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
            box.Add(note, flag=wx.ALL, border=4)
        vbox.Add(box, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        # --- Columns
        box = wx.StaticBoxSizer(wx.VERTICAL, self, "Extra columns in the engineering BOM")
        box.Add(wx.StaticText(box.GetStaticBox(), label=(
            "Qty, Reference, Value, Footprint, part number, DNP and 'Not on PCB' are always included.")),
            flag=wx.ALL, border=4)
        self.clb_fields = wx.CheckListBox(box.GetStaticBox(), size=(-1, 140))
        wanted = set(options.get('extra_fields', []))
        self._fill_fields(wanted)
        box.Add(self.clb_fields, proportion=1, flag=wx.EXPAND | wx.ALL, border=4)
        vbox.Add(box, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        btns = wx.StdDialogButtonSizer()
        ok = wx.Button(self, wx.ID_OK, "Generate BOM")
        ok.SetDefault()
        btns.AddButton(ok)
        btns.AddButton(wx.Button(self, wx.ID_CANCEL, "Skip BOM"))
        btns.Realize()
        vbox.Add(btns, flag=wx.EXPAND | wx.ALL, border=10)

        self.SetSizerAndFit(vbox)
        self.SetMinSize(self.GetSize())
        self.CenterOnParent()

    def _fill_fields(self, wanted):
        """Lists every field except the chosen part-number field (it has its own column)."""
        mpn = self.ch_mpn.GetStringSelection()
        self.clb_fields.Set([f for f in self.fields if f != mpn])
        for i in range(self.clb_fields.GetCount()):
            self.clb_fields.Check(i, self.clb_fields.GetString(i) in wanted)

    def _on_mpn_change(self, event):
        self._fill_fields(set(self.clb_fields.GetCheckedStrings()))

    def get_results(self):
        """(settings updates, per-project options)"""
        settings = {
            'generate_bom_eng': self.cb_eng.IsChecked(),
            'generate_bom_dist': self.cb_dist.IsChecked(),
            'mpn_field_name': self.ch_mpn.GetStringSelection(),
        }
        options = {
            'include_dnp': self.cb_dnp.IsChecked(),
            'include_off_board': self.cb_off.IsChecked(),
            'group_identical': self.cb_group.IsChecked(),
            'extra_fields': list(self.clb_fields.GetCheckedStrings()),
        }
        return settings, options


class BOMReportDialog(wx.Dialog):
    """Shown after a BOM run: what was written, and every component that is
    not in the BOM together with the reason."""

    def __init__(self, parent, report, project_dir):
        super().__init__(parent, title="BOM Generated", size=(720, 520),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        vbox = wx.BoxSizer(wx.VERTICAL)

        lines = []
        for path in report.files:
            lines.append("Wrote " + os.path.relpath(path, project_dir))
        for name, n in report.counts.items():
            lines.append(f"{name}: {n} of {report.total} components")
        vbox.Add(wx.StaticText(self, label="\n".join(lines) or "No BOM written."),
                 flag=wx.ALL, border=10)

        if report.excluded:
            head = wx.StaticText(self, label=f"Not in the BOM ({len(report.excluded)}):")
            f = head.GetFont()
            f.SetWeight(wx.FONTWEIGHT_BOLD)
            head.SetFont(f)
            vbox.Add(head, flag=wx.LEFT | wx.RIGHT, border=10)

            # Reason summary first, so a long list is still readable at a glance.
            counts = Counter(reason for _, _, reason in report.excluded)
            summary = "\n".join(f"  {n} x  {reason}" for reason, n in counts.most_common())
            vbox.Add(wx.StaticText(self, label=summary), flag=wx.ALL, border=10)

            lc = wx.ListCtrl(self, style=wx.LC_REPORT | wx.BORDER_SUNKEN)
            lc.InsertColumn(0, "Reference", width=90)
            lc.InsertColumn(1, "Value", width=160)
            lc.InsertColumn(2, "Reason", width=430)
            order = {reason: i for i, (reason, _) in enumerate(counts.most_common())}
            for i, (ref, value, reason) in enumerate(
                    sorted(report.excluded, key=lambda x: order[x[2]])):   # stable: refs stay sorted
                lc.InsertItem(i, ref)
                lc.SetItem(i, 1, value)
                lc.SetItem(i, 2, reason)
            vbox.Add(lc, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)
        else:
            vbox.Add(wx.StaticText(self, label="Every component is in the BOM."),
                     flag=wx.ALL, border=10)

        for note in report.notes:
            t = wx.StaticText(self, label=note)
            t.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
            vbox.Add(t, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)

        btns = wx.StdDialogButtonSizer()
        btns.AddButton(wx.Button(self, wx.ID_OK))
        btns.Realize()
        vbox.Add(btns, flag=wx.EXPAND | wx.ALL, border=10)

        self.SetSizer(vbox)
        self.CenterOnParent()
