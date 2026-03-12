import os
import subprocess
import tempfile
import sys
import shutil
import difflib
import re

# Fix for Windows: prevents the plugin from popping up CMD windows or hanging
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

class DiffEngine:
    def __init__(self, project_dir):
        self.project_dir = project_dir
        
        # Create a dedicated temp folder for this diff session
        self.tmp_dir = os.path.join(tempfile.gettempdir(), "kicad_git_diff")
        os.makedirs(self.tmp_dir, exist_ok=True)
        
        self.kicad_cli = "kicad-cli.exe" if sys.platform == "win32" else "kicad-cli"
        self.git_cmd = "git.exe" if sys.platform == "win32" else "git"

    def get_git_status(self):
        """Returns a dict of {filename: status_code} for modified files"""
        status_dict = {}
        try:
            res = subprocess.run([self.git_cmd, "-C", self.project_dir, "status", "--porcelain"], 
                                 capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            for line in res.stdout.split('\n'):
                if len(line) > 3:
                    code = line[0:2].strip()
                    fname = line[3:].strip().strip('"')
                    status_dict[fname] = code
        except Exception:
            pass # Not a git repo or git error
        return status_dict

    def _get_pcb_layers(self, pcb_file):
        """Quickly parse the .kicad_pcb file to find active copper layers and technical layers."""
        layers = ["F.Cu", "B.Cu", "F.Silkscreen", "B.Silkscreen", "Edge.Cuts"]
        if not os.path.exists(pcb_file):
            return layers
            
        try:
            with open(pcb_file, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read(10000) # Only need the header
                # Find (layers ...) section and extract (0 "F.Cu" signal) etc.
                matches = re.findall(r'\(\d+\s+"([^"]+)"\s+signal\)', content)
                if matches:
                    # Replace default copper with actual found copper layers
                    layers = matches + ["F.Silkscreen", "B.Silkscreen", "Edge.Cuts"]
        except:
            pass
        return layers

    def _generate_text_diff(self, old_file, new_file):
        """Helper to generate a unified diff from two text files (like Netlists or BOMs)"""
        if not old_file or not new_file or not os.path.exists(old_file) or not os.path.exists(new_file):
            return ""
        try:
            with open(old_file, 'r', encoding='utf-8', errors='ignore') as f:
                old_lines = f.readlines()
            with open(new_file, 'r', encoding='utf-8', errors='ignore') as f:
                new_lines = f.readlines()
            
            diff = difflib.unified_diff(old_lines, new_lines, fromfile='Reference', tofile='Current', n=3)
            return "".join(list(diff))
        except Exception as e:
            return f"Error generating text diff: {e}"

    def render_all_diffs(self, show_unchanged=False, compare_target="HEAD"):
        """
        Scans for .kicad_pcb and .kicad_sch. Exports visual and logical files.
        """
        git_status = self.get_git_status()
        target_files = []
        
        for fname in os.listdir(self.project_dir):
            if fname.endswith('.kicad_pcb') or fname.endswith('.kicad_sch'):
                target_files.append(fname)
                
        diffs = []
        summary_lines = []
        
        for fname in target_files:
            file_path = os.path.join(self.project_dir, fname)
            status_code = git_status.get(fname)
            
            if status_code in ['M', 'AM']: status_text = "Modified"
            elif status_code in ['A', '??']: status_text = "New/Untracked"
            else: status_text = "Unchanged"
            
            if status_text == "Unchanged" and not show_unchanged:
                continue
                
            summary_lines.append(f"{fname}: {status_text}")
            safe_name = fname.replace('.', '_')
            is_pcb = fname.endswith('.kicad_pcb')
            
            # Temporary storage for reference board from Git
            old_board_tmp = os.path.join(self.tmp_dir, f"tmp_git_{fname}")
            
            # Export Layers Map
            layers_to_export = ["Default"] # For Schematics
            if is_pcb:
                layers_to_export = self._get_pcb_layers(file_path)
            
            visuals = {} # Map layer_name -> {curr: path, old: path}
            netlist_diff = ""
            bom_diff = ""

            try:
                # 1. Export Git Reference version first if it exists
                has_old = False
                with open(old_board_tmp, "wb") as f:
                    res = subprocess.run([self.git_cmd, "-C", self.project_dir, "show", f"{compare_target}:{fname}"],
                                         stdout=f, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
                if res.returncode == 0:
                    has_old = True

                # 2. Iterate through layers (or just 'Default' for SCH)
                for layer in layers_to_export:
                    ext = "svg" if is_pcb else "pdf"
                    layer_safe = layer.replace('.', '_')
                    
                    curr_out = os.path.join(self.tmp_dir, f"curr_{safe_name}_{layer_safe}.{ext}")
                    old_out = os.path.join(self.tmp_dir, f"old_{safe_name}_{layer_safe}.{ext}")
                    
                    # Command setup
                    cli_args = [self.kicad_cli, "pcb" if is_pcb else "sch", "export", ext]
                    if is_pcb:
                        # For specific layers, we often want Edge.Cuts visible too for context
                        active_layers = layer
                        if layer != "Edge.Cuts":
                            active_layers += ",Edge.Cuts"
                        cli_args.extend(["--layers", active_layers, "--exclude-drawing-sheet"])
                    
                    # Render Current
                    subprocess.run(cli_args + [file_path, "--output", curr_out], 
                                   capture_output=True, creationflags=CREATE_NO_WINDOW)
                    
                    # Render Old
                    if has_old:
                        subprocess.run(cli_args + [old_board_tmp, "--output", old_out], 
                                       capture_output=True, creationflags=CREATE_NO_WINDOW)
                    
                    visuals[layer] = {
                        "curr": curr_out if os.path.exists(curr_out) else None,
                        "old": old_out if os.path.exists(old_out) else None
                    }

                # 3. Handle Logical Diffs (Schematics only)
                if not is_pcb:
                    curr_net = os.path.join(self.tmp_dir, f"curr_{safe_name}.net")
                    curr_bom = os.path.join(self.tmp_dir, f"curr_{safe_name}.csv")
                    subprocess.run([self.kicad_cli, "sch", "export", "netlist", file_path, "--output", curr_net], capture_output=True, creationflags=CREATE_NO_WINDOW)
                    subprocess.run([self.kicad_cli, "sch", "export", "bom", file_path, "--output", curr_bom], capture_output=True, creationflags=CREATE_NO_WINDOW)
                    
                    if has_old:
                        old_net = os.path.join(self.tmp_dir, f"old_{safe_name}.net")
                        old_bom = os.path.join(self.tmp_dir, f"old_{safe_name}.csv")
                        subprocess.run([self.kicad_cli, "sch", "export", "netlist", old_board_tmp, "--output", old_net], capture_output=True, creationflags=CREATE_NO_WINDOW)
                        subprocess.run([self.kicad_cli, "sch", "export", "bom", old_board_tmp, "--output", old_bom], capture_output=True, creationflags=CREATE_NO_WINDOW)
                        
                        netlist_diff = self._generate_text_diff(old_net, curr_net)
                        bom_diff = self._generate_text_diff(old_bom, curr_bom)

                diffs.append({
                    "name": fname,
                    "status": status_text,
                    "visuals": visuals,
                    "netlist_diff": netlist_diff,
                    "bom_diff": bom_diff
                })
                
            except Exception as e:
                print(f"Error rendering {fname}: {e}")

        summary = "\n".join(summary_lines) if summary_lines else "No files found."
        return diffs, summary