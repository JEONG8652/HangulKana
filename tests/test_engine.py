"""엔진 테스트: 기본 예시, 복모음·받침·탁점·백스페이스·변환 중·추적·대기 초성·띄움창 표시."""

import pytest

from hangulkana.engine import Backspace, Engine, ResendKey, Result, Romaji, Text
from hangulkana.layout import Event, classify
from imesim import ImeSim, keys, run


def converted(text):
    """입력 뒤 Enter로 확정한 결과."""
    _, sim = run(text + "<ENTER>")
    return sim.committed


# ---------------------------------------------------------------- 기본 예시

@pytest.mark.parametrize("text, expected", [
    ("가나", "がな"),
    ("간아", "がんあ"),
    ("쿄우", "きょう"),
    ("세이자", "せいざ"),
    ("자츠단", "ざつだん"),
    ("보슈우", "ぼしゅう"),
    ("카레-", "かれー"),
    ("죠우노우치", "じょうのうち"),
    ("쟈가이모", "じゃがいも"),
    ("텐카잇핀", "てんかいっぴん"),
    ("주스", "じゅす"),
    ("추", "ちゅ"),
    ("티", "てぃ"),
    ("투", "とぅ"),
    ("디", "でぃ"),
    ("두", "どぅ"),
    ("와", "わ"),
    ("화", "ふぁ"),
    ("위", "うぃ"),
    ("웨", "うぇ"),
    ("휘", "ふぃ"),
    ("예", "いぇ"),  # Shift+ㅔ = ㅖ는 탁점 예외
    ("이ㅔ", "いぇ"),
    ("버", ""),
    ("각", "が"),
    ("^치", "ぢ"),  # Shift 탁점
    ("^츠", "づ"),
    ("워", "を"),
])
def test_basic_examples(text, expected):
    assert converted(text) == expected


def test_pending_onset_backspace_is_swallowed():
    # ㄱ + 백스페이스 → 아무것도 보내지 않고 원래 키도 삼킴
    engine = Engine()
    sim = ImeSim()
    for vk, shift in keys("ㄱ<BS>"):
        result = engine.handle(classify(vk, shift, False))
        assert result.swallow and not result.actions
        sim.apply(vk, result)
    assert sim.text == ""
    assert engine.pending is None


# ---------------------------------------------------------------- 복모음 치환

def test_compound_sends_backspace_then_new_syllable():
    engine = Engine()
    engine.handle(classify(ord("D"), False, False))  # ㅇ
    assert engine.handle(classify(ord("H"), False, False)).actions == [Romaji("o")]
    assert engine.handle(classify(ord("K"), False, False)).actions == [Backspace(1), Romaji("wa")]


@pytest.mark.parametrize("text, romaji", [
    ("위", ["u", "li"]),
    ("웨", ["u", "le"]),
    ("휘", ["hu", "li"]),
    ("훼", ["hu", "le"]),
])
def test_u_compounds_use_small_kana_without_backspace(text, romaji):
    engine = Engine()
    sent = []
    for vk, shift in keys(text):
        sent += engine.handle(classify(vk, shift, False)).actions
    assert sent == [Romaji(r) for r in romaji]


@pytest.mark.parametrize("text, expected", [
    ("누ㅣ", "ぬぃ"),  # (ㄴ, ㅟ)는 허용 안 됨 → 작은 가나
    ("구ㅏ", "ぐぁ"),  # 복모음을 이루지 않으면 작은 가나
    ("후ㅔ", "ふぇ"),  # 훼
    ("오ㅏ", "わ"),
    ("고ㅏ", "ごぁ"),
    # ㅇ 없이 친 모음 → 작은 가나
    ("에", "え"),
    ("ㅔ", "ぇ"),
    ("ㅐ", "ぇ"),
    ("ㅏㅣㅜㅡㅗ", "ぁぃぅぅぉ"),
    ("ㅑㅠㅛ", "ゃゅょ"),
    ("테ㅔ", "てぇ"),
    ("ㅏㄴ", "ぁん"),  # 작은 가나 뒤 받침
    ("ㅗㅏ", "ぉぁ"),  # 작은 가나는 복모음 치환 대상 아님
    ("ㅜㅣ", "ぅぃ"),
])
def test_vowel_without_onset(text, expected):
    assert converted(text) == expected


def test_no_compound_across_intervening_key():
    engine, sim = run("오<LEFT>")
    assert not engine.tracking_valid
    engine, sim = run("ㅏ", engine, sim)
    assert sim.text == "おぁ"


