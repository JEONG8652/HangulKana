"""HangulKana.exe 빌드 (PyInstaller).

    .venv\\Scripts\\python.exe -X utf8 tools\\build_exe.py [--dist 폴더]

결과: dist\\HangulKana.exe (파일 하나, 콘솔 창 없음). 중간 파일은 build\\ 에 둔다 (둘 다 .gitignore).
dist\\HangulKana.exe가 실행 중이면 덮어쓸 수 없으므로 --dist로 다른 폴더를 지정한다.
빌드만 하고 실행하지 않는다.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build")
DIST = os.path.join(ROOT, "dist")


def make_icon(path):
    """트레이 활성 아이콘과 같은 모양의 .ico (여러 크기)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((16, 16, 240, 240), fill="#2e9d4f")
    d.rectangle((88, 72, 168, 184), fill="white")
    img.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def main():
    import argparse

    import PyInstaller.__main__

    ap = argparse.ArgumentParser(description="HangulKana.exe 빌드")
    ap.add_argument("--dist", default=DIST, help="결과 폴더 (기본 dist)")
    dist = os.path.abspath(ap.parse_args().dist)
    os.makedirs(BUILD, exist_ok=True)
    icon = os.path.join(BUILD, "HangulKana.ico")
    make_icon(icon)
    entry = os.path.join(BUILD, "hangulkana_entry.py")
    with open(entry, "w", encoding="utf-8", newline="\n") as f:
        f.write("import sys\nfrom hangulkana.app import main\n\nsys.exit(main())\n")
    PyInstaller.__main__.run([
        entry,
        "--name", "HangulKana",
        "--onefile",
        "--noconsole",
        "--icon", icon,
        "--paths", ROOT,
        "--hidden-import", "pystray._win32",  # pystray는 백엔드를 실행 중에 고르므로 명시
        "--exclude-module", "tkinter",
        "--exclude-module", "pytest",
        "--distpath", dist,
        "--workpath", os.path.join(BUILD, "pyinstaller"),
        "--specpath", BUILD,
        "--noconfirm",
        "--clean",
        "--log-level", "WARN",
    ])
    exe = os.path.join(dist, "HangulKana.exe")
    print(f"완료: {exe} ({os.path.getsize(exe) / 1e6:.1f} MB)")


if __name__ == "__main__":
    sys.exit(main())
