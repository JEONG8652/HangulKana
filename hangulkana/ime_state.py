"""포커스 창의 입력 언어·IME 모드·권한 감시.

훅 콜백 밖의 감시 스레드에서 조회하고, 결과(ImeState)를 속성 하나에 통째로 바꿔 넣는다.
훅 콜백은 current 속성만 읽고, 재조회가 필요하면 request_refresh()로 플래그만 세운다 (락 없음).
"""

import ctypes
import logging
import threading
import time
from ctypes import wintypes
from typing import NamedTuple, Optional

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
imm32 = ctypes.WinDLL("imm32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t

WM_IME_CONTROL = 0x0283
IMC_GETCONVERSIONMODE = 0x0001
IMC_GETOPENSTATUS = 0x0005
SMTO_ABORTIFHUNG = 0x0002
IME_CMODE_NATIVE = 0x0001
LANG_JAPANESE = 0x0411
VK_CAPITAL = 0x14

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenIntegrityLevel = 25


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND), ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
        ("rcCaret", RECT),
    ]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
user32.GetKeyboardLayout.restype = ctypes.c_void_p
user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.GetGUIThreadInfo.restype = wintypes.BOOL
user32.GetKeyState.argtypes = [ctypes.c_int]
user32.GetKeyState.restype = ctypes.c_short
user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ULONG_PTR),
]
user32.SendMessageTimeoutW.restype = LRESULT
imm32.ImmGetDefaultIMEWnd.argtypes = [wintypes.HWND]
imm32.ImmGetDefaultIMEWnd.restype = wintypes.HWND
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.GetCurrentProcessId.restype = wintypes.DWORD
advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
advapi32.GetTokenInformation.restype = wintypes.BOOL
advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)


class ImeState(NamedTuple):
    active: bool
    foreground: int  # 조회 시점의 포그라운드 창 핸들. 훅은 현재 포그라운드와 다르면 비활성으로 본다
    reason: str  # 비활성 사유 또는 "활성"


INACTIVE_INIT = ImeState(False, 0, "초기화 중")


def _token_integrity(process_handle) -> Optional[int]:
    tok = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(process_handle, TOKEN_QUERY, ctypes.byref(tok)):
        return None
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(tok, TokenIntegrityLevel, None, 0, ctypes.byref(size))
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(tok, TokenIntegrityLevel, buf, size, ctypes.byref(size)):
            return None
        sid = SID_AND_ATTRIBUTES.from_buffer(buf).Sid
        count = advapi32.GetSidSubAuthorityCount(sid)[0]
        return advapi32.GetSidSubAuthority(sid, count - 1)[0]
    finally:
        kernel32.CloseHandle(tok)


def process_integrity(pid: int) -> Optional[int]:
    """프로세스의 무결성 수준 RID (일반 0x2000, 관리자 0x3000). 확인 불가면 None."""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        return _token_integrity(h)
    finally:
        kernel32.CloseHandle(h)


def _ime_control(ime_wnd, cmd) -> Optional[int]:
    result = ULONG_PTR()
    ok = user32.SendMessageTimeoutW(ime_wnd, WM_IME_CONTROL, cmd, 0, SMTO_ABORTIFHUNG, 100, ctypes.byref(result))
    return result.value if ok else None


def caps_lock_on() -> bool:
    return bool(user32.GetKeyState(VK_CAPITAL) & 1)


class ImeWatcher(threading.Thread):
    """200ms 주기 + 재조회 요청 시 즉시 포커스 창 상태를 조회한다."""

    POLL_SEC = 0.2
    TICK_SEC = 0.02

    def __init__(self):
        super().__init__(name="HangulKana-ImeWatcher", daemon=True)
        self.current: ImeState = INACTIVE_INIT
        self.paused = False
        self._refresh = False
        self._stop = False
        self._own_pid = kernel32.GetCurrentProcessId()
        self._own_integrity = _token_integrity(kernel32.GetCurrentProcess())
        self._integrity_cache = {}

    # 훅 콜백에서 호출: 플래그만 세운다
    def request_refresh(self):
        self._refresh = True

    def stop(self):
        self._stop = True

    def run(self):
        last = 0.0
        while not self._stop:
            now = time.monotonic()
            if self._refresh or now - last >= self.POLL_SEC:
                self._refresh = False
                last = now
                try:
                    state = self._compute()
                except Exception:
                    log.exception("IME 상태 조회 실패")
                    state = ImeState(False, 0, "조회 오류")
                if state != self.current:
                    log.debug("상태: %s", state.reason)
                self.current = state
            time.sleep(self.TICK_SEC)

    def _integrity(self, pid):
        if pid not in self._integrity_cache:
            if len(self._integrity_cache) > 256:
                self._integrity_cache.clear()
            self._integrity_cache[pid] = process_integrity(pid)
        return self._integrity_cache[pid]

    def _compute(self) -> ImeState:
        fg = user32.GetForegroundWindow() or 0
        if self.paused:
            return ImeState(False, fg, "일시 정지")
        if not fg:
            return ImeState(False, 0, "포그라운드 창 없음")
        if caps_lock_on():
            return ImeState(False, fg, "Caps Lock 켜짐")
        pid = wintypes.DWORD()
        tid = user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
        if pid.value == self._own_pid:
            return ImeState(False, fg, "자기 창")
        own = self._own_integrity
        target = self._integrity(pid.value)
        if own is None or target is None or target > own:
            return ImeState(False, fg, "권한 높음/확인 불가")

        # 레이아웃은 포커스 창을 소유한 스레드에서 읽는다 (Win11 메모장은 최상위 창과 입력칸의 스레드가 다르다)
        hwnd = fg
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        if user32.GetGUIThreadInfo(tid, ctypes.byref(info)) and info.hwndFocus:
            hwnd = info.hwndFocus
            tid = user32.GetWindowThreadProcessId(hwnd, None)
        layout = (user32.GetKeyboardLayout(tid) or 0) & 0xFFFF
        if layout != LANG_JAPANESE:
            return ImeState(False, fg, f"일본어 아님(0x{layout:04X})")
        ime_wnd = imm32.ImmGetDefaultIMEWnd(hwnd)
        if not ime_wnd:
            return ImeState(False, fg, "IME 창 없음")
        if not _ime_control(ime_wnd, IMC_GETOPENSTATUS):
            return ImeState(False, fg, "IME 꺼짐/조회 실패")
        conv = _ime_control(ime_wnd, IMC_GETCONVERSIONMODE)
        if conv is None or not conv & IME_CMODE_NATIVE:
            return ImeState(False, fg, "영문 모드/조회 실패")
        return ImeState(True, fg, "활성")
