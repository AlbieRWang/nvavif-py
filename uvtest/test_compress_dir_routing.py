"""Routing regression tests for compress_dir.py (C3, REVIEW_FINDINGS 2026-09-13).

Script-style asserts — no pytest dependency; any failure raises and exits non-zero.
The --fast suite uses tiny images and AVIF test doubles without requiring a GPU.
The full suite also runs real GPU AVIF encoding. Animated/GIF skipping lives in main()'s scan
loop (not the worker) and is intentionally not covered here.

Run: uv run python uvtest/test_compress_dir_routing.py
"""
from __future__ import annotations

import sys
import tempfile
import time
import io
import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compress_dir as cd

CHECKS = 0
rng = np.random.default_rng(11)


def ok(cond: bool, label: str) -> None:
    global CHECKS
    if not cond:
        raise AssertionError(f"FAIL: {label}")
    CHECKS += 1
    print(f"  ok: {label}")


def make_task(path: Path, out_path: Path, **over: object) -> dict:
    probe = cd.probe_cost(path)
    task = {
        "path": str(path),
        "name": path.name,
        "out_path": str(out_path),
        "in_place": False,
        "cq": 20,
        "auto_quality": False,
        "device": "gpu",
        "preset": 7,
        "keep_smaller": True,
        "copy_skipped": False,
        "transparent_format": "webp",
        "webp_quality": 90,
        "webp_method": 4,
        "webp_lossless_max_mb": 1.0,
        "opaque_format": "avif",
        "opaque_webp_quality": 90,
        "oversize_format": "webp",
        "oversize_webp_quality": 80,
        "oversize_webp_method": 2,
        "oversize_max_edge": 0,
        **probe,
        **over,
    }
    return task


def run_task(task: dict) -> dict:
    res = cd._encode_task(task)
    if not res.get("ok"):
        raise AssertionError(f"worker failed: {res}")
    return res["row"]


