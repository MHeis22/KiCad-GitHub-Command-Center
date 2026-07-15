import os
import subprocess
import tempfile
import sys
import shutil
import difflib
import re
import glob
import time
import hashlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from .utils import CREATE_NO_WINDOW, find_kicad_cli
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

        # Old SVG/HTML artifacts from previous sessions pile up here forever
        # (a multilayer board can leave hundreds of MB), so sweep stale ones now.
        self._prune_temp_dir()

        self.kicad_cli = find_kicad_cli()
        self.git_cmd = "git.exe" if os.name == "nt" else "git"
        # {(file_md5, layer, is_pcb): svg_out_path} — avoids re-exporting unchanged files
        self._svg_cache = {}
        # {(abspath, mtime_ns, size, target_sha): bool} — memoizes the expensive
        # git-show + line-sort in file_content_changed(). The target_sha in the
        # key means a commit/checkout (which moves the ref) auto-invalidates it,
        # and the mtime/size means a re-saved working file does too.
        self._content_cache = {}

    def _prune_temp_dir(self, max_age_days=7):
        """Deletes files in the diff temp folder older than max_age_days so the
        cache of exported SVGs doesn't grow without bound across sessions."""
        try:
            cutoff = time.time() - max_age_days * 86400
            for name in os.listdir(self.tmp_dir):
                path = os.path.join(self.tmp_dir, name)
                try:
                    if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                        os.remove(path)
                except OSError:
                    pass
        except OSError:
            pass

    def get_kicad_version(self):
        """Fetches the installed KiCad version via CLI."""
        try:
            res = subprocess.run([self.kicad_cli, "--version"], capture_output=True, text=True, timeout=10, creationflags=CREATE_NO_WINDOW)
            version_str = res.stdout.strip()
            if version_str:
                return version_str
        except Exception:
            pass
        return "Unknown KiCad Version"

    def _normalized_multiset(self, text):
        """Order- and indentation-independent representation of a KiCad file.

        KiCad re-serializes elements (track segments, vias, footprints...) in a
        different order on nearly every save, producing large diffs with zero
        real design change. Stripping each line and sorting collapses that
        reordering/re-indentation noise while still reflecting any genuine
        content change (added/removed/edited lines alter the multiset).

        A Counter (line -> count) is an order-independent multiset compared in
        O(n); it replaces an earlier sort, which was O(n log n) and allocated a
        full list — a real cost on multi-megabyte boards checked repeatedly."""
        return Counter(line.strip() for line in text.splitlines() if line.strip())

    def _resolve_target_sha(self, target):
        """Resolves a target (HEAD, branch, commit-ish) to a commit SHA, or None.
        Cheap (git rev-parse) relative to the git-show + sort it helps cache."""
        try:
            res = subprocess.run(
                [self.git_cmd, "-C", self.project_dir, "rev-parse", target],
                capture_output=True, text=True, timeout=10, creationflags=CREATE_NO_WINDOW)
            if res.returncode == 0:
                return res.stdout.strip()
        except Exception:
            pass
        return None

    def file_content_changed(self, file_path, target="HEAD"):
        """Returns True if `file_path` differs semantically from `target`,
        ignoring pure element re-ordering noise.

        Returns True (treat as changed) when there is no committed version to
        compare against, or on any error — the safe direction is to regenerate.

        Memoized on (path, mtime, size, target SHA): the git-show + line-sort is
        skipped when the same working file is re-checked against the same commit,
        which happens repeatedly per commit and on every status refresh."""
        if not os.path.exists(file_path):
            return False

        # Build a cache key from the working file's stat + the resolved target.
        cache_key = None
        try:
            st = os.stat(file_path)
            target_sha = self._resolve_target_sha(target)
            if target_sha:
                cache_key = (os.path.abspath(file_path), st.st_mtime_ns, st.st_size, target_sha)
                if cache_key in self._content_cache:
                    return self._content_cache[cache_key]
        except OSError:
            cache_key = None

        # git show needs the repo-relative path (forward slashes), which also
        # works for files in subfolders.
        rel = os.path.relpath(file_path, self.project_dir).replace(os.sep, '/')
        try:
            res = subprocess.run(
                [self.git_cmd, "-C", self.project_dir, "show", f"{target}:{rel}"],
                capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
            if res.returncode != 0:
                # No committed version at this target -> it's new/changed.
                if cache_key is not None:
                    self._content_cache[cache_key] = True
                return True

            old_text = res.stdout.decode('utf-8', errors='ignore')
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                new_text = f.read()

            result = self._normalized_multiset(old_text) != self._normalized_multiset(new_text)
            if cache_key is not None:
                self._content_cache[cache_key] = result
            return result
        except Exception as e:
            print(f"file_content_changed check failed for {rel}: {e}")
            return True

    # KiCad S-expression files that get re-serialized (reordered) on every save.
    _SEXPR_EXTS = ('.kicad_pcb', '.kicad_sch')

    def filter_reorder_noise(self, status_dict, target="HEAD"):
        """Returns a copy of status_dict with modified KiCad S-expr files removed
        when their only change vs `target` is element re-ordering (save noise).

        New/deleted/renamed files are always kept; only in-place modifications
        (M/T) of .kicad_pcb/.kicad_sch are candidates for noise filtering."""
        filtered = {}
        for fname, code in status_dict.items():
            if code in ('M', 'T') and fname.lower().endswith(self._SEXPR_EXTS):
                full = os.path.join(self.project_dir, fname)
                if not self.file_content_changed(full, target=target):
                    continue  # cosmetic re-serialization only -> not a real change
            filtered[fname] = code
        return filtered

    def get_git_status(self, target="HEAD"):
        """Returns a dict of {filename: status_code} for files that differ between target and working tree"""
        status_dict = {}
        try:
            # 1. Compare working tree to the specific target commit/branch (will fail if HEAD missing on new repo)
            res = subprocess.run([self.git_cmd, "-C", self.project_dir, "diff", target, "--name-status"],
                                 capture_output=True, text=True, timeout=30, creationflags=CREATE_NO_WINDOW)
            for line in res.stdout.split('\n'):
                if line.strip():
                    parts = line.split('\t')
                    if len(parts) >= 2:
                        code = parts[0].strip()
                        # Renames/copies are "R100\told\tnew" (or C...): the file
                        # that exists in the working tree is the LAST field. Using
                        # parts[1] (the old path) meant the new file was never
                        # tracked or staged. Record the new path, and for a rename
                        # also record the old path's deletion so committing the
                        # pair actually completes the move (git re-detects it).
                        if code and code[0] in ('R', 'C') and len(parts) >= 3:
                            new_path = parts[2].strip().strip('"')
                            status_dict[new_path] = code[0]
                            if code[0] == 'R':
                                old_path = parts[1].strip().strip('"')
                                status_dict.setdefault(old_path, 'D')
                        else:
                            fname = parts[1].strip().strip('"')
                            status_dict[fname] = code

            # 2. Catch untracked files and staged files that might be missed if HEAD doesn't exist
            res_untracked = subprocess.run([self.git_cmd, "-C", self.project_dir, "status", "--porcelain"],
                                 capture_output=True, text=True, timeout=30, creationflags=CREATE_NO_WINDOW)
            for line in res_untracked.stdout.split('\n'):
                if len(line) > 2:
                    code = line[:2].strip()
                    rest = line[3:].strip()
                    # Porcelain renames read "R  old -> new"; keep the new path
                    # (and the old path's deletion) so it commits as a real move.
                    if code and code[0] in ('R', 'C') and ' -> ' in rest:
                        old_part, new_part = rest.split(' -> ', 1)
                        new_path = new_part.strip().strip('"')
                        if new_path not in status_dict:
                            status_dict[new_path] = code[0]
                        if code[0] == 'R':
                            status_dict.setdefault(old_part.strip().strip('"'), 'D')
                    else:
                        fname = rest.strip().strip('"')
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
                                 capture_output=True, text=True, timeout=10, creationflags=CREATE_NO_WINDOW)
            for line in res.stdout.split('\n'):
                target = line.strip()
                if target and target not in targets:
                    targets.append(target)
            
            # 2. Fetch Recent Commits
            res = subprocess.run([self.git_cmd, "-C", self.project_dir, "log", "-n", "15", "--format=%h (%s)"],
                                 capture_output=True, text=True, timeout=10, creationflags=CREATE_NO_WINDOW)
            for line in res.stdout.split('\n'):
                target = line.strip()
                if target:
                    # Truncate to 55 characters and add "..." if it was longer
                    if len(target) > 55:
                        target = target[:52] + "..."
                    targets.append(target)
        except Exception:
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
            except OSError: pass

        try:
            cmd = [self.kicad_cli, "pcb", "drc", "--format", "json", "--output", out_json, file_path]
            subprocess.run(cmd, capture_output=True, timeout=120, cwd=self.project_dir, creationflags=CREATE_NO_WINDOW)
            
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

    def _file_hash(self, path):
        h = hashlib.md5()
        try:
            with open(path, 'rb') as f:
                for chunk in iter(lambda: f.read(65536), b''):
                    h.update(chunk)
        except OSError:
            return None
        return h.hexdigest()

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

                # Cache check: skip CLI export if this exact file+layer was already rendered
                cache_key = None
                file_path = task['file_path']
                if os.path.exists(file_path):
                    fhash = self._file_hash(file_path)
                    if fhash:
                        cache_key = (fhash, task['layer'], task['is_pcb'])
                        cached = self._svg_cache.get(cache_key)
                        if cached and os.path.exists(cached):
                            task['result'] = cached
                            return task

                # Cleanup existing tmp outputs to prevent overlaps
                for old_temp in glob.glob(out_path.replace(".svg", "*.svg")):
                    try:
                        if os.path.isdir(old_temp): shutil.rmtree(old_temp)
                        else: os.remove(old_temp)
                    except OSError: pass

                subprocess.run(task['cli_args'] + [task['file_path'], "--output", out_path],
                               capture_output=True, timeout=120, cwd=self.project_dir, creationflags=CREATE_NO_WINDOW)

                if not task['is_pcb']:
                    out_path = self._find_correct_svg(out_path, task['base_name'])

                if os.path.exists(out_path) and os.path.getsize(out_path) > 0 and not os.path.isdir(out_path):
                    task['result'] = out_path
                    if cache_key:
                        self._svg_cache[cache_key] = out_path
                else:
                    task['result'] = None
                    
            elif task['type'] == 'netlist':
                subprocess.run(task['cli_args'], capture_output=True, timeout=60, cwd=self.project_dir, creationflags=CREATE_NO_WINDOW)
                task['result'] = task['out_path']
                
            elif task['type'] == 'drc':
                task['result'] = self._run_rule_check(task['file_path'], task['is_pcb'])
                
        except Exception as e:
            print(f"Task failed: {e}")
            task['result'] = None
        
        return task

    def render_all_diffs(self, show_unchanged=False, compare_target="HEAD", run_drc=False, progress_callback=None):
        """
        Scans for .kicad_pcb and .kicad_sch. Exports visual, logical files, and optionally DRC.
        progress_callback(current, total, label) reports task-level progress.

        Runs in three phases so that every file's heavy kicad-cli work shares ONE
        thread pool instead of each file getting its own sequential pool:
          A. per-file setup (export the git-reference copy, build the CLI task
             list) — cheap, sequential;
          B. run all CLI tasks (SVG/netlist/DRC) across all files concurrently;
          C. assemble each diff, doing the pcbnew-based extraction (LoadBoard is
             NOT thread-safe) sequentially on this thread.
        For a project with several sheets this cuts wall-clock roughly by the
        number of files, since previously the files ran one after another.
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

        summary_lines = []
        contexts = []          # per-file state carried from phase A to phase C
        all_tasks = []         # every CLI task across every file (one shared pool)

        # ---- Phase A: per-file setup + task assembly (sequential, cheap) ----
        for file_idx, fname in enumerate(target_files):
            file_path = os.path.join(self.project_dir, fname)
            status_code = git_status.get(fname)

            if status_code in ['M', 'T']: status_text = "Modified"
            elif status_code in ['A', '??']: status_text = "New/Untracked"
            elif status_code == 'D': status_text = "Deleted"
            else: status_text = "Unchanged"

            if status_text == "Unchanged" and not show_unchanged:
                continue

            summary_lines.append(f"{fname}: {status_text}")
            # fname can be a repo-relative path with subfolders (e.g.
            # "boards/main.kicad_pcb"). Flatten separators for tmp_dir output
            # names so they don't imply a nonexistent subdirectory, and place the
            # git-reference temp copy next to the real file so its sibling
            # .kicad_pro / library tables still resolve.
            is_pcb = fname.endswith('.kicad_pcb')
            file_dir = os.path.dirname(file_path)
            fname_only = os.path.basename(fname)
            base_only = os.path.splitext(fname_only)[0]
            safe_name = fname.replace('.', '_').replace('/', '_').replace('\\', '_')

            old_board_tmp = os.path.join(file_dir, f"tmp_git_old_{fname_only}")
            old_base_name = f"tmp_git_old_{base_only}"

            expected_pro = os.path.join(file_dir, f"{base_only}.kicad_pro")
            pro_path = expected_pro if os.path.exists(expected_pro) else None
            if not pro_path:
                pro_files = glob.glob(os.path.join(file_dir, "*.kicad_pro"))
                if pro_files: pro_path = pro_files[0]

            old_pro_tmp = None
            if pro_path:
                old_pro_tmp = os.path.join(file_dir, f"{old_base_name}.kicad_pro")

            layers_to_export = ["Default"]
            if is_pcb:
                layers_to_export = get_pcb_layers(file_path)

            visuals = {layer: {"curr": None, "old": None} for layer in layers_to_export}

            # Export the git-reference version of this file to disk (fast).
            has_old = False
            try:
                if status_code != 'A' and status_code != '??':
                    with open(old_board_tmp, "wb") as f:
                        res = subprocess.run([self.git_cmd, "-C", self.project_dir, "show", f"{actual_target}:{fname}"],
                                             stdout=f, stderr=subprocess.PIPE, timeout=30, creationflags=CREATE_NO_WINDOW)
                    if res.returncode == 0:
                        has_old = True
                        if pro_path:
                            shutil.copy2(pro_path, old_pro_tmp)
            except Exception as e:
                print(f"Error exporting reference for {fname}: {e}")

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
                    all_tasks.append({'type': 'svg', 'file_id': file_idx, 'fname': fname, 'layer': layer, 'version': 'curr', 'cli_args': cli_args, 'file_path': file_path, 'out_path': curr_out, 'is_pcb': is_pcb, 'base_name': base_only})

                if has_old:
                    all_tasks.append({'type': 'svg', 'file_id': file_idx, 'fname': fname, 'layer': layer, 'version': 'old', 'cli_args': cli_args, 'file_path': old_board_tmp, 'out_path': old_out, 'is_pcb': is_pcb, 'base_name': old_base_name})

            # Netlist Tasks (schematic only)
            curr_net = None
            old_net = None
            if not is_pcb:
                if status_text != "Deleted":
                    curr_net = os.path.join(self.tmp_dir, f"curr_{safe_name}.net")
                    all_tasks.append({'type': 'netlist', 'file_id': file_idx, 'fname': fname, 'version': 'curr', 'cli_args': [self.kicad_cli, "sch", "export", "netlist", file_path, "--output", curr_net], 'out_path': curr_net})
                if has_old:
                    old_net = os.path.join(self.tmp_dir, f"old_{safe_name}.net")
                    all_tasks.append({'type': 'netlist', 'file_id': file_idx, 'fname': fname, 'version': 'old', 'cli_args': [self.kicad_cli, "sch", "export", "netlist", old_board_tmp, "--output", old_net], 'out_path': old_net})

            # DRC Tasks (PCB only)
            if run_drc and is_pcb:
                if status_text != "Deleted":
                    all_tasks.append({'type': 'drc', 'file_id': file_idx, 'fname': fname, 'version': 'curr', 'file_path': file_path, 'is_pcb': is_pcb})
                if has_old:
                    all_tasks.append({'type': 'drc', 'file_id': file_idx, 'fname': fname, 'version': 'old', 'file_path': old_board_tmp, 'is_pcb': is_pcb})

            contexts.append({
                'file_id': file_idx, 'fname': fname, 'status_text': status_text,
                'is_pcb': is_pcb, 'file_path': file_path,
                'old_board_tmp': old_board_tmp, 'old_pro_tmp': old_pro_tmp, 'has_old': has_old,
                'visuals': visuals, 'curr_net': curr_net, 'old_net': old_net,
                'curr_health': [], 'old_health': [],
            })

        # ---- Phase B: run every CLI task across all files in one shared pool ----
        contexts_by_id = {c['file_id']: c for c in contexts}
        total_tasks = len(all_tasks)
        if total_tasks:
            # Cap threads to prevent RAM spikes on large boards (max 12 threads = ~1.5GB RAM usage)
            max_workers = min(12, (os.cpu_count() or 4))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(self._run_cmd_task, t) for t in all_tasks]
                done = 0
                for future in as_completed(futures):
                    res = future.result()
                    done += 1
                    if progress_callback:
                        progress_callback(done, total_tasks, res.get('fname', '') if res else '')
                    if not res:
                        continue
                    ctx = contexts_by_id.get(res.get('file_id'))
                    if ctx is None:
                        continue
                    if res['type'] == 'svg':
                        if res['layer'] in ctx['visuals']:
                            ctx['visuals'][res['layer']][res['version']] = res['result']
                    elif res['type'] == 'drc':
                        if res['version'] == 'curr':
                            ctx['curr_health'] = res['result'] or []
                        else:
                            ctx['old_health'] = res['result'] or []

        # ---- Phase C: logical extraction + assembly (sequential; pcbnew) ----
        diffs = []
        for ctx in contexts:
            fname = ctx['fname']
            is_pcb = ctx['is_pcb']
            has_old = ctx['has_old']
            file_path = ctx['file_path']
            old_board_tmp = ctx['old_board_tmp']
            status_text = ctx['status_text']

            netlist_diff = ""
            bom_data = {"curr": {}, "old": {}}
            pcb_logic_diff = ""
            health_data = {"new": [], "resolved": [], "unresolved": []}
            dims_data = {"curr": None, "old": None}

            try:
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
                        if ctx['curr_net'] and ctx['old_net']:
                            netlist_diff = self._generate_text_diff(ctx['old_net'], ctx['curr_net'])

                # Extract TODOs
                curr_todos = extract_todos(file_path) if status_text != "Deleted" else []
                old_todos = extract_todos(old_board_tmp) if has_old else []

                # Format DRC Diffs
                if run_drc:
                    old_set = set(ctx['old_health'])
                    curr_set = set(ctx['curr_health'])
                    health_data = {
                        "resolved": sorted(list(old_set - curr_set)),
                        "new": sorted(list(curr_set - old_set)),
                        "unresolved": sorted(list(old_set & curr_set))
                    }

                diffs.append({
                    "name": fname,
                    "status": status_text,
                    "visuals": ctx['visuals'],
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
                print(f"Error assembling diff for {fname}: {e}")
            finally:
                if os.path.exists(old_board_tmp):
                    try: os.remove(old_board_tmp)
                    except OSError: pass
                if ctx['old_pro_tmp'] and os.path.exists(ctx['old_pro_tmp']):
                    try: os.remove(ctx['old_pro_tmp'])
                    except OSError: pass

        summary = "\n".join(summary_lines) if summary_lines else "No files found."
        return diffs, summary