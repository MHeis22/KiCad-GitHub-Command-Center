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

    def get_git_status(self, target="HEAD"):
        """Returns a dict of {filename: status_code} for files that differ between target and working tree"""
        status_dict = {}
        try:
            # 1. Compare working tree to the specific target commit/branch
            res = subprocess.run([self.git_cmd, "-C", self.project_dir, "diff", target, "--name-status"], 
                                 capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            for line in res.stdout.split('\n'):
                if line.strip():
                    parts = line.split('\t')
                    if len(parts) >= 2:
                        code = parts[0].strip()
                        fname = parts[1].strip().strip('"')
                        status_dict[fname] = code

            # 2. Catch untracked files
            res_untracked = subprocess.run([self.git_cmd, "-C", self.project_dir, "status", "--porcelain"], 
                                 capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            for line in res_untracked.stdout.split('\n'):
                if line.startswith('??'):
                    fname = line[3:].strip().strip('"')
                    if fname not in status_dict:
                        status_dict[fname] = '??'
        except Exception:
            pass
        return status_dict

    def get_git_targets(self):
        """Returns a list of local branches and recent commits for comparison."""
        targets = ["HEAD"]
        try:
            res = subprocess.run([self.git_cmd, "-C", self.project_dir, "branch", "--format=%(refname:short)"], 
                                 capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            for line in res.stdout.split('\n'):
                target = line.strip()
                if target and target not in targets:
                    targets.append(target)
            
            res = subprocess.run([self.git_cmd, "-C", self.project_dir, "log", "-n", "10", "--format=%h (%s)"], 
                                 capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            for line in res.stdout.split('\n'):
                target = line.strip()
                if target:
                    targets.append(target)
        except:
            pass
        return targets

    def _get_pcb_layers(self, pcb_file):
        """Quickly parse the .kicad_pcb file to find active copper layers and technical layers."""
        layers = ["F.Cu", "B.Cu", "F.Silkscreen", "B.Silkscreen", "Edge.Cuts"]
        if not os.path.exists(pcb_file):
            return layers
            
        try:
            with open(pcb_file, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read(10000)
                matches = re.findall(r'\(\d+\s+"([^"]+)"\s+signal\)', content)
                if matches:
                    layers = matches + ["F.Silkscreen", "B.Silkscreen", "Edge.Cuts"]
        except:
            pass
        return layers

    def _generate_text_diff(self, old_file, new_file):
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
        if not file_path or not os.path.exists(file_path):
            return []
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            todos = re.findall(r'"([^"]*TODO[^"]*)"', content, re.IGNORECASE)
            
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

    def _format_violation_items(self, items):
        """Helper to extract clean descriptions from KiCad JSON item dictionaries."""
        formatted = []
        for i in items:
            if isinstance(i, dict) and "description" in i:
                formatted.append(str(i["description"]))
            else:
                formatted.append(str(i))
        return " - ".join(formatted)

    def _run_rule_check(self, file_path, is_pcb):
        """Runs DRC/ERC, parses JSON to extract clean rule violations."""
        if not file_path or not os.path.exists(file_path):
            return []
        
        safe_name = os.path.basename(file_path).replace(' ', '_')
        out_json = os.path.join(self.tmp_dir, f"report_{safe_name}.json")
        
        # Clean up previous run
        if os.path.exists(out_json):
            try: os.remove(out_json)
            except: pass
            
        try:
            cmd = [self.kicad_cli, "pcb" if is_pcb else "sch", "drc" if is_pcb else "erc", 
                   "--format", "json", "--output", out_json, file_path]
            
            subprocess.run(cmd, capture_output=True, cwd=self.project_dir, creationflags=CREATE_NO_WINDOW)
            
            if os.path.exists(out_json):
                with open(out_json, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read().strip()
                
                # Check if it successfully exported JSON
                if content.startswith('{'):
                    import json
                    data = json.loads(content)
                    violations = []
                    
                    # Process standard violations
                    for v in data.get("violations", []):
                        severity = v.get("severity", "warning")
                        desc = v.get("description", "Unknown violation")
                        items = v.get("items", [])
                        if items:
                            items_str = self._format_violation_items(items)
                            desc = f"{desc}: {items_str}"
                        violations.append(f"[{severity.upper()}] {desc}")
                        
                    # Process unrouted nets / unconnected items
                    for u in data.get("unconnected_items", []):
                        severity = u.get("severity", "error") # Unconnected nets are critical
                        desc = u.get("description", "Unconnected item")
                        items = u.get("items", [])
                        if items:
                            items_str = self._format_violation_items(items)
                            desc = f"{desc}: {items_str}"
                        violations.append(f"[UNCONNECTED] {desc}")
                        
                    return violations
                else:
                    # Fallback if KiCad generated text instead of JSON (older versions)
                    lines = [line.strip() for line in content.split('\n') if line.strip() and not line.startswith('**')]
                    return lines
        except Exception as e:
            return [f"Error running check: {e}"]
        return []

    def _find_correct_svg(self, out_path, expected_base_name):
        if os.path.isdir(out_path):
            expected = os.path.join(out_path, f"{expected_base_name}.svg")
            if os.path.exists(expected):
                return expected
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

    def render_all_diffs(self, show_unchanged=False, compare_target="HEAD", run_drc_erc=False):
        """
        Scans for .kicad_pcb and .kicad_sch. Exports visual, logical files, and optionally DRC/ERC.
        """
        actual_target = compare_target.split(' ')[0] if ' ' in compare_target else compare_target
        git_status = self.get_git_status(target=actual_target)
        
        all_potential = set()
        for fname in os.listdir(self.project_dir):
            if fname.endswith('.kicad_pcb') or fname.endswith('.kicad_sch'):
                all_potential.add(fname)
        for fname in git_status.keys():
            if fname.endswith('.kicad_pcb') or fname.endswith('.kicad_sch'):
                all_potential.add(fname)
                
        target_files = sorted(list(all_potential))
        diffs = []
        summary_lines = []
        
        for fname in target_files:
            file_path = os.path.join(self.project_dir, fname)
            status_code = git_status.get(fname)
            
            if status_code in ['M', 'T']: status_text = "Modified"
            elif status_code in ['A', '??']: status_text = "New/Untracked"
            elif status_code == 'D': status_text = "Deleted"
            else: status_text = "Unchanged"
            
            if status_text == "Unchanged" and not show_unchanged:
                continue
                
            summary_lines.append(f"{fname}: {status_text}")
            safe_name = fname.replace('.', '_')
            is_pcb = fname.endswith('.kicad_pcb')
            base_name = os.path.splitext(fname)[0]
            
            old_board_tmp = os.path.join(self.project_dir, f"tmp_git_old_{fname}")
            old_base_name = f"tmp_git_old_{base_name}"
            
            expected_pro = os.path.join(self.project_dir, f"{base_name}.kicad_pro")
            pro_path = expected_pro if os.path.exists(expected_pro) else None
            if not pro_path:
                pro_files = glob.glob(os.path.join(self.project_dir, "*.kicad_pro"))
                if pro_files: pro_path = pro_files[0]
                
            old_pro_tmp = None
            if pro_path:
                old_pro_tmp = os.path.join(self.project_dir, f"{old_base_name}.kicad_pro")
            
            layers_to_export = ["Default"]
            if is_pcb:
                layers_to_export = self._get_pcb_layers(file_path)
            
            visuals = {} 
            netlist_diff = ""
            bom_diff = ""
            health_data = {"new": [], "resolved": [], "unresolved": []}

            try:
                # 1. Export Git Reference version
                has_old = False
                if status_code != 'A' and status_code != '??':
                    with open(old_board_tmp, "wb") as f:
                        res = subprocess.run([self.git_cmd, "-C", self.project_dir, "show", f"{actual_target}:{fname}"],
                                             stdout=f, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
                    if res.returncode == 0:
                        has_old = True
                        if pro_path:
                            shutil.copy2(pro_path, old_pro_tmp)

                # 2. Iterate through layers
                for layer in layers_to_export:
                    ext = "svg"
                    layer_safe = layer.replace('.', '_')
                    
                    curr_out = os.path.join(self.tmp_dir, f"curr_{safe_name}_{layer_safe}.{ext}")
                    old_out = os.path.join(self.tmp_dir, f"old_{safe_name}_{layer_safe}.{ext}")
                    
                    for old_temp in glob.glob(curr_out.replace(".svg", "*.svg")) + glob.glob(old_out.replace(".svg", "*.svg")):
                        try:
                            if os.path.isdir(old_temp): shutil.rmtree(old_temp)
                            else: os.remove(old_temp)
                        except: pass

                    cli_args = [self.kicad_cli, "pcb" if is_pcb else "sch", "export", ext]
                    if is_pcb:
                        active_layers = layer
                        if layer != "Edge.Cuts":
                            active_layers += ",Edge.Cuts"
                        cli_args.extend(["--layers", active_layers, "--exclude-drawing-sheet"])
                    else:
                        cli_args.extend(["--exclude-drawing-sheet"])
                    
                    if status_text != "Deleted":
                        subprocess.run(cli_args + [file_path, "--output", curr_out], 
                                       capture_output=True, cwd=self.project_dir, creationflags=CREATE_NO_WINDOW)
                        if not is_pcb:
                            curr_out = self._find_correct_svg(curr_out, base_name)

                    if has_old:
                        subprocess.run(cli_args + [old_board_tmp, "--output", old_out], 
                                       capture_output=True, cwd=self.project_dir, creationflags=CREATE_NO_WINDOW)
                        if not is_pcb:
                            old_out = self._find_correct_svg(old_out, old_base_name)
                    
                    visuals[layer] = {
                        "curr": curr_out if os.path.exists(curr_out) and os.path.getsize(curr_out) > 0 and not os.path.isdir(curr_out) else None,
                        "old": old_out if os.path.exists(old_out) and os.path.getsize(old_out) > 0 and not os.path.isdir(old_out) else None
                    }

                # 3. Handle Logical Diffs
                if not is_pcb:
                    curr_net = os.path.join(self.tmp_dir, f"curr_{safe_name}.net")
                    curr_bom = os.path.join(self.tmp_dir, f"curr_{safe_name}.csv")
                    if status_text != "Deleted":
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
                curr_todos = self._extract_todos(file_path) if status_text != "Deleted" else []
                old_todos = self._extract_todos(old_board_tmp) if has_old else []
                
                # 5. Extract Health (DRC/ERC)
                if run_drc_erc:
                    curr_health = self._run_rule_check(file_path, is_pcb) if status_text != "Deleted" else []
                    old_health = self._run_rule_check(old_board_tmp, is_pcb) if has_old else []
                    
                    old_set = set(old_health)
                    curr_set = set(curr_health)
                    
                    health_data = {
                        "resolved": sorted(list(old_set - curr_set)),
                        "new": sorted(list(curr_set - old_set)),
                        "unresolved": sorted(list(old_set & curr_set))
                    }

                diffs.append({
                    "name": fname,
                    "status": status_text,
                    "visuals": visuals,
                    "netlist_diff": netlist_diff,
                    "bom_diff": bom_diff,
                    "todos": {
                        "curr": curr_todos,
                        "old": old_todos
                    },
                    "health": health_data
                })
                
            except Exception as e:
                print(f"Error rendering {fname}: {e}")
            finally:
                if os.path.exists(old_board_tmp):
                    try: os.remove(old_board_tmp)
                    except: pass
                if old_pro_tmp and os.path.exists(old_pro_tmp):
                    try: os.remove(old_pro_tmp)
                    except: pass

        summary = "\n".join(summary_lines) if summary_lines else "No files found."
        return diffs, summary