def fast_regressions() -> None:
    parser = cd.build_parser()
    for argv, expected in (([], None), (["--auto-quality", "88"], 88),
                           (["--fixed-cq"], None), (["--no-fixed-cq"], 88)):
        args = parser.parse_args(argv)
        cd.resolve_quality_mode(args, argv, parser)
        ok(args.auto_quality == expected, f"quality mode {argv}")
    args = parser.parse_args([])
    ok(args.opaque_format == "auto" and args.opaque_webp_max_mp == 2, "default pixel routing")
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent, prefix="_t_routing_fast_") as tmp:
        root = Path(tmp)
        src, dst = root / "src", root / "dst"
        src.mkdir()
        dst.mkdir()
        p = src / "alpha.png"
        Image.new("RGBA", (16, 16), (240, 20, 10, 80)).save(p)
        row = run_task(make_task(p, dst / "default.avif", keep_smaller=False))
        with Image.open(dst / "default.webp") as decoded:
            ok(row["format"] == "webp" and decoded.getchannel("A").getextrema() == (80, 80),
               "default transparent route keeps alpha in real WebP")
        for oversize in (False, True):
            task = make_task(p, dst / "alpha.avif", transparent_format="avif",
                             opaque_format="webp", keep_smaller=False)
            if oversize:
                task["width"] = 9000
            with patch.object(cd.nv, "encode_file", return_value=(b"avif-test", 20)) as encode:
                row = run_task(task)
            ok(row["action"] == "skipped_slow" and encode.call_count == 0,
               f"transparent AVIF skips CPU alpha, oversize={oversize}")

        for fmt in ("png", "webp"):
            out = dst / f"keep-{fmt}.avif"
            task = make_task(p, out, transparent_format=fmt, copy_skipped=True)
            with patch.object(Image.Image, "save", lambda self, fp, *a, **kw: Path(fp).write_bytes(b"x" * 4096)):
                row = run_task(task)
            ok(row["action"] == "kept_source" and (dst / p.name).read_bytes() == p.read_bytes(),
               f"{fmt} keep-smaller copies original")
            ok(not out.with_suffix(f".{fmt}").exists(), f"{fmt} rejected output absent")
            (dst / p.name).unlink()

        opaque = src / "opaque.png"
        Image.new("RGB", (132, 70), "red").save(opaque)
        for threshold, expected in ((0.00924, "webp"), (0.009239, "avif"), (0, "avif")):
            with patch.object(cd.nv, "encode_file", return_value=(b"gpu", 20)):
                row = run_task(make_task(opaque, dst / "threshold.avif", opaque_format="auto",
                                        opaque_webp_max_mp=threshold, keep_smaller=False))
            ok(row["format"] == expected, f"pixel threshold {threshold} -> {expected}")
        for overrides in ({"width": 17000}, {"width": 9000, "oversize_format": "avif"},
                          {"device": "cpu"}, {"width": 0}):
            with patch.object(cd.nv, "encode_file") as encode:
                row = run_task(make_task(opaque, dst / "slow.avif", copy_skipped=True, **overrides))
            ok(row["action"] == "skipped_slow" and not encode.called,
               f"no CPU fallback for {overrides}")
            ok((dst / opaque.name).read_bytes() == opaque.read_bytes(), "slow skip copies original")
        with patch.object(cd.nv, "encode_file", return_value=(b"gpu", 20)) as encode:
            run_task(make_task(opaque, dst / "gpu.avif", device="auto", keep_smaller=False))
            ok(encode.call_args.kwargs["device"] == "gpu", "auto batch uses strict GPU encoding")
        with patch.object(cd.nv, "encode_file", side_effect=RuntimeError("GPU unavailable")) as encode:
            try:
                cd._encode_task(make_task(opaque, dst / "gpu-error.avif", device="auto"))
            except RuntimeError:
                pass
            else:
                raise AssertionError("GPU error must propagate without fallback")
            ok(encode.call_count == 1, "GPU failure never retries CPU")
        with patch.object(Image.Image, "save", side_effect=lambda fp, *a, **kw: Path(fp).write_bytes(b"webp")) as save:
            run_task(make_task(p, dst / "method.avif", webp_method=6, keep_smaller=False))
            ok(save.call_args.kwargs["method"] == 6, "WebP keeps requested effort below 8192")
        with patch.object(Image.Image, "save", side_effect=lambda fp, *a, **kw: Path(fp).write_bytes(b"webp")) as save:
            over = make_task(p, dst / "method-oversize.avif", webp_method=6, keep_smaller=False)
            over["width"] = 9000
            run_task(over)
            ok(save.call_args.kwargs["method"] == 2, "WebP caps expensive effort on oversize sources")
        for fmt in ("png", "webp", "avif"):
            out = dst / f"failure.{fmt}"
            out.write_bytes(b"previous-output")
            task = make_task(opaque if fmt == "avif" else p, out.with_suffix(".avif"),
                             transparent_format=fmt, keep_smaller=False)
            with patch.object(cd.nv, "encode_file", return_value=(b"new-output", 20)), \
                 patch.object(cd.os, "replace", side_effect=OSError("injected replace failure")):
                try:
                    cd._encode_task(task)
                except OSError:
                    pass
                else:
                    raise AssertionError("replace failure must propagate")
            ok(out.read_bytes() == b"previous-output", f"{fmt} failure preserves prior output")
            ok(not list(dst.glob("*.tmp")), f"{fmt} failure cleans temporary output")
        ok(p.exists(), "source survives failed output replacement")

        small_src = root / "small-src"
        nested = small_src / "nested"
        nested.mkdir(parents=True)
        Image.new("RGB", (8, 8)).save(nested / "small.png")
        small_dst = root / "small-dst"
        with patch.object(sys, "argv", ["compress_dir.py", "--src", str(small_src), "--dst", str(small_dst),
                                       "--report", str(root / "report.json")]):
            cd.main()
        ok((small_dst / "nested" / "small.png").read_bytes() == (nested / "small.png").read_bytes(),
           "small-image skip creates mirrored subdirectory")


