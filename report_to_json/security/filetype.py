"""File-type sniffing from magic bytes (no libmagic dependency)."""

from __future__ import annotations

import os
from typing import Tuple

_SIGNATURES = [
    (b"MZ", "application/x-msdownload"),
    (b"\x7fELF", "application/x-executable"),
    (b"\xca\xfe\xba\xbe", "application/x-mach-binary"),
    (b"\xcf\xfa\xed\xfe", "application/x-mach-binary"),
    (b"#!", "text/x-shellscript"),
    (b"%PDF-", "application/pdf"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/x-ole-storage"),
    (b"PK\x03\x04", "application/zip"),
    (b"Rar!\x1a\x07", "application/x-rar-compressed"),
    (b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"BM", "image/bmp"),
    (b"\x01\x00\x00\x00", "image/emf"),
    (b"\xd7\xcd\xc6\x9a", "image/wmf"),
]

SAFE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/tiff", "image/bmp", "image/emf", "image/wmf",
              "text/plain", "application/xml", "application/json"}
DANGEROUS_TYPES = {"application/x-msdownload", "application/x-executable", "application/x-mach-binary",
                   "text/x-shellscript"}
# Containers that can carry anything; never auto-pass them.
CONTAINER_TYPES = {"application/zip", "application/x-rar-compressed", "application/x-7z-compressed",
                   "application/x-ole-storage", "application/pdf"}


def sniff(data: bytes) -> str:
    for magic, mime in _SIGNATURES:
        if data.startswith(magic):
            return mime
    head = data[:512].lstrip()
    if head.startswith(b"<?xml") or head.startswith(b"<"):
        return "application/xml"
    if head.startswith((b"{", b"[")):
        return "application/json"
    try:
        data[:4096].decode("utf-8")
        return "text/plain"
    except UnicodeDecodeError:
        return "application/octet-stream"


def classify_embedded_file(data: bytes, filename: str = "") -> Tuple[str, str]:
    """Return (verdict, mime) with verdict "SAFE" | "DANGEROUS" | "UNKNOWN".

    Unrecognised binaries are UNKNOWN, never SAFE.
    """
    mime = sniff(data)
    ext = os.path.splitext(filename)[1].lower()
    if mime in DANGEROUS_TYPES or ext in (".exe", ".dll", ".scr", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".hta"):
        return "DANGEROUS", mime
    if mime in SAFE_TYPES:
        return "SAFE", mime
    return "UNKNOWN", mime


def classify_txt_content(path: str) -> dict:
    """Is a .txt file really text, or a binary/script wearing a .txt extension?"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as exc:
        return {"mime_verdict": "UNKNOWN", "mime": "unknown", "recommendation": "BLOCK",
                "reason": f"Could not read file: {exc}"}
    verdict, mime = classify_embedded_file(data, "")
    if verdict == "DANGEROUS" or mime != "text/plain":
        return {"mime_verdict": verdict, "mime": mime, "recommendation": "BLOCK",
                "reason": f".txt content is '{mime}', not plain text"}
    return {"mime_verdict": verdict, "mime": mime, "recommendation": "PASS", "reason": "Plain text"}
