"""
Build standalone .exe for the tray launcher using PyInstaller.

Run:
    python tools/build_exe.py

Output:
    dist/USQuantLive.exe
"""
import shutil
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"


def ensure_pyinstaller():
    """Install PyInstaller if not present."""
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[Build] Installing PyInstaller...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"], check=True)


def main():
    ensure_pyinstaller()

    launcher_script = SCRIPT_DIR / "tray_launcher.py"
    icon_path = SCRIPT_DIR / "tray_icon.ico"

    if not launcher_script.exists():
        print(f"[Build] ERROR: launcher script not found: {launcher_script}")
        return 1

    if not icon_path.exists():
        print(f"[Build] Generating icon first...")
        from generate_tray_icon import main as gen_icon
        gen_icon()

    # Clean previous build
    for d in (DIST_DIR, BUILD_DIR):
        if d.exists():
            print(f"[Build] Removing old {d}")
            shutil.rmtree(d)

    # Build with PyInstaller
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "USQuantLive",
        "--icon", str(icon_path),
        "--onefile",                  # Single .exe
        "--windowed",                 # No console window
        "--noconfirm",
        "--clean",
        "--add-data", f"{icon_path};.",  # Bundle icon
        "--hidden-import", "pystray._win32",
        "--hidden-import", "PIL.Image",
        "--hidden-import", "PIL.ImageDraw",
        # Workdir
        "--distpath", str(DIST_DIR),
        "--workpath", str(BUILD_DIR),
        "--specpath", str(BUILD_DIR),
        str(launcher_script),
    ]

    print(f"[Build] Running PyInstaller...")
    print(f"[Build] Command: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))

    if result.returncode != 0:
        print(f"[Build] ERROR: PyInstaller failed with code {result.returncode}")
        return result.returncode

    exe_path = DIST_DIR / "USQuantLive.exe"
    if exe_path.exists():
        size_mb = exe_path.stat().st_size / 1024 / 1024
        print(f"\n[Build] SUCCESS!")
        print(f"[Build] Output: {exe_path}")
        print(f"[Build] Size: {size_mb:.1f} MB")
        print(f"\n========================================")
        print(f"IMPORTANT: How to run the .exe")
        print(f"========================================")
        print(f"")
        print(f"The .exe expects the project layout to be alongside it:")
        print(f"  <directory>/")
        print(f"    USQuantLive.exe           <- the bundled .exe")
        print(f"    tools/")
        print(f"      daily_alpaca_scheduler.py")
        print(f"      dashboard_server.py")
        print(f"      ...")
        print(f"    src/")
        print(f"    venv/Scripts/python.exe   <- venv with dependencies")
        print(f"    configs/alpaca_acounts/alpaca_accounts.local.json")
        print(f"")
        print(f"Option 1 (recommended): copy the .exe to the project root:")
        print(f"  copy {exe_path} {PROJECT_ROOT}\\USQuantLive.exe")
        print(f"")
        print(f"Option 2: ship the entire project tree along with the .exe.")
        print(f"")
        print(f"To use:")
        print(f"  1. Double-click USQuantLive.exe")
        print(f"  2. Look for the K-line icon in the system tray (bottom-right)")
        print(f"  3. Right-click for menu (Open Dashboard, Exit, etc.)")
    else:
        print(f"[Build] ERROR: Expected output not found: {exe_path}")
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