def resume_regressions() -> None:
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent, prefix="_t_resume_") as tmp:
        root = Path(tmp)
        src, dst = root / "src", root / "dst"
        src.mkdir()
        calls = []

        def kept_worker(task):
            calls.append(task["name"])
            p = Path(task["path"])
            if task["copy_skipped"]:
                shutil.copy2(p, Path(task["out_path"]).parent / p.name)
            return {"row": {"name": task["name"], "action": "kept_source", "format": "webp",
                            "src_bytes": p.stat().st_size, "avif_bytes": 10000}}

        def run(*options):
            calls.clear()
            argv = ["compress_dir.py", "--src", str(src), "--dst", str(dst),
                    "--fixed-cq", "--workers", "1", *options]
            with patch.object(sys, "argv", argv), \
                 patch.object(cd, "probe_cost", wraps=cd.probe_cost) as probe, \
                 patch.object(cd, "_encode_task", side_effect=kept_worker), \
                 patch.object(cd, "ProcessPoolExecutor", side_effect=lambda **kw: ThreadPoolExecutor(max_workers=1)), \
                 patch.object(cd, "ResourceSampler") as sampler, redirect_stdout(io.StringIO()):
                sampler.return_value.stop.return_value = {}
                cd.main()
                if probe.call_count != len(calls):
                    raise AssertionError("only pending files may be probed")
            return set(calls)

        a = src / "a.png"
        Image.new("RGB", (132, 70), "red").save(a)
        ok(run() == {"a.png"}, "resume first run records kept source")
        ok(run() == set(), "resume unchanged source skips encoding despite copied PNG")
        Image.new("RGB", (132, 70), "blue").save(src / "b.png")
        ok(run() == {"b.png"}, "resume encodes only newly added source")
        ok(run() == set(), "resume preserves older records across mixed and empty runs")
        stat = a.stat()
        os.utime(a, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
        ok(run() == {"a.png"}, "resume changed mtime invalidates old verdict despite copied PNG")
        with a.open("ab") as stream:
            stream.write(b"changed-size")
        os.utime(a, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
        ok(run() == {"a.png"}, "resume changed size invalidates verdict with same mtime")
        ok(run("--cq", "26") == {"a.png", "b.png"}, "resume changed quality invalidates verdicts")
        ok(run("--cq", "26", "--workers", "2") == set(), "resume worker count does not force encoding")
        (dst / "a.png").unlink()
        ok(run("--cq", "26") == set() and (dst / "a.png").read_bytes() == a.read_bytes(),
           "resume missing copy is restored without encoding")
        log = dst / "compress_report.stream.jsonl"
        with log.open("a", encoding="utf-8") as stream:
            stream.write('{"name": "truncated')
        Image.new("RGB", (132, 70), "green").save(src / "c.png")
        ok(run("--cq", "26") == {"c.png"}, "resume tolerates truncated final line")
        ok(run("--cq", "26") == set(), "resume new header remains readable after truncated line")
        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"name": "a.png", "action": "failed"}) + "\n")
        ok(run("--cq", "26") == {"a.png"}, "resume latest failure supersedes old kept verdict")
        ok(run("--cq", "26", "--overwrite") == {"a.png", "b.png", "c.png"},
           "resume overwrite forces processing")
        rows = cd.load_resume_rows(log, src, root / "different-dst")
        ok(not rows, "resume rejects records from another destination")
        ok(not cd.load_resume_rows(log, root / "different-src", dst),
           "resume rejects records from another source")
        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"name": "a.png", "action": "encoded"}) + "\n")
        ok("a.png" not in cd.load_resume_rows(log, src, dst),
           "resume latest encoded record supersedes old kept verdict")
        log.write_text(json.dumps({"stream": True, "src": str(src), "dst": str(dst)}) + "\n" +
                       json.dumps({"name": "a.png", "action": "kept_source"}) + "\n", encoding="utf-8")
        ok(run() == {"a.png"}, "resume legacy unverified verdict cannot hide behind copied PNG")


