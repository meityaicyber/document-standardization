"""
Desktop GUI (Tkinter) for the Report-to-JSON pipeline.

All processing goes through ``DocumentPipeline.process_file``; this module only
collects options, runs the pipeline on a worker thread and renders results.
The worker never touches Tk widgets: it posts events to a queue that the UI
thread drains with ``after``.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, Optional

from . import __version__, config
from .pipeline import STAGES, DocumentBlocked, DocumentPipeline, PipelineCancelled, PipelineError

# -----------------------------------------------------------------------------
# Theme
# -----------------------------------------------------------------------------
BG_ROOT = "#0b0f19"
BG_CARD = "#151b28"
BG_CARD_LIGHT = "#1c2436"
BG_INPUT = "#0f1422"
BG_CODE = "#080c14"
BORDER = "#232e42"

ACCENT = "#6366f1"
ACCENT_HOVER = "#4f46e5"
ACCENT_LIGHT = "#818cf8"
CYAN = "#06b6d4"
CYAN_LIGHT = "#22d3ee"
EMERALD = "#10b981"
AMBER = "#f59e0b"
ROSE = "#f43f5e"
PURPLE = "#a855f7"

TEXT_MAIN = "#f1f5f9"
TEXT_MUTED = "#94a3b8"
TEXT_DIM = "#64748b"

FONT_FAMILY = "Segoe UI" if sys.platform == "win32" else "Helvetica"
FONT_MONO = "Consolas" if sys.platform == "win32" else "Courier"

MODEL_KEYS = list(config.MODELS)  # combobox order
STATUS_STYLE = {  # sidecar "status" -> (banner text, colour)
    "ok": ("Complete", EMERALD),
    "needs_review": ("Needs review", AMBER),
    "fallback": ("MODEL PIPELINE FAILED - fallback output", ROSE),
}


def open_path(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - opening a local output file the user asked for
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])


class _QueueLogHandler(logging.Handler):
    """Forwards pipeline log records to the UI event queue."""

    def __init__(self, events: "queue.Queue"):
        super().__init__(logging.INFO)
        self.events = events
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        tag = "error" if record.levelno >= logging.ERROR else ("warn" if record.levelno >= logging.WARNING else "")
        self.events.put(("log", self.format(record), tag))


# -----------------------------------------------------------------------------
# Loading overlay
# -----------------------------------------------------------------------------
class LoadingOverlay(tk.Frame):
    def __init__(self, parent, on_cancel=None, **kwargs):
        super().__init__(parent, bg=BG_CARD, **kwargs)
        self.on_cancel = on_cancel
        self._angle = 0
        self._pulse_r = 18.0
        self._pulse_dir = 1
        self._is_animating = False
        self._start_time = 0.0
        self._step_widgets = []
        self._build_ui()

    def _build_ui(self):
        container = tk.Frame(self, bg=BG_CARD)
        container.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.88, relheight=0.92)

        tk.Label(container, text="PROCESSING REPORT", font=(FONT_FAMILY, 15, "bold"),
                 bg=BG_CARD, fg=CYAN_LIGHT).pack(pady=(10, 4))
        tk.Label(container, text="Extracting structure and standardising to the master schema...",
                 font=(FONT_FAMILY, 10), bg=BG_CARD, fg=TEXT_MUTED).pack(pady=(0, 10))

        self.canvas = tk.Canvas(container, width=140, height=140, bg=BG_CARD, highlightthickness=0)
        self.canvas.pack(pady=10)

        self.timer_lbl = tk.Label(container, text="Elapsed: 00:00.0", font=(FONT_MONO, 11, "bold"),
                                  bg=BG_CARD, fg=TEXT_MAIN)
        self.timer_lbl.pack(pady=(4, 0))
        self.status_detail_lbl = tk.Label(container, text="Starting...", font=(FONT_FAMILY, 10, "italic"),
                                          bg=BG_CARD, fg=CYAN)
        self.status_detail_lbl.pack(pady=(4, 10))

        stepper = tk.Frame(container, bg=BG_INPUT, highlightbackground=BORDER, highlightthickness=1)
        stepper.pack(fill="both", expand=True, padx=20, pady=10)
        tk.Label(stepper, text="PIPELINE STAGES", font=(FONT_FAMILY, 9, "bold"),
                 bg=BG_INPUT, fg=TEXT_MUTED).pack(anchor="w", padx=14, pady=(10, 6))
        self.pbar = ttk.Progressbar(stepper, orient="horizontal", mode="determinate", maximum=len(STAGES))
        self.pbar.pack(fill="x", padx=14, pady=(0, 12))

        rows = tk.Frame(stepper, bg=BG_INPUT)
        rows.pack(fill="both", expand=True, padx=14, pady=(0, 10))
        for name in STAGES:
            row = tk.Frame(rows, bg=BG_INPUT)
            row.pack(fill="x", pady=4)
            icon = tk.Label(row, text="-", font=(FONT_FAMILY, 10, "bold"), bg=BG_INPUT, fg=TEXT_DIM, width=3)
            icon.pack(side="left")
            label = tk.Label(row, text=name, font=(FONT_FAMILY, 10), bg=BG_INPUT, fg=TEXT_DIM, anchor="w")
            label.pack(side="left", fill="x", expand=True)
            pill = tk.Label(row, text="Waiting", font=(FONT_FAMILY, 8), bg=BG_CARD, fg=TEXT_DIM, padx=6, pady=1)
            pill.pack(side="right")
            self._step_widgets.append((icon, label, pill))

        if self.on_cancel:
            self.cancel_btn = tk.Button(
                container, text="Cancel Processing", command=self.on_cancel, font=(FONT_FAMILY, 9, "bold"),
                bg="#261b24", fg=ROSE, activebackground=ROSE, activeforeground="white",
                relief="flat", bd=0, padx=16, pady=6, cursor="hand2")
            self.cancel_btn.pack(pady=(10, 5))

    def start(self):
        self._is_animating = True
        self._start_time = time.time()
        self.pbar["value"] = 0
        self.status_detail_lbl.configure(text="Starting...")
        if self.on_cancel:
            self.cancel_btn.configure(state="normal", text="Cancel Processing")
        self.set_stage(-1)
        self._tick()

    def stop(self):
        self._is_animating = False

    def set_stage(self, index: int, detail: str = ""):
        self.pbar["value"] = max(index, 0)
        if detail:
            self.status_detail_lbl.configure(text=detail)
        for i, (icon, label, pill) in enumerate(self._step_widgets):
            if i < index:
                icon.configure(text="OK", fg=EMERALD)
                label.configure(fg=TEXT_MAIN, font=(FONT_FAMILY, 10))
                pill.configure(text="Done", bg="#0f2b20", fg=EMERALD)
            elif i == index:
                icon.configure(text=">", fg=CYAN)
                label.configure(fg=CYAN_LIGHT, font=(FONT_FAMILY, 10, "bold"))
                pill.configure(text="Running", bg="#0e2a38", fg=CYAN_LIGHT)
            else:
                icon.configure(text="-", fg=TEXT_DIM)
                label.configure(fg=TEXT_DIM, font=(FONT_FAMILY, 10))
                pill.configure(text="Waiting", bg=BG_CARD, fg=TEXT_DIM)

    def _tick(self):
        if not self._is_animating:
            return
        elapsed = time.time() - self._start_time
        self.timer_lbl.configure(text=f"Elapsed: {int(elapsed // 60):02d}:{elapsed % 60:04.1f}")

        c = self.canvas
        c.delete("all")
        cx = cy = 70
        self._angle = (self._angle + 6) % 360
        c.create_arc(22, 22, 118, 118, start=self._angle, extent=100, outline=CYAN, width=3, style="arc")
        c.create_arc(22, 22, 118, 118, start=(self._angle + 180) % 360, extent=60,
                     outline=ACCENT_LIGHT, width=3, style="arc")
        c.create_arc(36, 36, 104, 104, start=360 - (self._angle * 1.5) % 360, extent=120,
                     outline=PURPLE, width=2.5, style="arc")
        self._pulse_r += 0.4 * self._pulse_dir
        if not 14 < self._pulse_r < 20:
            self._pulse_dir *= -1
        r = self._pulse_r
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill="#14233c", outline=CYAN_LIGHT, width=1.5)
        c.create_text(cx, cy, text="R2J", font=(FONT_FAMILY, 9, "bold"), fill=TEXT_MAIN)
        self.after(33, self._tick)


# -----------------------------------------------------------------------------
# JSON highlighting
# -----------------------------------------------------------------------------
_JSON_PATTERNS = [
    ("key", re.compile(r'"([^"\\]*(?:\\.[^"\\]*)*)"\s*:')),
    ("str", re.compile(r':\s*"([^"\\]*(?:\\.[^"\\]*)*)"')),
    ("num", re.compile(r':\s*(-?\d+\.?\d*(?:[eE][+-]?\d+)?)')),
    ("bool", re.compile(r':\s*(true|false|null)')),
]


def highlight_json(widget: tk.Text, text: str) -> None:
    for tag, _ in _JSON_PATTERNS:
        widget.tag_remove(tag, "1.0", "end")
    for tag, pattern in _JSON_PATTERNS:
        for m in pattern.finditer(text):
            widget.tag_add(tag, f"1.0+{m.start(1)}c", f"1.0+{m.end(1)}c")


# -----------------------------------------------------------------------------
# Main window
# -----------------------------------------------------------------------------
class ReportToJsonApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Report-to-JSON {__version__}")
        self.geometry("1280x900")
        self.minsize(1050, 720)
        self.configure(bg=BG_ROOT)

        self._selected_file = tk.StringVar()
        self._output_dir = tk.StringVar(value=str(config.DEFAULT_OUTPUT_DIR))
        self._model_status = tk.StringVar()
        self._enable_gate = tk.BooleanVar(value=True)
        self._search_var = tk.StringVar()

        self._events: "queue.Queue" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._pipelines: Dict[tuple, DocumentPipeline] = {}
        self._result = None

        self._log_handler = _QueueLogHandler(self._events)
        logging.getLogger("report_to_json").addHandler(self._log_handler)
        logging.getLogger("report_to_json").setLevel(logging.INFO)

        self._configure_styles()
        self._build_layout()
        self._populate_samples()
        self._on_engine_selected()
        self.after(100, self._drain_events)

        self.lift()
        self.attributes("-topmost", True)
        self.after_idle(self.attributes, "-topmost", False)
        self.focus_force()

    # ------------------------------------------------------------------ layout

    def _configure_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TProgressbar", troughcolor=BG_CARD, background=ACCENT, bordercolor=BORDER,
                        lightcolor=CYAN, darkcolor=ACCENT)
        style.configure("TNotebook", background=BG_CARD, borderwidth=0, tabmargins=[0, 0, 0, 0])
        style.configure("TNotebook.Tab", background=BG_CARD, foreground=TEXT_MUTED, padding=[16, 8],
                        font=(FONT_FAMILY, 9, "bold"), borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", BG_INPUT)], foreground=[("selected", CYAN_LIGHT)])
        for orient in ("Vertical", "Horizontal"):
            style.configure(f"{orient}.TScrollbar", background=BG_CARD, troughcolor=BG_ROOT,
                            bordercolor=BORDER, arrowcolor=TEXT_MUTED)

    def _build_layout(self):
        header = tk.Frame(self, bg=BG_CARD, height=64)
        header.pack(fill="x")
        header.pack_propagate(False)
        brand = tk.Frame(header, bg=BG_CARD)
        brand.pack(side="left", padx=20, pady=12)
        tk.Label(brand, text="REPORT-TO-JSON", font=(FONT_FAMILY, 15, "bold"), bg=BG_CARD, fg=TEXT_MAIN).pack(side="left")
        tk.Label(brand, text=f"v{__version__}", font=(FONT_FAMILY, 8, "bold"), bg="#1e1b4b", fg=ACCENT_LIGHT,
                 padx=8, pady=2).pack(side="left", padx=(10, 0))
        tk.Label(brand, text="Security audit report standardiser", font=(FONT_FAMILY, 9),
                 bg=BG_CARD, fg=TEXT_MUTED).pack(side="left", padx=(14, 0))
        self.header_status = tk.Label(header, text="Ready", font=(FONT_FAMILY, 9, "bold"), bg=BG_CARD, fg=EMERALD)
        self.header_status.pack(side="right", padx=20)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        workspace = tk.Frame(self, bg=BG_ROOT)
        workspace.pack(fill="both", expand=True)
        sidebar = tk.Frame(workspace, bg=BG_ROOT, width=420)
        sidebar.pack(side="left", fill="y", padx=(14, 8), pady=14)
        sidebar.pack_propagate(False)
        self.right_panel = tk.Frame(workspace, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1)
        self.right_panel.pack(side="left", fill="both", expand=True, padx=(0, 14), pady=14)

        self._build_sidebar(sidebar)
        self._build_right_panel()

    def _card(self, parent, title):
        card = tk.Frame(parent, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1)
        tk.Label(card, text=title, font=(FONT_FAMILY, 9, "bold"), bg=BG_CARD, fg=TEXT_MUTED).pack(
            anchor="w", padx=12, pady=(8, 6))
        tk.Frame(card, bg=BORDER, height=1).pack(fill="x")
        return card

    def _build_sidebar(self, parent):
        c1 = self._card(parent, "1. Input report (PDF / DOCX)")
        c1.pack(fill="x", pady=(0, 12))
        self.dropzone = tk.Frame(c1, bg=BG_INPUT, highlightbackground=BORDER, highlightthickness=1, cursor="hand2")
        self.dropzone.pack(fill="x", padx=12, pady=(8, 8))
        self.drop_icon = tk.Label(self.dropzone, text="[DOC]", font=(FONT_MONO, 12, "bold"), bg=BG_INPUT, fg=CYAN)
        self.drop_icon.pack(pady=(8, 2))
        self.drop_text = tk.Label(self.dropzone, text="Click to select a report", font=(FONT_FAMILY, 10, "bold"),
                                  bg=BG_INPUT, fg=TEXT_MAIN)
        self.drop_text.pack()
        self.file_details_lbl = tk.Label(self.dropzone, text="VAPT, audit and compliance reports",
                                         font=(FONT_FAMILY, 8), bg=BG_INPUT, fg=TEXT_MUTED)
        self.file_details_lbl.pack(pady=(2, 10))
        for w in (self.dropzone, self.drop_icon, self.drop_text, self.file_details_lbl):
            w.bind("<Button-1>", lambda e: self._browse_file())

        sample_row = tk.Frame(c1, bg=BG_CARD)
        sample_row.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(sample_row, text="Local reports:", font=(FONT_FAMILY, 8, "bold"), bg=BG_CARD, fg=TEXT_MUTED).pack(side="left")
        self.sample_combo = ttk.Combobox(sample_row, state="readonly", font=(FONT_FAMILY, 8))
        self.sample_combo.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.sample_combo.bind("<<ComboboxSelected>>", self._on_sample_selected)

        c2 = self._card(parent, "2. Model (runs both passes)")
        c2.pack(fill="x", pady=(0, 12))
        self.engine_combo = ttk.Combobox(c2, state="readonly", font=(FONT_FAMILY, 9),
                                         values=[config.MODELS[k].label for k in MODEL_KEYS])
        self.engine_combo.pack(fill="x", padx=12, pady=(8, 4))
        self.engine_combo.current(MODEL_KEYS.index(config.DEFAULT_MODEL))
        self.engine_combo.bind("<<ComboboxSelected>>", self._on_engine_selected)
        self.model_status_lbl = tk.Label(c2, textvariable=self._model_status, font=(FONT_FAMILY, 8, "bold"),
                                         bg=BG_CARD, fg=EMERALD, anchor="w", justify="left", wraplength=380)
        self.model_status_lbl.pack(fill="x", padx=12, pady=(0, 6))
        opts = tk.Frame(c2, bg=BG_CARD)
        opts.pack(fill="x", padx=12, pady=(0, 8))
        tk.Checkbutton(opts, text="Malware pre-gate (YARA)", variable=self._enable_gate, bg=BG_CARD, fg=TEXT_MUTED,
                       selectcolor=BG_ROOT, activebackground=BG_CARD, activeforeground=CYAN,
                       font=(FONT_FAMILY, 8)).pack(side="left")

        c3 = self._card(parent, "3. Output folder")
        c3.pack(fill="x", pady=(0, 14))
        out_row = tk.Frame(c3, bg=BG_CARD)
        out_row.pack(fill="x", padx=12, pady=8)
        tk.Entry(out_row, textvariable=self._output_dir, bg=BG_INPUT, fg=TEXT_MAIN, insertbackground=TEXT_MAIN,
                 font=(FONT_MONO, 8), relief="flat", bd=4).pack(side="left", fill="x", expand=True)
        tk.Button(out_row, text="...", command=self._browse_output_dir, bg=BORDER, fg=TEXT_MAIN,
                  font=(FONT_FAMILY, 8, "bold"), relief="flat", bd=0, padx=6, cursor="hand2").pack(side="left", padx=(4, 0))

        self.process_btn = tk.Button(parent, text="PROCESS REPORT", command=self._start_processing,
                                     font=(FONT_FAMILY, 11, "bold"), bg=ACCENT, fg="white",
                                     activebackground=ACCENT_HOVER, activeforeground="white",
                                     relief="flat", bd=0, pady=13, cursor="hand2")
        self.process_btn.pack(fill="x")

    def _build_right_panel(self):
        container = tk.Frame(self.right_panel, bg=BG_CARD)
        container.pack(fill="both", expand=True)
        self.overlay = LoadingOverlay(container, on_cancel=self._cancel_processing)
        self.results = tk.Frame(container, bg=BG_CARD)
        self.results.pack(fill="both", expand=True)

        kpi = tk.Frame(self.results, bg=BG_INPUT, highlightbackground=BORDER, highlightthickness=1)
        kpi.pack(fill="x", padx=12, pady=(12, 6))
        # Right-hand widgets are packed first so a long title shrinks instead of hiding them.
        self.kpi_findings = tk.Label(kpi, text="0 findings", font=(FONT_FAMILY, 9, "bold"), bg="#1e1b4b",
                                     fg=ACCENT_LIGHT, padx=8, pady=2)
        self.kpi_findings.pack(side="right", padx=12)
        self.kpi_severity = tk.Label(kpi, text="", font=(FONT_FAMILY, 9), bg=BG_INPUT, fg=TEXT_MUTED)
        self.kpi_severity.pack(side="right", padx=10)
        self.kpi_title = tk.Label(kpi, text="No document processed", font=(FONT_FAMILY, 10, "bold"),
                                  bg=BG_INPUT, fg=TEXT_MAIN, anchor="w")
        self.kpi_title.pack(side="left", fill="x", expand=True, padx=12, pady=8)

        self.quality_lbl = tk.Label(self.results, text="", font=(FONT_FAMILY, 8), bg=BG_CARD, fg=AMBER,
                                    anchor="w", justify="left", wraplength=760)
        self.quality_lbl.pack(fill="x", padx=14)

        self.notebook = ttk.Notebook(self.results)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(4, 10))
        self.tab_json = tk.Frame(self.notebook, bg=BG_CARD)
        self.tab_findings = tk.Frame(self.notebook, bg=BG_CARD)
        self.tab_md = tk.Frame(self.notebook, bg=BG_CARD)
        self.tab_log = tk.Frame(self.notebook, bg=BG_CARD)
        for tab, text in ((self.tab_json, "Standardized JSON"), (self.tab_findings, "Findings"),
                          (self.tab_md, "Transcript"), (self.tab_log, "Log")):
            self.notebook.add(tab, text=f"  {text}  ")
        self._build_json_tab()
        self._build_findings_tab()
        self.md_text = self._text_area(self.tab_md, wrap="word")
        self._build_log_tab()

        actions = tk.Frame(self.results, bg=BG_CARD)
        actions.pack(fill="x", padx=12, pady=(0, 10))
        self.result_buttons = []
        for text, cmd in (("Open JSON", lambda: self._open_result("json_path")),
                          ("Run report", lambda: self._open_result("meta_path")),
                          ("Open output folder", lambda: self._open_result("output_dir")),
                          ("Image manifest", lambda: self._open_result("manifest"))):
            b = tk.Button(actions, text=text, command=cmd, bg=BG_CARD_LIGHT, fg=TEXT_MUTED,
                          font=(FONT_FAMILY, 8, "bold"), relief="flat", bd=0, padx=12, pady=6,
                          cursor="hand2", state="disabled")
            b.pack(side="left", padx=(0, 6))
            self.result_buttons.append(b)
        tk.Button(actions, text="Copy JSON", command=self._copy_json, bg=CYAN, fg=BG_ROOT,
                  font=(FONT_FAMILY, 8, "bold"), relief="flat", bd=0, padx=14, pady=6, cursor="hand2").pack(side="right")

    def _text_area(self, parent, wrap="none", font_size=9) -> tk.Text:
        frame = tk.Frame(parent, bg=BG_CODE)
        frame.pack(fill="both", expand=True, padx=6, pady=6)
        text = tk.Text(frame, bg=BG_CODE, fg="#c9d1d9", font=(FONT_MONO, font_size), relief="flat", bd=0,
                       state="disabled", wrap=wrap)
        vs = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        if wrap == "none":
            hs = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
            text.configure(xscrollcommand=hs.set)
            hs.pack(side="bottom", fill="x")
        text.pack(fill="both", expand=True, padx=6, pady=6)
        return text

    def _build_json_tab(self):
        bar = tk.Frame(self.tab_json, bg=BG_CARD)
        bar.pack(fill="x", padx=6, pady=(6, 0))
        tk.Label(bar, text="Search:", font=(FONT_FAMILY, 8), bg=BG_CARD, fg=TEXT_MUTED).pack(side="left")
        entry = tk.Entry(bar, textvariable=self._search_var, bg=BG_INPUT, fg=TEXT_MAIN, insertbackground=TEXT_MAIN,
                         font=(FONT_MONO, 8), relief="flat", bd=3, width=24)
        entry.pack(side="left", padx=6)
        entry.bind("<Return>", lambda e: self._search_json())
        tk.Button(bar, text="Find", command=self._search_json, bg=BORDER, fg=TEXT_MAIN, font=(FONT_FAMILY, 8),
                  relief="flat", bd=0, padx=8, cursor="hand2").pack(side="left")
        tk.Button(bar, text="Save JSON As...", command=self._save_json_as, bg=BORDER, fg=TEXT_MAIN,
                  font=(FONT_FAMILY, 8), relief="flat", bd=0, padx=10, cursor="hand2").pack(side="right")
        self.json_text = self._text_area(self.tab_json)
        for tag, colour in (("key", CYAN_LIGHT), ("str", "#a5d6ff"), ("num", "#ffa657"), ("bool", "#ff7b72")):
            self.json_text.tag_configure(tag, foreground=colour)
        self.json_text.tag_configure("search_match", background="#e3b341", foreground="#000000")

    def _build_findings_tab(self):
        wrap = tk.Frame(self.tab_findings, bg=BG_CARD)
        wrap.pack(fill="both", expand=True, padx=6, pady=6)
        self.findings_canvas = tk.Canvas(wrap, bg=BG_CARD, highlightthickness=0)
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.findings_canvas.yview)
        self.findings_canvas.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        self.findings_canvas.pack(side="left", fill="both", expand=True)
        self.findings_frame = tk.Frame(self.findings_canvas, bg=BG_CARD)
        window = self.findings_canvas.create_window((0, 0), window=self.findings_frame, anchor="nw")
        self.findings_frame.bind("<Configure>", lambda e: self.findings_canvas.configure(
            scrollregion=self.findings_canvas.bbox("all")))
        self.findings_canvas.bind("<Configure>", lambda e: self.findings_canvas.itemconfig(window, width=e.width))
        self._render_findings([])

    def _build_log_tab(self):
        bar = tk.Frame(self.tab_log, bg=BG_CARD)
        bar.pack(fill="x", padx=6, pady=(6, 0))
        tk.Button(bar, text="Clear", command=lambda: self._set_text(self.log_text, ""), bg=BORDER, fg=TEXT_MUTED,
                  font=(FONT_FAMILY, 8), relief="flat", bd=0, padx=8, cursor="hand2").pack(side="right")
        self.log_text = self._text_area(self.tab_log, wrap="word", font_size=8)
        for tag, colour in (("success", EMERALD), ("error", ROSE), ("warn", AMBER), ("accent", CYAN_LIGHT)):
            self.log_text.tag_configure(tag, foreground=colour)

    # ------------------------------------------------------------------ inputs

    def _populate_samples(self):
        base = config.DEFAULT_REPORTS_DIR
        self._samples = {}
        if base.is_dir():
            for path in sorted(p for p in base.rglob("*") if p.suffix.lower() in (".pdf", ".docx")):
                self._samples[str(path.relative_to(base))] = path
        self.sample_combo["values"] = list(self._samples)
        self.sample_combo.set("Select..." if self._samples else f"(put reports in {base.name}/)")

    def _on_sample_selected(self, _event=None):
        path = self._samples.get(self.sample_combo.get())
        if path:
            self._set_selected_file(str(path))

    def _selected_model(self) -> config.ModelProfile:
        return config.MODELS[MODEL_KEYS[max(self.engine_combo.current(), 0)]]

    def _on_engine_selected(self, _event=None):
        profile = self._selected_model()
        endpoint = config.model_endpoint(profile)
        weights = config.local_weights(profile)
        if endpoint:
            self._model_status.set(f"Local server: {endpoint}\n{profile.note}")
            self.model_status_lbl.configure(fg=EMERALD)
        elif weights:
            self._model_status.set(f"In-process from {weights}\n{profile.note}")
            self.model_status_lbl.configure(fg=EMERALD)
        else:
            self._model_status.set(f"Not available: set {profile.env_prefix}_ENDPOINT or place weights in "
                                   f"{config.models_dir() / profile.local_folder}. Runs will use the fallback.")
            self.model_status_lbl.configure(fg=ROSE)

    def _browse_file(self):
        path = filedialog.askopenfilename(
            title="Select audit report",
            filetypes=[("Reports", "*.pdf *.docx"), ("PDF", "*.pdf"), ("Word", "*.docx")])
        if path:
            self._set_selected_file(path)

    def _set_selected_file(self, path: str):
        self._selected_file.set(path)
        p = Path(path)
        self.drop_text.configure(text=p.name, fg=CYAN_LIGHT)
        self.file_details_lbl.configure(text=f"{p.stat().st_size / 1048576:.2f} MB  |  {p.parent.name}/")
        self.header_status.configure(text=f"Loaded: {p.name}", fg=CYAN)
        self._log(f"Selected {path}", "accent")

    def _browse_output_dir(self):
        d = filedialog.askdirectory(title="Select output folder")
        if d:
            self._output_dir.set(d)

    # ------------------------------------------------------------------ processing

    def _pipeline(self) -> DocumentPipeline:
        # Cached per model only, so changing other options never reloads the model.
        key = self._selected_model().key
        if key not in self._pipelines:
            for old in self._pipelines.values():  # keep at most one model loaded
                old.close()
            self._pipelines = {key: DocumentPipeline(model=key)}
        pipeline = self._pipelines[key]
        pipeline.enable_security_gate = self._enable_gate.get()
        return pipeline

    def _start_processing(self):
        if self._worker is not None:
            return
        path = self._selected_file.get()
        if not path or not os.path.isfile(path):
            messagebox.showwarning("Select a report", "Please select a PDF or DOCX report first.")
            return
        if not self._enable_gate.get() and not messagebox.askyesno(
                "Malware pre-gate disabled",
                "The document will be parsed without a malware scan. Continue?"):
            return

        try:
            pipeline = self._pipeline()
        except Exception as exc:
            messagebox.showerror("Engine error", str(exc))
            return

        self._cancel.clear()
        self.process_btn.configure(state="disabled", text="PROCESSING...", bg="#312e81")
        self.header_status.configure(text="Processing...", fg=AMBER)
        for b in self.result_buttons:
            b.configure(state="disabled", fg=TEXT_MUTED, bg=BG_CARD_LIGHT)
        self.results.pack_forget()
        self.overlay.pack(fill="both", expand=True)
        self.overlay.start()
        self._log(f"Processing {Path(path).name}", "accent")

        output_dir = self._output_dir.get()

        def work():
            try:
                result = pipeline.process_file(
                    path, output_dir,
                    progress=lambda i, name: self._events.put(("stage", i, name)),
                    cancel=self._cancel.is_set)
                self._events.put(("done", result))
            except PipelineCancelled:
                self._events.put(("cancelled",))
            except DocumentBlocked as exc:
                self._events.put(("blocked", str(exc)))
            except PipelineError as exc:
                self._events.put(("error", str(exc)))
            except Exception as exc:  # unexpected: report with type for diagnosis
                logging.getLogger("report_to_json").exception("Unexpected pipeline failure")
                self._events.put(("error", f"{type(exc).__name__}: {exc}"))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def _cancel_processing(self):
        if self._worker is None:
            return
        self._cancel.set()
        self.overlay.cancel_btn.configure(state="disabled", text="Cancelling...")
        self.overlay.status_detail_lbl.configure(text="Stopping after the current stage...")
        self._log("Cancellation requested; the current stage will finish first", "warn")

    def _drain_events(self):
        try:
            while True:
                event = self._events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self._log(event[1], event[2])
                elif kind == "stage":
                    self.overlay.set_stage(event[1], event[2])
                else:
                    self._finish(kind, event[1] if len(event) > 1 else None)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _finish(self, kind: str, payload: Any):
        self._worker = None
        self.overlay.stop()
        self.overlay.pack_forget()
        self.results.pack(fill="both", expand=True)
        self.process_btn.configure(state="normal", text="PROCESS REPORT", bg=ACCENT)

        if kind == "done":
            self._show_result(payload)
        elif kind == "cancelled":
            self.header_status.configure(text="Cancelled", fg=ROSE)
            self._log("Processing cancelled", "warn")
        elif kind == "blocked":
            self.header_status.configure(text="Blocked by malware pre-gate", fg=ROSE)
            self._log(payload, "error")
            messagebox.showerror("Document blocked", payload)
        else:
            self.header_status.configure(text="Processing failed", fg=ROSE)
            self._log(payload, "error")
            messagebox.showerror("Processing error", f"{payload}\n\nSee the Log tab for details.")

    # ------------------------------------------------------------------ results

    def _show_result(self, result):
        self._result = result
        data = result.data
        meta = result.meta
        observations = data.get("detailed_observations") or []

        banner, colour = STATUS_STYLE.get(result.status, (result.status, AMBER))
        self.header_status.configure(text=f"{banner}  ({result.elapsed_seconds:.0f}s)", fg=colour)
        for b in self.result_buttons:
            b.configure(state="normal", fg=TEXT_MAIN, bg=BORDER)

        title = (data.get("report_meta") or {}).get("report_title") or meta.get("source_file", "")
        self.kpi_title.configure(text=title if len(title) <= 60 else title[:57] + "...")
        self.kpi_findings.configure(text=f"{len(observations)} findings")
        counts = {s: 0 for s in ("Critical", "High", "Medium", "Low")}
        for obs in observations:
            if obs.get("severity") in counts:
                counts[obs["severity"]] += 1
        self.kpi_severity.configure(text="  ·  ".join(f"{v} {k}" for k, v in counts.items() if v))

        cov = meta.get("coverage", {})
        notes = [f"Status: {banner}",
                 f"Model: {meta.get('model', {}).get('key')}",
                 f"Pre-gate: {meta.get('security_gate', {}).get('verdict')}",
                 f"{cov.get('placed_in_schema', 0):.1%} of text placed in schema, "
                 f"{cov.get('unmapped_entries', 0)} unmapped passage(s) and "
                 f"{len(meta.get('dropped_values', []))} dropped value(s) kept in the run report"]
        self.quality_lbl.configure(text="   •   ".join(notes) + ("\n" + "\n".join(result.warnings) if result.warnings else ""),
                                   fg=colour if result.status != "ok" else TEXT_MUTED)
        for w in result.warnings:
            self._log(w, "error" if result.status == "fallback" else "warn")
        self._log(f"Saved {result.json_path}", "success")

        pretty = json.dumps(data, indent=2, ensure_ascii=False)
        self._set_text(self.json_text, pretty)
        highlight_json(self.json_text, pretty)
        try:
            self._set_text(self.md_text, result.transcript_path.read_text(encoding="utf-8"))
        except OSError:
            self._set_text(self.md_text, "")
        self._render_findings(observations)
        self.notebook.select(self.tab_findings if observations else self.tab_json)

    def _render_findings(self, findings):
        for w in self.findings_frame.winfo_children():
            w.destroy()
        if not findings:
            tk.Label(self.findings_frame, text="No findings to show.", font=(FONT_FAMILY, 11, "bold"),
                     bg=BG_CARD, fg=TEXT_MUTED).pack(pady=40)
            return
        for idx, item in enumerate(findings, 1):
            sev = (item.get("severity") or "Not stated").upper()
            colours = {"CRITICAL": ("#3f1d24", ROSE), "HIGH": ("#3b2219", AMBER),
                       "MEDIUM": ("#382d19", "#fde047"), "LOW": ("#152d24", EMERALD)}
            sev_bg, sev_fg = colours.get(sev, (BG_CARD_LIGHT, TEXT_MUTED))

            card = tk.Frame(self.findings_frame, bg=BG_INPUT, highlightbackground=BORDER, highlightthickness=1)
            card.pack(fill="x", padx=10, pady=6)
            head = tk.Frame(card, bg=BG_INPUT)
            head.pack(fill="x", padx=12, pady=(10, 6))
            tk.Label(head, text=f" {sev} ", font=(FONT_FAMILY, 8, "bold"), bg=sev_bg, fg=sev_fg,
                     padx=6, pady=2).pack(side="left")
            tk.Label(head, text=f" {item.get('finding_id') or idx}  {item.get('title') or '(untitled)'}",
                     font=(FONT_FAMILY, 10, "bold"), bg=BG_INPUT, fg=TEXT_MAIN).pack(side="left", padx=(8, 0))
            badges = [item.get("status")] + (item.get("cwe") or [])[:2]
            if item.get("cvss_score") is not None:
                badges.append(f"CVSS {item['cvss_score']}")
            for badge in filter(None, reversed(badges)):
                tk.Label(head, text=str(badge)[:40], font=(FONT_FAMILY, 8, "bold"), bg=BG_CARD_LIGHT,
                         fg=CYAN_LIGHT, padx=6, pady=2).pack(side="right", padx=(4, 0))

            assets = ", ".join(item.get("affected_asset") or []) or "not stated"
            tk.Label(card, text=f"Affected: {assets[:200]}", font=(FONT_FAMILY, 8), bg=BG_INPUT, fg=TEXT_MUTED,
                     anchor="w", justify="left", wraplength=700).pack(fill="x", padx=12, pady=(0, 6))
            for label, key, bg, fg in (("Observation", "observation_description", BG_CARD, TEXT_MUTED),
                                       ("Recommendation", "recommendation", "#0e231d", EMERALD)):
                value = item.get(key)
                if not value:
                    continue
                box = tk.Frame(card, bg=bg, highlightbackground=BORDER, highlightthickness=1)
                box.pack(fill="x", padx=12, pady=(0, 6))
                text = value if len(value) <= 320 else value[:320] + "..."
                tk.Label(box, text=f"{label}: {text}", font=(FONT_FAMILY, 8), bg=bg, fg=fg,
                         justify="left", wraplength=700).pack(anchor="w", padx=8, pady=5)
            poc = item.get("proof_of_concept") or []
            if poc:
                retest = sum(1 for p in poc if p.get("is_retest"))
                tk.Label(card, text=f"Evidence: {len(poc)} item(s), {retest} retest", font=(FONT_FAMILY, 8),
                         bg=BG_INPUT, fg=TEXT_DIM).pack(anchor="w", padx=12, pady=(0, 8))

    # ------------------------------------------------------------------ actions

    def _set_text(self, widget: tk.Text, text: str):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.configure(state="disabled")

    def _search_json(self):
        query = self._search_var.get()
        self.json_text.tag_remove("search_match", "1.0", "end")
        if not query:
            return
        pos, first = "1.0", None
        while (pos := self.json_text.search(query, pos, nocase=True, stopindex="end")):
            end = f"{pos}+{len(query)}c"
            self.json_text.tag_add("search_match", pos, end)
            first = first or pos
            pos = end
        if first:
            self.json_text.see(first)

    def _copy_json(self):
        if self._result is None:
            return
        self.clipboard_clear()
        self.clipboard_append(json.dumps(self._result.data, indent=2, ensure_ascii=False))
        self._log("JSON copied to clipboard", "success")

    def _save_json_as(self):
        if self._result is None:
            messagebox.showwarning("Nothing to save", "Process a report first.")
            return
        target = filedialog.asksaveasfilename(title="Save JSON", defaultextension=".json",
                                              filetypes=[("JSON", "*.json")])
        if target:
            Path(target).write_text(json.dumps(self._result.data, indent=2, ensure_ascii=False), encoding="utf-8")
            self._log(f"Saved {target}", "success")

    def _open_result(self, which: str):
        if self._result is None:
            return
        path = {"json_path": self._result.json_path, "meta_path": self._result.meta_path,
                "manifest": Path(self._result.manifest_path),
                "output_dir": self._result.json_path.parent}[which]
        if path.exists():
            open_path(str(path))

    def _log(self, message: str, tag: str = ""):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{ts}] {message}\n", tag or ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def destroy(self):
        logging.getLogger("report_to_json").removeHandler(self._log_handler)
        super().destroy()


def main() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(config.REPO_ROOT / ".env")
    except ImportError:
        pass
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if stream and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ReportToJsonApp().mainloop()


if __name__ == "__main__":
    main()
