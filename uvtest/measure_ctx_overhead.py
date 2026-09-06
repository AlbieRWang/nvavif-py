"""Measure the per-encode NVENC context-open overhead (OPTIMIZATION_PROPOSALS Q).

Answers one question: what share of per-image wall time is the NVENC/FFmpeg
encoder context creation (the part a persistent-encoder/session-reuse design
could remove), and how does that share move with image size and with auto_cq
(trial encodes add two same-size 512x512 opens per image)?

Method: synthetic corpora are encoded in a worker subprocess with
NVAVIF_DEBUG_TIMING=1; the Rust side emits one stderr line per encoder open
(`nvenc ctx_open`) and one per finished GPU encode (`gpu encode ... total`),
so ctx_open share of per-image wall time is measured, not estimated.

Corpora (generated under uvtest/out/ctx_overhead_corpus/, never test_imgs):
    same512  N images 512x512 JPEG      (session-reuse best case: every
                                         image shares one dimension pair)
    mixed    N images 256..1536 px JPEG (worst case: every open is a fresh size)

Usage:
    uv run python uvtest/measure_ctx_overhead.py [--images 200] [--repeats 3]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "uvtest" / "out" / "ctx_overhead_corpus"

CTX_RE = re.compile(r"nvenc ctx_open (\d+)x(\d+) ([\d.]+) ms")
ENC_RE = re.compile(r"gpu encode (\d+)x(\d+) total ([\d.]+) ms")


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


def run_worker(images: list[str], mode_args: list[str]) -> tuple[dict, dict]:
    """Encode the list in a subprocess with timing on; return (timings, stderr-aggregates)."""
    env = dict(os.environ, NVAVIF_DEBUG_TIMING="1")
    payload = json.dumps({"images": images, "mode_args": mode_args})
    proc = subprocess.run(
        [sys.executable, __file__, "--worker"],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )
    if proc.returncode != 0:
        sys.exit(f"worker failed:\n{proc.stderr[-2000:]}")
    timings = json.loads(proc.stdout)
    opens = [float(m.group(3)) for line in proc.stderr.splitlines() if (m := CTX_RE.search(line))]
    encs = [float(m.group(3)) for line in proc.stderr.splitlines() if (m := ENC_RE.search(line))]
    return timings, {"opens": opens, "enc_total_ms": encs}


def median(vals: list[float]) -> float:
    s = sorted(vals)
    return s[len(s) // 2] if s else 0.0


def main() -> None:
    if "--worker" in sys.argv:
        payload = json.loads(sys.stdin.read())
        import time

        import nvavif_py as nv

        kwargs: dict = {}
        for kv in payload["mode_args"]:
            kwargs.update(kv)
        per_image = []
        for p in payload["images"]:
            t0 = time.perf_counter()
            nv.encode_file(p, **kwargs)
            per_image.append(round(time.perf_counter() - t0, 4))
        print(json.dumps({"per_image_ms": [v * 1e3 for v in per_image]}))
        return

    ap = argparse.ArgumentParser()
    ap.add_argument("--images", type=int, default=200)
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()

    corpora = generate_corpus(args.images)
    modes = {
        "cq20": [{"cq": 20}],
        "auto_cq90": [{"auto_cq": True, "target_quality": 90}],
    }

    print(f"corpus: {args.images} images per variant, {args.repeats} repeats (median)\n")
    header = f"{'variant/mode':<18}{'img_ms':>9}{'open/img':>10}{'open_ms/img':>13}{'open share':>12}{'enc_ms/img':>12}"
    print(header)
    results = {}
    for name, paths in corpora.items():
        for mode, kwargs in modes.items():
            runs = []
            for _ in range(args.repeats):
                timings, agg = run_worker([str(p) for p in paths], kwargs)
                runs.append((timings, agg))
            # Median over repeats of the per-run means (per-image wall is noisy)
            img_ms = median([sum(t["per_image_ms"]) / len(t["per_image_ms"]) for t, _ in runs])
            open_cnt = median([len(a["opens"]) / len(paths) for _, a in runs])
            open_ms = median([sum(a["opens"]) / len(paths) for _, a in runs])
            enc_ms = median([sum(a["enc_total_ms"]) / len(paths) for _, a in runs])
            share = open_ms / img_ms * 100 if img_ms else 0
            results[f"{name}/{mode}"] = {"img_ms": img_ms, "open_per_img": open_cnt, "open_ms": open_ms, "share_pct": share, "enc_ms": enc_ms}
            print(f"{name}/{mode:<12}{img_ms:>9.1f}{open_cnt:>10.2f}{open_ms:>13.2f}{share:>11.1f}%{enc_ms:>12.1f}")

    out = CORPUS / "results.json"
    out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(f"\nresults: {out}")


if __name__ == "__main__":
    main()