@pytest.mark.parametrize("invalidate", [
    lambda e: e.mouse_click(),
    lambda e: e.handle(classify(0x25, False, False)),  # 화살표
    lambda e: e.handle(classify(ord("C"), False, True)),  # Ctrl+C
    lambda e: e.handle(classify(0x1B, False, False)),  # Esc
], ids=["click", "arrow", "ctrl", "esc"])
@pytest.mark.parametrize("text, expected", [("와", "わ"), ("위", "うぃ"), ("화", "ふぁ")])
def test_compound_after_invalidation(invalidate, text, expected):
    # 실기 테스트: 클릭·화살표 등 뒤에 와·위가 おあ·うい로 들어가던 문제
    engine, _ = run("가")
    invalidate(engine)
    assert not engine.tracking_valid
    engine, sim = run(text, engine)
    assert sim.text == expected


@pytest.mark.parametrize("text, expected", [
    ("워", "を"),
    ("우ㅓ", "を"),
    ("구ㅓ", "ぐ"),  # (ㄱ, ㅝ)는 없음 → ㅓ 무시
    ("ㅓ", ""),
])
def test_wo(text, expected):
    assert converted(text) == expected


# ---------------------------------------------------------------- 받침

@pytest.mark.parametrize("text, expected", [
    ("간나", "がんな"),
    ("갓타", "がった"),
    ("간1", "がん１"),  # 숫자 키 앞에서 받침 확정
    ("ㄴ가", "が"),  # 직전 음절 없는 대기 자음은 버림
    ("각나", "がな"),  # 불허 받침 버림 후 다음 초성
    ("간ㄴ가", "がんが"),  # 받침 뒤 자음은 받침이 될 수 없음
    ("강나", "がんな"),  # ㅇ 받침 → ん
    ("강아", "がんあ"),  # ㅇ 받침 + ㅇ (ㄱㅏㅇㅇㅏ)
    ("겡키", "げんき"),
    ("강", "がん"),  # 끝의 ㅇ 받침
])
def test_batchim(text, expected):
    assert converted(text) == expected


def test_batchim_committed_before_space():
    engine, sim = run("간<SPACE>")
    assert sim.text == "がん"
    assert engine.converting


# ---------------------------------------------------------------- 불허 입력

@pytest.mark.parametrize("text", ["ㄲ", "가ㅓ", "가ㅕ", "ㄷㅑ", "ㅌㅛ", "ㅇㅓ", "ㅕ", "ㄱㅖ"])
def test_disallowed_input_is_swallowed(text):
    engine = Engine()
    result = None
    for vk, shift in keys(text):
        result = engine.handle(classify(vk, shift, False))
    # 마지막 키는 삼키고 아무것도 보내지 않는다
    assert result.swallow and not result.actions


def test_ignored_vowel_keeps_pending_onset():
    # 다 + ㅑ(무시) → 대기 ㄷ 유지 → ㅏ → だ
    assert converted("ㄷㅑㅏ") == "だ"


# ---------------------------------------------------------------- Shift 탁점

@pytest.mark.parametrize("text, expected", [
    # 탁점 글자가 있는 행
    ("^카^키^쿠!케^코", "がぎぐげご"),  # ㅔ의 Shift는 ㅖ이므로 케는 자음에서 Shift
    ("^캬^큐^쿄", "ぎゃぎゅぎょ"),
    ("^타^티^투!테^토^튜", "だでぃどぅでど" + "でゅ"),
    ("^파^피^푸!페^포", "ばびぶべぼ"),
    ("^하^히^후!헤^호", "ばびぶべぼ"),
    ("^사^시^스!세^소^샤", "ざじずぜぞじゃ"),
    ("^차^치^츠!체^초^추", "ぢゃぢづぢぇぢょぢゅ"),
    ("!새", "ぜ"),  # ㅐ의 Shift(ㅒ)는 탁점
    ("^화", "ぶぁ"),
    ("^휘", "ぶぃ"),  # 후(Shift) + 작은 ぃ
    # 우 → ゔ, 나머지 아행 → ゛ 따로
    ("^우", "ゔ"),
    ("^아^이!에^오", "あ゛い゛え゛お゛"),
    ("^에", "いぇ"),  # 모음 Shift+ㅔ = 예
    ("!예", "いぇ"),  # Shift를 누른 채 ㅇ과 ㅔ를 쳐도 예에는 탁점 없음
    ("!ㅖ", "いぇ"),
    ("^ㅐ", "ぇ゛"),
    ("^야^유^요", "や゛ゆ゛よ゛"),
    ("^ㅏ", "ぁ゛"),
    # 탁점 글자가 없는 행, 이미 탁음인 행 → 그대로
    ("^나^라^마", "ならま"),
    ("^가^다^바^자^지^즈", "がだばざじず"),
    ("^워", "を"),
])
def test_shift_dakuten(text, expected):
    assert converted(text) == expected


