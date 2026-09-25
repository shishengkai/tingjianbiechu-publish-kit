"""Cover title OCR matching and soft-QA parsing."""
from __future__ import annotations

import json
import re
from typing import Any


def normalize_cover_text(value: str) -> str:
    text = value.replace("\r", "\n")
    text = re.sub(r"[\s\u3000]+", "", text)
    # Common OCR punctuation variants and decorative quote frames
    table = str.maketrans({
        "？": "?",
        "！": "!",
        "：": ":",
        "，": ",",
        "。": ".",
        "／": "/",
        "/": "",
        "·": "",
        "—": "",
        "-": "",
        "“": "",
        "”": "",
        "\"": "",
        "'": "",
        "「": "",
        "」": "",
        "『": "",
        "』": "",
        "《": "",
        "》": "",
        "【": "",
        "】": "",
        "（": "",
        "）": "",
        "(": "",
        ")": "",
        "〔": "",
        "〕": "",
    })
    return text.translate(table)


def ocr_matches_cover_title(ocr_text: str, cover_title: str) -> bool:
    got = normalize_cover_text(ocr_text)
    want = normalize_cover_text(cover_title)
    if not want:
        return False
    if got == want:
        return True
    # Title glyphs must be present exactly. Extra Latin / decorative noise is soft-QA
    # (extra_text), not a hard OCR miss — aligns with scope: 多余小字 → soft gate.
    if want in got:
        rest = got.replace(want, "", 1)
        rest_cjk = "".join(ch for ch in rest if "\u4e00" <= ch <= "\u9fff")
        return rest_cjk == ""
    return False


def parse_soft_qa(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("soft QA must be object")
    return {
        "pass": bool(data.get("pass")),
        "face_ok": bool(data.get("face_ok", True)),
        "extra_text": bool(data.get("extra_text", False)),
        "blocks_face": bool(data.get("blocks_face", False)),
        "thumbnail_ok": bool(data.get("thumbnail_ok", True)),
        "issues": list(data.get("issues") or []),
    }


def soft_qa_passed(result: dict[str, Any]) -> bool:
    if result.get("extra_text") or result.get("blocks_face"):
        return False
    if not result.get("face_ok", True) or not result.get("thumbnail_ok", True):
        return False
    # aigc_impact is advisory during soft QA; missing key must not hard-fail old records.
    if result.get("aigc_impact") is False:
        return False
    return bool(result.get("pass"))
