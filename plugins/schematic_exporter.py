import os
import glob
import subprocess
from .utils import CREATE_NO_WINDOW, find_kicad_cli


class SchematicExporter:
    """Wraps kicad-cli to export the schematic as SVG image(s).

    'kicad-cli sch export svg' emits one SVG per sheet page into an output
    directory. Files are written to <project>/docs (same place PCB renders go),
    keeping the project root clean. SVG is used rather than PNG because kicad-cli
    has no raster schematic export, and GitHub renders repo-hosted SVG in READMEs.
    """

    IMAGE_SUBDIR = "docs"

    def __init__(self, project_dir, settings=None, kicad_version=""):
        self.project_dir = project_dir
        self.settings = settings or {}
        self.kicad_version = kicad_version
        self.kicad_cli = find_kicad_cli()

    def _project_base(self):
        """Best guess at the project's base name (used to name the root sheet).

        Prefers the .kicad_pro name, then the .kicad_pcb name, so multi-sheet
        exports can be grouped by that prefix.
        """
        for pattern in ("*.kicad_pro", "*.kicad_pcb"):
            hits = glob.glob(os.path.join(self.project_dir, pattern))
            if hits:
                return os.path.splitext(os.path.basename(hits[0]))[0]
        return None

    def _find_root_schematic(self):
        """Returns the root .kicad_sch (the one matching the project name when
        possible, otherwise the first found), or None."""
        sch_files = glob.glob(os.path.join(self.project_dir, "*.kicad_sch"))
        if not sch_files:
            return None
        base = self._project_base()
        if base:
            for sch in sch_files:
                if os.path.splitext(os.path.basename(sch))[0] == base:
                    return sch
        return sch_files[0]

    def output_exists(self):
        """True if the root-sheet SVG has already been exported to docs/.

        Lets callers force a first-time export when the feature is enabled on a
        project whose schematic is committed/unchanged (so the change-detection
        gate would otherwise skip it forever)."""
        root_sch = self._find_root_schematic()
        if not root_sch:
            return False
        root_base = os.path.splitext(os.path.basename(root_sch))[0]
        return os.path.exists(os.path.join(self.project_dir, self.IMAGE_SUBDIR, f"{root_base}.svg"))

    def export_svgs(self):
        """Exports every schematic sheet to <project>/docs as SVG.

        Returns a list of repo-relative POSIX paths (root sheet first) for use as
        README image sources. Raises on failure.
        """
        root_sch = self._find_root_schematic()
        if not root_sch:
            raise FileNotFoundError("No .kicad_sch file found in the project folder.")

        out_dir = os.path.join(self.project_dir, self.IMAGE_SUBDIR)
        os.makedirs(out_dir, exist_ok=True)
        root_base = os.path.splitext(os.path.basename(root_sch))[0]

        cmd = [self.kicad_cli, "sch", "export", "svg",
               "--output", out_dir]
        if self.settings.get('schematic_bw', False):
            cmd.append("--black-and-white")
        if self.settings.get('schematic_no_sheet', False):
            cmd.append("--exclude-drawing-sheet")
        cmd.append(root_sch)

        # errors="replace": kicad-cli can emit non-UTF-8 bytes (locale text, tool
        # messages) on Windows, which would otherwise crash the decode.
        res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=300,
                             cwd=self.project_dir, creationflags=CREATE_NO_WINDOW)

        # kicad-cli names files <root>.svg (root sheet) plus <root>-<sheet>.svg
        # per sub-sheet. Collect whatever landed for this schematic's prefix.
        produced = sorted(glob.glob(os.path.join(out_dir, f"{root_base}*.svg")))
        if res.returncode != 0 or not produced:
            raise RuntimeError((res.stderr or "").strip() or (res.stdout or "").strip() or "Schematic SVG export failed.")

        # Order the exact root sheet first, sub-sheets after.
        root_svg = os.path.join(out_dir, f"{root_base}.svg")
        produced.sort(key=lambda p: (os.path.abspath(p) != os.path.abspath(root_svg), p))

        return [f"{self.IMAGE_SUBDIR}/{os.path.basename(p)}" for p in produced]