@pytest.mark.parametrize("text", ["!츠", "^츠", "!^츠"])
def test_shift_on_consonant_or_vowel(text):
    assert converted(text) == "づ"


def test_shift_double_consonant_keys_are_dakuten():
    # Shift+QWERT(ㅃㅉㄸㄲㅆ)도 같은 자음 + 탁점
    assert converted("!사!가") == "ざが"


def test_dakuten_romaji_actions():
    engine = Engine()
    sent = []
    for vk, shift in keys("^치^챠^아"):
        sent += engine.handle(classify(vk, shift, False)).actions
    assert sent == [Romaji("di"), Romaji("di"), Romaji("lya"), Romaji("a"), Text("゛")]


def test_compound_carries_dakuten():
    # 오 다음 Shift+ㅏ → お 지우고 わ゛
    engine = Engine()
    sent = []
    for vk, shift in keys("오^ㅏ"):
        sent += engine.handle(classify(vk, shift, False)).actions
    assert sent == [Romaji("o"), Backspace(1), Romaji("wa"), Text("゛")]


def test_no_compound_after_separate_dakuten():
    # ゛를 따로 보낸 음절 뒤에는 복모음 치환(백스페이스)을 하지 않는다
    assert converted("^오ㅏ") == "お゛ぁ"


# ---------------------------------------------------------------- 백스페이스

def test_backspace_passes_and_pops():
    engine, sim = run("캬<BS>")
    assert sim.text == "き"
    assert [c.kana for c in engine.composition] == ["き"]
    assert engine.last is None


def test_compound_disabled_after_backspace():
    assert converted("오가<BS>ㅏ") == "おぁ"


# ---------------------------------------------------------------- 변환 중

def test_hangul_after_conversion_starts_new_composition():
    engine, sim = run("가<SPACE>")
    assert engine.converting
    engine, sim = run("나", engine, sim)
    assert not engine.converting
    assert engine.tracking_valid
    assert [c.kana for c in engine.composition] == ["な"]


def test_space_and_arrows_pass_while_converting():
    engine, _ = run("가<SPACE>")
    for vk in (0x20, 0x25, 0x31):
        assert not engine.handle(classify(vk, False, False)).swallow


def test_enter_after_conversion_clears():
    engine, _ = run("가<SPACE><ENTER>")
    assert not engine.converting and engine.composition == [] and engine.tracking_valid


def test_esc_while_converting_invalidates():
    engine, _ = run("가<SPACE><ESC>")
    assert not engine.converting and not engine.tracking_valid


def test_space_on_empty_composition_does_not_enter_converting():
    engine, _ = run("<SPACE>")
    assert not engine.converting


# ---------------------------------------------------------------- 추적 무효화·재개

def test_navigation_commits_batchim_and_invalidates():
    engine = Engine()
    run("간", engine)
    result = engine.handle(classify(0x25, False, False))
    assert result.swallow and result.actions[0] == Romaji("nn")
    assert not engine.tracking_valid


def test_combo_drops_pending_without_sending():
    engine, _ = run("간")
    result = engine.handle(classify(ord("C"), False, True))  # Ctrl+C
    assert not result.swallow
    assert engine.pending is None and not engine.tracking_valid


def test_mouse_click_invalidates():
    engine, _ = run("오")
    engine.mouse_click()
    _, sim = run("ㅏ", engine)
    assert not engine.tracking_valid


def test_focus_change_resets_on_next_input():
    engine, _ = run("가<LEFT>")
    engine.focus_changed()
    run("나", engine)
    assert engine.tracking_valid
    assert [c.kana for c in engine.composition] == ["な"]


def test_enter_resumes_tracking():
    engine, _ = run("가<LEFT><ENTER>")
    assert engine.tracking_valid


def test_ignore_keys_do_not_change_state():
    engine, _ = run("간")
    assert engine.handle(Event("ignore")).swallow is False
    assert engine.pending.char == "ㄴ"


# ---------------------------------------------------------------- 긴 문장 + 불변 조건

@pytest.mark.parametrize("text", [
    "와타시와 니혼고오 벤쿄우시테이마스",
    "오와와오와와와",
    "후ㅏ후ㅣ후ㅔ우ㅣ우ㅔ",
    "간<BS>ㅏ<BS><BS><BS>나",
    "가<SPACE>나<SPACE><SPACE><ESC>다<ENTER>",
    "간ㄴㄴ<BS>가-1<LEFT>ㄷ<SPACE>",
])
def test_invariants_hold(text):
    # run()이 매 키마다 불변 조건 1·2와 추적 일치를 단언한다
    run(text.replace(" ", "<SPACE>") + "<ENTER>")


