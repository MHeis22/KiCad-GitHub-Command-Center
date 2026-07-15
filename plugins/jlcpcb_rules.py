import wx
import pcbnew

def set_jlcpcb_constraints(parent_window):
    """
    Analyzes the active board and automatically applies JLCPCB's 
    most cost-effective manufacturing constraints.
    """
    try:
        board = pcbnew.GetBoard()
        if not board:
            wx.MessageBox("No board loaded.", "Error", wx.ICON_ERROR)
            return
            
        layer_count = board.GetCopperLayerCount()
        
        # JLCPCB limits vary heavily depending on the tier.
        if layer_count <= 2:
            tier_info = "1/2-Layer Economic Tier"
            min_track = 0.127      # 5 mil (Free)
            min_clearance = 0.127  
            min_via_drill = 0.3    # JLCPCB absolute cheapest via
            min_via_pad = 0.45     # 0.45mm dia at a 0.3mm drill -> the 0.075mm min annular ring (JLC cheapest via)
            alt_info = "Tip: JLCPCB allows smaller 0.09mm tracks on 4+ layers for free."
        else:
            tier_info = f"{layer_count}-Layer Economic Tier"
            min_track = 0.09       # 3.5 mil (Free for Multilayer)
            min_clearance = 0.09   
            min_via_drill = 0.3    # Maintained at 0.3mm to ensure the $2 promotional price
            min_via_pad = 0.45     # 0.45mm dia at a 0.3mm drill -> the 0.075mm min annular ring (JLC cheapest via)
            alt_info = "Note: While JLC technically supports 0.15mm vias on 4-layers, using 0.3mm vias guarantees you qualify for the absolute cheapest promotional price ($2 tier)."

        msg = (
            f"Detected Layer Count: {layer_count}\n\n"
            f"Applying the safest rules for the {tier_info}:\n\n"
            f"• Min Track & Clearance: {min_track} mm\n"
            f"• Min Via Drill: {min_via_drill} mm\n"
            f"• Min Via Pad: {min_via_pad} mm\n"
            f"• Min Annular Ring: 0.075 mm\n"
            f"• Solder Mask Clearance: 0.05 mm\n\n"
            f"{alt_info}\n\n"
            f"Overwrite your PCB's global constraints and Default netclass?"
        )
        
        dlg = wx.MessageDialog(parent_window, msg, "Apply JLCPCB Constraints", wx.YES_NO | wx.ICON_QUESTION)
        result = dlg.ShowModal()
        dlg.Destroy()
        
        if result == wx.ID_YES:
            settings = board.GetDesignSettings()
            
            # 1. Global Constraints (Hard DRC limits)
            if hasattr(settings, 'm_TrackMinWidth'): settings.m_TrackMinWidth = pcbnew.FromMM(min_track)
            if hasattr(settings, 'm_ViasMinSize'): settings.m_ViasMinSize = pcbnew.FromMM(min_via_pad)
            if hasattr(settings, 'm_ViasMinDrill'): settings.m_ViasMinDrill = pcbnew.FromMM(min_via_drill)
            
            # Solder Mask
            if hasattr(settings, 'm_SolderMaskMargin'): settings.m_SolderMaskMargin = pcbnew.FromMM(0.05)
            if hasattr(settings, 'm_SolderMaskMinWidth'): settings.m_SolderMaskMinWidth = pcbnew.FromMM(0.1)
            
            # Microvias: Disable entirely, and clamp their visual limits so the UI looks clean
            if hasattr(settings, 'm_MicroViasAllowed'): settings.m_MicroViasAllowed = False
            if hasattr(settings, 'm_MicroViasMinSize'): settings.m_MicroViasMinSize = pcbnew.FromMM(min_via_pad)
            if hasattr(settings, 'm_MicroViasMinDrill'): settings.m_MicroViasMinDrill = pcbnew.FromMM(min_via_drill)
            
            # Enforce absolute global clearance 
            if hasattr(settings, 'm_MinClearance'):
                settings.m_MinClearance = pcbnew.FromMM(min_clearance)
            
            # Calculate and enforce the annular ring constraint ((Pad - Drill) / 2)
            min_annular = (min_via_pad - min_via_drill) / 2.0
            if hasattr(settings, 'm_ViasMinAnnulus'):
                settings.m_ViasMinAnnulus = pcbnew.FromMM(min_annular)
                
            # Silkscreen Limits (from JLCPCB PDF)
            # Text Height >= 1.0mm, Thickness >= 0.153mm, Clearance >= 0.15mm
            if hasattr(settings, 'm_MinSilkTextHeight'): settings.m_MinSilkTextHeight = pcbnew.FromMM(1.0)
            if hasattr(settings, 'm_MinSilkTextThickness'): settings.m_MinSilkTextThickness = pcbnew.FromMM(0.153)
            if hasattr(settings, 'm_MinSilkClearance'): settings.m_MinSilkClearance = pcbnew.FromMM(0.15)
            
            # Hole to Hole Clearance (JLCPCB Via-to-Via is 0.2mm, but 0.25mm is a safer standard)
            if hasattr(settings, 'm_HoleToHoleMin'): settings.m_HoleToHoleMin = pcbnew.FromMM(0.25)
            
            # 2. Default Netclass (Bulletproof cross-version support)
            try:
                # Duck typing to support KiCad 6, 7, and 8+
                if hasattr(settings, 'GetNetClasses'):
                    netclasses = settings.GetNetClasses()
                elif hasattr(settings, 'm_NetClasses'):
                    netclasses = settings.m_NetClasses
                else:
                    netclasses = None

                if netclasses:
                    if hasattr(netclasses, 'GetDefault'):
                        default_nc = netclasses.GetDefault()
                    elif hasattr(netclasses, 'Find'):
                        default_nc = netclasses.Find("Default")
                    else:
                        default_nc = None

                    if default_nc:
                        default_nc.SetClearance(pcbnew.FromMM(min_clearance))
                        default_nc.SetTrackWidth(pcbnew.FromMM(min_track))
                        default_nc.SetViaDiameter(pcbnew.FromMM(min_via_pad))
                        default_nc.SetViaDrill(pcbnew.FromMM(min_via_drill))
            except Exception as nc_err:
                print(f"Warning: Could not update NetClasses dynamically: {nc_err}")
            
            pcbnew.Refresh()
            wx.MessageBox("Constraints applied successfully. \n\nCheck File -> Board Setup to verify.", "Success", wx.ICON_INFORMATION)
            
    except Exception as e:
        wx.MessageBox(f"Failed to apply constraints:\n{str(e)}", "Error", wx.ICON_ERROR)