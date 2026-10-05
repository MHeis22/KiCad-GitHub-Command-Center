import os
import re
import json
import shutil
import hashlib
import datetime
import subprocess
from .utils import CREATE_NO_WINDOW, find_kicad_cli

# Bundles every non-stock symbol, footprint and 3D model a project uses into
# <project>/libs and relinks the design to it through ${KIPRJMOD}, so the
# project opens correctly on a machine without the author's personal libraries.
#
# Two passes: scan() builds a BundlePlan without touching anything (shown to the
# user as a preview), apply() performs it. Schematics are edited as text on disk
# (eeschema must be closed); the board is edited through the live pcbnew object
# so the open PCB editor stays authoritative, then saved to disk by apply().

LIBS_SUBDIR = "libs"
MODELS_SUBDIR = "3dmodels"
BACKUP_DIR = ".bundle_backup"
BUNDLE_DESCR = "Bundled by GitHub Command Center"

# ${KICAD9_SYMBOL_DIR}, ${KICAD10_3DMODEL_DIR}, ... — KiCad's stock library variables.
_STOCK_VAR_RE = re.compile(r'^\$\{KICAD\d*_(?:SYMBOL|FOOTPRINT|3DMODEL)_DIR\}')
_STOCK_VAR_NAME_RE = re.compile(r'^KICAD\d*_(?:SYMBOL|FOOTPRINT|3DMODEL)_DIR$')
_VAR_RE = re.compile(r'\$\{([^}]+)\}|\$\(([^)]+)\)')
_LIB_ENTRY_RE = re.compile(
    r'\(lib\s+\(name\s+"((?:[^"\\]|\\.)*)"\)\s*\(type\s+"([^"]*)"\)\s*\(uri\s+"((?:[^"\\]|\\.)*)"\)')
_SCH_VERSION_RE = re.compile(r'\(version\s+(\d+)\)')
_DEFAULT_SYM_LIB_VERSION = 20241209


# ---------------------------------------------------------------------------
# S-expression text helpers
# ---------------------------------------------------------------------------

def _sexpr_end(text, start):
    """Returns the index just past the ')' that closes the '(' at text[start]."""
    depth = 0
    i = start
    n = len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            if c == '\\':
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("Unbalanced s-expression")


def _q(s):
    """Quotes a string the way KiCad writes it."""
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _unq(s):
    return s.replace('\\"', '"').replace('\\\\', '\\')


def _lib_symbols_span(sch_text):
    """(start, end) of the (lib_symbols ...) block, or None."""
    m = re.search(r'\(lib_symbols\b', sch_text)
    if not m:
        return None
    return m.start(), _sexpr_end(sch_text, m.start())


def _top_level_symbols(block_text, body_start):
    """Yields (name, start, end) for each direct (symbol "name" ...) child of a
    lib_symbols / kicad_symbol_lib block. Nested unit symbols are skipped
    because the scan resumes after each block's end."""
    pat = re.compile(r'\(symbol\s+"((?:[^"\\]|\\.)*)"')
    pos = body_start
    while True:
        m = pat.search(block_text, pos)
        if not m:
            return
        end = _sexpr_end(block_text, m.start())
        yield _unq(m.group(1)), m.start(), end
        pos = end


def _reindent(block, delta):
    """Shifts every line after the first by `delta` tabs (negative = dedent)."""
    lines = block.split('\n')
    out = [lines[0]]
    for ln in lines[1:]:
        if delta < 0:
            k = 0
            while k < -delta and ln.startswith('\t'):
                ln = ln[1:]
                k += 1
        else:
            ln = '\t' * delta + ln
        out.append(ln)
    return '\n'.join(out)


def _rename_symbol_block(block, new_name, old_base, new_base):
    """Renames a symbol block's own name, and its unit sub-symbols
    ("<base>_<unit>_<style>") when the base name changes."""
    block = re.sub(r'^\(symbol\s+"(?:[^"\\]|\\.)*"', '(symbol ' + _q(new_name), block, count=1)
    if old_base != new_base:
        block = re.sub(r'\(symbol\s+"' + re.escape(old_base) + r'(_\d+_\d+)"',
                       lambda m: '(symbol ' + _q(new_base + m.group(1)), block)
    return block