# ---------------------------------------------------------------- 대기 초성 (화면에 보내지 않음)
# 초성 미리보기는 2026-11-01 제거: 조합이 하나뿐일 때 백스페이스로 비우면 트위터(X) 입력창이
# 조합을 초기화해 뒤따르는 키가 사라졌다. 초성은 모음이 올 때까지 아무것도 보내지 않는다.

def step(engine, text):
    return [engine.handle(classify(vk, shift, False)) for vk, shift in keys(text)]


@pytest.mark.parametrize("onset", ["ㄱ", "ㅋ", "ㅊ", "ㅎ", "ㅇ", "!ㅋ", "!ㅊ"])
def test_onset_sends_nothing(onset):
    assert step(Engine(), onset)[0] == Result(True)


def test_onset_then_vowel_sends_no_backspace():
    engine = Engine()
    step(engine, "ㅎ")
    assert step(engine, "ㅗ")[0].actions == [Romaji("ho")]
    step(engine, "ㅅ")
    assert step(engine, "ㅣ")[0].actions == [Romaji("si")]


def test_hoshikawa():
    # 2026-11-01 트위터 실기에서 ほsいかわ로 깨졌던 예
    engine = Engine()
    actions = [a for r in step(engine, "호시카와") for a in r.actions]
    assert actions == [Romaji("ho"), Romaji("si"), Romaji("ka"), Romaji("o"), Backspace(1), Romaji("wa")]
    assert converted("호시카와") == "ほしかわ"


def test_batchim_sequence():
    engine = Engine()
    step(engine, "가")
    assert step(engine, "ㄴ")[0] == Result(True)
    assert step(engine, "ㄱ")[0].actions == [Romaji("nn")]


def test_kyun_space():
    engine = Engine()
    step(engine, "큐ㄴ")
    assert step(engine, "<SPACE>")[0].actions == [Romaji("nn"), ResendKey()]
    assert converted("큔") == "きゅん"


def test_pending_onset_backspace_after_syllable_is_swallowed():
    engine, sim = run("가ㄴ")
    assert step(engine, "<BS>")[0] == Result(True)
    assert engine.pending is None


def test_combo_drops_pending_onset():
    engine, _ = run("가ㄴ")
    result = engine.handle(classify(ord("C"), False, True))
    assert not result.swallow and engine.pending is None


def test_navigation_with_pending_onset_passes():
    engine, _ = run("ㄷ")
    assert engine.handle(classify(0x25, False, False)) == Result(False)


@pytest.mark.parametrize("text, shown", [
    ("ㅊ", "ㅊ"),
    ("츠", "츠"),
    ("츠ㅈ", "츶"),  # 대기 초성은 한국어 IME처럼 앞 음절 받침으로 보여 줌
    ("가ㄴ", "간"),
    ("간ㅇ", "간ㅇ"),  # 받침 확정 뒤 다음 초성
    ("간아", "간아"),
    ("가나", "가나"),
    ("각", "각"),  # 버리는 받침도 친 그대로
    ("츠즈키", "츠즈키"),
    ("와", "와"),  # 복모음 치환
    ("위", "위"),  # 작은 가나로 이어도 화면은 한 음절
    ("화", "화"),
    ("워", "워"),
    ("ㅏ", "ㅏ"),
    ("이ㅔ", "이ㅔ"),
    ("예", "예"),
    ("^츠", "츠゛"),
    ("!카레-", "카゛레ー"),
    ("가<BS>", ""),
    ("간<BS>", "가"),  # 대기 초성만 지움
    ("간가<BS>", "간"),
    ("쿄<BS>", "쿄"),  # きょ → き: 음절은 가나가 다 지워질 때까지 남김
    ("쿄<BS><BS>", ""),
    ("간<SPACE>", ""),  # 변환하면 비움
    ("가<ENTER>나", "나"),
    ("가<LEFT>", ""),
])
def test_display_text(text, shown):
    engine, _ = run(text)
    assert engine.display_text() == shown


def test_display_cleared_on_click_and_focus():
    engine, _ = run("가나")
    engine.mouse_click()
    assert engine.display_text() == ""
    engine, _ = run("가ㄴ")
    engine.focus_changed()
    assert engine.display_text() == ""
    run("다", engine)
    assert engine.display_text() == "다"


def test_shift_dakuten_onset():
    engine, sim = run("!츠")
    assert sim.text == "づ"
