# Document Standardization Pipeline

`report_to_json` is an air-gapped document standardisation pipeline for security audit reports (VAPT, network, mobile and
IoT; PDF or DOCX). It produces a single standardised JSON format defined by
[`master_schema.json`](report_to_json/schema/master_schema.json):

```
malware gate ─► PDF ─► images to deep storage (pointer tags) ─► VLM transcription ─► schema restructuring ─► JSON
```

- **One model runs both passes.** The user picks it: Gemma 4 31B, Llama 4 Scout, or Nemotron 3 Nano Omni.
- **Exactly the master schema.** The output JSON has precisely the structure of `master_schema.json`.
- **Nothing is dropped.** Transcription is checked against the PDF's text layer. Text that fits no schema
  field, and values the schema can't hold, are kept in a separate run report next to the JSON.
- **Nothing is invented.** Values the document doesn't state are `null`.
- **Fallback is failure.** The rule-based parser only runs when the model pipeline fails, and the output
  is then marked `fallback`.

Details:
- how each stage works: [docs/pipeline_overview.md](docs/pipeline_overview.md)
- model choice and serving: [docs/models.md](docs/models.md); staging the weights: [models/README.md](models/README.md)
- what each schema field means: [docs/MASTER_SCHEMA_SPECIFICATION.md](docs/MASTER_SCHEMA_SPECIFICATION.md)

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate              # Windows  (source .venv/bin/activate elsewhere)
pip install -r requirements.txt
copy .env.example .env              # then set the model endpoints / paths
```

Models run in a local inference server (recommended; see [docs/models.md](docs/models.md)) or in-process
(`pip install -r requirements-models.txt` and put the weights in `models/`). DOCX input needs Microsoft
Word (Windows) or LibreOffice for conversion to PDF.

## Run

```bash
python -m report_to_json      # or run_gui.bat, or python gui_app.py
```

Pick a report and a model, then press **Process report**. The model panel shows whether the selected
model is reachable. Output goes to `outputs/`:

| Output | Contents |
| --- | --- |
| `<name>.json` | The standardised report: exactly the structure of `master_schema.json` |
| `<name>.meta.json` | Run report: status, per-page and per-section reports, gate verdicts, coverage, `unmapped_content`, `dropped_values` |
| `<name>_transcript.md` | The pass-1 transcript the JSON was built from |
| `deep_storage/manifests/<id>.json` | Image manifest: each `[IMAGE_PAGE_n_FIG_m]` tag mapped to its stored image |

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The tests use a scripted model to drive the whole route: gate, conversion order, masking, retries,
fallbacks, coverage and the GUI. No model weights or GPU are needed.

```
report_to_json/
  pipeline.py      the route, stages 0-5, and run status
  security/        YARA pre-gate, OOXML inspection, rules/*.yar
  convert.py       DOCX -> PDF (Word or LibreOffice)
  storage.py       deep storage (content-addressed) and image manifests
  images.py        image offload, page masking with pointer tags, tag reconciliation
  vlm.py           pass 1 (transcription) and pass 2 (sectioned schema restructuring)
  backends.py      local OpenAI-compatible server / in-process Transformers
  coverage.py      page recall and transcript-to-JSON coverage
  structurer.py    rule-based fallback parser (textparse.py holds its helpers)
  transcribe.py    text-layer fallback transcription
  schema/          master template, derived JSON Schemas, validation
  gui.py           Tkinter desktop app
```

**Never commit client reports or anything derived from them.** `reports/`, `outputs/`, `deep_storage/`
and all `*.pdf` / `*.docx` files are git-ignored for that reason.
