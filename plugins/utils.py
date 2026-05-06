import os
import json
import subprocess
import shutil

# Fix for Windows: prevents the plugin from popping up CMD windows or hanging
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

def get_settings_path():
    """Returns the path for the global plugin settings file."""
    return os.path.expanduser('~/.kicad_git_diff_settings.json')

def load_settings():
    """Loads settings from the user's home directory."""
    try:
        with open(get_settings_path(), 'r') as f:
            return json.load(f)
    except Exception:
        # Default settings if file doesn't exist
        return {'include_kicad_version': True}

def save_settings(settings):
    """Saves settings to the user's home directory."""
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
        return "kicad-cli.exe"
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