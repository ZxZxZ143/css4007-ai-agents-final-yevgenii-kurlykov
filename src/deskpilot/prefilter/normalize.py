"""Text normalization for the request pre-filter. Pure, deterministic, stdlib only."""

import unicodedata

MAX_INPUT_CHARS = 2000
LATIN_SHARE_FOR_TRANSLIT = 0.8

_LATIN_TO_CYR = dict(zip("aeopcxykmthb", "аеорсхукмтнв", strict=True))
_CYR_TO_LATIN = {c: la for la, c in _LATIN_TO_CYR.items()}

_TRANSLIT_MULTI = (
    ("shch", "щ"),
    ("sch", "щ"),
    ("yo", "е"),
    ("yu", "ю"),
    ("ju", "ю"),
    ("ya", "я"),
    ("ja", "я"),
    ("ye", "е"),
    ("zh", "ж"),
    ("kh", "х"),
    ("ts", "ц"),
    ("ch", "ч"),
    ("sh", "ш"),
    ("iy", "ий"),
)
_TRANSLIT_SINGLE = dict(
    zip(
        "abdefgijklmnopqrstuvwxyz",
        ["а", "б", "д", "е", "ф", "г", "и", "й", "к", "л", "м", "н", "о", "п", "к", "р"]
        + ["с", "т", "у", "в", "в", "кс", "ы", "з"],
        strict=True,
    )
)
_TRANSLIT_SINGLE["c"] = "к"
_TRANSLIT_SINGLE["h"] = "х"
_DRIVE_LETTERS = "cdef"
_ENGLISH_ONLY = ("th", "ck", "oo", "ee", "ing", "tion", "wh", "ou", "ght", "ph", "qu")


def normalize(text: str) -> str:
    """NFKC, lower case, ё->е, drop invisible chars, punctuation to spaces, collapse spaces."""
    out: list[str] = []
    for ch in unicodedata.normalize("NFKC", text[:MAX_INPUT_CHARS]).lower().replace("ё", "е"):
        cat = unicodedata.category(ch)
        if ch.isspace():
            out.append(" ")
        elif cat in ("Cf", "Cc", "Mn", "Co", "Cn"):
            continue
        elif cat[0] in "PSZ":
            out.append(" ")
        else:
            out.append(ch)
    return " ".join("".join(out).split())


def _script(ch: str) -> str:
    if "a" <= ch <= "z":
        return "lat"
    if "а" <= ch <= "я":
        return "cyr"
    return ""


def _fix_token(token: str) -> str:
    scripts = [_script(ch) for ch in token]
    lat, cyr = scripts.count("lat"), scripts.count("cyr")
    if not lat or not cyr:
        return token
    # Minority-script look-alikes are converted to the majority script.
    to_cyr = cyr >= lat
    table, minority = (_LATIN_TO_CYR, "lat") if to_cyr else (_CYR_TO_LATIN, "cyr")
    return "".join(
        table.get(ch, ch) if s == minority else ch for ch, s in zip(token, scripts, strict=True)
    )


def _transliterate_token(token: str) -> str:
    if len(token) == 1 and token in _DRIVE_LETTERS:
        return token
    out: list[str] = []
    i = 0
    while i < len(token):
        for src, dst in _TRANSLIT_MULTI:
            if token.startswith(src, i):
                out.append(dst)
                i += len(src)
                break
        else:
            out.append(_TRANSLIT_SINGLE.get(token[i], token[i]))
            i += 1
    return "".join(out)


def _mostly_latin(text: str) -> bool:
    scripts = [s for s in map(_script, text) if s]
    return bool(scripts) and scripts.count("lat") / len(scripts) >= LATIN_SHARE_FOR_TRANSLIT


def variants(text: str) -> tuple[str, ...]:
    """Normalized text plus homoglyph-fixed and Latin->Cyrillic transliterated variants."""
    base = normalize(text)
    fixed = " ".join(_fix_token(t) for t in base.split())
    out = [base]
    if fixed != base:
        out.append(fixed)
    if _mostly_latin(fixed) and not any(p in fixed for p in _ENGLISH_ONLY):
        translit = " ".join(_transliterate_token(t) for t in fixed.split())
        if translit not in out:
            out.append(translit)
    return tuple(out)
