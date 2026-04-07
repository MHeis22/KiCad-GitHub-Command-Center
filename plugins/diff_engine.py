import os
import subprocess
import tempfile
import sys
import shutil
import difflib
import re
import glob
from concurrent.futures import ThreadPoolExecutor, as_completed
from .utils import CREATE_NO_WINDOW
from .kicad_parser import (
    get_pcb_layers, get_pcb_dimensions, get_pcb_structure,
    get_sch_structure, get_bom_data, compare_logic_data, extract_todos
)

class DiffEngine:
    def __init__(self, project_dir):
        self.project_dir = project_dir
        
        # Create a dedicated temp folder for this diff session
        self.tmp_dir = os.path.join(tempfile.gettempdir(), "kicad_git_diff")
        os.makedirs(self.tmp_dir, exist_ok=True)
        
        self.kicad_cli = "kicad-cli.exe" if sys.platform == "win32" else "kicad-cli"
        self.git_cmd = "git.exe" if sys.platform == "win32" else "git"

    def get_kicad_version(self):
        """Fetches the installed KiCad version via CLI."""
        try:
            res = subprocess.run([self.kicad_cli, "--version"], capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            version_str = res.stdout.strip()
            if version_str:
                return version_str
        except Exception:
            pass
        return "Unknown KiCad Version"

    def get_git_status(self, target="HEAD"):
        """Returns a dict of {filename: status_code} for files that differ between target and working tree"""
        status_dict = {}
        try:
            # 1. Compare working tree to the specific target commit/branch (will fail if HEAD missing on new repo)
            res = subprocess.run([self.git_cmd, "-C", self.project_dir, "diff", target, "--name-status"], 
                                 capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            for line in res.stdout.split('\n'):
                if line.strip():
                    parts = line.split('\t')
                    if len(parts) >= 2:
                        code = parts[0].strip()
                        fname = parts[1].strip().strip('"')
                        status_dict[fname] = code

            # 2. Catch untracked files and staged files that might be missed if HEAD doesn't exist
            res_untracked = subprocess.run([self.git_cmd, "-C", self.project_dir, "status", "--porcelain"], 
                                 capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            for line in res_untracked.stdout.split('\n'):
                if len(line) > 2:
                    code = line[:2].strip()
                    fname = line[3:].strip().strip('"')
                    if fname not in status_dict:
                        status_dict[fname] = code
        except Exception:
            pass
        return status_dict

    def get_git_targets(self):
        """Returns a list of local branches and recent commits for comparison."""
        targets = ["HEAD"]
        try:
            # 1. Fetch Local Branches
            res = subprocess.run([self.git_cmd, "-C", self.project_dir, "branch", "--format=%(refname:short)"], 
                                 capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            for line in res.stdout.split('\n'):
                target = line.strip()
                if target and target not in targets:
                    targets.append(target)
            
            # 2. Fetch Recent Commits
            res = subprocess.run([self.git_cmd, "-C", self.project_dir, "log", "-n", "15", "--format=%h (%s)"], 
                                 capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            for line in res.stdout.split('\n'):
                target = line.strip()
                if target:
                    # Truncate to 55 characters and add "..." if it was longer
                    if len(target) > 55:
                        target = target[:52] + "..."
                    targets.append(target)
        except:
            pass
        return targets

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
        """Runs DRC, parses JSON to extract clean rule violations."""
        if not file_path or not os.path.exists(file_path):
            return []
        
        safe_name = os.path.basename(file_path).replace(' ', '_')
        out_json = os.path.join(self.tmp_dir, f"report_{safe_name}.json")
        
        if os.path.exists(out_json):
            try: os.remove(out_json)
            except: pass
            
        try:
            cmd = [self.kicad_cli, "pcb" if is_pcb else "sch", "drc" if is_pcb else "erc", "--format", "json", "--output", out_json, file_path]
            subprocess.run(cmd, capture_output=True, cwd=self.project_dir, creationflags=CREATE_NO_WINDOW)
            
            if os.path.exists(out_json):
                with open(out_json, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read().strip()
                
                if content.startswith('{'):
                    import json
                    data = json.loads(content)
                    violations = []
                    
                    for v in data.get("violations", []):
                        severity = v.get("severity", "warning")
                        desc = v.get("description", "Unknown violation")
                        items = v.get("items", [])
                        if items:
                            items_str = self._format_violation_items(items)
                            desc = f"{desc}: {items_str}"
                        violations.append(f"[{severity.upper()}] {desc}")
                        
                    for u in data.get("unconnected_items", []):
                        severity = u.get("severity", "error") 
                        desc = u.get("description", "Unconnected item")
                        items = u.get("items", [])
                        if items:
                            items_str = self._format_violation_items(items)
                            desc = f"{desc}: {items_str}"
                        violations.append(f"[UNCONNECTED] {desc}")
                        
                    return violations
                else:
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

    def _run_cmd_task(self, task):
        """Worker function that executes CLI tasks concurrently"""
        try:
            if task['type'] == 'svg':
                out_path = task['out_path']
                
                # Cleanup existing tmp outputs to prevent overlaps
                for old_temp in glob.glob(out_path.replace(".svg", "*.svg")):
                    try:
                        if os.path.isdir(old_temp): shutil.rmtree(old_temp)
                        else: os.remove(old_temp)
                    except: pass

                subprocess.run(task['cli_args'] + [task['file_path'], "--output", out_path], 
                               capture_output=True, cwd=self.project_dir, creationflags=CREATE_NO_WINDOW)
                
                if not task['is_pcb']:
                    out_path = self._find_correct_svg(out_path, task['base_name'])

                if os.path.exists(out_path) and os.path.getsize(out_path) > 0 and not os.path.isdir(out_path):
                    task['result'] = out_path
                else:
                    task['result'] = None
                    
            elif task['type'] == 'netlist':
                subprocess.run(task['cli_args'], capture_output=True, cwd=self.project_dir, creationflags=CREATE_NO_WINDOW)
                task['result'] = task['out_path']
                
            elif task['type'] == 'drc':
                task['result'] = self._run_rule_check(task['file_path'], task['is_pcb'])
                
        except Exception as e:
            print(f"Task failed: {e}")
            task['result'] = None
        
        return task

    def render_all_diffs(self, show_unchanged=False, compare_target="HEAD", run_drc=False):
        """
        Scans for .kicad_pcb and .kicad_sch. Exports visual, logical files, and optionally DRC.
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
                
        target_files = sorted(list(all_potential), key=lambda x: (0 if x.endswith('.kicad_pcb') else 1, x))
        
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
                layers_to_export = get_pcb_layers(file_path)
            
            visuals = {layer: {"curr": None, "old": None} for layer in layers_to_export}
            netlist_diff = ""
            bom_data = {"curr": {}, "old": {}}
            pcb_logic_diff = ""
            health_data = {"new": [], "resolved": [], "unresolved": []}
            dims_data = {"curr": None, "old": None}

            try:
                # 1. Export Git Reference version to disk (Synchronous, extremely fast)
                has_old = False
                if status_code != 'A' and status_code != '??':
                    with open(old_board_tmp, "wb") as f:
                        res = subprocess.run([self.git_cmd, "-C", self.project_dir, "show", f"{actual_target}:{fname}"],
                                             stdout=f, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
                    if res.returncode == 0:
                        has_old = True
                        if pro_path:
                            shutil.copy2(pro_path, old_pro_tmp)

                # ==============================================================
                # 2. Setup Parallel Tasks
                # ==============================================================
                tasks = []

                # SVG Export Tasks
                for layer in layers_to_export:
                    ext = "svg"
                    layer_safe = layer.replace('.', '_')
                    
                    curr_out = os.path.join(self.tmp_dir, f"curr_{safe_name}_{layer_safe}.{ext}")
                    old_out = os.path.join(self.tmp_dir, f"old_{safe_name}_{layer_safe}.{ext}")
                    
                    cli_args = [self.kicad_cli, "pcb" if is_pcb else "sch", "export", ext]
                    if is_pcb:
                        active_layers = layer
                        if layer != "Edge.Cuts":
                            active_layers += ",Edge.Cuts"
                        cli_args.extend(["--layers", active_layers, "--exclude-drawing-sheet"])
                    else:
                        cli_args.extend(["--exclude-drawing-sheet"])
                    
                    if status_text != "Deleted":
                        tasks.append({'type': 'svg', 'layer': layer, 'version': 'curr', 'cli_args': cli_args, 'file_path': file_path, 'out_path': curr_out, 'is_pcb': is_pcb, 'base_name': base_name})

                    if has_old:
                        tasks.append({'type': 'svg', 'layer': layer, 'version': 'old', 'cli_args': cli_args, 'file_path': old_board_tmp, 'out_path': old_out, 'is_pcb': is_pcb, 'base_name': old_base_name})

                # Netlist Tasks
                curr_net = None
                old_net = None
                if not is_pcb:
                    if status_text != "Deleted":
                        curr_net = os.path.join(self.tmp_dir, f"curr_{safe_name}.net")
                        tasks.append({'type': 'netlist', 'version': 'curr', 'cli_args': [self.kicad_cli, "sch", "export", "netlist", file_path, "--output", curr_net], 'out_path': curr_net})
                    if has_old:
                        old_net = os.path.join(self.tmp_dir, f"old_{safe_name}.net")
                        tasks.append({'type': 'netlist', 'version': 'old', 'cli_args': [self.kicad_cli, "sch", "export", "netlist", old_board_tmp, "--output", old_net], 'out_path': old_net})

                # DRC Tasks
                if run_drc:
                    if status_text != "Deleted":
                        tasks.append({'type': 'drc', 'version': 'curr', 'file_path': file_path, 'is_pcb': is_pcb})
                    if has_old:
                        tasks.append({'type': 'drc', 'version': 'old', 'file_path': old_board_tmp, 'is_pcb': is_pcb})

                # ==============================================================
                # 3. Execute all Heavy CLI tasks simultaneously via ThreadPool
                # ==============================================================
                curr_health, old_health = [], []
                
                # Cap threads to prevent RAM spikes on large boards (max 12 threads = ~1.5GB RAM usage)
                max_workers = min(12, (os.cpu_count() or 4))
                
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = [executor.submit(self._run_cmd_task, t) for t in tasks]
                    for future in as_completed(futures):
                        res = future.result()
                        if not res: continue
                        
                        if res['type'] == 'svg':
                            visuals[res['layer']][res['version']] = res['result']
                        elif res['type'] == 'drc':
                            if res['version'] == 'curr':
                                curr_health = res['result'] or []
                            else:
                                old_health = res['result'] or []

                # ==============================================================
                # 4. Handle Logical Extraction
                # ==============================================================
                if is_pcb:
                    dims_data["curr"] = get_pcb_dimensions(file_path)

                if has_old:
                    if is_pcb:
                        old_comp = get_pcb_structure(old_board_tmp)
                        curr_comp = get_pcb_structure(file_path)
                        pcb_logic_diff = compare_logic_data(old_comp, curr_comp)
                        dims_data["old"] = get_pcb_dimensions(old_board_tmp)
                    else:
                        old_comp = get_sch_structure(old_board_tmp)
                        curr_comp = get_sch_structure(file_path)
                        pcb_logic_diff = compare_logic_data(old_comp, curr_comp)

                if not is_pcb:
                    if status_text != "Deleted":
                        bom_data["curr"] = get_bom_data(file_path)
                    
                    if has_old:
                        bom_data["old"] = get_bom_data(old_board_tmp)
                        if curr_net and old_net:
                            netlist_diff = self._generate_text_diff(old_net, curr_net)

                # Extract TODOs
                curr_todos = extract_todos(file_path) if status_text != "Deleted" else []
                old_todos = extract_todos(old_board_tmp) if has_old else []
                
                # Format DRC Diffs
                if run_drc:
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
                    "bom_data": bom_data,
                    "pcb_logic_diff": pcb_logic_diff,
                    "todos": {
                        "curr": curr_todos,
                        "old": old_todos
                    },
                    "health": health_data,
                    "dimensions": dims_data
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