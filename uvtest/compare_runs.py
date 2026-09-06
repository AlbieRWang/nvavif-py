"""Compare the outputs of two compress_dir runs (e.g. AVIF pipeline vs
all-WebP pipeline) against the same source folder.

For every source image that both runs encoded (kept_source rows are skipped —
their output files do not exist) this reports output bytes and the shared
quality metrics: 8x8 block luma SSIM on a 4x box-downscaled plane (same
metric family as the rest of uvtest, see OPTIMIZATION_PROPOSALS metric note)
and alpha MAE at full resolution for RGBA sources.

A perceptual subset (sources small enough for CPU LPIPS) additionally gets
composited-over-white LPIPS + dE2000 worst-1% — RGBA metrics must composite:
the RGB under transparent pixels is unspecified.

Usage: uv run python uvtest/compare_runs.py --a uvtest/out/compressed --b uvtest/out/compressed_webp
"""

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _dll_dir in (ROOT / "ffmpeg-out" / "bin", ROOT / "msys64" / "mingw64" / "bin"):
    if _dll_dir.is_dir():
        os.add_dll_directory(str(_dll_dir))

import numpy as np
from PIL import Image  # noqa: E402

Image.MAX_IMAGE_PIXELS = None

import nvavif_py as nv  # noqa: E402
from quality_metrics import alpha_mae, luma_box, ssim_blocks  # noqa: E402

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
OUT_EXTS = ("avif", "webp", "png")


