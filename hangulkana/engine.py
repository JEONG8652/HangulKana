"""자모 입력 → 송신 액션, 조합 추적. OS 비의존.

엔진은 키 누름 이벤트(layout.Event)를 받아 Result를 돌려준다.
- swallow=False: 원래 키를 그대로 통과시킨다. actions는 항상 비어 있다.
- swallow=True: 원래 키를 삼키고 actions를 순서대로 보낸다. ResendKey가 있으면 그 위치에서
  원래 키를 다시 보낸다 (앞선 송신과 순서를 보장하기 위해).

불변 조건 (모든 코드와 테스트가 지킨다)
1. MS IME에 미완성 로마자를 남기지 않는다. 보내는 로마자는 항상 완결된 가나 단위(ga, kyo, ltu, nn 등)라서,
   숫자·기호 키를 그대로 통과시켜도 조합 문자열 1글자에 1:1로 대응한다.
2. 엔진이 보내는 백스페이스 수는 엔진이 추적 중이며 아직 남아 있는 조합 글자 수를 넘지 않는다.
   어기면 이미 확정된 문서 내용이 지워진다.
3. 변환기가 보낸 입력(주입 이벤트)은 훅에서 다시 처리하지 않는다 (hook.py).
"""

from dataclasses import dataclass, field
from typing import List, NamedTuple, Optional, Union

from . import romaji_table as table
from .layout import Event, Jamo


class Romaji(NamedTuple):
    text: str


class Backspace(NamedTuple):
    count: int


class ResendKey(NamedTuple):
    pass


class Text(NamedTuple):
    """로마자로 입력할 수 없는 글자(゛)를 유니코드 문자로 직접 보낸다."""

    text: str


Action = Union[Romaji, Backspace, ResendKey, Text]


@dataclass
class Result:
    swallow: bool
    actions: List[Action] = field(default_factory=list)


PASS = Result(False)


class Char(NamedTuple):
    """조합 문자열의 글자 하나."""

    kana: str
    source: str  # "syllable" | "batchim" | "pass"


class Syllable(NamedTuple):
    onset: str
    vowel: str
    kana: str
    dakuten: bool


# ---------------------------------------------------------------- 한글 띄움창 표시

_CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
_JUNG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
_JONG = ["", "ㄱ", "ㄲ", "ㄳ", "ㄴ", "ㄵ", "ㄶ", "ㄷ", "ㄹ", "ㄺ", "ㄻ", "ㄼ", "ㄽ", "ㄾ", "ㄿ", "ㅀ",
         "ㅁ", "ㅂ", "ㅄ", "ㅅ", "ㅆ", "ㅇ", "ㅈ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ"]
# 표시용 겹모음: 가나 쪽은 작은 가나로 이어 붙지만(우+ㅣ → うぃ), 화면에는 한 음절(위)로 보여 준다
_DISPLAY_COMPOUND = {
    ("ㅗ", "ㅏ"): "ㅘ", ("ㅗ", "ㅐ"): "ㅙ", ("ㅗ", "ㅣ"): "ㅚ", ("ㅜ", "ㅓ"): "ㅝ",
    ("ㅜ", "ㅔ"): "ㅞ", ("ㅜ", "ㅣ"): "ㅟ", ("ㅡ", "ㅣ"): "ㅢ",
}


def compose_hangul(cho: str, jung: str, jong: str = "") -> str:
    return chr(0xAC00 + (_CHO.index(cho) * 21 + _JUNG.index(jung)) * 28 + _JONG.index(jong))


class HangulUnit:
    """띄움창에 보여 줄 한글 한 단위와, 그 단위가 조합에서 차지하는 가나 글자 수."""

    __slots__ = ("cho", "jung", "jong", "kana", "jong_kana", "dakuten", "raw")

    def __init__(self, cho="", jung="", kana=0, dakuten=False, raw=""):
        self.cho, self.jung, self.jong = cho, jung, ""
        self.kana, self.jong_kana = kana, 0
        self.dakuten = dakuten
        self.raw = raw  # 숫자·ー 등 그대로 보여 줄 글자

    def syllable_ok(self) -> bool:
        return not self.raw and bool(self.cho)

    def render(self, jong: Optional[str] = None) -> str:
        if self.raw:
            return self.raw
        if not self.cho:
            return self.jung  # ㅇ 없이 친 모음: 자모 그대로
        text = compose_hangul(self.cho, self.jung, self.jong if jong is None else jong)
        return text + (table.DAKUTEN_MARK if self.dakuten else "")


