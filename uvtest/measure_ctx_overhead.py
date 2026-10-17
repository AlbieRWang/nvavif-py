"""Measure Python, YUV and NVENC API stages with strict GPU encoding.

Usage:
    uv run python uvtest/measure_ctx_overhead.py --images 12 --repeats 3
    uv run python uvtest/measure_ctx_overhead.py --source-dir test_imgs --min-mp 8

Each repeat uses a fresh process; the hardware probe is outside measured calls.
Synthetic same512/mixed corpora isolate reuse; real sources are filtered to
opaque, supported dimensions. All stages measure host wall time. Host-frame
preparation is a CPU copy; send/receive include FFmpeg, driver, transfer and
encoder work, not separately measured upload or pure NVENC execution.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "uvtest" / "out" / "ctx_overhead_corpus"

STAGES = ("python input", "python normalize", "yuv_build", "nvenc ctx_evict",
          "nvenc ctx_open", "nvenc ctx_close", "nvenc host_frame",
          "nvenc send", "nvenc receive")
INPUT_STAGES = ("python decode", "python color_orientation", "python array")
STAGE_RE = re.compile(
    r"timing: (python decode|python color_orientation|python array|"
    r"python input|python normalize|python native|yuv_build|"
    r"nvenc host_frame|nvenc send|nvenc receive|nvenc ctx_open|"
    r"nvenc ctx_evict|nvenc ctx_close|nvenc ctx_reuse|gpu encode) "
    r"(\d+)x(\d+) (?:total )?([\d.]+) ms"
)


def generate_corpus(n: int) -> dict[str, list[Path]]:
    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(7)
    corpora: dict[str, list[Path]] = {}
    specs = {
        "same512": [(512, 512)] * n,
        "mixed": [(int(rng.integers(256, 1537)) // 2 * 2, int(rng.integers(256, 1537)) // 2 * 2) for _ in range(n)],
    }
    for name, dims in specs.items():
        d = CORPUS / name
        d.mkdir(parents=True, exist_ok=True)
        paths = []
        for i, (w, h) in enumerate(dims):
            p = d / f"img_{i:04d}.jpg"
            if not p.exists():
                # Structured content (gradient + moderate noise), not pure
                # noise: keep encode cost representative rather than worst-case
                # entropy-limited.
                x = np.linspace(0, 255, w, dtype=np.float32)
                grad = np.tile(x, (h, 1))
                noise = rng.normal(0, 8, (h, w)).astype(np.float32)
                arr = np.clip(grad + noise, 0, 255).astype(np.uint8)
                Image.fromarray(arr, "L").convert("RGB").save(p, "JPEG", quality=95)
            paths.append(p)
        corpora[name] = paths
    return corpora


def select_sources(directory: Path, n: int, min_mp: float) -> tuple[list[Path], list[dict]]:
    from PIL import Image, ImageOps

    selected, excluded = [], []
    for path in sorted(directory.rglob("*")):
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}:
            continue
        try:
            with Image.open(path) as source:
                w, h = source.size
                reason = None
                if getattr(source, "is_animated", False):
                    reason = "animated"
                elif max(w, h) > 8192 or min(w, h) < 130:
                    reason = "dimensions"
                elif w * h <= min_mp * 1e6:
                    reason = "below_min_mp"
                elif "A" in source.getbands() or "transparency" in source.info:
                    with ImageOps.exif_transpose(source).convert("RGBA") as rgba:
                        if rgba.getchannel("A").getextrema() != (255, 255):
                            reason = "transparent"
                if reason:
                    excluded.append({"path": str(path), "reason": reason})
                else:
                    selected.append(path)
        except Exception as exc:
            excluded.append({"path": str(path), "reason": str(exc)})
    return selected[:n], excluded


def run_worker(images: list[str], mode_args: list[dict], capacity: int) -> dict:
    env = dict(os.environ, NVAVIF_DEBUG_TIMING="1", NVAVIF_CTX_CACHE=str(capacity))
    payload = json.dumps({"images": images, "mode_args": mode_args})
    begin = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, __file__, "--worker"], input=payload,
        capture_output=True, text=True, env=env, cwd=str(ROOT),
    )
    process_ms = (time.perf_counter() - begin) * 1e3
    if proc.returncode != 0:
        sys.exit(f"worker failed:\n{proc.stderr[-4000:]}")
    result = json.loads(proc.stdout)
    events = [dict(stage=m[1], width=int(m[2]), height=int(m[3]), ms=float(m[4]))
              for m in STAGE_RE.finditer(proc.stderr)]
    counts = {stage: sum(e["stage"] == stage for e in events)
              for stage in (*STAGES, *INPUT_STAGES, "python native", "nvenc ctx_reuse")}
    frame_count = counts["nvenc ctx_open"] + counts["nvenc ctx_reuse"]
    if (any(counts[s] != len(images) for s in
            ("python input", "python normalize", "python native", "yuv_build", *INPUT_STAGES))
            or any(counts[s] != frame_count for s in
                   ("nvenc host_frame", "nvenc send", "nvenc receive"))):
        sys.exit("Incomplete stage timings; rebuild and reinstall the current wheel first")
    result.update(process_ms=process_ms, events=events, stderr=proc.stderr, counts=counts)
    return result


def summarize(runs: list[dict], n: int) -> dict:
    stages = {s: statistics.median(
        sum(e["ms"] for e in run["events"] if e["stage"] == s) / n for run in runs
    ) for s in STAGES}
    native_other = statistics.median(
        (sum(e["ms"] for e in run["events"] if e["stage"] == "python native")
         - sum(e["ms"] for e in run["events"] if e["stage"] in STAGES[2:])) / n
        for run in runs
    )
    misses = statistics.median(run["counts"]["nvenc ctx_open"] for run in runs)
    hits = statistics.median(run["counts"]["nvenc ctx_reuse"] for run in runs)
    return {"wall_ms_per_image": statistics.median(
                sum(run["per_image_ms"]) / n for run in runs),
            "stages_ms_per_image": stages,
            "input_breakdown_ms_per_image": {s: statistics.median(
                sum(e["ms"] for e in run["events"] if e["stage"] == s) / n for run in runs
            ) for s in INPUT_STAGES},
            "native_other_ms_per_image": native_other,
            "context_opens": misses, "context_hits": hits,
            "context_hit_pct": 100 * hits / (hits + misses) if hits + misses else 0,
            "loop_ms": statistics.median(run["loop_ms"] for run in runs),
            "process_ms": statistics.median(run["process_ms"] for run in runs)}


def main() -> None:
    if "--worker" in sys.argv:
        payload = json.loads(sys.stdin.read())
        import nvavif_py as nv

        os.environ["NVAVIF_DEBUG_TIMING"] = "0"
        if not nv.is_supported():
            raise RuntimeError("NVENC AV1 unavailable; CPU fallback is disabled")
        os.environ["NVAVIF_DEBUG_TIMING"] = "1"
        kwargs: dict = {}
        for kv in payload["mode_args"]:
            kwargs.update(kv)
        per_image, sizes = [], []
        started = time.perf_counter()
        for p in payload["images"]:
            t0 = time.perf_counter()
            data = nv.encode_file(p, device="gpu", **kwargs)
            per_image.append((time.perf_counter() - t0) * 1e3)
            sizes.append(len(data))
        print(json.dumps({"per_image_ms": per_image, "output_bytes": sizes,
                          "paths": payload["images"], "module": nv.__file__,
                          "loop_ms": (time.perf_counter() - started) * 1e3}))
        return

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--images", type=int, default=12)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--cache", type=int, default=1)
    ap.add_argument("--source-dir", type=Path)
    ap.add_argument("--min-mp", type=float, default=8)
    ap.add_argument("--auto-cq", action="store_true")
    args = ap.parse_args()
    if args.images < 1 or args.repeats < 1 or args.cache < 0 or args.min_mp < 0:
        ap.error("images/repeats must be positive and cache/min-mp nonnegative")
    excluded = []
    if args.source_dir:
        if not args.source_dir.is_dir():
            ap.error("source-dir must be an existing directory")
        paths, excluded = select_sources(args.source_dir, args.images, args.min_mp)
        if not paths:
            ap.error("no eligible opaque images")
        corpora = {"sources": paths}
    else:
        corpora = generate_corpus(args.images)
    modes = {"cq20": [{"cq": 20}]}
    if args.auto_cq:
        modes["auto_cq90"] = [{"auto_cq": True, "target_quality": 90}]

    report = {"settings": vars(args) | {"source_dir": str(args.source_dir)},
              "excluded": excluded, "variants": {}}
    CORPUS.mkdir(parents=True, exist_ok=True)
    out = CORPUS / f"stages_{time.time_ns()}.json"
    for name, paths in corpora.items():
        for mode, kwargs in modes.items():
            runs = [run_worker([str(p.resolve()) for p in paths], kwargs, args.cache)
                    for _ in range(args.repeats)]
            summary = summarize(runs, len(paths))
            report["variants"][f"{name}/{mode}"] = {
                "images": len(paths), "summary": summary, "runs": runs}
            out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"{name}/{mode}: {len(paths)} images, {summary['wall_ms_per_image']:.2f} ms/image, "
                  f"context hits {summary['context_hit_pct']:.1f}%", flush=True)
            for stage, ms in summary["stages_ms_per_image"].items():
                print(f"  {stage:<20} {ms:9.2f} ms/image")
            print(f"  {'native other':<20} {summary['native_other_ms_per_image']:9.2f} ms/image")
            print("  Within python input (already included above):")
            for stage, ms in summary["input_breakdown_ms_per_image"].items():
                print(f"    {stage:<26} {ms:9.2f} ms/image")
    print(f"Report: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
