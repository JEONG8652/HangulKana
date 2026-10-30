"""설정 파일 config.toml 읽기·쓰기와 단축키 해석. OS 비의존.

Python 3.10에는 tomllib이 없으므로, 이 프로그램이 쓰는 단순한 형식(한 줄에 `키 = 값`, 값은
true/false·정수·"문자열", `#` 주석)만 직접 읽는다. 모르는 키는 무시하고, 잘못된 값은 기본값을 쓴다.
"""

import os
import re
from dataclasses import dataclass, field, fields
from typing import FrozenSet, List, Optional, Tuple

LOG_LEVELS = ("error", "warning", "info", "debug")


@dataclass
class Config:
    overlay: bool = True  # 한글 띄움창. 트레이 메뉴에서 바꾸면 저장된다
    preview: bool = True  # 초성 미리보기 (ㅋ → ｋ)
    pause_hotkey: str = "ctrl+alt+p"  # 일시 정지 켜기/끄기. 비우면 없음
    log_level: str = "error"  # error / warning / info / debug (debug는 입력 내용이 기록됨)
    warnings: List[str] = field(default_factory=list, compare=False, repr=False)


TEMPLATE = """\
# HangulKana 설정 파일
# 바꾼 뒤 HangulKana를 다시 실행하면 적용됩니다.

# 한글 띄움창 (트레이 메뉴 "한글 띄움창"으로도 켜고 끌 수 있으며, 그때 이 값이 저장됩니다)
overlay = {overlay}

# 자음만 쳤을 때 입력 위치에 로마자 한 글자로 미리 보여 주기 (ㅋ → ｋ)
preview = {preview}

# 일시 정지 켜기/끄기 단축키. 예: "ctrl+alt+p", "ctrl+shift+f12". 빈 문자열("")이면 단축키 없음
pause_hotkey = "{pause_hotkey}"

# 로그 수준: "error"(기본) / "warning" / "info" / "debug"
# debug는 입력한 내용이 로그 파일에 기록되므로 문제를 조사할 때만 쓰세요.
log_level = "{log_level}"
"""

_LINE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$')


def _strip_comment(value: str) -> str:
    """따옴표 밖의 # 이후를 지운다."""
    out, quoted = [], False
    for ch in value:
        if ch == '"':
            quoted = not quoted
        elif ch == "#" and not quoted:
            break
        out.append(ch)
    return "".join(out).strip()


def _parse_value(raw: str):
    if raw in ("true", "false"):
        return raw == "true"
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return raw[1:-1]
    if re.fullmatch(r"[+-]?\d+", raw):
        return int(raw)
    raise ValueError(raw)


def parse(text: str) -> Config:
    cfg = Config()
    types = {f.name: f.type for f in fields(Config) if f.name != "warnings"}
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            cfg.warnings.append(f"{n}번째 줄을 읽을 수 없습니다: {line.strip()}")
            continue
        key, raw = m.group(1), _strip_comment(m.group(2))
        if key not in types:
            cfg.warnings.append(f"모르는 설정 '{key}'는 무시합니다")
            continue
        try:
            value = _parse_value(raw)
        except ValueError:
            cfg.warnings.append(f"'{key}' 값 {raw}을(를) 읽을 수 없어 기본값을 씁니다")
            continue
        expected = bool if types[key] in (bool, "bool") else str
        if not isinstance(value, expected):
            cfg.warnings.append(f"'{key}' 값의 형식이 맞지 않아 기본값을 씁니다")
            continue
        setattr(cfg, key, value)
    if cfg.log_level not in LOG_LEVELS:
        cfg.warnings.append(f"log_level '{cfg.log_level}'은(는) 쓸 수 없어 error로 합니다")
        cfg.log_level = "error"
    if cfg.pause_hotkey and parse_hotkey(cfg.pause_hotkey) is None:
        cfg.warnings.append(f"pause_hotkey '{cfg.pause_hotkey}'을(를) 해석할 수 없어 단축키 없이 실행합니다")
        cfg.pause_hotkey = ""
    return cfg


def dump(cfg: Config) -> str:
    return TEMPLATE.format(
        overlay=str(cfg.overlay).lower(), preview=str(cfg.preview).lower(),
        pause_hotkey=cfg.pause_hotkey, log_level=cfg.log_level)


def load(path: str, create: bool = True) -> Config:
    """설정을 읽는다. 파일이 없으면 기본값으로 만들고(create) 기본값을 돌려준다."""
    if not os.path.exists(path):
        cfg = Config()
        if create:
            save(path, cfg)
        return cfg
    with open(path, encoding="utf-8-sig") as f:
        return parse(f.read())


def save(path: str, cfg: Config) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(dump(cfg))
    os.replace(tmp, path)


# ---------------------------------------------------------------- 단축키

MODIFIERS = ("ctrl", "alt", "shift", "win")
_KEY_ALIASES = {"control": "ctrl", "menu": "alt", "windows": "win"}


def parse_hotkey(text: str) -> Optional[Tuple[FrozenSet[str], int]]:
    """"ctrl+alt+p" → (frozenset({"ctrl", "alt"}), VK). 수식키가 없거나 해석할 수 없으면 None.

    수식키 없이 글자 키 하나만 잡으면 평소 입력을 가로채므로 허용하지 않는다."""
    parts = [_KEY_ALIASES.get(p.strip().lower(), p.strip().lower()) for p in text.split("+")]
    if not parts or not all(parts):
        return None
    *mods, key = parts
    if not mods or any(m not in MODIFIERS for m in mods) or len(set(mods)) != len(mods):
        return None
    vk = _key_vk(key)
    if vk is None:
        return None
    return frozenset(mods), vk


def _key_vk(key: str) -> Optional[int]:
    if len(key) == 1 and ("a" <= key <= "z" or "0" <= key <= "9"):
        return ord(key.upper())
    m = re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", key)
    if m:
        return 0x6F + int(m.group(1))  # VK_F1 = 0x70
    return {"pause": 0x13, "scrolllock": 0x91, "insert": 0x2D, "home": 0x24, "end": 0x23}.get(key)
