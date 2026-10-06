import os
import re
import subprocess
import shutil
import tempfile
from .utils import CREATE_NO_WINDOW, find_kicad_cli
from .project_bundler import _sexpr_end

# Merges a server branch into the local branch when both sides changed files
# that git can't merge safely. Design files get an explicit per-file choice
# (mine / the server's); generated outputs are never chosen, they're rebuilt
# from the chosen design afterwards. Everything happens inside one
# `git merge --no-commit`, so any failure is undone with `git merge --abort`.
#
# No wx here: the UI collects the choices, this module does the git work.

DESIGN_EXTS = ('.kicad_pcb', '.kicad_sch', '.kicad_mod')
VIEW_STATE_EXTS = ('.kicad_prl',)
# Written by the plugin's generators (production/, 3d/, docs/); rebuilt after a merge.
GENERATED_DIRS = ('production/', '3d/', 'docs/')
README_START = "<!-- KICAD_DIFF_GEN_START -->"
README_END = "<!-- KICAD_DIFF_GEN_END -->"

MINE, THEIRS, BOTH = 'mine', 'theirs', 'both'


class MergeItem:
    """One file both sides touched (or that git can't merge)."""

    def __init__(self, path, kind, conflict, mine_exists, theirs_exists):
        self.path = path              # repo-root relative, forward slashes
        self.kind = kind              # 'design' | 'generated' | 'readme' | 'view' | 'other'
        self.conflict = conflict      # git reports a textual conflict
        self.mine_exists = mine_exists
        self.theirs_exists = theirs_exists

    @property
    def options(self):
        """Choices the UI offers. Combining is only possible when git can
        merge the text; the result is then checked by loading it in KiCad."""
        if self.kind == 'design' and self.mine_exists and self.theirs_exists and not self.conflict:
            return (BOTH, MINE, THEIRS)
        return (MINE, THEIRS)

    @property
    def needs_choice(self):
        """Design files always (a clean text merge can still be an invalid
        board); other files only when git can't merge them."""
        if self.kind == 'design':
            return True
        return self.kind in ('other', 'readme') and self.conflict

    def __repr__(self):
        return f"MergeItem({self.path!r}, {self.kind}, conflict={self.conflict})"


class MergeResult:
    def __init__(self):
        self.commit = None
        self.ref_mismatches = []  # (board ref, schematic ref) pairs new in the merge
        self.regenerate = []      # generated outputs that must be rebuilt now
        self.taken_theirs = []    # design files replaced by the server's version
        self.mixed = False        # some design files from each side


