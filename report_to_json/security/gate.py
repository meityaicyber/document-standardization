"""
Malware pre-gate: every input is screened before any parser (pdfplumber,
python-docx, a model) touches it.

The gate fails closed: if YARA is not installed or the rules do not compile,
``malware_gate`` raises ``GateUnavailableError`` rather than letting files through.

What is scanned
  * the raw file bytes;
  * PDFs: every object dictionary as decompressed by PyMuPDF (so keywords inside
    compressed object streams are visible) and every embedded file attachment;
  * OOXML: every archive member, plus a structural check for macros,
    remote-template/OLE relationships and executable embedded objects.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..config import YARA_RULES_DIR
from .filetype import classify_embedded_file
from .ooxml import inspect_with_members

log = logging.getLogger(__name__)

VERDICTS = ("CLEAN", "TEST_SIGNATURE_DETECTED", "SUSPICIOUS", "MALICIOUS")
DEFAULT_BLOCK_ON = ("MALICIOUS", "SUSPICIOUS")
MAX_SCAN_BYTES = 256 * 1024 * 1024
_RULE_VERDICT = {"malicious": "MALICIOUS", "suspicious": "SUSPICIOUS", "test": "TEST_SIGNATURE_DETECTED"}
_OOXML_SUFFIXES = {".docx", ".docm", ".dotx", ".dotm", ".xlsx", ".xlsm", ".pptx", ".pptm"}


class GateUnavailableError(RuntimeError):
    """The scanner cannot run (yara-python missing or rules invalid). Inputs must not proceed."""


class MalwareDetectedError(Exception):
    """Raised when the gate blocks a file."""

    def __init__(self, scan_result: Dict[str, Any]):
        self.scan_result = scan_result
        reasons = [m["rule"] for m in scan_result["rule_matches"]] + \
                  [c["detail"] for c in scan_result["checks"] if c["verdict"] != "CLEAN"]
        super().__init__(f"Blocked '{Path(scan_result['file']).name}': verdict={scan_result['verdict']} "
                         f"({'; '.join(reasons) or 'no detail'})")


_RULES = None


def compile_rules(rules_dir: Path = YARA_RULES_DIR):
    try:
        import yara
    except ImportError as exc:
        raise GateUnavailableError("yara-python is not installed (pip install yara-python)") from exc
    files = {p.stem: str(p) for p in sorted(Path(rules_dir).glob("*.yar"))}
    if not files:
        raise GateUnavailableError(f"No YARA rules found in {rules_dir}")
    try:
        return yara.compile(filepaths=files, externals={"kind": ""})
    except yara.Error as exc:
        raise GateUnavailableError(f"YARA rules failed to compile: {exc}") from exc


def _rules():
    global _RULES
    if _RULES is None:
        _RULES = compile_rules()
    return _RULES


def _worst(verdicts) -> str:
    return max(verdicts, key=VERDICTS.index, default="CLEAN")


# A scan target: (label, kind, bytes). ``kind`` is passed to the rules as the
# external variable of the same name ("raw", "pdfobj", "ooxml-part", "embedded").
Target = Tuple[str, str, bytes]


def _match(rules, target: Target) -> List[Dict[str, str]]:
    label, kind, data = target
    return [
        {
            "rule": m.rule,
            "verdict": _RULE_VERDICT.get(str(m.meta.get("verdict", "")).lower(), "SUSPICIOUS"),
            "description": m.meta.get("description", ""),
            "target": label,
        }
        for m in rules.match(data=data, externals={"kind": kind})
    ]


MAX_PDF_STREAM_BYTES = 512 * 1024 * 1024  # budget for decompressing a PDF's streams


def _attachment_check(name: str, data: bytes, where: str) -> Dict[str, str]:
    verdict, mime = classify_embedded_file(data, name)
    return {"check": "pdf_attachment",
            "verdict": {"DANGEROUS": "MALICIOUS", "SAFE": "CLEAN"}.get(verdict, "SUSPICIOUS"),
            "detail": f"{where} {name} ({mime})"}


def _pdf_targets(path: Path) -> Tuple[List[Target], List[Dict[str, str]]]:
    """Decompressed object dictionaries, attachments and streams of a PDF.

    Attachments are taken from the document's EmbeddedFiles tree and from
    FileAttachment annotations. In addition every non-image stream is decompressed
    and scanned (kind "pdfstream", which only the executable-header and test-signature
    rules apply to), so a payload hidden in any other compressed stream is still seen.
    """
    import fitz

    targets: List[Target] = []
    checks: List[Dict[str, str]] = []
    try:
        doc = fitz.open(str(path))
    except Exception as exc:
        checks.append({"check": "pdf_parse", "verdict": "SUSPICIOUS", "detail": f"PDF could not be parsed: {exc}"})
        return targets, checks
    with doc:
        if doc.needs_pass:
            checks.append({"check": "pdf_encryption", "verdict": "SUSPICIOUS",
                           "detail": "PDF is password-protected; its contents cannot be inspected"})
            return targets, checks

        objects = []
        budget = MAX_PDF_STREAM_BYTES
        for xref in range(1, doc.xref_length()):
            try:
                source = doc.xref_object(xref, compressed=False)
            except Exception:
                continue
            objects.append(source)
            if not doc.xref_is_stream(xref) or "/Subtype /Image" in source or "/Subtype/Image" in source:
                continue
            try:
                data = doc.xref_stream(xref)
            except Exception:
                continue
            if not data:
                continue
            budget -= len(data)
            if budget < 0:
                checks.append({"check": "pdf_streams", "verdict": "SUSPICIOUS",
                               "detail": f"decompressed streams exceed {MAX_PDF_STREAM_BYTES} bytes"})
                break
            targets.append((f"stream:{xref}", "pdfstream", data))
        targets.append(("pdf-objects", "pdfobj", "\n".join(objects).encode("latin-1", errors="replace")))

        for i in range(doc.embfile_count()):
            name = doc.embfile_info(i).get("filename", f"embedded-{i}")
            data = doc.embfile_get(i)
            targets.append((f"embedded:{name}", "embedded", data))
            checks.append(_attachment_check(name, data, "embedded file"))
        for page in doc:
            for annot in page.annots(types=[fitz.PDF_ANNOT_FILE_ATTACHMENT]):
                try:
                    name = annot.file_info.get("filename", "attachment")
                    data = annot.get_file()
                except Exception as exc:
                    checks.append({"check": "pdf_attachment", "verdict": "SUSPICIOUS",
                                   "detail": f"unreadable attachment on page {page.number + 1}: {exc}"})
                    continue
                targets.append((f"annotation:{name}", "embedded", data))
                checks.append(_attachment_check(name, data, f"page {page.number + 1} attachment"))
    return targets, checks


def _ooxml_targets(path: Path) -> Tuple[List[Target], List[Dict[str, str]]]:
    """Structural checks and every archive member, from a single decompression pass."""
    checks: List[Dict[str, str]] = []
    report, members = inspect_with_members(str(path))
    if report["recommendation"] == "BLOCK":
        verdict = "MALICIOUS" if report["dangerous_embeds"] else "SUSPICIOUS"
        checks.append({"check": "ooxml_structure", "verdict": verdict, "detail": report["reason"]})
    elif report["recommendation"] == "SANITIZE":
        checks.append({"check": "ooxml_structure", "verdict": "CLEAN", "detail": report["reason"]})
    targets = [(f"member:{name}", "embedded" if "/embeddings/" in name.lower() else "ooxml-part", data)
               for name, data in members]
    return targets, checks


def scan_file(path, rules=None) -> Dict[str, Any]:
    """Scan one file and return the full result (never raises for detections)."""
    rules = rules or _rules()
    path = Path(path)
    size = path.stat().st_size
    result: Dict[str, Any] = {"file": str(path), "size": size, "sha256": None,
                              "verdict": "CLEAN", "rule_matches": [], "checks": []}
    if size > MAX_SCAN_BYTES:
        result["checks"].append({"check": "size", "verdict": "SUSPICIOUS",
                                 "detail": f"{size} bytes exceeds the {MAX_SCAN_BYTES}-byte scan limit"})
        result["verdict"] = "SUSPICIOUS"
        return result

    raw = path.read_bytes()
    result["sha256"] = hashlib.sha256(raw).hexdigest()
    targets: List[Target] = [("raw", "raw", raw)]
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        if b"%PDF-" not in raw[:1024]:
            result["checks"].append({"check": "file_type", "verdict": "SUSPICIOUS",
                                     "detail": ".pdf file does not contain a PDF header"})
        extra, checks = _pdf_targets(path)
        targets += extra
        result["checks"] += checks
    elif suffix in _OOXML_SUFFIXES:
        extra, checks = _ooxml_targets(path)
        targets += extra
        result["checks"] += checks

    seen = set()
    for target in targets:
        for match in _match(rules, target):
            if (match["rule"], match["target"]) not in seen:
                seen.add((match["rule"], match["target"]))
                result["rule_matches"].append(match)

    result["verdict"] = _worst([m["verdict"] for m in result["rule_matches"]] +
                               [c["verdict"] for c in result["checks"]])
    return result


def malware_gate(input_path, block_on: Tuple[str, ...] = DEFAULT_BLOCK_ON, rules=None) -> Dict[str, Any]:
    """Scan ``input_path``; raise ``MalwareDetectedError`` if its verdict is in ``block_on``.

    Raises ``GateUnavailableError`` if the scanner cannot run.
    """
    result = scan_file(input_path, rules)
    log.info("Pre-gate verdict for %s: %s", Path(input_path).name, result["verdict"])
    if result["verdict"] in block_on:
        raise MalwareDetectedError(result)
    return result