def load_run_formats(run_dir: Path) -> dict[str, str]:
    """source name -> output format, from the run's report (authoritative).

    Output folders can contain stale files from older runs with different
    routing (e.g. pre-WebP-default transparent AVIFs); the suffix-guessing
    fallback picked those up. The report is the ground truth for what today's
    run actually wrote.
    """
    rep = run_dir / "compress_report.json"
    if not rep.exists():
        return {}
    try:
        data = json.loads(rep.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {r["name"]: r.get("format") for r in data.get("images", []) if r.get("action") == "encoded"}


def find_output(run_dir: Path, src: Path, formats: dict[str, str]) -> Path | None:
    cands = [run_dir / f"{src.stem}.{e}" for e in OUT_EXTS if (run_dir / f"{src.stem}.{e}").exists()]
    if not cands:
        return None
    want = formats.get(src.name)
    for c in cands:
        if c.suffix == f".{want}":
            return c
    return max(cands, key=lambda p: p.stat().st_mtime)


def load_rgb(path: Path) -> Image.Image:
    if path.suffix == ".avif":
        return Image.fromarray(nv.decode_file(str(path))).convert("RGB")
    with Image.open(path) as im:
        return im.convert("RGB").copy()


def load_rgba(path: Path) -> Image.Image:
    if path.suffix == ".avif":
        return Image.fromarray(nv.decode_file(str(path))).convert("RGBA")
    with Image.open(path) as im:
        return im.convert("RGBA").copy()


def crop_pair(ref: Image.Image, test: Image.Image) -> tuple[Image.Image, Image.Image]:
    # Odd-dimension sources decode one row/column short on the 4:2:0 AVIF
    # path — align both to the common area before any full-res metric.
    w, h = min(ref.width, test.width), min(ref.height, test.height)
    return ref.crop((0, 0, w, h)), test.crop((0, 0, w, h))


def maybe_downscale(ref: Image.Image, test: Image.Image, max_mp: float) -> tuple[Image.Image, Image.Image]:
    # LPIPS runs on CPU here; keep the pair at full res when small, else box-
    # downscale both by the SAME factor (relative comparison survives).
    mp = ref.width * ref.height / 1e6
    if mp <= max_mp:
        return ref, test
    s = (max_mp / mp) ** 0.5
    size = (max(1, int(ref.width * s)), max(1, int(ref.height * s)))
    return ref.resize(size, Image.BOX), test.resize(size, Image.BOX)


def _compare_one(job: dict) -> dict:
    """Pool worker: SSIM + alpha MAE for one source against both runs' outputs."""
    path, pa, pb = Path(job["path"]), Path(job["pa"]), Path(job["pb"])
    with Image.open(path) as im:
        src_w, src_h = im.width, im.height
        has_alpha = im.mode in ("RGBA", "LA", "PA")
    ref_rgba = load_rgba(path) if has_alpha else None

    row = {"name": path.name, "mp": round(src_w * src_h / 1e6, 2)}
    ssim_s = 0.0  # metric compute only — decode/crop time is reported separately by the wall clock
    for label, out_path in ((job["label_a"], pa), (job["label_b"], pb)):
        img = load_rgba(out_path) if has_alpha else load_rgb(out_path)
        ref_img = ref_rgba if has_alpha else load_rgb(path)
        ref_img, img = crop_pair(ref_img, img)
        t0 = time.perf_counter()
        entry = {
            "bytes": out_path.stat().st_size,
            "ssim": round(ssim_blocks(luma_box(ref_img.convert("RGB"), factor=4), luma_box(img.convert("RGB"), factor=4)), 4),
        }
        ssim_s += time.perf_counter() - t0
        if has_alpha:
            ref_a = np.asarray(ref_img.getchannel("A"), dtype=np.int16)
            entry["alpha_mae"] = round(alpha_mae(ref_a, np.asarray(img.getchannel("A"), dtype=np.int16)), 3)
        row[label] = entry
    row["ssim_s"] = round(ssim_s, 3)
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, default=ROOT / "test_imgs")
    parser.add_argument("--a", type=Path, required=True, help="first run's output folder")
    parser.add_argument("--b", type=Path, required=True, help="second run's output folder")
    parser.add_argument("--label-a", default="a")
    parser.add_argument("--label-b", default="b")
    parser.add_argument("--perceptual-mp", type=float, default=4.0, help="max source megapixels for the LPIPS/dE2000 subset")
    parser.add_argument("--perceptual-limit", type=int, default=8, help="max images in the perceptual subset")
    parser.add_argument("--workers", type=int, default=8, help="parallel compare processes (default: 8)")
    parser.add_argument(
        "--de2000",
        action="store_true",
        default=False,
        help="also compute dE2000 worst-1% in the perceptual subset (default: off — it is by far the most expensive metric, ~3-6x LPIPS, and LPIPS already covers the perceptual verdict; enable when specifically hunting 4:2:0 chroma fringing)",
    )
    args = parser.parse_args()

    if args.perceptual_limit > 0:
        import torch
        import lpips as lpips_lib

        loss_fn = lpips_lib.LPIPS(net="alex", verbose=False)

    from compare_perceptual import delta_e2000_worst1, over_white

    files = sorted(p for p in args.src.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    fmt_a, fmt_b = load_run_formats(args.a), load_run_formats(args.b)
    jobs = []
    for path in files:
        pa, pb = find_output(args.a, path, fmt_a), find_output(args.b, path, fmt_b)
        if pa is None or pb is None:
            continue  # kept_source / not encoded in one of the runs
        jobs.append({"path": str(path), "pa": str(pa), "pb": str(pb), "label_a": args.label_a, "label_b": args.label_b})

    # Decode+SSIM per image is independent and CPU-bound (100 MP sources take
    # tens of seconds serially) — same multiprocess pattern as compress_dir.
    rows: list[dict] = []
    t0 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for row in pool.map(_compare_one, jobs):
            rows.append(row)
    compare_s = time.perf_counter() - t0
    rows.sort(key=lambda r: r["name"])
    for row in rows:
        a, b = row[args.label_a], row[args.label_b]
        extra = f"  aMAE {a.get('alpha_mae', '-'):>5} / {b.get('alpha_mae', '-'):>5}" if "alpha_mae" in a else ""
        print(
            f"{row['name'][:28]:30} {row['mp']:7.2f}MP  {args.label_a}: {a['bytes']/1e6:6.2f}MB s{a['ssim']:.4f} | "
            f"{args.label_b}: {b['bytes']/1e6:6.2f}MB s{b['ssim']:.4f}{extra}",
            flush=True,
        )

    # Perceptual subset: composited LPIPS + dE2000, prioritizing the images
    # where the cheap luma SSIM disagreed the most (that is where a second
    # opinion matters); sources above --perceptual-mp are box-downscaled by a
    # common factor so CPU LPIPS stays feasible.
    subset = sorted(rows, key=lambda r: min(r[args.label_a]["ssim"], r[args.label_b]["ssim"]))[: args.perceptual_limit]
    perc_t0 = time.perf_counter()
    lpips_s = de_s = 0.0
    for r in subset:
        path = args.src / r["name"]
        for label, run_dir in ((args.label_a, args.a), (args.label_b, args.b)):
            out_path = find_output(run_dir, path, fmt_a if run_dir == args.a else fmt_b)
            img = load_rgba(out_path)
            ref = load_rgba(path)
            ref, img = crop_pair(ref, img)
            ref, img = maybe_downscale(ref, img, args.perceptual_mp)
            ref_np, test_np = over_white(np.asarray(ref)), over_white(np.asarray(img))
            ref_t = torch.from_numpy(ref_np.astype(np.float32) / 255 * 2 - 1)[None].permute(0, 3, 1, 2)
            test_t = torch.from_numpy(test_np.astype(np.float32) / 255 * 2 - 1)[None].permute(0, 3, 1, 2)
            t0 = time.perf_counter()
            with torch.no_grad():
                lp = float(loss_fn(ref_t, test_t).item())
            lp_dt = time.perf_counter() - t0
            lpips_s += lp_dt
            de = de_dt = 0.0
            if args.de2000:
                t0 = time.perf_counter()
                de = round(delta_e2000_worst1(ref_np, test_np), 2)
                de_dt = time.perf_counter() - t0
                de_s += de_dt
            r[label]["perceptual"] = {"lpips": round(lp, 4), "lpips_s": round(lp_dt, 3), "de2000_worst1": de, "de_s": round(de_dt, 3)}
    perc_s = time.perf_counter() - perc_t0

    def agg(key, sub=None):
        pool = subset if sub else rows
        vals = [(r[args.label_a][sub or key], r[args.label_b][sub or key]) for r in pool]
        va, vb = zip(*vals) if vals else ((0,), (0,))
        return sum(va) / len(va), sum(vb) / len(vb)

    ba, bb = sum(r[args.label_a]["bytes"] for r in rows), sum(r[args.label_b]["bytes"] for r in rows)
    sa, sb = agg("ssim")
    print(f"\n=== {len(rows)} images, {sum(r['mp'] for r in rows):.1f} MP, compared in {compare_s:.1f} s ({args.workers} workers) ===")
    print(f"bytes : {args.label_a} {ba/1e6:.1f} MB | {args.label_b} {bb/1e6:.1f} MB ({ba/max(bb,1):.2f}x)")
    print(f"SSIM  : {args.label_a} mean {sa:.4f} | {args.label_b} mean {sb:.4f}")
    worst = sorted(rows, key=lambda r: min(r[args.label_a]["ssim"], r[args.label_b]["ssim"]))[:5]
    for r in worst:
        print(f"  worst: {r['name'][:28]:30} {args.label_a} {r[args.label_a]['ssim']:.4f} | {args.label_b} {r[args.label_b]['ssim']:.4f}")
    lp = de = 0.0
    if subset:
        keys = [("lpips", ".4f")] + ([("de2000_worst1", ".2f")] if args.de2000 else [])
        for key, fmt in keys:
            va, vb = zip(*[(r[args.label_a]["perceptual"][key], r[args.label_b]["perceptual"][key]) for r in subset])
            print(f"{key:14}: {args.label_a} mean {sum(va)/len(va):{fmt}} | {args.label_b} mean {sum(vb)/len(vb):{fmt}}  (worst-SSIM subset, {len(subset)} images)")
        lp, de = lpips_s, de_s
    ssim_t = sum(r["ssim_s"] for r in rows)
    print(
        f"metric time   : SSIM {ssim_t:.1f} s | LPIPS {lp:.1f} s | dE2000 {de:.1f} s "
        f"(CPU sums across {args.workers} workers; wall {compare_s:.1f} s main + {perc_s:.1f} s perceptual)"
    )
    out = ROOT / "uvtest" / "out" / "compare_runs.json"
    out.write_text(json.dumps({"label_a": args.label_a, "label_b": args.label_b, "images": rows}, indent=1))
    print(f"report: {out}")


if __name__ == "__main__":
    main()
