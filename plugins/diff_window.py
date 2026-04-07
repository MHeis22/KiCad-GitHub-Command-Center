import os
import tempfile
import webbrowser
import pathlib
import json
import base64
import gzip
import re

class DiffWindow:
    def __init__(self, diffs, summary_text, target_name="HEAD", kicad_version="Unknown KiCad Version", colorblind=False):
        """
        diffs expects a list of dicts: 
        [{'name': '...', 'status': '...', 'visuals': {...}, 'bom_data': {'curr':{}, 'old':{}}}]
        """
        self.diffs = diffs
        self.summary_text = summary_text.replace('\n', '<br>')
        self.target_name = target_name
        self.kicad_version = kicad_version
        self.colorblind = colorblind

    def _minify_svg(self, svg_text):
        """Removes unnecessary whitespace, comments, and metadata to highly compress SVG text."""
        # Remove XML comments
        svg_text = re.sub(r'<!--.*?-->', '', svg_text, flags=re.DOTALL)
        # Remove metadata block which can be huge and is useless for the visual diff
        svg_text = re.sub(r'<metadata>.*?</metadata>', '', svg_text, flags=re.DOTALL)
        # Remove whitespace between tags
        svg_text = re.sub(r'>\s+<', '><', svg_text)
        return svg_text.strip()

    def _get_file_data(self, file_path):
        """
        Reads a file. If it's an SVG, returns minified raw text.
        Otherwise, returns a Base64 encoded Data URI (for PDFs, etc.).
        Returns tuple: (is_svg_boolean, data_string)
        """
        if not file_path or not os.path.exists(file_path):
            return False, ""
        try:
            ext = file_path.lower().split('.')[-1]
            if ext == "svg":
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    raw_svg = f.read()
                    minified_svg = self._minify_svg(raw_svg)
                    return True, minified_svg
            else:
                mime_type = "application/pdf" if ext == "pdf" else "image/png"
                with open(file_path, "rb") as f:
                    encoded = base64.b64encode(f.read()).decode('utf-8')
                return False, f"data:{mime_type};base64,{encoded}"
        except Exception as e:
            print(f"Error processing {file_path}: {e}")
            return False, ""

    def Show(self):
        base_name = "KiCad"
        if self.diffs and len(self.diffs) > 0:
            # Extract the raw project name from the first file (e.g. 'my_board.kicad_pcb' -> 'my_board')
            base_name = os.path.splitext(self.diffs[0]['name'])[0]
            
        # Sanitize the target name so it's safe for Windows/Mac/Linux file systems
        safe_target = self.target_name.replace('/', '_').replace('\\', '_').replace(' ', '_').replace('*', '')
        
        smart_filename = f"{base_name}_Diff_vs_{safe_target}.html"
        html_path = os.path.join(tempfile.gettempdir(), smart_filename)
        # --------------------------------
        
        # Prepare data for JavaScript
        js_diffs = []
        for d in self.diffs:
            processed_visuals = {}
            for layer, paths in d.get('visuals', {}).items():
                curr_is_svg, curr_data = self._get_file_data(paths.get('curr'))
                old_is_svg, old_data = self._get_file_data(paths.get('old'))

                # DEDUPLICATION ENGINE: 
                # If the layer hasn't changed at all, tell JS to reuse the 'curr' data.
                # This cuts the file size in half for unmodified layers!
                if curr_is_svg and old_is_svg and curr_data and old_data and curr_data == old_data:
                    old_data = "SAME_AS_CURR"

                processed_visuals[layer] = {
                    "curr": {"is_svg": curr_is_svg, "data": curr_data},
                    "old": {"is_svg": old_is_svg, "data": old_data}
                }

            js_diffs.append({
                "name": d['name'],
                "status": d.get('status', 'Unknown'),
                "visuals": processed_visuals,
                "netlistDiff": d.get('netlist_diff', ''),
                "bomData": d.get('bom_data', {'curr': {}, 'old': {}}),
                "pcbLogicDiff": d.get('pcb_logic_diff', ''),
                "todos": d.get('todos', {'curr': [], 'old': []}),
                "dimensions": d.get('dimensions', {'curr': None, 'old': None}),
                "health": d.get('health', {'new': [], 'resolved': [], 'unresolved': []})
            })

        # Minify JSON by stripping all indentations and spaces between separators
        diff_json = json.dumps(js_diffs, separators=(',', ':'))
        
        # GZIP the entire payload, then Base64 encode it so it fits in a JS string safely
        compressed_bytes = gzip.compress(diff_json.encode('utf-8'))
        b64_compressed = base64.b64encode(compressed_bytes).decode('utf-8')
        
        colorblind_class = "colorblind-theme" if self.colorblind else ""

        # Load the template file
        template_path = os.path.join(os.path.dirname(__file__), "viewer_template.html")
        with open(template_path, 'r', encoding='utf-8') as f:
            html_content = f.read()

        # Inject Python variables safely
        html_content = html_content.replace('__COLORBLIND_CLASS__', colorblind_class)
        html_content = html_content.replace('__TARGET_NAME__', self.target_name)
        html_content = html_content.replace('__KICAD_VERSION__', self.kicad_version)
        html_content = html_content.replace('__DIFF_B64_GZIP__', b64_compressed)

        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
            
        webbrowser.open(pathlib.Path(html_path).as_uri())