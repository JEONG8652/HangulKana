"""VK 코드 + Shift 상태 → 두벌식 자모, 그리고 엔진 이벤트 분류. OS 비의존.

VK 값은 Windows 가상 키 코드 숫자일 뿐이며 Windows API를 import하지 않는다.
"""

from typing import NamedTuple, Optional


class Jamo(NamedTuple):
    kind: str  # "consonant" | "vowel"
    char: str = ""
    dakuten: bool = False  # Shift로 친 자모: 그 음절에 탁점


class Event(NamedTuple):
    kind: str  # jamo | char | term | bs | nav | combo | ignore
    jamo: Optional[Jamo] = None
    sub: str = ""  # term: space/enter/fkey, nav: arrow/esc/other, char: 가나 추정값


_CONSONANTS = {
    "Q": "ㅂ", "W": "ㅈ", "E": "ㄷ", "R": "ㄱ", "T": "ㅅ",
    "A": "ㅁ", "S": "ㄴ", "D": "ㅇ", "F": "ㄹ", "G": "ㅎ",
    "Z": "ㅋ", "X": "ㅌ", "C": "ㅊ", "V": "ㅍ",
}
_VOWELS = {
    "Y": "ㅛ", "U": "ㅕ", "I": "ㅑ", "O": "ㅐ", "P": "ㅔ", "H": "ㅗ",
    "J": "ㅓ", "K": "ㅏ", "L": "ㅣ", "B": "ㅠ", "N": "ㅜ", "M": "ㅡ",
}

VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_OEM_MINUS = 0xBD
_ARROWS = {0x25, 0x26, 0x27, 0x28}
_FKEYS_TERM = set(range(0x75, 0x7A))  # F6–F10

# 상태를 바꾸지 않고 통과시키는 키: Shift·Ctrl·Alt·Win, 잠금 키, IME 관련 키
_IGNORE = {
    0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C,
    0x14, 0x90, 0x91,
    0x15, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x1C, 0x1D, 0x1E, 0x1F,
    0xE5, 0xF0, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6,
}
# IME 상태를 바꿀 수 있는 키 → 즉시 재조회
IME_TOGGLE_KEYS = {0x14, 0x15, 0x19, 0x1C, 0x1D, 0xF0, 0xF1, 0xF2, 0xF3, 0xF4}

# 조합에 1글자로 들어가는 숫자·기호 키
_CHAR_KEYS = set(range(0x30, 0x3A)) | set(range(0xBA, 0xC1)) | set(range(0xDB, 0xE0)) | {0xE2}


def to_jamo(vk: int, shift: bool) -> Optional[Jamo]:
    """Shift는 쌍자음·ㅒ가 아니라 탁점 표시로 해석한다 (Shift+ㅂ = ㅂ + 탁점).

    예외: Shift+ㅔ는 두벌식 그대로 ㅖ (예 = いぇ).
    """
    if not 0x41 <= vk <= 0x5A:
        return None
    key = chr(vk)
    if key in _CONSONANTS:
        return Jamo("consonant", _CONSONANTS[key], shift)
    if shift and key == "P":
        return Jamo("vowel", "ㅖ")
    return Jamo("vowel", _VOWELS[key], shift)


def _char_guess(vk: int, shift: bool) -> str:
    if vk == VK_OEM_MINUS and not shift:
        return "ー"
    if 0x30 <= vk <= 0x39 and not shift:
        return chr(ord("０") + vk - 0x30)
    return "?"


def classify(vk: int, shift: bool, modifier: bool) -> Event:
    """키 누름 하나를 엔진 이벤트로 분류한다. modifier = Ctrl·Alt·Win 중 하나가 눌림."""
    if vk in _IGNORE:
        return Event("ignore")
    if modifier:
        return Event("combo")
    jamo = to_jamo(vk, shift)
    if jamo is not None:
        return Event("jamo", jamo)
    if vk in _CHAR_KEYS:
        return Event("char", sub=_char_guess(vk, shift))
    if vk == VK_SPACE:
        return Event("term", sub="space")
    if vk == VK_RETURN:
        return Event("term", sub="enter")
    if vk in _FKEYS_TERM:
        return Event("term", sub="fkey")
    if vk == VK_BACK:
        return Event("bs")
    if vk in _ARROWS:
        return Event("nav", sub="arrow")
    if vk == VK_ESCAPE:
        return Event("nav", sub="esc")
    return Event("nav", sub="other")
