# Models

The pipeline runs entirely on local models; no inference API is ever called. The user picks one of the
three models below in the GUI, and that model runs both passes.

The weights themselves are **not stored in this repository**. They total about 346 GB, and single files
reach 50 GB, well beyond what Git or GitHub can hold. Instead, [`manifest.json`](manifest.json) pins each
model to an exact revision and lists every file with its size and checksum. Weights are staged once
from a connected machine and verified on the air-gapped system.

| Key | Model | Revision | Size | Licence |
| --- | --- | --- | --- | --- |
| `gemma-4-31b` | [google/gemma-4-31B-it](https://huggingface.co/google/gemma-4-31B-it) | `842da37` | 62.6 GB | Apache 2.0 |
| `llama-4-scout` | [meta-llama/Llama-4-Scout-17B-16E-Instruct](https://huggingface.co/meta-llama/Llama-4-Scout-17B-16E-Instruct) | `92f3b15` | 217.3 GB | Llama 4 Community License (gated) |
| `nemotron-3-nano-omni` | [nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-BF16](https://huggingface.co/nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-BF16) | `e5e9932` | 66.1 GB | NVIDIA Open Model Agreement |

## Staging for an air-gapped site

1. On a machine with internet access:

   ```bash
   pip install -r requirements-models.txt
   python -m report_to_json.download --model gemma-4-31b --dest /transfer/models
   ```

   The download is pinned to the manifest's revision and checked against it before the command
   reports success. Llama 4 is gated: accept its licence on Hugging Face and set `HF_TOKEN` first.

2. Carry `/transfer/models/<folder>` across, and place it at `models/<folder>` (or under `MODELS_DIR`).

3. On the air-gapped system, confirm the copy is complete and intact:

   ```bash
   python -m report_to_json.download --verify gemma-4-31b
   ```

4. Serve it locally (recommended, see [docs/models.md](../docs/models.md)):

   ```bash
   vllm serve models/gemma-4-31B-it --served-model-name google/gemma-4-31B-it --enable-prefix-caching
   ```

   Alternatively, leave it in `models/` and the app loads it in-process.

To move to newer revisions, run `python -m report_to_json.download --refresh-manifest` on a connected
machine, review the diff of `manifest.json`, and commit it.

## Licence obligations

Each model's licence governs its use and any internal redistribution of the weights. Read the full terms
on the model's page before deploying.
- **Gemma 4:** Apache 2.0. Keep the licence and notice files that ship with the weights.
- **Llama 4 Scout:** Llama 4 Community License and Acceptable Use Policy. If you redistribute the
  weights, include a copy of the licence and display the attribution the licence requires.
- **Nemotron 3 Nano Omni:** NVIDIA Open Model Agreement. Keep its notices with the weights.

Weight folders placed here are git-ignored; only this README and `manifest.json` are tracked.