def _norm_ws(s):
    return re.sub(r'\s+', ' ', s).strip()


def _file_hash(path):
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 16), b''):
            h.update(chunk)
    return h.hexdigest()


def _read(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def _write(path, text):
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)


def _safe_part(s):
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', s).strip('_') or "lib"


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

class BundlePlan:
    """Everything scan() found. Each item records where it came from and the
    name it will have in the project library."""

    def __init__(self, nickname):
        self.nickname = nickname
        # (lib, name) -> {'new_name', 'block', 'origin'}
        self.symbols = {}
        # (lib, name) -> {'new_name', 'src' (path or None), 'origin'}
        self.footprints = {}
        # original model string -> {'src', 'dest_name'}
        self.models = {}
        self.warnings = []
        self.skipped_stock = {'symbols': 0, 'footprints': 0}

    def is_empty(self):
        return not (self.symbols or self.footprints or self.models)


class ProjectBundler:

    def __init__(self, project_dir, board=None, include_stock=False):
        self.project_dir = os.path.abspath(project_dir)
        self.board = board
        self.include_stock = include_stock

        self.pcb_file = board.GetFileName() if board is not None else None
        if not self.pcb_file:
            pcbs = [f for f in os.listdir(self.project_dir) if f.endswith('.kicad_pcb')]
            self.pcb_file = os.path.join(self.project_dir, pcbs[0]) if pcbs else None
        base = os.path.splitext(os.path.basename(self.pcb_file or self.project_dir))[0]
        self.project_name = base
        self.nickname = _safe_part(base)

        self.libs_dir = os.path.join(self.project_dir, LIBS_SUBDIR)
        self.sym_lib_path = os.path.join(self.libs_dir, f"{self.nickname}.kicad_sym")
        self.fp_lib_path = os.path.join(self.libs_dir, f"{self.nickname}.pretty")
        self.models_dir = os.path.join(self.libs_dir, MODELS_SUBDIR)

        self.env = self._build_env()
        self.sym_table = self._load_tables('sym-lib-table')
        self.fp_table = self._load_tables('fp-lib-table')
        self.stock_dirs = self._stock_dirs()

    # ----- environment & library tables ------------------------------------

    def _user_settings_dir(self):
        try:
            import pcbnew
            return pcbnew.SETTINGS_MANAGER.GetUserSettingsPath()
        except Exception:
            pass
        base = os.environ.get('APPDATA') or os.path.expanduser('~/.config')
        root = os.path.join(base, 'kicad')
        try:
            versions = sorted((d for d in os.listdir(root) if re.match(r'^\d+\.\d+$', d)),
                              key=lambda v: [int(x) for x in v.split('.')])
            return os.path.join(root, versions[-1]) if versions else root
        except OSError:
            return root

    def _build_env(self):
        """Path variables as KiCad would resolve them. Inside KiCad they are
        already in os.environ; kicad_common.json covers headless runs."""
        env = {}
        try:
            with open(os.path.join(self._user_settings_dir(), 'kicad_common.json'), encoding='utf-8') as f:
                env.update((json.load(f).get('environment') or {}).get('vars') or {})
        except Exception:
            pass
        env.update(os.environ)
        env['KIPRJMOD'] = self.project_dir
        return env

    def _expand(self, raw):
        def sub(m):
            name = m.group(1) or m.group(2)
            return self.env.get(name, m.group(0))
        path = _VAR_RE.sub(sub, raw)
        if '${' in path or '$(' in path:
            return None
        path = re.sub(r'(?<!^)[\\/]{2,}', '/', path)  # "C://Users//x" -> "C:/Users/x"
        if not os.path.isabs(path):
            path = os.path.join(self.project_dir, path)
        return os.path.normpath(path)

    def _load_tables(self, name):
        """nickname -> raw uri. Project table entries override global ones."""
        table = {}
        for path in (os.path.join(self._user_settings_dir(), name), os.path.join(self.project_dir, name)):
            try:
                text = _read(path)
            except OSError:
                continue
            for m in _LIB_ENTRY_RE.finditer(text):
                table[_unq(m.group(1))] = _unq(m.group(3))
        return table

    def _stock_dirs(self):
        dirs = set()
        for k, v in self.env.items():
            if _STOCK_VAR_NAME_RE.match(k) and v:
                dirs.add(os.path.normcase(os.path.normpath(v)))
        return dirs

    def _under(self, path, root):
        try:
            return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
        except ValueError:
            return False

    def _classify(self, raw):
        """'stock' | 'project' | 'custom' for a library URI or model path."""
        if _STOCK_VAR_RE.match(raw):
            return 'stock'
        if raw.startswith('${KIPRJMOD}') or raw.startswith('kicad-embed://'):
            return 'project'
        path = self._expand(raw)
        if path:
            if self._under(path, self.project_dir):
                return 'project'
            if any(self._under(path, d) for d in self.stock_dirs):
                return 'stock'
        return 'custom'

    # ----- project files ----------------------------------------------------

    def schematic_files(self):
        """Root schematic plus every sub-sheet, following Sheetfile references."""
        root = os.path.splitext(self.pcb_file)[0] + '.kicad_sch' if self.pcb_file else None
        if not root or not os.path.exists(root):
            return sorted(os.path.join(self.project_dir, f) for f in os.listdir(self.project_dir)
                          if f.endswith('.kicad_sch'))
        seen, stack = [], [os.path.normpath(root)]
        while stack:
            path = stack.pop()
            if path in seen or not os.path.exists(path):
                continue
            seen.append(path)
            for m in re.finditer(r'\(property\s+"Sheetfile"\s+"((?:[^"\\]|\\.)*)"', _read(path)):
                stack.append(os.path.normpath(os.path.join(os.path.dirname(path), _unq(m.group(1)))))
        return seen

    def locked_schematics(self):
        """Schematics that eeschema currently has open (KiCad lock file present)."""
        return [p for p in self.schematic_files()
                if os.path.exists(os.path.join(os.path.dirname(p), '~' + os.path.basename(p) + '.lck'))]

    # ----- scan -------------------------------------------------------------

    def scan(self):
        plan = BundlePlan(self.nickname)
        sch_files = self.schematic_files()
        sch_texts = {p: _read(p) for p in sch_files}

        # Schematic cache: "lib:name" -> symbol block (first one wins).
        cache = {}
        lib_ids = []
        fp_fields = set()
        for text in sch_texts.values():
            span = _lib_symbols_span(text)
            if span:
                for name, s, e in _top_level_symbols(text, span[0] + 1):
                    cache.setdefault(name, text[s:e])
            for m in re.finditer(r'\(lib_id\s+"((?:[^"\\]|\\.)*)"\)', text):
                lid = _unq(m.group(1))
                if ':' in lid and lid not in lib_ids:
                    lib_ids.append(lid)
            for m in re.finditer(r'\(property\s+"Footprint"\s+"((?:[^"\\]|\\.)*)"', text):
                v = _unq(m.group(1))
                if ':' in v:
                    fp_fields.add(v)

        self._scan_symbols(plan, lib_ids, cache)
        board_fps = self._scan_footprints(plan, fp_fields)
        self._scan_models(plan, board_fps)
        return plan

    def _wants(self, lib, table, plan, kind):
        """True if items from this library nickname should be bundled."""
        if lib == self.nickname:
            return False  # already bundled
        raw = table.get(lib)
        if raw is None:
            plan.warnings.append(f"{kind} library '{lib}' is not in any library table; "
                                 f"its parts are taken from the copies stored in the design where possible.")
            return True
        cls = self._classify(raw)
        if cls == 'project':
            return False
        if cls == 'stock' and not self.include_stock:
            return 'stock'
        return True

    def _scan_symbols(self, plan, lib_ids, cache):
        used_names = {}  # new_name -> normalized block
        decided = {}
        for lid in lib_ids:
            lib, name = lid.split(':', 1)
            if lib not in decided:
                decided[lib] = self._wants(lib, self.sym_table, plan, "Symbol")
            want = decided[lib]
            if want == 'stock':
                plan.skipped_stock['symbols'] += 1
                continue
            if not want:
                continue

            block, origin = cache.get(lid), "schematic cache"
            if block is None:
                block, origin = self._symbol_from_library(lib, name), "library file"
            if block is None:
                plan.warnings.append(f"Symbol '{lid}' not found in the schematic cache or its library; skipped.")
                continue

            new_name = name
            norm = _norm_ws(_rename_symbol_block(block, name, name, name))
            if new_name in used_names and used_names[new_name] != norm:
                new_name = f"{name}_{_safe_part(lib)}"
                plan.warnings.append(f"Symbol name '{name}' is used by several libraries; "
                                     f"'{lid}' becomes '{self.nickname}:{new_name}'.")
            used_names.setdefault(new_name, norm)
            plan.symbols[(lib, name)] = {'new_name': new_name, 'block': block, 'origin': origin}

    def _symbol_from_library(self, lib, name):
        path = self._expand(self.sym_table.get(lib, '')) if lib in self.sym_table else None
        if not path or not os.path.isfile(path):
            return None
        text = _read(path)
        for n, s, e in _top_level_symbols(text, 1):
            if n == name:
                block = _reindent(text[s:e], 1)
                if '(extends' in block:
                    return None  # derived symbol: needs its parent flattened — rely on the cache
                return block
        return None

    def _scan_footprints(self, plan, fp_fields):
        """Collects footprints from the board and from schematic Footprint fields.
        Returns the board footprints (for the model scan)."""
        board_fps = []
        refs = {}
        if self.board is not None:
            for fp in self.board.GetFootprints():
                fpid = fp.GetFPID()
                key = (str(fpid.GetLibNickname()), str(fpid.GetLibItemName()))
                board_fps.append(fp)
                refs.setdefault(key, fp)
        keys = list(refs.keys()) + [tuple(v.split(':', 1)) for v in sorted(fp_fields)]

        decided = {}
        used_names = {}
        for lib, name in keys:
            if (lib, name) in plan.footprints or not lib:
                continue
            if lib not in decided:
                decided[lib] = self._wants(lib, self.fp_table, plan, "Footprint")
            want = decided[lib]
            if want == 'stock':
                plan.skipped_stock['footprints'] += 1
                continue
            if not want:
                continue

            src = None
            lib_dir = self._expand(self.fp_table[lib]) if lib in self.fp_table else None
            if lib_dir and os.path.isfile(os.path.join(lib_dir, name + '.kicad_mod')):
                src = os.path.join(lib_dir, name + '.kicad_mod')
            if src is None and (lib, name) not in refs:
                plan.warnings.append(f"Footprint '{lib}:{name}' (schematic field) not found; skipped.")
                continue
            origin = "library file" if src else "board copy"

            new_name = name
            ident = _file_hash(src) if src else f"board:{lib}:{name}"
            if new_name in used_names and used_names[new_name] != ident:
                new_name = f"{name}_{_safe_part(lib)}"
                plan.warnings.append(f"Footprint name '{name}' is used by several libraries; "
                                     f"'{lib}:{name}' becomes '{self.nickname}:{new_name}'.")
            used_names.setdefault(new_name, ident)
            plan.footprints[(lib, name)] = {'new_name': new_name, 'src': src, 'origin': origin,
                                            'board_fp': refs.get((lib, name))}
        return board_fps

    def _scan_models(self, plan, board_fps):
        raw_models = []
        for fp in board_fps:
            for m in fp.Models():
                raw_models.append(m.m_Filename)
        for info in plan.footprints.values():
            if info['src']:
                for m in re.finditer(r'\(model\s+"((?:[^"\\]|\\.)*)"', _read(info['src'])):
                    raw_models.append(_unq(m.group(1)))

        dest_names = {}  # lower-case dest name -> source hash
        for raw in raw_models:
            if not raw or raw in plan.models or self._classify(raw) != 'custom':
                continue
            src = self._expand(raw)
            if not src or not os.path.isfile(src):
                plan.warnings.append(f"3D model not found: {raw}")
                continue
            dest = os.path.basename(src)
            h = _file_hash(src)
            stem, ext = os.path.splitext(dest)
            k = 1
            while dest.lower() in dest_names and dest_names[dest.lower()] != h:
                dest = f"{stem}_{k}{ext}"
                k += 1
            dest_names[dest.lower()] = h
            plan.models[raw] = {'src': src, 'dest_name': dest}

    # ----- apply ------------------------------------------------------------

    def new_model_path(self, dest_name):
        return f"${{KIPRJMOD}}/{LIBS_SUBDIR}/{MODELS_SUBDIR}/{dest_name}"

    def apply(self, plan, progress=None):
        """Performs the bundle. Returns a summary dict. Board changes are made
        on the live board and saved to disk."""
        def step(msg):
            if progress:
                progress(msg)

        locked = self.locked_schematics()
        if locked:
            raise RuntimeError("Close the Schematic Editor first (open: " +
                               ", ".join(os.path.basename(p) for p in locked) + ").")

        step("Backing up files...")
        backup = self._backup()

        os.makedirs(self.libs_dir, exist_ok=True)
        fp_map = {f"{lib}:{name}": f"{self.nickname}:{i['new_name']}" for (lib, name), i in plan.footprints.items()}
        model_map = {raw: self.new_model_path(i['dest_name']) for raw, i in plan.models.items()}

        if plan.models:
            step("Copying 3D models...")
            os.makedirs(self.models_dir, exist_ok=True)
            for info in plan.models.values():
                dest = os.path.join(self.models_dir, info['dest_name'])
                if not os.path.exists(dest) or _file_hash(dest) != _file_hash(info['src']):
                    shutil.copy2(info['src'], dest)

        if plan.footprints:
            step("Writing footprint library...")
            self._write_footprints(plan, model_map)

        if plan.symbols:
            step("Writing symbol library...")
            self._write_symbols(plan, fp_map)

        step("Relinking schematics...")
        changed_sheets = self._relink_schematics(plan, fp_map)

        step("Relinking board...")
        changed_fps = self._relink_board(plan, model_map)
        if changed_fps:
            step("Saving board...")
            self._save_board(model_map)

        step("Updating project library tables...")
        if plan.symbols:
            self._add_to_table('sym-lib-table', 'sym_lib_table',
                               f"${{KIPRJMOD}}/{LIBS_SUBDIR}/{self.nickname}.kicad_sym")
        if plan.footprints:
            self._add_to_table('fp-lib-table', 'fp_lib_table',
                               f"${{KIPRJMOD}}/{LIBS_SUBDIR}/{self.nickname}.pretty")

        return {'symbols': len(plan.symbols), 'footprints': len(plan.footprints),
                'models': len(plan.models), 'sheets': changed_sheets,
                'board_footprints': changed_fps, 'backup': backup}

    def _backup(self):
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        dest = os.path.join(self.project_dir, BACKUP_DIR, stamp)
        os.makedirs(dest, exist_ok=True)
        files = self.schematic_files() + [os.path.join(self.project_dir, t) for t in ('sym-lib-table', 'fp-lib-table')]
        if self.pcb_file:
            files.append(self.pcb_file)
        for f in files:
            if os.path.isfile(f):
                rel = os.path.relpath(f, self.project_dir)
                os.makedirs(os.path.dirname(os.path.join(dest, rel)), exist_ok=True)
                shutil.copy2(f, os.path.join(dest, rel))
        self._ensure_gitignored(BACKUP_DIR + '/')
        return dest

    def _ensure_gitignored(self, entry):
        path = os.path.join(self.project_dir, '.gitignore')
        try:
            lines = _read(path).splitlines() if os.path.exists(path) else []
            if entry not in (l.strip() for l in lines):
                with open(path, 'a', encoding='utf-8', newline='\n') as f:
                    if lines and lines[-1].strip():
                        f.write('\n')
                    f.write(entry + '\n')
        except OSError:
            pass

    def _rewrite_models(self, text, model_map):
        def sub(m):
            raw = _unq(m.group(1))
            return '(model ' + _q(model_map[raw]) if raw in model_map else m.group(0)
        return re.sub(r'\(model\s+"((?:[^"\\]|\\.)*)"', sub, text)

    def _write_footprints(self, plan, model_map):
        os.makedirs(self.fp_lib_path, exist_ok=True)
        for (lib, name), info in plan.footprints.items():
            dest = os.path.join(self.fp_lib_path, info['new_name'] + '.kicad_mod')
            if info['src']:
                text = _read(info['src'])
            else:
                text = self._export_board_footprint(info['board_fp'])
                if text is None:
                    plan.warnings.append(f"Could not export '{lib}:{name}' from the board.")
                    continue
            if info['new_name'] != name:
                text = re.sub(r'^\(footprint\s+"(?:[^"\\]|\\.)*"', '(footprint ' + _q(info['new_name']),
                              text, count=1)
            _write(dest, self._rewrite_models(text, model_map))

    def _export_board_footprint(self, fp):
        """Saves a board footprint (reset to origin, front side) to a temp
        library and returns its text."""
        import tempfile
        import pcbnew
        tmp = tempfile.mkdtemp(suffix='.pretty')
        try:
            clone = pcbnew.FOOTPRINT(fp)
            try:
                if clone.IsFlipped():
                    try:
                        clone.Flip(clone.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
                    except Exception:
                        clone.Flip(clone.GetPosition(), False)
                clone.SetOrientationDegrees(0)
                clone.SetPosition(pcbnew.VECTOR2I(0, 0))
            except Exception:
                pass
            pcbnew.FootprintSave(tmp, clone)
            files = [f for f in os.listdir(tmp) if f.endswith('.kicad_mod')]
            return _read(os.path.join(tmp, files[0])) if files else None
        except Exception as e:
            print(f"Footprint export error: {e}")
            return None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _write_symbols(self, plan, fp_map):
        existing_text = _read(self.sym_lib_path) if os.path.exists(self.sym_lib_path) else None
        existing = set()
        if existing_text:
            existing = {n for n, _, _ in _top_level_symbols(existing_text, 1)}

        blocks = []
        written = set(existing)
        for (lib, name), info in plan.symbols.items():
            if info['new_name'] in written:
                continue
            written.add(info['new_name'])
            block = _rename_symbol_block(info['block'], info['new_name'], name, info['new_name'])
            block = self._rewrite_fp_fields(block, fp_map)
            blocks.append('\t' + _reindent(block, -1))

        if existing_text:
            end = existing_text.rstrip().rfind(')')
            text = existing_text[:end].rstrip('\n') + '\n' + '\n'.join(blocks) + '\n)\n' if blocks else existing_text
        else:
            version = max([_DEFAULT_SYM_LIB_VERSION] + self._source_sym_versions(plan))
            text = ("(kicad_symbol_lib\n"
                    f"\t(version {version})\n"
                    "\t(generator \"github_command_center\")\n"
                    "\t(generator_version \"1.1\")\n"
                    + '\n'.join(blocks) + "\n)\n")
        _write(self.sym_lib_path, text)
        self._normalize_sym_lib()

    def _source_sym_versions(self, plan):
        versions = []
        for lib in {lib for lib, _ in plan.symbols}:
            path = self._expand(self.sym_table[lib]) if lib in self.sym_table else None
            try:
                with open(path, encoding='utf-8') as f:
                    m = _SCH_VERSION_RE.search(f.read(400))
                if m:
                    versions.append(int(m.group(1)))
            except Exception:
                pass
        return versions

    def _normalize_sym_lib(self):
        """Lets KiCad re-save the library in its own canonical format. Optional:
        if kicad-cli is unavailable or fails, the hand-written file is kept."""
        try:
            tmp = self.sym_lib_path + '.tmp'
            res = subprocess.run([find_kicad_cli(), 'sym', 'upgrade', '--force', '--output', tmp, self.sym_lib_path],
                                 capture_output=True, text=True, errors='replace', timeout=120,
                                 creationflags=CREATE_NO_WINDOW)
            if res.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                os.replace(tmp, self.sym_lib_path)
            elif os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass

    def _rewrite_fp_fields(self, text, fp_map):
        def sub(m):
            v = _unq(m.group(2))
            return m.group(1) + _q(fp_map[v]) if v in fp_map else m.group(0)
        return re.sub(r'(\(property\s+"Footprint"\s+)"((?:[^"\\]|\\.)*)"', sub, text)

    def _relink_schematics(self, plan, fp_map):
        id_map = {f"{lib}:{name}": f"{self.nickname}:{i['new_name']}" for (lib, name), i in plan.symbols.items()}
        changed = 0
        for path in self.schematic_files():
            text = _read(path)
            orig = text

            # 1. Rename cache entries; drop duplicates created when two old
            #    libraries pointed at the same symbol.
            span = _lib_symbols_span(text)
            if span:
                s0, e0 = span
                block = text[s0:e0]
                parts, last, seen = [], 0, set()
                for name, s, e in _top_level_symbols(block, 1):
                    sym = block[s:e]
                    if name in id_map:
                        lib, base = name.split(':', 1)
                        new_full = id_map[name]
                        sym = _rename_symbol_block(sym, new_full, base, new_full.split(':', 1)[1])
                        name = new_full
                    if name in seen:
                        # drop the block plus its leading indentation/newline
                        cut = len(block[last:s]) - len(block[last:s].rstrip(' \t\n'))
                        parts.append(block[last:s][:len(block[last:s]) - cut])
                    else:
                        seen.add(name)
                        parts.append(block[last:s])
                        parts.append(sym)
                    last = e
                parts.append(block[last:])
                text = text[:s0] + ''.join(parts) + text[e0:]

            # 2. Instance lib_ids
            def sub_id(m):
                v = _unq(m.group(1))
                return '(lib_id ' + _q(id_map[v]) + ')' if v in id_map else m.group(0)
            text = re.sub(r'\(lib_id\s+"((?:[^"\\]|\\.)*)"\)', sub_id, text)

            # 3. Footprint fields (instances and cache)
            text = self._rewrite_fp_fields(text, fp_map)

            if text != orig:
                _write(path, text)
                changed += 1
        return changed

    def _relink_board(self, plan, model_map):
        if self.board is None:
            return 0
        import pcbnew
        changed = 0
        for fp in self.board.GetFootprints():
            fpid = fp.GetFPID()
            key = (str(fpid.GetLibNickname()), str(fpid.GetLibItemName()))
            touched = False
            if key in plan.footprints:
                fp.SetFPID(pcbnew.LIB_ID(self.nickname, plan.footprints[key]['new_name']))
                touched = True
            # Index, don't iterate: iterating the SWIG vector yields copies,
            # so assignments through the loop variable are silently lost.
            models = fp.Models()
            for i in range(len(models)):
                if models[i].m_Filename in model_map:
                    models[i].m_Filename = model_map[models[i].m_Filename]
                    touched = True
            changed += touched
        return changed

    def _save_board(self, model_map):
        """Writes the relinked board to disk. Edits made through Python do not
        mark the editor as modified, so Ctrl+S is a no-op and closing the
        project discards them silently — the board must be saved here."""
        import pcbnew
        pcbnew.SaveBoard(self.pcb_file, self.board)
        text = _read(self.pcb_file)
        missing = [p for p in set(model_map.values()) if '(model ' + _q(p) not in text]
        if missing and any(m.m_Filename in missing for fp in self.board.GetFootprints() for m in fp.Models()):
            raise RuntimeError("The board was saved but the relinked 3D model paths are missing from "
                               f"{os.path.basename(self.pcb_file)}.")

    def _add_to_table(self, filename, root_token, uri):
        path = os.path.join(self.project_dir, filename)
        entry = (f'\t(lib (name {_q(self.nickname)}) (type "KiCad") (uri {_q(uri)}) '
                 f'(options "") (descr {_q(BUNDLE_DESCR)}))')
        if os.path.exists(path):
            text = _read(path)
            for m in _LIB_ENTRY_RE.finditer(text):
                if _unq(m.group(1)) == self.nickname:
                    if _unq(m.group(3)) != uri:
                        raise RuntimeError(f"{filename} already has a '{self.nickname}' library "
                                           f"pointing elsewhere ({_unq(m.group(3))}).")
                    return
            end = text.rstrip().rfind(')')
            text = text[:end].rstrip('\n') + '\n' + entry + '\n)\n'
        else:
            text = f"({root_token}\n\t(version 7)\n{entry}\n)\n"
        _write(path, text)