class MergeResolver:

    def __init__(self, project_dir, git_cmd=None):
        self.git_cmd = git_cmd or ("git.exe" if os.name == "nt" else "git")
        self.project_dir = project_dir
        self.top = self._git(["rev-parse", "--show-toplevel"], cwd=project_dir).stdout.strip()

    def _git(self, args, cwd=None, check=False, input=None):
        res = subprocess.run([self.git_cmd, "-c", "core.quotePath=false"] + args, cwd=cwd or self.top,
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             input=input, timeout=120, creationflags=CREATE_NO_WINDOW)
        if check and res.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {(res.stderr or res.stdout).strip()}")
        return res

    def _paths(self, args):
        out = self._git(args + ["-z"]).stdout
        return {p for p in out.split("\0") if p}

    @staticmethod
    def kind_of(path):
        low = path.lower()
        name = os.path.basename(low)
        if low.endswith(DESIGN_EXTS):
            return 'design'
        if low.endswith(VIEW_STATE_EXTS):
            return 'view'
        if name == 'readme.md':
            return 'readme'
        # Generated outputs live in these folders of the project (which may be
        # a subfolder of the repository).
        if any(f"/{d}" in f"/{low}" for d in GENERATED_DIRS):
            return 'generated'
        return 'other'

    def _exists(self, rev, path):
        return self._git(["cat-file", "-e", f"{rev}:{path}"]).returncode == 0

    # ----- analysis -----------------------------------------------------------

    def analyze(self, upstream):
        """Files changed on both sides since the merge base, plus any file a
        trial merge reports as conflicting. Doesn't touch the working tree."""
        base = self._git(["merge-base", "HEAD", upstream], check=True).stdout.strip()
        mine = self._paths(["diff", "--name-only", base, "HEAD"])
        theirs = self._paths(["diff", "--name-only", base, upstream])
        conflicted = set()
        trial = self._git(["merge-tree", "--write-tree", "--name-only", "HEAD", upstream])
        if trial.returncode == 1:
            for line in trial.stdout.splitlines()[1:]:
                if not line:
                    break
                conflicted.add(line)
        items = []
        for path in sorted((mine & theirs) | conflicted):
            item = MergeItem(path, self.kind_of(path), path in conflicted,
                             self._exists("HEAD", path), self._exists(upstream, path))
            if item.kind == 'readme' and item.conflict and item.mine_exists and item.theirs_exists:
                # Differences in the generated block are rebuilt, not merged.
                item.conflict = self._merge_readme_text(path, upstream)[0] != 0
            items.append(item)
        return items

    # ----- merge --------------------------------------------------------------

    def merge(self, upstream, items, choices, message=None):
        """Merges `upstream` with `choices` ({path: MINE | THEIRS}) for every
        item that needs one. Returns a MergeResult. On any error the merge is
        aborted and the repository is exactly as before."""
        missing = [i.path for i in items if i.needs_choice and choices.get(i.path) not in i.options]
        if missing:
            raise ValueError("No valid choice for: " + ", ".join(missing))
        if self._paths(["diff", "--name-only", "HEAD"]) - {i.path for i in items if i.kind == 'view'}:
            raise RuntimeError("Commit or stash local changes before merging.")

        head_before = self._git(["rev-parse", "HEAD"], check=True).stdout.strip()
        res = self._git(["merge", "--no-commit", "--no-ff", upstream])
        if self._git(["rev-parse", "-q", "--verify", "MERGE_HEAD"]).returncode != 0:
            raise RuntimeError("git could not start the merge: " + (res.stderr or res.stdout).strip())

        result = MergeResult()
        try:
            for item in items:
                self._resolve(item, upstream, choices.get(item.path), result)
            left = self._paths(["diff", "--name-only", "--diff-filter=U"])
            if left:
                raise RuntimeError("Unresolved files remain: " + ", ".join(sorted(left)))
            combined = [i.path for i in items if i.kind == 'design' and choices[i.path] == BOTH]
            broken = [p for p in combined if not self.loads_in_kicad(os.path.join(self.top, p))]
            if broken:
                raise MergeCheckFailed(broken)
            sides = {choices[i.path] for i in items if i.kind == 'design'}
            result.mixed = MINE in sides and THEIRS in sides
            if any(i.kind == 'design' for i in items):
                self._check_design(upstream, result)
            self._git(["commit", "--no-edit"] + (["-m", message] if message else []), check=True)
            result.commit = self._git(["rev-parse", "HEAD"], check=True).stdout.strip()
        except BaseException:
            self._git(["merge", "--abort"])
            # merge --abort restores the tree; make sure HEAD is where it was.
            if self._git(["rev-parse", "HEAD"]).stdout.strip() != head_before:
                self._git(["reset", "--hard", head_before])
            raise
        return result

    # ----- design checks ------------------------------------------------------

    def _project_rel(self):
        rel = os.path.relpath(self.project_dir, self.top).replace(os.sep, '/')
        return '' if rel == '.' else rel + '/'

    def _design_texts(self, rev):
        """{path: text} of the project's schematics and board at `rev`
        (None = the working tree)."""
        prefix = self._project_rel()
        if rev is None:
            names = [prefix + f for f in os.listdir(self.project_dir) if f.endswith(('.kicad_sch', '.kicad_pcb'))]
            names += [prefix + os.path.relpath(os.path.join(r, f), self.project_dir).replace(os.sep, '/')
                      for r, _, fs in os.walk(self.project_dir) if r != self.project_dir and '.git' not in r
                      for f in fs if f.endswith('.kicad_sch')]
            out = {}
            for n in set(names):
                with open(os.path.join(self.top, n), encoding='utf-8', errors='replace') as f:
                    out[n] = f.read()
            return out
        listing = self._git(["ls-tree", "-r", "--name-only", rev, "--", prefix or "."]).stdout.splitlines()
        return {n: self._git(["show", f"{rev}:{n}"]).stdout for n in listing
                if n.endswith(('.kicad_sch', '.kicad_pcb'))}

    def _check_design(self, upstream, result):
        """Catches what a file-by-file choice can break although every file
        loads: references used twice (e.g. one side re-annotated, and sheets
        were taken from different sides), and board footprints whose reference
        no longer matches their symbol. Duplicates abort the merge."""
        pros = [f for f in os.listdir(self.project_dir) if f.endswith('.kicad_pro')]
        project = os.path.splitext(pros[0])[0] if len(pros) == 1 else None
        merged = _design_facts(self._design_texts(None), project)
        mine = _design_facts(self._design_texts("HEAD"), project)
        theirs = _design_facts(self._design_texts(upstream), project)
        new_dups = sorted(merged['dups'] - mine['dups'] - theirs['dups'])
        if new_dups:
            raise DuplicateReferences(new_dups)
        result.ref_mismatches = sorted(merged['mismatch'] - mine['mismatch'] - theirs['mismatch'])

    @staticmethod
    def loads_in_kicad(path):
        return loads_in_kicad(path)

    def _take(self, item, side, upstream):
        """Puts one side's version of the file in the tree and index (or
        removes it, when that side deleted the file)."""
        rev, exists = ("HEAD", item.mine_exists) if side == MINE else (upstream, item.theirs_exists)
        if exists:
            self._git(["checkout", rev, "--", item.path], check=True)
            self._git(["add", "--", item.path], check=True)
        else:
            self._git(["rm", "-q", "--cached", "--ignore-unmatch", "--", item.path], check=True)
            full = os.path.join(self.top, item.path)
            if os.path.exists(full):
                os.remove(full)

    def _resolve(self, item, upstream, choice, result):
        if item.kind == 'design':
            if choice == BOTH:
                self._git(["add", "--", item.path], check=True)  # git's clean merge, checked before commit
            else:
                self._take(item, choice, upstream)
            if choice == THEIRS:
                result.taken_theirs.append(item.path)
        elif item.kind == 'generated':
            # Never chosen: keep mine for now and rebuild from the merged design.
            self._take(item, MINE, upstream)
            result.regenerate.append(item.path)
        elif item.kind == 'view':
            self._take(item, MINE, upstream)
        elif item.kind == 'readme':
            self._resolve_readme(item, upstream, choice, result)
        elif item.conflict:
            self._take(item, choice, upstream)
        # else: git merged it cleanly; keep that.

    def _resolve_readme(self, item, upstream, choice, result):
        """The generated summary block is rebuilt after the merge, so only the
        hand-written text is merged; a conflict there uses the user's choice."""
        if not (item.mine_exists and item.theirs_exists):
            self._take(item, choice or MINE, upstream)
            return
        code, merged, mine_block = self._merge_readme_text(item.path, upstream)
        if code != 0:  # the hand-written text itself conflicts
            self._take(item, choice or MINE, upstream)
        else:
            with open(os.path.join(self.top, item.path), "w", encoding="utf-8", newline="") as f:
                f.write(_readme_with_block(merged, mine_block))
            self._git(["add", "--", item.path], check=True)
        result.regenerate.append(item.path)

    def _merge_readme_text(self, path, upstream):
        """Three-way merge of the README without its generated block.
        Returns (exit code, merged text, my generated block)."""
        base = self._git(["merge-base", "HEAD", upstream], check=True).stdout.strip()
        texts = []
        for rev in ("HEAD", base, upstream):
            show = self._git(["show", f"{rev}:{path}"])
            texts.append(show.stdout if show.returncode == 0 else "")
        tmp = [os.path.join(self.top, ".git", f"readme_merge_{n}") for n in ("mine", "base", "theirs")]
        try:
            for f_path, text in zip(tmp, texts):
                with open(f_path, "w", encoding="utf-8", newline="") as f:
                    f.write(_readme_without_block(text))
            merged = self._git(["merge-file", "-p", *tmp])
        finally:
            for f_path in tmp:
                if os.path.exists(f_path):
                    os.remove(f_path)
        return merged.returncode, merged.stdout, _readme_block(texts[0])


