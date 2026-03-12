import os
import tempfile
import webbrowser
import pathlib
import json

class DiffWindow:
    def __init__(self, diffs, summary_text):
        """
        diffs expects a list of dicts: 
        [{'name': '...', 'status': '...', 'visuals': {'LayerName': {'curr': '...', 'old': '...'}}, 'netlist_diff': '...', 'bom_diff': '...', 'todos': {'curr': [], 'old': []}}]
        """
        self.diffs = diffs
        self.summary_text = summary_text.replace('\n', '<br>')

    def Show(self):
        html_path = os.path.join(tempfile.gettempdir(), "kicad_diff_viewer.html")
        
        # Prepare data for JavaScript
        js_diffs = []
        for d in self.diffs:
            processed_visuals = {}
            # Loop through all pre-rendered layers
            for layer, paths in d.get('visuals', {}).items():
                curr_uri = pathlib.Path(paths['curr']).as_uri() if paths.get('curr') else ""
                old_uri = pathlib.Path(paths['old']).as_uri() if paths.get('old') else ""
                
                # Force multi-page PDFs to only show the first page (for Schematics).
                if curr_uri and paths.get('curr', '').lower().endswith('.pdf'):
                    curr_uri += "#page=1&navpanes=0&view=FitH"
                if old_uri and paths.get('old', '').lower().endswith('.pdf'):
                    old_uri += "#page=1&navpanes=0&view=FitH"
                
                processed_visuals[layer] = {"curr": curr_uri, "old": old_uri}

            js_diffs.append({
                "name": d['name'],
                "status": d.get('status', 'Unknown'),
                "visuals": processed_visuals,
                "netlistDiff": d.get('netlist_diff', ''),
                "bomDiff": d.get('bom_diff', ''),
                "todos": d.get('todos', {'curr': [], 'old': []})
            })

        diff_json = json.dumps(js_diffs)

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <title>KiCad Hardware Diff Viewer</title>
    <style>
        body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #1e1e1e; color: #eee; margin: 0; display: flex; height: 100vh; overflow: hidden; }}
        
        /* Sidebar Styles */
        #sidebar {{ width: 280px; background: #252526; border-right: 1px solid #333; display: flex; flex-direction: column; }}
        .sidebar-header {{ padding: 15px; background: #2d2d30; border-bottom: 1px solid #3e3e42; font-weight: bold; }}
        .file-list {{ flex: 1; overflow-y: auto; list-style: none; padding: 0; margin: 0; }}
        .file-item {{ padding: 12px 15px; border-bottom: 1px solid #333; cursor: pointer; transition: background 0.2s; }}
        .file-item:hover {{ background: #2a2d2e; }}
        .file-item.active {{ background: #37373d; border-left: 4px solid #007acc; }}
        .file-name {{ font-weight: bold; margin-bottom: 4px; word-break: break-all; }}
        .file-status {{ font-size: 0.85em; color: #aaa; }}
        
        /* Main Content Styles */
        #main-content {{ flex: 1; display: flex; flex-direction: column; background: #1e1e1e; }}
        #topbar {{ padding: 15px; background: #252526; border-bottom: 1px solid #333; display: flex; justify-content: space-between; align-items: center; }}
        
        .summary-box {{ padding: 10px 15px; background: #2d2d30; border-radius: 6px; font-size: 0.9em; max-width: 35%; max-height: 60px; overflow-y: auto; border: 1px solid #444; }}
        
        .controls-wrapper {{ display: flex; flex-direction: column; align-items: flex-end; gap: 10px; }}
        
        /* Selection Controls */
        .selection-row {{ display: flex; align-items: center; gap: 12px; }}
        .layer-selector {{ display: flex; align-items: center; gap: 8px; background: #333; padding: 4px 10px; border-radius: 4px; border: 1px solid #555; font-size: 13px; }}
        select {{ background: #444; color: white; border: 1px solid #666; padding: 3px 6px; border-radius: 3px; cursor: pointer; font-size: 13px; }}
        select:focus {{ outline: none; border-color: #007acc; }}

        .checkbox-label {{ display: flex; align-items: center; gap: 6px; cursor: pointer; font-size: 13px; user-select: none; }}
        .checkbox-label input {{ cursor: pointer; }}

        .view-toggle {{ display: flex; gap: 5px; }}
        .view-btn {{ padding: 6px 12px; font-size: 13px; font-weight: bold; cursor: pointer; background: #333; color: #ccc; border: 1px solid #555; border-radius: 4px; transition: 0.2s; }}
        .view-btn.active {{ background: #007acc; color: white; border-color: #007acc; }}
        .view-btn:hover:not(.active) {{ background: #444; }}
        
        .controls {{ display: flex; align-items: center; gap: 10px; }}
        button {{ padding: 8px 16px; font-size: 13px; font-weight: bold; cursor: pointer; background: #007acc; color: white; border: none; border-radius: 4px; transition: 0.2s; }}
        button:hover {{ background: #005999; }}
        button.btn-secondary {{ background: #555; }}
        button.btn-secondary:hover {{ background: #777; }}
        .status-indicator {{ font-size: 14px; color: #ccc; min-width: 220px; text-align: right; }}
        
        /* Document Viewers */
        #viewer-container {{ flex: 1; display: flex; justify-content: center; align-items: center; padding: 20px; overflow: hidden; position: relative; }}
        
        /* THE KEY FIX: Dark background for PCB area so white silk and copper colors pop */
        .board-viewer {{ 
            width: 100%; 
            height: 100%; 
            border: 1px solid #444; 
            background: #0a0a0a; 
            border-radius: 4px; 
            box-shadow: 0 4px 25px rgba(0,0,0,0.8); 
        }}
        
        .pdf-viewer {{ position: absolute; width: calc(100% - 40px); height: calc(100% - 40px); }}
        
        /* Copper Enhancement: Boosting contrast and saturation for "deep color" */
        img.board-viewer {{ 
            position: absolute; 
            width: 100%; 
            height: 100%; 
            object-fit: contain; 
            pointer-events: none; 
            filter: contrast(1.15) saturate(1.2);
        }} 

        .hidden {{ display: none !important; }}
        
        /* The Ghosting/Overlay Effect Class */
        .overlay-mode {{ 
            opacity: 0.8; 
            background: transparent;
            mix-blend-mode: screen; 
            pointer-events: none; 
            z-index: 10; 
        }}
        
        /* Silkscreen Specific styling: Screen blend drops the dark background */
        .silk-overlay {{ 
            z-index: 20; 
            opacity: 1.0; 
            background: transparent;
            mix-blend-mode: screen; 
            filter: brightness(1.1) contrast(1.2); /* Make silk even sharper */
        }}

        /* Text Diff Viewer & TODOs */
        #text-diff-container, #todos-container {{ flex: 1; padding: 20px; overflow-y: auto; background: #1e1e1e; font-family: 'Consolas', 'Courier New', monospace; font-size: 13px; white-space: pre-wrap; line-height: 1.5; }}
        .diff-line {{ padding: 0 5px; border-radius: 2px; }}

        /* TODO List Styles */
        .todos-wrapper {{ display: flex; gap: 20px; height: 100%; }}
        .todos-column {{ flex: 1; display: flex; flex-direction: column; background: #252526; border-radius: 6px; border: 1px solid #333; }}
        .todos-header {{ padding: 12px 15px; background: #2d2d30; border-bottom: 1px solid #3e3e42; font-weight: bold; font-size: 14px; border-radius: 6px 6px 0 0; }}
        .todo-list {{ list-style: none; padding: 15px; margin: 0; overflow-y: auto; flex: 1; }}
        .todo-item {{ padding: 12px 15px; margin-bottom: 10px; border-radius: 4px; background: #2d2d30; border-left: 4px solid #444; font-family: 'Segoe UI', sans-serif; box-shadow: 0 2px 4px rgba(0,0,0,0.2); }}
        .todo-item.todo-new {{ border-left-color: #4CAF50; }}
        .todo-item.todo-old {{ border-left-color: #FF9800; }}
        .todo-empty {{ color: #888; font-style: italic; padding: 10px 0; }}

        /* Pan & Zoom Wrapper for SVGs */
        #img-wrapper {{ width: 100%; height: 100%; display: flex; justify-content: center; align-items: center; transform-origin: 0 0; cursor: grab; position: relative; }}
        #img-wrapper:active {{ cursor: grabbing; }}
        
        .no-data-msg {{ color: #888; font-style: italic; font-size: 1.2em; }}
    </style>
</head>
<body>

    <div id="sidebar">
        <div class="sidebar-header">Project Files</div>
        <ul class="file-list" id="file-list">
            <!-- Populated by JS -->
        </ul>
    </div>

    <div id="main-content">
        <div id="topbar">
            <div class="summary-box">
                <strong>Change Summary:</strong><br>
                {self.summary_text}
            </div>
            
            <div class="controls-wrapper">
                <div class="selection-row">
                    <label id="silk-toggle-cont" class="checkbox-label hidden">
                        <input type="checkbox" id="silk-checkbox" onchange="toggleSilk(this.checked)"> Show Silk
                    </label>

                    <div id="layer-container" class="layer-selector hidden">
                        <span>Layer:</span>
                        <select id="layer-dropdown" onchange="changeLayer(this.value)">
                            <!-- Populated by JS -->
                        </select>
                    </div>

                    <div class="view-toggle" id="view-toggles">
                        <button class="view-btn active" id="tab-visual" onclick="switchTab('visual')">Visual View</button>
                        <button class="view-btn" id="tab-todos" onclick="switchTab('todos')">TODOs</button>
                        <button class="view-btn" id="tab-netlist" onclick="switchTab('netlist')">Logic (Netlist)</button>
                        <button class="view-btn" id="tab-bom" onclick="switchTab('bom')">BOM Diff</button>
                    </div>
                </div>
                
                <div class="controls">
                    <div class="status-indicator" id="status-text">Select a file...</div>
                    <button onclick="toggleOverlay()" id="btn-toggle-overlay" class="hidden btn-secondary">Overlay (O)</button>
                    <button onclick="toggleDiff()" id="btn-toggle-diff" class="hidden">Toggle Old / New (Space)</button>
                    <button onclick="resetTransform()" id="reset-btn" class="hidden btn-secondary">Reset Zoom</button>
                </div>
            </div>
        </div>
        
        <div id="viewer-container">
            <p id="no-selection" class="no-data-msg">No file selected.</p>
            <p id="no-old-msg" class="no-data-msg hidden">No previous Git commit found for this layer.</p>
            
            <div id="img-wrapper" class="hidden">
                <img id="old-img" class="board-viewer hidden" src="" />
                <img id="new-img" class="board-viewer hidden" src="" />
                <!-- Silk Overlays -->
                <img id="old-silk-img" class="board-viewer silk-overlay hidden" src="" />
                <img id="new-silk-img" class="board-viewer silk-overlay hidden" src="" />
            </div>

            <iframe id="old-pdf" class="board-viewer pdf-viewer hidden" src=""></iframe>
            <iframe id="new-pdf" class="board-viewer pdf-viewer hidden" src=""></iframe>
            <!-- Silk PDFs -->
            <iframe id="old-silk-pdf" class="board-viewer pdf-viewer silk-overlay hidden" src=""></iframe>
            <iframe id="new-silk-pdf" class="board-viewer pdf-viewer silk-overlay hidden" src=""></iframe>
        </div>
        
        <div id="text-diff-container" class="hidden">
            <!-- Populated by JS for netlist/bom -->
        </div>

        <div id="todos-container" class="hidden">
            <!-- Populated by JS for TODOs -->
        </div>
    </div>

    <script>
        const diffData = {diff_json};
        let activeIndex = -1;
        let showOld = false;
        let overlayMode = false;
        let currentTab = 'visual'; 
        let currentLayer = 'Default';
        let showSilk = false;

        const fileListEl = document.getElementById('file-list');
        const newImgEl = document.getElementById('new-img');
        const oldImgEl = document.getElementById('old-img');
        const newSilkImgEl = document.getElementById('new-silk-img');
        const oldSilkImgEl = document.getElementById('old-silk-img');

        const newPdfEl = document.getElementById('new-pdf');
        const oldPdfEl = document.getElementById('old-pdf');
        const newSilkPdfEl = document.getElementById('new-silk-pdf');
        const oldSilkPdfEl = document.getElementById('old-silk-pdf');

        const imgWrapper = document.getElementById('img-wrapper');
        const statusTextEl = document.getElementById('status-text');
        const noSelectionEl = document.getElementById('no-selection');
        const noOldMsgEl = document.getElementById('no-old-msg');
        const resetBtn = document.getElementById('reset-btn');
        const layerCont = document.getElementById('layer-container');
        const layerDrop = document.getElementById('layer-dropdown');
        const silkToggleCont = document.getElementById('silk-toggle-cont');
        const silkCheckbox = document.getElementById('silk-checkbox');
        
        const viewerContainer = document.getElementById('viewer-container');
        const textDiffContainer = document.getElementById('text-diff-container');
        const todosContainer = document.getElementById('todos-container');
        const viewToggles = document.getElementById('view-toggles');
        const btnToggleDiff = document.getElementById('btn-toggle-diff');
        const btnToggleOverlay = document.getElementById('btn-toggle-overlay');

        // --- Pan & Zoom Logic for SVGs ---
        let scale = 1, panning = false, pointX = 0, pointY = 0, start = {{ x: 0, y: 0 }};

        function setTransform() {{
            imgWrapper.style.transform = 'translate(' + pointX + 'px, ' + pointY + 'px) scale(' + scale + ')';
        }}

        function resetTransform() {{
            scale = 1; pointX = 0; pointY = 0;
            setTransform();
        }}

        viewerContainer.onmousedown = function (e) {{
            if (imgWrapper.classList.contains('hidden')) return; 
            e.preventDefault();
            start = {{ x: e.clientX - pointX, y: e.clientY - pointY }};
            panning = true;
        }};

        viewerContainer.onmouseup = function (e) {{ panning = false; }};
        viewerContainer.onmouseleave = function (e) {{ panning = false; }};

        viewerContainer.onmousemove = function (e) {{
            if (!panning || imgWrapper.classList.contains('hidden')) return;
            e.preventDefault();
            pointX = (e.clientX - start.x);
            pointY = (e.clientY - start.y);
            setTransform();
        }};

        viewerContainer.onwheel = function (e) {{
            if (imgWrapper.classList.contains('hidden')) return; 
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

        // --- Text/Data Formatters ---
        function escapeHtml(unsafe) {{
            return unsafe.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
        }}

        function formatDiff(diffText) {{
            if (!diffText || diffText.trim() === '') return '';
            return diffText.split('\\n').map(line => {{
                let safeLine = escapeHtml(line);
                if (safeLine.startsWith('+++') || safeLine.startsWith('---')) {{
                    return `<div class="diff-line" style="color: #999; font-weight: bold; margin-top: 10px;">${{safeLine}}</div>`;
                }}
                if (safeLine.startsWith('+')) {{
                    return `<div class="diff-line" style="color: #4CAF50; background-color: rgba(76, 175, 80, 0.15);">${{safeLine}}</div>`;
                }}
                if (safeLine.startsWith('-')) {{
                    return `<div class="diff-line" style="color: #F44336; background-color: rgba(244, 67, 54, 0.15);">${{safeLine}}</div>`;
                }}
                if (safeLine.startsWith('@@')) {{
                    return `<div class="diff-line" style="color: #00bcd4; font-weight: bold;">${{safeLine}}</div>`;
                }}
                return `<div class="diff-line" style="color: #ccc;">${{safeLine}}</div>`;
            }}).join('');
        }}

        // --- View Initialization ---
        function init() {{
            diffData.forEach((file, index) => {{
                const li = document.createElement('li');
                li.className = 'file-item';
                li.onclick = () => selectFile(index);
                
                let color = "#aaa";
                if (file.status === "Modified") color = "#F44336";
                else if (file.status === "New/Untracked") color = "#4CAF50";

                li.innerHTML = `
                    <div class="file-name">${{escapeHtml(file.name)}}</div>
                    <div class="file-status" style="color: ${{color}};">${{file.status}}</div>
                `;
                fileListEl.appendChild(li);
            }});

            if (diffData.length > 0) {{
                selectFile(0);
            }} else {{
                statusTextEl.innerText = "No files rendered.";
            }}
        }}

        function selectFile(index) {{
            activeIndex = index;
            const file = diffData[index];
            showOld = false; 
            overlayMode = false;
            
            // Populate Layer Dropdown
            layerDrop.innerHTML = '';
            const layers = Object.keys(file.visuals);
            layers.forEach(l => {{
                const opt = document.createElement('option');
                opt.value = l; opt.innerText = l;
                layerDrop.appendChild(opt);
            }});

            // Logic selection: Default to F.Cu if it exists for PCBs
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

        function toggleSilk(val) {{
            showSilk = val;
            renderView();
        }}

        function changeLayer(val) {{
            currentLayer = val;
            renderView();
        }}

        function switchTab(tab) {{
            currentTab = tab;
            document.getElementById('tab-visual').classList.toggle('active', tab === 'visual');
            document.getElementById('tab-todos').classList.toggle('active', tab === 'todos');
            document.getElementById('tab-netlist').classList.toggle('active', tab === 'netlist');
            document.getElementById('tab-bom').classList.toggle('active', tab === 'bom');
            renderView();
        }}

        function renderView() {{
            if (activeIndex < 0) return;
            const file = diffData[activeIndex];
            const visual = file.visuals[currentLayer] || {{}};
            const isSch = file.name.endsWith('.kicad_sch');
            
            // Logic for matching Silkscreen to Copper (Front to Front, Back to Back)
            let silkLayer = null;
            if (showSilk) {{
                if (currentLayer.startsWith('F.')) silkLayer = 'F.Silkscreen';
                else if (currentLayer.startsWith('B.')) silkLayer = 'B.Silkscreen';
            }}
            const silkVisual = silkLayer ? file.visuals[silkLayer] : null;

            // Show sch-specific tabs only for Schematics (netlist/bom), but TODOs for both!
            document.getElementById('tab-netlist').classList.toggle('hidden', !isSch);
            document.getElementById('tab-bom').classList.toggle('hidden', !isSch);

            noSelectionEl.classList.add('hidden');
            noOldMsgEl.classList.add('hidden');
            
            // Reset visibility
            viewerContainer.classList.add('hidden');
            textDiffContainer.classList.add('hidden');
            todosContainer.classList.add('hidden');

            // Handle Logic Text Views (Netlist / BOM)
            if (currentTab === 'netlist' || currentTab === 'bom') {{
                btnToggleDiff.classList.add('hidden');
                btnToggleOverlay.classList.add('hidden');
                resetBtn.classList.add('hidden');
                textDiffContainer.classList.remove('hidden');
                
                const diffContent = currentTab === 'netlist' ? file.netlistDiff : file.bomDiff;
                textDiffContainer.innerHTML = diffContent ? formatDiff(diffContent) : `<span style="color:#888;">No logic changes found.</span>`;
                statusTextEl.innerHTML = `Showing: <strong>${{currentTab === 'netlist' ? 'Netlist Text Diff' : 'BOM Text Diff'}}</strong>`;
                return;
            }}
            
            // Handle TODOs View
            if (currentTab === 'todos') {{
                btnToggleDiff.classList.add('hidden');
                btnToggleOverlay.classList.add('hidden');
                resetBtn.classList.add('hidden');
                todosContainer.classList.remove('hidden');
                
                const todos = file.todos || {{curr: [], old: []}};
                
                let html = '<div class="todos-wrapper">';
                
                // Old TODOs Column
                html += '<div class="todos-column"><div class="todos-header" style="color:#FF9800;">Previous TODOs</div><ul class="todo-list">';
                if (!todos.old || todos.old.length === 0) {{
                    html += '<li class="todo-empty">No TODOs found in the previous commit.</li>';
                }} else {{
                    todos.old.forEach(t => html += `<li class="todo-item todo-old">${{escapeHtml(t)}}</li>`);
                }}
                html += '</ul></div>';

                // Current TODOs Column
                html += '<div class="todos-column"><div class="todos-header" style="color:#4CAF50;">Current TODOs</div><ul class="todo-list">';
                if (!todos.curr || todos.curr.length === 0) {{
                    html += '<li class="todo-empty">No TODOs found in the working tree.</li>';
                }} else {{
                    todos.curr.forEach(t => html += `<li class="todo-item todo-new">${{escapeHtml(t)}}</li>`);
                }}
                html += '</ul></div>';
                
                html += '</div>';

                todosContainer.innerHTML = html;
                statusTextEl.innerHTML = `Showing: <strong>Design TODOs</strong>`;
                return;
            }}

            // --- Handle Visual View ---
            viewerContainer.classList.remove('hidden');
            
            if (visual.old && visual.curr) {{
                btnToggleDiff.classList.remove('hidden');
                btnToggleOverlay.classList.remove('hidden');
            }} else {{
                btnToggleDiff.classList.add('hidden');
                btnToggleOverlay.classList.add('hidden');
            }}
            
            const isPdf = (visual.curr && visual.curr.toLowerCase().includes('.pdf')) || 
                          (visual.old && visual.old.toLowerCase().includes('.pdf'));

            imgWrapper.classList.add('hidden');
            [newImgEl, oldImgEl, newSilkImgEl, oldSilkImgEl, newPdfEl, oldPdfEl, newSilkPdfEl, oldSilkPdfEl].forEach(e => e.classList.add('hidden'));

            if (isPdf) {{
                resetBtn.classList.add('hidden');
                newPdfEl.classList.remove('overlay-mode');
                
                if (overlayMode && visual.old && visual.curr) {{
                    oldPdfEl.src = visual.old; newPdfEl.src = visual.curr;
                    oldPdfEl.classList.remove('hidden'); newPdfEl.classList.remove('hidden');
                    newPdfEl.classList.add('overlay-mode');
                    statusTextEl.innerHTML = 'Showing: <strong style="color: #FF9800;">Overlay Mode</strong>';
                }} else if (showOld && visual.old) {{
                    oldPdfEl.src = visual.old; oldPdfEl.classList.remove('hidden');
                    if (silkVisual && silkVisual.old) {{ oldSilkPdfEl.src = silkVisual.old; oldSilkPdfEl.classList.remove('hidden'); }}
                    statusTextEl.innerHTML = 'Showing: <strong style="color: #F44336;">Old Version</strong>';
                }} else {{
                    if (visual.curr) {{
                        newPdfEl.src = visual.curr; newPdfEl.classList.remove('hidden');
                        if (silkVisual && silkVisual.curr) {{ newSilkPdfEl.src = silkVisual.curr; newSilkPdfEl.classList.remove('hidden'); }}
                        if (!visual.old && file.status !== "Unchanged") noOldMsgEl.classList.remove('hidden');
                    }}
                    statusTextEl.innerHTML = 'Showing: <strong style="color: #4CAF50;">Current Version</strong>';
                }}
            }} else {{
                resetBtn.classList.remove('hidden');
                imgWrapper.classList.remove('hidden');
                newImgEl.classList.remove('overlay-mode');
                
                if (overlayMode && visual.old && visual.curr) {{
                    oldImgEl.src = visual.old; newImgEl.src = visual.curr;
                    oldImgEl.classList.remove('hidden'); newImgEl.classList.remove('hidden');
                    newImgEl.classList.add('overlay-mode');
                    statusTextEl.innerHTML = 'Showing: <strong style="color: #FF9800;">Overlay Mode</strong>';
                }} else if (showOld && visual.old) {{
                    oldImgEl.src = visual.old; oldImgEl.classList.remove('hidden');
                    if (silkVisual && silkVisual.old) {{ oldSilkImgEl.src = silkVisual.old; oldSilkImgEl.classList.remove('hidden'); }}
                    statusTextEl.innerHTML = 'Showing: <strong style="color: #F44336;">Old Version</strong>';
                }} else {{
                    if (visual.curr) {{
                        newImgEl.src = visual.curr; newImgEl.classList.remove('hidden');
                        if (silkVisual && silkVisual.curr) {{ newSilkImgEl.src = silkVisual.curr; newSilkImgEl.classList.remove('hidden'); }}
                        if (!visual.old && file.status !== "Unchanged") noOldMsgEl.classList.remove('hidden');
                    }}
                    statusTextEl.innerHTML = 'Showing: <strong style="color: #4CAF50;">Current Version</strong>';
                }}
            }}
        }}

        function toggleOverlay() {{
            if (activeIndex < 0 || currentTab !== 'visual') return;
            const file = diffData[activeIndex];
            const visual = file.visuals[currentLayer];
            if (visual && visual.old && visual.curr) {{
                overlayMode = !overlayMode;
                if (overlayMode) showOld = false; 
                renderView();
            }}
        }}

        function toggleDiff() {{
            if (activeIndex < 0 || currentTab !== 'visual') return;
            const file = diffData[activeIndex];
            const visual = file.visuals[currentLayer];
            if (visual && visual.old) {{
                showOld = !showOld;
                overlayMode = false;
                renderView();
            }}
        }}

        document.addEventListener('keydown', function(event) {{
            if (event.code === 'Space') {{
                event.preventDefault(); toggleDiff();
            }} else if (event.code === 'KeyO') {{
                event.preventDefault(); toggleOverlay();
            }} else if (event.key >= '1' && event.key <= '9') {{
                const idx = parseInt(event.key) - 1;
                if (layerDrop.options[idx]) {{
                    layerDrop.selectedIndex = idx;
                    changeLayer(layerDrop.value);
                }}
            }} else if (event.code === 'KeyS') {{
                silkCheckbox.checked = !silkCheckbox.checked;
                toggleSilk(silkCheckbox.checked);
            }}
        }});

        init();
    </script>
</body>
</html>"""
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
            
        webbrowser.open(pathlib.Path(html_path).as_uri())