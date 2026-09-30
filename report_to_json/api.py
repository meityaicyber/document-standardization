"""
Integration entry points.

    from report_to_json import standardize_report

    data = standardize_report("path/to/report.pdf")   # dict shaped exactly like master_schema.json

``standardize_report`` returns only the JSON, so it refuses to hand back a result
produced by the fallback: a caller could not tell it from a model result. Use
``process_report`` when you want the run status and report alongside the JSON, or
pass ``allow_fallback=True`` to accept fallback output knowingly.

The pipeline (and with it the model) is created once per process and reused.
Calls are serialised: one document is processed at a time.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Union

from . import config
from .pipeline import DocumentPipeline, PipelineError, PipelineResult

PathLike = Union[str, "os.PathLike[str]"]

_lock = threading.Lock()
_pipeline: Optional[DocumentPipeline] = None


class ModelPipelineFailed(PipelineError):
    """The model could not produce the result and the fallback was used instead.

    ``result`` holds what the fallback produced (``result.data``) and why
    (``result.meta["status_reasons"]``); the output files were written.
    """

    def __init__(self, result: PipelineResult):
        self.result = result
        reasons = "; ".join(result.meta.get("status_reasons") or result.warnings) or "no reason recorded"
        super().__init__(f"Model pipeline failed, fallback output only: {reasons}")


def process_report(report_path: PathLike, output_dir: Optional[PathLike] = None, *,
                   enable_security_gate: bool = True) -> PipelineResult:
    """Run the full pipeline on one report and return the complete result.

    ``result.data`` is the master-schema JSON, ``result.status`` is "ok",
    "needs_review" or "fallback", ``result.meta`` is the run report, and
    ``result.json_path`` / ``result.meta_path`` are the files written.

    Raises ``DocumentBlocked`` if the malware pre-gate blocks the file and
    ``PipelineError`` for anything else that prevents processing.
    """
    global _pipeline
    out_dir = Path(output_dir) if output_dir is not None else config.DEFAULT_OUTPUT_DIR
    with _lock:
        if _pipeline is None:
            _pipeline = DocumentPipeline()
        _pipeline.enable_security_gate = enable_security_gate
        return _pipeline.process_file(Path(report_path), out_dir)


def standardize_report(report_path: PathLike, output_dir: Optional[PathLike] = None, *,
                       allow_fallback: bool = False, enable_security_gate: bool = True) -> Dict[str, Any]:
    """Convert one audit report (PDF or DOCX) into the standardised JSON.

    Parameters
    ----------
    report_path : path to the report.
    output_dir : where ``<name>.json``, ``<name>.meta.json`` and the transcript are
        written. Defaults to the project's ``outputs/`` folder.
    allow_fallback : return the fallback's JSON instead of raising when the model
        pipeline fails.
    enable_security_gate : run the malware pre-gate (leave on for untrusted files).

    Returns
    -------
    dict with exactly the structure of ``master_schema.json``. Fields the report does
    not state are ``None``.

    Raises
    ------
    ModelPipelineFailed : the model was unavailable or failed and ``allow_fallback`` is False.
    DocumentBlocked : the malware pre-gate blocked the file.
    PipelineError : the file is missing, of an unsupported type, or could not be processed.
    """
    result = process_report(report_path, output_dir, enable_security_gate=enable_security_gate)
    if result.status == "fallback" and not allow_fallback:
        raise ModelPipelineFailed(result)
    return result.data


def close() -> None:
    """Release the model held by this process (the next call sets it up again)."""
    global _pipeline
    with _lock:
        if _pipeline is not None:
            _pipeline.close()
            _pipeline = None
