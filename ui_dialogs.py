import wx

class SettingsDialog(wx.Dialog):
    def __init__(self, parent, current_settings):
        # Increased window height from 380 to 520 to accommodate the new BOM inputs
        super().__init__(parent, title="Settings", size=(470, 520)) 
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
        
        self.cb_bom_eng = wx.CheckBox(self, label="Generate Engineering BOM (Includes Value & Footprint)")
        self.cb_bom_eng.SetValue(self.settings.get('generate_bom_eng', False))
        self.cb_bom_eng.SetToolTip("Detailed CSV easier for human review.")
        bom_sizer.Add(self.cb_bom_eng, flag=wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, border=10)
        
        mpn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        mpn_sizer.Add(wx.StaticText(self, label="Custom MPN Field Name:"), flag=wx.ALIGN_CENTER_VERTICAL|wx.RIGHT, border=5)
        self.tc_mpn = wx.TextCtrl(self, value=self.settings.get('mpn_field_name', 'MPN'))
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
    def __init__(self, parent, changed_files, kicad_version="", include_version=True):
        super().__init__(parent, title="Commit Changes", size=(500, 450))
        
        self.changed_files = changed_files
        self.kicad_version = kicad_version
        self.include_version = include_version
        
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
        
        # File selection
        vbox.Add(wx.StaticText(self, label="Select files to commit:"), flag=wx.LEFT, border=10)
        self.clb_files = wx.CheckListBox(self, choices=self.changed_files)
        for i in range(len(self.changed_files)):
            self.clb_files.Check(i, True)  # Check all by default
        vbox.Add(self.clb_files, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)
        
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

    def get_message(self):
        msg = self.tc_msg.GetValue().strip()
        if self.include_version and self.kicad_version and msg:
            msg += f"\n\n[KiCad Version: {self.kicad_version}]"
        return msg
        
    def get_branch(self):
        return self.tc_branch.GetValue().strip()
        
    def get_selected_files(self):
        return [self.changed_files[i] for i in range(self.clb_files.GetCount()) if self.clb_files.IsChecked(i)]