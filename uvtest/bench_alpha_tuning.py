"""Benchmark nvavif transparent-image encoding for alpha speed tuning.

Encodes every real-transparent image in test_imgs, records encode time and
file size, then decodes and measures alpha fidelity against the source.
Run before and after a rav1e alpha tuning change and compare the JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]

# Local wheels are not delvewheel-repaired; register the project DLL paths.
for _dll_dir in (ROOT / "ffmpeg-out" / "bin", ROOT / "msys64" / "mingw64" / "bin"):
    if _dll_dir.is_dir() and hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(_dll_dir))

import nvavif_py


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "uvtest" / "out" / "alpha_tuning"


def find_transparent_sources() -> list[Path]:
    sources = []
    for candidate in sorted((ROOT / "test_imgs").iterdir()):
        try:
            with Image.open(candidate) as image:
                if "A" not in image.getbands():
                    continue
                alpha = np.array(image.convert("RGBA"))[:, :, 3]
                if alpha.min() < 255:
                    sources.append(candidate)
        except (OSError, ValueError):
            continue
    return sources


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="run")
    args = parser.parse_args()

    sources = find_transparent_sources()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"transparent sources: {len(sources)}")
    rows = []
    for source in sources:
        with Image.open(source) as image:
            rgba = np.array(image.convert("RGBA"))
        width, height = image.size
        megapixels = width * height / 1_000_000

        started = time.perf_counter()
        encoded = nvavif_py.encode_file(source)
        encode_s = time.perf_counter() - started

        temp_avif = OUTPUT_DIR / f"{source.stem}.avif"
        temp_avif.write_bytes(encoded)
        # Use the library's own FFmpeg/dav1d decode path for fidelity checks;
        # Pillow's AVIF plugin showed a separate alpha anomaly on two samples.
        decoded = nvavif_py.decode_file(str(temp_avif))
        decoded = np.asarray(decoded)[..., :4] if decoded.shape[2] >= 4 else None
        alpha_error = np.abs(decoded[:, :, 3].astype(int) - rgba[:, :, 3].astype(int))

        rows.append(
            {
                "name": source.name,
                "mp": round(megapixels, 2),
                "encode_s": round(encode_s, 3),
                "mps": round(megapixels / encode_s, 2),
                "bytes": len(encoded),
                "alpha_mae": round(float(alpha_error.mean()), 3),
                "alpha_max": int(alpha_error.max()),
            }
        )
        print(
            f"{source.name[:44]:44} {megapixels:7.2f} MP  {encode_s:7.3f} s  "
            f"{megapixels / encode_s:6.2f} MP/s  {len(encoded):9,} B  "
            f"alpha MAE {alpha_error.mean():6.3f} max {alpha_error.max()}"
        )

    total_mp = sum(r["mp"] for r in rows)
    total_s = sum(r["encode_s"] for r in rows)
    summary = {
        "tag": args.tag,
        "images": len(rows),
        "total_mp": round(total_mp, 2),
        "total_s": round(total_s, 3),
        "throughput_mps": round(total_mp / total_s, 2),
        "median_encode_s": sorted(r["encode_s"] for r in rows)[len(rows) // 2],
        "mean_alpha_mae": round(sum(r["alpha_mae"] for r in rows) / len(rows), 4),
        "total_bytes": sum(r["bytes"] for r in rows),
        "rows": rows,
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report = OUTPUT_DIR / f"{args.tag}.json"
    report.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(
        f"\n[{args.tag}] total {total_s:.2f} s for {total_mp:.1f} MP -> "
        f"{summary['throughput_mps']} MP/s, mean alpha MAE {summary['mean_alpha_mae']}"
    )
    print(f"report: {report}")


if __name__ == "__main__":
    main()
