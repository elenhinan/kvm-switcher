import os
import subprocess
import sys
from PIL import Image, ImageDraw

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(BASE_DIR, "pyKVM.ico")
SCRIPT_PATH = os.path.join(BASE_DIR, "pyKVM.pyw")


def generate_ico(path):
    """Generates a multi-resolution Windows .ico file matching the tray monitor icon."""
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = "#2ecc71"
    # Monitor bezel
    draw.rounded_rectangle([20, 28, 236, 176], radius=16, outline=color, width=16)
    draw.rectangle([48, 56, 208, 148], fill=color)
    # Stand
    draw.rectangle([112, 176, 144, 216], fill=color)
    draw.rounded_rectangle([72, 216, 184, 232], radius=6, fill=color)
    img.save(path, format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])


def main():
    print("Stopping any running pyKVM.exe instance...")
    subprocess.run(
        ["taskkill", "/F", "/IM", "pyKVM.exe"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    print("Generating application icon (pyKVM.ico)...")
    generate_ico(ICON_PATH)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        "pyKVM",
        "--icon",
        ICON_PATH,
        "--add-data",
        f"{ICON_PATH};.",
        "--hidden-import",
        "pystray._win32",
        "--hidden-import",
        "pythoncom",
        "--hidden-import",
        "pywintypes",
        "--hidden-import",
        "win32api",
        "--hidden-import",
        "wmi",
        SCRIPT_PATH,
    ]

    print("Running PyInstaller...")
    subprocess.check_call(cmd, cwd=BASE_DIR)

    exe_path = os.path.join(BASE_DIR, "dist", "pyKVM.exe")
    print(f"\nBuild complete! Executable created at:\n  {exe_path}")


if __name__ == "__main__":
    main()
