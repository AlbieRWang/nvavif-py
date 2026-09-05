"""Validate OVERSIZE_RAV1E_PRESET (I1): speed and quality of the CPU color
fallback for images larger than the NVENC 8192 dimension cap.

Compares on one oversized source image:
  A. device=auto  -> NVENC attempt fails -> CPU fallback with the new fast
     preset (OVERSIZE_RAV1E_PRESET=4, rav1e speed 7)
  B. device=cpu, preset=P7 -> the old behavior (rav1e speed 4)

Reports encode time, file size, and color fidelity (MAE / PSNR / SSIM on
downscaled luma) so the preset can be judged, not guessed.

Usage: uv run python uvtest/test_oversize_preset.py [--image test_imgs/01.jpg]
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _dll_dir in (ROOT / "ffmpeg-out" / "bin", ROOT / "msys64" / "mingw64" / "bin"):
    if _dll_dir.is_dir():
        os.add_dll_directory(str(_dll_dir))

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None  # oversized sources are the point of this test

import nvavif_py as nv  # noqa: E402


def luma_downscaled(rgb: np.ndarray, max_side: int = 1024) -> np.ndarray:
    h, w, _ = rgb.shape
    scale = min(1.0, max_side / max(h, w))
    im = Image.fromarray(rgb).convert("L")
    im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.BILINEAR)
    return np.asarray(im, dtype=np.float64)


def metrics(ref_l: np.ndarray, test_l: np.ndarray) -> dict:
    mse = float(np.mean((ref_l - test_l) ** 2))
    mae = float(np.mean(np.abs(ref_l - test_l)))
    psnr = float("inf") if mse == 0 else 10 * np.log10(255**2 / mse)
    # global SSIM on the downscaled luma (single-window approximation)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    mu_x, mu_y = ref_l.mean(), test_l.mean()
    vx, vy = ref_l.var(), test_l.var()
    cov = np.mean((ref_l - mu_x) * (test_l - mu_y))
    ssim = ((2 * mu_x * mu_y + c1) * (2 * cov + c2)) / (
        (mu_x**2 + mu_y**2 + c1) * (vx + vy + c2)
    )
    return {"mae": round(mae, 3), "psnr": round(psnr, 2), "ssim": round(float(ssim), 5)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=str(ROOT / "test_imgs" / "01.jpg"))
    parser.add_argument("--cq", type=int, default=20)
    args = parser.parse_args()

    src = np.asarray(Image.open(args.image).convert("RGB"))
    ref_l = luma_downscaled(src)
    print(f"source: {args.image} {src.shape[1]}x{src.shape[0]}")

    results = {}
    for label, kwargs in (
        ("auto_fallback_fast_preset", {"device": "auto"}),
        ("cpu_p7_old_behavior", {"device": "cpu", "preset": 7}),
    ):
        t0 = time.perf_counter()
        data = nv.encode_file(args.image, cq=args.cq, **kwargs)
        elapsed = time.perf_counter() - t0
        tmp = ROOT / "uvtest" / "out" / "oversize_tmp.avif"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        decoded = nv.decode_file(tmp)
        dec_l = luma_downscaled(decoded[:, :, :3])
        m = metrics(ref_l, dec_l)
        results[label] = {
            "encode_s": round(elapsed, 2),
            "bytes": len(data),
            "mb": round(len(data) / 1e6, 2),
            **m,
        }
        print(f"{label}: {elapsed:.1f} s, {len(data)/1e6:.2f} MB, MAE {m['mae']}, PSNR {m['psnr']} dB, SSIM {m['ssim']}")

    a, b = results["auto_fallback_fast_preset"], results["cpu_p7_old_behavior"]
    print(f"\nspeedup: {b['encode_s'] / a['encode_s']:.2f}x")
    print(f"size delta: {(a['bytes'] - b['bytes']) / b['bytes'] * 100:+.1f}%")

    out = ROOT / "uvtest" / "out" / "oversize_preset_check.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"image": args.image, "cq": args.cq, "results": results}, indent=1))
    print(f"report: {out}")


if __name__ == "__main__":
    main()
