"""Structural inspection of OOXML containers (.docx/.docm/.xlsx/...)."""

from __future__ import annotations

import re
import zipfile
from typing import Dict, Iterator, List, Tuple

from .filetype import classify_embedded_file

MAX_MEMBERS = 5000
MAX_TOTAL_UNCOMPRESSED = 512 * 1024 * 1024
MAX_RATIO = 200  # compression ratio above which a large member is treated as a zip bomb

# External relationship types that make Office fetch and load remote content on open.
_DANGEROUS_EXTERNAL_RELS = {"attachedtemplate", "oleobject", "frame", "subdocument", "afchunk",
                            "externallinkpath", "package"}
# Any namespace prefix; attributes in either quote style (both are valid XML and accepted by Word).
_REL_RE = re.compile(r"<(?:[\w.-]+:)?Relationship\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"([\w:.-]+)\s*=\s*([\"'])(.*?)\2", re.DOTALL)


class UnsafeArchiveError(Exception):
    """The container is too large, too deeply compressed or malformed to inspect safely."""


def iter_members(path: str) -> Iterator[Tuple[str, bytes]]:
    """Yield (name, bytes) for each member, enforcing zip-bomb limits."""
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        if len(infos) > MAX_MEMBERS:
            raise UnsafeArchiveError(f"{len(infos)} archive members (limit {MAX_MEMBERS})")
        total = sum(i.file_size for i in infos)
        if total > MAX_TOTAL_UNCOMPRESSED:
            raise UnsafeArchiveError(f"{total} bytes uncompressed (limit {MAX_TOTAL_UNCOMPRESSED})")
        for info in infos:
            if info.file_size > 10 * 1024 * 1024 and info.compress_size and \
                    info.file_size / info.compress_size > MAX_RATIO:
                raise UnsafeArchiveError(f"{info.filename} decompresses {info.file_size // info.compress_size}x")
            yield info.filename, z.read(info)


def _external_targets(rels_xml: str) -> List[str]:
    targets = []
    for element in _REL_RE.findall(rels_xml):
        attrs = {name.split(":")[-1].lower(): value for name, _, value in _ATTR_RE.findall(element)}
        if attrs.get("targetmode", "").strip().lower() != "external":
            continue
        if attrs.get("type", "").rstrip("/").rsplit("/", 1)[-1].lower() in _DANGEROUS_EXTERNAL_RELS:
            targets.append(attrs.get("target", "?"))
    return targets


def inspect_with_members(path: str) -> Tuple[Dict, List[Tuple[str, bytes]]]:
    """Structural report plus the decompressed members (one pass over the archive).

    The report has ``macro_verdict`` ("NONE" | "PRESENT"), ``external_ref`` ("NONE" |
    "FOUND"), ``external_targets``, ``embedded_objects``, ``dangerous_embeds``,
    ``recommendation`` ("PASS" | "SANITIZE" | "BLOCK") and ``reason``. Members are
    empty when the archive could not be read safely (the report then says BLOCK).
    """
    result = {
        "macro_verdict": "NONE",
        "external_ref": "NONE",
        "external_targets": [],
        "embedded_objects": [],
        "dangerous_embeds": [],
        "recommendation": "PASS",
        "reason": "No active content detected",
    }
    if not zipfile.is_zipfile(path):
        result.update(recommendation="BLOCK", reason="Not a valid OOXML (zip) container")
        return result, []

    members: List[Tuple[str, bytes]] = []
    try:
        for name, data in iter_members(path):
            members.append((name, data))
            lower = name.lower()
            if lower.endswith("vbaproject.bin"):
                result["macro_verdict"] = "PRESENT"
            if lower.endswith(".rels"):
                result["external_targets"] += _external_targets(data.decode("utf-8", errors="ignore"))
            if "/embeddings/" in lower or lower.startswith("word/activex/"):
                result["embedded_objects"].append(name)
                verdict, mime = classify_embedded_file(data, name)
                if verdict == "DANGEROUS":
                    result["dangerous_embeds"].append(f"{name} ({mime})")
    except Exception as exc:  # zip bombs, bad zips, encrypted or unsupported-compression members, zlib errors
        result.update(recommendation="BLOCK", reason=f"Could not safely inspect container: {exc}")
        return result, []

    reasons: List[str] = []
    if result["macro_verdict"] == "PRESENT":
        reasons.append("VBA macro project present")
    if result["external_targets"]:
        result["external_ref"] = "FOUND"
        reasons.append(f"remote content loaded on open: {result['external_targets'][:5]}")
    if result["dangerous_embeds"]:
        reasons.append(f"executable embedded objects: {result['dangerous_embeds'][:5]}")

    if reasons:
        result.update(recommendation="BLOCK", reason="; ".join(reasons))
    elif result["embedded_objects"]:
        result.update(recommendation="SANITIZE",
                      reason=f"embedded objects present: {result['embedded_objects'][:5]}")
    return result, members


def inspect_ooxml(path: str) -> Dict:
    """Structural report only (see ``inspect_with_members``)."""
    return inspect_with_members(path)[0]


# Backwards-compatible name used by the original test-suite.
classify_docx_content = inspect_ooxml