class MergeCheckFailed(RuntimeError):
    """A combined design file didn't load in KiCad; the merge was undone."""

    def __init__(self, paths):
        super().__init__("These combined files don't load in KiCad, so nothing was merged: "
                         + ", ".join(paths) + ". Choose mine or the server's version for them.")
        self.paths = paths


class DuplicateReferences(RuntimeError):
    """The merged design would use some references twice; the merge was undone."""

    def __init__(self, refs):
        shown = ", ".join(refs[:12]) + (" ..." if len(refs) > 12 else "")
        super().__init__(
            "With these choices, these references would be used by two different parts: " + shown + ". "
            "This happens when one side re-annotated and sheets are taken from different sides. "
            "Nothing was merged. Take the same side for all affected sheets, or re-annotate after merging "
            "one side completely.")
        self.refs = refs


_SYMBOL_RE = re.compile(r'\n\t\(symbol\s*\n\t\t\(lib_id "([^"]+)"\)')
_FOOTPRINT_RE = re.compile(r'\n\t\(footprint "')


def _design_facts(texts, project=None):
    """From {path: text} of schematics and a board of `project` (the
    .kicad_pro name; None = every project): 'dups', the references
    (with unit) used by more than one symbol instance, and 'mismatch', the
    (board ref, schematic ref) pairs of footprints whose reference differs
    from their symbol's. Power symbols (#PWR...) are ignored."""
    seen, sym_ref = {}, {}
    dups = set()
    board_refs = {}
    for path, text in texts.items():
        if path.endswith('.kicad_sch'):
            for m in _SYMBOL_RE.finditer(text):
                s = m.start() + 1
                block = text[s:_sexpr_end(text, s)]
                uid = re.search(r'\(uuid "([^"]+)"\)', block)
                # A sheet copied from another project keeps that project's
                # references too (project "Other" ...): only this project's count.
                own = block
                if project:
                    pm = re.search(r'\(project\s+"' + re.escape(project) + r'"', block)
                    own = block[pm.start():_sexpr_end(block, pm.start())] if pm else ""
                # One entry per placement: a sheet used twice places its symbols twice.
                for inst in re.finditer(r'\(path "([^"]*)"\s*\(reference "([^"]+)"\)\s*\(unit (\d+)\)', own):
                    ref = inst.group(2)
                    if ref.startswith('#') or ref.endswith('?'):
                        continue
                    owner = (uid.group(1) if uid else None, inst.group(1))
                    if (ref, inst.group(3)) in seen and seen[(ref, inst.group(3))] != owner:
                        dups.add(ref)
                    seen.setdefault((ref, inst.group(3)), owner)
                    if uid:
                        sym_ref[uid.group(1)] = ref
        elif path.endswith('.kicad_pcb'):
            for m in _FOOTPRINT_RE.finditer(text):
                s = m.start() + 1
                block = text[s:_sexpr_end(text, s)]
                p = re.search(r'\(path "([^"]+)"\)', block)
                r = re.search(r'\(property "Reference" "([^"]+)"', block)
                if p and r:
                    board_refs[p.group(1).split('/')[-1]] = r.group(1)
    mismatch = {(board_refs[u], sym_ref[u]) for u in board_refs if u in sym_ref and board_refs[u] != sym_ref[u]}
    return {'dups': dups, 'mismatch': mismatch}