def main() -> None:
    fast_regressions()
    resume_regressions()
    if "--fast" in sys.argv:
        print(f"\n{CHECKS} checks passed")
        return
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent, prefix="_t_c3_") as tmp:
        tmp = Path(tmp)
        src = tmp / "src"
        dst = tmp / "dst"
        src.mkdir()
        dst.mkdir()

        # --- estimate_jpeg_quality: PIL quality is (near-)IJG scale ---
        print("estimate_jpeg_quality:")
        ref = (np.arange(136 * 136 * 3).reshape(136, 136, 3) % 256).astype(np.uint8)
        im = Image.fromarray(ref, "RGB")
        for q in (50, 95):
            p = src / f"q{q}.jpg"
            im.save(p, "JPEG", quality=q)
            est = cd.estimate_jpeg_quality(p)
            ok(abs(est - q) <= 8, f"quality={q} estimated ~{est:.0f}")

        # --- opaque JPEG -> GPU/CPU AVIF route ---
        print("routing:")
        p = src / "photo.jpg"
        im.save(p, "JPEG", quality=92)
        row = run_task(make_task(p, dst / "photo.avif"))
        ok(row["action"] == "encoded" and row["format"] == "avif", "opaque JPEG -> avif")
        ok((dst / "photo.avif").exists() and (dst / "photo.avif").stat().st_size > 0, "avif written")

        # --- transparent PNG -> whole WebP, alpha preserved (R1) ---
        arr = np.zeros((128, 128, 4), np.uint8)
        yy, xx = np.mgrid[0:128, 0:128]
        m = (xx - 64) ** 2 + (yy - 64) ** 2 < 40**2
        arr[..., 0][m] = 220
        arr[..., 3][m] = 255
        p = src / "trans.png"
        Image.fromarray(arr, "RGBA").save(p)
        row = run_task(make_task(p, dst / "trans.avif"))
        ok(row["action"] == "encoded" and row["format"] == "webp", "transparent PNG -> webp")
        with Image.open(dst / "trans.webp") as out:
            ok("A" in out.getbands() and out.getchannel("A").getextrema()[0] == 0, "webp keeps real alpha")

        # --- constant-opaque RGBA stays on the AVIF route (R7) ---
        # gradient content so the AVIF genuinely wins the keep-smaller guard
        arr_op = np.dstack([ref, np.full(ref.shape[:2], 255, np.uint8)])
        p = src / "constopaque.png"
        Image.fromarray(arr_op, "RGBA").save(p)
        row = run_task(make_task(p, dst / "constopaque.avif"))
        ok(row["format"] == "avif" and row["action"] in ("encoded", "kept_source"),
           "constant-opaque RGBA -> avif route (keep-smaller may keep it)")

        # --- opaque WebP route stays RGB, no alpha plane (B8/R8) ---
        # low-frequency noise: WebP q90 reliably beats PNG so the row encodes
        smooth = Image.fromarray(rng.integers(0, 256, (16, 16), dtype=np.uint8), "L").resize((128, 128), Image.BICUBIC)
        arr_rgb = np.dstack([np.asarray(smooth)] * 3)
        p = src / "opaque.png"
        Image.fromarray(arr_rgb, "RGB").save(p)
        row = run_task(make_task(p, dst / "opaque.avif", opaque_format="webp"))
        ok(row["action"] == "encoded" and row["format"] == "webp", "opaque -> webp route")
        with Image.open(dst / "opaque.webp") as out:
            ok("A" not in out.getbands(), "opaque webp has no alpha plane")

        # --- oversize image -> whole WebP, RGB, no resize (R4) ---
        wide = np.asarray(
            Image.fromarray(rng.integers(0, 256, (5, 450), dtype=np.uint8), "L").resize((9000, 100), Image.BICUBIC)
        )
        wide = np.dstack([wide] * 3)
        p = src / "wide.png"
        Image.fromarray(wide, "RGB").save(p)
        row = run_task(make_task(p, dst / "wide.avif"))
        ok(row["action"] == "encoded" and row["format"] == "webp", "oversize -> webp route")
        ok("resized_from" not in row, "oversize_max_edge=0 does not resize")

        # --- keep-smaller guard: solid color PNG survives (R11) ---
        # device="gpu" (the batch default): NVENC's bitstream overhead loses to
        # the tiny PNG; the CPU rav1e fallback actually compresses flat fields
        # below PNG size and would encode instead.
        p = src / "solid.png"
        # 132x70 (above the NVENC minimum) + optimize: 180-byte PNG sits below
        # the ~300-byte AVIF floor at ANY cq, so keep-smaller deterministically
        # keeps the source instead of encoding
        Image.new("RGB", (132, 70), (250, 250, 250)).save(p, optimize=True)
        row = run_task(make_task(p, dst / "solid.avif", device="gpu"))
        ok(row["action"] == "kept_source" and not (dst / "solid.avif").exists(), "keep-smaller keeps source")

    print(f"\n{CHECKS} checks passed")


if __name__ == "__main__":
    started = time.perf_counter()
    try:
        main()
    finally:
        print(f"Test elapsed: {time.perf_counter() - started:.3f} s")
