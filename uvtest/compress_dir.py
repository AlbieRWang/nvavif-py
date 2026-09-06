"""One-command batch compression: compress a folder of images to AVIF.

Wraps nvavif_py.encode_file for every image in the source directory.
Input formats are whatever Pillow opens (PNG/JPEG/WebP/BMP/TIFF/GIF).
By default, truly transparent images are routed to whole-image WebP
(lossless alpha, no rav1e involved); opaque images go to AVIF on the GPU.

Images are encoded by a process pool: each worker gets its own NVENC
session, so GPU color encodes of different images overlap, and CPU alpha
encodes overlap with GPU color encodes of other images. The worker count
is capped by the driver's concurrent-NVENC-session limit (~8 on RTX 40
series); oversized (>8192) images never take the GPU anyway. Jobs are
submitted slowest-first (header-probe cost estimate), and each worker's
rav1e contexts are thread-capped so parallel CPU encodes do not thrash
(alpha: cores/workers, oversize color fallback: cores/oversize-jobs).

Usage (from the repo root, single root .venv):
    uv run python uvtest/compress_dir.py                       # test_imgs -> out/compressed
    uv run python uvtest/compress_dir.py --cq 26               # web-quality preset
    uv run python uvtest/compress_dir.py --src DIR --dst DIR   # custom folders
    uv run python uvtest/compress_dir.py --auto-quality 80     # SSIM-targeted quality
    uv run python uvtest/compress_dir.py --workers 4           # cap parallelism
    uv run python uvtest/compress_dir.py --transparent-format webp   # transparent -> WebP (default)
    uv run python uvtest/compress_dir.py --copy-skipped        # mirror skipped/kept sources into the output folder

Production config: every option can live in a JSON config file passed with
--config (CLI flags override the file; unknown keys are rejected). Generate
a template with the current effective values:
    uv run python uvtest/compress_dir.py --write-config my_config.json

Routing rules (all tunable):
    opaque              -> GPU AVIF (NVENC), keep-smaller guard
    truly transparent   -> whole-image WebP q90 (--transparent-format,
                           --webp-quality); sources <= --webp-lossless-max-mb
                           (default 1 MB) use LOSSLESS WebP (same size as
                           lossy on simple graphics, zero fidelity loss)
    oversized > 8192    -> whole-image WebP q80 method 2 (--oversize-format,
                           ~5.9x faster than the rav1e fallback); >16383
                           (WebP limit) or --oversize-format avif take the
                           AVIF CPU fallback
    low-quality JPEGs   -> kept as-is (--min-jpeg-quality)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
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

# Same NVENC AV1 input cap as src/lib.rs; images beyond it take the all-CPU
# fallback path, which is orders of magnitude slower per megapixel.
NVENC_MAX_DIMENSION = 8192

# WebP format limit (Pillow raises above this).
WEBP_MAX_DIMENSION = 16383

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


def probe_cost(path: Path) -> dict:
    """Relative encode-cost estimate from the image header (no pixel decode).

    Base cost is megapixels, weighted up for the paths that cannot use the
    GPU: oversize (>8192) images encode entirely on the CPU (~0.16 s/MP
    measured vs ~0.0003 s/MP on NVENC), transparent images pay an extra CPU
    rav1e alpha encode. Used only to submit the slowest jobs first
    (longest-processing-time scheduling) and to size the rav1e thread caps;
    returns the probe details alongside the cost for those two consumers.
    """
    width = height = 0
    has_alpha = False
    try:
        from PIL import Image

        with Image.open(path) as im:
            width, height = im.width, im.height
            has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
    except Exception:
        pass
    if width:
        cost = width * height / 1e6
        if width > NVENC_MAX_DIMENSION or height > NVENC_MAX_DIMENSION:
            cost *= 200.0
        elif has_alpha:
            cost *= 8.0
        return {"cost": cost, "width": width, "height": height, "has_alpha": has_alpha}
    return {
        "cost": path.stat().st_size / 1e6,  # unreadable header: fall back to file size
        "width": 0,
        "height": 0,
        "has_alpha": False,
    }


def _encode_task(task: dict) -> dict:
    """Pool worker: encode one image, return a per-image report row."""
    from PIL import Image

    path = Path(task["path"])
    out_dir = Path(task["out_path"]).parent
    t0 = time.perf_counter()

    # Whole-image WebP routing covers two cases: (a) transparent images when
    # --transparent-format is webp/png (WebP codes alpha losslessly and keeps
    # the entire encode off the CPU rav1e alpha path), and (b) oversized
    # (>8192) images when --oversize-format is webp (single-threaded WebP is
    # still ~3x faster and 2-3x smaller than the rav1e CPU fallback, measured
    # 2026-09-06, see OPTIMIZATION_PROPOSALS I4). Constant-opaque RGBA and
    # images beyond the WebP 16383 limit stay on the AVIF path.
    width, height = task["width"], task["height"]
    oversize = width > NVENC_MAX_DIMENSION or height > NVENC_MAX_DIMENSION
    whole_format = None  # "webp" | "png": whole-image routing, no AVIF at all
    quality = None
    lossless = False
    transparent_route = False
    fmt = task["transparent_format"]
    if fmt != "avif":
        with Image.open(path) as im:
            alpha_lo, _ = im.convert("RGBA").getchannel("A").getextrema()
        if alpha_lo < 255:  # real transparency (constant-opaque stays on AVIF)
            whole_format = fmt  # webp or png
            transparent_route = True
            quality = task["webp_quality"] if fmt == "webp" else None
            # Auto-lossless: on simple graphics (small sources) lossless WebP
            # compresses to the same size as q90 lossy with zero fidelity
            # loss — measured 2026-09-06 (0.03-0.05 MB either way) — so take
            # it under the size threshold. Large artwork lossless runs 3-5x
            # bigger than lossy, so those stay lossy unless the threshold
            # (--webp-lossless-max-mb) is raised.
            if whole_format == "webp" and task["webp_lossless_max_mb"] > 0 and path.stat().st_size <= task["webp_lossless_max_mb"] * 1e6:
                lossless = True
    if whole_format is None and task["oversize_format"] == "webp" and oversize and max(width, height) <= WEBP_MAX_DIMENSION:
        whole_format = "webp"
        quality = task["oversize_webp_quality"]

    if whole_format is not None:
        # method>=3 runs an exhaustive partition search that explodes on some
        # large-image content (73.jpg: 42 s vs 2.3 s at method=2 for 10%
        # smaller output) — the oversize route defaults to method=2.
        method = task["webp_method"] if transparent_route else task["oversize_webp_method"]
        out_path = Path(task["out_path"]).with_suffix(f".{whole_format}")
        with Image.open(path) as im:
            rgba = im.convert("RGBA")
            if whole_format == "webp":
                rgba.save(out_path, "WEBP", quality=quality, lossless=lossless, method=method)
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
                    "format": whole_format,
                    "src_bytes": src_bytes,
                    "avif_bytes": out_bytes,
                    "encode_s": round(elapsed, 3),
                },
            }
        return {
            "ok": True,
                "row": {
                    "name": path.name,
                    "action": "encoded",
                    "format": whole_format,
                    "mode": "RGBA",
                    "alpha": True,
                    "lossless": lossless,
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
        default="webp",
        help="output format for images with real transparency (default: webp — codes alpha losslessly, keeps the whole image off the CPU rav1e alpha path, and beat AVIF on the hard-edged transparent test set: 3.5 MB/5.0 s vs 6.1 MB/12.3 s). Constant-opaque RGBA stays on the GPU AVIF path either way",
    )
    parser.add_argument("--webp-quality", type=int, default=90, help="WebP quality 0-100 for --transparent-format webp (default: 90)")
    parser.add_argument("--webp-method", type=int, default=4, help="libwebp effort 0-6 for transparent WebP (default: 4; small images, quality first)")
    parser.add_argument(
        "--webp-lossless-max-mb",
        type=float,
        default=1.0,
        metavar="MB",
        help="transparent sources at or below this file size use LOSSLESS WebP (measured: on simple graphics lossless compresses to the same size as q90 lossy with zero fidelity loss; large artwork lossless runs 3-5x bigger, so those stay lossy). 0 disables",
    )
    parser.add_argument(
        "--oversize-format",
        choices=["avif", "webp"],
        default="webp",
        help="output format for images beyond the NVENC 8192 cap (default: webp — measured ~5.9x batch speedup and 1/3 the size of the rav1e CPU fallback at SSIM 0.991 vs 0.995, both visually lossless; OPTIMIZATION_PROPOSALS I4). avif keeps the max-fidelity CPU fallback; images beyond the WebP 16383 limit always take the AVIF fallback",
    )
    parser.add_argument("--oversize-webp-quality", type=int, default=80, help="WebP quality 0-100 for --oversize-format webp (default: 80)")
    parser.add_argument("--oversize-webp-method", type=int, default=2, help="libwebp effort for oversized WebP (default: 2; method>=3 explodes on some content: 42 s vs 2.3 s for 10%% smaller output)")
    parser.add_argument(
        "--alpha-rav1e-threads",
        type=int,
        default=0,
        help="rav1e thread cap per worker for CPU alpha encodes (default: 0 = auto, cores/workers)",
    )
    parser.add_argument(
        "--color-rav1e-threads",
        type=int,
        default=0,
        help="rav1e thread cap for the CPU color fallback (default: 0 = all cores; measured: capping regresses wall time, OPTIMIZATION_PROPOSALS J)",
    )
    parser.add_argument("--overwrite", action="store_true", help="re-encode even if the output already exists")
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("compress_report.json"),
        help="per-image + resource-usage JSON report, relative to the output folder (default: compress_report.json; 'none' disables)",
    )
    parser.add_argument("--config", type=Path, default=None, help="JSON config file; every CLI option can be set there by its long name (e.g. \"cq\": 26). CLI flags override the file")
    parser.add_argument("--write-config", type=Path, default=None, metavar="PATH", help="write the effective config (defaults + CLI overrides) as JSON to PATH and exit — use it to generate a config template")
    return parser


def apply_config(parser: argparse.ArgumentParser, config_path: Path) -> None:
    """Load a JSON config and inject it as parser defaults (CLI still wins).

    Keys are the long option names without the leading dashes, values use the
    same types as the CLI (ints/floats/bools/strings). Unknown keys are an
    error so a typo'd config can never silently run with wrong defaults.
    """
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        sys.exit(f"config {config_path}: {exc}")
    if not isinstance(config, dict):
        sys.exit(f"config {config_path}: expected a JSON object")
    by_dest = {action.dest: action for action in parser._actions}
    for key, value in config.items():
        action = by_dest.get(key)
        if action is None or key in ("config", "write_config", "help"):
            sys.exit(f"config {config_path}: unknown key {key!r} (valid keys: {', '.join(sorted(k for k in by_dest if k not in ('config', 'write_config', 'help')))}')")
        # set_defaults bypasses argparse type coercion, so apply the action's
        # type explicitly (matters for ints/floats/Paths from JSON).
        parser.set_defaults(**{key: action.type(value) if action.type else value})


def effective_config(args: argparse.Namespace) -> dict:
    return {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items() if k not in ("config", "write_config")}


def main() -> None:
    parser = build_parser()
    argv = sys.argv[1:]
    # First pass: only resolve --config so the file can seed the defaults; the
    # second parse then applies CLI overrides on top of them.
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--config", type=Path)
    cfg_args, _ = pre_parser.parse_known_args(argv)
    if cfg_args.config is not None:
        apply_config(parser, cfg_args.config)
    args = parser.parse_args(argv)

    if args.write_config is not None:
        args.write_config.write_text(json.dumps(effective_config(args), indent=1), encoding="utf-8")
        print(f"config template written: {args.write_config}")
        return

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
                "webp_method": args.webp_method,
                "webp_lossless_max_mb": args.webp_lossless_max_mb,
                "oversize_format": args.oversize_format,
                "oversize_webp_quality": args.oversize_webp_quality,
                "oversize_webp_method": args.oversize_webp_method,
                **probe_cost(path),
            }
        )

    # Longest job first: a cost-ordered submission keeps every worker busy
    # through the tail instead of ending with one worker grinding on the
    # slowest image (CPU-fallback oversize / transparent AVIF encodes).
    tasks.sort(key=lambda t: t["cost"], reverse=True)

    # Fair-share rav1e threads for the alpha encodes: many short transparent
    # images across 8 workers would otherwise spin up a full all-cores
    # context per job and thrash. Only the alpha path is capped — measured
    # (2026-09-06 full corpus): capping the oversize color fallback strands
    # idle cores and REGRESSES wall time (45.0 s uncapped -> 54.8 s with
    # cores/4 jobs, 64.6 s with cores/8), because the total CPU work of the
    # long oversize jobs is invariant and OS timesharing packs it well; the
    # per-image slowdown under contention (16 s -> 40 s) does not hurt wall
    # time. Spawned pool workers inherit the environment variables; 0/absent
    # keeps the single-process all-cores default.
    alpha_threads = args.alpha_rav1e_threads or max(1, (os.cpu_count() or 8) // workers)
    os.environ["NVAVIF_RAV1E_THREADS"] = str(alpha_threads)
    if args.color_rav1e_threads > 0:
        os.environ["NVAVIF_RAV1E_THREADS_COLOR"] = str(args.color_rav1e_threads)

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
            "webp_method": args.webp_method,
            "webp_lossless_max_mb": args.webp_lossless_max_mb,
            "oversize_format": args.oversize_format,
            "oversize_webp_quality": args.oversize_webp_quality,
            "oversize_webp_method": args.oversize_webp_method,
            "alpha_rav1e_threads": alpha_threads,
            "color_rav1e_threads": args.color_rav1e_threads,
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
