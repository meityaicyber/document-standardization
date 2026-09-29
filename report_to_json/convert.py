"""
DOCX -> PDF normalisation.

Every document enters the pipeline as a PDF. Word files are converted only
**after** they pass the malware pre-gate, in a child process with a timeout, and
the resulting PDF is scanned again.

Converters (``DOCX_CONVERTER`` = auto | word | libreoffice):
  * word         Microsoft Word via COM (Windows, needs pywin32). Opened read-only in
                 a private instance with macros force-disabled and link updates off.
  * libreoffice  ``soffice --headless`` with a throw-away user profile.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 180

# Word constants
_WD_ALERTS_NONE = 0
_MSO_AUTOMATION_SECURITY_FORCE_DISABLE = 3
_WD_EXPORT_FORMAT_PDF = 17
_WD_DO_NOT_SAVE_CHANGES = 0


class ConversionError(RuntimeError):
    pass


def _soffice() -> Optional[str]:
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    for candidate in (r"C:\Program Files\LibreOffice\program\soffice.exe",
                      r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
                      "/usr/bin/soffice", "/opt/libreoffice/program/soffice",
                      "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if Path(candidate).exists():
            return candidate
    return None


def _word_available() -> bool:
    if sys.platform != "win32":
        return False
    import importlib.util

    return importlib.util.find_spec("win32com") is not None


def available_converter() -> Optional[str]:
    choice = os.environ.get("DOCX_CONVERTER", "auto").lower()
    if choice in ("word", "auto") and _word_available():
        return "word"
    if choice in ("libreoffice", "auto") and _soffice():
        return "libreoffice"
    return None


def docx_to_pdf(docx_path: Path, out_dir: Path) -> Path:
    """Convert ``docx_path`` to ``out_dir/<stem>.pdf`` and return the PDF path."""
    converter = available_converter()
    if converter is None:
        raise ConversionError("No DOCX converter available: install Microsoft Word with pywin32, or LibreOffice")
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{docx_path.stem}.pdf"
    # A PDF left by an earlier run must never be mistaken for this conversion's output.
    target.unlink(missing_ok=True)
    log.info("Converting %s to PDF with %s", docx_path.name, converter)

    if converter == "word":
        cmd = [sys.executable, "-m", "report_to_json.convert", str(docx_path.resolve()), str(target.resolve())]
        _run(cmd, cwd=str(Path(__file__).resolve().parent.parent))  # where the package is importable
    else:
        with tempfile.TemporaryDirectory() as profile:
            cmd = [_soffice(), f"-env:UserInstallation={Path(profile).as_uri()}", "--headless", "--norestore",
                   "--nolockcheck", "--convert-to", "pdf", "--outdir", str(out_dir.resolve()), str(docx_path.resolve())]
            _run(cmd, cwd=profile)
    if not target.exists() or target.stat().st_size == 0:
        raise ConversionError(f"{converter} produced no PDF for {docx_path.name}")
    return target


def _run(cmd, cwd) -> None:
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise ConversionError(f"Conversion timed out after {TIMEOUT_SECONDS}s") from exc
    if proc.returncode != 0:
        raise ConversionError(f"Conversion failed ({proc.returncode}): {(proc.stderr or proc.stdout).strip()[:500]}")


def _word_convert(src: str, dst: str) -> None:
    """Runs in the child process started by ``docx_to_pdf``."""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    word = win32com.client.DispatchEx("Word.Application")  # private instance, not the user's Word
    # Word.Options are the user's persistent settings: change them only for this conversion.
    saved_options = {}
    try:
        word.Visible = False
        word.DisplayAlerts = _WD_ALERTS_NONE
        word.AutomationSecurity = _MSO_AUTOMATION_SECURITY_FORCE_DISABLE  # per instance, not persisted
        for option in ("UpdateLinksAtOpen", "ConfirmConversions"):
            saved_options[option] = getattr(word.Options, option)
            setattr(word.Options, option, False)
        doc = word.Documents.Open(src, ConfirmConversions=False, ReadOnly=True, AddToRecentFiles=False,
                                  Visible=False, OpenAndRepair=False, NoEncodingDialog=True)
        try:
            doc.ExportAsFixedFormat(OutputFileName=dst, ExportFormat=_WD_EXPORT_FORMAT_PDF,
                                    OpenAfterExport=False, IncludeDocProps=False, DocStructureTags=True,
                                    BitmapMissingFonts=True)
        finally:
            doc.Close(SaveChanges=_WD_DO_NOT_SAVE_CHANGES)
    finally:
        for option, value in saved_options.items():
            try:
                setattr(word.Options, option, value)
            except Exception:
                pass
        word.Quit()
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    _word_convert(sys.argv[1], sys.argv[2])
