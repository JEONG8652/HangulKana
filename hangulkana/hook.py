"""키보드·마우스 저수준 훅과 SendInput.

안전 원칙
- 콜백 맨 앞에서 비상 정지(Ctrl+Alt+Shift+Q)를 검사한다.
- 콜백은 예외를 밖으로 던지지 않는다. 예외가 나면 키를 통과시키고 변환기를 끈다.
- 주입(LLKHF_INJECTED) 이벤트는 무조건 통과시킨다.
- 키를 삼키는 것은 대체 입력 송신이 성공한 경우뿐이다. dry-run에서는 절대 삼키지 않는다.
- 훅 스레드가 어떤 경로로 끝나도 finally에서 UnhookWindowsHookEx를 호출한다.
- 콜백 안에서는 파일 I/O·sleep·락 대기를 하지 않는다. 로그는 큐에 넣기만 한다.
"""

import ctypes
import logging
import queue
import threading
from ctypes import wintypes

from . import layout
from .engine import Backspace, Engine, Preview, ResendKey, Romaji, Text

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
HC_ACTION = 0
WM_QUIT = 0x0012
WM_TIMER = 0x0113
WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN, WM_XBUTTONDOWN = 0x0201, 0x0204, 0x0207, 0x020B
LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10
LLKHF_UP = 0x80
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MAPVK_VK_TO_VSC = 0
EXTRA_INFO_TAG = 0x484B414E  # "HKAN": 우리가 보낸 이벤트 표시 (디버그용)

VK_BACK = 0x08
VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_LSHIFT, VK_RSHIFT = 0xA0, 0xA1
VK_LWIN, VK_RWIN = 0x5B, 0x5C
VK_Q = 0x51


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
WINEVENTPROC = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD,
)

user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.SetWinEventHook.argtypes = [
    wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, WINEVENTPROC, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
]
user32.SetWinEventHook.restype = wintypes.HANDLE
user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
user32.UnhookWinEvent.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.SetTimer.argtypes = [wintypes.HWND, ULONG_PTR, wintypes.UINT, ctypes.c_void_p]
user32.SetTimer.restype = ULONG_PTR
user32.KillTimer.argtypes = [wintypes.HWND, ULONG_PTR]
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
user32.GetLastInputInfo.restype = wintypes.BOOL
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.GetTickCount.restype = wintypes.DWORD


def _down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def _key_input(vk, up, scan=None, extended=False):
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki.wVk = vk
    inp.u.ki.wScan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC) if scan is None else scan
    inp.u.ki.dwFlags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if extended else 0)
    inp.u.ki.dwExtraInfo = EXTRA_INFO_TAG
    return inp


def _unicode_input(ch, up):
    """VK 없이 유니코드 문자 하나를 보낸다 (゛ 송신용)."""
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki.wVk = 0
    inp.u.ki.wScan = ord(ch)
    inp.u.ki.dwFlags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    inp.u.ki.dwExtraInfo = EXTRA_INFO_TAG
    return inp


def build_inputs(actions, shift_held, original):
    """엔진 액션을 SendInput 이벤트 목록으로 만든다.

    shift_held: 물리적으로 눌린 Shift VK 목록. 로마자가 대문자로 들어가지 않도록 앞에서 떼고
    뒤에서 다시 누른다 (MS IME에서 실측 확인). 원래 키 재송신은 Shift 복구 뒤에 둔다.
    original: (vk, scanCode, extended)
    """
    body = []
    resend = False
    for act in actions:
        if isinstance(act, (Romaji, Preview)):
            for ch in act[0]:
                vk = ord(ch.upper())
                body += [_key_input(vk, False), _key_input(vk, True)]
        elif isinstance(act, Backspace):
            for _ in range(act.count):
                body += [_key_input(VK_BACK, False), _key_input(VK_BACK, True)]
        elif isinstance(act, Text):
            for ch in act.text:
                body += [_unicode_input(ch, False), _unicode_input(ch, True)]
        elif isinstance(act, ResendKey):
            resend = True
    events = []
    if body:
        events += [_key_input(vk, True) for vk in shift_held]
        events += body
        events += [_key_input(vk, False) for vk in shift_held]
    if resend:
        vk, scan, extended = original
        events.append(_key_input(vk, False, scan, extended))
    return events


