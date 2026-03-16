import pcbnew
import os
import shutil
import tempfile

class JLCPCBExporter:
    def __init__(self, board):
        self.board = board
        # Standard layers JLCPCB cares about for up to 6-layer boards
        self.layers = [
            (pcbnew.F_Cu, 'F_Cu'),
            (pcbnew.B_Cu, 'B_Cu'),
            (pcbnew.F_SilkS, 'F_SilkS'),
            (pcbnew.B_SilkS, 'B_SilkS'),
            (pcbnew.F_Mask, 'F_Mask'),
            (pcbnew.B_Mask, 'B_Mask'),
            (pcbnew.Edge_Cuts, 'Edge_Cuts'),
            (pcbnew.In1_Cu, 'In1_Cu'),
            (pcbnew.In2_Cu, 'In2_Cu'),
            (pcbnew.In3_Cu, 'In3_Cu'),
            (pcbnew.In4_Cu, 'In4_Cu'),
            (pcbnew.In5_Cu, 'In5_Cu'),
            (pcbnew.In6_Cu, 'In6_Cu'),
        ]

    def generate_zip(self, output_directory, zip_filename="gerbers"):
        """
        Generates Gerbers and Drill files into a temporary directory, 
        zips them, and moves the zip to a 'production' folder inside the output_directory.
        """
        # Ensure the production directory exists
        production_dir = os.path.join(output_directory, "production")
        os.makedirs(production_dir, exist_ok=True)

        # 1. Create a safe temporary directory
        temp_dir = tempfile.mkdtemp()
        
        try:
            # Try to auto-fill zones before plotting (Highly recommended by JLCPCB)
            try:
                filler = pcbnew.ZONE_FILLER(self.board)
                filler.Fill(self.board.Zones())
            except Exception as e:
                print(f"Warning: Could not fill zones automatically: {e}")

            # 2. Generate Files
            self._generate_gerbers(temp_dir)
            self._generate_drills(temp_dir)
            
            # 3. Create the ZIP archive
            zip_base_path = os.path.join(production_dir, zip_filename)
            shutil.make_archive(zip_base_path, 'zip', temp_dir)
            
            return f"{zip_base_path}.zip"
            
        finally:
            # 4. Clean up the temporary directory regardless of success or failure
            shutil.rmtree(temp_dir)

    def _generate_gerbers(self, temp_dir):
        pctl = pcbnew.PLOT_CONTROLLER(self.board)
        popt = pctl.GetPlotOptions()
        
        # JLCPCB Specific settings
        popt.SetOutputDirectory(temp_dir)
        popt.SetPlotFrameRef(False)
        popt.SetSketchPadLineWidth(pcbnew.FromMM(0.1))
        popt.SetPlotReference(True)
        popt.SetPlotValue(True)
        
        if hasattr(popt, 'SetPlotInvisibleText'):
            popt.SetPlotInvisibleText(False)
        
        # Disable drill marks entirely (Handling API changes across KiCad versions)
        if hasattr(pcbnew, 'PCB_PLOT_PARAMS') and hasattr(pcbnew.PCB_PLOT_PARAMS, 'NO_DRILL_SHAPE'):
            popt.SetDrillMarksType(pcbnew.PCB_PLOT_PARAMS.NO_DRILL_SHAPE)
        elif hasattr(pcbnew, 'PLOT_DRILL_MARKS_NO_DRILL_SHAPE'):
            popt.SetDrillMarksType(pcbnew.PLOT_DRILL_MARKS_NO_DRILL_SHAPE)
        else:
            popt.SetDrillMarksType(0)
            
        popt.SetUseGerberProtelExtensions(True) # IMPORTANT for JLCPCB (.gtl, .gbl, etc)
        popt.SetCreateGerberJobFile(False)
        popt.SetSubtractMaskFromSilk(True)
        popt.SetUseAuxOrigin(False) # JLCPCB typically requires Absolute Origin

        for layer_info in self.layers:
            layer_id = layer_info[0]
            # Only plot if the layer is actually enabled on the board
            if self.board.IsLayerEnabled(layer_id):
                pctl.SetLayer(layer_id)
                pctl.OpenPlotfile(layer_info[1], pcbnew.PLOT_FORMAT_GERBER, layer_info[1])
                pctl.PlotLayer()
        
        pctl.ClosePlot()

    def _generate_drills(self, temp_dir):
        drlwriter = pcbnew.EXCELLON_WRITER(self.board)
        
        # JLCPCB Specific drill settings
        mirror = False
        header = True
        
        # KiCad 7+ uses VECTOR2I for coordinates, older versions use wxPoint
        if hasattr(pcbnew, 'VECTOR2I'):
            offset = pcbnew.VECTOR2I(0, 0)
        else:
            offset = pcbnew.wxPoint(0, 0)
            
        merge_npth = True # JLCPCB strongly recommends merging PTH and NPTH
        
        drlwriter.SetFormat(True, pcbnew.EXCELLON_WRITER.DECIMAL_FORMAT, 3, 3)
        
        # JLCPCB requires alternate drill mode for oval holes
        if hasattr(drlwriter, 'SetRouteModeForOvalHoles'):
            drlwriter.SetRouteModeForOvalHoles(True)
            
        drlwriter.SetOptions(mirror, header, offset, merge_npth)
        
        # Generate the drill files (Map is not strictly required but harmless)
        drlwriter.CreateDrillandMapFilesSet(temp_dir, True, False)