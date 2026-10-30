"""트레이, 한글 띄움창, 설정, 로깅, 수명 주기.

개발용 실행 (안전장치: 기본 dry-run, 시간 제한):
    .venv\\Scripts\\python.exe -X utf8 -m hangulkana                 # dry-run, 60초 후 자동 종료
    .venv\\Scripts\\python.exe -X utf8 -m hangulkana --live --max-seconds 300

실행 파일(HangulKana.exe, PyInstaller)은 실사용용이라 기본이 live·시간 제한 없음이다 (--dry-run으로 바꿀 수 있음).
설정·로그 위치: 실행 파일은 %APPDATA%\\HangulKana\\, 소스 실행은 프로젝트의 .devdata\\ (개발 중에는 프로젝트 밖에 쓰지 않는다).

비상 정지: Ctrl+Alt+Shift+Q. 그래도 안 되면 Ctrl+Alt+Del → 작업 관리자에서 python.exe·HangulKana.exe 종료.
"""

import argparse
import ctypes
import gc
import logging
import logging.handlers
import os
import sys
import threading
import time
from ctypes import wintypes

from . import config as config_mod
from .engine import Backspace, Preview, ResendKey, Romaji, Text
from .hook import KeyboardHook
from .ime_state import ImeWatcher

log = logging.getLogger("hangulkana")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
HANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
kernel32.SetConsoleCtrlHandler.argtypes = [HANDLER_ROUTINE, wintypes.BOOL]
kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL

MUTEX_NAME = "Local\\HangulKana-SingleInstance"
ERROR_ALREADY_EXISTS = 183
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FROZEN = getattr(sys, "frozen", False)  # PyInstaller 실행 파일인가


def default_data_dir():
    """설정·로그 폴더. 실행 파일은 %APPDATA%\\HangulKana, 소스 실행(개발)은 프로젝트 안 .devdata."""
    if FROZEN:
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "HangulKana")
    return os.path.join(PROJECT_ROOT, ".devdata")


def message_box(text):
    """콘솔이 없는 실행 파일에서 사용자에게 알릴 때."""
    try:
        ctypes.windll.user32.MessageBoxW(None, text, "HangulKana", 0x40)  # MB_ICONINFORMATION
    except Exception:
        pass


def acquire_single_instance():
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return None
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return handle


def setup_logging(file_level, logdir):
    """파일 로그는 설정의 수준(기본 error), 콘솔(소스 실행)은 info 이상.

    debug 수준에서만 입력 내용이 기록된다. 로그 폴더는 설정 폴더 아래 logs."""
    root = logging.getLogger()
    level = getattr(logging, file_level.upper())
    root.setLevel(min(level, logging.INFO))
    if sys.stdout is not None:  # 콘솔 없는 실행 파일에서는 None
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(min(level, logging.INFO))
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        root.addHandler(console)
    try:
        os.makedirs(logdir, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            os.path.join(logdir, "hangulkana.log"), maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root.addHandler(fh)
    except OSError as e:
        log.error("로그 파일을 만들 수 없습니다: %s", e)


def describe_actions(actions):
    parts = []
    for a in actions:
        if isinstance(a, Romaji):
            parts.append(a.text)
        elif isinstance(a, Backspace):
            parts.append(f"BS×{a.count}")
        elif isinstance(a, Text):
            parts.append(f"문자 {a.text}")
        elif isinstance(a, Preview):
            parts.append(f"미리보기 {a.letter}")
        elif isinstance(a, ResendKey):
            parts.append("+원래키")
    return " ".join(parts) or "(삼킴)"


# ---------------------------------------------------------------- 한글 띄움창
#
# 지금 치는 한글을 글자 커서 위에 작게 보여 준다. 포커스·클릭을 가져가지 않는 창이어야 하므로
# tkinter 대신 Win32 창을 직접 만든다 (WS_EX_NOACTIVATE: 활성화 안 됨, WS_EX_TRANSPARENT: 클릭 통과).
# 그림은 Pillow로 그려(둥근 모서리·그림자·반투명) UpdateLayeredWindow로 픽셀 단위 투명도를 입힌다.
# 자기 스레드의 타이머로 30ms마다 훅이 남긴 문자열을 읽는다. 훅 콜백은 튜플 대입만 한다.

gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
shcore = ctypes.WinDLL("shcore", use_last_error=True)
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON),
    ]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND), ("rcCaret", wintypes.RECT),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 1)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", wintypes.BYTE), ("BlendFlags", wintypes.BYTE),
                ("SourceConstantAlpha", wintypes.BYTE), ("AlphaFormat", wintypes.BYTE)]


