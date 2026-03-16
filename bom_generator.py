import os
import csv
import glob
import re
from .kicad_parser import get_bom_data

class BOMGenerator:
    def __init__(self, project_dir, settings=None):
        self.project_dir = project_dir
        self.settings = settings or {}
        self.mpn_field = self.settings.get('mpn_field_name', 'MPN')

    @staticmethod
    def _natural_sort_key(ref):
        """Splits strings into text and numbers for proper alphanumeric sorting (e.g., R2 comes before R10)."""
        return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', ref)]

    def generate_boms(self):
        """Generates two CSV files: a distributor-friendly one and a full engineering one."""
        gen_dist = self.settings.get('generate_bom_dist', False)
        gen_eng = self.settings.get('generate_bom_eng', False)
        
        if not gen_dist and not gen_eng:
            return []

        sch_files = glob.glob(os.path.join(self.project_dir, "*.kicad_sch"))
        if not sch_files:
            return []

        # Merge BOM data from all schematic files (Respecting custom MPN field)
        all_bom = {}
        for sch in sch_files:
            all_bom.update(get_bom_data(sch, mpn_field=self.mpn_field))

        # Filter out unannotated/template components (e.g., 'R', 'C', 'U') that lack numbers
        all_bom = {ref: data for ref, data in all_bom.items() if any(c.isdigit() for c in ref)}

        if not all_bom:
            return []

        # Get project name for file naming
        project_name = "project"
        pro_files = glob.glob(os.path.join(self.project_dir, "*.kicad_pro"))
        if pro_files:
            project_name = os.path.splitext(os.path.basename(pro_files[0]))[0]

        generated_files = []

        # 1. Generate Full Engineering BOM
        if gen_eng:
            full_path = os.path.join(self.project_dir, f"{project_name}_full_bom.csv")
            self._write_full_bom(full_path, all_bom)
            generated_files.append(full_path)

        # 2. Generate Distributor BOM
        if gen_dist:
            dist_path = os.path.join(self.project_dir, f"{project_name}_distributor_bom.csv")
            self._write_distributor_bom(dist_path, all_bom)
            generated_files.append(dist_path)

        return generated_files

    def _write_full_bom(self, path, bom):
        """Detailed BOM for human review."""
        groups = {}
        for ref, data in bom.items():
            # Signature for grouping preventing identical parts from splitting
            sig = (data['val'], data['fp'], data.get('mpn', ''), data.get('desc', ''))
            if sig not in groups:
                groups[sig] = {'refs': [], 'data': data}
            groups[sig]['refs'].append(ref)

        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['Qty', 'Reference', 'Value', 'Footprint', self.mpn_field, 'Description'])
            
            # Sort references within each group natively
            for item in groups.values():
                item['refs'].sort(key=self._natural_sort_key)
                
            # Sort groups by the first reference in each group
            sorted_groups = sorted(groups.values(), key=lambda g: self._natural_sort_key(g['refs'][0]))
            
            for item in sorted_groups:
                writer.writerow([
                    len(item['refs']),
                    ", ".join(item['refs']),
                    item['data']['val'],
                    item['data']['fp'],
                    item['data'].get('mpn', ''),
                    item['data'].get('desc', '')
                ])

    def _write_distributor_bom(self, path, bom):
        """Compact BOM for automated tool uploads."""
        groups = {}
        for ref, data in bom.items():
            mpn = data.get('mpn', '').strip()
            if not mpn:
                continue # Skip parts with no MPN for distributor BOM
                
            # Signature includes Value & Footprint to prevent merging DIFFERENT parts that share "NO_MPN"
            sig = (data['val'], data['fp'], mpn)
            if sig not in groups:
                groups[sig] = {'refs': [], 'mpn': mpn}
            groups[sig]['refs'].append(ref)

        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['Qty', 'Reference', self.mpn_field])
            
            # Sort references within each group natively
            for item in groups.values():
                item['refs'].sort(key=self._natural_sort_key)
                
            # Sort groups by the first reference in each group
            sorted_groups = sorted(groups.values(), key=lambda g: self._natural_sort_key(g['refs'][0]))
            
            for item in sorted_groups:
                writer.writerow([
                    len(item['refs']),
                    ", ".join(item['refs']),
                    item['mpn']
                ])