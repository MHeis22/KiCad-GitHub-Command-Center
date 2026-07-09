import os
import json
import subprocess
import shutil

# Fix for Windows: prevents the plugin from popping up CMD windows or hanging
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

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