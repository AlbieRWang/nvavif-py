"""Temp B3 probe: per-channel RGB MAE + luma SSIM + size for GPU AVIF cq=20.

Run before and after the 420 box-average change; current installed wheel is
the baseline. Usage: python uvtest/_b3_metric.py out.json
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nvavif_py as nv
from quality_metrics import ssim_blocks

ROOT = Path(__file__).resolve().parent.parent
Image.MAX_IMAGE_PIXELS = None


def synthetic_chroma() -> Image.Image:
    """Saturated red/blue checker + color ramps — maximizes chroma sampling error."""
    w = h = 512
    yy, xx = np.mgrid[0:h, 0:w]
    checker = ((xx // 32 + yy // 32) % 2).astype(np.float32)
    ramp_x = xx / (w - 1)
    ramp_y = yy / (h - 1)
    r = checker * 255 * ramp_x + (1 - checker) * 30
    g = ramp_y * 255 * (1 - checker) + checker * 40
    b = (1 - checker) * 255 * (1 - ramp_x) + checker * 220
    arr = np.stack([r, g, b], axis=-1).clip(0, 255).astype(np.uint8)
    return Image.fromarray(arr, "RGB")


def real_sources() -> list[Path]:
    imgs = sorted((ROOT / "test_imgs").glob("*.jpg"))
    # medium-size spread: first, middle, last
    picks = [imgs[0], imgs[len(imgs) // 2], imgs[-1]]
    pngs = sorted((ROOT / "test_imgs").glob("*.png"))
    if pngs:
        picks.append(pngs[len(pngs) // 2])
    return picks


def even_crop(img: Image.Image) -> Image.Image:
    w, h = img.size
    return img.crop((0, 0, w - w % 2, h - h % 2)).convert("RGB")


def main() -> None:
    out_path = Path(sys.argv[1])
    rows = []
    sources: list[tuple[str, Image.Image]] = [("synthetic_checker", synthetic_chroma())]
    for p in real_sources():
        try:
            im = Image.open(p)
            im.load()
        except Exception as exc:  # skip unreadable, keep going
            print(f"skip {p.name}: {exc}")
            continue
        if min(im.size) < 130:  # below NVENC min dims
            continue
        sources.append((p.name, even_crop(im)))

    for name, ref in sources:
        w, h = ref.size
        if w > 4096 or h > 4096:
            ref = ref.resize((min(w, 4096), min(h, 4096 * h // w)), Image.LANCZOS)
            w, h = ref.size
        buf = io.BytesIO()
        ref.save(buf, "PNG")
        png_bytes = buf.getvalue()

        avif = nv.encode_file(buf.getvalue(), cq=20)
        tmp_avif = out_path.with_suffix(f".{name}.avif")
        tmp_avif.write_bytes(avif)
        dec = np.asarray(nv.decode_file(tmp_avif))
        ref_arr = np.asarray(ref, dtype=np.int16)
        dec_arr = dec.astype(np.int16)
        ch = min(ref_arr.shape[0], dec_arr.shape[0])
        cw = min(ref_arr.shape[1], dec_arr.shape[1])
        ref_arr = ref_arr[:ch, :cw]
        dec_arr = dec_arr[:ch, :cw]
        mae = [float(np.abs(ref_arr[..., c] - dec_arr[..., c]).mean()) for c in range(3)]
        ref_l = np.asarray(ref.convert("L"), dtype=np.float64)
        dec_l = np.asarray(Image.fromarray(dec.astype(np.uint8)).convert("L"), dtype=np.float64)
        rows.append(
            {
                "name": name,
                "size": [w, h],
                "avif_bytes": len(avif),
                "png_bytes": len(png_bytes),
                "mae_rgb": [round(m, 3) for m in mae],
                "mae_mean": round(sum(mae) / 3, 3),
                "ssim": round(ssim_blocks(ref_l, dec_l), 5),
            }
        )
        print(rows[-1])

    out_path.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
