"""(초성, 중성) → 송신 로마자·가나 표. OS 비의존."""

from typing import List, NamedTuple, Optional


class Unit(NamedTuple):
    romaji: str
    kana: str


# 표의 열 순서. ㅐ는 ㅔ와 같다.
_COLUMNS = ("ㅏ", "ㅣ", "ㅜ", "ㅡ", "ㅔ", "ㅗ", "ㅑ", "ㅠ", "ㅛ")

# "-" = 무시
_ROWS = {
    "ㅇ": "a:あ i:い u:う u:う e:え o:お ya:や yu:ゆ yo:よ",
    "ㄱ": "ga:が gi:ぎ gu:ぐ gu:ぐ ge:げ go:ご gya:ぎゃ gyu:ぎゅ gyo:ぎょ",
    "ㅋ": "ka:か ki:き ku:く ku:く ke:け ko:こ kya:きゃ kyu:きゅ kyo:きょ",
    "ㄷ": "da:だ dhi:でぃ dwu:どぅ dwu:どぅ de:で do:ど - dhu:でゅ -",
    "ㅌ": "ta:た thi:てぃ twu:とぅ twu:とぅ te:て to:と - thu:てゅ -",
    "ㅂ": "ba:ば bi:び bu:ぶ bu:ぶ be:べ bo:ぼ bya:びゃ byu:びゅ byo:びょ",
    "ㅍ": "pa:ぱ pi:ぴ pu:ぷ pu:ぷ pe:ぺ po:ぽ pya:ぴゃ pyu:ぴゅ pyo:ぴょ",
    "ㅅ": "sa:さ si:し su:す su:す se:せ so:そ sya:しゃ syu:しゅ syo:しょ",
    "ㅈ": "za:ざ zi:じ zyu:じゅ zu:ず ze:ぜ zo:ぞ zya:じゃ zyu:じゅ zyo:じょ",
    "ㅊ": "cha:ちゃ chi:ち chu:ちゅ tsu:つ che:ちぇ cho:ちょ cha:ちゃ chu:ちゅ cho:ちょ",
    "ㄴ": "na:な ni:に nu:ぬ nu:ぬ ne:ね no:の nya:にゃ nyu:にゅ nyo:にょ",
    "ㄹ": "ra:ら ri:り ru:る ru:る re:れ ro:ろ rya:りゃ ryu:りゅ ryo:りょ",
    "ㅁ": "ma:ま mi:み mu:む mu:む me:め mo:も mya:みゃ myu:みゅ myo:みょ",
    "ㅎ": "ha:は hi:ひ hu:ふ hu:ふ he:へ ho:ほ hya:ひゃ hyu:ひゅ hyo:ひょ",
}


def _parse():
    table = {}
    for onset, row in _ROWS.items():
        cells = row.split()
        assert len(cells) == len(_COLUMNS), onset
        for vowel, cell in zip(_COLUMNS, cells):
            if cell != "-":
                table[(onset, vowel)] = Unit(*cell.split(":"))
        if (onset, "ㅔ") in table:
            table[(onset, "ㅐ")] = table[(onset, "ㅔ")]
    return table


SYLLABLES = _parse()

# 복모음: 아래 음절만 허용
# 위·웨·휘·훼는 우·후 + 작은 가나(ぃ・ぇ)로 같은 결과가 나오므로 치환하지 않는다
SYLLABLES.update({
    ("ㅇ", "ㅘ"): Unit("wa", "わ"),
    ("ㅇ", "ㅝ"): Unit("wo", "を"),  # 워 = を
    ("ㅇ", "ㅖ"): Unit("ye", "いぇ"),  # 예 (ㅇ 이외 초성 + ㅖ는 무시)
    ("ㅎ", "ㅘ"): Unit("fa", "ふぁ"),
})

# 직전 음절 모음 + ㅇ 없이 친 모음 → 복모음. お+ぁ ≠ わ, う+ㅓ → を 이므로 치환이 필요하다
COMPOUND_VOWELS = {("ㅗ", "ㅏ"): "ㅘ", ("ㅜ", "ㅓ"): "ㅝ"}

