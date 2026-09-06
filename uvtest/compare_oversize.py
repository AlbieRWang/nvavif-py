"""Compare WebP vs the AVIF CPU fallback for oversized (>8192) images.

Images beyond the NVENC 8192 dimension cap can never use the GPU; the AVIF
route therefore encodes entirely on rav1e (~36-39 s per 100 MP image). WebP
covers dimensions up to 16383 and was measured ~3x faster and 2-3x smaller
with a small SSIM cost (OPTIMIZATION_PROPOSALS I4). This script re-runs that
comparison on any folder: for every image with a side in (8192, 16383] it
encodes both routes and reports size, encode time and color fidelity.

Usage: uv run python uvtest/compare_oversize.py [--webp-quality 80] [--limit 4]
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

from PIL import Image  # noqa: E402

import nvavif_py as nv  # noqa: E402
from quality_metrics import luma_box, ssim_blocks  # noqa: E402

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
NVENC_MAX_DIMENSION = 8192
WEBP_MAX_DIMENSION = 16383


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, default=ROOT / "test_imgs")
    parser.add_argument("--webp-quality", type=int, default=80)
    parser.add_argument("--method", type=int, default=2, help="libwebp effort (2: fast; >=3 explodes on some content)")
    parser.add_argument("--cq", type=int, default=20, help="AVIF cq for the CPU fallback")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    files = sorted(p for p in args.src.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    oversize = []
    for p in files:
        try:
            with Image.open(p) as im:
                if max(im.width, im.height) > NVENC_MAX_DIMENSION and max(im.width, im.height) <= WEBP_MAX_DIMENSION:
                    oversize.append((p, im.width, im.height))
        except Exception:
            continue
        if args.limit and len(oversize) >= args.limit:
            break
    if not oversize:
        print(f"no images in ({NVENC_MAX_DIMENSION}, {WEBP_MAX_DIMENSION}] found in {args.src}")
        return
    print(f"{len(oversize)} oversized images (webp q{args.webp_quality} m{args.method} vs avif cq{args.cq} CPU fallback)\n")

    out_dir = ROOT / "uvtest" / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    tot_webp_b = tot_avif_b = 0
    tot_webp_t = tot_avif_t = 0.0
    for path, width, height in oversize:
        ref_l = luma_box(Image.open(path).convert("RGB"), factor=4)

        t0 = time.perf_counter()
        webp_path = out_dir / f"_cmp_{path.stem}.webp"
        with Image.open(path) as im:
            im.convert("RGB").save(webp_path, "WEBP", quality=args.webp_quality, method=args.method)
        t_webp = time.perf_counter() - t0
        with Image.open(webp_path) as im2:
            webp_ssim = ssim_blocks(ref_l, luma_box(im2.convert("RGB"), factor=4))
        webp = {"bytes": webp_path.stat().st_size, "encode_s": round(t_webp, 2), "ssim": round(webp_ssim, 4)}
        webp_path.unlink()

        t0 = time.perf_counter()
        avif_bytes = nv.encode_file(path, cq=args.cq)
        t_avif = time.perf_counter() - t0
        avif_path = out_dir / f"_cmp_{path.stem}.avif"
        avif_path.write_bytes(avif_bytes)
        decoded = Image.fromarray(nv.decode_file(str(avif_path))).convert("RGB")
        avif_path.unlink()
        avif = {
            "bytes": len(avif_bytes),
            "encode_s": round(t_avif, 2),
            "ssim": round(ssim_blocks(ref_l, luma_box(decoded, factor=4)), 4),
        }

        rows.append({"name": path.name, "width": width, "height": height, "webp": webp, "avif": avif})
        tot_webp_b += webp["bytes"]; tot_avif_b += avif["bytes"]
        tot_webp_t += t_webp; tot_avif_t += t_avif
        print(
            f"{path.name[:30]:32} {width}x{height} WebP: {webp['bytes']/1e6:5.2f}MB {t_webp:5.1f}s SSIM {webp['ssim']:.4f} | "
            f"AVIF: {avif['bytes']/1e6:5.2f}MB {t_avif:5.1f}s SSIM {avif['ssim']:.4f}",
            flush=True,
        )

    print(
        f"\nTOTAL  WebP: {tot_webp_b/1e6:.1f}MB {tot_webp_t:.1f}s | "
        f"AVIF: {tot_avif_b/1e6:.1f}MB {tot_avif_t:.1f}s | "
        f"speedup {tot_avif_t / max(tot_webp_t, 1e-6):.1f}x"
    )
    out = out_dir / "compare_oversize.json"
    out.write_text(json.dumps({"webp_quality": args.webp_quality, "method": args.method, "cq": args.cq, "images": rows}, indent=1))
    print(f"report: {out}")


if __name__ == "__main__":
    main()
