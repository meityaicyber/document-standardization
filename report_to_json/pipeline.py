"""
The document standardisation pipeline. Every front-end calls ``DocumentPipeline.process_file``.

  0  Malware pre-gate on the file as received (fails closed)
  1  Normalise to PDF (DOCX converted after passing the gate, then re-scanned)
  2  Offload images to deep storage; each placement gets a pointer tag
  3  Pass 1: the model (Nemotron 3 Nano Omni) transcribes every page (images masked by their tags)
  4  Pass 2: the same model restructures the transcript into the master schema
  5  Enforce the master schema, check completeness, save

Outputs:
  <name>.json        exactly the structure of master_schema.json, nothing else
  <name>.meta.json   the sidecar: run status and reports, gate verdicts, coverage,
                     ``unmapped_content`` (transcript text no schema field holds) and
                     ``dropped_values`` (values the schema's types/lists cannot hold)

Run status (``status`` in the sidecar):
  ok            both passes ran on the model and every completeness check passed
  needs_review  model output, but a check failed (low page recall, low coverage,
                missing image tags, schema issues)
  fallback      the model pipeline failed somewhere and the text layer or the
                rule-based parser was used instead
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import __version__, config, coverage, schema
from .backends import BackendError, ModelBackend, create_backend
from .convert import ConversionError, docx_to_pdf
from .images import offload_images
from .security import GateUnavailableError, MalwareDetectedError, malware_gate
from .storage import DeepStorage, LocalDeepStorage, build_manifest
from .structurer import RuleBasedStructurer
from .transcribe import transcribe_pdf
from .vlm import Cancelled, Pass1Transcriber, Pass2Structurer

log = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = (".pdf", ".docx")

STAGES = [
    "Security pre-gate",
    "Normalising to PDF",
    "Offloading images to deep storage",
    "Pass 1: vision transcription",
    "Pass 2: schema structuring",
    "Completeness check and save",
]

ProgressFn = Callable[[int, str], None]
CancelFn = Callable[[], bool]


class PipelineError(Exception):
    """The document could not be processed."""


class PipelineCancelled(Exception):
    """Processing was cancelled by the caller."""


class DocumentBlocked(PipelineError):
    """The malware pre-gate blocked the document."""

    def __init__(self, scan_result: Dict[str, Any], message: str):
        super().__init__(message)
        self.scan_result = scan_result


def safe_stem(stem: str) -> str:
    """Filesystem-safe output name (Windows drops trailing spaces/dots from directory names)."""
    return re.sub(r"[^\w.\-]+", "_", stem).strip("._ ") or "document"


@dataclass
class PipelineResult:
    data: Dict[str, Any]   # exactly the master schema
    meta: Dict[str, Any]   # run report, unmapped_content, dropped_values (the sidecar)
    status: str
    json_path: Path
    meta_path: Path
    transcript_path: Path
    manifest_path: str
    elapsed_seconds: float
    warnings: List[str] = field(default_factory=list)


class DocumentPipeline:
    def __init__(self, model: str = config.DEFAULT_MODEL, enable_security_gate: bool = True,
                 storage: Optional[DeepStorage] = None, backend: Optional[ModelBackend] = None):
        self.profile = config.get_model(model)
        self.enable_security_gate = enable_security_gate
        self.storage = storage or LocalDeepStorage(config.deep_storage_dir())
        self._backend = backend

    def _get_backend(self) -> ModelBackend:
        if self._backend is None:
            self._backend = create_backend(self.profile.key)
        return self._backend

    def close(self) -> None:
        if self._backend is not None:
            self._backend.close()
            self._backend = None

    # ------------------------------------------------------------------ main entry

    def process_file(self, input_path, output_dir, progress: Optional[ProgressFn] = None,
                     cancel: Optional[CancelFn] = None) -> PipelineResult:
        started = _dt.datetime.now()
        src = Path(input_path)
        out_dir = Path(output_dir)
        warnings: List[str] = []

        def step(index: int, detail: str = "") -> None:
            if cancel and cancel():
                raise PipelineCancelled()
            log.info("[%d/%d] %s %s", index + 1, len(STAGES), STAGES[index], detail)
            if progress:
                progress(index, f"{STAGES[index]} - {detail}" if detail else STAGES[index])

        if not src.is_file():
            raise PipelineError(f"Input file not found: {src}")
        if src.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise PipelineError(f"Unsupported file type {src.suffix!r}; expected one of {SUPPORTED_SUFFIXES}")

        # 0. Nothing opens the file before the gate passes.
        step(0)
        gate = self._run_gate(src, warnings)

        stem = safe_stem(src.stem)
        out_dir.mkdir(parents=True, exist_ok=True)
        json_path = out_dir / f"{stem}.json"
        meta_path = out_dir / f"{stem}.meta.json"
        transcript_path = out_dir / f"{stem}_transcript.md"

        # 1. Normalise to PDF.
        step(1)
        converted_gate = None
        if src.suffix.lower() == ".docx":
            try:
                pdf = docx_to_pdf(src, out_dir / "converted")
            except ConversionError as exc:
                raise PipelineError(str(exc)) from exc
            converted_gate = self._run_gate(pdf, warnings)
        else:
            pdf = src

        # 2. Images to deep storage, replaced by pointer tags.
        step(2)
        document_id = f"{stem}-{(gate.get('sha256') or _sha256(src))[:12]}"
        offload = offload_images(str(pdf), self.storage)
        images = offload.figures
        manifest_path = self.storage.write_manifest(
            document_id, build_manifest(document_id, src.name, offload.all_figures(), offload.layout))

        # 3-4. The two model passes (or the recorded fallback).
        model_meta: Dict[str, Any] = {"key": self.profile.key, "repo_id": self.profile.repo_id}
        try:
            backend = self._get_backend()
            backend.check()
            model_meta["backend"] = backend.description
        except BackendError as exc:
            backend = None
            model_meta["unavailable"] = str(exc)
            warnings.append(f"MODEL PIPELINE FAILED - {exc}. Output comes from the rule-based fallback.")

        try:
            if backend is not None:
                step(3)
                transcript, page_reports = Pass1Transcriber(backend).transcribe(
                    str(pdf), images, detail=lambda d: step(3, d), cancel=cancel)
            else:
                step(3, "fallback: text layer")
                transcript, page_reports = transcribe_pdf(str(pdf), images), []
            transcript_path.write_text(transcript, encoding="utf-8")

            if backend is not None:
                step(4)
                data, section_reports = Pass2Structurer(backend).structure(
                    transcript, detail=lambda d: step(4, d), cancel=cancel)
            else:
                step(4, "fallback: rule-based parser")
                data, section_reports = RuleBasedStructurer().structure(transcript), []
        except Cancelled as exc:
            raise PipelineCancelled() from exc

        # 5. Exactly the master schema in the output; everything else in the sidecar.
        step(5)
        data, dropped = schema.enforce(data)
        errors = schema.validate(data)  # always empty after enforce; kept as a guard
        cov = coverage.transcript_coverage(transcript, data)
        unmapped = coverage.unmapped_entries(cov.uncovered)
        status, reasons = _status(backend is not None, page_reports, section_reports, cov.ratio, errors, dropped)
        warnings += reasons

        elapsed = (_dt.datetime.now() - started).total_seconds()
        meta = {
            "status": status,
            "status_reasons": reasons,
            "pipeline_version": __version__,
            "source_file": src.name,
            "source_sha256": gate.get("sha256"),
            "document_id": document_id,
            "image_manifest": manifest_path,
            "processed_at": started.isoformat(timespec="seconds"),
            "elapsed_seconds": round(elapsed, 2),
            "model": model_meta,
            "security_gate": _gate_summary(gate),
            "security_gate_converted_pdf": _gate_summary(converted_gate) if converted_gate else None,
            "pass1_pages": [asdict(r) for r in page_reports],
            "pass2_sections": [asdict(r) for r in section_reports],
            "coverage": {"placed_in_schema": round(cov.ratio, 4),
                         "unmapped_entries": len(unmapped),
                         "structural_units_skipped": cov.structural_units,
                         "threshold": config.coverage_threshold()},
            "validation_errors": errors,
            "missing_fields": schema.missing_fields(data),
            "dropped_values": dropped,
            "unmapped_content": unmapped,
        }
        json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("Saved %s: status=%s, %d findings, coverage %.1f%%, %.1fs", json_path, status,
                 len(data["detailed_observations"]), cov.ratio * 100, elapsed)
        return PipelineResult(data, meta, status, json_path, meta_path, transcript_path, manifest_path,
                              elapsed, warnings)

    # ------------------------------------------------------------------ helpers

    def _run_gate(self, path: Path, warnings: List[str]) -> Dict[str, Any]:
        if not self.enable_security_gate:
            warnings.append(f"Malware pre-gate was disabled for {path.name}")
            return {"verdict": "NOT_SCANNED", "rule_matches": [], "checks": [], "sha256": None}
        try:
            return malware_gate(str(path))
        except MalwareDetectedError as exc:
            raise DocumentBlocked(exc.scan_result, str(exc)) from exc
        except GateUnavailableError as exc:
            raise PipelineError(f"Malware pre-gate unavailable, refusing to process: {exc}") from exc


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _gate_summary(gate: Dict[str, Any]) -> Dict[str, Any]:
    return {k: gate.get(k) for k in ("verdict", "sha256", "rule_matches", "checks")}


def _status(model_ran: bool, pages, sections, coverage_ratio: float, errors: List[str],
            dropped: Optional[List[Dict[str, Any]]] = None):
    reasons: List[str] = []
    if not model_ran:
        return "fallback", ["model unavailable: whole document produced by the rule-based fallback"]
    fallback_pages = [p.page for p in pages if p.source != "model"]
    fallback_sections = [s.name for s in sections if s.source != "model"]
    if fallback_pages:
        reasons.append(f"pass 1 fell back to the text layer on page(s) {fallback_pages}")
    if fallback_sections:
        reasons.append(f"pass 2 fell back for: {fallback_sections}")
    if reasons:
        return "fallback", reasons

    threshold = config.page_recall_threshold()
    low = [p.page for p in pages if p.recall is not None and p.recall < threshold]
    tag_issues = [p.page for p in pages if any("tag" in i for i in p.issues)]
    if low:
        reasons.append(f"page recall below {threshold} on page(s) {low}")
    if tag_issues:
        reasons.append(f"image tags repaired on page(s) {tag_issues}")
    if coverage_ratio < config.coverage_threshold():
        reasons.append(f"only {coverage_ratio:.1%} of transcript text placed in schema fields "
                       f"(rest kept in unmapped_content)")
    if errors:
        reasons.append(f"{len(errors)} schema validation issue(s)")
    if dropped:
        reasons.append(f"{len(dropped)} value(s) the master schema cannot hold were set to null "
                       f"(originals in dropped_values)")
    return ("needs_review" if reasons else "ok"), reasons
