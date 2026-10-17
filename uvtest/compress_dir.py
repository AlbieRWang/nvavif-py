"""One-command batch compression: pixel-based WebP/GPU AVIF routing.

Default: opaque images <=2 MP use WebP; larger ones use GPU AVIF (CQ36 up to
8 MP, CQ30 above).
--opaque-format avif/webp overrides automatic routing.
--auto-quality TARGET explicitly enables AVIF quality calibration.
Known slow AVIF paths are skipped; no CPU AVIF fallback is allowed.

Wraps nvavif_py.encode_file for every image in the source directory.
Input formats are whatever Pillow opens (PNG/JPEG/WebP/BMP/TIFF/GIF).
By default, truly transparent images are routed to whole-image WebP
(lossless alpha, no rav1e involved); opaque images follow the pixel threshold.

Images are encoded by a process pool: each worker gets its own NVENC
session, so GPU color encodes and CPU WebP tasks can overlap. The worker count
is capped by the driver's concurrent-NVENC-session limit (~8 on RTX 40
series); oversized (>8192) images never take the GPU anyway. Each worker
keeps its NVENC encoder contexts open across images (LRU sized so that
workers x capacity stays within the driver session budget), which removes
the ~80 ms context-open cost per encode (measured 2026-09-06; ~75% of
per-image wall time at 512x512). Jobs are submitted slowest-first
(header-probe cost estimate). CPU AVIF paths are skipped.

Very large runs (100k+ sources) keep the orchestration flat-memory: header
probes run in a thread pool, at most 4xworkers tasks are in flight (no
million-future materialization), every finished row is streamed to
<report>.stream.jsonl so an interrupted run still leaves a per-image
record, the resource sampler stretches its interval past 20k samples, and
pool workers are recycled every 10000 tasks. An interrupted run still
writes the .json report, marked "interrupted": true.

Usage (from the repo root, single root .venv):
    uv run python uvtest/compress_dir.py                       # test_imgs -> out/compressed, auto routing (CQ36/CQ30)
                                                               # (bare-run defaults == compress_config.json)
    uv run python uvtest/compress_dir.py --src DIR --dst DIR   # custom folders
    uv run python uvtest/compress_dir.py --auto-quality 85     # different SSIM target
    uv run python uvtest/compress_dir.py --fixed-cq --cq 26    # fixed-CQ benchmark mode (no calibration)
    uv run python uvtest/compress_dir.py --workers 4           # cap parallelism
    uv run python uvtest/compress_dir.py --transparent-format webp   # transparent -> WebP (default)
    uv run python uvtest/compress_dir.py --copy-skipped        # mirror kept/skipped/failed sources (default on)
    uv run python uvtest/compress_dir.py --in-place            # replace sources in their own folder
    uv run python uvtest/compress_dir.py --no-recursive        # scan only the top level (recursive is the default)

Production config: every option can live in a JSON config file passed with
--config (CLI flags override the file; unknown keys are rejected). Generate
a template with the current effective values:
    uv run python uvtest/compress_dir.py --write-config my_config.json

Routing rules (all tunable):
    opaque              -> <=2 MP WebP, otherwise GPU AVIF (CQ36 for <=8 MP,
                           CQ30 above); --opaque-format avif/webp overrides
    truly transparent   -> whole-image WebP q90 (--transparent-format,
                           --webp-quality); sources <= --webp-lossless-max-mb
                           (default 1 MB) use LOSSLESS WebP (same size as
                           lossy on simple graphics, zero fidelity loss)
    oversized > 8192    -> whole-image WebP q80 method 2 (--oversize-format,
                           ~5.9x faster than the rav1e fallback); >16383
                           (WebP limit) is resized by --oversize-max-edge
                           (default 16383); if still too large or explicitly
                           routed to AVIF, skip and retain the source
    low-quality JPEGs   -> kept as-is (--min-jpeg-quality)

Unchanged sources (kept by --keep-smaller, pre-filtered JPEGs, failures) are
copied into the output folder by default (--copy-skipped) so the output count
matches the input; --in-place replaces sources in their own folder instead.

Metadata: EXIF orientation is physically applied to the pixels, and the
remaining EXIF plus the ICC profile and XMP are embedded in WebP/PNG outputs
(wide-gamut sources keep their profile). The AVIF route embeds the source EXIF
too (minus the Orientation tag, which the encoder already applied), but cannot
carry ICC or XMP — avif-serialize has no slot for either and those pixels are
converted to sRGB; such images get a `dropped_meta` list in the report so the
gap is visible rather than silent. Compressed outputs also inherit the source's
modification and creation times (Windows) so date-sorted libraries keep their
ordering; access time is written too but lands on the encode time, since
reading the source already refreshes it.

The scan is recursive by default and the output mirrors the source's relative
folder structure. GIF and animated WebP sources are skipped untouched (a
still-image encode would flatten them to their first frame); animated-image
support is a possible future feature.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
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
from PIL import Image, ImageOps  # noqa: E402

# Sources beyond Pillow's default ~89 MP "decompression bomb" guard are
# legitimate inputs here (oversized photography; --oversize-max-edge even
# requires decoding them to resize).
Image.MAX_IMAGE_PIXELS = None

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}

# Driver cap on concurrent NVENC sessions for consumer GPUs (RTX 40 series).
NVENC_SESSION_LIMIT = 8

# Same NVENC AV1 input cap as src/lib.rs; images beyond it take the all-CPU
# fallback path, which is orders of magnitude slower per megapixel.
NVENC_MAX_DIMENSION = 8192

# NVENC practical minimum, measured on RTX 4070 (2026-09-13): av1_nvenc
# refuses sessions below ~130 wide / ~66 high and the library silently falls
# back to CPU rav1e — for sub-100px sources that detour buys nothing, so the
# batch tool skips them outright instead of force-compat-ing the slow path.
NVENC_MIN_WIDTH = 130
NVENC_MIN_HEIGHT = 66

# Upper edge of the AVIF mid band. The 2-8 MP band only became AVIF after the
# context-hit re-measurement (DEVELOPMENT_NOTES §21.1); it uses --mid-cq so the
# format switch stays size-neutral, while larger sources keep --cq.
MID_AVIF_MAX_MP = 8.0

# WebP format limit (Pillow raises above this).
WEBP_MAX_DIMENSION = 16383

# EXIF tag id for Orientation (ExifTags.Base.Orientation).
_ORIENTATION_TAG = 0x0112

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
    img_format = None
    animated = False
    try:
        from PIL import Image

        with Image.open(path) as im:
            width, height = im.width, im.height
            has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
            img_format = im.format
            animated = bool(getattr(im, "is_animated", False))
    except Exception:
        pass
    if width:
        cost = width * height / 1e6
        if width > NVENC_MAX_DIMENSION or height > NVENC_MAX_DIMENSION:
            cost *= 200.0
        elif has_alpha:
            cost *= 8.0
        return {"cost": cost, "width": width, "height": height, "has_alpha": has_alpha, "format": img_format, "animated": animated}
    return {
        "cost": path.stat().st_size / 1e6,  # unreadable header: fall back to file size
        "width": 0,
        "height": 0,
        "has_alpha": False,
        "format": img_format,
        "animated": animated,
    }


# Source metadata the AVIF container cannot carry: avif-serialize 0.8.8 only
# exposes set_exif, so ICC and XMP have nowhere to go (the pixels are converted
# to sRGB instead). Listed per image in the report's `dropped_meta`.
_AVIF_UNSUPPORTED_META = ("icc_profile", "xmp")


def _source_exif_bytes(path: Path) -> tuple[bytes | None, list[str]]:
    """AVIF-conformant EXIF payload for `path`, plus what the container cannot hold.

    Returns ``(payload, dropped)``; `dropped` lists the tracked source metadata
    that will not reach the output — the `_AVIF_UNSUPPORTED_META` keys present
    in the source, or ``"exif"`` when the bytes could not be parsed. It is
    recorded in the report so a metadata-free source and a bare-metal container
    are not confused.

    The AVIF route used to drop EXIF entirely (PARITY_MATRIX M5). Handing the
    bytes over is not enough — the HEIF Exif item has its own layout:
    - drop the Orientation tag: encode_file applies it to the pixels itself, and
      a leftover tag would make viewers rotate the output a second time;
    - strip the JPEG/WebP "Exif\\0\\0" marker and prepend the 4-byte TIFF-header
      offset the item requires. Neither (nor the raw Pillow bytes, which have
      the marker and no offset) is accepted: libavif rejects the whole file with
      "Invalid Exif payload" — Pillow cannot even open it.
    """
    dropped: list[str] = []
    try:
        with Image.open(path) as im:
            for key in _AVIF_UNSUPPORTED_META:
                if im.info.get(key):
                    dropped.append(key)
            raw = im.info.get("exif")
            if not isinstance(raw, (bytes, bytearray)) or not raw:
                return None, dropped
            exif = im.getexif()
    except Exception:
        return None, dropped
    data = bytes(raw)
    if exif.get(_ORIENTATION_TAG, 1) not in (None, 1):
        del exif[_ORIENTATION_TAG]
        data = exif.tobytes()
    while data.startswith(b"Exif\x00\x00"):
        data = data[6:]  # JPEG/WebP marker; Exif.tobytes() emits it again
    if data[:4] not in (b"II*\x00", b"MM\x00*"):
        dropped.append("exif")  # unknown layout: no EXIF beats an unopenable file
        return None, dropped
    return b"\x00\x00\x00\x00" + data, dropped


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    _kernel32.SetFileTime.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
    )
    _kernel32.SetFileTime.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    def _set_creation_time(path: Path, timestamp: float) -> None:
        # os.utime only covers access/modification; Explorer's 创建时间 needs
        # SetFileTime on a handle opened with FILE_WRITE_ATTRIBUTES.
        handle = _kernel32.CreateFileW(
            str(path), 0x0100, 0, None, 3, 0, None  # FILE_WRITE_ATTRIBUTES, OPEN_EXISTING
        )
        if not handle or handle == ctypes.c_void_p(-1).value:
            return
        try:
            ticks = int((timestamp + 11644473600) * 10_000_000)  # unix -> FILETIME epochs
            ft = wintypes.FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)
            _kernel32.SetFileTime(handle, ctypes.byref(ft), None, None)
        finally:
            _kernel32.CloseHandle(handle)


def _copy_file_times(dst: Path, src_stat: os.stat_result) -> None:
    """Give the compressed output the source's file times.

    A re-encoded image is a brand-new file, so without this the original dates
    are lost from every compressed image — which is what photo libraries sort
    by. Copies of unencoded sources already go through shutil.copy2, which
    preserves them; this covers the encoded ones.
    """
    try:
        os.utime(dst, (src_stat.st_atime, src_stat.st_mtime))
        if os.name == "nt":
            _set_creation_time(dst, src_stat.st_ctime)
    except OSError:
        pass


def _encode_task(task: dict) -> dict:
    """Pool worker: encode one image, return a per-image report row."""
    from PIL import Image, PngImagePlugin

    path = Path(task["path"])
    in_place = task["in_place"]
    display_name = task.get("name") or path.name
    out_dir = Path(task["out_path"]).parent
    t0 = time.perf_counter()
    # Capture BEFORE any in-place overwrite — after writing, stat() would
    # return the new (compressed) file's size and break the keep-smaller guard.
    # The full stat is kept for the output's timestamp copy.
    src_stat = path.stat()
    src_bytes = src_stat.st_size

    def skip_slow(reason):
        if task["copy_skipped"] and not in_place:
            shutil.copy2(path, out_dir / path.name)
        return {"ok": True, "row": {"name": display_name, "action": "skipped_slow",
                "reason": reason, "src_bytes": src_bytes}}

    # Whole-image WebP routing covers two cases: (a) transparent images when
    # --transparent-format is webp/png (WebP codes alpha losslessly and keeps
    # the entire encode off the CPU rav1e alpha path), and (b) oversized
    # (>8192) images when --oversize-format is webp (single-threaded WebP is
    # still ~3x faster and 2-3x smaller than the rav1e CPU fallback, measured
    # 2026-09-06, see OPTIMIZATION_PROPOSALS I4). Constant-opaque RGBA and
    # images beyond the WebP 16383 limit stay on the AVIF path.
    width, height = task["width"], task["height"]
    oversize = width > NVENC_MAX_DIMENSION or height > NVENC_MAX_DIMENSION
    # --oversize-max-edge: opt-in resize of sources whose longer edge exceeds
    # the limit (typically the WebP 16383 cap) down to fit, so they take the
    # fast whole-image WebP route instead of the 40 s-class AVIF CPU fallback.
    # AVIF routes (--oversize-format avif / --transparent-format avif) keep
    # full resolution by design; width/height below refer to the ENCODED
    # dimensions from here on.
    resize_limit = task["oversize_max_edge"]
    orig_width, orig_height = width, height
    need_resize = bool(resize_limit) and max(width, height) > resize_limit
    if need_resize:
        scale = resize_limit / max(orig_width, orig_height)
        width = max(1, int(orig_width * scale))
        height = max(1, int(orig_height * scale))
    webp_fits = max(width, height) <= WEBP_MAX_DIMENSION
    if not orig_width or not orig_height:
        return skip_slow("unknown_dimensions")
    if oversize and (not webp_fits or task["oversize_format"] == "avif"):
        return skip_slow("oversize_no_fast_route")
    whole_format = None  # "webp" | "png": whole-image routing, no AVIF at all
    quality = None
    lossless = False
    transparent_route = False
    has_transparency = False
    fmt = task["transparent_format"]
    # Pixel-level transparency check only when the header claims an alpha
    # channel (probe_cost): an opaque-header JPEG/PNG cannot sprout alpha
    # pixels, and the full decode + RGBA convert costs 0.2-0.4 s per 12 MP
    # source for nothing (REVIEW_FINDINGS B1). width == 0 (unreadable
    # header) keeps the old always-check behavior as a fallback.
    if task.get("has_alpha") or not task.get("width"):
        with Image.open(path) as im:
            alpha_lo, _ = im.convert("RGBA").getchannel("A").getextrema()
        has_transparency = alpha_lo < 255
        if has_transparency and fmt != "avif" and (fmt == "png" or webp_fits):
            # real transparency (constant-opaque stays on AVIF); a webp
            # transparent route beyond the 16383 cap falls through to AVIF
            # unless --oversize-max-edge resized it to fit
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
    if whole_format is None and not has_transparency and task["oversize_format"] == "webp" and oversize and webp_fits:
        whole_format = "webp"
        quality = task["oversize_webp_quality"]

    auto_webp = (
        task["opaque_format"] == "auto"
        and task["opaque_webp_max_mp"] > 0
        and width * height <= task["opaque_webp_max_mp"] * 1e6
    )
    if whole_format is None and not has_transparency and (task["opaque_format"] == "webp" or auto_webp) and not oversize and webp_fits:
        whole_format = "webp"
        quality = task["opaque_webp_quality"]

    if whole_format is not None:
        # method>=3 runs an exhaustive partition search that explodes on some
        # large-image content (73.jpg, 8736x11648: 42 s vs 2.3 s at method=2
        # for 10% smaller output), so only the oversize route is capped.
        # Below 8192 the requested effort is kept: a full-batch A/B measured
        # method 4 as no slower end-to-end and 1.5% smaller on the
        # transparent route.
        requested = task["webp_method"] if (transparent_route or not oversize) else task["oversize_webp_method"]
        method = min(2, requested) if oversize else requested
        out_path = Path(task["out_path"]).with_suffix(f".{whole_format}")
        with Image.open(path) as im:
            src_exif = im.info.get("exif")
            src_icc = im.info.get("icc_profile")
            src_xmp = im.info.get("xmp")
            # Only the transparent route needs the alpha plane; on the
            # oversize/opaque routes an all-opaque alpha channel costs WebP
            # bytes and encode time for nothing (REVIEW_FINDINGS B8).
            enc_img = im.convert("RGBA") if transparent_route else im.convert("RGB")
        if src_exif:
            # Match the AVIF route (encode_file wrapper): physically apply the
            # EXIF orientation so the output shows what every viewer showed
            # for the source; the returned image's EXIF has the orientation
            # tag removed, so viewers won't rotate the correct pixels again.
            enc_img = ImageOps.exif_transpose(enc_img)
            src_exif = enc_img.info.get("exif")
            if enc_img.size != (orig_width, orig_height):
                # Transposed (90° orientation): header dims no longer describe
                # the pixels — swap them and recompute the resize target.
                orig_width, orig_height = enc_img.size
                width, height = orig_width, orig_height
                if need_resize:
                    scale = resize_limit / max(orig_width, orig_height)
                    width = max(1, int(orig_width * scale))
                    height = max(1, int(orig_height * scale))
            if need_resize:
                enc_img = enc_img.resize((width, height), Image.LANCZOS)
        elif need_resize:
            enc_img = enc_img.resize((width, height), Image.LANCZOS)
        # WebP/PNG containers carry ICC, EXIF and XMP natively, so embed all
        # three as-is — wide-gamut sources keep their profile instead of being
        # silently reinterpreted as sRGB. (The AVIF route converts pixels to
        # sRGB and keeps only EXIF: avif-serialize has no ICC/XMP slot.)
        save_meta = {}
        if src_exif:
            save_meta["exif"] = src_exif
        if src_icc:
            save_meta["icc_profile"] = src_icc
        if src_xmp:
            if whole_format == "webp":
                save_meta["xmp"] = src_xmp
            else:
                # PNG has no xmp= keyword: XMP travels as the Adobe iTXt chunk.
                pnginfo = PngImagePlugin.PngInfo()
                pnginfo.add_itxt("XML:com.adobe.xmp", src_xmp)
                save_meta["pnginfo"] = pnginfo
        tmp_path = out_path.with_name(out_path.name + ".tmp")
        try:
            if whole_format == "webp":
                enc_img.save(tmp_path, "WEBP", quality=quality, lossless=lossless, method=method, **save_meta)
            else:
                enc_img.save(tmp_path, "PNG", **save_meta)
            out_bytes = tmp_path.stat().st_size
            if task["keep_smaller"] and out_bytes >= src_bytes:
                if task["copy_skipped"] and not in_place:
                    shutil.copy2(path, out_path.parent / path.name)
                return {
                    "ok": True,
                    "row": {
                        "name": display_name,
                        "action": "kept_source",
                        "format": whole_format,
                        "src_bytes": src_bytes,
                        "avif_bytes": out_bytes,
                        "encode_s": round(time.perf_counter() - t0, 3),
                    },
                }
            os.replace(tmp_path, out_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        if in_place and path.resolve() != out_path.resolve():
            path.unlink()
        _copy_file_times(out_path, src_stat)
        elapsed = time.perf_counter() - t0
        return {
            "ok": True,
            "row": {
                "name": display_name,
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
                **({"resized_from": [orig_width, orig_height]} if need_resize else {}),
            },
        }

    slow_reason = (
        "transparent_avif_cpu_alpha" if has_transparency else
        "oversize_avif_cpu" if oversize else
        "cpu_device" if task["device"] == "cpu" else
        "unknown_dimensions" if not orig_width or not orig_height else None
    )
    if slow_reason:
        return skip_slow(slow_reason)
    kwargs = {"device": "gpu", "preset": task["preset"]}
    if task["auto_quality"] is not None:
        kwargs.update(auto_cq=True, target_quality=task["auto_quality"])
    else:
        kwargs["cq"] = task["cq"]
    src_exif, dropped_meta = _source_exif_bytes(path)
    if src_exif:
        kwargs["exif"] = src_exif
    data, chosen_cq = nv.encode_file(path, with_cq=True, **kwargs)
    elapsed = time.perf_counter() - t0

    # Guard: never store something bigger than the source. The source stays
    # wherever it is, so "keeping" it costs zero additional bytes.
    out_path = Path(task["out_path"])
    if task["keep_smaller"] and len(data) >= src_bytes:
        if task["copy_skipped"] and not in_place:
            shutil.copy2(path, out_path.parent / path.name)
        return {
            "ok": True,
                "row": {
                    "name": display_name,
                    "action": "kept_source",
                    "format": "avif",
                    "cq": chosen_cq,
                    "src_bytes": src_bytes,
                    "avif_bytes": len(data),
                    "encode_s": round(elapsed, 3),
                },
        }
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    try:
        tmp_path.write_bytes(data)
        os.replace(tmp_path, out_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    _copy_file_times(out_path, src_stat)

    with Image.open(path) as im:
        width, height, mode = im.width, im.height, im.mode
    if in_place:
        path.unlink()  # output suffix (.avif) never equals a source suffix
    return {
        "ok": True,
        "row": {
            "name": display_name,
            "action": "encoded",
            "format": "avif",
            "cq": chosen_cq,
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
            # ICC/XMP the AVIF container cannot hold; the source channel is the
            # only copy left, so flag it instead of letting it vanish silently.
            **({"dropped_meta": dropped_meta} if dropped_meta else {}),
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
        self._t0 = time.monotonic()
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
            # Bound sampler memory for multi-hour runs: base interval for the
            # first 20k samples, then stretch x2 per 20k (cap 5 s) — a 12 h
            # run ends with ~60k samples instead of ~216k.
            n = len(self.samples)
            if n < 20000:
                wait_s = self.interval
            else:
                wait_s = min(5.0, self.interval * (1 << (n // 20000)))
            self._stop.wait(wait_s)

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
    parser.add_argument(
        "--recursive",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="recurse into subfolders and mirror the relative structure in the output folder (default: on; --no-recursive scans only the top level)",
    )
    parser.add_argument("--dst", type=Path, default=ROOT / "uvtest" / "out" / "compressed", help="output folder for .avif files")
    parser.add_argument("--cq", type=int, default=30, help="fixed quality for opaque AVIF above 8 MP, 0-51 (encode_file clamp; 0 maps to 1), lower is better (default 30)")
    parser.add_argument("--mid-cq", type=int, default=36, help="fixed quality for opaque AVIF up to 8 MP, 0-51; 36 keeps that band size-neutral to WebP Q90 (default 36; lower for more quality, larger files)")
    parser.add_argument("--auto-quality", type=float, default=None, metavar="TARGET", help="opt in to auto_cq SSIM targeting on the 0-100 scale; per-image calibrated CQ is recorded in the report")
    parser.add_argument(
        "--fixed-cq",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="use fixed --cq without calibration (default); --auto-quality TARGET opts into calibration",
    )
    parser.add_argument("--device", choices=["auto", "gpu", "cpu"], default="auto")
    parser.add_argument(
        "--preset",
        type=int,
        choices=[1, 2, 3, 4, 5, 6, 7],
        default=7,
        help="NVENC preset 1-7 for the AVIF route (default 7 = best compression; lower is faster but larger). Batch AVIF has no CPU fallback, so this never selects a rav1e preset",
    )
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
        default=True,
        help="copy kept/skipped/failed sources unchanged into the output folder so the output count matches the input set (default: on); ignored with --in-place",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        default=False,
        help="compress in place: replace each source file with its compressed output (originals are only removed after a successful, smaller encode; kept/skipped sources stay untouched). Overrides --dst; --copy-skipped is a no-op",
    )
    parser.add_argument(
        "--transparent-format",
        choices=["avif", "webp", "png"],
        default="webp",
        help="output format for images with real transparency (default: webp — codes alpha losslessly, keeps the whole image off the CPU rav1e alpha path, and beat AVIF on the hard-edged transparent test set: 3.5 MB/5.0 s vs 6.1 MB/12.3 s). Constant-opaque RGBA stays on the GPU AVIF path either way",
    )
    parser.add_argument("--webp-quality", type=int, default=90, help="WebP quality 0-100 for --transparent-format webp (default: 90)")
    parser.add_argument("--webp-method", type=int, default=4, help="libwebp effort 0-6 for transparent/opaque WebP routes (default: 4; oversize sources are capped at 2)")
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
        help="output format for images beyond the NVENC 8192 cap (default: webp — measured ~5.9x batch speedup and 1/3 the size of the rav1e CPU fallback at SSIM 0.991 vs 0.995, both visually lossless; OPTIMIZATION_PROPOSALS I4). Batch AVIF never falls back to CPU: with avif, or when WebP cannot fit the 16383 limit, the source is skipped and retained (skipped_slow)",
    )
    parser.add_argument("--oversize-webp-quality", type=int, default=80, help="WebP quality 0-100 for --oversize-format webp (default: 80)")
    parser.add_argument("--oversize-webp-method", type=int, default=2, help="libwebp effort for oversized WebP (default: 2; effective effort is capped at 2 because method>=3 explodes on some content: 42 s vs 2.3 s for 10%% smaller output)")
    parser.add_argument(
        "--opaque-format",
        choices=["auto", "avif", "webp"],
        default="auto",
        help="opaque output: auto uses WebP at or below --opaque-webp-max-mp and GPU AVIF above it (default); avif forces GPU AVIF; webp forces CPU WebP",
    )
    parser.add_argument("--opaque-webp-quality", type=int, default=90, help="WebP quality 0-100 for --opaque-format webp/auto (default: 90)")
    parser.add_argument("--opaque-webp-max-mp", type=float, default=2.0, metavar="MP", help="auto opaque routing uses WebP at or below this pixel count; larger images use AVIF (default: 2 MP, the measured speed crossover with NVENC context hits; 0 disables auto WebP)")
    parser.add_argument(
        "--oversize-max-edge",
        type=int,
        default=16383,
        metavar="N",
        help="resize sources whose longer edge exceeds N (LANCZOS, aspect kept) so they take the fast whole-image WebP route instead of the AVIF CPU fallback (default: 16383, the WebP limit; 0 = off, full resolution kept). AVIF routes (--oversize-format avif) ignore this. Measured: LANCZOS resize of a 101.7 MP source is ~2.3 s vs the 40 s-class CPU AVIF fallback; every resized image is recorded via 'resized_from' in the report",
    )
    parser.add_argument(
        "--alpha-rav1e-threads",
        type=int,
        default=0,
        help="rav1e thread cap per worker (default: 0 = auto, cores/workers); batch AVIF never runs on CPU, so this is retained for config compatibility",
    )
    parser.add_argument(
        "--color-rav1e-threads",
        type=int,
        default=0,
        help="rav1e thread cap for the CPU color fallback (default: 0 = all cores; batch AVIF never runs on CPU, so this is retained for config compatibility)",
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
        # type explicitly (matters for ints/floats/Paths from JSON). JSON null
        # means "use the CLI default" for optional args (auto_quality, limit,
        # workers...) — no value to coerce.
        parser.set_defaults(**{key: None if value is None else (action.type(value) if action.type else value)})


def effective_config(args: argparse.Namespace) -> dict:
    return {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items() if k not in ("config", "write_config")}


def resolve_quality_mode(args, argv, parser):
    explicit_auto = any(arg == "--auto-quality" or arg.startswith("--auto-quality=") for arg in argv)
    if explicit_auto and "--fixed-cq" in argv:
        parser.error("--fixed-cq and --auto-quality are mutually exclusive")
    if "--fixed-cq" in argv:
        args.auto_quality = None
    elif explicit_auto:
        args.fixed_cq = False
    elif "--no-fixed-cq" in argv and args.auto_quality is None:
        args.auto_quality = 88.0
    elif args.fixed_cq:
        args.auto_quality = None
    elif args.auto_quality is None:
        args.auto_quality = 88.0
    args.fixed_cq = args.auto_quality is None


def resume_config(args: argparse.Namespace) -> dict:
    ignored = {"src", "dst", "report", "recursive", "limit", "workers", "overwrite",
               "copy_skipped", "alpha_rav1e_threads", "color_rav1e_threads", "fixed_cq"}
    return {k: v for k, v in effective_config(args).items() if k not in ignored}


def source_fingerprint(path: Path) -> dict:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def load_resume_rows(path: Path, src: Path, dst: Path) -> dict:
    rows = {}
    context = None
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                if row.get("stream"):
                    context = row if (isinstance(row.get("src"), str) and isinstance(row.get("dst"), str)
                                      and Path(row["src"]).resolve() == src.resolve()
                                      and Path(row["dst"]).resolve() == dst.resolve()) else None
                elif context is not None and isinstance(row.get("name"), str):
                    if row.get("action") in ("kept_source", "skipped_slow", "failed"):
                        rows[row["name"]] = {"action": row["action"],
                                             "source_fingerprint": row.get("source_fingerprint"),
                                             "resume_config": context.get("resume_config"),
                                             "resume_version": context.get("resume_version")}
                    else:
                        rows.pop(row["name"], None)
    except FileNotFoundError:
        pass
    return rows


def main() -> None:
    # Redirected stdout/stderr use the locale codec (e.g. cp936 on Chinese
    # Windows); a 100k-source run WILL contain names outside it and a print
    # would raise UnicodeEncodeError mid-run. UTF-8 with replacement keeps
    # the console output lossy-but-alive (reports/stream.jsonl are always
    # real UTF-8 files).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

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
    resolve_quality_mode(args, argv, parser)
    if args.opaque_webp_max_mp < 0 or not math.isfinite(args.opaque_webp_max_mp):
        parser.error("--opaque-webp-max-mp must be finite and non-negative")

    if args.write_config is not None:
        args.write_config.write_text(json.dumps(effective_config(args), indent=1), encoding="utf-8")
        print(f"config template written: {args.write_config}")
        return

    scan = args.src.rglob("*") if args.recursive else args.src.iterdir()
    files = sorted(p for p in scan if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)
    if args.limit:
        files = files[: args.limit]
    if not files:
        hint = "" if args.recursive else " (subfolders exist? they are included by default; you passed --no-recursive)"
        print(f"no images found in {args.src}{hint}")
        return
    if args.in_place:
        args.dst = args.src  # outputs replace the sources in their own folder
    args.dst.mkdir(parents=True, exist_ok=True)
    workers = args.workers or default_workers()
    workers = max(1, min(workers, len(files), NVENC_SESSION_LIMIT))

    tasks = []
    candidates = []
    skipped_quality: list[dict] = []
    skipped_animated: list[dict] = []
    skipped_small: list[dict] = []
    skipped_prev_kept: list[str] = []
    seen_stems: dict[Path, set] = {}
    out_exts = ("avif", "webp", "png")
    current_resume_config = resume_config(args)
    prev_attempts: dict[str, dict] = {}
    if not args.overwrite and args.report != Path("none"):
        prev_report = args.report if args.report.is_absolute() else args.dst / args.report
        prev_stream = prev_report.parent / (prev_report.stem + ".stream.jsonl")
        prev_attempts = load_resume_rows(prev_stream, args.src, args.dst)
    for i, path in enumerate(files, 1):
        rel = path.relative_to(args.src)
        out_dir = args.dst / rel.parent
        # Suffix collision guard: a.png and a.jpg in the SAME folder would
        # otherwise write the same output name — disambiguate the later one
        # by source suffix. Different subfolders may reuse stems freely.
        stem = path.stem
        dir_stems = seen_stems.setdefault(rel.parent, set())
        if stem in dir_stems:
            stem = f"{path.stem}_{path.suffix.strip('.')}"
        dir_stems.add(stem)
        out_path = out_dir / (stem + ".avif")
        previous = prev_attempts.get(rel.as_posix())
        fingerprint = source_fingerprint(path)
        if (previous is not None and previous.get("action") in ("kept_source", "skipped_slow")
                and previous.get("resume_version") == 2
                and previous.get("resume_config") == current_resume_config
                and previous.get("source_fingerprint") == fingerprint):
            if args.copy_skipped and not args.in_place:
                copied_path = out_dir / path.name
                if not copied_path.exists() or source_fingerprint(copied_path) != fingerprint:
                    out_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, copied_path)
            skipped_prev_kept.append(rel.as_posix())
            print(f"[{i}/{len(files)}] skip (kept as source by previous run): {rel.as_posix()}")
            continue
        if not args.overwrite and previous is None:
            # In-place: exclude the source file itself, or a png/webp source
            # would always "already exist" under the output suffix set.
            exists = any(
                (p := out_path.with_suffix(f".{e}")).exists() and (not args.in_place or p.resolve() != path.resolve())
                for e in out_exts
            )
            if exists:
                print(f"[{i}/{len(files)}] skip (exists): {rel.as_posix()}.*")
                continue
        candidates.append((i, path, rel, out_dir, out_path, fingerprint))

    pending_paths = [entry[1] for entry in candidates]
    if len(pending_paths) > 64:
        with ThreadPoolExecutor(max_workers=min(32, (os.cpu_count() or 8) * 4)) as tp:
            probes = list(tp.map(probe_cost, pending_paths, chunksize=64))
    else:
        probes = [probe_cost(p) for p in pending_paths]
    for (i, path, rel, out_dir, out_path, fingerprint), probe in zip(candidates, probes):
        # Animated content (GIF — the format is skipped entirely per policy —
        # and animated WebP) is out of scope for the still-image pipeline:
        # whole-image WebP/AVIF would silently flatten it to its first frame.
        if probe.get("format") == "GIF" or probe.get("animated"):
            reason = "gif" if probe.get("format") == "GIF" else "animated"
            skipped_animated.append({"name": rel.as_posix(), "reason": reason, "src_bytes": path.stat().st_size})
            if args.copy_skipped and not args.in_place:
                out_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, out_dir / path.name)
            print(f"[{i}/{len(files)}] skip ({reason}): {rel.as_posix()}")
            continue
        # Below the NVENC practical minimum (measured RTX 4070, see the
        # NVENC_MIN_* constants) the GPU session is refused and the encode
        # silently falls back to CPU rav1e — skip instead. width == 0
        # (unreadable header) keeps the old always-encode behavior.
        pw, ph = probe.get("width", 0) or 0, probe.get("height", 0) or 0
        if 0 < pw < NVENC_MIN_WIDTH or 0 < ph < NVENC_MIN_HEIGHT:
            skipped_small.append({"name": rel.as_posix(), "width": pw, "height": ph, "src_bytes": path.stat().st_size})
            if args.copy_skipped and not args.in_place:
                out_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, out_dir / path.name)
            print(f"[{i}/{len(files)}] skip (below NVENC min {NVENC_MIN_WIDTH}x{NVENC_MIN_HEIGHT}): {rel.as_posix()}")
            continue
        # Pre-filter: a JPEG already compressed harder than our cq target can
        # only grow or waste encode time — keep it and move on (zero cost).
        if args.min_jpeg_quality > 0 and path.suffix.lower() in (".jpg", ".jpeg"):
            q = estimate_jpeg_quality(path)
            if 0 < q < args.min_jpeg_quality:
                if args.copy_skipped and not args.in_place:
                    out_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, out_dir / path.name)
                skipped_quality.append({"name": rel.as_posix(), "estimated_quality": q, "src_bytes": path.stat().st_size})
                print(f"[{i}/{len(files)}] skip (source JPEG quality ~{q:.0f} < {args.min_jpeg_quality:.0f}): {rel.as_posix()}")
                continue
        out_dir.mkdir(parents=True, exist_ok=True)
        pixels = probe.get("width", 0) * probe.get("height", 0)
        tasks.append(
            {
                "index": i,
                "source_fingerprint": fingerprint,
                "path": str(path),
                "name": rel.as_posix(),
                "out_path": str(out_path),
                "cq": args.mid_cq if 0 < pixels <= MID_AVIF_MAX_MP * 1e6 else args.cq,
                "auto_quality": args.auto_quality,
                "device": args.device,
                "preset": args.preset,
                "keep_smaller": args.keep_smaller,
                "copy_skipped": args.copy_skipped and not args.in_place,
                "in_place": args.in_place,
                "transparent_format": args.transparent_format,
                "webp_quality": args.webp_quality,
                "webp_method": args.webp_method,
                "webp_lossless_max_mb": args.webp_lossless_max_mb,
                "opaque_format": args.opaque_format,
                "opaque_webp_quality": args.opaque_webp_quality,
                "opaque_webp_max_mp": args.opaque_webp_max_mp,
                "oversize_format": args.oversize_format,
                "oversize_webp_quality": args.oversize_webp_quality,
                "oversize_webp_method": args.oversize_webp_method,
                "oversize_max_edge": args.oversize_max_edge,
                **probe,
            }
        )

    # Longest job first: a cost-ordered submission keeps every worker busy
    # through the tail instead of ending with one worker grinding on the
    # slowest image (CPU-fallback oversize / transparent AVIF encodes).
    # Break cost ties by exact size: equal-megapixel but differently shaped
    # sources (1080x1920 vs 1440x1440) would otherwise interleave by path and
    # break each worker's NVENC context-cache run on every such file.
    tasks.sort(key=lambda t: (t["cost"], t["width"], t["height"]), reverse=True)

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

    # Session-reuse budget (OPTIMIZATION_PROPOSALS Q): each worker's NVENC
    # context cache HOLDS its sessions open, so workers * capacity must stay
    # within the driver's concurrent-session cap. workers=8 -> 1 (never worse
    # than the old open/close-per-image), workers=4 -> 2, 1 worker -> 8.
    ctx_cache = max(1, NVENC_SESSION_LIMIT // workers)
    os.environ["NVAVIF_CTX_CACHE"] = str(ctx_cache)

    total_in = total_out = 0
    total_px = 0
    kept_src_bytes = 0
    kept_source = 0
    failures: list[str] = []
    rows: list[dict] = []
    # Initialized outside `if tasks:` — a fully-resumed rerun (every source
    # already handled) reaches the report write with zero tasks and used to
    # die there with UnboundLocalError on `interrupted`.
    interrupted = False
    started = time.perf_counter()

    sampler = ResourceSampler()
    if args.report != Path("none"):
        sampler.start()

    if tasks:
        print(f"encoding {len(tasks)} images with {workers} workers (device={args.device})")
        stream_f = None
        if args.report != Path("none"):
            report_path = args.report if args.report.is_absolute() else args.dst / args.report
            stream_path = report_path.parent / (report_path.stem + ".stream.jsonl")
            stream_path.parent.mkdir(parents=True, exist_ok=True)
            # A leading newline isolates a truncated final record from the next run.
            stream_f = open(stream_path, "a", encoding="utf-8", buffering=1)
            stream_f.write("\n" + json.dumps({
                "stream": True, "src": str(args.src.resolve()), "dst": str(args.dst.resolve()),
                "cq": args.cq, "mid_cq": args.mid_cq, "auto_quality": args.auto_quality,
                "resume_version": 2, "resume_config": current_resume_config,
            }) + "\n")
        pool = None
        try:
            with ProcessPoolExecutor(max_workers=workers, max_tasks_per_child=10000) as pool:
                # Sliding-window submission: at most 4xworkers tasks in flight,
                # so a 1M-source run keeps main-process memory flat instead of
                # materializing a million task dicts + Futures up front (GBs).
                inflight: dict = {}
                task_iter = iter(tasks)
                window = max(workers * 4, 16)

                def _fill():
                    while len(inflight) < window:
                        try:
                            t = next(task_iter)
                        except StopIteration:
                            return
                        try:
                            fut = pool.submit(_encode_task, t)
                        except RuntimeError:  # executor broken: stop feeding
                            return
                        inflight[fut] = t

                _fill()
                while inflight:
                    done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
                    for future in done:
                        task = inflight.pop(future)
                        tag = f"[{task['index']}/{len(files)}]"
                        try:
                            result = future.result()
                            row = result["row"]
                            row["source_fingerprint"] = task["source_fingerprint"]
                            rows.append(row)
                            if stream_f is not None:
                                stream_f.write(json.dumps(row, ensure_ascii=False) + "\n")
                            if row["action"] == "skipped_slow":
                                kept_source += 1
                                kept_src_bytes += row["src_bytes"]
                                print(f"{tag} {row['name']}: skipped slow path ({row['reason']})")
                                continue
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
                            failures.append(f"{task.get('name', task['path'])}: {exc}")
                            if stream_f is not None:
                                stream_f.write(json.dumps({
                                    "index": task["index"], "name": task.get("name", task["path"]),
                                    "action": "failed", "error": str(exc),
                                }, ensure_ascii=False) + "\n")
                            print(f"{tag} FAIL {task.get('name', task['path'])}: {exc}")
                            if args.copy_skipped and not args.in_place:
                                try:
                                    # failed source is copied too, so the output folder
                                    # still mirrors the input count — into the source's
                                    # mirrored subfolder (out_path's parent), not the
                                    # output root, which would flatten recursive runs
                                    # and collide same-named sources
                                    fail_dir = Path(task["out_path"]).parent
                                    fail_dir.mkdir(parents=True, exist_ok=True)
                                    shutil.copy2(task["path"], fail_dir / Path(task["path"]).name)
                                except Exception:
                                    pass
                    _fill()
        except KeyboardInterrupt:
            interrupted = True
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
            print("\n[interrupted] queued tasks cancelled; partial report will be written")
        finally:
            if stream_f is not None:
                stream_f.close()

    elapsed = time.perf_counter() - started
    resource = sampler.stop()

    n = len(rows)
    rows.sort(key=lambda r: r["name"])
    n_resized = sum(1 for r in rows if r.get("resized_from"))
    print("\n=== summary ===")
    print(f"encoded {sum(1 for r in rows if r['action'] == 'encoded')}/{len(files)} images in {elapsed:.1f} s ({workers} workers)")
    if n_resized:
        print(f"resized: {n_resized} sources with longer edge > {args.oversize_max_edge} (LANCZOS, originals in 'resized_from')")
    if skipped_quality:
        print(f"pre-filter: {len(skipped_quality)} JPEG sources below quality {args.min_jpeg_quality:.0f} kept as-is")
    if skipped_animated:
        print(f"animated: {len(skipped_animated)} GIF/animated-WebP sources skipped (still-image pipeline)")
    if kept_source:
        print(f"kept: {kept_source} sources unchanged (size guard or skipped slow path)")
    if skipped_small:
        print(f"small: {len(skipped_small)} sources below the NVENC minimum kept as-is")
    if skipped_prev_kept:
        print(f"rerun: {len(skipped_prev_kept)} sources already kept as-is by the previous run skipped")
    if args.copy_skipped and not args.in_place and (kept_source or skipped_quality or skipped_animated or skipped_small):
        print(f"copy-skipped: {kept_source + len(skipped_quality) + len(skipped_animated) + len(skipped_small)} sources copied unchanged into the output folder")
    if total_px:
        print(
            f"encoded: {total_px/1e6:.1f} MP, throughput {total_px/1e6/max(elapsed,1e-6):.2f} MP/s, "
            f"{total_in/1e6:.1f} MB -> {total_out/1e6:.1f} MB ({total_in/max(total_out,1):.2f}x, "
            f"saved {100*(1-total_out/max(total_in,1)):.0f}%)"
        )
        final_store = total_out + kept_src_bytes
        src_all = total_in + kept_src_bytes + sum(s["src_bytes"] for s in skipped_quality) + sum(s["src_bytes"] for s in skipped_small)
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
            **({"interrupted": True} if interrupted else {}),
            "cq": args.cq,
            "mid_cq": args.mid_cq,
            "auto_quality": args.auto_quality,
            "device": args.device,
            "preset": args.preset,
            "workers": workers,
            "min_jpeg_quality": args.min_jpeg_quality,
            "keep_smaller": args.keep_smaller,
            "copy_skipped": args.copy_skipped and not args.in_place,
            "in_place": args.in_place,
            "transparent_format": args.transparent_format,
            "webp_quality": args.webp_quality,
            "webp_method": args.webp_method,
            "webp_lossless_max_mb": args.webp_lossless_max_mb,
            "opaque_format": args.opaque_format,
            "opaque_webp_quality": args.opaque_webp_quality,
            "opaque_webp_max_mp": args.opaque_webp_max_mp,
            "oversize_format": args.oversize_format,
            "oversize_webp_quality": args.oversize_webp_quality,
            "oversize_webp_method": args.oversize_webp_method,
            "oversize_max_edge": args.oversize_max_edge,
            "alpha_rav1e_threads": alpha_threads,
            "color_rav1e_threads": args.color_rav1e_threads,
            "ctx_cache_per_worker": ctx_cache,
            "summary": {
                "total": len(files),
                "encoded": len(enc_rows),
                "kept_source_bigger": kept_source,
                "skipped_low_quality_jpeg": len(skipped_quality),
                "skipped_animated": len(skipped_animated),
                "skipped_small": len(skipped_small),
                "rerun_skipped_prev_kept": len(skipped_prev_kept),
                "resized": n_resized,
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
            "skipped_animated": skipped_animated,
            "skipped_small": skipped_small,
            "images": rows,
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"report: {report_path}")
        if interrupted:
            print(f"partial run: {len(rows)}/{len(files)} encoded rows recorded; "
                  f"rerun with the same arguments to resume (existing outputs are skipped)")


if __name__ == "__main__":
    main()
