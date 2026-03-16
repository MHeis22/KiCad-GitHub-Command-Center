import os
import re
import glob
import urllib.parse
import sys
import subprocess
import json
import tempfile
from .kicad_parser import get_pcb_dimensions, get_pcb_layers, get_bom_data, extract_todos, get_pcb_structure

class ReadmeGenerator:
    def __init__(self, project_dir, settings=None):
        self.project_dir = project_dir
        self.settings = settings or {}

    def _find_readme(self):
        """Case-insensitive search for README.md to prevent duplicating files."""
        for f in os.listdir(self.project_dir):
            if f.lower() == 'readme.md':
                return os.path.join(self.project_dir, f)
        return os.path.join(self.project_dir, "README.md")

    def format_link(self, val, mpn):
        """Creates a clickable search link using the user's preferred search engine."""
        clean_val = str(val).replace('|', '-') if val else "Unknown"
        if mpn:
            clean_mpn = str(mpn).strip()
            # URL encode the MPN so characters like '#' don't break the link
            encoded_mpn = urllib.parse.quote(clean_mpn)
            
            # Fetch preferences
            engine = self.settings.get('search_engine', 'Octopart')
            currency = self.settings.get('currency', 'USD')
            
            if engine == 'ComponentSearchEngine':
                return f"[{clean_val}](https://componentsearchengine.com/search.html?searchString={encoded_mpn})"
            else:
                # Default is Octopart
                url = f"https://octopart.com/search?q={encoded_mpn}"
                # Only append currency if it's explicitly not USD
                if currency and currency != 'USD':
                    url += f"&currency={currency}"
                    
                return f"[{clean_val}]({url})"
                
        return clean_val

    def _get_drc_status(self, pcb_file):
        """Silently runs DRC in the background and returns a formatted markdown string."""
        kicad_cli = "kicad-cli.exe" if sys.platform == "win32" else "kicad-cli"
        out_json = os.path.join(tempfile.gettempdir(), "readme_drc_report.json")
        
        if os.path.exists(out_json):
            try: os.remove(out_json)
            except: pass
            
        try:
            # Run DRC and dump to a temp JSON file
            subprocess.run([kicad_cli, "pcb", "drc", "--format", "json", "--output", out_json, pcb_file], 
                           capture_output=True, cwd=self.project_dir)
            
            if os.path.exists(out_json):
                with open(out_json, 'r', encoding='utf-8', errors='ignore') as f:
                    data = json.load(f)
                    
                errors = 0
                warnings = 0
                
                # Count standard violations
                for v in data.get("violations", []):
                    if v.get("severity") == "error": errors += 1
                    else: warnings += 1
                    
                # Count unconnected items (which are effectively errors)
                unconnected = len(data.get("unconnected_items", []))
                errors += unconnected
                
                if errors == 0 and warnings == 0:
                    return "✅ Clean (0 Errors, 0 Warnings)"
                else:
                    return f"⚠️ {errors} Errors, {warnings} Warnings"
        except Exception as e:
            return f"❓ Check Failed ({e})"
        finally:
            if os.path.exists(out_json):
                try: os.remove(out_json)
                except: pass
                
        return "❓ Unknown"

    def _extract_pcb_advanced(self, pcb_file):
        """Extracts deep technical insights from the PCB file via Regex."""
        data = {
            'vias': {'total': 0, 'through': 0, 'blind': 0, 'micro': 0},
            'smd_count': 0,
            'tht_count': 0
        }
        if not pcb_file or not os.path.exists(pcb_file): return data
        
        with open(pcb_file, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
            
            # 1. Via Statistics
            data['vias']['micro'] = len(re.findall(r'\(via\s+micro', content))
            data['vias']['blind'] = len(re.findall(r'\(via\s+blind', content))
            total_vias = len(re.findall(r'\(via\b', content))
            data['vias']['total'] = total_vias
            data['vias']['through'] = total_vias - data['vias']['micro'] - data['vias']['blind']
                
            # 2. Component Mounting Types
            data['smd_count'] = len(re.findall(r'\(\s*attr\s+smd\b', content))
            data['tht_count'] = len(re.findall(r'\(\s*attr\s+through_hole\b', content))
                
        return data

    def _extract_sch_advanced(self, sch_files):
        """Extracts high-level architecture details from Schematic files via Regex."""
        data = {
            'buses': set(),
            'sheets': set(),
            'title_block': {},
            'dnp_list': set()
        }
        if not sch_files: return data
        
        bus_patterns = [r'sda', r'scl', r'tx', r'rx', r'mosi', r'miso', r'sck', r'd\+', r'd\-', r'can_h', r'can_l', r'usb', r'i2c', r'spi', r'uart']
        bus_re = re.compile(r'(?i).*(' + '|'.join(bus_patterns) + r').*')

        # Read the main sch file for title block
        main_sch = sch_files[0]
        with open(main_sch, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
            tb = re.search(r'\(title_block(.*?)\)', content, re.DOTALL)
            if tb:
                tb_str = tb.group(1)
                for key in ['title', 'company', 'rev', 'date']:
                    m = re.search(rf'\({key}\s+"([^"]+)"\)', tb_str)
                    if m and m.group(1).strip() and m.group(1).strip() != '""':
                        data['title_block'][key] = m.group(1).strip()

        # Read all sheets for buses, DNPs, and sub-sheets
        for sch in sch_files:
            with open(sch, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                
                # Sheets
                sheets = re.findall(r'\(property\s+"Sheetname"\s+"([^"]+)"\)', content)
                for s in sheets:
                    if s != "Root":
                        data['sheets'].add(s)
                
                # Buses
                labels = re.findall(r'\((?:global|hierarchical_label|label)\s+"([^"]+)"', content)
                for lbl in labels:
                    if bus_re.match(lbl):
                        data['buses'].add(lbl)
                
                # DNP List
                blocks = re.split(r'\(\s*symbol\s+', content)[1:]
                for block in blocks:
                    if re.search(r'\(\s*on_board\s+no\s*\)', block) or re.search(r'\(\s*property\s+"dnp".*?"(?:yes|true|1)"', block, re.IGNORECASE):
                        ref_match = re.search(r'\(\s*property\s+"Reference"\s+"([^"]+)"', block)
                        val_match = re.search(r'\(\s*property\s+"Value"\s+"([^"]*)"', block)
                        if ref_match:
                            ref = ref_match.group(1)
                            val = val_match.group(1) if val_match else ""
                            if ref != "Reference" and not ref.startswith("TP") and not ref.startswith("#"):
                                data['dnp_list'].add(f"{ref} ({val})")
                                
        return data

    def update_readme(self, kicad_version="Unknown KiCad Version"):
        pcb_files = glob.glob(os.path.join(self.project_dir, "*.kicad_pcb"))
        sch_files = glob.glob(os.path.join(self.project_dir, "*.kicad_sch"))

        pcb_file = pcb_files[0] if pcb_files else None

        # --- 1. Gather Basic Metrics ---
        dims_str = "Unknown"
        area_str = "Unknown"
        layer_count = "Unknown"
        drc_status_str = None
        all_todos = set()

        if pcb_file:
            dims = get_pcb_dimensions(pcb_file)
            if dims:
                dims_str = f"{dims['w']} x {dims['h']} mm"
                area_str = f"{dims['area']} mm²"

            layers = get_pcb_layers(pcb_file)
            cu_layers = [l for l in layers if l.endswith('.Cu')]
            layer_count = f"{len(cu_layers)} Copper Layers" if cu_layers else "Unknown"
            
            all_todos.update(extract_todos(pcb_file))
            
            # Extract optional DRC metrics
            if self.settings.get('readme_drc', False):
                drc_status_str = self._get_drc_status(pcb_file)

        # --- 2. Gather Advanced Metrics ---
        pcb_adv = self._extract_pcb_advanced(pcb_file)
        sch_adv = self._extract_sch_advanced(sch_files)

        total_comps = 0
        unique_parts = 0
        
        core_ics = []
        connectors = []
        crystals = []
        passives = {}
        power_nets = set()
        mount_holes = []

        bom = {}
        mpn_field_setting = self.settings.get('mpn_field_name', 'MPN')
        if sch_files:
            for sch in sch_files:
                # Pass mpn_field into get_bom_data so it renders the correct part numbers in README
                sch_bom = get_bom_data(sch, include_excluded_from_bom=True, mpn_field=mpn_field_setting)
                bom.update(sch_bom)
                all_todos.update(extract_todos(sch))
                
                try:
                    with open(sch, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                        matches = re.findall(r'\(lib_id\s+"power:([^"]+)"', content)
                        for m in matches: power_nets.add(m)
                except Exception as e:
                    print(f"Error parsing power nets: {e}")

            total_comps = len(bom)
            unique_set = set()

            for ref, data in bom.items():
                val = data.get('val', '')
                fp = data.get('fp', '')
                desc = data.get('desc', '')
                mpn = data.get('mpn', '')
                
                unique_set.add(f"{val}_{fp}_{mpn}")

                is_ic = re.match(r'^(U|IC|MOD|MK)\d+', ref)
                is_connector = re.match(r'^(J|P|BT|SW)\d+', ref)
                is_crystal = re.match(r'^(Y|X)\d+', ref)
                is_mh = re.match(r'^(H|MH)\d+', ref) or 'mountinghole' in fp.lower()
                is_passive = re.match(r'^(R|C|L|D|FB)\d+', ref)

                if is_ic:
                    core_ics.append({'ref': ref, 'val': val, 'desc': desc, 'mpn': mpn})
                elif is_connector:
                    c_type = 'Battery' if ref.startswith('BT') else 'Switch' if ref.startswith('SW') else 'Connector'
                    connectors.append({'ref': ref, 'val': val, 'desc': desc, 'mpn': mpn, 'type': c_type})
                elif is_crystal:
                    crystals.append({'ref': ref, 'val': val, 'desc': desc, 'fp': fp, 'mpn': mpn})
                elif is_mh:
                    mount_holes.append({'ref': ref, 'val': val, 'fp': fp})
                elif is_passive:
                    size_match = re.search(r'(?<!\d)(0201|0402|0603|0805|1206|1210|2010|2512)(?!\d)', fp)
                    size = size_match.group(1) if size_match else "Other"
                    comp_type = "Resistor" if ref.startswith('R') else "Capacitor" if ref.startswith('C') else "Inductor" if ref.startswith('L') else "Diode" if ref.startswith('D') else "Ferrite Bead"

                    if comp_type not in passives: passives[comp_type] = {}
                    if size not in passives[comp_type]: passives[comp_type][size] = 0
                    passives[comp_type][size] += 1

            unique_parts = len(unique_set)

        # --- 3. Build Markdown ---
        md = [
            "<!-- KICAD_DIFF_GEN_START -->",
            "---",
            "## 🛠 Technical Hardware Summary",
            "*Autogenerated by KiCad Hardware Control*\n",
            "### 📋 Project Overview",
            "| Metric | Value |",
            "| :--- | :--- |"
        ]

        tb = sch_adv.get('title_block', {})
        if tb.get('title'): md.append(f"| **Project Title** | {tb['title']} |")
        if tb.get('company'): md.append(f"| **Company** | {tb['company']} |")
        if tb.get('rev'): md.append(f"| **Revision** | {tb['rev']} |")
        if tb.get('date'): md.append(f"| **Date** | {tb['date']} |")

        # Utilize the improved components parser rather than raw footprint regex count
        # This properly excludes Test Points (if filtered in the parser)
        pcb_components = get_pcb_structure(pcb_file) if pcb_file else {}
        pcb_total = len(pcb_components) if pcb_file else total_comps

        md.extend([
            f"| **Board Dimensions** | {dims_str} |",
            f"| **Total Area** | {area_str} |",
            f"| **Layer Count** | {layer_count} |",
            f"| **Total Components** | {pcb_total} |",
            f"| **SMD Components** | {pcb_adv['smd_count']} |",
            f"| **THT Components** | {pcb_adv['tht_count']} |",
            f"| **Unique Parts** | {unique_parts} |"
        ])

        if drc_status_str:
            md.append(f"| **DRC Status** | {drc_status_str} |")

        md.append(f"| **KiCad Version** | {kicad_version} |\n")

        if pcb_file:
            md.append("### 📐 Manufacturing & DRC")
            md.append("| Metric | Value |")
            md.append("| :--- | :--- |")
            v = pcb_adv['vias']
            md.append(f"| **Vias** | {v['total']} Total ({v['through']} TH, {v['blind']} Blind, {v['micro']} Micro) |\n")

        sheets = sorted(list(sch_adv.get('sheets', set())))
        buses = sorted(list(sch_adv.get('buses', set())))
        filtered_power = sorted([p for p in power_nets if p.lower() not in ['vcc', 'vdd']])
        
        if sheets or buses or filtered_power:
            md.append("### 📄 Architecture")
            if sheets: md.append(f"- **Hierarchical Sheets:** `{', '.join(sheets)}`")
            if buses: md.append(f"- **Digital Buses:** `{', '.join(buses)}`")
            if filtered_power: md.append(f"- **Power Domains:** `{', '.join(filtered_power)}`")
            md.append("")

        if mount_holes:
            md.append("### ⚙️ Mechanical")
            mh_details = [f"{m['ref']} ({str(m['fp']).split(':')[-1].replace('|', '-') if m['fp'] else 'Unknown'})" for m in mount_holes]
            md.append(f"- **Mounting Holes:** {len(mount_holes)} ({', '.join(mh_details)})")
            md.append("")

        dnp_list = sorted(list(sch_adv.get('dnp_list', set())))
        if dnp_list:
            md.append("### 🚫 Do Not Populate (DNP)")
            for dnp in dnp_list: md.append(f"- {dnp}")
            md.append("")

        if all_todos:
            md.append("### 📝 Project TODOs")
            for t in sorted(list(all_todos)): md.append(f"- {t}")
            md.append("")

        if core_ics:
            md.append("### 🧠 Core ICs & Modules")
            md.append("| Reference | Component | Function / Description |")
            md.append("| :--- | :--- | :--- |")
            for ic in core_ics:
                comp_text = self.format_link(ic['val'], ic['mpn'])
                clean_desc = str(ic['desc']).replace('|', '-')
                md.append(f"| {ic['ref']} | {comp_text} | {clean_desc} |")
            md.append("")

        if connectors:
            md.append("### 🔌 Connectors & Interfaces")
            md.append("| Reference | Type | Component | Description |")
            md.append("| :--- | :--- | :--- | :--- |")
            for c in connectors:
                comp_text = self.format_link(c['val'], c['mpn'])
                clean_desc = str(c['desc']).replace('|', '-')
                md.append(f"| {c['ref']} | {c['type']} | {comp_text} | {clean_desc} |")
            md.append("")

        if crystals:
            md.append("### ⏱ Clocks & Oscillators")
            md.append("| Reference | Value | Package / Footprint |")
            md.append("| :--- | :--- | :--- |")
            for c in crystals:
                comp_text = self.format_link(c['val'], c['mpn'])
                clean_fp = str(c['fp']).split(':')[-1].replace('|', '-')
                md.append(f"| {c['ref']} | {comp_text} | {clean_fp} |")
            md.append("")

        if passives:
            md.append("### 📏 Passive Components")
            md.append("| Component | Breakdown |")
            md.append("| :--- | :--- |")
            
            for comp_type in sorted(passives.keys()):
                sizes = passives[comp_type]
                sorted_sizes = sorted(sizes.keys(), key=lambda s: (s == 'Other', s))
                breakdown_parts = [f"{sizes[s]}x{s}" for s in sorted_sizes]
                
                if len(breakdown_parts) > 1:
                    breakdown_str = ", ".join(breakdown_parts[:-1]) + " and " + breakdown_parts[-1]
                else:
                    breakdown_str = breakdown_parts[0]
                    
                md.append(f"| {comp_type} | {breakdown_str} |")
            md.append("")

        md.extend([
            "---",
            "<!-- KICAD_DIFF_GEN_END -->"
        ])
        
        new_block = "\n".join(md)

        readme_path = self._find_readme()
        content = ""
        
        if os.path.exists(readme_path):
            with open(readme_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()

        content = re.sub(r'<!-- KICAD_DIFF_GEN_START -->.*?<!-- KICAD_DIFF_GEN_END -->', '', content, flags=re.DOTALL)
        content = content.rstrip()

        if content: content += "\n\n"
        content += new_block + "\n"

        with open(readme_path, "w", encoding="utf-8") as f:
            f.write(content)

        return readme_path