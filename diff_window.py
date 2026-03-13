import os
import tempfile
import webbrowser
import pathlib
import json
import base64

class DiffWindow:
    def __init__(self, diffs, summary_text, target_name="HEAD", kicad_version="Unknown KiCad Version"):
        """
        diffs expects a list of dicts: 
        [{'name': '...', 'status': '...', 'visuals': {...}, 'bom_data': {'curr':{}, 'old':{}}}]
        """
        self.diffs = diffs
        self.summary_text = summary_text.replace('\n', '<br>')
        self.target_name = target_name
        self.kicad_version = kicad_version

    def _get_data_uri(self, file_path):
        """Reads a file and returns a Base64 encoded Data URI for embedding."""
        if not file_path or not os.path.exists(file_path):
            return ""
        try:
            ext = file_path.lower().split('.')[-1]
            mime_type = "image/svg+xml" if ext == "svg" else ("application/pdf" if ext == "pdf" else "image/png")
            with open(file_path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode('utf-8')
            return f"data:{mime_type};base64,{encoded}"
        except Exception as e:
            print(f"Error encoding {file_path}: {e}")
            return ""

    def Show(self):
        html_path = os.path.join(tempfile.gettempdir(), "kicad_diff_viewer.html")
        
        # Prepare data for JavaScript
        js_diffs = []
        for d in self.diffs:
            processed_visuals = {}
            for layer, paths in d.get('visuals', {}).items():
                # Convert the local files directly into Base64 strings
                curr_uri = self._get_data_uri(paths.get('curr'))
                old_uri = self._get_data_uri(paths.get('old'))
                
                processed_visuals[layer] = {"curr": curr_uri, "old": old_uri}

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

        diff_json = json.dumps(js_diffs)

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>KiCad Hardware Diff Viewer</title>
    <style>
        :root {{
            --bg-main: #1e1e1e;
            --bg-sidebar: #252526;
            --bg-header: #2d2d30;
            --border-color: #333;
            --text-main: #eee;
            --text-muted: #aaa;
            --bg-hover: #2a2d2e;
            --bg-active: #37373d;
            --pcb-bg: #0a0a0a;
            --diff-bg: #1e1e1e;
            --diff-add: rgba(76, 175, 80, 0.15);
            --diff-del: rgba(244, 67, 54, 0.15);
            --diff-mod: rgba(255, 152, 0, 0.15);
        }}
        
        body.light-theme {{
            --bg-main: #f5f5f5;
            --bg-sidebar: #eaeaea;
            --bg-header: #dcdcdc;
            --border-color: #ccc;
            --text-main: #222;
            --text-muted: #666;
            --bg-hover: #dfdfdf;
            --bg-active: #cce4f7;
            --pcb-bg: #0a0a0a; 
            --diff-bg: #fafafa;
            --diff-add: #e6ffed;
            --diff-del: #ffeef0;
            --diff-mod: #fff5e6;
        }}

        /* --- Schematic Overlay & Version Watermark --- */
        #version-watermark {{
            position: fixed;
            bottom: 15px;
            right: 20px;
            font-size: 15px;
            color: var(--text-muted);
            opacity: 0.6;
            z-index: 1000;
            pointer-events: none;
            font-family: 'Consolas', 'Courier New', monospace;
        }}
        .sch-overlay-mode {{ opacity: 1.0; mix-blend-mode: screen; z-index: 10; background: transparent; }}
        .sch-diff-old {{ filter: invert(1) sepia(100%) saturate(500%) hue-rotate(-50deg) brightness(0.9) !important; mix-blend-mode: normal; }}
        .sch-diff-new {{ filter: invert(1) sepia(100%) saturate(500%) hue-rotate(80deg) brightness(0.9) !important; mix-blend-mode: normal; }}
        .sch-diff-container {{ background: #111 !important; }}

        body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: var(--bg-main); color: var(--text-main); margin: 0; display: flex; height: 100vh; overflow: hidden; transition: background 0.3s, color 0.3s; overscroll-behavior: none; }}
        
        #sidebar {{ width: 280px; background: var(--bg-sidebar); border-right: 1px solid var(--border-color); display: flex; flex-direction: column; transition: 0.3s; flex-shrink: 0; }}
        .sidebar-header {{ padding: 15px; background: var(--bg-header); border-bottom: 1px solid var(--border-color); font-weight: bold; transition: 0.3s; }}
        .file-list {{ flex: 1; overflow-y: auto; list-style: none; padding: 0; margin: 0; }}
        .file-item {{ padding: 12px 15px; border-bottom: 1px solid var(--border-color); cursor: pointer; transition: background 0.2s; }}
        .file-item:hover {{ background: var(--bg-hover); }}
        .file-item.active {{ background: var(--bg-active); border-left: 4px solid #007acc; }}
        .file-name {{ font-weight: bold; margin-bottom: 4px; word-break: break-all; }}
        .file-status {{ font-size: 0.85em; color: var(--text-muted); }}
        
        #main-content {{ flex: 1; display: flex; flex-direction: column; background: var(--bg-main); transition: 0.3s; overflow: hidden; }}
        #topbar {{ padding: 15px; background: var(--bg-sidebar); border-bottom: 1px solid var(--border-color); display: flex; justify-content: space-between; align-items: center; transition: 0.3s; }}
        
        .summary-box {{ padding: 10px 15px; background: var(--bg-header); border-radius: 6px; font-size: 0.9em; max-width: 35%; max-height: 60px; overflow-y: auto; border: 1px solid var(--border-color); transition: 0.3s; }}
        
        .controls-wrapper {{ display: flex; flex-direction: column; align-items: flex-end; gap: 10px; }}
        
        .selection-row {{ display: flex; align-items: center; gap: 12px; }}
        .layer-selector {{ display: flex; align-items: center; gap: 8px; background: var(--bg-header); padding: 4px 10px; border-radius: 4px; border: 1px solid var(--border-color); font-size: 13px; }}
        select {{ background: var(--bg-main); color: var(--text-main); border: 1px solid var(--border-color); padding: 3px 6px; border-radius: 3px; cursor: pointer; font-size: 13px; max-width: 150px; }}
        select:focus {{ outline: none; border-color: #007acc; }}

        .checkbox-label {{ display: flex; align-items: center; gap: 6px; cursor: pointer; font-size: 13px; user-select: none; }}
        .checkbox-label input {{ cursor: pointer; }}

        .view-toggle {{ display: flex; gap: 5px; }}
        .view-btn {{ padding: 6px 12px; font-size: 13px; font-weight: bold; cursor: pointer; background: var(--bg-main); color: var(--text-main); border: 1px solid var(--border-color); border-radius: 4px; transition: 0.2s; }}
        .view-btn.active {{ background: #007acc; color: white; border-color: #007acc; }}
        .view-btn:hover:not(.active) {{ background: var(--bg-hover); }}
        
        .controls {{ display: flex; align-items: center; gap: 10px; }}
        button {{ padding: 8px 16px; font-size: 13px; font-weight: bold; cursor: pointer; background: #007acc; color: white; border: none; border-radius: 4px; transition: 0.2s; }}
        button:hover {{ background: #005999; }}
        button.btn-secondary {{ background: #555; }}
        button.btn-secondary:hover {{ background: #777; }}
        .status-indicator {{ font-size: 14px; color: var(--text-muted); min-width: 220px; text-align: right; }}
        
        #viewer-container {{ flex: 1; display: flex; justify-content: center; align-items: center; padding: 20px; overflow: hidden; position: relative; touch-action: none; }}
        
        .viewer-absolute {{ position: absolute; top: 0; left: 0; width: 100%; height: 100%; display: flex; justify-content: center; align-items: center; pointer-events: none; }}
        .img-transform-wrapper {{ width: 100%; height: 100%; display: flex; justify-content: center; align-items: center; transform-origin: 0 0; position: absolute; }}
        
        .board-viewer {{ width: 100%; height: 100%; border: 1px solid #444; background: var(--pcb-bg); border-radius: 4px; box-shadow: 0 4px 25px rgba(0,0,0,0.8); }}
        .pdf-viewer {{ position: absolute; width: calc(100% - 40px); height: calc(100% - 40px); pointer-events: auto; }}
        
        img.board-viewer {{ position: absolute; width: 100%; height: 100%; object-fit: contain; pointer-events: none; filter: contrast(1.15) saturate(1.2); }} 
        .hidden {{ display: none !important; }}
        
        #swipe-slider-handle {{ position: absolute; top: 0; bottom: 0; left: 50%; width: 2px; background: #00bcd4; cursor: ew-resize; z-index: 100; touch-action: none; }}
        #swipe-slider-handle::after {{ content: '< >'; position: absolute; top: 50%; left: -14px; width: 30px; height: 30px; background: #00bcd4; color: #fff; border-radius: 50%; text-align: center; line-height: 30px; font-weight: bold; transform: translateY(-50%); font-family: sans-serif; box-shadow: 0 2px 5px rgba(0,0,0,0.5); pointer-events: none; }}
        .overlay-mode {{ opacity: 0.8; background: transparent; mix-blend-mode: screen; z-index: 10; }}
        .silk-overlay {{ z-index: 20; opacity: 1.0; background: transparent; mix-blend-mode: screen; filter: brightness(1.1) contrast(1.2); }}

        #text-diff-container, #todos-container, #health-container, #bom-container {{ flex: 1; padding: 20px; overflow-y: auto; background: var(--diff-bg); font-family: 'Consolas', 'Courier New', monospace; font-size: 13px; white-space: pre-wrap; line-height: 1.5; transition: 0.3s; }}
        .diff-line {{ padding: 0 5px; border-radius: 2px; }}
        .diff-header {{ color: var(--text-muted); font-weight: bold; margin-top: 10px; }}
        .diff-add {{ color: #4CAF50; background-color: var(--diff-add); }}
        .diff-del {{ color: #F44336; background-color: var(--diff-del); }}
        .diff-mod {{ color: #FF9800; background-color: var(--diff-mod); }}
        .diff-chunk {{ color: #00bcd4; font-weight: bold; }}
        .diff-normal {{ color: var(--text-main); }}

        /* --- Modern Grouped BOM --- */
        #bom-container {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; white-space: normal; padding: 20px; }}
        .bom-table-wrapper {{ height: 100%; overflow: auto; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-sidebar); }}
        .bom-table {{ width: 100%; border-collapse: collapse; text-align: left; }}
        .bom-table th, .bom-table td {{ padding: 10px 14px; border-bottom: 1px solid var(--border-color); font-size: 13px; vertical-align: top; }}
        .bom-table th {{ background: var(--bg-header); position: sticky; top: 0; z-index: 10; font-weight: bold; color: var(--text-muted); box-shadow: 0 1px 0 var(--border-color); }}
        .bom-row-add td {{ background-color: var(--diff-add); }}
        .bom-row-del td {{ background-color: var(--diff-del); }}
        .bom-row-mod td {{ background-color: var(--diff-mod); }}
        .bom-table tr:hover td {{ background-color: var(--bg-hover); }}
        .bom-table del {{ opacity: 0.6; color: #F44336; text-decoration: line-through; margin-right: 6px; }}
        .bom-table b {{ color: #4CAF50; font-weight: 600; }}

        /* Reusable Flex Columns (TODOs and Health) */
        .todos-wrapper {{ display: flex; gap: 20px; height: 100%; }}
        .todos-column {{ flex: 1; display: flex; flex-direction: column; background: var(--bg-sidebar); border-radius: 6px; border: 1px solid var(--border-color); transition: 0.3s; }}
        .todos-header {{ padding: 12px 15px; background: var(--bg-header); border-bottom: 1px solid var(--border-color); font-weight: bold; font-size: 14px; border-radius: 6px 6px 0 0; transition: 0.3s; }}
        .todo-list {{ list-style: none; padding: 15px; margin: 0; overflow-y: auto; flex: 1; }}
        .todo-item {{ padding: 12px 15px; margin-bottom: 10px; border-radius: 4px; background: var(--bg-header); border-left: 4px solid var(--text-muted); font-family: 'Segoe UI', sans-serif; box-shadow: 0 2px 4px rgba(0,0,0,0.2); transition: 0.3s; word-wrap: break-word; white-space: normal; }}
        .todo-item.todo-new {{ border-left-color: #4CAF50; }}
        .todo-item.todo-old {{ border-left-color: #FF9800; }}
        .todo-empty {{ color: var(--text-muted); font-style: italic; padding: 10px 0; }}

        .no-data-msg {{ color: var(--text-muted); font-style: italic; font-size: 1.2em; }}

        /* --- Mobile Responsiveness --- */
        @media (max-width: 768px) {{
            /* Allow vertical scrolling on mobile and remove fixed height */
            body {{ display: block; height: auto; overflow-y: auto; }}
            #main-content {{ display: block; height: auto; overflow: visible; }}
            
            #sidebar {{ width: 100%; height: 140px; flex-shrink: 0; border-right: none; border-bottom: 1px solid var(--border-color); }}
            #topbar {{ flex-direction: column; align-items: flex-start; gap: 10px; padding: 10px; flex-shrink: 0; }}
            
            .summary-box {{ max-width: 100%; width: 100%; max-height: max-content; box-sizing: border-box; }}
            .controls-wrapper {{ width: 100%; align-items: flex-start; gap: 8px; }}
            .selection-row {{ flex-wrap: wrap; gap: 8px; width: 100%; }}
            
            .view-toggle {{ flex-wrap: wrap; gap: 4px; width: 100%; }}
            .view-btn {{ flex: 1 1 auto; text-align: center; padding: 8px; }}
            
            .controls {{ flex-wrap: wrap; justify-content: flex-start; width: 100%; gap: 6px; }}
            .status-indicator {{ text-align: left; min-width: auto; width: 100%; margin-bottom: 4px; font-weight: bold; }}
            button {{ padding: 10px; flex: 1 1 auto; }}
            
            .todos-wrapper {{ flex-direction: column; height: auto; }}
            .todos-column {{ min-height: 250px; }}
            
            /* Give the viewer container a strong guaranteed height on mobile */
            #viewer-container {{ padding: 10px; height: 75vh; min-height: 450px; flex: none; }}
            #text-diff-container, #todos-container, #health-container, #bom-container {{ min-height: 60vh; flex: none; }}
            .layer-selector {{ flex: 1 1 auto; justify-content: space-between; }}
        }}
    </style>
</head>
<body>

    <div id="sidebar">
        <div class="sidebar-header">Project Files</div>
        <ul class="file-list" id="file-list"></ul>
    </div>

    <div id="main-content">
        <div id="topbar">
            <div class="summary-box">
                <strong>Current Diff:</strong><br>
                Comparing Local against: <span style="color:#00bcd4; font-weight:bold;">{self.target_name}</span>
            </div>
            
            <div class="controls-wrapper">
                <div class="selection-row">
                    <button class="btn-secondary" onclick="toggleTheme()" title="Shortcut: T">Toggle Theme</button>
                    <button class="btn-secondary" onclick="saveReport()">Save Report</button>
                    
                    <label id="silk-toggle-cont" class="checkbox-label hidden">
                        <input type="checkbox" id="silk-checkbox" onchange="toggleSilk(this.checked)"> Show Silk
                    </label>

                    <div id="layer-container" class="layer-selector hidden">
                        <span>Layer:</span>
                        <select id="layer-dropdown" onchange="changeLayer(this.value)"></select>
                    </div>

                    <div id="dim-container" class="layer-selector hidden" style="color: var(--text-main); font-weight: bold; font-family: monospace;"></div>

                    <div class="view-toggle" id="view-toggles">
                        <button class="view-btn active" id="tab-visual" onclick="switchTab('visual')">Visual View</button>
                        <button class="view-btn" id="tab-health" onclick="switchTab('health')">DRC</button>
                        <button class="view-btn" id="tab-todos" onclick="switchTab('todos')">TODOs</button>
                        <button class="view-btn" id="tab-pcb-logic" onclick="switchTab('pcb-logic')">Logic</button>
                        <button class="view-btn" id="tab-netlist" onclick="switchTab('netlist')">Netlist</button>
                        <button class="view-btn" id="tab-bom" onclick="switchTab('bom')">BOM</button>
                    </div>
                </div>
                
                <div class="controls">
                    <div class="status-indicator" id="status-text">Select a file...</div>
                    <button onclick="toggleSwipe()" id="btn-toggle-swipe" class="hidden btn-secondary">Swipe</button>
                    <button onclick="toggleOverlay()" id="btn-toggle-overlay" class="hidden btn-secondary">Overlay</button>
                    <button onclick="toggleDiff()" id="btn-toggle-diff" class="hidden">Toggle (Space)</button>
                    <button onclick="resetTransform()" id="reset-btn" class="hidden btn-secondary">Reset Zoom</button>
                </div>
            </div>
        </div>
        
        <div id="viewer-container">
            <p id="no-selection" class="no-data-msg">No file selected.</p>
            <p id="no-old-msg" class="no-data-msg hidden">No data found in <span class="target-name-val"></span> for this layer.</p>
            
            <div id="viewer-wrapper-old" class="viewer-absolute hidden">
                <div id="img-wrapper-old" class="img-transform-wrapper hidden">
                    <img id="old-img" class="board-viewer hidden" src="" />
                    <img id="old-silk-img" class="board-viewer silk-overlay hidden" src="" />
                </div>
                <iframe id="old-pdf" class="board-viewer pdf-viewer hidden" src=""></iframe>
                <iframe id="old-silk-pdf" class="board-viewer pdf-viewer silk-overlay hidden" src=""></iframe>
            </div>

            <div id="viewer-wrapper-new" class="viewer-absolute hidden">
                <div id="img-wrapper-new" class="img-transform-wrapper hidden">
                    <img id="new-img" class="board-viewer hidden" src="" />
                    <img id="new-silk-img" class="board-viewer silk-overlay hidden" src="" />
                </div>
                <iframe id="new-pdf" class="board-viewer pdf-viewer hidden" src=""></iframe>
                <iframe id="new-silk-pdf" class="board-viewer pdf-viewer silk-overlay hidden" src=""></iframe>
            </div>
            
            <div id="swipe-slider-handle" class="hidden"></div>
        </div>
        
        <div id="text-diff-container" class="hidden"></div>
        <div id="bom-container" class="hidden"></div>
        <div id="todos-container" class="hidden"></div>
        <div id="health-container" class="hidden"></div>
        
        <div id="version-watermark">{self.kicad_version}</div>
    </div>

    <script>
        const diffData = {diff_json};
        const targetName = "{self.target_name}";
        let activeIndex = -1;
        let showOld = false;
        let overlayMode = false;
        let swipeMode = false;
        let swipePos = 50;
        
        let currentTab = 'visual'; 
        let currentLayer = 'Default';
        let showSilk = false;

        const fileListEl = document.getElementById('file-list');
        const wrapperOld = document.getElementById('viewer-wrapper-old');
        const wrapperNew = document.getElementById('viewer-wrapper-new');
        const imgWrapperOld = document.getElementById('img-wrapper-old');
        const imgWrapperNew = document.getElementById('img-wrapper-new');
        
        const newImgEl = document.getElementById('new-img');
        const oldImgEl = document.getElementById('old-img');
        const newSilkImgEl = document.getElementById('new-silk-img');
        const oldSilkImgEl = document.getElementById('old-silk-img');
        const newPdfEl = document.getElementById('new-pdf');
        const oldPdfEl = document.getElementById('old-pdf');
        const newSilkPdfEl = document.getElementById('new-silk-pdf');
        const oldSilkPdfEl = document.getElementById('old-silk-pdf');

        const sliderHandle = document.getElementById('swipe-slider-handle');
        const statusTextEl = document.getElementById('status-text');
        const noSelectionEl = document.getElementById('no-selection');
        const noOldMsgEl = document.getElementById('no-old-msg');
        const resetBtn = document.getElementById('reset-btn');
        const layerCont = document.getElementById('layer-container');
        const layerDrop = document.getElementById('layer-dropdown');
        const dimContainer = document.getElementById('dim-container');
        const silkToggleCont = document.getElementById('silk-toggle-cont');
        const silkCheckbox = document.getElementById('silk-checkbox');
        
        const viewerContainer = document.getElementById('viewer-container');
        const textDiffContainer = document.getElementById('text-diff-container');
        const bomContainer = document.getElementById('bom-container');
        const todosContainer = document.getElementById('todos-container');
        const healthContainer = document.getElementById('health-container');
        const viewToggles = document.getElementById('view-toggles');
        
        const btnToggleDiff = document.getElementById('btn-toggle-diff');
        const btnToggleOverlay = document.getElementById('btn-toggle-overlay');
        const btnToggleSwipe = document.getElementById('btn-toggle-swipe');

        document.querySelectorAll('.target-name-val').forEach(el => el.innerText = targetName);

        function toggleTheme() {{ document.body.classList.toggle('light-theme'); }}

        function saveReport() {{
            const docHtml = '<!DOCTYPE html>\\n<html lang="en">' + document.documentElement.innerHTML + '</html>';
            const blob = new Blob([docHtml], {{ type: 'text/html' }});
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = 'kicad_diff_report.html';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        }}

        // === ZOOM AND PAN LOGIC (MOUSE & TOUCH) ===
        let scale = 1, panning = false, pointX = 0, pointY = 0, start = {{ x: 0, y: 0 }};
        let draggingSlider = false;
        
        let initialPinchDistance = null;
        let initialScale = 1;
        let initialPointX = 0;
        let initialPointY = 0;
        let pinchCenter = {{x: 0, y: 0}};

        function setTransform() {{
            const tf = 'translate(' + pointX + 'px, ' + pointY + 'px) scale(' + scale + ')';
            imgWrapperOld.style.transform = tf;
            imgWrapperNew.style.transform = tf;
        }}

        function resetTransform() {{
            scale = 1; pointX = 0; pointY = 0;
            setTransform();
        }}

        // Mouse Events
        sliderHandle.addEventListener('mousedown', (e) => {{
            draggingSlider = true;
            e.stopPropagation(); e.preventDefault();
        }});
        
        window.addEventListener('mouseup', () => {{ draggingSlider = false; panning = false; }});

        viewerContainer.onmousedown = function (e) {{
            if (imgWrapperOld.classList.contains('hidden') && imgWrapperNew.classList.contains('hidden')) return; 
            e.preventDefault();
            start = {{ x: e.clientX - pointX, y: e.clientY - pointY }};
            panning = true;
        }};

        viewerContainer.onmouseleave = function (e) {{ panning = false; draggingSlider = false; }};

        viewerContainer.onmousemove = function (e) {{
            if (draggingSlider && swipeMode) {{
                const rect = viewerContainer.getBoundingClientRect();
                swipePos = ((e.clientX - rect.left) / rect.width) * 100;
                swipePos = Math.max(0, Math.min(100, swipePos));
                sliderHandle.style.left = swipePos + '%';
                wrapperNew.style.clipPath = `polygon(0 0, ${{swipePos}}% 0, ${{swipePos}}% 100%, 0 100%)`;
                return;
            }}
            if (!panning || (imgWrapperOld.classList.contains('hidden') && imgWrapperNew.classList.contains('hidden'))) return;
            e.preventDefault();
            pointX = (e.clientX - start.x);
            pointY = (e.clientY - start.y);
            setTransform();
        }};

        viewerContainer.onwheel = function (e) {{
            if (imgWrapperOld.classList.contains('hidden') && imgWrapperNew.classList.contains('hidden')) return; 
            e.preventDefault();
            let xs = (e.clientX - pointX) / scale;
            let ys = (e.clientY - pointY) / scale;
            let delta = (e.wheelDelta ? e.wheelDelta : -e.deltaY);
            (delta > 0) ? (scale *= 1.2) : (scale /= 1.2);
            if (scale < 0.1) scale = 0.1;
            if (scale > 50) scale = 50;
            pointX = e.clientX - xs * scale;
            pointY = e.clientY - ys * scale;
            setTransform();
        }};

        // Touch Events (Mobile Pinch-to-Zoom & Pan)
        sliderHandle.addEventListener('touchstart', (e) => {{
            draggingSlider = true;
            e.stopPropagation();
        }}, {{passive: false}});

        viewerContainer.addEventListener('touchstart', function(e) {{
            if (imgWrapperOld.classList.contains('hidden') && imgWrapperNew.classList.contains('hidden')) return;
            if (e.touches.length === 1) {{
                start = {{ x: e.touches[0].clientX - pointX, y: e.touches[0].clientY - pointY }};
                panning = true;
            }} else if (e.touches.length === 2) {{
                panning = false;
                initialPinchDistance = Math.hypot(
                    e.touches[0].clientX - e.touches[1].clientX,
                    e.touches[0].clientY - e.touches[1].clientY
                );
                initialScale = scale;
                initialPointX = pointX;
                initialPointY = pointY;
                
                const rect = viewerContainer.getBoundingClientRect();
                pinchCenter = {{
                    x: (e.touches[0].clientX + e.touches[1].clientX) / 2 - rect.left,
                    y: (e.touches[0].clientY + e.touches[1].clientY) / 2 - rect.top
                }};
            }}
        }}, {{passive: false}});

        viewerContainer.addEventListener('touchmove', function(e) {{
            if (imgWrapperOld.classList.contains('hidden') && imgWrapperNew.classList.contains('hidden')) return;
            
            if (draggingSlider && swipeMode && e.touches.length === 1) {{
                e.preventDefault();
                const rect = viewerContainer.getBoundingClientRect();
                swipePos = ((e.touches[0].clientX - rect.left) / rect.width) * 100;
                swipePos = Math.max(0, Math.min(100, swipePos));
                sliderHandle.style.left = swipePos + '%';
                wrapperNew.style.clipPath = `polygon(0 0, ${{swipePos}}% 0, ${{swipePos}}% 100%, 0 100%)`;
                return;
            }}

            if (panning && e.touches.length === 1) {{
                e.preventDefault();
                pointX = e.touches[0].clientX - start.x;
                pointY = e.touches[0].clientY - start.y;
                setTransform();
            }} else if (e.touches.length === 2) {{
                e.preventDefault();
                const currentDistance = Math.hypot(
                    e.touches[0].clientX - e.touches[1].clientX,
                    e.touches[0].clientY - e.touches[1].clientY
                );
                const zoomFactor = currentDistance / initialPinchDistance;
                scale = initialScale * zoomFactor;
                if (scale < 0.1) scale = 0.1;
                if (scale > 50) scale = 50;

                // Adjust pointX/pointY to zoom around the center point between fingers
                pointX = pinchCenter.x - (pinchCenter.x - initialPointX) * zoomFactor;
                pointY = pinchCenter.y - (pinchCenter.y - initialPointY) * zoomFactor;

                setTransform();
            }}
        }}, {{passive: false}});

        viewerContainer.addEventListener('touchend', function(e) {{
            panning = false;
            initialPinchDistance = null;
            draggingSlider = false;
        }});


        // === DATA FORMATTING AND UI ===
        function escapeHtml(unsafe) {{ return unsafe ? unsafe.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;") : ''; }}

        function formatHealthItem(text) {{
            let safe = escapeHtml(text);
            safe = safe.replace(/\[ERROR\]/g, '<strong style="color:#F44336;">[ERROR]</strong>');
            safe = safe.replace(/\[WARNING\]/g, '<strong style="color:#FF9800;">[WARNING]</strong>');
            safe = safe.replace(/\[UNCONNECTED\]/g, '<strong style="color:#E91E63;">[UNCONNECTED]</strong>');
            return safe;
        }}

        function formatDiff(diffText) {{
            if (!diffText || diffText.trim() === '') return '';
            return diffText.split('\\n').map(line => {{
                let safeLine = escapeHtml(line);
                if (safeLine.startsWith('+++') || safeLine.startsWith('---')) return `<div class="diff-line diff-header">${{safeLine}}</div>`;
                if (safeLine.startsWith('+')) return `<div class="diff-line diff-add">${{safeLine}}</div>`;
                if (safeLine.startsWith('-')) return `<div class="diff-line diff-del">${{safeLine}}</div>`;
                if (safeLine.startsWith('M')) return `<div class="diff-line diff-mod">${{safeLine}}</div>`;
                if (safeLine.startsWith('@@')) return `<div class="diff-line diff-chunk">${{safeLine}}</div>`;
                return `<div class="diff-line diff-normal">${{safeLine}}</div>`;
            }}).join('');
        }}

        // Builds a highly readable grouped BOM showing exact diffs
        function renderGroupedBom(oldBom, currBom) {{
            const getSig = (item) => `${{item.val}}|${{item.fp}}|${{item.mpn}}|${{item.desc}}`;
            
            const oldGroups = {{}};
            for (const [ref, item] of Object.entries(oldBom)) {{
                const sig = getSig(item);
                if (!oldGroups[sig]) oldGroups[sig] = {{ ...item, refs: [], qty: 0 }};
                oldGroups[sig].refs.push(ref);
                oldGroups[sig].qty++;
            }}
            
            const currGroups = {{}};
            for (const [ref, item] of Object.entries(currBom)) {{
                const sig = getSig(item);
                if (!currGroups[sig]) currGroups[sig] = {{ ...item, refs: [], qty: 0 }};
                currGroups[sig].refs.push(ref);
                currGroups[sig].qty++;
            }}
            
            const allSigs = new Set([...Object.keys(oldGroups), ...Object.keys(currGroups)]);
            const sortedSigs = Array.from(allSigs).sort();
            
            if (sortedSigs.length === 0) {{
                return `<div class="no-data-msg" style="padding: 20px;">No components found (or all have 'Exclude from board' enabled).</div>`;
            }}

            let html = `<div class="bom-table-wrapper"><table class="bom-table">
                <thead><tr><th style="width:120px;">Status</th><th>Qty</th><th>References</th><th>Value</th><th>Footprint</th><th>MPN</th><th>Description</th></tr></thead>
                <tbody>`;
                
            for (const sig of sortedSigs) {{
                const o = oldGroups[sig];
                const c = currGroups[sig];
                
                if (c && !o) {{
                    html += `<tr class="bom-row-add"><td>➕ New Part</td><td>${{c.qty}}</td><td>${{c.refs.sort().join(', ')}}</td><td>${{escapeHtml(c.val)}}</td><td>${{escapeHtml(c.fp)}}</td><td>${{escapeHtml(c.mpn)}}</td><td>${{escapeHtml(c.desc)}}</td></tr>`;
                }} else if (o && !c) {{
                    html += `<tr class="bom-row-del"><td>➖ Removed</td><td><del>${{o.qty}}</del> 0</td><td><del>${{o.refs.sort().join(', ')}}</del></td><td>${{escapeHtml(o.val)}}</td><td>${{escapeHtml(o.fp)}}</td><td>${{escapeHtml(o.mpn)}}</td><td>${{escapeHtml(o.desc)}}</td></tr>`;
                }} else {{
                    const cRefsStr = c.refs.sort().join(', ');
                    const oRefsStr = o.refs.sort().join(', ');
                    
                    if (c.qty !== o.qty || cRefsStr !== oRefsStr) {{
                        let qtyHtml = c.qty !== o.qty ? `<del>${{o.qty}}</del> <b>${{c.qty}}</b>` : c.qty;
                        let refHtml = cRefsStr !== oRefsStr ? `<div style="font-size: 0.85em; color: #F44336; text-decoration: line-through;">${{oRefsStr}}</div><div style="color: #4CAF50;">${{cRefsStr}}</div>` : cRefsStr;
                        
                        html += `<tr class="bom-row-mod"><td>📝 Changed</td><td>${{qtyHtml}}</td><td>${{refHtml}}</td><td>${{escapeHtml(c.val)}}</td><td>${{escapeHtml(c.fp)}}</td><td>${{escapeHtml(c.mpn)}}</td><td>${{escapeHtml(c.desc)}}</td></tr>`;
                    }} else {{
                        html += `<tr><td>✓ Unchanged</td><td>${{c.qty}}</td><td>${{cRefsStr}}</td><td>${{escapeHtml(c.val)}}</td><td>${{escapeHtml(c.fp)}}</td><td>${{escapeHtml(c.mpn)}}</td><td>${{escapeHtml(c.desc)}}</td></tr>`;
                    }}
                }}
            }}
            html += `</tbody></table></div>`;
            return html;
        }}

        function init() {{
            fileListEl.innerHTML = '';
            diffData.forEach((file, index) => {{
                const li = document.createElement('li');
                li.className = 'file-item';
                li.onclick = () => selectFile(index);
                
                let color = "var(--text-muted)";
                if (file.status === "Modified") color = "#F44336";
                else if (file.status === "New/Untracked") color = "#4CAF50";
                else if (file.status === "Deleted") color = "#999999";

                li.innerHTML = `
                    <div class="file-name">${{escapeHtml(file.name)}}</div>
                    <div class="file-status" style="color: ${{color}};">${{file.status}}</div>
                `;
                fileListEl.appendChild(li);
            }});

            if (diffData.length > 0) {{ selectFile(0); }} 
            else {{ statusTextEl.innerText = "No files rendered."; }}
        }}

        function selectFile(index) {{
            activeIndex = index;
            const file = diffData[index];
            showOld = false; overlayMode = false; swipeMode = false;
            
            layerDrop.innerHTML = '';
            const layers = Object.keys(file.visuals);
            layers.forEach(l => {{
                const opt = document.createElement('option');
                opt.value = l; opt.innerText = l;
                layerDrop.appendChild(opt);
            }});

            if (file.name.endsWith('.kicad_pcb')) {{
                currentLayer = layers.includes('F.Cu') ? 'F.Cu' : layers[0];
                layerCont.classList.remove('hidden');
                silkToggleCont.classList.remove('hidden');
            }} else {{
                currentLayer = 'Default';
                layerCont.classList.add('hidden');
                silkToggleCont.classList.add('hidden');
            }}
            layerDrop.value = currentLayer;
            
            switchTab('visual'); 
            resetTransform(); 
            
            document.querySelectorAll('.file-item').forEach((el, i) => {{
                el.classList.toggle('active', i === index);
            }});
            renderView();
        }}

        function toggleSilk(val) {{ showSilk = val; renderView(); }}
        function changeLayer(val) {{ currentLayer = val; renderView(); }}

        function switchTab(tab) {{
            currentTab = tab;
            document.getElementById('tab-visual').classList.toggle('active', tab === 'visual');
            document.getElementById('tab-health').classList.toggle('active', tab === 'health');
            document.getElementById('tab-todos').classList.toggle('active', tab === 'todos');
            document.getElementById('tab-pcb-logic').classList.toggle('active', tab === 'pcb-logic');
            document.getElementById('tab-netlist').classList.toggle('active', tab === 'netlist');
            document.getElementById('tab-bom').classList.toggle('active', tab === 'bom');
            renderView();
        }}

        function renderView() {{
            if (activeIndex < 0) return;
            const file = diffData[activeIndex];
            const visual = file.visuals[currentLayer] || {{}};
            const isSch = file.name.endsWith('.kicad_sch');
            const isPcb = file.name.endsWith('.kicad_pcb');
            
            let silkLayer = null;
            if (showSilk) {{
                if (currentLayer.startsWith('F.')) silkLayer = 'F.Silkscreen';
                else if (currentLayer.startsWith('B.')) silkLayer = 'B.Silkscreen';
            }}
            const silkVisual = silkLayer ? file.visuals[silkLayer] : null;

            document.getElementById('tab-netlist').classList.toggle('hidden', !isSch);
            document.getElementById('tab-bom').classList.toggle('hidden', !isSch);
            document.getElementById('tab-pcb-logic').classList.remove('hidden');
            document.getElementById('tab-health').classList.toggle('hidden', isSch);

            noSelectionEl.classList.add('hidden');
            noOldMsgEl.classList.add('hidden');
            
            viewerContainer.classList.add('hidden');
            textDiffContainer.classList.add('hidden');
            bomContainer.classList.add('hidden');
            todosContainer.classList.add('hidden');
            healthContainer.classList.add('hidden');

            // --- Update PCB Dimensions ---
            if (isPcb && currentTab === 'visual') {{
                dimContainer.classList.remove('hidden');
                const dims = file.dimensions || {{curr: null, old: null}};
                
                if (dims.curr) {{
                    let dimText = `📐 ${{dims.curr.w}} x ${{dims.curr.h}} mm (${{dims.curr.area}} mm²)`;
                    if (dims.old && (dims.old.w !== dims.curr.w || dims.old.h !== dims.curr.h)) {{
                        dimText = `📐 <span style="text-decoration:line-through; color:#F44336; margin-right:4px;">${{dims.old.w}}x${{dims.old.h}}</span> ➔ <span style="color:#4CAF50; margin-left:4px;">${{dims.curr.w}} x ${{dims.curr.h}} mm</span>`;
                    }}
                    dimContainer.innerHTML = dimText;
                }} else {{
                    dimContainer.innerHTML = "📐 Dims: Unknown";
                }}
            }} else {{
                dimContainer.classList.add('hidden');
            }}

            // --- Modern BOM Diff ---
            if (currentTab === 'bom') {{
                btnToggleDiff.classList.add('hidden'); btnToggleOverlay.classList.add('hidden'); btnToggleSwipe.classList.add('hidden'); resetBtn.classList.add('hidden');
                bomContainer.classList.remove('hidden');
                
                const currBom = file.bomData.curr || {{}};
                const oldBom = file.bomData.old || {{}};
                
                bomContainer.innerHTML = renderGroupedBom(oldBom, currBom);
                statusTextEl.innerHTML = `Showing: <strong>Modern BOM</strong>`;
                return;
            }}

            // --- Logical Diffs (PCB or SCH Netlist) ---
            if (currentTab === 'netlist' || currentTab === 'pcb-logic') {{
                btnToggleDiff.classList.add('hidden'); btnToggleOverlay.classList.add('hidden'); btnToggleSwipe.classList.add('hidden'); resetBtn.classList.add('hidden');
                textDiffContainer.classList.remove('hidden');
                
                let diffContent = "";
                let tabName = "";
                if (currentTab === 'netlist') {{ diffContent = file.netlistDiff; tabName = "Netlist Text Diff"; }}
                else if (currentTab === 'pcb-logic') {{ diffContent = file.pcbLogicDiff; tabName = "Net/Component Changes"; }}

                textDiffContainer.innerHTML = diffContent ? formatDiff(diffContent) : `<span style="color:var(--text-muted);">No structural changes found.</span>`;
                statusTextEl.innerHTML = `Showing: <strong>${{tabName}}</strong>`;
                return;
            }}
            
            // --- DRC Violations ---
            if (currentTab === 'health') {{
                btnToggleDiff.classList.add('hidden'); btnToggleOverlay.classList.add('hidden'); btnToggleSwipe.classList.add('hidden'); resetBtn.classList.add('hidden');
                healthContainer.classList.remove('hidden');
                
                const health = file.health || {{new: [], resolved: [], unresolved: []}};
                
                let emptyChecksMsg = "";
                if (health.new.length === 0 && health.resolved.length === 0 && health.unresolved.length === 0) {{
                    emptyChecksMsg = "<div style='padding:15px; background:rgba(255,152,0,0.1); border:1px solid #FF9800; border-radius:4px; margin-bottom:15px;'><strong>Note:</strong> No violations detected, OR the <i>'Run DRC Check'</i> box was not checked before generating this view.</div>";
                }}

                let html = emptyChecksMsg + '<div class="todos-wrapper">';
                
                // Resolved
                html += '<div class="todos-column"><div class="todos-header" style="color:#4CAF50;">Resolved (Fixed)</div><ul class="todo-list">';
                if (health.resolved.length === 0) html += '<li class="todo-empty">No issues were fixed in this pass.</li>';
                else health.resolved.forEach(t => html += `<li class="todo-item todo-new">${{formatHealthItem(t)}}</li>`);
                html += '</ul></div>';

                // Unresolved
                html += '<div class="todos-column"><div class="todos-header" style="color:#FF9800;">Unresolved (Existing)</div><ul class="todo-list">';
                if (health.unresolved.length === 0) html += '<li class="todo-empty">No persistent issues.</li>';
                else health.unresolved.forEach(t => html += `<li class="todo-item todo-old">${{formatHealthItem(t)}}</li>`);
                html += '</ul></div>';
                
                // New
                html += '<div class="todos-column"><div class="todos-header" style="color:#F44336;">New Issues</div><ul class="todo-list">';
                if (health.new.length === 0) html += '<li class="todo-empty">No new issues introduced! 🎉</li>';
                else health.new.forEach(t => html += `<li class="todo-item" style="border-left-color: #F44336;">${{formatHealthItem(t)}}</li>`);
                html += '</ul></div>';

                html += '</div>';
                healthContainer.innerHTML = html;
                statusTextEl.innerHTML = `Showing: <strong>DRC Violations</strong>`;
                return;
            }}

            // --- TODOs ---
            if (currentTab === 'todos') {{
                btnToggleDiff.classList.add('hidden'); btnToggleOverlay.classList.add('hidden'); btnToggleSwipe.classList.add('hidden'); resetBtn.classList.add('hidden');
                todosContainer.classList.remove('hidden');
                
                const todos = file.todos || {{curr: [], old: []}};
                let html = '<div class="todos-wrapper">';
                
                html += '<div class="todos-column"><div class="todos-header" style="color:#FF9800;">' + targetName + ' TODOs</div><ul class="todo-list">';
                if (!todos.old || todos.old.length === 0) html += '<li class="todo-empty">No TODOs found in ' + targetName + '.</li>';
                else todos.old.forEach(t => html += `<li class="todo-item todo-old">${{escapeHtml(t)}}</li>`);
                html += '</ul></div>';

                html += '<div class="todos-column"><div class="todos-header" style="color:#4CAF50;">Local Changes TODOs</div><ul class="todo-list">';
                if (!todos.curr || todos.curr.length === 0) html += '<li class="todo-empty">No TODOs found in the working tree.</li>';
                else todos.curr.forEach(t => html += `<li class="todo-item todo-new">${{escapeHtml(t)}}</li>`);
                html += '</ul></div></div>';

                todosContainer.innerHTML = html;
                statusTextEl.innerHTML = `Showing: <strong>Design TODOs</strong>`;
                return;
            }}

            // --- Visual View ---
            viewerContainer.classList.remove('hidden');
            
            // Clear schematic overlay classes
            wrapperNew.classList.remove('overlay-mode', 'sch-overlay-mode');
            oldImgEl.classList.remove('sch-diff-old');
            newImgEl.classList.remove('sch-diff-new');
            viewerContainer.classList.remove('sch-diff-container');
            
            if (visual.old && visual.curr) {{ btnToggleDiff.classList.remove('hidden'); btnToggleOverlay.classList.remove('hidden'); btnToggleSwipe.classList.remove('hidden'); }} 
            else {{ btnToggleDiff.classList.add('hidden'); btnToggleOverlay.classList.add('hidden'); btnToggleSwipe.classList.add('hidden'); }}
            
            const isPdf = (visual.curr && visual.curr.startsWith('data:application/pdf')) || (visual.old && visual.old.startsWith('data:application/pdf'));

            if (isSch && !isPdf && !overlayMode) {{
                newImgEl.style.backgroundColor = '#ffffff'; oldImgEl.style.backgroundColor = '#ffffff';
                newImgEl.style.filter = 'none'; oldImgEl.style.filter = 'none';
            }} else {{
                newImgEl.style.backgroundColor = ''; oldImgEl.style.backgroundColor = '';
                newImgEl.style.filter = ''; oldImgEl.style.filter = '';
            }}

            imgWrapperOld.classList.add('hidden'); imgWrapperNew.classList.add('hidden');
            [newImgEl, oldImgEl, newSilkImgEl, oldSilkImgEl, newPdfEl, oldPdfEl, newSilkPdfEl, oldSilkPdfEl].forEach(e => e.classList.add('hidden'));

            if (isPdf) {{
                resetBtn.classList.add('hidden');
                if (visual.old) {{ oldPdfEl.src = visual.old; oldPdfEl.classList.remove('hidden'); }}
                if (visual.curr) {{ newPdfEl.src = visual.curr; newPdfEl.classList.remove('hidden'); }}
                if (silkVisual && silkVisual.old) {{ oldSilkPdfEl.src = silkVisual.old; oldSilkPdfEl.classList.remove('hidden'); }}
                if (silkVisual && silkVisual.curr) {{ newSilkPdfEl.src = silkVisual.curr; newSilkPdfEl.classList.remove('hidden'); }}
            }} else {{
                resetBtn.classList.remove('hidden');
                imgWrapperOld.classList.remove('hidden'); imgWrapperNew.classList.remove('hidden');
                if (visual.old) {{ oldImgEl.src = visual.old; oldImgEl.classList.remove('hidden'); }}
                if (visual.curr) {{ newImgEl.src = visual.curr; newImgEl.classList.remove('hidden'); }}
                if (silkVisual && silkVisual.old) {{ oldSilkImgEl.src = silkVisual.old; oldSilkImgEl.classList.remove('hidden'); }}
                if (silkVisual && silkVisual.curr) {{ newSilkImgEl.src = silkVisual.curr; newSilkImgEl.classList.remove('hidden'); }}
            }}

            wrapperOld.classList.add('hidden'); wrapperNew.classList.add('hidden');
            wrapperNew.style.clipPath = ''; sliderHandle.classList.add('hidden');

            if (swipeMode && visual.old && visual.curr) {{
                wrapperOld.classList.remove('hidden'); wrapperNew.classList.remove('hidden'); sliderHandle.classList.remove('hidden');
                wrapperNew.style.clipPath = `polygon(0 0, ${{swipePos}}% 0, ${{swipePos}}% 100%, 0 100%)`; sliderHandle.style.left = swipePos + '%';
                statusTextEl.innerHTML = 'Showing: <strong style="color: #00bcd4;">Swipe Mode</strong>';
            }} else if (overlayMode && visual.old && visual.curr) {{
                wrapperOld.classList.remove('hidden'); wrapperNew.classList.remove('hidden'); 
                
                if (isSch) {{
                    wrapperNew.classList.add('sch-overlay-mode');
                    oldImgEl.classList.add('sch-diff-old');
                    newImgEl.classList.add('sch-diff-new');
                    viewerContainer.classList.add('sch-diff-container');
                }} else {{
                    wrapperNew.classList.add('overlay-mode');
                }}
                
                statusTextEl.innerHTML = 'Showing: <strong style="color: #FF9800;">Overlay Mode</strong>';
            }} else if (showOld && visual.old) {{
                wrapperOld.classList.remove('hidden'); statusTextEl.innerHTML = 'Showing: <strong style="color: #F44336;">' + targetName + '</strong>';
            }} else {{
                if (visual.curr) {{
                    wrapperNew.classList.remove('hidden');
                    if (!visual.old && file.status !== "Unchanged") noOldMsgEl.classList.remove('hidden');
                }}
                statusTextEl.innerHTML = 'Showing: <strong style="color: #4CAF50;">Local Changes</strong>';
            }}
        }}

        function toggleSwipe() {{ if (activeIndex < 0 || currentTab !== 'visual') return; const visual = diffData[activeIndex].visuals[currentLayer]; if (visual && visual.old && visual.curr) {{ swipeMode = !swipeMode; if (swipeMode) {{ overlayMode = false; showOld = false; }} renderView(); }} }}
        function toggleOverlay() {{ if (activeIndex < 0 || currentTab !== 'visual') return; const visual = diffData[activeIndex].visuals[currentLayer]; if (visual && visual.old && visual.curr) {{ overlayMode = !overlayMode; if (overlayMode) {{ swipeMode = false; showOld = false; }} renderView(); }} }}
        function toggleDiff() {{ if (activeIndex < 0 || currentTab !== 'visual') return; const visual = diffData[activeIndex].visuals[currentLayer]; if (visual && visual.old) {{ showOld = !showOld; overlayMode = false; swipeMode = false; renderView(); }} }}

        document.addEventListener('keydown', function(event) {{
            if(document.activeElement.tagName === 'SELECT' || document.activeElement.tagName === 'INPUT') return;
            if (event.code === 'Space') {{ event.preventDefault(); toggleDiff(); }}
            else if (event.code === 'KeyO') {{ event.preventDefault(); toggleOverlay(); }}
            else if (event.code === 'KeyW') {{ event.preventDefault(); toggleSwipe(); }}
            else if (event.code === 'KeyT') {{ event.preventDefault(); toggleTheme(); }}
            else if (event.key >= '1' && event.key <= '9') {{ const idx = parseInt(event.key) - 1; if (layerDrop.options[idx]) {{ layerDrop.selectedIndex = idx; changeLayer(layerDrop.value); }} }}
            else if (event.code === 'KeyS') {{ silkCheckbox.checked = !silkCheckbox.checked; toggleSilk(silkCheckbox.checked); }}
        }});

        init();
    </script>
</body>
</html>"""
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
            
        webbrowser.open(pathlib.Path(html_path).as_uri())