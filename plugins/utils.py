import os
import json
import subprocess
import shutil
import glob

# Fix for Windows: prevents the plugin from popping up CMD windows or hanging
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Network git commands (fetch/pull/push) must never wait for interactive input:
# a credential or host-key prompt nobody can answer would block forever and,
# on the UI thread, freeze KiCad. Fail fast instead and report the error.
GIT_NETWORK_TIMEOUT = 120

def git_network_kwargs():
    """subprocess.run kwargs for git commands that talk to a remote."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return {"env": env, "stdin": subprocess.DEVNULL, "timeout": GIT_NETWORK_TIMEOUT}

# Prefix of the git-reference copies DiffEngine writes next to the real files.
TMP_OLD_PREFIX = "tmp_git_old_"

def sweep_temp_files(project_dir):
    """Removes leftovers of DiffEngine's reference copies anywhere in the
    project: the copies themselves, their .kicad_prl/-backups, and the
    '~tmp_git_old_*.lck' lock files kicad-cli leaves next to them. The prefix
    is only ever used by this plugin, so nothing of the user's is touched."""
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d != '.git']
        for d in list(dirs):
            if d.startswith(TMP_OLD_PREFIX) and d.endswith('-backups'):
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                dirs.remove(d)
        for f in files:
            if f.startswith(TMP_OLD_PREFIX) or f.startswith('~' + TMP_OLD_PREFIX):
                try:
                    os.remove(os.path.join(root, f))
                except OSError:
                    pass  # still open somewhere; the next sweep gets it

def project_files(project_dir, ext):
    """Sorted '*<ext>' files in project_dir, ignoring DiffEngine's temporary
    reference copies (which can be left behind if KiCad quits mid-diff and
    would otherwise be picked as 'the' board/schematic)."""
    return sorted(p for p in glob.glob(os.path.join(project_dir, "*" + ext))
                  if not os.path.basename(p).startswith(TMP_OLD_PREFIX))

PCM_IDENTIFIER = "com.github.mheis22.kicad-github-command-center"

def get_installed_version():
    """The plugin's own version string, or None if it can't be determined.

    A source checkout has metadata.json next to plugins/. A PCM install does
    not (PCM only copies plugins/), so the version comes from KiCad's record
    of installed packages instead."""
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "metadata.json"), encoding="utf-8") as f:
            versions = json.load(f).get("versions") or []
        if versions and versions[0].get("version"):
            return versions[0]["version"]
    except Exception:
        pass
    try:
        import pcbnew
        settings_dir = pcbnew.SETTINGS_MANAGER.GetUserSettingsPath()
        with open(os.path.join(settings_dir, "installed_packages.json"), encoding="utf-8") as f:
            for pkg in json.load(f).get("packages", []):
                if pkg.get("package", {}).get("identifier") == PCM_IDENTIFIER:
                    return pkg.get("current_version")
    except Exception as e:
        print(f"GitHub Command Center: could not read the installed version: {e}")
    return None

