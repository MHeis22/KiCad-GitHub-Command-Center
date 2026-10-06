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
        with open(get_project_settings_path(project_dir), 'r') as f:
            return json.load(f)
    except Exception:
        return {}

def save_project_settings(project_dir, settings):
    """Saves per-project settings (only project-specific keys, not global ones)."""
    try:
        with open(get_project_settings_path(project_dir), 'w') as f:
            json.dump(settings, f)
    except Exception as e:
        print(f"Error saving project settings: {e}")

def get_last_target(project_dir):
    """Returns the last comparison target used for this project, or None."""
    return load_project_settings(project_dir).get('last_target')

def save_last_target(project_dir, target):
    """Persists the last comparison target for this project."""
    proj = load_project_settings(project_dir)
    proj['last_target'] = target
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