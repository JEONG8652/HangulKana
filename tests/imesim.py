"""MS IME 로마자 시뮬레이터와 테스트 입력 도우미.

시뮬레이터는 엔진이 보낸 완결 로마자를 가나로 바꾸고, 백스페이스와 통과 키를 반영한다.
engine.py의 불변 조건 1(완결 로마자만 송신)과 2(백스페이스 수 ≤ 남은 조합 글자 수)를 매 송신마다 단언한다.
"""

from hangulkana import romaji_table
from hangulkana.engine import Backspace, Engine, Preview, ResendKey, Romaji, Text
from hangulkana.layout import classify

PREVIEW_CHARS = set("abcdefghijklmnopqrstuvwxyz")  # 조합에 남은 미완성 로마자

# 두벌식 자모 → (VK, Shift)
_JAMO_KEYS = {
    "ㅂ": "q", "ㅈ": "w", "ㄷ": "e", "ㄱ": "r", "ㅅ": "t", "ㅛ": "y", "ㅕ": "u", "ㅑ": "i",
    "ㅐ": "o", "ㅔ": "p", "ㅁ": "a", "ㄴ": "s", "ㅇ": "d", "ㄹ": "f", "ㅎ": "g", "ㅗ": "h",
    "ㅓ": "j", "ㅏ": "k", "ㅣ": "l", "ㅋ": "z", "ㅌ": "x", "ㅊ": "c", "ㅍ": "v", "ㅠ": "b",
    "ㅜ": "n", "ㅡ": "m",
    "ㅃ": "Q", "ㅉ": "W", "ㄸ": "E", "ㄲ": "R", "ㅆ": "T", "ㅒ": "O", "ㅖ": "P",
}
_COMPOUND = {"ㅘ": "ㅗㅏ", "ㅙ": "ㅗㅐ", "ㅚ": "ㅗㅣ", "ㅝ": "ㅜㅓ", "ㅞ": "ㅜㅔ", "ㅟ": "ㅜㅣ", "ㅢ": "ㅡㅣ"}
_L = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
_V = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
_T = ["", "ㄱ", "ㄲ", "ㄱㅅ", "ㄴ", "ㄴㅈ", "ㄴㅎ", "ㄷ", "ㄹ", "ㄹㄱ", "ㄹㅁ", "ㄹㅂ", "ㄹㅅ", "ㄹㅌ",
      "ㄹㅍ", "ㄹㅎ", "ㅁ", "ㅂ", "ㅂㅅ", "ㅅ", "ㅆ", "ㅇ", "ㅈ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ"]

SPECIAL = {
    "<BS>": 0x08, "<ENTER>": 0x0D, "<SPACE>": 0x20, "<LEFT>": 0x25, "<ESC>": 0x1B, "<F7>": 0x76,
}