def _sig(dll, name, restype, *argtypes):
    f = getattr(dll, name)
    f.restype, f.argtypes = restype, list(argtypes)


_H, _P = wintypes.HANDLE, ctypes.c_void_p
_sig(kernel32, "GetModuleHandleW", wintypes.HMODULE, wintypes.LPCWSTR)
_sig(user32, "RegisterClassExW", wintypes.ATOM, ctypes.POINTER(WNDCLASSEXW))
_sig(user32, "UnregisterClassW", wintypes.BOOL, wintypes.LPCWSTR, wintypes.HINSTANCE)
_sig(user32, "CreateWindowExW", wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
     ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, _P)
_sig(user32, "DefWindowProcW", LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_sig(user32, "DestroyWindow", wintypes.BOOL, wintypes.HWND)
_sig(user32, "PostQuitMessage", None, ctypes.c_int)
_sig(user32, "GetMessageW", wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
_sig(user32, "TranslateMessage", wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
_sig(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(wintypes.MSG))
_sig(user32, "PostMessageW", wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_sig(user32, "SetTimer", ctypes.c_size_t, wintypes.HWND, ctypes.c_size_t, wintypes.UINT, _P)
_sig(user32, "KillTimer", wintypes.BOOL, wintypes.HWND, ctypes.c_size_t)
_sig(user32, "SetWindowPos", wintypes.BOOL, wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
     ctypes.c_int, ctypes.c_int, wintypes.UINT)
_sig(user32, "ShowWindow", wintypes.BOOL, wintypes.HWND, ctypes.c_int)
_sig(user32, "GetDC", wintypes.HDC, wintypes.HWND)
_sig(user32, "ReleaseDC", ctypes.c_int, wintypes.HWND, wintypes.HDC)
_sig(user32, "UpdateLayeredWindow", wintypes.BOOL, wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
     ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
     ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD)
_sig(user32, "GetForegroundWindow", wintypes.HWND)
_sig(user32, "GetWindowThreadProcessId", wintypes.DWORD, wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_sig(user32, "GetGUIThreadInfo", wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(GUITHREADINFO))
_sig(user32, "ClientToScreen", wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.POINT))
_sig(user32, "MonitorFromWindow", _H, wintypes.HWND, wintypes.DWORD)
_sig(user32, "MonitorFromPoint", _H, wintypes.POINT, wintypes.DWORD)
_sig(user32, "GetMonitorInfoW", wintypes.BOOL, _H, ctypes.POINTER(MONITORINFO))
_sig(user32, "SetThreadDpiAwarenessContext", _H, _H)
_sig(shcore, "GetDpiForMonitor", ctypes.HRESULT, _H, ctypes.c_int, ctypes.POINTER(wintypes.UINT),
     ctypes.POINTER(wintypes.UINT))
_sig(gdi32, "CreateCompatibleDC", wintypes.HDC, wintypes.HDC)
_sig(gdi32, "DeleteDC", wintypes.BOOL, wintypes.HDC)
_sig(gdi32, "CreateDIBSection", wintypes.HBITMAP, wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
     ctypes.POINTER(_P), _H, wintypes.DWORD)
_sig(gdi32, "SelectObject", wintypes.HGDIOBJ, wintypes.HDC, wintypes.HGDIOBJ)
_sig(gdi32, "DeleteObject", wintypes.BOOL, wintypes.HGDIOBJ)

WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_TRANSPARENT = 0x00000008, 0x00000080, 0x00000020
WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x00080000, 0x08000000
WM_DESTROY, WM_CLOSE, WM_TIMER, WM_MOUSEACTIVATE = 0x0002, 0x0010, 0x0113, 0x0021
MA_NOACTIVATE = 3
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
SWP_NOSIZE, SWP_NOACTIVATE = 0x0001, 0x0010
HWND_TOPMOST = wintypes.HWND(-1)
ULW_ALPHA, AC_SRC_ALPHA = 0x2, 0x1
MONITOR_DEFAULTTONEAREST = 2
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = _H(-4)


class Bubble:
    """띄움창 그림 (Pillow). 한글은 맑은 고딕, 그 밖(゛·ー·가나)은 Yu Gothic으로 글자마다 고른다
    (맑은 고딕에는 ゛·ー가 없고, Yu Gothic에는 한글이 없다)."""

    SIZE = 20  # 100% 배율 기준 글자 크기 (px)
    BG = (30, 32, 38, 236)
    BORDER = (255, 255, 255, 34)
    TEXT = (244, 246, 250, 255)
    TYPING = (110, 214, 142, 255)  # 아직 치는 중인 음절·초성 (트레이 활성 아이콘과 같은 초록 계열)
    SUPERSAMPLE = 3

    def __init__(self):
        from PIL import ImageFont  # 띄움창을 쓸 때만 불러온다

        self._ImageFont = ImageFont
        fonts = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
        self._paths = {"hangul": os.path.join(fonts, "malgun.ttf"), "other": os.path.join(fonts, "YuGothM.ttc")}
        self._cache = {}

    def _font(self, kind, px):
        key = (kind, px)
        if key not in self._cache:
            try:
                self._cache[key] = self._ImageFont.truetype(self._paths[kind], px)
            except OSError:
                other = self._paths["hangul" if kind == "other" else "other"]
                try:
                    self._cache[key] = self._ImageFont.truetype(other, px)
                except OSError:
                    self._cache[key] = self._ImageFont.load_default(px)
        return self._cache[key]

    @staticmethod
    def _is_hangul(ch):
        cp = ord(ch)
        return 0xAC00 <= cp <= 0xD7A3 or 0x3130 <= cp <= 0x318F or 0x1100 <= cp <= 0x11FF

    def _runs(self, text, color, px):
        runs = []
        for ch in text:
            kind = "hangul" if self._is_hangul(ch) else ("mark" if ch == "゛" else "other")
            if runs and runs[-1][1] == kind and kind != "mark":
                runs[-1][0] += ch
            else:
                runs.append([ch, kind, color])
        return [(s, self._font("other" if k == "mark" else k, px), c, k == "mark") for s, k, c in runs]

    def _ink(self, s, f):
        """글자의 실제 잉크 가로 범위 (왼쪽, 오른쪽). getbbox는 전각 칸 전체를 돌려주므로 그려서 잰다."""
        key = ("ink", s, id(f))
        if key not in self._cache:
            from PIL import Image, ImageDraw

            size = round(f.getlength(s)) * 2 + 4
            im = Image.new("L", (size, size), 0)
            ImageDraw.Draw(im).text((2, size - 2), s, font=f, fill=255, anchor="ls")
            box = im.getbbox()
            self._cache[key] = (box[0] - 2, box[2] - 2) if box else (0, round(f.getlength(s)))
        return self._cache[key]

    def _advance(self, s, f, mark):
        if mark:
            left, right = self._ink(s, f)
            return right - left + 2
        return f.getlength(s)

    def render(self, fixed, typing, scale):
        """(너비, 높이, 미리 곱한 알파 BGRA 바이트). 창 크기 = 말풍선 + 그림자 여백."""
        from PIL import Image, ImageDraw, ImageFilter

        px = max(10, round(self.SIZE * scale))
        pad_x, pad_y = round(13 * scale), round(6 * scale)
        radius, margin = round(9 * scale), round(9 * scale)
        runs = self._runs(fixed, self.TEXT, px) + self._runs(typing, self.TYPING, px)
        ascent = max(f.getmetrics()[0] for _, f, _, _ in runs)
        descent = max(f.getmetrics()[1] for _, f, _, _ in runs)
        text_w = round(sum(self._advance(s, f, mark) for s, f, _, mark in runs))
        bw, bh = text_w + 2 * pad_x, ascent + descent + 2 * pad_y
        w, h = bw + 2 * margin, bh + 2 * margin

        # 그림자: 아래로 살짝 내린 흐린 둥근 사각형
        shadow_mask = Image.new("L", (w, h), 0)
        off = max(1, round(2 * scale))
        ImageDraw.Draw(shadow_mask).rounded_rectangle(
            (margin, margin + off, margin + bw - 1, margin + bh - 1 + off), radius, fill=120)
        shadow_mask = shadow_mask.filter(ImageFilter.GaussianBlur(max(1, 4 * scale)))
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        img.putalpha(shadow_mask)

        # 말풍선: 크게 그려 줄여서 모서리를 매끄럽게
        ss = self.SUPERSAMPLE
        big = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
        ImageDraw.Draw(big).rounded_rectangle(
            (margin * ss, margin * ss, (margin + bw) * ss - 1, (margin + bh) * ss - 1), radius * ss,
            fill=self.BG, outline=self.BORDER, width=max(1, round(scale)) * ss)
        img.alpha_composite(big.resize((w, h), Image.LANCZOS))

        # 글자: 색마다 마스크를 만들어 합성 (가장자리 안티앨리어싱 유지)
        x, base = margin + pad_x, margin + pad_y + ascent
        for s, f, color, mark in runs:
            mask = Image.new("L", (w, h), 0)
            # ゛는 전각 문자라 그대로 두면 앞 글자와 크게 벌어진다 → 잉크 부분만 앞 글자 바로 뒤에 붙인다
            dx = 1 - self._ink(s, f)[0] if mark else 0
            ImageDraw.Draw(mask).text((x + dx, base), s, font=f, fill=255, anchor="ls")
            layer = Image.new("RGBA", (w, h), color[:3] + (0,))
            layer.putalpha(mask.point(lambda v, a=color[3]: v * a // 255))
            img.alpha_composite(layer)
            x += self._advance(s, f, mark)

        r, g, b, a = img.convert("RGBa").split()  # RGBa = 알파를 미리 곱한 형식 (UpdateLayeredWindow 요구)
        return w, h, Image.merge("RGBA", (b, g, r, a)).tobytes()


class Overlay(threading.Thread):
    TIMER_MS = 30
    GAP, MARGIN = 2, 16  # 커서와의 간격, 화면 가장자리 여백 (100% 기준)
    CLASS_NAME = "HangulKanaOverlay"

    def __init__(self, source, watcher):
        """source(): (확정, 치는 중) 문자열을 돌려주는 함수 (훅의 display)."""
        super().__init__(name="HangulKana-Overlay", daemon=True)
        self.source = source
        self.watcher = watcher
        self.enabled = True  # 트레이 메뉴에서 켜고 끈다
        self._hwnd = None
        self._stopping = False  # threading.Thread._stop과 이름이 겹치지 않게
        self._bubble = None
        self._rendered = None  # (parts, scale) → (w, h)
        self._pos = None
        self._visible = False
        self._wndproc = WNDPROC(self._proc)  # GC 방지

    def stop(self):
        self._stopping = True
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)

    def run(self):
        # 이 스레드만 모니터별 DPI 인식: 고배율에서도 글자가 선명하고 좌표가 실제 픽셀이 된다.
        # 훅·트레이 스레드에는 영향 없음. 실패하면(구형 Windows) 그냥 진행
        try:
            user32.SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        except (AttributeError, OSError):
            pass
        try:
            self._bubble = Bubble()
        except ImportError:
            log.error("Pillow를 불러올 수 없어 한글 띄움창 없이 실행합니다.")
            return
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = self.CLASS_NAME
        registered = user32.RegisterClassExW(ctypes.byref(wc))
        try:
            if not registered:
                log.error("띄움창 클래스 등록 실패: %s", ctypes.get_last_error())
                return
            ex = WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED | WS_EX_TRANSPARENT
            self._hwnd = user32.CreateWindowExW(ex, self.CLASS_NAME, "HangulKana", WS_POPUP,
                                                0, 0, 1, 1, None, None, hinst, None)
            if not self._hwnd:
                log.error("띄움창 생성 실패: %s", ctypes.get_last_error())
                return
            user32.SetTimer(self._hwnd, 1, self.TIMER_MS, None)
            if self._stopping:
                user32.DestroyWindow(self._hwnd)
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            log.exception("띄움창 오류")
        finally:
            if self._hwnd:
                user32.DestroyWindow(self._hwnd)  # 이미 파괴됐으면 아무 일도 없다
                self._hwnd = None
            if registered:
                user32.UnregisterClassW(self.CLASS_NAME, hinst)

    # ------------------------------------------------------------ 창 프로시저

    def _proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TIMER:
                self._tick()
                return 0
            if msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE
            if msg == WM_CLOSE:
                user32.KillTimer(hwnd, 1)
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                self._hwnd = None
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("띄움창 처리 오류")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _tick(self):
        st = self.watcher.current
        parts = self.source() if self.enabled and st.active and not self.watcher.paused else ("", "")
        if not any(parts):
            if self._visible:
                user32.ShowWindow(self._hwnd, SW_HIDE)
                self._visible = False
                self._pos = None
            return
        caret, work, dpi = self._anchor()
        scale = dpi / 96
        key = (parts, scale)
        if self._rendered is None or self._rendered[0] != key:
            w, h, bits = self._bubble.render(parts[0], parts[1], scale)
            pos = self._place(caret, work, w, h, scale)
            self._update(bits, w, h, pos)
            self._rendered, self._pos = (key, (w, h)), pos
        else:
            w, h = self._rendered[1]
            pos = self._place(caret, work, w, h, scale)
            if pos != self._pos:
                user32.SetWindowPos(self._hwnd, HWND_TOPMOST, pos[0], pos[1], 0, 0, SWP_NOSIZE | SWP_NOACTIVATE)
                self._pos = pos
        if not self._visible:
            user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
            user32.SetWindowPos(self._hwnd, HWND_TOPMOST, 0, 0, 0, 0, 0x0002 | SWP_NOSIZE | SWP_NOACTIVATE)
            self._visible = True

    def _anchor(self):
        """(커서 위치 (x, 위, 아래) 또는 None, 모니터 작업 영역, DPI)."""
        fg = user32.GetForegroundWindow()
        caret = None
        gti = GUITHREADINFO()
        gti.cbSize = ctypes.sizeof(GUITHREADINFO)
        tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        if tid and user32.GetGUIThreadInfo(tid, ctypes.byref(gti)) and gti.hwndCaret \
                and gti.rcCaret.bottom > gti.rcCaret.top:
            top = wintypes.POINT(gti.rcCaret.left, gti.rcCaret.top)
            bottom = wintypes.POINT(gti.rcCaret.left, gti.rcCaret.bottom)
            if user32.ClientToScreen(gti.hwndCaret, ctypes.byref(top)) and \
                    user32.ClientToScreen(gti.hwndCaret, ctypes.byref(bottom)):
                caret = (top.x, top.y, bottom.y)
        hmon = (user32.MonitorFromPoint(wintypes.POINT(caret[0], caret[1]), MONITOR_DEFAULTTONEAREST) if caret
                else user32.MonitorFromWindow(fg, MONITOR_DEFAULTTONEAREST))
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        work = mi.rcWork if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)) else None
        dx, dy = wintypes.UINT(96), wintypes.UINT(96)
        try:
            shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy))  # MDT_EFFECTIVE_DPI
        except OSError:
            pass
        return caret, work, dx.value or 96

    def _place(self, caret, work, w, h, scale):
        """창 왼쪽 위 좌표. 말풍선이 커서 바로 위에 오도록 (그림자 여백만큼 보정). 화면 밖으로 나가지 않게."""
        margin = round(9 * scale)  # Bubble의 그림자 여백과 같다
        gap = round(self.GAP * scale)
        if caret is not None:
            x = caret[0] - margin
            y = caret[1] - gap - (h - margin)
            if work is not None and y < work.top:  # 위에 자리가 없으면 커서 아래로
                y = caret[2] + gap - margin
        elif work is not None:
            edge = round(self.MARGIN * scale)
            return work.right - w - edge, work.bottom - h - edge
        else:
            return 0, 0
        if work is not None:
            x = min(max(x, work.left), work.right - w)
        return x, y

    def _update(self, bits, w, h, pos):
        """미리 곱한 알파 BGRA 그림으로 창을 갱신한다."""
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = w, -h  # 위에서 아래로
        bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
        ptr = _P()
        hbmp = gdi32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(ptr), None, 0)
        try:
            if not hbmp or not ptr:
                raise OSError(f"CreateDIBSection 실패: {ctypes.get_last_error()}")
            ctypes.memmove(ptr, bits, len(bits))
            old = gdi32.SelectObject(mem, hbmp)
            blend = BLENDFUNCTION(0, 0, 255, AC_SRC_ALPHA)
            user32.UpdateLayeredWindow(self._hwnd, screen, ctypes.byref(wintypes.POINT(*pos)),
                                       ctypes.byref(wintypes.SIZE(w, h)), mem, ctypes.byref(wintypes.POINT(0, 0)),
                                       0, ctypes.byref(blend), ULW_ALPHA)
            gdi32.SelectObject(mem, old)
        finally:
            if hbmp:
                gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(None, screen)


