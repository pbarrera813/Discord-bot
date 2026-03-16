from __future__ import annotations


def normalize_language(language_code: str | None) -> str:
    if not language_code:
        return "en"
    code = language_code.strip().lower()
    return code if code in {"en", "es"} else "en"


def _repair_mojibake_text(text: str) -> str:
    if not text:
        return text
    # Common broken UTF-8-in-Latin1 rendering seen in Discord outputs.
    if not any(marker in text for marker in ("Ã", "Â", "â")):
        return text

    repaired = text
    for codec in ("latin1", "cp1252"):
        try:
            candidate = repaired.encode(codec, errors="strict").decode("utf-8", errors="strict")
        except UnicodeError:
            continue
        repaired = candidate
    return repaired


def tr(lang: str, en_text: str, es_text: str) -> str:
    selected = es_text if normalize_language(lang) == "es" else en_text
    return _repair_mojibake_text(selected)