def _jamo_seq(text):
    for ch in text:
        code = ord(ch) - 0xAC00
        if 0 <= code < 11172:
            yield _L[code // 588]
            yield from _COMPOUND.get(_V[code // 28 % 21], _V[code // 28 % 21])
            yield from _T[code % 28]
        else:
            yield ch


def keys(text):
    """'가나', '카레-', '<BS>', '^츠'(Shift 탁점) 등을 [(vk, shift), ...] 로 바꾼다.

    '^'는 바로 다음 모음 키를, '!'는 바로 다음 자음 키를 Shift로 친다.
    """
    out = []
    i = 0
    shift_vowel = shift_consonant = False
    while i < len(text):
        if text[i] == "<":
            j = text.index(">", i)
            out.append((SPECIAL[text[i:j + 1]], False))
            i = j + 1
            continue
        if text[i] in "^!":
            if text[i] == "^":
                shift_vowel = True
            else:
                shift_consonant = True
            i += 1
            continue
        for jamo in _jamo_seq(text[i]):
            if jamo == "-":
                out.append((0xBD, False))
            elif jamo.isdigit():
                out.append((ord(jamo), False))
            else:
                key = _JAMO_KEYS[jamo]
                shift = key.isupper()
                if shift_vowel and jamo in _V:
                    shift, shift_vowel = True, False
                elif shift_consonant and jamo in _L:
                    shift, shift_consonant = True, False
                out.append((ord(key.upper()), shift))
        i += 1
    return out


class ImeSim:
    """조합 문자열(composition)과 확정 텍스트(committed)를 흉내 낸다."""

    def __init__(self):
        self.composition = []
        self.committed = ""
        self.converting = False
        self.swallowed = 0

    def _commit(self):
        self.committed += "".join(self.composition)
        self.composition = []
        self.converting = False

    def _pass_key(self, vk):
        if self.converting:
            # 변환 중: Enter 확정, Esc 읽기 상태로 복귀, 그 밖(Space·화살표·숫자)은 후보 조작
            if vk == 0x0D:
                self._commit()
            elif vk == 0x1B:
                self.converting = False
            return
        if vk == 0x0D:
            self._commit()
        elif vk in (0x20, 0x76) and self.composition:
            self.converting = True
        elif vk == 0x1B:
            self.composition = []
        elif vk == 0x08:
            if self.composition:
                self.composition.pop()
        elif vk == 0xBD:
            self.composition.append("ー")
        elif 0x30 <= vk <= 0x39:
            self.composition.append(chr(ord("０") + vk - 0x30))
        # Space·화살표 등은 변환·이동이며 여기서는 조합 문자열을 바꾸지 않는다

    def apply(self, vk, result):
        if not result.swallow:
            assert not result.actions
            self._pass_key(vk)
            return
        self.swallowed += 1
        for act in result.actions:
            if isinstance(act, (Romaji, Text)):
                # 미리보기 로마자가 남은 채 다음 글자를 보내면 MS IME가 이어 붙여 다른 가나가 된다
                assert not (self.composition and self.composition[-1] in PREVIEW_CHARS), self.composition
            if isinstance(act, Preview):
                assert act.letter in romaji_table.PREVIEW_LETTER.values(), act.letter
                if self.converting:
                    self._commit()
                self.composition.append(act.letter)  # 화면에는 ｋ처럼 보인다
            elif isinstance(act, Romaji):
                # 불변 조건 1: 완결된 가나 단위만 보낸다
                assert act.text in romaji_table.ROMAJI_TO_KANA, act.text
                if self.converting:
                    self._commit()  # 한글 입력 → 변환 확정 후 새 조합
                self.composition.extend(romaji_table.ROMAJI_TO_KANA[act.text])
            elif isinstance(act, Backspace):
                # 불변 조건 2: 남아 있는 조합 글자 수를 넘지 않는다
                assert 0 < act.count <= len(self.composition), (act.count, self.composition)
                del self.composition[-act.count:]
            elif isinstance(act, Text):
                # 유니코드 송신: ゛만 조합에 들어간다 (한글 자모는 조합 밖으로 나감, MS IME 실측)
                assert act.text == romaji_table.DAKUTEN_MARK, act.text
                if self.converting:
                    self._commit()
                self.composition.extend(act.text)
            elif isinstance(act, ResendKey):
                self._pass_key(vk)
            else:
                raise AssertionError(act)

    @property
    def text(self):
        return self.committed + "".join(self.composition)


def run(text, engine=None, sim=None):
    engine = engine or Engine()
    sim = sim or ImeSim()
    for vk, shift in keys(text):
        result = engine.handle(classify(vk, shift, False))
        sim.apply(vk, result)
        # 엔진이 추적하는 조합은 실제 조합의 끝부분과 같아야 한다 (추적 유효 시)
        if engine.tracking_valid and not engine.converting and not sim.converting:
            tracked = "".join(c.kana for c in engine.composition)
            assert "".join(sim.composition) == tracked, (tracked, sim.composition)
    return engine, sim
