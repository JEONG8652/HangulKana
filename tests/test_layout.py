import pytest

from hangulkana import romaji_table
from hangulkana.layout import Jamo, classify, to_jamo


@pytest.mark.parametrize("key, jamo", list(zip(
    "QWERTYUIOPASDFGHJKLZXCVBNM",
    "ㅂㅈㄷㄱㅅㅛㅕㅑㅐㅔㅁㄴㅇㄹㅎㅗㅓㅏㅣㅋㅌㅊㅍㅠㅜㅡ",
)))
def test_dubeolsik_layout(key, jamo):
    assert to_jamo(ord(key), False).char == jamo


@pytest.mark.parametrize("key, jamo", list(zip(
    "QWERTYUIOPASDFGHJKLZXCVBNM",
    "ㅂㅈㄷㄱㅅㅛㅕㅑㅐㅔㅁㄴㅇㄹㅎㅗㅓㅏㅣㅋㅌㅊㅍㅠㅜㅡ",
)))
def test_shift_is_dakuten_not_double_or_shifted_vowel(key, jamo):
    # Shift+QWERT(쌍자음)·Shift+O(ㅒ)도 같은 자모 + 탁점. Shift+P만 예외로 ㅖ
    j = to_jamo(ord(key), True)
    if key == "P":
        assert j == Jamo("vowel", "ㅖ")
    else:
        assert j.char == jamo and j.dakuten
    assert not to_jamo(ord(key), False).dakuten


def test_classify():
    assert classify(0x20, False, False).sub == "space"
    assert classify(0x0D, False, False).sub == "enter"
    assert classify(0x77, False, False).sub == "fkey"  # F8
    assert classify(0x08, False, False).kind == "bs"
    assert classify(0xBD, False, False) == classify(0xBD, False, False)._replace(kind="char", sub="ー")
    assert classify(0x31, False, False).sub == "１"
    assert classify(0x25, False, False).sub == "arrow"
    assert classify(0x1B, False, False).sub == "esc"
    assert classify(0x09, False, False).sub == "other"
    assert classify(ord("C"), False, True).kind == "combo"
    for vk in (0x10, 0xA0, 0xA2, 0x14, 0x15, 0xF3):
        assert classify(vk, False, False).kind == "ignore"


def test_table_counts():
    # 음절 표: 14초성 × 9열 - "무시" 4칸 + ㅐ열(=ㅔ) 14 + 와·워·화·예 4 (위·웨·휘·훼는 작은 가나로 처리)
    assert len(romaji_table.SYLLABLES) == 14 * 9 - 4 + 14 + 4
    assert romaji_table.syllable("ㅊ", "ㅡ").romaji == "tsu"
    assert romaji_table.syllable("ㅈ", "ㅜ").romaji == "zyu"
    assert romaji_table.syllable("ㄷ", "ㅑ") is None
    assert romaji_table.syllable("ㅇ", "ㅝ").kana == "を"


@pytest.mark.parametrize("kana, voiced", [
    ("か", "が"), ("ちゃ", "ぢゃ"), ("つ", "づ"), ("ぱ", "ば"), ("ふぁ", "ぶぁ"), ("てぃ", "でぃ"),
    ("う", "ゔ"), ("あ", "あ゛"), ("や", "や゛"), ("ぁ", "ぁ゛"),
    ("な", "な"), ("が", "が"), ("じ", "じ"), ("を", "を"),
])
def test_voiced(kana, voiced):
    assert romaji_table.voiced(kana) == voiced


def test_every_reachable_kana_has_romaji():
    # 모든 음절·작은 가나에 Shift 탁점을 붙여도 완결 로마자(또는 ゛)로 보낼 수 있다 (불변 조건 1)
    units = [*romaji_table.SYLLABLES.values(), *romaji_table.SMALL.values()]
    for u in units:
        for kana in (u.kana, romaji_table.voiced(u.kana)):
            parts = romaji_table.to_romaji(kana)
            rebuilt = "".join(p if p == romaji_table.DAKUTEN_MARK else romaji_table.ROMAJI_TO_KANA[p] for p in parts)
            assert rebuilt == kana
