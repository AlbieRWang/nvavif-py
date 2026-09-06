"""Compare whole-image WebP vs the AVIF dual-stream path for transparency.

For every truly-transparent image in the source folder (alpha min < 255,
detected from the Pillow header + a full alpha scan):
  A. WebP q<quality> method=<m> — whole-image route (--transparent-format webp)
  B. AVIF cq=<cq> — GPU color + CPU rav1e alpha dual-stream (transparent AVIF)

Reports size, encode time, alpha fidelity (MAE, 0 = lossless) and color
fidelity (8x8 block SSIM on 2x box-downscaled luma). Measured 2026-09-06 on
the hard-edged transparent test set: WebP wins size/speed/alpha-losslessness,
AVIF wins color SSIM by a structural margin (VP8 4:2:0 + block artifacts);
see OPTIMIZATION_PROPOSALS I4.

Usage: uv run python uvtest/compare_transparent.py [--webp-quality 90] [--limit 10]
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
from quality_metrics import alpha_mae, alpha_plane, luma_box, ssim_blocks  # noqa: E402

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def is_truly_transparent(path: Path) -> bool:
    try:
        with Image.open(path) as im:
            has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
            if not has_alpha:
                return False
            lo, _ = im.convert("RGBA").getchannel("A").getextrema()
            return lo < 255
    except Exception:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, default=ROOT / "test_imgs")
    parser.add_argument("--webp-quality", type=int, default=90)
    parser.add_argument("--method", type=int, default=4, help="libwebp effort 0-6")
    parser.add_argument("--cq", type=int, default=20, help="AVIF cq for the dual-stream path")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    files = sorted(p for p in args.src.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    transparent = [p for p in files if is_truly_transparent(p)][: args.limit]
    if not transparent:
        print(f"no truly-transparent images found in {args.src}")
        return
    print(f"{len(transparent)} truly-transparent images (webp q{args.webp_quality} vs avif cq{args.cq})\n")

    out_dir = ROOT / "uvtest" / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    tot_webp_b = tot_avif_b = 0
    tot_webp_t = tot_avif_t = 0.0
    for path in transparent:
        with Image.open(path) as im:
            rgba = im.convert("RGBA")
            ref_a = alpha_plane(rgba)
            ref_l = luma_box(rgba, factor=2)

        t0 = time.perf_counter()
        webp_path = out_dir / f"_cmp_{path.stem}.webp"
        rgba.save(webp_path, "WEBP", quality=args.webp_quality, method=args.method)
        t_webp = time.perf_counter() - t0
        with Image.open(webp_path) as im2:
            wimg = im2.convert("RGBA")
        webp = {
            "bytes": webp_path.stat().st_size,
            "encode_s": round(t_webp, 2),
            "alpha_mae": alpha_mae(ref_a, alpha_plane(wimg)),
            "ssim": round(ssim_blocks(ref_l, luma_box(wimg, factor=2)), 4),
        }
        webp_path.unlink()

        t0 = time.perf_counter()
        avif_bytes = nv.encode_file(path, cq=args.cq)
        t_avif = time.perf_counter() - t0
        avif_path = out_dir / f"_cmp_{path.stem}.avif"
        avif_path.write_bytes(avif_bytes)
        decoded = Image.fromarray(nv.decode_file(str(avif_path))).convert("RGBA")
        avif_path.unlink()
        avif = {
            "bytes": len(avif_bytes),
            "encode_s": round(t_avif, 2),
            "alpha_mae": alpha_mae(ref_a, alpha_plane(decoded)),
            "ssim": round(ssim_blocks(ref_l, luma_box(decoded, factor=2)), 4),
        }

        rows.append({"name": path.name, "webp": webp, "avif": avif})
        tot_webp_b += webp["bytes"]; tot_avif_b += avif["bytes"]
        tot_webp_t += t_webp; tot_avif_t += t_avif
        print(
            f"{path.name[:30]:32} WebP: {webp['bytes']/1e6:5.2f}MB {t_webp:5.1f}s "
            f"aMAE {webp['alpha_mae']:5.2f} SSIM {webp['ssim']:.4f} | "
            f"AVIF: {avif['bytes']/1e6:5.2f}MB {t_avif:5.1f}s "
            f"aMAE {avif['alpha_mae']:5.2f} SSIM {avif['ssim']:.4f}",
            flush=True,
        )

    n = len(rows)
    print(
        f"\nTOTAL  WebP: {tot_webp_b/1e6:.1f}MB {tot_webp_t:.1f}s | "
        f"AVIF: {tot_avif_b/1e6:.1f}MB {tot_avif_t:.1f}s | "
        f"SSIM min: webp {min(r['webp']['ssim'] for r in rows):.4f} vs "
        f"avif {min(r['avif']['ssim'] for r in rows):.4f}"
    )
    out = out_dir / "compare_transparent.json"
    out.write_text(json.dumps({"webp_quality": args.webp_quality, "method": args.method, "cq": args.cq, "images": rows}, indent=1))
    print(f"report: {out}")


if __name__ == "__main__":
    main()
