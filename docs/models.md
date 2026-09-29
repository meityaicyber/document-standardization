# Models

The user picks one model in the GUI, and that model runs **both** passes: pass 1 transcribes page images
and pass 2 restructures the transcript into the master schema. All three are open-weight vision-language
models that accept images and text. All inference is local; the system is designed to run air-gapped.

| Key | Model | Architecture | Context | Licence | Notes |
| --- | --- | --- | --- | --- | --- |
| `gemma-4-31b` (default) | `google/gemma-4-31B-it` | dense, 31B | 256K | Apache 2.0 | OmniDocBench 1.5 edit distance 0.131 (lower is better). Images must come before text in the prompt; the backend does this. Use the largest visual token budget (1120) for small print, via `R2J_GEMMA_4_31B_MM_PROCESSOR_KWARGS` with the parameter name your serving stack uses. |
| `llama-4-scout` | `meta-llama/Llama-4-Scout-17B-16E-Instruct` | MoE, 109B total / 17B active | 10M | Llama 4 Community License | Natively multimodal. Meta published no document-OCR benchmarks at launch, so run the bake-off below before relying on it. |
| `nemotron-3-nano-omni` | `nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-BF16` | hybrid Mamba-Transformer MoE, 31B / ~3B active | 256K | NVIDIA Open Model Agreement | OCRBench-V2 (English) 67.0 and MMLongBench-Doc 57.5, at or near the top of both. Reasoning is switched off (`enable_thinking: false`) for literal transcription. FP8 and NVFP4 checkpoints are also published. |

Benchmark figures are from the vendors' model cards. They use different benchmarks and are not directly
comparable.

## Serving

Recommended: one local OpenAI-compatible server per model, for example vLLM:

```bash
vllm serve /models/gemma-4-31B-it --served-model-name google/gemma-4-31B-it \
     --enable-prefix-caching --max-model-len 262144
```

Then set `R2J_GEMMA_4_31B_ENDPOINT=http://<host>:8000/v1`. With a server, the pipeline gets two things:
- **Constrained decoding:** every pass-2 response is forced to match its section's JSON Schema.
- **Prefix caching:** pass-2 prompts all start with the same transcript, so the server encodes it once
  per document instead of once per call.

The in-process Transformers backend is used when no endpoint is set and weights exist in
`MODELS_DIR/<folder>`. It has no constrained decoding; outputs are validated and repaired with retries.

## Staging weights for an air-gapped site

On a connected machine (Gemma 4 and Llama 4 are gated: accept the licence and set `HF_TOKEN` first):

```bash
python -m report_to_json.download --model gemma-4-31b --dest /transfer/models
```

The download is pinned to the revision in `models/manifest.json` and checked against its checksums. Copy the
folder across, then run `python -m report_to_json.download --verify gemma-4-31b` on the air-gapped system.
The pipeline sets `HF_HUB_OFFLINE=1` and only ever loads local paths. See [models/README.md](../models/README.md).

## Choosing: run a bake-off on your own reports

Every run report records how well the model did. Process the same set of reports with each model and compare:
- `status`
- page recall in `pass1_pages[].recall`
- `coverage.placed_in_schema`
- the number of `unmapped_content` and `dropped_values` items

These all live in the run report, `<name>.meta.json`.
