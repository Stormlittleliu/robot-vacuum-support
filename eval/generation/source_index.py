# -*- coding: utf-8 -*-
"""Read-only source index for the robot-vacuum eval dataset.

The knowledge corpus is never modified.  PDF text is extracted with
``pdftotext -layout`` when available (the same method used by the v0 audit) and
falls back to pypdf.  Quotes are checked twice: exact substring first, then a
whitespace-normalised form so that PDF line wrapping does not create a false
"missing quote" while still recording the weaker match.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_DIR = ROOT / "assets" / "knowledge"
README = ROOT / "README.md"
PDF_REL = "assets/knowledge/扫地机器人100问.pdf"
TXT_RELS = (
    "assets/knowledge/故障排除.txt",
    "assets/knowledge/扫地机器人100问2.txt",
    "assets/knowledge/扫拖一体机器人100问.txt",
    "assets/knowledge/维护保养.txt",
    "assets/knowledge/选购指南.txt",
)
DOC_RELS = (TXT_RELS[0], PDF_REL, *TXT_RELS[1:])
_CREATE_NO_WINDOW = 0x08000000
_WS = re.compile(r"[ \t\u3000\r\n]+")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def normalize_quote(text: str) -> str:
    return _WS.sub("", text.replace("\ufeff", ""))


def _extract_pdf() -> tuple[str, str]:
    pdf = ROOT / PDF_REL
    exe = shutil.which("pdftotext") or r"D:\texlive\2026\bin\windows\pdftotext.exe"
    if exe and Path(exe).is_file():
        try:
            proc = subprocess.run(
                [exe, "-enc", "UTF-8", "-layout", str(pdf), "-"],
                check=True,
                capture_output=True,
                creationflags=_CREATE_NO_WINDOW,
            )
            text = proc.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text, "pdftotext -enc UTF-8 -layout"
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(pdf))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if text.strip():
            return text, "pypdf extract_text"
    except Exception:  # pragma: no cover - optional fallback
        pass
    raise RuntimeError("cannot extract PDF text; install pdftotext or pypdf")


def load_sources() -> dict[str, dict[str, Any]]:
    """Return {relative path: {text, sha256, bytes, extraction, kind}}."""
    out: dict[str, dict[str, Any]] = {}
    pdf_text, pdf_method = _extract_pdf()
    for rel in DOC_RELS:
        path = ROOT / rel
        raw = path.read_bytes()
        if rel == PDF_REL:
            text, method = pdf_text, pdf_method
        else:
            text, method = raw.decode("utf-8-sig"), "utf-8 text"
        out[rel] = {
            "text": text,
            "sha256": sha256_bytes(raw),
            "bytes": len(raw),
            "extraction": method,
            "kind": "knowledge",
        }
    readme_raw = README.read_bytes()
    out["README.md"] = {
        "text": readme_raw.decode("utf-8-sig"),
        "sha256": sha256_bytes(readme_raw),
        "bytes": len(readme_raw),
        "extraction": "utf-8 text",
        "kind": "requirement",
    }
    return out


def check_quote(quote: str, text: str) -> str:
    """Return exact | normalized | missing."""
    if quote in text:
        return "exact"
    if normalize_quote(quote) in normalize_quote(text):
        return "normalized"
    return "missing"


def doc_entry(rel: str, source: dict[str, Any]) -> dict[str, Any]:
    entry = {
        "path": rel,
        "sha256": source["sha256"],
        "bytes": source["bytes"],
        "kind": source["kind"],
        "extraction": source["extraction"],
    }
    if rel == PDF_REL:
        entry["note"] = (
            "PDF text differs by extractor; quotes are checked against "
            "pdftotext -layout output and whitespace-normalised as fallback."
        )
    return entry
