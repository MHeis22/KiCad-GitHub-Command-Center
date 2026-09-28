"""Bill of Materials generation on top of KiCad's own exporters.

KiCad resolves the schematic hierarchy (multi-instance sheets, per-instance
references, text variables) far better than any regex over .kicad_sch files,
so the data comes from kicad-cli:

  * `sch export netlist`  - every real component, with its fields and the
                            'exclude from BOM' / 'DNP' flags. This is the list
                            the report compares against, so it can say why a
                            part is missing from the BOM.
  * `sch export bom`      - the BOM rows themselves (one per reference), with
                            generated fields like ${DNP} / ${EXCLUDE_FROM_BOARD}.

The plugin then applies the user's choices (DNP, off-board parts, MPN) and
groups identical parts into the CSV files.
"""
import os
import csv
import re
import shutil
import subprocess
import tempfile

from .utils import CREATE_NO_WINDOW, find_kicad_cli, project_files, load_project_settings

CLI_TIMEOUT = 120

DEFAULT_OPTIONS = {
    'include_dnp': True,          # keep DNP parts, flagged in a DNP column
    'include_off_board': True,    # keep parts not placed on the PCB (panel parts, cables...)
    'group_identical': True,      # one row per identical part instead of per reference
    'extra_fields': ['Description'],
}


def _natural_sort_key(ref):
    """Splits strings into text and numbers for proper alphanumeric sorting (R2 before R10)."""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', ref)]


def _parse_sexpr(text):
    """Minimal S-expression parser for KiCad netlists. Returns nested lists of
    str; quoted strings are unescaped and kept as str as well."""
    tokens = re.finditer(r'\(|\)|"((?:[^"\\]|\\.)*)"|[^\s()"]+', text)
    stack, cur = [], []
    for m in tokens:
        tok = m.group(0)
        if tok == '(':
            stack.append(cur)
            cur = []
        elif tok == ')':
            done = cur
            cur = stack.pop()
            cur.append(done)
        elif m.group(1) is not None:
            cur.append(re.sub(r'\\(.)', r'\1', m.group(1)))
        else:
            cur.append(tok)
    return cur


def _child(node, name):
    for item in node:
        if isinstance(item, list) and item and item[0] == name:
            return item
    return None


def _children(node, name):
    return [i for i in node if isinstance(i, list) and i and i[0] == name]


def _value(node, name, default=""):
    c = _child(node, name)
    return c[1] if c and len(c) > 1 and isinstance(c[1], str) else default


class BOMReport:
    """What happened in a BOM run, for the summary dialog."""
    def __init__(self):
        self.files = []            # written CSV paths
        self.total = 0             # real components in the design
        self.counts = {}           # BOM name -> number of references in it
        self.excluded = []         # (ref, value, reason) sorted by ref
        self.notes = []            # free-text notes shown under the list