class KeyboardHook(threading.Thread):
    """전용 스레드에 WH_KEYBOARD_LL·WH_MOUSE_LL·포그라운드 WinEvent를 설치하고 메시지 루프를 돈다."""

    HEALTH_INTERVAL_MS = 1000
    REINSTALL_BACKOFF_MS = (3000, 60000)  # 재설치해도 콜백이 안 오면 다음 재설치까지 기다리는 시간 (처음, 최대)

    def __init__(self, watcher, dry_run=True, on_emergency=None, preview=False, trace=False,
                 pause_hotkey=None, on_pause=None):
        super().__init__(name="HangulKana-Hook", daemon=True)
        self.watcher = watcher
        self.dry_run = dry_run
        self.trace = trace  # live에서도 송신 내용을 로그 큐에 넣는다 (--debug 전용, 입력 내용 포함)
        self.on_emergency = on_emergency
        self.pause_hotkey = pause_hotkey  # (수식키 frozenset, VK) 또는 None (config.parse_hotkey)
        self.on_pause = on_pause
        self.engine = Engine(preview=preview)
        self.display = ("", "")  # 띄움창이 읽어 가는 (확정, 치는 중) 한글. 튜플 대입만 하므로 락 불필요
        self.logq = queue.SimpleQueue()  # 콜백 → 로그 스레드
        self.installed = threading.Event()
        self.faulted = False
        self._tid = 0
        self._stop_requested = False
        self._kbd = None
        self._mouse = None
        self._winevent = None
        self._shift = set()  # 물리적으로 눌린 Shift (VK_LSHIFT/VK_RSHIFT)
        self._swallowed = set()  # 누름을 삼킨 키: 뗌도 삼킨다
        self._was_active = False
        self._last_fg = 0
        self._last_cb_tick = 0
        self._reinstalled_tick = 0  # 마지막 재설치 시각 (0 = 재설치한 적 없음)
        self._backoff_ms = 0
        # 콜백 객체는 GC로 해제되지 않도록 속성으로 붙잡아 둔다
        self._kbd_proc = HOOKPROC(self._keyboard_cb)
        self._mouse_proc = HOOKPROC(self._mouse_cb)
        self._winevent_proc = WINEVENTPROC(self._winevent_cb)

    # ------------------------------------------------------------ 수명 주기

    def stop(self):
        self._stop_requested = True
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)

    def run(self):
        self._tid = kernel32.GetCurrentThreadId()
        timer = 0
        try:
            if self._stop_requested:
                return
            self._install()
            self._winevent = user32.SetWinEventHook(
                EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None, self._winevent_proc,
                0, 0, WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS,
            )
            timer = user32.SetTimer(None, 0, self.HEALTH_INTERVAL_MS, None)
            self.installed.set()
            msg = wintypes.MSG()
            while not self._stop_requested:
                r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r == 0 or r == -1:
                    break
                if msg.message == WM_TIMER:
                    self._health_check()
        except Exception:
            log.exception("훅 스레드 오류")
        finally:
            if timer:
                user32.KillTimer(None, timer)
            if self._winevent:
                user32.UnhookWinEvent(self._winevent)
                self._winevent = None
            self._uninstall()
            self.installed.set()  # 설치 실패 시 기다리는 쪽을 깨운다
            log.info("훅 해제 완료")

    def _install(self):
        hmod = kernel32.GetModuleHandleW(None)
        self._kbd = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kbd_proc, hmod, 0)
        if not self._kbd:
            raise OSError(f"키보드 훅 설치 실패: {ctypes.get_last_error()}")
        self._mouse = user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_proc, hmod, 0)
        if not self._mouse:
            log.error("마우스 훅 설치 실패: %s (클릭 무효화 없이 계속)", ctypes.get_last_error())
        self._last_cb_tick = kernel32.GetTickCount()

    def _uninstall(self):
        if self._kbd:
            user32.UnhookWindowsHookEx(self._kbd)
            self._kbd = None
        if self._mouse:
            user32.UnhookWindowsHookEx(self._mouse)
            self._mouse = None

    def _health_check(self):
        """시스템이 시간 초과로 훅을 조용히 제거했는지 확인하고, 그렇다면 재설치한다.

        게임(안티치트 등)이 입력을 먼저 가로채면 재설치해도 콜백이 오지 않는다. 그때 계속 재설치하면
        백신이 키로거로 오인할 수 있으므로(실제로 격리된 적 있음) 재설치 간격을 두 배씩 늘린다.
        콜백이 한 번이라도 오면 처음으로 돌아간다. 콜백과 이 검사는 같은 스레드라 경합이 없다."""
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not user32.GetLastInputInfo(ctypes.byref(lii)):
            return
        lag = (lii.dwTime - self._last_cb_tick) & 0xFFFFFFFF
        if not 2000 < lag < 0x80000000:
            return
        first, cap = self.REINSTALL_BACKOFF_MS
        if self._reinstalled_tick and self._last_cb_tick == self._reinstalled_tick:
            # 지난 재설치 뒤로 콜백이 한 번도 없었다
            since = (kernel32.GetTickCount() - self._reinstalled_tick) & 0xFFFFFFFF
            if since < self._backoff_ms:
                return
            self._backoff_ms = min(self._backoff_ms * 2, cap)
        else:
            self._backoff_ms = first
        log.error("입력은 있었으나 훅 콜백이 %dms 동안 호출되지 않음 → 훅 재설치 (계속 안 되면 %d초 뒤 다시)",
                  lag, self._backoff_ms // 1000)
        self._uninstall()
        self._install()
        self._reinstalled_tick = self._last_cb_tick
        self.engine.reset()
        self.display = ("", "")

    # ------------------------------------------------------------ 콜백

    def _keyboard_cb(self, ncode, wparam, lparam):
        try:
            if ncode == HC_ACTION:
                kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if self._on_key(kb):
                    return 1
        except BaseException as e:  # noqa: BLE001 - 콜백 밖으로 예외를 내보내지 않는다
            self.faulted = True
            self.logq.put(("error", f"키보드 콜백 예외로 변환기를 끕니다: {e!r}"))
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    def _mods(self):
        mods = set()
        if _down(VK_CONTROL):
            mods.add("ctrl")
        if _down(VK_MENU):
            mods.add("alt")
        if self._shift:
            mods.add("shift")
        if _down(VK_LWIN) or _down(VK_RWIN):
            mods.add("win")
        return frozenset(mods)

    def _on_key(self, kb) -> bool:
        """True = 원래 키를 삼킨다."""
        self._last_cb_tick = kernel32.GetTickCount()
        vk = kb.vkCode
        up = bool(kb.flags & LLKHF_UP)

        # 1. 비상 정지: 다른 모든 처리보다 먼저
        if not up and vk == VK_Q and _down(VK_CONTROL) and _down(VK_MENU) and _down(VK_SHIFT):
            self.faulted = True
            self.logq.put(("info", "비상 정지 키 입력"))
            self.stop()
            if self.on_emergency:
                self.on_emergency()
            return False

        # 2. 주입 이벤트는 무조건 통과 (자기 입력 재처리 방지)
        if kb.flags & LLKHF_INJECTED:
            if vk in layout.IME_TOGGLE_KEYS:
                self.watcher.request_refresh()
            return False

        if vk in (VK_LSHIFT, VK_RSHIFT):
            (self._shift.discard if up else self._shift.add)(vk)
            return False
        if vk in layout.IME_TOGGLE_KEYS:
            self.watcher.request_refresh()

        if up:
            if vk in self._swallowed:
                self._swallowed.discard(vk)
                return True
            return False

        # 일시 정지 단축키: 활성 여부와 관계없이 동작. 처리는 플래그 전환뿐이다
        if self.pause_hotkey is not None and vk == self.pause_hotkey[1] and self._mods() == self.pause_hotkey[0]:
            self.engine.reset()
            self.display = ("", "")
            if self.on_pause:
                self.on_pause()
            if self.dry_run:
                return False
            self._swallowed.add(vk)
            return True

        if self.faulted:
            return False

        # 3. 활성 판정: 캐시된 상태 + 포그라운드가 조회 시점과 같은지
        state = self.watcher.current
        fg = user32.GetForegroundWindow() or 0
        if fg != state.foreground:
            self.watcher.request_refresh()
        if not (state.active and fg == state.foreground):
            if self.trace:
                self.logq.put(("live", (vk, "비활성", None)))
            self._was_active = False
            return False
        if not self._was_active or fg != self._last_fg:
            self.engine.reset()
            self._was_active = True
            self._last_fg = fg

        # 4. 변환
        modifier = _down(VK_CONTROL) or _down(VK_MENU) or _down(VK_LWIN) or _down(VK_RWIN)
        event = layout.classify(vk, bool(self._shift), modifier)
        result = self.engine.handle(event)
        self.display = self.engine.display_parts()

        if self.dry_run:
            if result.swallow:
                self.logq.put(("dry", (vk, event.kind, result.actions)))
            return False
        if self.trace:
            self.logq.put(("live", (vk, event.kind, result.actions if result.swallow else None)))
        if not result.swallow:
            return False
        events = build_inputs(result.actions, sorted(self._shift), (vk, kb.scanCode, bool(kb.flags & LLKHF_EXTENDED)))
        if events:
            arr = (INPUT * len(events))(*events)
            sent = user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
            if sent != len(events):
                # 송신이 막혔다 (예: UIPI). 키를 삼키면 입력이 사라지므로 통과시키고 추적을 무효화
                self.engine.reset()
                self.engine.tracking_valid = False
                self.logq.put(("error", f"SendInput 실패 {sent}/{len(events)}"))
                return False
        if not any(isinstance(a, ResendKey) for a in result.actions):
            self._swallowed.add(vk)
        return True

    def _mouse_cb(self, ncode, wparam, lparam):
        try:
            if ncode == HC_ACTION:
                self._last_cb_tick = kernel32.GetTickCount()
                if wparam in (WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN, WM_XBUTTONDOWN):
                    self.engine.mouse_click()
                    self.display = self.engine.display_parts()
        except BaseException as e:  # noqa: BLE001
            self.logq.put(("error", f"마우스 콜백 예외: {e!r}"))
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    def _winevent_cb(self, hook, event, hwnd, id_object, id_child, thread, time_ms):
        try:
            self.watcher.request_refresh()
            self.engine.focus_changed()
            self.display = self.engine.display_parts()
        except BaseException as e:  # noqa: BLE001
            self.logq.put(("error", f"WinEvent 콜백 예외: {e!r}"))
