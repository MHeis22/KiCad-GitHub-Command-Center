import os
import tempfile
import webbrowser
import pathlib
import json

class DiffWindow:
    def __init__(self, diffs, summary_text):
        """
        diffs expects a list of dicts: 
        [{'name': '...', 'status': '...', 'curr': '...', 'old': '...', 'netlist_diff': '...', 'bom_diff': '...'}]
        """
        self.diffs = diffs
        self.summary_text = summary_text.replace('\n', '<br>')

    def Show(self):
        html_path = os.path.join(tempfile.gettempdir(), "kicad_diff_viewer.html")
        
        # Prepare data for JavaScript
        js_diffs = []
        for d in self.diffs:
            curr_uri = pathlib.Path(d['curr']).as_uri() if d.get('curr') else ""
            old_uri = pathlib.Path(d['old']).as_uri() if d.get('old') else ""
            
            # Force multi-page PDFs to only show the first page (the actual isolated sheet).
            if curr_uri and d.get('curr', '').lower().endswith('.pdf'):
                curr_uri += "#page=1&navpanes=0&view=FitH"
            if old_uri and d.get('old', '').lower().endswith('.pdf'):
                old_uri += "#page=1&navpanes=0&view=FitH"

            js_diffs.append({
                "name": d['name'],
                "status": d.get('status', 'Unknown'),
                "currUri": curr_uri,
                "oldUri": old_uri,
                "netlistDiff": d.get('netlist_diff', ''),
                "bomDiff": d.get('bom_diff', '')
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
        
        .summary-box {{ padding: 10px 15px; background: #2d2d30; border-radius: 6px; font-size: 0.9em; max-width: 40%; max-height: 60px; overflow-y: auto; border: 1px solid #444; }}
        
        .controls-wrapper {{ display: flex; flex-direction: column; align-items: flex-end; gap: 10px; }}
        .view-toggle {{ display: flex; gap: 5px; }}
        .view-btn {{ padding: 6px 12px; font-size: 13px; font-weight: bold; cursor: pointer; background: #333; color: #ccc; border: 1px solid #555; border-radius: 4px; transition: 0.2s; }}
        .view-btn.active {{ background: #007acc; color: white; border-color: #007acc; }}
        .view-btn:hover:not(.active) {{ background: #444; }}
        
        .controls {{ display: flex; align-items: center; gap: 10px; }}
        button {{ padding: 10px 20px; font-size: 14px; font-weight: bold; cursor: pointer; background: #007acc; color: white; border: none; border-radius: 4px; transition: 0.2s; }}
        button:hover {{ background: #005999; }}
        button.btn-secondary {{ background: #555; }}
        button.btn-secondary:hover {{ background: #777; }}
        .status-indicator {{ font-size: 15px; min-width: 200px; text-align: right; }}
        
        /* Document Viewers */
        #viewer-container {{ flex: 1; display: flex; justify-content: center; align-items: center; padding: 20px; overflow: hidden; position: relative; }}
        .board-viewer {{ width: 100%; height: 100%; border: none; background: white; border-radius: 4px; box-shadow: 0 4px 15px rgba(0,0,0,0.5); }}
        
        /* Absolute positioning is used to stack visuals perfectly when overlaying */
        .pdf-viewer {{ position: absolute; width: calc(100% - 40px); height: calc(100% - 40px); }}
        img.board-viewer {{ position: absolute; width: 100%; height: 100%; object-fit: contain; pointer-events: none; }} 

        .hidden {{ display: none !important; }}
        
        /* The Ghosting/Overlay Effect Class */
        .overlay-mode {{ opacity: 0.8; mix-blend-mode: multiply; pointer-events: none; z-index: 10; }}
        
        /* Text Diff Viewer */
        #text-diff-container {{ flex: 1; padding: 20px; overflow-y: auto; background: #1e1e1e; font-family: 'Consolas', 'Courier New', monospace; font-size: 14px; white-space: pre-wrap; line-height: 1.5; }}
        .diff-line {{ padding: 0 5px; border-radius: 2px; }}

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
                <div class="view-toggle hidden" id="view-toggles">
                    <button class="view-btn active" id="tab-visual" onclick="switchTab('visual')">Visual View</button>
                    <button class="view-btn" id="tab-netlist" onclick="switchTab('netlist')">Logic (Netlist)</button>
                    <button class="view-btn" id="tab-bom" onclick="switchTab('bom')">BOM Diff</button>
                </div>
                
                <div class="controls">
                    <div class="status-indicator" id="status-text">Select a file to view...</div>
                    <button onclick="toggleOverlay()" id="btn-toggle-overlay" class="hidden btn-secondary">Overlay (O)</button>
                    <button onclick="toggleDiff()" id="btn-toggle-diff" class="hidden">Toggle Old / New (Space)</button>
                    <button onclick="resetTransform()" id="reset-btn" class="hidden btn-secondary">Reset Zoom</button>
                </div>
            </div>
        </div>
        
        <div id="viewer-container">
            <p id="no-selection" class="no-data-msg">No file selected.</p>
            <p id="no-old-msg" class="no-data-msg hidden">No previous Git commit found for this file. Displaying current version only.</p>
            
            <!-- Custom Pan/Zoom wrapper for PCBs (SVGs) -->
            <div id="img-wrapper" class="hidden">
                <img id="old-img" class="board-viewer hidden" src="" />
                <img id="new-img" class="board-viewer hidden" src="" />
            </div>

            <!-- Native iframe for Schematics (PDFs) to utilize browser's PDF engine -->
            <iframe id="old-pdf" class="board-viewer pdf-viewer hidden" src=""></iframe>
            <iframe id="new-pdf" class="board-viewer pdf-viewer hidden" src=""></iframe>
        </div>
        
        <div id="text-diff-container" class="hidden">
            <!-- Populated by JS -->
        </div>
    </div>

    <script>
        const diffData = {diff_json};
        let activeIndex = -1;
        let showOld = false;
        let overlayMode = false;
        let currentTab = 'visual'; // 'visual', 'netlist', 'bom'

        const fileListEl = document.getElementById('file-list');
        const newImgEl = document.getElementById('new-img');
        const oldImgEl = document.getElementById('old-img');
        const newPdfEl = document.getElementById('new-pdf');
        const oldPdfEl = document.getElementById('old-pdf');
        const imgWrapper = document.getElementById('img-wrapper');
        const statusTextEl = document.getElementById('status-text');
        const noSelectionEl = document.getElementById('no-selection');
        const noOldMsgEl = document.getElementById('no-old-msg');
        const resetBtn = document.getElementById('reset-btn');
        
        const viewerContainer = document.getElementById('viewer-container');
        const textDiffContainer = document.getElementById('text-diff-container');
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
            if (scale < 0.2) scale = 0.2;
            if (scale > 50) scale = 50;
            pointX = e.clientX - xs * scale;
            pointY = e.clientY - ys * scale;
            setTransform();
        }};

        // --- Text Diff Formatter ---
        function formatDiff(diffText) {{
            if (!diffText || diffText.trim() === '') return '';
            return diffText.split('\\n').map(line => {{
                let safeLine = line.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
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
                    <div class="file-name">${{file.name}}</div>
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

        function switchTab(tab) {{
            currentTab = tab;
            document.getElementById('tab-visual').classList.toggle('active', tab === 'visual');
            document.getElementById('tab-netlist').classList.toggle('active', tab === 'netlist');
            document.getElementById('tab-bom').classList.toggle('active', tab === 'bom');
            renderView();
        }}

        function selectFile(index) {{
            activeIndex = index;
            showOld = false; 
            overlayMode = false;
            currentTab = 'visual'; // reset to visual on file change
            switchTab('visual'); 
            resetTransform(); 
            
            document.querySelectorAll('.file-item').forEach((el, i) => {{
                el.classList.toggle('active', i === index);
            }});

            renderView();
        }}

        function renderView() {{
            if (activeIndex < 0) return;
            const file = diffData[activeIndex];
            const isSch = file.name.endsWith('.kicad_sch');
            
            // Toggle Tab Availability
            if (isSch) {{
                viewToggles.classList.remove('hidden');
            }} else {{
                viewToggles.classList.add('hidden');
            }}

            noSelectionEl.classList.add('hidden');
            noOldMsgEl.classList.add('hidden');

            // Handle Logical Text Views
            if (currentTab !== 'visual') {{
                viewerContainer.classList.add('hidden');
                btnToggleDiff.classList.add('hidden');
                btnToggleOverlay.classList.add('hidden');
                resetBtn.classList.add('hidden');
                textDiffContainer.classList.remove('hidden');
                
                const diffContent = currentTab === 'netlist' ? file.netlistDiff : file.bomDiff;
                
                if (diffContent && diffContent.trim() !== '') {{
                    textDiffContainer.innerHTML = formatDiff(diffContent);
                }} else {{
                    textDiffContainer.innerHTML = `<span style="color:#888;">No logical differences found in ${{currentTab.toUpperCase()}}, or file is untracked/unchanged.</span>`;
                }}
                
                statusTextEl.innerHTML = `Showing: <strong>${{currentTab === 'netlist' ? 'Netlist Text Diff' : 'BOM Text Diff'}}</strong>`;
                return;
            }}

            // Handle Visual View
            textDiffContainer.classList.add('hidden');
            viewerContainer.classList.remove('hidden');
            
            if (file.oldUri && file.currUri) {{
                btnToggleDiff.classList.remove('hidden');
                btnToggleOverlay.classList.remove('hidden');
            }} else {{
                btnToggleDiff.classList.add('hidden');
                btnToggleOverlay.classList.add('hidden');
            }}
            
            const isPdf = (file.currUri && file.currUri.toLowerCase().includes('.pdf')) || 
                          (file.oldUri && file.oldUri.toLowerCase().includes('.pdf'));

            imgWrapper.classList.add('hidden');
            newImgEl.classList.add('hidden');
            oldImgEl.classList.add('hidden');
            newPdfEl.classList.add('hidden');
            oldPdfEl.classList.add('hidden');

            if (isPdf) {{
                resetBtn.classList.add('hidden');
                newPdfEl.classList.remove('overlay-mode');
                
                if (overlayMode && file.oldUri && file.currUri) {{
                    oldPdfEl.src = file.oldUri;
                    newPdfEl.src = file.currUri;
                    oldPdfEl.classList.remove('hidden');
                    newPdfEl.classList.remove('hidden');
                    newPdfEl.classList.add('overlay-mode');
                    statusTextEl.innerHTML = 'Showing: <strong style="color: #FF9800;">Overlay Mode (Both)</strong>';
                }} else if (showOld && file.oldUri) {{
                    oldPdfEl.src = file.oldUri;
                    oldPdfEl.classList.remove('hidden');
                    statusTextEl.innerHTML = 'Showing: <strong style="color: #F44336;">Old Version (Git HEAD)</strong>';
                }} else {{
                    if (file.currUri) {{
                        newPdfEl.src = file.currUri;
                        newPdfEl.classList.remove('hidden');
                        if (!file.oldUri && file.status !== "Unchanged") {{
                            noOldMsgEl.classList.remove('hidden');
                        }}
                    }}
                    statusTextEl.innerHTML = 'Showing: <strong style="color: #4CAF50;">New Version (Current)</strong>';
                }}
            }} else {{
                resetBtn.classList.remove('hidden');
                imgWrapper.classList.remove('hidden');
                newImgEl.classList.remove('overlay-mode');
                
                if (overlayMode && file.oldUri && file.currUri) {{
                    oldImgEl.src = file.oldUri;
                    newImgEl.src = file.currUri;
                    oldImgEl.classList.remove('hidden');
                    newImgEl.classList.remove('hidden');
                    newImgEl.classList.add('overlay-mode');
                    statusTextEl.innerHTML = 'Showing: <strong style="color: #FF9800;">Overlay Mode (Both)</strong>';
                }} else if (showOld && file.oldUri) {{
                    oldImgEl.src = file.oldUri;
                    oldImgEl.classList.remove('hidden');
                    statusTextEl.innerHTML = 'Showing: <strong style="color: #F44336;">Old Version (Git HEAD)</strong>';
                }} else {{
                    if (file.currUri) {{
                        newImgEl.src = file.currUri;
                        newImgEl.classList.remove('hidden');
                        if (!file.oldUri && file.status !== "Unchanged") {{
                            noOldMsgEl.classList.remove('hidden');
                        }}
                    }}
                    statusTextEl.innerHTML = 'Showing: <strong style="color: #4CAF50;">New Version (Current)</strong>';
                }}
            }}
        }}

        function toggleOverlay() {{
            if (activeIndex < 0 || currentTab !== 'visual') return;
            const file = diffData[activeIndex];
            if (file.oldUri && file.currUri) {{
                overlayMode = !overlayMode;
                if (overlayMode) showOld = false; // reset standard toggle if overlay goes on
                renderView();
            }}
        }}

        function toggleDiff() {{
            if (activeIndex < 0 || currentTab !== 'visual') return;
            const file = diffData[activeIndex];
            if (file.oldUri) {{
                showOld = !showOld;
                overlayMode = false; // disable overlay if using spacebar toggle
                renderView();
            }}
        }}

        document.addEventListener('keydown', function(event) {{
            if (event.code === 'Space') {{
                event.preventDefault(); 
                toggleDiff();
            }} else if (event.code === 'KeyO') {{
                event.preventDefault();
                toggleOverlay();
            }}
        }});

        init();
    </script>
</body>
</html>"""
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
            
        webbrowser.open(pathlib.Path(html_path).as_uri())