# uvtest — benchmarks, comparisons, and the batch compressor

All scripts run from the repo root against the single root `.venv`
(`uv run python uvtest/<script>.py`); see `../DEVELOPMENT_NOTES.md` §10.0 for
the command conventions and the `out/_t_*` scratch-directory rule.

## Production entry point

- **`compress_dir.py`** — the one-command batch compressor (AVIF/WebP routing,
  fixed CQ by default (CQ36 up to 8 MP, CQ30 above), optional auto-CQ, persistent process pool, streaming report). Config file: `compress_config.json`
  (regenerate a template with `--write-config`). Full option reference:
  `uv run python uvtest/compress_dir.py --help`.

Default opaque routing is `--opaque-format auto`: up to 2 MP uses WebP Q90, larger GPU-eligible images use AVIF (CQ36 up to 8 MP, CQ30 above). Change the boundary with `--opaque-webp-max-mp`; `--opaque-format webp` or `avif` forces one format. The 2 MP boundary is the measured speed crossover with NVENC context hits. Transparent and oversize routing retain priority.

Use `--auto-quality 88` to opt into calibration, or `--fixed-cq` (optionally with `--cq N`) to override an auto-quality config. Passing both options explicitly is an error; `--no-fixed-cq` enables target 88 when no target is configured. CLI quality mode overrides the config. Workers persist for up to 10000 tasks and reuse NVENC contexts; their total count is capped at 8 to respect the session budget.

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
- `measure_ctx_overhead.py` — measures Python input/preparation, RGB→YUV,
  context opening/eviction/closing, host-frame preparation, and NVENC API
  send/receive wall time with `NVAVIF_DEBUG_TIMING=1`. Counts cache hits and
  misses; strict GPU mode prevents CPU fallback from contaminating results.
  Rebuild and reinstall the wheel after changing the library instrumentation.

```bash
uv run python uvtest/measure_ctx_overhead.py --images 12 --repeats 3
uv run python uvtest/measure_ctx_overhead.py --source-dir test_imgs --min-mp 8 --images 12 --repeats 3
```

Each repeat starts a fresh worker and excludes its initial hardware probe from
per-image timings. Synthetic same512/mixed corpora test cache behavior; the
real-source mode selects static opaque images above the requested MP threshold
within NVENC dimensions, including RGBA with fully opaque alpha. Reports in
`out/ctx_overhead_corpus/stages_<timestamp>.json` retain raw events, source paths,
per-image wall time, worker-loop time and whole-process time separately. The
summary is the median of per-repeat per-image means, not batch throughput.
`--auto-cq` explicitly adds calibration measurements; `--cache 0` disables reuse.
`python input` also reports its decode, ICC/orientation/mode conversion and
NumPy-copy sub-stages; those sub-stages are already included in the parent.
Host-frame time measures CPU allocation/copy. The send/receive calls include
FFmpeg/driver, upload, encoder work and synchronization; they cannot be labelled
pure GPU upload or pure NVENC execution time. `native other` is the unassigned
native-call remainder, which includes auto-CQ work when enabled. Source selection
scans filenames in sorted order and takes at most `--images` eligible files;
results describe that subset, not the entire gallery or production concurrency.

**Comparing runs (methodology).** Do not A/B across sessions: the same unchanged
code has measured 387 and 447 ms/image on different days — drift larger than most
optimizations. A/B must be same-wheel and back-to-back, ideally with a temporary
env gate that selects the old path inside one build (removed once measured). The
host wall-clock also mixes GPU-side variability into the Python sub-stages, so
per-stage numbers for a given (width, height) can swing ±60% between runs even
when the code path is identical; prefer interleaved micro-measurements for
copy-level changes.

- `bench_opaque_formats.py [--fixed-cq]` — the format-vs-format speed benchmark.
  Runs the batch's own `_encode_task` path (decode + preprocessing + encode +
  write) over the opaque corpus, excluding alpha-header / animated / oversized
  sources, one format per round, interleaved avif→webp→webp→avif to cancel
  drift. `--fixed-cq` makes the AVIF rounds use the benchmark's fixed CQ20
  instead of auto quality 88. Judge by wall time / Σ task time, not per-image median: WebP's
  per-image distribution is right-skewed (near-free on small images, heavy on
  large), so its median lands on the small half. See `DEVELOPMENT_NOTES.md` §21.
  `--same-size N --same-size-wh WxH` gives every worker N back-to-back tasks of
  one size (all but each worker's first are context hits); add `--alternate-wh`
  to alternate two sizes instead (steady-state evict+open cost). Reports separate
  cold / hit / switch timings.
- `_verify_pixel_path.py DIR` — asserts the AVIF route's pixel buffer is
  byte-identical between the current single-copy path and the previous
  `np.array`-based pipeline, for every supported image in DIR.
- `_verify_icc_skip.py DIR` — reports how many profile-carrying sources are
  detected as sRGB-equivalent (transform identity within 1 LSB) and skipped, and
  the max per-channel delta versus always converting.
- `_verify_copy_savings.py DIR [REPS] [MIN_MP]` — interleaved per-image timing of
  the copy operations the single-copy path removes (`np.array(im).tobytes()`
  versus `im.tobytes()`, plus the now-conditional `exif_transpose`). Interleaving
  cancels machine-state drift, which the stage harness cannot.

## Quality / comparison tooling

- `quality_metrics.py` — SSIM/MAE helpers shared by the compare scripts.
- `compare_perceptual.py` — SSIM + LPIPS + chroma metrics between
  two runs (see `../DEVELOPMENT_NOTES.md` §19 for the metric caveats: SSIM is
  blind to 4:2:0 chroma bleed; chroma PSNR uses the standard BT.709 matrix,
  ΔE2000's hue-mean remains simplified).
- `compare_runs.py` — pairs two batch runs by report rows (never by filename —
  stale outputs poisoned an early comparison, hence the report-keyed pairing).
  RGBA sources are composited over white before the block SSIM; each image
  also reports its worst tile and that tile's source-pixel location.
- `compare_transparent.py` / `compare_oversize.py` — focused route comparisons
  (WebP vs AVIF for transparent / oversized sources). `compare_transparent`
  composites RGBA over white before the SSIM; `compare_oversize` flattens both
  sides to RGB instead (self-consistent for its opaque corpus, not composited).
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
