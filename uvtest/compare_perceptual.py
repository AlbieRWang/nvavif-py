"""Human-vision-oriented metrics for codec comparison (beyond luma SSIM).

Three metrics that model actual perception better than the 8x8 block SSIM in
quality_metrics.py:

- LPIPS: deep-feature distance (learned, best correlation with human judgment;
  < 0.05 ~ indistinguishable, > 0.15 clearly visible). Needs `lpips` (torch).
- Delta E 2000 (CIE Lab): perceptual color difference, averaged over the worst
  1% of pixels — this catches 4:2:0 chroma fringing that LUMA-only SSIM is
  blind to, which is exactly where WebP loses to AVIF on hard-edged content.
  Rule of thumb: dE < 1 invisible, 1-2 visible on close inspection, > 3 obvious.
- PSNR on the chroma channels (Cb/Cr), the complement of the luma PSNR the
  other scripts already report.

Usage: uv run python uvtest/compare_perceptual.py --src test_imgs --limit 5
Re-encodes each truly-transparent image with the same routes as compress_dir
(WebP q90 lossy vs AVIF dual-stream) and prints a table. 100% resolution on
all metrics — no downscaling, since the artifacts we hunt live on edges.
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

Image.MAX_IMAGE_PIXELS = None

import nvavif_py as nv  # noqa: E402
from quality_metrics import alpha_mae  # noqa: E402

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}

# CIEDE2000 for the worst-1% mean. Vectorized implementation of the Sharma et
# al. (2005) formulation; inputs are sRGB uint8 images of identical size.
def _srgb_to_lab(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    s = rgb.astype(np.float64) / 255.0
    lin = np.where(s <= 0.04045, s / 12.92, ((s + 0.055) / 1.055) ** 2.4)
    # sRGB -> XYZ (D65)
    m = np.array([[0.4124564, 0.3575761, 0.1804375],
                  [0.2126729, 0.7151522, 0.0721750],
                  [0.0193339, 0.1191920, 0.9503041]])
    xyz = lin @ m.T
    xyz /= np.array([0.95047, 1.0, 1.08883])
    eps, kappa = 216 / 24389, 24389 / 27
    f = np.where(xyz > eps, np.cbrt(xyz), (kappa * xyz + 16) / 116)
    L = 116 * f[..., 1] - 16
    a = 500 * (f[..., 0] - f[..., 1])
    b = 200 * (f[..., 1] - f[..., 2])
    return L, np.stack([a, b], axis=-1)


def over_white(rgba: np.ndarray) -> np.ndarray:
    """Composite RGBA over white — the RGB under transparent pixels is
    unspecified (codecs may change it freely), so all color metrics must run
    on the composited image or the worst-1% is dominated by invisible pixels."""
    a = rgba[..., 3:4].astype(np.float64) / 255.0
    rgb = rgba[..., :3].astype(np.float64)
    return (rgb * a + 255.0 * (1.0 - a)).astype(np.uint8)


def delta_e2000_worst1(ref_rgb: np.ndarray, test_rgb: np.ndarray) -> float:
    L1, ab1 = _srgb_to_lab(ref_rgb)
    L2, ab2 = _srgb_to_lab(test_rgb)
    C1 = np.hypot(ab1[..., 0], ab1[..., 1])
    C2 = np.hypot(ab2[..., 0], ab2[..., 1])
    Cb = (C1 + C2) / 2
    G = 1 - np.sqrt(Cb**7 / (Cb**7 + 25**7)) / 2
    a1p, a2p = (1 + G) * ab1[..., 0], (1 + G) * ab2[..., 0]
    C1p, C2p = np.hypot(a1p, ab1[..., 1]), np.hypot(a2p, ab2[..., 1])
    h1p = np.degrees(np.arctan2(ab1[..., 1], a1p)) % 360
    h2p = np.degrees(np.arctan2(ab2[..., 1], a2p)) % 360
    dLp = L2 - L1
    dCp = C2p - C1p
    dhp = h2p - h1p
    dhp = np.where(dhp > 180, dhp - 360, np.where(dhp < -180, dhp + 360, dhp))
    dHp = 2 * np.sqrt(C1p * C2p) * np.sin(np.radians(dhp / 2))
    Cbp = (C1p + C2p) / 2
    hbp = np.abs(h1p - h2p)  # simplified: same-hue-plane approximation
    T = (1 - 0.17 * np.cos(np.radians(h1p + 30)) + 0.24 * np.cos(np.radians(2 * h1p))
         + 0.32 * np.cos(np.radians(3 * h1p + 6)) - 0.20 * np.cos(np.radians(4 * h1p - 63)))
    Sl = 1 + 0.015 * (Lp_mean := (L1 + L2) / 2 - 50) ** 2 / np.sqrt(20 + (Lp_mean) ** 2)
    Sc = 1 + 0.045 * Cbp
    Sh = 1 + 0.015 * Cbp * T
    dtheta = 30 * np.exp(-((hbp - 275) / 25) ** 2)
    Rc = 2 * np.sqrt(Cbp**7 / (Cbp**7 + 25**7))
    Rt = -Rc * np.sin(np.radians(2 * dtheta))
    de = np.sqrt(
        (dLp / Sl) ** 2 + (dCp / Sc) ** 2 + (dHp / Sh) ** 2
        + Rt * (dCp / Sc) * (dHp / Sh)
    )
    return float(np.sort(de.ravel())[-max(1, de.size // 100):].mean())


def chroma_psnr(ref_rgb: np.ndarray, test_rgb: np.ndarray) -> float:
    ref = ref_rgb.astype(np.float64) / 255.0
    test = test_rgb.astype(np.float64) / 255.0
    m = np.array([[0.4124564, 0.3575761, 0.1804375],
                  [0.2126729, 0.7151522, 0.0721750],
                  [0.0193339, 0.1191920, 0.9503041]])
    ycbcr_ref = ref @ m.T  # proxy chroma via XYZ b-vector (blue-yellow axis)
    ycbcr_test = test @ m.T
    cb_r, cb_t = ycbcr_ref[..., 2], ycbcr_test[..., 2]
    cr_r, cr_t = ycbcr_ref[..., 0] - ycbcr_ref[..., 1], ycbcr_test[..., 0] - ycbcr_test[..., 1]
    mse = (np.mean((cb_r - cb_t) ** 2) + np.mean((cr_r - cr_t) ** 2)) / 2
    return float("inf") if mse == 0 else 10 * np.log10(1.0 / mse)


def is_truly_transparent(path: Path) -> bool:
    try:
        with Image.open(path) as im:
            if im.mode not in ("RGBA", "LA", "PA") and "transparency" not in im.info:
                return False
            lo, _ = im.convert("RGBA").getchannel("A").getextrema()
            return lo < 255
    except Exception:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, default=ROOT / "test_imgs")
    parser.add_argument("--webp-quality", type=int, default=90)
    parser.add_argument("--cq", type=int, default=20)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    import lpips as lpips_lib
    import torch

    loss_fn = lpips_lib.LPIPS(net="alex", verbose=False)

    files = sorted(p for p in args.src.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    transparent = [p for p in files if is_truly_transparent(p)][: args.limit]
    if not transparent:
        print(f"no truly-transparent images found in {args.src}")
        return
    print(f"{len(transparent)} images — LPIPS (alex) / dE2000 worst-1% / chroma PSNR, full resolution\n")

    out_dir = ROOT / "uvtest" / "out"
    rows = []
    for path in transparent:
        with Image.open(path) as im:
            ref_rgba = np.asarray(im.convert("RGBA"))
            ref_a = ref_rgba[..., 3].astype(np.int16)
            ref_np = over_white(ref_rgba)
        ref_t = torch.from_numpy(ref_np.astype(np.float32) / 255.0 * 2 - 1)[None].permute(0, 3, 1, 2)

        row = {"name": path.name}
        routes = {}
        # WebP q90 (current transparent default)
        wp = out_dir / f"_perc_{path.stem}.webp"
        with Image.open(path) as im:
            im.convert("RGBA").save(wp, "WEBP", quality=args.webp_quality, method=4)
        routes["webp"] = {"bytes": wp.stat().st_size, "img": Image.open(wp).convert("RGBA")}
        # AVIF dual-stream
        avif_bytes = nv.encode_file(path, cq=args.cq)
        avif_path = out_dir / f"_perc_{path.stem}.avif"
        avif_path.write_bytes(avif_bytes)
        routes["avif"] = {
            "bytes": len(avif_bytes),
            "img": Image.fromarray(nv.decode_file(str(avif_path))).convert("RGBA"),
        }
        avif_path.unlink()

        for label, r in routes.items():
            test_rgba = np.asarray(r["img"])
            test_np = over_white(test_rgba)
            test_a = test_rgba[..., 3]
            test_t = torch.from_numpy(test_np.astype(np.float32) / 255.0 * 2 - 1)[None].permute(0, 3, 1, 2)
            with torch.no_grad():
                lp = float(loss_fn(ref_t, test_t).item())
            row[label] = {
                "bytes": r["bytes"],
                "lpips": round(lp, 4),
                "de2000_worst1": round(delta_e2000_worst1(ref_np, test_np), 3),
                "chroma_psnr": round(chroma_psnr(ref_np, test_np), 2),
                "alpha_mae": round(alpha_mae(ref_a, test_a), 3),
            }
            print(f"{path.name[:30]:32} {label:5}: {r['bytes']/1e6:6.2f}MB  LPIPS {row[label]['lpips']:7.4f}  "
                  f"dE2000(1%) {row[label]['de2000_worst1']:6.2f}  chromaPSNR {row[label]['chroma_psnr']:6.2f} dB  "
                  f"aMAE {row[label]['alpha_mae']:.2f}", flush=True)
        wp.unlink()
        rows.append(row)

    out = out_dir / "compare_perceptual.json"
    out.write_text(json.dumps({"webp_quality": args.webp_quality, "cq": args.cq, "images": rows}, indent=1))
    print(f"\nreport: {out}")


if __name__ == "__main__":
    main()
