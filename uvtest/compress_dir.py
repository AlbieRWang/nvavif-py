"""One-command batch compression: compress a folder of images to AVIF.

Wraps nvavif_py.encode_file for every image in the source directory.
Input formats are whatever Pillow opens (PNG/JPEG/WebP/BMP/...);
transparency is preserved automatically (GPU color + fast CPU alpha).

Images are encoded by a process pool: each worker gets its own NVENC
session, so GPU color encodes of different images overlap, and CPU alpha
encodes overlap with GPU color encodes of other images. The worker count
is capped by the driver's concurrent-NVENC-session limit (~8 on RTX 40
series); oversized (>8192) images never take the GPU anyway.

Usage (from the repo root, single root .venv):
    uv run python uvtest/compress_dir.py                       # test_imgs -> out/compressed
    uv run python uvtest/compress_dir.py --cq 26               # web-quality preset
    uv run python uvtest/compress_dir.py --src DIR --dst DIR   # custom folders
    uv run python uvtest/compress_dir.py --auto-quality 80     # SSIM-targeted quality
    uv run python uvtest/compress_dir.py --workers 4           # cap parallelism
    uv run python uvtest/compress_dir.py --transparent-format webp   # transparent -> WebP (lossless alpha, beat AVIF on the hard-edged transparent set: 3.5 MB/5.0 s vs 6.1 MB/12.3 s)
    uv run python uvtest/compress_dir.py --copy-skipped        # mirror skipped/kept sources into the output folder
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Same DLL registration as bench_alpha_tuning.py. With the repaired wheel
# (dist/repaired-current, installed by setup_env.py) the FFmpeg DLLs are
# bundled and this is a no-op fallback kept for unrepaired dist-local wheels.
# Module level so pool workers (which re-import this file) get it too.
for _dll_dir in (
    ROOT / "ffmpeg-out" / "bin",
    ROOT / "msys64" / "mingw64" / "bin",
):
    if _dll_dir.is_dir():
        os.add_dll_directory(str(_dll_dir))

# NOTE: do not add ROOT to sys.path — the root source package (no .pyd)
# would shadow the nvavif_py wheel installed in the root .venv.

import nvavif_py as nv  # noqa: E402

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}

# Driver cap on concurrent NVENC sessions for consumer GPUs (RTX 40 series).
NVENC_SESSION_LIMIT = 8

# IJG standard luminance quantization table (the baseline every mainstream
# JPEG encoder scales to express its quality setting).
_IJG_LUMA_TABLE = [
    16, 11, 10, 16, 24, 40, 51, 61,
    12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77,
    24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99,
]


def estimate_jpeg_quality(path: Path) -> float:
    """Estimate a JPEG's IJG quality setting from its quantization tables.

    Zero-cost (no pixel decoding). Returns 0 if the tables are unreadable
    (caller should not rely on the estimate).

    IJG mapping: quality <= 50 -> scale = 5000/quality; quality > 50 ->
    scale = 200 - 2*quality. Each table entry is base*scale/100 (clamped
    1..255), so an entry implies a scale; average over entries and invert.
    """
    try:
        from PIL import Image

        with Image.open(path) as im:
            tables = im.quantization
            luma = tables.get(0)
            if not luma or len(luma) != 64:
                return 0.0
            scales = []
            for got, base in zip(luma, _IJG_LUMA_TABLE):
                if base < 1 or got < 1:
                    continue
                scales.append(got * 100.0 / base)
            if not scales:
                return 0.0
            scale = sum(scales) / len(scales)
            if scale < 100:  # quality > 50
                return round(max(50.0, (200.0 - scale) / 2.0))
            return round(min(50.0, 5000.0 / scale))
    except Exception:
        return 0.0


def default_workers() -> int:
    # Half the cores leaves headroom for the in-worker rav1e alpha threads.
    return max(1, min(NVENC_SESSION_LIMIT, (os.cpu_count() or 8) // 2))


def _encode_task(task: dict) -> dict:
    """Pool worker: encode one image, return a per-image report row."""
    from PIL import Image

    path = Path(task["path"])
    out_dir = Path(task["out_path"]).parent
    t0 = time.perf_counter()

    # Transparent images can be routed to WebP/PNG instead of AVIF: WebP
    # codes alpha losslessly, beats AVIF on hard-edged masks (measured
    # 3.5 MB / 5.0 s vs 6.1 MB / 12.3 s on the transparent test set), and
    # carries zero rav1e risk. Opaque images stay on the GPU AVIF path.
    fmt = task["transparent_format"]
    if fmt != "avif":
        with Image.open(path) as im:
            rgba = im.convert("RGBA")
            alpha_lo, _ = rgba.getchannel("A").getextrema()
        if alpha_lo < 255:  # real transparency (constant-opaque stays on AVIF)
            out_path = out_dir / (path.stem + f".{fmt}")
            if fmt == "webp":
                rgba.save(out_path, "WEBP", quality=task["webp_quality"], method=4)
            else:
                rgba.save(out_path, "PNG")
            elapsed = time.perf_counter() - t0
            src_bytes = path.stat().st_size
            out_bytes = out_path.stat().st_size
            if task["keep_smaller"] and out_bytes >= src_bytes:
                out_path.unlink()
                return {
                    "ok": True,
                    "row": {
                        "name": path.name,
                        "action": "kept_source",
                        "format": fmt,
                        "src_bytes": src_bytes,
                        "avif_bytes": out_bytes,
                        "encode_s": round(elapsed, 3),
                    },
                }
            with Image.open(path) as im:
                width, height, mode = im.width, im.height, im.mode
            return {
                "ok": True,
                "row": {
                    "name": path.name,
                    "action": "encoded",
                    "format": fmt,
                    "mode": mode,
                    "alpha": True,
                    "width": width,
                    "height": height,
                    "megapixels": round(width * height / 1e6, 3),
                    "src_bytes": src_bytes,
                    "out_bytes": out_bytes,
                    "ratio": round(src_bytes / max(out_bytes, 1), 2),
                    "bits_per_pixel": round(out_bytes * 8 / max(width * height, 1), 3),
                    "encode_s": round(elapsed, 3),
                    "mp_per_s": round(width * height / 1e6 / max(elapsed, 1e-6), 2),
                },
            }

    kwargs = {"device": task["device"]}
    if task["auto_quality"] is not None:
        kwargs.update(auto_cq=True, target_quality=task["auto_quality"])
    else:
        kwargs["cq"] = task["cq"]
    data = nv.encode_file(path, **kwargs)
    elapsed = time.perf_counter() - t0

    # Guard: never store something bigger than the source. The source stays
    # wherever it is, so "keeping" it costs zero additional bytes.
    out_path = Path(task["out_path"])
    if task["keep_smaller"] and len(data) >= path.stat().st_size:
        if task["copy_skipped"]:
            shutil.copy2(path, out_path.parent / path.name)
        return {
            "ok": True,
            "row": {
                "name": path.name,
                "action": "kept_source",
                "format": "avif",
                "src_bytes": path.stat().st_size,
                "avif_bytes": len(data),
                "encode_s": round(elapsed, 3),
            },
        }
    out_path.write_bytes(data)

    with Image.open(path) as im:
        width, height, mode = im.width, im.height, im.mode
    src_bytes = path.stat().st_size
    return {
        "ok": True,
        "row": {
            "name": path.name,
            "action": "encoded",
            "format": "avif",
            "mode": mode,
            "alpha": "A" in mode.upper() or mode == "PA",
            "width": width,
            "height": height,
            "megapixels": round(width * height / 1e6, 3),
            "src_bytes": src_bytes,
            "out_bytes": len(data),
            "ratio": round(src_bytes / max(len(data), 1), 2),
            "bits_per_pixel": round(len(data) * 8 / max(width * height, 1), 3),
            "encode_s": round(elapsed, 3),
            "mp_per_s": round(width * height / 1e6 / max(elapsed, 1e-6), 2),
        },
    }


class ResourceSampler:
    """Background sampler for CPU/RSS (whole process tree) and GPU/NVENC.

    All fields degrade gracefully: if psutil or pynvml is missing, the
    corresponding series simply stays empty.
    """

    def __init__(self, interval: float = 0.2):
        self.interval = interval
        self.samples: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc = None
        self._cache: dict[int, object] = {}
        self._nvml = None
        self._gpu = None

        try:
            import psutil

            self._psutil = psutil
            self._proc = psutil.Process()
            self._proc.cpu_percent(interval=None)  # prime the counter
        except Exception:
            pass
        try:
            import pynvml

            pynvml.nvmlInit()
            self._nvml = pynvml
            self._gpu = pynvml.nvmlDeviceGetHandleByIndex(0)
        except Exception:
            pass

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _prime(self, pid: int):
        if pid not in self._cache:
            try:
                proc = self._psutil.Process(pid)
                proc.cpu_percent(interval=None)
                self._cache[pid] = proc
            except Exception:
                self._cache[pid] = None
        return self._cache[pid]

    def _loop(self):
        while not self._stop.is_set():
            sample: dict = {"t": time.perf_counter()}
            if self._proc is not None:
                try:
                    cpu = self._proc.cpu_percent(interval=None)
                    rss = self._proc.memory_info().rss
                    for child in self._proc.children(recursive=True):
                        proc = self._prime(child.pid)
                        if proc is None:
                            continue
                        try:
                            cpu += proc.cpu_percent(interval=None)
                            rss += proc.memory_info().rss
                        except Exception:
                            pass
                    sample["cpu_pct"] = cpu
                    sample["rss_mb"] = rss / 1e6
                except Exception:
                    pass
            if self._gpu is not None:
                try:
                    util = self._nvml.nvmlDeviceGetUtilizationRates(self._gpu)
                    sample["gpu_pct"] = util.gpu
                    sample["vram_mb"] = (
                        self._nvml.nvmlDeviceGetMemoryInfo(self._gpu).used / 1e6
                    )
                    enc, _period = self._nvml.nvmlDeviceGetEncoderUtilization(self._gpu)
                    sample["enc_pct"] = enc
                except Exception:
                    pass
            self.samples.append(sample)
            self._stop.wait(self.interval)

    def stop(self) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        stats: dict[str, dict] = {}
        for key in ("cpu_pct", "rss_mb", "gpu_pct", "enc_pct", "vram_mb"):
            values = [s[key] for s in self.samples if key in s]
            if values:
                stats[key] = {
                    "avg": round(sum(values) / len(values), 1),
                    "max": round(max(values), 1),
                    "n": len(values),
                }
        return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, default=ROOT / "test_imgs", help="source image folder")
    parser.add_argument("--dst", type=Path, default=ROOT / "uvtest" / "out" / "compressed", help="output folder for .avif files")
    parser.add_argument("--cq", type=int, default=20, help="quality 0-51, lower is better (default 20)")
    parser.add_argument("--auto-quality", type=float, default=None, metavar="TARGET", help="enable auto_cq with this 0-100 quality target instead of --cq")
    parser.add_argument("--device", choices=["auto", "gpu", "cpu"], default="auto")
    parser.add_argument("--workers", type=int, default=None, help="parallel encode processes (default: min(8, cores/2); NVENC session limit is the hard cap)")
    parser.add_argument("--limit", type=int, default=None, help="only process the first N images (smoke test)")
    parser.add_argument(
        "--min-jpeg-quality",
        type=float,
        default=85.0,
        metavar="Q",
        help="skip JPEG sources whose estimated IJG quality is below Q (they are already more compressed than the cq target would be; 0 disables)",
    )
    parser.add_argument(
        "--keep-smaller",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="keep the source (write nothing) when the AVIF is not smaller (default: on)",
    )
    parser.add_argument(
        "--copy-skipped",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="copy skipped/kept sources unchanged into the output folder so it mirrors the input set (default: off)",
    )
    parser.add_argument(
        "--transparent-format",
        choices=["avif", "webp", "png"],
        default="avif",
        help="output format for images with real transparency (default: avif). webp codes alpha losslessly and beat AVIF on the hard-edged transparent test set (3.5 MB/5.0 s vs 6.1 MB/12.3 s)",
    )
    parser.add_argument("--webp-quality", type=int, default=90, help="WebP quality 0-100 for --transparent-format webp (default: 90)")
    parser.add_argument("--overwrite", action="store_true", help="re-encode even if the output already exists")
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("compress_report.json"),
        help="per-image + resource-usage JSON report, relative to the output folder (default: compress_report.json; 'none' disables)",
    )
    args = parser.parse_args()

    files = sorted(p for p in args.src.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"no images found in {args.src}")
        return
    args.dst.mkdir(parents=True, exist_ok=True)
    workers = args.workers or default_workers()
    workers = min(workers, len(files))

    tasks = []
    skipped_quality: list[dict] = []
    seen_stems: set[str] = set()
    out_exts = ("avif", "webp", "png")
    for i, path in enumerate(files, 1):
        # Suffix collision guard: a.png and a.jpg would otherwise write the
        # same output name — disambiguate the later one by source suffix.
        stem = path.stem
        if stem in seen_stems:
            stem = f"{path.stem}_{path.suffix.strip('.')}"
        seen_stems.add(stem)
        out_path = args.dst / (stem + ".avif")
        if not args.overwrite and any((args.dst / f"{stem}.{e}").exists() for e in out_exts):
            print(f"[{i}/{len(files)}] skip (exists): {stem}.*")
            continue
        # Pre-filter: a JPEG already compressed harder than our cq target can
        # only grow or waste encode time — keep it and move on (zero cost).
        if args.min_jpeg_quality > 0 and path.suffix.lower() in (".jpg", ".jpeg"):
            q = estimate_jpeg_quality(path)
            if 0 < q < args.min_jpeg_quality:
                if args.copy_skipped:
                    shutil.copy2(path, args.dst / path.name)
                skipped_quality.append({"name": path.name, "estimated_quality": q, "src_bytes": path.stat().st_size})
                print(f"[{i}/{len(files)}] skip (source JPEG quality ~{q:.0f} < {args.min_jpeg_quality:.0f}): {path.name}")
                continue
        tasks.append(
            {
                "index": i,
                "path": str(path),
                "out_path": str(out_path),
                "cq": args.cq,
                "auto_quality": args.auto_quality,
                "device": args.device,
                "keep_smaller": args.keep_smaller,
                "copy_skipped": args.copy_skipped,
                "transparent_format": args.transparent_format,
                "webp_quality": args.webp_quality,
            }
        )

    total_in = total_out = 0
    total_px = 0
    kept_src_bytes = 0
    kept_source = 0
    failures: list[str] = []
    rows: list[dict] = []
    started = time.perf_counter()

    sampler = ResourceSampler()
    if args.report != Path("none"):
        sampler.start()

    if tasks:
        print(f"encoding {len(tasks)} images with {workers} workers (device={args.device})")
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_encode_task, t): t for t in tasks}
            for future in as_completed(futures):
                task = futures[future]
                tag = f"[{task['index']}/{len(files)}]"
                try:
                    result = future.result()
                    row = result["row"]
                    rows.append(row)
                    if row["action"] == "kept_source":
                        kept_source += 1
                        kept_src_bytes += row["src_bytes"]
                        print(
                            f"{tag} {row['name']}: kept source (AVIF would be "
                            f"{row['avif_bytes']/1e6:.2f} MB >= source {row['src_bytes']/1e6:.2f} MB)"
                        )
                        continue
                    total_in += row["src_bytes"]
                    total_out += row["out_bytes"]
                    total_px += row["megapixels"] * 1e6
                    print(
                        f"{tag} {row['name']}: {row['src_bytes']/1e6:.2f} MB -> "
                        f"{row['out_bytes']/1e6:.2f} MB ({row['ratio']}x), "
                        f"{row['megapixels']:.1f} MP in {row['encode_s']:.2f} s ({row['mp_per_s']} MP/s)"
                    )
                except Exception as exc:  # keep going; report at the end
                    failures.append(f"{Path(task['path']).name}: {exc}")
                    print(f"{tag} FAIL {Path(task['path']).name}: {exc}")

    elapsed = time.perf_counter() - started
    resource = sampler.stop()

    n = len(rows)
    rows.sort(key=lambda r: r["name"])
    print("\n=== summary ===")
    print(f"encoded {sum(1 for r in rows if r['action'] == 'encoded')}/{len(files)} images in {elapsed:.1f} s ({workers} workers)")
    if skipped_quality:
        print(f"pre-filter: {len(skipped_quality)} JPEG sources below quality {args.min_jpeg_quality:.0f} kept as-is")
    if kept_source:
        print(f"guard: {kept_source} images kept as source (AVIF was not smaller)")
    if args.copy_skipped and (kept_source or skipped_quality):
        print(f"copy-skipped: {kept_source + len(skipped_quality)} sources copied unchanged into the output folder")
    if total_px:
        print(
            f"encoded: {total_px/1e6:.1f} MP, throughput {total_px/1e6/max(elapsed,1e-6):.2f} MP/s, "
            f"{total_in/1e6:.1f} MB -> {total_out/1e6:.1f} MB ({total_in/max(total_out,1):.2f}x, "
            f"saved {100*(1-total_out/max(total_in,1)):.0f}%)"
        )
        final_store = total_out + kept_src_bytes
        src_all = total_in + kept_src_bytes + sum(s["src_bytes"] for s in skipped_quality)
        print(
            f"overall storage: {src_all/1e6:.1f} MB -> {final_store/1e6:.1f} MB "
            f"({src_all/max(final_store,1):.2f}x, saved {100*(1-final_store/max(src_all,1)):.0f}%)"
        )
        print(f"output folder: {args.dst}")
        if resource:
            print("resources:", "  ".join(f"{k}(avg {v['avg']} max {v['max']})" for k, v in resource.items()))
    if failures:
        print(f"{len(failures)} failures:")
        for f in failures:
            print(f"  - {f}")

    if args.report != Path("none"):
        report_path = args.report if args.report.is_absolute() else args.dst / args.report
        kept_rows = [r for r in rows if r["action"] == "kept_source"]
        enc_rows = [r for r in rows if r["action"] == "encoded"]
        report = {
            "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "src": str(args.src),
            "dst": str(args.dst),
            "cq": args.cq,
            "auto_quality": args.auto_quality,
            "device": args.device,
            "workers": workers,
            "min_jpeg_quality": args.min_jpeg_quality,
            "keep_smaller": args.keep_smaller,
            "copy_skipped": args.copy_skipped,
            "transparent_format": args.transparent_format,
            "webp_quality": args.webp_quality,
            "summary": {
                "total": len(files),
                "encoded": len(enc_rows),
                "kept_source_bigger": kept_source,
                "skipped_low_quality_jpeg": len(skipped_quality),
                "elapsed_s": round(elapsed, 2),
                "total_megapixels": round(total_px / 1e6, 2),
                "aggregate_mp_per_s": round(total_px / 1e6 / max(elapsed, 1e-6), 2),
                "encoded_src_mb": round(total_in / 1e6, 1),
                "encoded_out_mb": round(total_out / 1e6, 1),
                "encoded_ratio": round(total_in / max(total_out, 1), 2),
                "final_store_mb": round((total_out + kept_src_bytes) / 1e6, 1),
                "failures": failures,
            },
            "resource": resource,
            "skipped_quality_jpeg": skipped_quality,
            "images": rows,
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"report: {report_path}")


if __name__ == "__main__":
    main()