class App:
    def __init__(self, args, cfg, config_path, logdir):
        self.args = args
        self.cfg = cfg
        self.config_path = config_path
        self.logdir = logdir
        self.stop_event = threading.Event()
        self.watcher = ImeWatcher()
        trace = args.debug or cfg.log_level == "debug"
        hotkey = config_mod.parse_hotkey(cfg.pause_hotkey) if cfg.pause_hotkey else None
        self.hook = KeyboardHook(self.watcher, dry_run=not args.live, on_emergency=self.request_stop,
                                 preview=cfg.preview and not args.no_preview, trace=trace,
                                 pause_hotkey=hotkey, on_pause=self.toggle_pause)
        self.overlay = Overlay(lambda: self.hook.display, self.watcher)
        self.overlay.enabled = cfg.overlay and not args.no_overlay
        self.icon = None
        self._ctrl_handler = HANDLER_ROUTINE(self._on_console_ctrl)  # GC 방지

    def toggle_pause(self):
        """트레이 메뉴·단축키 공용. 훅 콜백에서도 불리므로 플래그 전환만 한다."""
        self.watcher.paused = not self.watcher.paused
        self.watcher.request_refresh()
        self.hook.logq.put(("info", "일시 정지" if self.watcher.paused else "일시 정지 해제"))

    def toggle_overlay(self):
        self.overlay.enabled = not self.overlay.enabled
        self.cfg.overlay = self.overlay.enabled
        try:
            config_mod.save(self.config_path, self.cfg)
        except OSError as e:
            log.error("설정 저장 실패: %s", e)

    @staticmethod
    def _open(path):
        try:
            os.makedirs(path if not os.path.splitext(path)[1] else os.path.dirname(path), exist_ok=True)
            os.startfile(path)  # 설정 파일은 기본 편집기, 폴더는 탐색기로 연다
        except OSError as e:
            log.error("열 수 없습니다: %s (%s)", path, e)

    def request_stop(self, reason="종료 요청"):
        """어느 스레드(훅 콜백 포함)에서든 부를 수 있도록 가볍게: 플래그만 세운다."""
        if not self.stop_event.is_set():
            self.hook.logq.put(("info", f"종료: {reason}"))
            self.stop_event.set()

    def _stopper(self):
        """종료 요청이 오면 훅 스레드와 트레이를 멈춘다."""
        self.stop_event.wait()
        self.hook.stop()
        self.overlay.stop()
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                log.exception("트레이 종료 실패")

    def _on_console_ctrl(self, ctrl_type):
        self.request_stop("Ctrl+C")
        return True

    # ------------------------------------------------------------ 보조 스레드

    def _log_pump(self):
        """훅 콜백이 큐에 넣은 기록을 출력한다 (콜백 안에서는 I/O 금지)."""
        last_reason = None
        while not (self.stop_event.is_set() and self.hook.logq.empty()):
            reason = self.watcher.current.reason
            if reason != last_reason:
                log.info("상태: %s", reason)
                last_reason = reason
            try:
                kind, payload = self.hook.logq.get(timeout=0.1)
            except Exception:
                continue
            if kind == "dry":
                vk, ev_kind, actions = payload
                log.info("[dry-run] VK 0x%02X (%s) → 삼키고 보낼 것: %s", vk, ev_kind, describe_actions(actions))
            elif kind == "live":
                vk, ev_kind, actions = payload
                sent = "통과" if actions is None else "삼키고 보냄: " + describe_actions(actions)
                log.debug("[live] VK 0x%02X (%s) %s | 상태: %s", vk, ev_kind, sent, self.watcher.current.reason)
            elif kind == "error":
                log.error(payload)
            else:
                log.info(payload)

    def _deadline(self):
        if self.args.max_seconds > 0 and not self.stop_event.wait(self.args.max_seconds):
            self.request_stop(f"--max-seconds {self.args.max_seconds}초 경과")

    # ------------------------------------------------------------ 트레이

    def _make_icons(self):
        from PIL import Image, ImageDraw

        icons = {}
        for name, color in (("active", "#2e9d4f"), ("standby", "#8a8a8a"), ("paused", "#d9822b")):
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.ellipse((4, 4, 60, 60), fill=color)
            d.rectangle((22, 18, 42, 46), fill="white")
            icons[name] = img
        return icons

    def _run_tray(self):
        try:
            import pystray
        except ImportError:
            log.error("pystray를 불러올 수 없어 트레이 없이 실행합니다.")
            self.stop_event.wait()
            return

        icons = self._make_icons()
        hotkey = f" ({self.cfg.pause_hotkey})" if self.hook.pause_hotkey else ""
        menu = pystray.Menu(
            pystray.MenuItem("한글 띄움창", lambda icon, item: self.toggle_overlay(),
                             checked=lambda item: self.overlay.enabled),
            pystray.MenuItem(f"일시 정지{hotkey}", lambda icon, item: self.toggle_pause(),
                             checked=lambda item: self.watcher.paused),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("설정 파일 열기", lambda icon, item: self._open(self.config_path)),
            pystray.MenuItem("로그 폴더 열기", lambda icon, item: self._open(self.logdir)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("종료", lambda icon, item: self.request_stop("트레이 종료")),
        )
        name = "HangulKana" if self.args.live else "HangulKana (dry-run)"
        self.icon = pystray.Icon("HangulKana", icons["standby"], name, menu)

        def updater(icon):
            icon.visible = True
            shown = None
            while not self.stop_event.is_set():
                st = self.watcher.current
                state = "paused" if self.watcher.paused else ("active" if st.active else "standby")
                if (state, st.reason) != shown:
                    icon.icon = icons[state]
                    icon.title = f"{name} - {'일시 정지' if state == 'paused' else st.reason}"[:127]
                    shown = (state, st.reason)
                time.sleep(0.3)

        if self.stop_event.is_set():
            return
        self.icon.run(setup=updater)

    # ------------------------------------------------------------ 실행

    def run(self):
        kernel32.SetConsoleCtrlHandler(self._ctrl_handler, True)
        pump = threading.Thread(target=self._log_pump, name="HangulKana-Log", daemon=True)
        pump.start()
        threading.Thread(target=self._stopper, name="HangulKana-Stopper", daemon=True).start()
        try:
            self.watcher.start()
            self.hook.start()
            self.hook.installed.wait(5)
            if not self.hook.is_alive():
                log.error("훅 설치에 실패해 종료합니다.")
                return 1
            self.overlay.start()
            gc.collect()
            gc.freeze()  # 큰 객체를 GC 대상에서 제외해 콜백 중 정지 시간 감소
            threading.Thread(target=self._deadline, name="HangulKana-Deadline", daemon=True).start()
            log.info("시작 (%s, 최대 %s초). 비상 정지: Ctrl+Alt+Shift+Q",
                     "LIVE" if self.args.live else "dry-run", self.args.max_seconds or "무제한")
            if self.args.no_tray:
                self.stop_event.wait()
            else:
                self._run_tray()
            return 0
        finally:
            self.request_stop("정리")
            self.hook.stop()
            self.hook.join(3)
            self.overlay.stop()
            if self.overlay.is_alive():
                self.overlay.join(1)
            self.watcher.stop()
            self.watcher.join(1)
            if self.hook.is_alive():
                log.error("훅 스레드가 3초 안에 끝나지 않았습니다. 프로세스를 종료하면 훅도 해제됩니다.")
            pump.join(1)
            kernel32.SetConsoleCtrlHandler(self._ctrl_handler, False)


def parse_args(argv=None, frozen=FROZEN):
    """소스 실행은 개발 안전장치가 기본(dry-run, 60초). 실행 파일은 실사용 기본(live, 무제한)."""
    ap = argparse.ArgumentParser(prog="hangulkana", description="두벌식 한글 → MS IME 로마자 변환기")
    ap.add_argument("--live", action="store_true", help="실제로 키를 삼키고 송신한다 (소스 실행의 기본은 dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="키를 가로채지 않고 보낼 내용만 기록한다")
    ap.add_argument("--max-seconds", type=int, default=None,
                    help="N초 후 자동 종료 (0 = 무제한. 기본: 소스 실행 60, 실행 파일 0)")
    ap.add_argument("--no-tray", action="store_true", help="트레이 아이콘 없이 실행")
    ap.add_argument("--debug", action="store_true", help="디버그 로그 (입력 내용이 기록됨)")
    ap.add_argument("--no-preview", action="store_true", help="대기 중인 초성을 로마자 한 글자(ㅋ → ｋ)로 미리 보여 주지 않는다")
    ap.add_argument("--no-overlay", action="store_true", help="한글 띄움창을 끈 채로 시작한다 (트레이 메뉴에서 다시 켤 수 있음)")
    ap.add_argument("--data-dir", default=None, help="설정·로그 폴더 (기본: 실행 파일 %%APPDATA%%\\HangulKana, 소스 .devdata)")
    args = ap.parse_args(argv)
    args.live = not args.dry_run and (args.live or frozen)
    if args.max_seconds is None:
        args.max_seconds = 0 if frozen else 60
    return args


def main(argv=None):
    args = parse_args(argv)
    data_dir = args.data_dir or default_data_dir()
    config_path = os.path.join(data_dir, "config.toml")
    logdir = os.path.join(data_dir, "logs")
    try:
        cfg = config_mod.load(config_path)
    except OSError as e:
        cfg = config_mod.Config(warnings=[f"설정 파일을 읽을 수 없어 기본값으로 실행합니다: {e}"])
    setup_logging("debug" if args.debug else cfg.log_level, logdir)
    for w in cfg.warnings:
        log.warning("설정: %s", w)
    mutex = acquire_single_instance()
    if mutex is None:
        log.error("HangulKana가 이미 실행 중입니다. 새로 훅을 설치하지 않고 종료합니다.")
        if FROZEN:
            message_box("HangulKana가 이미 실행 중입니다.\n작업 표시줄 오른쪽 아래 트레이 아이콘을 확인하세요.")
        return 2
    try:
        return App(args, cfg, config_path, logdir).run()
    finally:
        kernel32.CloseHandle(mutex)
