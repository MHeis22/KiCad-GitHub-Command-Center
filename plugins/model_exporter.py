import os
import re
import glob
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from .utils import CREATE_NO_WINDOW, find_kicad_cli

# kicad-cli 'pcb render' was introduced in KiCad 9.0 (dev 8.99).
# STEP export ('pcb export step') has been available since KiCad 7.0.
RENDER_MIN_MAJOR = 9


def parse_major_version(version_str):
    """Extracts the leading major version integer from a kicad-cli version string.

    Accepts strings like '9.0.1', '8.0.5', '9.0.0-rc1', or the plugin's
    'Unknown KiCad Version' fallback. Returns 0 when no number can be parsed.
    """
    if not version_str:
        return 0
    match = re.search(r'(\d+)', version_str)
    return int(match.group(1)) if match else 0


def render_supported(version_str):
    """True when the installed KiCad is new enough for 'kicad-cli pcb render'."""
    return parse_major_version(version_str) >= RENDER_MIN_MAJOR


class Model3DExporter:
    """Wraps kicad-cli to produce a STEP model and a rendered PCB image.

    STEP files are written to <project>/3d and rendered PNGs to <project>/docs,
    keeping the project root clean (mirrors how gerbers go to production/).
    """

    STEP_SUBDIR = "3d"
    IMAGE_SUBDIR = "docs"

    def __init__(self, project_dir, settings=None, kicad_version=""):
        self.project_dir = project_dir
        self.settings = settings or {}
        self.kicad_version = kicad_version
        self.kicad_cli = find_kicad_cli()

    def _find_pcb(self):
        """Returns the first .kicad_pcb in the project, or None."""
        pcb_files = glob.glob(os.path.join(self.project_dir, "*.kicad_pcb"))
        return pcb_files[0] if pcb_files else None

    def step_output_exists(self):
        """True if the STEP model has already been exported to 3d/. Lets callers
        force a first-time export when the PCB is committed/unchanged."""
        pcb_file = self._find_pcb()
        if not pcb_file:
            return False
        base = os.path.splitext(os.path.basename(pcb_file))[0]
        return os.path.exists(os.path.join(self.project_dir, self.STEP_SUBDIR, f"{base}.step"))

    def renders_exist(self):
        """True only if a render PNG exists for every configured side. A single
        missing side counts as 'not done' so it gets generated."""
        pcb_file = self._find_pcb()
        if not pcb_file:
            return False
        base = os.path.splitext(os.path.basename(pcb_file))[0]
        for side in self._configured_sides():
            if not os.path.exists(os.path.join(self.project_dir, self.IMAGE_SUBDIR, f"{base}_{side}.png")):
                return False
        return True

    def export_step(self):
        """Exports a 3D STEP model to <project>/3d/<board>.step.

        Returns the absolute path of the generated file. Raises on failure.
        """
        pcb_file = self._find_pcb()
        if not pcb_file:
            raise FileNotFoundError("No .kicad_pcb file found in the project folder.")

        out_dir = os.path.join(self.project_dir, self.STEP_SUBDIR)
        os.makedirs(out_dir, exist_ok=True)
        base_name = os.path.splitext(os.path.basename(pcb_file))[0]
        out_path = os.path.join(out_dir, f"{base_name}.step")

        cmd = [self.kicad_cli, "pcb", "export", "step", "--force", "--output", out_path]

        # Geometric options (these are the only STEP-relevant toggles; a STEP
        # file carries no appearance/render settings).
        if self.settings.get('step_subst_models', True):
            cmd.append("--subst-models")
        if self.settings.get('step_no_dnp', False):
            cmd.append("--no-dnp")
        if self.settings.get('step_board_only', False):
            cmd.append("--board-only")

        cmd.append(pcb_file)

        # errors="replace": kicad-cli can emit non-UTF-8 bytes (locale text, tool
        # messages) on Windows, which would otherwise crash the decode.
        res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=300,
                             cwd=self.project_dir, creationflags=CREATE_NO_WINDOW)

        if res.returncode != 0 or not os.path.exists(out_path):
            raise RuntimeError((res.stderr or "").strip() or (res.stdout or "").strip() or "STEP export failed.")

        return out_path

    def _configured_sides(self):
        """Returns the list of sides to render based on settings."""
        if self.settings.get('render_both_sides', False):
            return ['top', 'bottom']
        return [self.settings.get('render_side', 'top')]

    def render_images(self):
        """Renders every configured side (single side, or top+bottom).

        Each side is an independent 'kicad-cli pcb render' subprocess writing a
        distinct output file, so multiple sides are rendered concurrently — a
        meaningful win for the slow ray-traced ('high') quality. Renders are
        attempted per-side so a failure on one still yields the others; raises
        only if every side failed. Input order (top before bottom) is preserved.
        """
        sides = self._configured_sides()
        results = {}
        errors = {}

        if len(sides) <= 1:
            for side in sides:
                try:
                    results[side] = self.render_image(side)
                except Exception as e:
                    errors[side] = e
        else:
            with ThreadPoolExecutor(max_workers=len(sides)) as executor:
                future_map = {executor.submit(self.render_image, s): s for s in sides}
                for future in as_completed(future_map):
                    side = future_map[future]
                    try:
                        results[side] = future.result()
                    except Exception as e:
                        errors[side] = e

        paths = [results[s] for s in sides if s in results]
        if not paths and errors:
            raise RuntimeError("; ".join(f"{s}: {e}" for s, e in errors.items()))
        return paths

    DIM_SUFFIX = "_dimensioned"

    def dimensioned_exists(self, side="top"):
        """True if the dimensioned drawing has already been produced."""
        pcb_file = self._find_pcb()
        if not pcb_file:
            return False
        base = os.path.splitext(os.path.basename(pcb_file))[0]
        return os.path.exists(os.path.join(self.project_dir, self.IMAGE_SUBDIR,
                                            f"{base}_{side}{self.DIM_SUFFIX}.png"))

    def render_dimensioned(self, side="top"):
        """Produces a technical drawing: a flat top/bottom render with automatic
        dimensions overlaid (board W/H, corner radius, mounting-hole diameters &
        positions). Returns the repo-relative POSIX path, or None if the board
        couldn't be parsed. Raises on render failure.

        Always uses a TRANSPARENT background — KiCad's opaque render draws a
        gradient floor larger than the board, which defeats the silhouette
        detection the overlay relies on. Basic quality keeps it a clean, flat
        drawing (and fast); the annotator composites it onto white."""
        from .dimension_annotator import DimensionAnnotator

        if not render_supported(self.kicad_version):
            raise RuntimeError(
                f"Dimensioned render requires KiCad 9.0+ (detected: {self.kicad_version or 'unknown'}).")
        pcb_file = self._find_pcb()
        if not pcb_file:
            raise FileNotFoundError("No .kicad_pcb file found in the project folder.")
        annot = DimensionAnnotator(self.project_dir)
        if not annot.available():
            return None

        out_dir = os.path.join(self.project_dir, self.IMAGE_SUBDIR)
        os.makedirs(out_dir, exist_ok=True)
        base = os.path.splitext(os.path.basename(pcb_file))[0]
        width = int(self.settings.get('render_width', 1600))
        height = int(self.settings.get('render_height', 1200))
        src = os.path.join(out_dir, f".{base}_{side}_dimsrc.png")   # throwaway transparent base

        cmd = [self.kicad_cli, "pcb", "render", "--output", src, "--side", side,
               "--background", "transparent", "--quality", "basic",
               "--width", str(width), "--height", str(height), pcb_file]
        res = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                             timeout=600, cwd=self.project_dir, creationflags=CREATE_NO_WINDOW)
        if res.returncode != 0 or not os.path.exists(src):
            raise RuntimeError((res.stderr or "").strip() or (res.stdout or "").strip()
                               or "Dimensioned base render failed.")

        out_name = f"{base}_{side}{self.DIM_SUFFIX}.png"
        # 2x-size dimension text, scaled with render width so it stays legible.
        text_px = max(48, int(width / 26))
        result = annot.annotate(src, side=side,
                                out_path=os.path.join(out_dir, out_name), text_px=text_px)
        try:
            os.remove(src)
        except OSError:
            pass
        return f"{self.IMAGE_SUBDIR}/{out_name}" if result else None

    def render_image(self, side=None):
        """Renders a PCB image to <project>/docs/<board>_<side>.png.

        Requires KiCad 9.0+. Returns the project-relative path (POSIX slashes,
        for use as a README image src). Raises on failure.
        """
        if not render_supported(self.kicad_version):
            raise RuntimeError(
                f"PCB image rendering requires KiCad 9.0+ (detected: {self.kicad_version or 'unknown'})."
            )

        pcb_file = self._find_pcb()
        if not pcb_file:
            raise FileNotFoundError("No .kicad_pcb file found in the project folder.")

        out_dir = os.path.join(self.project_dir, self.IMAGE_SUBDIR)
        os.makedirs(out_dir, exist_ok=True)
        base_name = os.path.splitext(os.path.basename(pcb_file))[0]
        side = side or self.settings.get('render_side', 'top')
        out_name = f"{base_name}_{side}.png"
        out_path = os.path.join(out_dir, out_name)

        quality = self.settings.get('render_quality', 'basic')
        background = self.settings.get('render_background', 'opaque')
        width = int(self.settings.get('render_width', 1600))
        height = int(self.settings.get('render_height', 1200))

        cmd = [self.kicad_cli, "pcb", "render",
               "--output", out_path,
               "--side", side,
               "--background", background,
               "--quality", quality,
               "--width", str(width),
               "--height", str(height),
               pcb_file]

        res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=600,
                             cwd=self.project_dir, creationflags=CREATE_NO_WINDOW)

        if res.returncode != 0 or not os.path.exists(out_path):
            raise RuntimeError((res.stderr or "").strip() or (res.stdout or "").strip() or "PCB render failed.")

        # README needs a repo-relative, forward-slash path.
        return f"{self.IMAGE_SUBDIR}/{out_name}"
