import os
import re
import glob
import urllib.parse
from .kicad_parser import get_pcb_dimensions, get_pcb_layers, get_bom_data

class ReadmeGenerator:
    def __init__(self, project_dir, settings=None):
        self.project_dir = project_dir
        self.settings = settings or {}

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

    def update_readme(self, kicad_version="Unknown KiCad Version"):
        pcb_files = glob.glob(os.path.join(self.project_dir, "*.kicad_pcb"))
        sch_files = glob.glob(os.path.join(self.project_dir, "*.kicad_sch"))

        pcb_file = pcb_files[0] if pcb_files else None

        # --- 1. Gather Metrics ---
        dims_str = "Unknown"
        area_str = "Unknown"
        layer_count = "Unknown"

        if pcb_file:
            dims = get_pcb_dimensions(pcb_file)
            if dims:
                dims_str = f"{dims['w']} x {dims['h']} mm"
                area_str = f"{dims['area']} mm²"

            layers = get_pcb_layers(pcb_file)
            cu_layers = [l for l in layers if l.endswith('.Cu')]
            layer_count = f"{len(cu_layers)} Copper Layers" if cu_layers else "Unknown"

        total_comps = 0
        unique_parts = 0
        
        core_ics = []
        connectors = []
        crystals = []
        passives = {}  # Format: (Type, Size, Value) -> {'count': int, 'hs_refs': []}
        power_nets = set()

        bom = {}
        if sch_files:
            for sch in sch_files:
                # Merge BOM data from ALL hierarchical sheets
                sch_bom = get_bom_data(sch)
                bom.update(sch_bom)
                
                # Extract Power Nets safely directly from the schematic text
                try:
                    with open(sch, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                        # KiCad power symbols use lib_ids like "power:+3.3V" or "power:GND"
                        matches = re.findall(r'\(lib_id\s+"power:([^"]+)"', content)
                        for m in matches:
                            power_nets.add(m)
                except Exception as e:
                    print(f"Error parsing power nets from {sch}: {e}")

            total_comps = len(bom)
            unique_set = set()

            for ref, data in bom.items():
                val = data.get('val', '')
                fp = data.get('fp', '')
                desc = data.get('desc', '')
                mpn = data.get('mpn', '')
                
                # Create a unique signature for the part
                unique_set.add(f"{val}_{fp}_{mpn}")

                # Component Classification Logic
                is_ic = re.match(r'^(U|IC|MOD|MK)\d+', ref)
                is_connector = re.match(r'^(J|P|BT|SW)\d+', ref)
                is_crystal = re.match(r'^(Y|X)\d+', ref)
                is_passive = re.match(r'^(R|C|L|D|FB)\d+', ref)

                if is_ic:
                    core_ics.append({'ref': ref, 'val': val, 'desc': desc, 'mpn': mpn})
                elif is_connector:
                    c_type = 'Battery' if ref.startswith('BT') else 'Switch' if ref.startswith('SW') else 'Connector'
                    connectors.append({'ref': ref, 'val': val, 'desc': desc, 'mpn': mpn, 'type': c_type})
                elif is_crystal:
                    crystals.append({'ref': ref, 'val': val, 'desc': desc, 'fp': fp, 'mpn': mpn})
                elif is_passive:
                    # Look for size ignoring surrounding underscores/characters
                    size_match = re.search(r'(?<!\d)(0201|0402|0603|0805|1206|1210|2010|2512)(?!\d)', fp)
                    size = size_match.group(1) if size_match else "Other"

                    # Determine specific passive type
                    comp_type = "Resistor" if ref.startswith('R') else "Capacitor" if ref.startswith('C') else "Inductor" if ref.startswith('L') else "Diode" if ref.startswith('D') else "Ferrite Bead"

                    if comp_type not in passives:
                        passives[comp_type] = {}
                    if size not in passives[comp_type]:
                        passives[comp_type][size] = 0
                    
                    passives[comp_type][size] += 1

            unique_parts = len(unique_set)

        # Sort items for clean display
        core_ics = sorted(core_ics, key=lambda x: x['ref'])
        connectors = sorted(connectors, key=lambda x: x['ref'])
        crystals = sorted(crystals, key=lambda x: x['ref'])
        
        # Filter Power Nets (Ignore generic VCC, VDD)
        ignore_nets = ['vcc', 'vdd']
        filtered_power = sorted([p for p in power_nets if p.lower() not in ignore_nets])

        md = [
            "<!-- KICAD_DIFF_GEN_START -->",
            "---",
            "## 🛠 Technical Hardware Summary",
            "*Autogenerated by KiCad Hardware Control*\n",
            "### 📋 Project Overview",
            "| Metric | Value |",
            "| :--- | :--- |",
            f"| **Board Dimensions** | {dims_str} |",
            f"| **Total Area** | {area_str} |",
            f"| **Layer Count** | {layer_count} |",
            f"| **Total Components** | {total_comps} |",
            f"| **Unique Parts** | {unique_parts} |",
            f"| **KiCad Version** | {kicad_version} |\n"
        ]

        if filtered_power:
            md.append("### ⚡ Power Domains")
            md.append(f"**Detected Nets:** `{', '.join(filtered_power)}`\n")

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
                clean_fp = str(c['fp']).split(':')[-1].replace('|', '-') # Simplify footprint name
                md.append(f"| {c['ref']} | {comp_text} | {clean_fp} |")
            md.append("")

        if passives:
            md.append("### 📏 Passive Components")
            md.append("| Component | Breakdown |")
            md.append("| :--- | :--- |")
            
            # Sort types (Capacitor, Diode, Inductor, Resistor, etc.)
            for comp_type in sorted(passives.keys()):
                sizes = passives[comp_type]
                
                # Sort sizes alphabetically, but force 'Other' to the very end
                sorted_sizes = sorted(sizes.keys(), key=lambda s: (s == 'Other', s))
                
                breakdown_parts = []
                for s in sorted_sizes:
                    breakdown_parts.append(f"{sizes[s]}x{s}")
                
                # Format gracefully (e.g., "5x0402 and 10x0603" or "5x0402, 10x0603, and 2xOther")
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

        # --- 3. Inject Sticky Footer into README.md ---
        readme_path = os.path.join(self.project_dir, "README.md")
        content = ""
        
        if os.path.exists(readme_path):
            with open(readme_path, "r", encoding="utf-8") as f:
                content = f.read()

        # Remove the old autogenerated block
        content = re.sub(r'<!-- KICAD_DIFF_GEN_START -->.*?<!-- KICAD_DIFF_GEN_END -->', '', content, flags=re.DOTALL)
        content = content.rstrip()

        if content:
            content += "\n\n"

        # Append fresh sticky footer
        content += new_block + "\n"

        with open(readme_path, "w", encoding="utf-8") as f:
            f.write(content)

        return readme_path