# ㅇ 없이 친 모음 → 작은 가나
SMALL = {
    "ㅏ": Unit("la", "ぁ"),
    "ㅣ": Unit("li", "ぃ"),
    "ㅜ": Unit("lu", "ぅ"),
    "ㅡ": Unit("lu", "ぅ"),
    "ㅔ": Unit("le", "ぇ"),
    "ㅐ": Unit("le", "ぇ"),
    "ㅗ": Unit("lo", "ぉ"),
    "ㅑ": Unit("lya", "ゃ"),
    "ㅠ": Unit("lyu", "ゅ"),
    "ㅛ": Unit("lyo", "ょ"),
}

# 받침. 나머지 받침은 버린다.
BATCHIM = {"ㄴ": Unit("nn", "ん"), "ㅇ": Unit("nn", "ん"), "ㅅ": Unit("ltu", "っ")}

# ---------------------------------------------------------------- Shift 탁점

DAKUTEN_MARK = "゛"  # 로마자로 입력할 수 없어 유니코드 문자로 직접 보낸다

# 첫 글자에 탁점을 붙인 글자. ぱ행(반탁점)은 ば행으로
_VOICED = dict(zip(
    "かきくけこさしすせそたちつてとはひふへほぱぴぷぺぽう",
    "がぎぐげござじずぜぞだぢづでどばびぶべぼばびぶべぼゔ",
))
# 탁점 글자가 없는 あ행: 뒤에 ゛를 따로 붙인다. な・ら・ま행과 이미 탁음인 행은 그대로
_SEPARATE = set("あいえおやゆよわぁぃぅぇぉゃゅょ")

# 탁점 글자의 로마자 (위 표에 없는 것). ぢゃ 등은 ぢ + ゃ(lya)로 나눠 보낸다
_EXTRA_ROMAJI = {"ぢ": "di", "づ": "du", "ゔ": "vu"}

# 엔진이 보낼 수 있는 모든 완결 로마자 (engine.py 불변 조건 1)
ROMAJI_TO_KANA = {u.romaji: u.kana for u in (*SYLLABLES.values(), *SMALL.values(), *BATCHIM.values())}
ROMAJI_TO_KANA.update({r: k for k, r in _EXTRA_ROMAJI.items()})

# 가나 → 로마자 (긴 것 우선으로 나눠 보낸다)
_KANA_TO_ROMAJI = {}
for _r, _k in ROMAJI_TO_KANA.items():
    _KANA_TO_ROMAJI.setdefault(_k, _r)
_MAX_KANA = max(len(k) for k in _KANA_TO_ROMAJI)


def voiced(kana: str) -> str:
    """Shift 탁점: か→が, ちゃ→ぢゃ, う→ゔ, あ→あ゛. 탁점을 붙일 수 없으면 그대로."""
    first = kana[0]
    if first in _VOICED:
        return _VOICED[first] + kana[1:]
    if first in _SEPARATE:
        return kana + DAKUTEN_MARK
    return kana


def to_romaji(kana: str) -> List[str]:
    """가나 문자열 → 완결 로마자 목록 (゛는 그대로 한 항목). 바꿀 수 없으면 ValueError."""
    out = []
    i = 0
    while i < len(kana):
        if kana[i] == DAKUTEN_MARK:
            out.append(DAKUTEN_MARK)
            i += 1
            continue
        for n in range(min(_MAX_KANA, len(kana) - i), 0, -1):
            r = _KANA_TO_ROMAJI.get(kana[i:i + n])
            if r is not None:
                out.append(r)
                i += n
                break
        else:
            raise ValueError(kana)
    return out


def syllable(onset: str, vowel: str) -> Optional[Unit]:
    """음절의 송신 단위. 표에서 "무시"이거나 허용되지 않으면 None."""
    return SYLLABLES.get((onset, vowel))
