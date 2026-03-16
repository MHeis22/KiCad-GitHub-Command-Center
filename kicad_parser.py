import os
import re

def get_pcb_layers(pcb_file):
    """Quickly parse the .kicad_pcb file to find active copper layers and technical layers."""
    layers = ["F.Cu", "B.Cu", "F.Silkscreen", "B.Silkscreen", "Edge.Cuts"]
    if not os.path.exists(pcb_file):
        return layers
        
    try:
        with open(pcb_file, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read(50000) # Increased to safely reach the (layers ...) block in large boards
            
            # Match all valid copper types instead of just 'signal'
            matches = re.findall(r'\(\d+\s+"([^"]+)"\s+(?:signal|power|mixed|jumper)\)', content)
            if matches:
                layers = matches + ["F.Silkscreen", "B.Silkscreen", "Edge.Cuts"]
                
                # Fail-safe: ensure outer layers aren't accidentally dropped if parsing behaves oddly
                if "F.Cu" not in layers: layers.insert(0, "F.Cu")
                if "B.Cu" not in layers: layers.insert(1, "B.Cu")
    except:
        pass
        
    # De-duplicate while preserving order
    seen = set()
    return [x for x in layers if not (x in seen or seen.add(x))]

def get_chain_area(chain):
    """Helper function to calculate area of a polygon loop using the Shoelace formula."""
    try:
        import pcbnew
        pts = []
        for k in range(chain.GetPointCount()):
            p = chain.CPoint(k) if hasattr(chain, 'CPoint') else chain.Point(k)
            x = getattr(p, 'x', p[0] if hasattr(p, '__getitem__') else 0)
            y = getattr(p, 'y', p[1] if hasattr(p, '__getitem__') else 0)
            
            if hasattr(pcbnew, 'ToMM'):
                x_mm = pcbnew.ToMM(x)
                y_mm = pcbnew.ToMM(y)
            else:
                x_mm = x / 1000000.0
                y_mm = y / 1000000.0
            pts.append((x_mm, y_mm))
        
        if len(pts) < 3: 
            return 0.0
        
        area = 0.0
        j = len(pts) - 1
        for i in range(len(pts)):
            area += (pts[j][0] + pts[i][0]) * (pts[j][1] - pts[i][1])
            j = i
        return abs(area) / 2.0
    except Exception as e:
        print(f"Chain Area Error: {e}")
        return 0.0

def get_pcb_dimensions(file_path):
    """Uses pcbnew to accurately calculate the board's physical dimensions based on Edge.Cuts."""
    if not file_path or not os.path.exists(file_path):
        return None
    try:
        import pcbnew
        board = pcbnew.LoadBoard(file_path)
        
        bbox = board.GetBoardEdgesBoundingBox()
        
        if hasattr(pcbnew, 'ToMM'):
            w_mm = pcbnew.ToMM(bbox.GetWidth())
            h_mm = pcbnew.ToMM(bbox.GetHeight())
        else:
            w_mm = bbox.GetWidth() / 1000000.0
            h_mm = bbox.GetHeight() / 1000000.0
            
        line_thickness_mm = 0.0
        for item in board.GetDrawings():
            if item.GetLayer() == pcbnew.Edge_Cuts:
                if hasattr(item, 'GetWidth'):
                    if hasattr(pcbnew, 'ToMM'):
                        line_thickness_mm = pcbnew.ToMM(item.GetWidth())
                    else:
                        line_thickness_mm = item.GetWidth() / 1000000.0
                    break
                    
        if w_mm > line_thickness_mm and h_mm > line_thickness_mm:
            w_mm -= line_thickness_mm
            h_mm -= line_thickness_mm
            
        if w_mm <= 0.1 or h_mm <= 0.1:
            return None
            
        true_area_mm2 = 0.0
        
        try:
            if hasattr(board, 'GetBoardArea'):
                true_area_mm2 = board.GetBoardArea() / 1000000000000.0
        except:
            pass
            
        if true_area_mm2 <= 0.1:
            try:
                poly_set = None
                try:
                    ps = pcbnew.SHAPE_POLY_SET()
                    res = board.GetBoardPolygonOutlines(ps)
                    
                    if hasattr(res, 'OutlineCount') and not isinstance(res, bool):
                        poly_set = res
                    elif isinstance(res, tuple):
                        for item in res:
                            if hasattr(item, 'OutlineCount'):
                                poly_set = item
                                break
                    if poly_set is None:
                        poly_set = ps
                except TypeError:
                    res = board.GetBoardPolygonOutlines()
                    if hasattr(res, 'OutlineCount') and not isinstance(res, bool):
                        poly_set = res
                    elif isinstance(res, tuple):
                        for item in res:
                            if hasattr(item, 'OutlineCount'):
                                poly_set = item
                                break

                if poly_set and hasattr(poly_set, 'OutlineCount'):
                    for i in range(poly_set.OutlineCount()):
                        outline = poly_set.Outline(i)
                        true_area_mm2 += get_chain_area(outline)
                        
                        for j in range(poly_set.HoleCount(i)):
                            hole = poly_set.Hole(i, j)
                            true_area_mm2 -= get_chain_area(hole)
            except Exception as e:
                print(f"Polygon Area Extraction Error: {e}")
            
        if true_area_mm2 <= 0.1:
            true_area_mm2 = w_mm * h_mm

        return {
            "w": round(w_mm, 2), 
            "h": round(h_mm, 2), 
            "area": round(true_area_mm2, 2)
        }
    except Exception as e:
        print(f"Dimension Extraction Error: {e}")
        return None

def get_pcb_structure(file_path):
    components = {} 
    if not file_path or not os.path.exists(file_path):
        return components
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
            blocks = re.split(r'\(footprint\s+', content)[1:]
            for block in blocks:
                name_match = re.search(r'^"([^"]+)"', block)
                if not name_match: 
                    name_match = re.search(r'^([^"\s\)]+)', block)
                name = name_match.group(1) if name_match else "Unknown"
                
                ref_match = re.search(r'\((?:fp_text\s+reference|property\s+"Reference")\s+"([^"]+)"', block)
                val_match = re.search(r'\((?:fp_text\s+value|property\s+"Value")\s+"([^"]*)"', block)
                
                if ref_match:
                    ref = ref_match.group(1)
                    if ref.startswith('TP') or ref.startswith('#'): continue # EXCLUDE TEST POINTS AND VIRTUAL PARTS
                    
                    val = val_match.group(1) if val_match else ""
                    components[ref] = {'fp': name, 'val': val}
    except Exception as e:
        print(f"PCB Structure Error: {e}")
    return components

def get_sch_structure(file_path):
    components = {}
    if not file_path or not os.path.exists(file_path):
        return components
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
            blocks = re.split(r'\(\s*symbol\s+', content)[1:]
            for block in blocks:
                lib_match = re.search(r'\(lib_id\s+"([^"]+)"', block)
                fp_match  = re.search(r'\(\s*property\s+"Footprint"\s+"([^"]*)"', block)
                ref_match = re.search(r'\(\s*property\s+"Reference"\s+"([^"]+)"', block)
                val_match = re.search(r'\(\s*property\s+"Value"\s+"([^"]*)"', block)
                
                if ref_match:
                    ref = ref_match.group(1)
                    if ref == "Reference" or ref.startswith('TP') or ref.startswith('#'): continue # EXCLUDE TEST POINTS AND VIRTUAL PARTS
                    
                    fp = fp_match.group(1) if fp_match and fp_match.group(1) else (lib_match.group(1) if lib_match else "Unknown")
                    val = val_match.group(1) if val_match else ""
                    components[ref] = {'fp': fp, 'val': val}
    except Exception as e:
        print(f"SCH Structure Error: {e}")
    return components

def get_bom_data(file_path, include_excluded_from_bom=False, mpn_field=None):
    """Extracts structured BOM data, respecting Exclude from Board / DNP flags."""
    bom = {}
    if not file_path or not os.path.exists(file_path):
        return bom
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
            blocks = re.split(r'\(\s*symbol\s+', content)[1:]
            for block in blocks:
                # Skip if excluded from BOM unless explicitly told to include them
                if not include_excluded_from_bom and re.search(r'\(\s*in_bom\s+no\s*\)', block):
                    continue 
                    
                ref_match = re.search(r'\(\s*property\s+"Reference"\s+"([^"]+)"', block)
                if not ref_match: continue
                ref = ref_match.group(1)
                
                if ref == "Reference" or ref.startswith('TP') or ref.startswith('#'): continue # EXCLUDE TEMPLATES, TEST POINTS, AND VIRTUAL PARTS
                
                val_match = re.search(r'\(\s*property\s+"Value"\s+"([^"]*)"', block)
                fp_match  = re.search(r'\(\s*property\s+"Footprint"\s+"([^"]*)"', block)
                desc_match = re.search(r'\(\s*property\s+"Description"\s+"([^"]*)"', block)
                
                # Catch common MPN property variants, prioritizing user's custom field
                if mpn_field:
                    safe_field = re.escape(mpn_field)
                    mpn_regex = rf'\(\s*property\s+"(?:{safe_field}|MPN|Part Number|Manufacturer Part Number|Manufacturer_Part_Number|LCSC Part|LCSC)"\s+"([^"]*)"'
                else:
                    mpn_regex = r'\(\s*property\s+"(?:MPN|Part Number|Manufacturer Part Number|Manufacturer_Part_Number|LCSC Part|LCSC)"\s+"([^"]*)"'
                
                mpn_match = re.search(mpn_regex, block, re.IGNORECASE)
                
                bom[ref] = {
                    'val': val_match.group(1) if val_match else "",
                    'fp': fp_match.group(1) if fp_match else "",
                    'desc': desc_match.group(1) if desc_match else "",
                    'mpn': mpn_match.group(1) if mpn_match else ""
                }
    except Exception as e:
        print(f"BOM Extraction Error: {e}")
    return bom

def compare_logic_data(old_data, curr_data):
    """Compares two component dictionaries and returns a text diff."""
    changes = []
    all_refs = sorted(set(old_data.keys()) | set(curr_data.keys()))
    
    for ref in all_refs:
        if ref not in old_data:
            changes.append(f"+ {ref}: Added ['{curr_data[ref]['val']}', FP: '{curr_data[ref]['fp']}']")
        elif ref not in curr_data:
            changes.append(f"- {ref}: Removed (was '{old_data[ref]['val']}')")
        else:
            o = old_data[ref]
            c = curr_data[ref]
            diffs = []
            if o['val'] != c['val']:
                diffs.append(f"Value '{o['val']}' -> '{c['val']}'")
            if o['fp'] != c['fp']:
                diffs.append(f"Footprint/Lib '{o['fp']}' -> '{c['fp']}'")
            
            if diffs:
                changes.append(f"M {ref}: " + ", ".join(diffs))
                
    return "\n".join(changes)

def extract_todos(file_path):
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