class Engine:
    def __init__(self):
        self.reset()

    # ------------------------------------------------------------ 상태

    def reset(self):
        """조합을 비우고 추적을 새로 시작한다 (활성화·포커스 변경 직후)."""
        self.pending: Optional[Jamo] = None  # 대기 중인 초성 (미송신). Shift 여부 포함
        # 보류 음절 (미송신): 조합이 빈 상태에서 친 오·호·우. 다음 키가 ㅏ·ㅓ면 바로 와·화·워를 보낸다.
        # 오를 먼저 보내고 백스페이스로 지우면 조합이 비는 순간 트위터(X) 입력창이 조합을 초기화해
        # 뒤따르는 송신이 망가진다 (2026-11-01). (onset, vowel, dakuten, kana)
        self.held: Optional[tuple] = None
        self.last: Optional[Syllable] = None  # 복모음 치환용 직전 음절. 조합 끝에 그대로 있을 때만 유지
        self.open_syllable = False  # 직전 입력이 받침을 받을 수 있는 음절인가
        self.composition: List[Char] = []
        self.hangul: List[HangulUnit] = []  # 띄움창 표시
        self.converting = False
        self.tracking_valid = True
        self._reset_on_next = False

    def _clear(self):
        self.held = None
        self.composition = []
        self.hangul = []
        self.last = None
        self.open_syllable = False
        self.converting = False
        self.tracking_valid = True

    def _invalidate(self):
        self.tracking_valid = False
        self.hangul = []
        self.last = None
        self.open_syllable = False

    def display_parts(self):
        """띄움창에 보여 줄 한글 (확정된 부분, 아직 치는 중인 부분).

        대기 초성은 받침이 될 수 있으면 앞 음절의 받침으로 보여 준다 (한국어 IME처럼 가 + ㄴ → 간).
        그 음절(또는 대기 초성 자모)이 "치는 중" 부분이 된다."""
        parts = [u.render() for u in self.hangul]
        if self.held is not None:
            onset, vowel, dakuten, _ = self.held
            return "".join(parts), HangulUnit(onset, vowel, dakuten=dakuten and vowel != "ㅖ").render()
        p = self.pending
        if p is None:
            return "".join(parts), ""
        last = self.hangul[-1] if self.hangul else None
        if self.open_syllable and last is not None and last.syllable_ok() and not last.jong:
            return "".join(parts[:-1]), last.render(jong=p.char)
        return "".join(parts), p.char

    def display_text(self) -> str:
        return "".join(self.display_parts())

    # ------------------------------------------------------------ 외부 이벤트

    def mouse_click(self):
        self._abandon_pending()
        self._invalidate()

    def focus_changed(self):
        self._abandon_pending()
        self._invalidate()
        self._reset_on_next = True  # 다음 입력에서 조합을 새로 시작

    def handle(self, ev: Event) -> Result:
        if ev.kind == "ignore":
            return PASS
        if self._reset_on_next:
            self._reset_on_next = False
            self._clear()
        flushed: List[Action] = []
        if self.held is not None and self._held_compound(ev) is None:
            if ev.kind == "bs":
                self.held = None
                return Result(True)  # 화면에 아무것도 없으므로 키도 삼킨다
            if ev.kind == "combo":
                self.held = None  # 수식키가 눌려 있어 로마자를 보낼 수 없다
            else:
                flushed = self._flush_held()
        result = getattr(self, "_on_" + ev.kind)(ev)
        if not flushed:
            return result
        if not result.swallow:
            return Result(True, flushed + [ResendKey()])
        return Result(True, flushed + result.actions)

    # ------------------------------------------------------------ 키 종류별

    def _on_jamo(self, ev: Event) -> Result:
        j: Jamo = ev.jamo
        if self.converting:
            # 한글 입력 → MS IME가 변환을 확정하고 새 조합 시작
            self._clear()
        if j.kind == "consonant":
            actions = self._commit_batchim()
            self.pending = j  # 모음이 올 때까지 아무것도 보내지 않는다 (띄움창에만 보인다)
            return Result(True, actions)
        return Result(True, self._vowel(j))

    # ------------------------------------------------------------ 대기 초성

    def _abandon_pending(self):
        """클릭·포커스 변경·단축키: 대기 초성과 보류 음절을 버린다 (아직 아무것도 보내지 않았다)."""
        self.pending = None
        self.held = None

    # ------------------------------------------------------------ 보류 음절 (첫 글자 오·호·우)

    @staticmethod
    def _compounds_from(onset, vowel):
        return [cv for (v1, _), cv in table.COMPOUND_VOWELS.items()
                if v1 == vowel and table.syllable(onset, cv) is not None]

    def _should_hold(self, onset, vowel, dakuten, kana) -> bool:
        """조합이 비어 있고, 이 음절이 다음 모음과 복모음이 될 수 있으면 보류한다."""
        if self.composition or not self._compounds_from(onset, vowel):
            return False
        voiced = dakuten and vowel != "ㅖ"
        # ゛가 붙는 음절은 복모음 치환 대상이 아니므로(_emit) 보류할 이유가 없다
        return not (voiced and table.DAKUTEN_MARK in table.voiced(kana))

    def _held_compound(self, ev: Event) -> Optional[str]:
        """보류 음절과 이 키가 복모음이 되면 그 복모음."""
        if self.held is None or ev.kind != "jamo" or ev.jamo.kind != "vowel":
            return None
        onset, vowel, _, _ = self.held
        compound = table.COMPOUND_VOWELS.get((vowel, ev.jamo.char))
        if compound is None or table.syllable(onset, compound) is None:
            return None
        return compound

    def _send_syllable(self, onset, vowel, dakuten, kana) -> List[Action]:
        actions = self._emit(onset, vowel, dakuten, kana)
        self.hangul.append(HangulUnit(onset, vowel, self._emitted, self._voiced))
        return actions

    def _flush_held(self) -> List[Action]:
        onset, vowel, dakuten, kana = self.held
        self.held = None
        return self._send_syllable(onset, vowel, dakuten, kana)

    # ------------------------------------------------------------ 음절

    def _vowel(self, j: Jamo) -> List[Action]:
        compound = self._held_compound(Event("jamo", j))
        if compound is not None:
            # 보류해 둔 오·호·우 + ㅏ·ㅓ: 백스페이스 없이 바로 와·화·워
            onset, _, dakuten, _ = self.held
            self.held = None
            return self._send_syllable(onset, compound, dakuten or j.dakuten, table.syllable(onset, compound).kana)
        if self.pending is not None:
            unit = table.syllable(self.pending.char, j.char)
            if unit is None:
                return []  # 무시: 대기 자음 유지
            onset, self.pending = self.pending, None
            dakuten = onset.dakuten or j.dakuten
            if self._should_hold(onset.char, j.char, dakuten, unit.kana):
                self.held = (onset.char, j.char, dakuten, unit.kana)
                return []
            return self._send_syllable(onset.char, j.char, dakuten, unit.kana)

        # last는 직전 키로 송신한 음절일 때만 남아 있으므로, 추적이 무효여도 지우는 것은
        # 방금 보낸 글자뿐이다 (불변 조건 2)
        last = self.last
        if last is not None:
            compound = table.COMPOUND_VOWELS.get((last.vowel, j.char))
            unit = table.syllable(last.onset, compound) if compound else None
            if unit is not None:
                n = len(last.kana)
                assert n <= len(self.composition)  # 불변 조건 2
                del self.composition[-n:]
                actions = [Backspace(n)] + self._emit(last.onset, compound, last.dakuten or j.dakuten, unit.kana)
                if self.hangul:
                    u = self.hangul[-1]
                    u.jung, u.kana, u.dakuten = compound, self._emitted, self._voiced
                return actions

        # ㅇ 없이 친 모음 → 작은 가나
        small = table.SMALL.get(j.char)
        if small is not None:
            actions = self._emit("ㅇ", j.char, j.dakuten, small.kana)
            self.last = None  # 작은 가나는 복모음 치환 대상이 아니다
            self._show_bare_vowel(j.char)
            return actions
        unit = table.syllable("ㅇ", j.char)  # ㅖ: 작은 가나가 없어 예(いぇ)
        if unit is not None:
            actions = self._emit("ㅇ", j.char, j.dakuten, unit.kana)
            self._show_bare_vowel(j.char)
            return actions
        return []  # ㅓ·ㅕ 등: 무시

    def _show_bare_vowel(self, vowel):
        """띄움창: 우 + ㅣ처럼 겹모음이 되면 앞 음절에 합치고(위), 아니면 모음 자모로 보여 준다."""
        last = self.hangul[-1] if self.hangul else None
        compound = _DISPLAY_COMPOUND.get((last.jung, vowel)) if last is not None and last.syllable_ok() else None
        if compound and not last.jong:
            last.jung = compound
            last.kana += self._emitted
            last.dakuten = last.dakuten or self._voiced
        else:
            self.hangul.append(HangulUnit("", vowel, self._emitted, self._voiced))

    def _emit(self, onset, vowel, dakuten, kana) -> List[Action]:
        # ㅖ는 Shift+ㅔ라서, Shift를 누른 채 예를 치면 ㅇ에도 Shift가 걸린다. 예에는 탁점을 붙이지 않는다
        self._voiced = dakuten and vowel != "ㅖ"
        if self._voiced:
            kana = table.voiced(kana)
        self._emitted = len(kana)  # 띄움창 단위가 차지하는 가나 글자 수
        self.composition.extend(Char(k, "syllable") for k in kana)
        # ゛를 따로 붙인 음절은 복모음 치환 대상에서 뺀다 (Shift+오 + ㅏ → お゛ぁ)
        self.last = None if table.DAKUTEN_MARK in kana else Syllable(onset, vowel, kana, dakuten)
        self.open_syllable = True
        return [Text(r) if r == table.DAKUTEN_MARK else Romaji(r) for r in table.to_romaji(kana)]

    def _commit_batchim(self) -> List[Action]:
        """대기 자음을 직전 음절의 받침으로 확정한다. 받침이 될 수 없으면 버린다."""
        if self.pending is None:
            return []
        onset, self.pending = self.pending.char, None
        can_attach = self.open_syllable
        self.last = None
        self.open_syllable = False
        last = self.hangul[-1] if self.hangul else None
        if can_attach and last is not None and last.syllable_ok() and not last.jong:
            last.jong = onset  # 띄움창에는 버리는 받침도 친 그대로 보여 준다 (각)
            last.jong_kana = 1 if onset in table.BATCHIM else 0
        if not can_attach or onset not in table.BATCHIM:
            return []
        unit = table.BATCHIM[onset]
        self.composition.append(Char(unit.kana, "batchim"))
        return [Romaji(unit.romaji)]

    def _with_key(self, actions: List[Action]) -> Result:
        """actions 뒤에 원래 키를 보낸다. 보낼 것이 없으면 그냥 통과."""
        if actions:
            return Result(True, actions + [ResendKey()])
        return PASS

    def _on_term(self, ev: Event) -> Result:
        if self.converting:
            if ev.sub == "enter":
                self._clear()
            return PASS  # Space·F6–F10: 후보 선택, 교정하지 않음
        actions = self._commit_batchim()
        self.last = None
        self.open_syllable = False
        self.hangul = []  # 띄움창: 변환·확정하면 비운다
        if ev.sub == "enter":
            self._clear()
        elif self.composition or not self.tracking_valid:
            self.converting = True
        return self._with_key(actions)

    def _on_char(self, ev: Event) -> Result:
        if self.converting:
            # 후보 선택(숫자) 또는 확정 후 새 조합. 어느 쪽인지 모르므로 추적만 무효화
            self.tracking_valid = False
            return PASS
        actions = self._commit_batchim()
        self.last = None
        self.open_syllable = False
        self.composition.append(Char(ev.sub, "pass"))
        self.hangul.append(HangulUnit(kana=1, raw=ev.sub if ev.sub != "?" else ""))
        return self._with_key(actions)

    def _on_bs(self, ev: Event) -> Result:
        if self.pending is not None:
            self.pending = None
            return Result(True)  # 화면에 아무것도 없으므로 키도 삼킨다
        if self.converting:
            self.converting = False
            self._invalidate()
            return PASS
        if self.composition:
            self.composition.pop()
        self.last = None
        self.open_syllable = bool(self.composition) and self.composition[-1].source == "syllable"
        self._display_backspace()
        return PASS

    def _display_backspace(self):
        """띄움창: 조합에서 가나 한 글자가 지워졌다. 받침 → 음절 순으로 줄인다."""
        if not self.hangul:
            return
        u = self.hangul[-1]
        if u.jong:
            had_kana = u.jong_kana
            u.jong, u.jong_kana = "", 0
            if had_kana:
                return
        u.kana -= 1
        if u.kana <= 0:
            self.hangul.pop()

    def _on_nav(self, ev: Event) -> Result:
        if self.converting:
            if ev.sub == "arrow":
                return PASS  # 문절·후보 이동
            self.converting = False
            self._invalidate()
            return PASS
        actions = self._commit_batchim()
        self._invalidate()
        return self._with_key(actions)

    def _on_combo(self, ev: Event) -> Result:
        # Ctrl·Alt·Win 조합 중에는 로마자를 보내면 단축키가 되므로 대기 자음을 버린다.
        self._abandon_pending()
        self._invalidate()
        return PASS
