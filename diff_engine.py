import os
import subprocess
import tempfile
import sys
import shutil
import difflib
import re
import glob

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

    def _extract_todos(self, file_path):
        """Extract TODOs from KiCad schematic or PCB files."""
        if not file_path or not os.path.exists(file_path):
            return []
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            # Find anything inside double quotes containing "TODO" (case-insensitive)
            todos = re.findall(r'"([^"]*TODO[^"]*)"', content, re.IGNORECASE)
            
            # Clean up whitespace and remove duplicates while preserving order
            seen = set()
            result = []
            for t in todos:
                clean_t = t.strip()
                if clean_t and clean_t not in seen:
                    seen.add(clean_t)
                    result.append(clean_t)
            return result
        except Exception as e:
            return [f"Error extracting TODOs: {e}"]

    def _find_correct_svg(self, out_path, expected_base_name):
        """Helper to explicitly find the right SVG sheet, ignoring KiCad subsheets"""
        if os.path.isdir(out_path):
            expected = os.path.join(out_path, f"{expected_base_name}.svg")
            if os.path.exists(expected):
                return expected
            # Fallback
            svgs = glob.glob(os.path.join(out_path, "*.svg"))
            return svgs[0] if svgs else out_path
            
        if not os.path.exists(out_path):
            matches = glob.glob(out_path.replace(".svg", "*.svg"))
            for m in matches:
                if os.path.basename(m) == f"{expected_base_name}.svg":
                    return m
            if matches:
                return matches[0]
                
        return out_path

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
            base_name = os.path.splitext(fname)[0]
            
            # Temporary storage for reference board from Git.
            # MUST NOT start with a dot '.' otherwise glob ignores it.
            old_board_tmp = os.path.join(self.project_dir, f"tmp_git_old_{fname}")
            old_base_name = f"tmp_git_old_{base_name}"
            
            # Find the active project file to copy alongside the temp board 
            # so KiCad correctly initializes the environment and doesn't render blanks.
            expected_pro = os.path.join(self.project_dir, f"{base_name}.kicad_pro")
            pro_path = expected_pro if os.path.exists(expected_pro) else None
            if not pro_path:
                pro_files = glob.glob(os.path.join(self.project_dir, "*.kicad_pro"))
                if pro_files: pro_path = pro_files[0]
                
            old_pro_tmp = None
            if pro_path:
                old_pro_tmp = os.path.join(self.project_dir, f"{old_base_name}.kicad_pro")
            
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
                    if pro_path:
                        shutil.copy2(pro_path, old_pro_tmp)

                # 2. Iterate through layers (or just 'Default' for SCH)
                for layer in layers_to_export:
                    ext = "svg" # Always use SVG for both PCB and Schematic
                    layer_safe = layer.replace('.', '_')
                    
                    curr_out = os.path.join(self.tmp_dir, f"curr_{safe_name}_{layer_safe}.{ext}")
                    old_out = os.path.join(self.tmp_dir, f"old_{safe_name}_{layer_safe}.{ext}")
                    
                    # Pre-clean stale files/directories safely
                    for old_temp in glob.glob(curr_out.replace(".svg", "*.svg")) + glob.glob(old_out.replace(".svg", "*.svg")):
                        try:
                            if os.path.isdir(old_temp): shutil.rmtree(old_temp)
                            else: os.remove(old_temp)
                        except: pass

                    # Command setup
                    cli_args = [self.kicad_cli, "pcb" if is_pcb else "sch", "export", ext]
                    if is_pcb:
                        active_layers = layer
                        if layer != "Edge.Cuts":
                            active_layers += ",Edge.Cuts"
                        cli_args.extend(["--layers", active_layers, "--exclude-drawing-sheet"])
                    else:
                        # Schematics: DO NOT use --black-and-white (it crashes KiCad for SVG exports)
                        cli_args.extend(["--exclude-drawing-sheet"])
                    
                    # Render Current
                    res_curr = subprocess.run(cli_args + [file_path, "--output", curr_out], 
                                   capture_output=True, cwd=self.project_dir, creationflags=CREATE_NO_WINDOW)
                    
                    if res_curr.returncode != 0:
                        print(f"Error rendering current {fname}: {res_curr.stderr.decode('utf-8', errors='ignore')}")

                    # Fix for KiCad Schematic SVGs: Find the explicit correct page.
                    if not is_pcb:
                        curr_out = self._find_correct_svg(curr_out, base_name)

                    # Render Old
                    if has_old:
                        res_old = subprocess.run(cli_args + [old_board_tmp, "--output", old_out], 
                                       capture_output=True, cwd=self.project_dir, creationflags=CREATE_NO_WINDOW)
                        
                        if res_old.returncode != 0:
                            print(f"Error rendering old {fname}: {res_old.stderr.decode('utf-8', errors='ignore')}")

                        if not is_pcb:
                            old_out = self._find_correct_svg(old_out, old_base_name)
                    
                    visuals[layer] = {
                        "curr": curr_out if os.path.exists(curr_out) and os.path.getsize(curr_out) > 0 and not os.path.isdir(curr_out) else None,
                        "old": old_out if os.path.exists(old_out) and os.path.getsize(old_out) > 0 and not os.path.isdir(old_out) else None
                    }

                # 3. Handle Logical Diffs (Schematics only)
                if not is_pcb:
                    curr_net = os.path.join(self.tmp_dir, f"curr_{safe_name}.net")
                    curr_bom = os.path.join(self.tmp_dir, f"curr_{safe_name}.csv")
                    subprocess.run([self.kicad_cli, "sch", "export", "netlist", file_path, "--output", curr_net], capture_output=True, cwd=self.project_dir, creationflags=CREATE_NO_WINDOW)
                    subprocess.run([self.kicad_cli, "sch", "export", "bom", file_path, "--output", curr_bom], capture_output=True, cwd=self.project_dir, creationflags=CREATE_NO_WINDOW)
                    
                    if has_old:
                        old_net = os.path.join(self.tmp_dir, f"old_{safe_name}.net")
                        old_bom = os.path.join(self.tmp_dir, f"old_{safe_name}.csv")
                        subprocess.run([self.kicad_cli, "sch", "export", "netlist", old_board_tmp, "--output", old_net], capture_output=True, cwd=self.project_dir, creationflags=CREATE_NO_WINDOW)
                        subprocess.run([self.kicad_cli, "sch", "export", "bom", old_board_tmp, "--output", old_bom], capture_output=True, cwd=self.project_dir, creationflags=CREATE_NO_WINDOW)
                        
                        netlist_diff = self._generate_text_diff(old_net, curr_net)
                        bom_diff = self._generate_text_diff(old_bom, curr_bom)

                # 4. Extract TODOs
                curr_todos = self._extract_todos(file_path)
                old_todos = []
                if has_old:
                    old_todos = self._extract_todos(old_board_tmp)

                diffs.append({
                    "name": fname,
                    "status": status_text,
                    "visuals": visuals,
                    "netlist_diff": netlist_diff,
                    "bom_diff": bom_diff,
                    "todos": {
                        "curr": curr_todos,
                        "old": old_todos
                    }
                })
                
            except Exception as e:
                print(f"Error rendering {fname}: {e}")
            finally:
                # Always clean up our temporary git files so we don't pollute the project dir
                if os.path.exists(old_board_tmp):
                    try: os.remove(old_board_tmp)
                    except: pass
                if old_pro_tmp and os.path.exists(old_pro_tmp):
                    try: os.remove(old_pro_tmp)
                    except: pass

        summary = "\n".join(summary_lines) if summary_lines else "No files found."
        return diffs, summary