# uvtest — benchmarks, comparisons, and the batch compressor

All scripts run from the repo root against the single root `.venv`
(`uv run python uvtest/<script>.py`); see `../DEVELOPMENT_NOTES.md` §10.0 for
the command conventions and the `out/_t_*` scratch-directory rule.

## Production entry point

- **`compress_dir.py`** — the one-command batch compressor (AVIF/WebP routing,
  auto-CQ, process pool, streaming report). Config file: `compress_config.json`
  (regenerate a template with `--write-config`). Full option reference:
  `uv run python uvtest/compress_dir.py --help`.

## Library tests & benchmarks

- `test_nvavif.py` — interactive smoke pass over encode/decode paths (chroma ×
  depth matrix, alpha, auto-CQ, Pillow plugin). Prints only; no asserts.
- `test_compress_dir_routing.py` — routing regression tests with real asserts
  (exit non-zero on failure): JPEG-quality estimation, opaque/transparent/
  oversize/keep-smaller routing via `_encode_task`, alpha-plane behavior. No
  pytest dependency; needs the built wheel, runs tiny CPU/GPU encodes.
- `_b3_metric.py` — B3 regression probe: per-channel RGB MAE + luma SSIM +
  size for GPU AVIF at cq=20 on a fixed 5-image set (writes a JSON baseline).
- `benchmark.py` — encode/decode timing benchmark, results to
  `benchmark_results.json`.
- `measure_ctx_overhead.py` — measures the NVENC context-open cost and the
  session-reuse (LRU) gain behind `NVAVIF_CTX_CACHE`.

## Quality / comparison tooling

- `quality_metrics.py` — SSIM/MAE helpers shared by the compare scripts.
- `compare_perceptual.py` — SSIM + LPIPS + simplified chroma metrics between
  two runs (see `../OPTIMIZATION_PROPOSALS.md` measurement caveats: SSIM is
  blind to 4:2:0 chroma bleed; chroma PSNR/ΔE are simplified implementations).
- `compare_runs.py` — pairs two batch runs by report rows (never by filename —
  stale outputs poisoned an early comparison, hence the report-keyed pairing).
- `compare_transparent.py` / `compare_oversize.py` — focused route comparisons
  (WebP vs AVIF for transparent / oversized sources).
- `bench_alpha_tuning.py` — rav1e alpha speed/quality sweep
  (`ALPHA_RAV1E_PRESET`).
- `test_oversize_preset.py` — validates the oversize CPU-fallback preset
  quality trade-off.
- `export_test_set.py` — builds a representative test subset from a gallery.

## Notes

- `out/` is disposable scratch space (hundreds of MB; reports, encoded
  outputs, debug fixtures). Safe to delete; scripts recreate what they need.
- `compress_report*.json` / `*.stream.jsonl` in `out/` are the run reports the
  comparator tools key off — keep them if you plan to diff runs.