def fetch_json(url, timeout=8):
    """GETs a JSON document. Falls back to the system curl when Python's own
    TLS setup fails — KiCad's bundled Python on macOS ships without CA
    certificates, so urllib can't verify github.com there."""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "KiCad-GitHub-Command-Center"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        curl = shutil.which("curl")
        if not curl:
            raise
        res = subprocess.run([curl, "-fsSL", "--max-time", str(timeout), "-H",
                              "User-Agent: KiCad-GitHub-Command-Center", url],
                             capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
        if res.returncode != 0:
            raise RuntimeError(f"{e}; curl: {res.stderr.strip()}")
        return json.loads(res.stdout)

def get_settings_path():
    """Returns the path for the global plugin settings file."""
    return os.path.expanduser('~/.kicad_git_diff_settings.json')

def get_project_settings_path(project_dir):
    """Returns the path for per-project plugin settings (overrides globals)."""
    return os.path.join(project_dir, '.kicad_git_plugin.json')

def load_settings():
    """Loads global settings from the user's home directory."""
    try:
        with open(get_settings_path(), 'r') as f:
            return json.load(f)
    except Exception:
        return {'include_kicad_version': True}

def load_project_settings(project_dir):
    """Loads per-project settings, falling back to an empty dict if none exist."""
    try:
        with open(get_project_settings_path(project_dir), 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def project_settings_problem(project_dir):
    """A message when the project settings file exists but can't be read,
    typically after a git merge left conflict markers in it; else None."""
    path = get_project_settings_path(project_dir)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return None
    try:
        json.loads(text)
        return None
    except ValueError:
        what = "has unresolved git merge conflicts" if "<<<<<<<" in text else "is not valid JSON"
        return (f"{os.path.basename(path)} {what}, so this project's settings can't be read. "
                f"Defaults are used until it is fixed; saving settings keeps a copy of it as "
                f"{os.path.basename(path)}.bak.")

def save_project_settings(project_dir, settings):
    """Saves per-project settings. The file is committed with the project, so
    it is written stably (sorted, indented) and only when it actually changes.
    An unreadable file (e.g. merge conflict) is kept as .bak, never lost."""
    path = get_project_settings_path(project_dir)
    text = json.dumps(settings, indent=2, sort_keys=True) + "\n"
    try:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                current = f.read()
            if current == text:
                return
            if project_settings_problem(project_dir):
                with open(path + '.bak', 'w', encoding='utf-8', newline='') as f:
                    f.write(current)
        except OSError:
            pass
        with open(path, 'w', newline='\n') as f:
            f.write(text)
    except Exception as e:
        print(f"Error saving project settings: {e}")

# How a person works, rather than what the project produces: these stay on
# this computer. Every other setting is stored with the project.
PERSONAL_KEYS = ('include_kicad_version', 'silent_pull')

def load_effective_settings(project_dir):
    """Settings for this project: the project's own values over this
    computer's. A project that was never configured starts from the values
    last saved in any project."""
    settings = load_settings()
    settings.pop('last_targets', None)  # bookkeeping, managed by get/save_last_target
    project = load_project_settings(project_dir).get('settings') or {}
    settings.update({k: v for k, v in project.items() if k not in PERSONAL_KEYS})
    return settings

def save_effective_settings(project_dir, settings):
    """Personal keys go to this computer; the rest to the project (and to this
    computer too, as the starting point for unconfigured projects)."""
    glob_settings = load_settings()
    glob_settings.update({k: v for k, v in settings.items() if k != 'last_targets'})
    save_settings(glob_settings)
    proj = load_project_settings(project_dir)
    proj['settings'] = {k: v for k, v in settings.items() if k not in PERSONAL_KEYS and k != 'last_targets'}
    save_project_settings(project_dir, proj)

def _project_key(project_dir):
    return os.path.normcase(os.path.abspath(project_dir))

def get_last_target(project_dir):
    """Returns the last comparison target used for this project, or None.
    Kept on this computer: it's a personal view choice, not project data."""
    target = (load_settings().get('last_targets') or {}).get(_project_key(project_dir))
    return target or load_project_settings(project_dir).get('last_target')

def save_last_target(project_dir, target):
    """Persists the last comparison target for this project."""
    settings = load_settings()
    settings.setdefault('last_targets', {})[_project_key(project_dir)] = target
    save_settings(settings)
    proj = load_project_settings(project_dir)
    if 'last_target' in proj:  # older versions kept it in the committed project file
        del proj['last_target']
        save_project_settings(project_dir, proj)

def save_settings(settings):
    """Saves global settings to the user's home directory."""
    try:
        with open(get_settings_path(), 'w') as f:
            json.dump(settings, f)
    except Exception as e:
        print(f"Error saving settings: {e}")

def is_git_installed():
    """Checks if git is available on the system PATH."""
    try:
        git_cmd = "git.exe" if os.name == "nt" else "git"
        subprocess.run([git_cmd, "--version"], capture_output=True, check=True, creationflags=CREATE_NO_WINDOW)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False

def find_kicad_cli():
    """Returns the path to kicad-cli, searching known locations if not in PATH."""
    if os.name == "nt":
        return shutil.which("kicad-cli.exe") or shutil.which("kicad-cli") or "kicad-cli.exe"
    candidate = shutil.which("kicad-cli")
    if candidate:
        return candidate
    # macOS fallback — KiCad installer does not add to PATH
    for path in [
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
        "/Applications/KiCad/kicad-cli",
    ]:
        if os.path.isfile(path):
            return path
    return "kicad-cli"