def _kicad_cli_ok(args):
    try:
        res = subprocess.run([find_kicad_cli()] + args, capture_output=True, text=True, errors="replace",
                             timeout=300, creationflags=CREATE_NO_WINDOW)
        return res.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def loads_in_kicad(path):
    """True if KiCad can read the file: a board, a schematic sheet, or a footprint."""
    out = tempfile.mkdtemp()
    try:
        low = path.lower()
        if low.endswith('.kicad_pcb'):
            return _kicad_cli_ok(["pcb", "export", "pos", "--output", os.path.join(out, "x.pos"), path])
        if low.endswith('.kicad_sch'):
            return _kicad_cli_ok(["sch", "export", "netlist", "--output", os.path.join(out, "x.net"), path])
        if low.endswith('.kicad_mod'):
            lib = os.path.join(out, "check.pretty")
            os.makedirs(lib)
            shutil.copy(path, lib)
            return _kicad_cli_ok(["fp", "upgrade", "--output", os.path.join(out, "up.pretty"), lib])
        return True
    finally:
        shutil.rmtree(out, ignore_errors=True)


def _readme_block(text):
    a, b = text.find(README_START), text.find(README_END)
    return text[a:b + len(README_END)] if a != -1 and b > a else ""


def _readme_without_block(text):
    """The README with its generated block replaced by a fixed placeholder,
    so differences inside the block never cause a merge conflict."""
    block = _readme_block(text)
    return text.replace(block, README_START + README_END) if block else text


def _readme_with_block(text, block):
    placeholder = README_START + README_END
    if block and placeholder in text:
        return text.replace(placeholder, block)
    return text