class BOMGenerator:
    def __init__(self, project_dir, settings=None):
        self.project_dir = project_dir
        self.settings = settings or {}
        self.mpn_field = self.settings.get('mpn_field_name', 'MPN')
        self.kicad_cli = find_kicad_cli()
        self._components = None
        self._rows = None

    # Common names for the manufacturer part number field, most specific first.
    MPN_CANDIDATES = ("MPN", "Manufacturer_Part_Number", "Manufacturer Part Number",
                      "ManufacturerPartNumber", "PartNumber", "Part Number", "LCSC")

    def guess_mpn_field(self):
        """The configured MPN field if any part uses it, else the first common
        part-number field that has values, else the configured name."""
        used = {n for c in self.load_components().values()
                for n, v in c['fields'].items() if v.strip()}
        if self.mpn_field in used:
            return self.mpn_field
        return next((n for n in self.MPN_CANDIDATES if n in used), self.mpn_field)

    def summary(self):
        """Counts for the options dialog: components, DNP, off-board and
        excluded-from-BOM parts."""
        comps = self.load_components()
        rows = self.load_rows()
        return {
            'total': len(comps),
            'dnp': sum(1 for r in rows.values() if r['dnp']),
            'off_board': sum(1 for r in rows.values() if r['off_board']),
            'excluded_from_bom': sum(1 for c in comps.values() if c['exclude_from_bom']),
        }

    def load_rows(self):
        """BOM rows with every available field, exported once and cached."""
        if self._rows is None:
            self._rows = self._export_rows(self.available_fields())
        return self._rows

    # ------------------------------------------------------------------ inputs

    def enabled(self):
        return bool(self.settings.get('generate_bom_dist') or self.settings.get('generate_bom_eng'))

    def project_name(self):
        pro_files = project_files(self.project_dir, ".kicad_pro")
        if pro_files:
            return os.path.splitext(os.path.basename(pro_files[0]))[0]
        return "project"

    def root_schematic(self):
        """The top-level sheet: <project>.kicad_sch, else the first schematic."""
        candidate = os.path.join(self.project_dir, self.project_name() + ".kicad_sch")
        if os.path.isfile(candidate):
            return candidate
        sch_files = project_files(self.project_dir, ".kicad_sch")
        return sch_files[0] if sch_files else None

    def load_options(self):
        opts = dict(DEFAULT_OPTIONS)
        opts.update(load_project_settings(self.project_dir).get('bom_options', {}))
        return opts

    def _run_cli(self, args):
        res = subprocess.run([self.kicad_cli] + args, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=CLI_TIMEOUT,
                             cwd=self.project_dir, stdin=subprocess.DEVNULL,
                             creationflags=CREATE_NO_WINDOW)
        if res.returncode != 0:
            raise RuntimeError(f"kicad-cli {' '.join(args[:3])} failed:\n"
                               + ((res.stderr or res.stdout or "").strip() or f"exit code {res.returncode}"))

    def load_components(self):
        """Every real component (power symbols and flags are not components and
        never appear) as {ref: {...}}, from KiCad's netlist exporter. Cached."""
        if self._components is not None:
            return self._components
        sch = self.root_schematic()
        if not sch:
            raise RuntimeError("No schematic found in the project folder.")

        fd, net_path = tempfile.mkstemp(suffix=".net", prefix="gcc_bom_")
        os.close(fd)
        try:
            self._run_cli(["sch", "export", "netlist", "--format", "kicadsexpr",
                           "--output", net_path, sch])
            with open(net_path, encoding="utf-8", errors="replace") as f:
                tree = _parse_sexpr(f.read())
        finally:
            try:
                os.remove(net_path)
            except OSError:
                pass

        root = _child(tree, "export") or []
        comps_node = _child(root, "components") or []

        components = {}
        for comp in _children(comps_node, "comp"):
            ref = _value(comp, "ref")
            if not ref:
                continue
            fields = {}
            for fld in _children(_child(comp, "fields") or [], "field"):
                name = _value(fld, "name")
                val = next((x for x in fld[2:] if isinstance(x, str)), "")
                if name:
                    fields[name] = val
            props = {_value(p, "name") for p in _children(comp, "property")}
            sheet = sheetfile = ""
            for p in _children(comp, "property"):
                if _value(p, "name") == "Sheetname":
                    sheet = _value(p, "value")
                elif _value(p, "name") == "Sheetfile":
                    sheetfile = _value(p, "value")
            components[ref] = {
                'ref': ref,
                'value': _value(comp, "value"),
                'footprint': _value(comp, "footprint"),
                'fields': fields,
                'dnp': "dnp" in props,
                'exclude_from_bom': "exclude_from_bom" in props,
                'sheet': sheet,
                'sheetfile': sheetfile,   # the .kicad_sch the symbol is drawn in
            }
        # The netlist leaves out symbols marked 'Exclude from board', and the
        # BOM export leaves out 'Exclude from BOM' ones, so a part with both
        # flags would be in neither. Pick those (and the sheet of off-board
        # parts) up from the placed symbols themselves.
        scanned = self._scan_symbols()
        for ref, sym in scanned.items():
            if ref not in components:
                components[ref] = sym
            elif not components[ref]['sheetfile']:
                components[ref]['sheetfile'] = sym['sheetfile']
        # Multi-unit parts can be drawn on several sheets; the netlist only
        # names one of them.
        for ref, comp in components.items():
            files = set(scanned.get(ref, {}).get('sheetfiles', ()))
            if comp['sheetfile']:
                files.add(comp['sheetfile'])
            comp['sheetfiles'] = sorted(files)
        self._components = components
        return components

    def _scan_symbols(self):
        """Placed symbols across the sheet hierarchy (walked from the root
        sheet), keyed by every instance reference that belongs to this project.
        Only used to fill the gaps KiCad's exporters leave (see above)."""
        root = self.root_schematic()
        if not root:
            return {}
        root_dir = os.path.dirname(root)
        project = self.project_name()
        found, seen = {}, set()

        def flag(node, name):
            c = _child(node, name)
            return c[1] if c and len(c) > 1 else None

        def walk(path):
            real = os.path.realpath(path)
            if real in seen or not os.path.isfile(path):
                return
            seen.add(real)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    tree = _child(_parse_sexpr(f.read()), "kicad_sch") or []
            except OSError:
                return
            sheetfile = os.path.relpath(path, root_dir).replace(os.sep, "/")
            for sym in _children(tree, "symbol"):
                if not _child(sym, "lib_id"):
                    continue
                props = {p[1]: (p[2] if len(p) > 2 and isinstance(p[2], str) else "")
                         for p in _children(sym, "property") if len(p) > 1}
                refs = []
                for proj in _children(_child(sym, "instances") or [], "project"):
                    if len(proj) > 1 and proj[1] != project:
                        continue   # stale instance data from another project
                    for p in _children(proj, "path"):
                        r = _value(p, "reference")
                        if r:
                            refs.append(r)
                if not refs and props.get("Reference"):
                    refs = [props["Reference"]]
                for ref in refs:
                    if ref.startswith("#") or ref.endswith("?"):
                        continue   # power symbols/flags, unannotated symbols
                    if ref in found:   # another unit of the same part
                        found[ref]['sheetfiles'].add(sheetfile)
                        continue
                    found[ref] = {
                        'ref': ref,
                        'value': props.get("Value", ""),
                        'footprint': props.get("Footprint", ""),
                        'fields': {k: v for k, v in props.items()
                                   if k not in ("Reference", "Value", "Footprint")},
                        'dnp': flag(sym, "dnp") == "yes",
                        'exclude_from_bom': flag(sym, "in_bom") == "no",
                        'exclude_from_board': flag(sym, "on_board") == "no",
                        'sheet': "",
                        'sheetfile': sheetfile,
                        'sheetfiles': {sheetfile},
                    }
            for sheet in _children(tree, "sheet"):
                for p in _children(sheet, "property"):
                    if len(p) > 2 and p[1] in ("Sheetfile", "Sheet file"):
                        walk(os.path.join(os.path.dirname(path), p[2]))

        walk(root)
        return found

    def available_fields(self):
        """Custom field names found on the components, for the options dialog."""
        names = set()
        for comp in self.load_components().values():
            names.update(comp['fields'].keys())
        names.discard("Footprint")   # always its own column
        names.discard("Reference")
        names.discard("Value")
        return sorted(names, key=str.lower)

    def _export_rows(self, fields):
        """One row per reference from `kicad-cli sch export bom`. Returns
        {ref: {field: value}} plus the generated DNP / off-board flags.
        Excluded-from-BOM symbols are never exported by KiCad."""
        # Generated flags come back as localized text ("Excluded from board",
        # "Poissuljettu piirilevyltä"...), so only their presence matters.
        cli_fields = ["Reference", "Value", "Footprint", "${DNP}", "${EXCLUDE_FROM_BOARD}"] + \
                     [f for f in fields if f not in ("Reference", "Value", "Footprint")]
        # Field names are comma separated on the command line.
        cli_fields = [f for f in cli_fields if "," not in f]
        keys = ["ref", "value", "footprint", "dnp", "off_board"] + cli_fields[5:]

        fd, out_path = tempfile.mkstemp(suffix=".tsv", prefix="gcc_bom_")
        os.close(fd)
        try:
            # Tab-separated with no quoting: KiCad strips tabs and line breaks
            # from field values by default, so the split below is unambiguous.
            self._run_cli(["sch", "export", "bom",
                           "--fields", ",".join(cli_fields),
                           "--labels", ",".join(f"c{i}" for i in range(len(cli_fields))),
                           "--group-by", "",
                           "--ref-range-delimiter", "",
                           "--field-delimiter", "\t",
                           "--string-delimiter", "",
                           "--output", out_path, self.root_schematic()])
            with open(out_path, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        finally:
            try:
                os.remove(out_path)
            except OSError:
                pass

        rows = {}
        for line in lines[1:]:   # first line is the header
            if not line.strip():
                continue
            cells = line.split("\t")
            cells += [""] * (len(keys) - len(cells))
            row = dict(zip(keys, cells))
            row['dnp'] = bool(row['dnp'].strip())
            row['off_board'] = bool(row['off_board'].strip())
            # Ungrouped export gives one reference per row; be defensive anyway.
            for ref in [r.strip() for r in row['ref'].split(",") if r.strip()]:
                rows[ref] = dict(row, ref=ref)
        return rows

    # --------------------------------------------------- design-level views
    # Same shape the old regex parser (kicad_parser.get_bom_data) returned, so
    # the README and the visual diff can use KiCad's hierarchy-aware data:
    # {ref: {'val', 'fp', 'desc', 'mpn', 'dnp', 'sheetfiles'}}

    def component_dict(self, include_excluded=True):
        """Every real component in the design, from the netlist."""
        mpn = self.guess_mpn_field()
        out = {}
        for ref, c in self.load_components().items():
            if c['exclude_from_bom'] and not include_excluded:
                continue
            f = c['fields']
            out[ref] = {'val': c['value'], 'fp': c['footprint'],
                        'desc': f.get('Description', ''), 'mpn': f.get(mpn, ''),
                        'dnp': c['dnp'], 'sheetfiles': c['sheetfiles']}
        return out

    def bom_dict(self):
        """Exactly what KiCad's BOM export lists (excluded-from-BOM parts are
        not in it), for the BOM tab of the visual diff."""
        mpn = self.guess_mpn_field()
        comps = self.load_components()
        out = {}
        for ref, row in self.load_rows().items():
            out[ref] = {'val': row['value'], 'fp': row['footprint'],
                        'desc': row.get('Description', ''), 'mpn': row.get(mpn, ''),
                        'dnp': row['dnp'],
                        'sheetfiles': comps.get(ref, {}).get('sheetfiles', [])}
        return out

    @classmethod
    def at_revision(cls, project_dir, target, settings=None, git_cmd="git"):
        """A generator over the design as committed at git `target`. The
        schematics and project file are written to a temporary folder outside
        the project (so nothing is left behind in it); call cleanup() when done.
        Returns None if the revision has no schematic."""
        def git(args, **kw):
            return subprocess.run([git_cmd, "-C", project_dir] + args, capture_output=True,
                                  timeout=60, stdin=subprocess.DEVNULL,
                                  creationflags=CREATE_NO_WINDOW, **kw)

        # Paths relative to project_dir (ls-tree is relative to the cwd).
        res = git(["-c", "core.quotePath=false", "ls-tree", "-r", "-z", "--name-only", target, "--", "."])
        if res.returncode != 0:
            return None
        wanted = [p for p in res.stdout.decode("utf-8", "replace").split("\0")
                  if p.endswith((".kicad_sch", ".kicad_pro"))]
        if not any(p.endswith(".kicad_sch") for p in wanted):
            return None

        tmp = tempfile.mkdtemp(prefix="gcc_bom_rev_")
        try:
            for rel in wanted:
                blob = git(["show", f"{target}:./{rel}"])
                if blob.returncode != 0:
                    continue
                dest = os.path.join(tmp, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(blob.stdout)
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        gen = cls(tmp, settings)
        gen._tmp_dir = tmp
        return gen

    def cleanup(self):
        tmp = getattr(self, "_tmp_dir", None)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
            self._tmp_dir = None

    @staticmethod
    def on_sheet(sheetfiles, fname):
        """Is a component drawn on the schematic `fname`? Sheet files are
        relative to the root sheet; fname may carry a subfolder."""
        b = (fname or "").replace("\\", "/")
        for a in sheetfiles or ():
            a = a.replace("\\", "/")
            if a == b or b.endswith("/" + a) or os.path.basename(a) == os.path.basename(b):
                return True
        return False

    # ------------------------------------------------------------------ output

    def generate_boms(self, options=None):
        """Writes the enabled BOM CSVs into production/ and returns a BOMReport."""
        opts = dict(DEFAULT_OPTIONS)
        opts.update(options or {})
        gen_dist = self.settings.get('generate_bom_dist', False)
        gen_eng = self.settings.get('generate_bom_eng', False)
        report = BOMReport()
        if not gen_dist and not gen_eng:
            return report

        components = self.load_components()
        report.total = len(components)
        extra = [f for f in opts.get('extra_fields', []) if f != self.mpn_field]
        rows = self.load_rows()

        excluded = {}   # ref -> reason

        def exclude(ref, reason):
            excluded.setdefault(ref, reason)

        for ref, comp in components.items():
            if ref in rows:
                continue
            if comp['exclude_from_bom']:
                extra_flags = [n for n, on in (("DNP", comp['dnp']),
                                               ("not on the PCB", comp.get('exclude_from_board')))
                               if on]
                exclude(ref, "Symbol is marked 'Exclude from bill of materials'"
                             + (f" (also {', '.join(extra_flags)})" if extra_flags else ""))
            else:
                exclude(ref, "Not reported by KiCad's BOM export")

        eng_rows = []
        for ref, row in rows.items():
            if row['dnp'] and not opts['include_dnp']:
                exclude(ref, "Do not populate (DNP) - you chose to leave DNP parts out")
            elif row['off_board'] and not opts['include_off_board']:
                exclude(ref, "Not placed on the PCB ('Exclude from board') - "
                             "you chose to leave off-board parts out")
            else:
                eng_rows.append(row)

        production_dir = os.path.join(self.project_dir, "production")
        os.makedirs(production_dir, exist_ok=True)
        name = self.project_name()

        if gen_eng:
            path = os.path.join(production_dir, f"{name}_full_bom.csv")
            self._write_full_bom(path, eng_rows, extra, opts['group_identical'])
            report.files.append(path)

        if gen_eng:
            report.counts["Engineering BOM"] = len(eng_rows)

        if gen_dist:
            dist_rows = [r for r in eng_rows if r.get(self.mpn_field, "").strip()]
            no_mpn = [r['ref'] for r in eng_rows if not r.get(self.mpn_field, "").strip()]
            report.counts["Distributor BOM"] = len(dist_rows)
            path = os.path.join(production_dir, f"{name}_distributor_bom.csv")
            if dist_rows:
                self._write_distributor_bom(path, dist_rows, opts['group_identical'])
                report.files.append(path)
            else:
                report.notes.append(f"Distributor BOM not written: no part has a "
                                    f"'{self.mpn_field}' value.")
            for ref in no_mpn:
                if gen_eng:   # in the engineering BOM, just not the distributor one
                    exclude(ref, f"Distributor BOM only: no '{self.mpn_field}' value")
                else:
                    exclude(ref, f"No '{self.mpn_field}' value - the distributor BOM only "
                                 "lists parts with a part number")

        value_of = lambda r: (components.get(r) or rows.get(r) or {}).get('value', "")
        report.excluded = sorted(((r, value_of(r), why) for r, why in excluded.items()),
                                 key=lambda x: _natural_sort_key(x[0]))
        report.notes.append("Power symbols and power flags are not components and never "
                            "appear in a BOM.")
        if any("," in f for f in [self.mpn_field] + extra):
            report.notes.append("Fields whose name contains a comma can't be exported by "
                                "kicad-cli and were left out.")
        return report

    def _group(self, rows, key_fields, group):
        """[(refs, row)] with refs naturally sorted, ordered by first reference."""
        groups = {}
        for row in rows:
            sig = tuple(row.get(k, "") for k in key_fields) if group else (row['ref'],)
            groups.setdefault(sig, {'refs': [], 'row': row})['refs'].append(row['ref'])
        out = []
        for g in groups.values():
            g['refs'].sort(key=_natural_sort_key)
            out.append((g['refs'], g['row']))
        out.sort(key=lambda g: _natural_sort_key(g[0][0]))
        return out

    def _write_full_bom(self, path, rows, extra, group):
        """Detailed BOM for human review. DNP and off-board parts that are kept
        are flagged in their own columns, and never merged with populated twins."""
        key_fields = ['value', 'footprint', self.mpn_field] + extra + ['dnp', 'off_board']
        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['Qty', 'Reference', 'Value', 'Footprint', self.mpn_field]
                            + extra + ['DNP', 'Not on PCB'])
            for refs, row in self._group(rows, key_fields, group):
                writer.writerow([len(refs), ", ".join(refs), row['value'], row['footprint'],
                                 row.get(self.mpn_field, "")]
                                + [row.get(e, "") for e in extra]
                                + ['DNP' if row['dnp'] else '',
                                   'yes' if row['off_board'] else ''])

    def _write_distributor_bom(self, path, rows, group):
        """Compact BOM for distributor upload tools: Qty, Reference, MPN, DNP.
        Grouped by MPN (plus value/footprint so different parts that share a
        placeholder MPN never merge, and DNP so populated/DNP twins stay apart)."""
        key_fields = ['value', 'footprint', self.mpn_field, 'dnp']
        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['Qty', 'Reference', self.mpn_field, 'DNP'])
            for refs, row in self._group(rows, key_fields, group):
                writer.writerow([len(refs), ", ".join(refs), row[self.mpn_field].strip(),
                                 'DNP' if row['dnp'] else ''])
