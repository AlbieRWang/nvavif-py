"""Routing regression tests for compress_dir.py (C3, REVIEW_FINDINGS 2026-09-13).

Script-style asserts — no pytest dependency; any failure raises and exits non-zero.
Runs the real `_encode_task` worker in-process on tiny synthetic images with
device="cpu" so it needs no GPU. Animated/GIF skipping lives in main()'s scan
loop (not the worker) and is intentionally not covered here.

Run: uv run python uvtest/test_compress_dir_routing.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compress_dir as cd

CHECKS = 0
rng = np.random.default_rng(11)


def ok(cond: bool, label: str) -> None:
    global CHECKS
    if not cond:
        raise AssertionError(f"FAIL: {label}")
    CHECKS += 1
    print(f"  ok: {label}")


def make_task(path: Path, out_path: Path, **over: object) -> dict:
    probe = cd.probe_cost(path)
    task = {
        "path": str(path),
        "name": path.name,
        "out_path": str(out_path),
        "in_place": False,
        "cq": 20,
        "auto_quality": False,
        "device": "cpu",
        "preset": 7,
        "keep_smaller": True,
        "copy_skipped": False,
        "transparent_format": "webp",
        "webp_quality": 90,
        "webp_method": 4,
        "webp_lossless_max_mb": 1.0,
        "opaque_format": "avif",
        "opaque_webp_quality": 90,
        "oversize_format": "webp",
        "oversize_webp_quality": 80,
        "oversize_webp_method": 2,
        "oversize_max_edge": 0,
        **probe,
        **over,
    }
    return task


def run_task(task: dict) -> dict:
    res = cd._encode_task(task)
    if not res.get("ok"):
        raise AssertionError(f"worker failed: {res}")
    return res["row"]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="_t_c3_") as tmp:
        tmp = Path(tmp)
        src = tmp / "src"
        dst = tmp / "dst"
        src.mkdir()
        dst.mkdir()

        # --- estimate_jpeg_quality: PIL quality is (near-)IJG scale ---
        print("estimate_jpeg_quality:")
        ref = (np.arange(128 * 128 * 3).reshape(128, 128, 3) % 256).astype(np.uint8)
        im = Image.fromarray(ref, "RGB")
        for q in (50, 95):
            p = src / f"q{q}.jpg"
            im.save(p, "JPEG", quality=q)
            est = cd.estimate_jpeg_quality(p)
            ok(abs(est - q) <= 8, f"quality={q} estimated ~{est:.0f}")

        # --- opaque JPEG -> GPU/CPU AVIF route ---
        print("routing:")
        p = src / "photo.jpg"
        im.save(p, "JPEG", quality=92)
        row = run_task(make_task(p, dst / "photo.avif"))
        ok(row["action"] == "encoded" and row["format"] == "avif", "opaque JPEG -> avif")
        ok((dst / "photo.avif").exists() and (dst / "photo.avif").stat().st_size > 0, "avif written")

        # --- transparent PNG -> whole WebP, alpha preserved (R1) ---
        arr = np.zeros((128, 128, 4), np.uint8)
        yy, xx = np.mgrid[0:128, 0:128]
        m = (xx - 64) ** 2 + (yy - 64) ** 2 < 40**2
        arr[..., 0][m] = 220
        arr[..., 3][m] = 255
        p = src / "trans.png"
        Image.fromarray(arr, "RGBA").save(p)
        row = run_task(make_task(p, dst / "trans.avif"))
        ok(row["action"] == "encoded" and row["format"] == "webp", "transparent PNG -> webp")
        with Image.open(dst / "trans.webp") as out:
            ok("A" in out.getbands() and out.getchannel("A").getextrema()[0] == 0, "webp keeps real alpha")

        # --- constant-opaque RGBA stays on the AVIF route (R7) ---
        # gradient content so the AVIF genuinely wins the keep-smaller guard
        arr_op = np.dstack([ref, np.full((128, 128), 255, np.uint8)])
        p = src / "constopaque.png"
        Image.fromarray(arr_op, "RGBA").save(p)
        row = run_task(make_task(p, dst / "constopaque.avif"))
        ok(row["format"] == "avif" and row["action"] in ("encoded", "kept_source"),
           "constant-opaque RGBA -> avif route (keep-smaller may keep it)")

        # --- opaque WebP route stays RGB, no alpha plane (B8/R8) ---
        # low-frequency noise: WebP q90 reliably beats PNG so the row encodes
        smooth = Image.fromarray(rng.integers(0, 256, (16, 16), dtype=np.uint8), "L").resize((128, 128), Image.BICUBIC)
        arr_rgb = np.dstack([np.asarray(smooth)] * 3)
        p = src / "opaque.png"
        Image.fromarray(arr_rgb, "RGB").save(p)
        row = run_task(make_task(p, dst / "opaque.avif", opaque_format="webp"))
        ok(row["action"] == "encoded" and row["format"] == "webp", "opaque -> webp route")
        with Image.open(dst / "opaque.webp") as out:
            ok("A" not in out.getbands(), "opaque webp has no alpha plane")

        # --- oversize image -> whole WebP, RGB, no resize (R4) ---
        wide = np.asarray(
            Image.fromarray(rng.integers(0, 256, (5, 450), dtype=np.uint8), "L").resize((9000, 100), Image.BICUBIC)
        )
        wide = np.dstack([wide] * 3)
        p = src / "wide.png"
        Image.fromarray(wide, "RGB").save(p)
        row = run_task(make_task(p, dst / "wide.avif"))
        ok(row["action"] == "encoded" and row["format"] == "webp", "oversize -> webp route")
        ok("resized_from" not in row, "oversize_max_edge=0 does not resize")

        # --- keep-smaller guard: solid color PNG survives (R11) ---
        # device="gpu" (the batch default): NVENC's bitstream overhead loses to
        # the tiny PNG; the CPU rav1e fallback actually compresses flat fields
        # below PNG size and would encode instead.
        p = src / "solid.png"
        # 132x70 (above the NVENC minimum) + optimize: 180-byte PNG sits below
        # the ~300-byte AVIF floor at ANY cq, so keep-smaller deterministically
        # keeps the source instead of encoding
        Image.new("RGB", (132, 70), (250, 250, 250)).save(p, optimize=True)
        row = run_task(make_task(p, dst / "solid.avif", device="gpu"))
        ok(row["action"] == "kept_source" and not (dst / "solid.avif").exists(), "keep-smaller keeps source")

    print(f"\n{CHECKS} checks passed")


if __name__ == "__main__":
    main()
