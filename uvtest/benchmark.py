"""Benchmark supported test images and compare the GPU and CPU encoders.

Run from this directory with the project-local wheel and FFmpeg DLLs:
    python benchmark.py

Images whose width or height exceeds --skip-max-dimension are reported but
skipped because NVENC cannot encode them at their original dimensions.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

ROOT = Path(__file__).resolve().parent.parent

# Keep the benchmark self-contained when run against the project-local wheel.
for dll_dir in (ROOT / "ffmpeg-out" / "bin", ROOT / "msys64" / "mingw64" / "bin"):
    if dll_dir.is_dir() and hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(dll_dir))

import nvavif_py


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * fraction))]


def summarize(samples: list[dict[str, Any]], total_pixels: int) -> dict[str, float | int]:
    times = [sample["encode_ms"] for sample in samples]
    sizes = [sample["bytes"] for sample in samples]
    total_seconds = sum(times) / 1000.0
    result: dict[str, float | int] = {
        "count": len(samples),
        "total_pixels": total_pixels,
        "total_megapixels": total_pixels / 1_000_000,
        "total_encode_ms": sum(times),
        "aggregate_megapixels_per_second": (total_pixels / 1_000_000) / total_seconds,
        "mean_encode_ms": statistics.mean(times),
        "median_encode_ms": statistics.median(times),
        "p95_encode_ms": percentile(times, 0.95),
        "min_encode_ms": min(times),
        "max_encode_ms": max(times),
        "mean_output_bytes": statistics.mean(sizes),
        "total_output_bytes": sum(sizes),
        "mean_output_bits_per_pixel": statistics.mean(
            sample["output_bits_per_pixel"] for sample in samples
        ),
    }
    decode_times = [sample["decode_ms"] for sample in samples]
    if decode_times:
        decode_seconds = sum(decode_times) / 1000.0
        result.update(
            {
                "total_decode_ms": sum(decode_times),
                "aggregate_decode_megapixels_per_second": (total_pixels / 1_000_000)
                / decode_seconds,
                "mean_decode_ms": statistics.mean(decode_times),
                "median_decode_ms": statistics.median(decode_times),
                "p95_decode_ms": percentile(decode_times, 0.95),
            }
        )
    return result


def run_batch(
    image_dir: Path,
    skip_max_dimension: int,
    validation_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    samples: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failures: list[str] = []

    files = sorted(path for path in image_dir.iterdir() if path.is_file())
    for index, source in enumerate(files, 1):
        try:
            with Image.open(source) as image:
                width, height = image.size
                has_alpha_metadata = "A" in image.getbands() or "transparency" in image.info
                if has_alpha_metadata:
                    rgba_arr = np.array(image.convert("RGBA"))
                    has_alpha = bool(rgba_arr[:, :, 3].min() < 255)
                else:
                    has_alpha = False
        except Exception as error:
            failures.append(f"{source.name}: probe failed: {error}")
            print(f"[{index:02d}/{len(files)}] FAIL {source.name}: {error}", flush=True)
            continue

        if width > skip_max_dimension or height > skip_max_dimension:
            skipped.append({"name": source.name, "width": width, "height": height})
            print(f"[{index:02d}/{len(files)}] SKIP {source.name}: {width}x{height}", flush=True)
            continue

        effective_width = width - width % 2
        effective_height = height - height % 2
        pixels = effective_width * effective_height
        started = time.perf_counter()
        try:
            encoded = nvavif_py.encode_file(source, device="auto")
        except Exception as error:
            failures.append(f"{source.name}: encode failed: {error}")
            print(f"[{index:02d}/{len(files)}] FAIL {source.name}: {error}", flush=True)
            continue

        elapsed_ms = (time.perf_counter() - started) * 1000
        validation_path = validation_dir / f"{index:03d}.avif"
        validation_path.write_bytes(encoded)
        decode_started = time.perf_counter()
        try:
            with Image.open(validation_path) as output_image:
                output_image.load()
                pillow_mode = output_image.mode
            decoded = nvavif_py.decode_file(validation_path)
            expected_channels = 4 if has_alpha else 3
            if decoded.ndim != 3 or decoded.shape[2] != expected_channels:
                raise RuntimeError(
                    f"decoded shape {decoded.shape}, expected {expected_channels} channels"
                )
        except Exception as error:
            failures.append(f"{source.name}: validation failed: {error}")
            print(f"[{index:02d}/{len(files)}] FAIL {source.name}: {error}", flush=True)
            continue
        decode_ms = (time.perf_counter() - decode_started) * 1000
        sample = {
            "name": source.name,
            "width": width,
            "height": height,
            "effective_width": effective_width,
            "effective_height": effective_height,
            "megapixels": pixels / 1_000_000,
            "alpha": has_alpha,
            "encode_ms": elapsed_ms,
            "decode_ms": decode_ms,
            "megapixels_per_second": pixels / 1_000_000 / (elapsed_ms / 1000),
            "bytes": len(encoded),
            "output_bits_per_pixel": len(encoded) * 8 / pixels,
            "pillow_mode": pillow_mode,
            "decoded_shape": list(decoded.shape),
        }
        samples.append(sample)
        print(
            f"[{index:02d}/{len(files)}] OK {source.name}: "
            f"encode={elapsed_ms:.1f} ms, decode={decode_ms:.1f} ms, {len(encoded):,} B, "
            f"{sample['megapixels_per_second']:.1f} MP/s",
            flush=True,
        )

    return samples, skipped, failures


def run_device_comparison(repeats: int) -> dict[str, Any]:
    image = np.random.default_rng(2026).integers(
        0, 256, (1024, 1024, 3), dtype=np.uint8
    )
    results: dict[str, Any] = {
        "width": 1024,
        "height": 1024,
        "pixels": 1024 * 1024,
        "repeats": repeats,
    }

    for device in ("gpu", "cpu"):
        timings: list[float] = []
        sizes: list[int] = []
        # Warm up the encoder once so DLL loading and one-time initialization
        # do not dominate the reported steady-state samples.
        nvavif_py.encode_file(image, device=device)
        for _ in range(repeats):
            started = time.perf_counter()
            encoded = nvavif_py.encode_file(image, device=device)
            timings.append((time.perf_counter() - started) * 1000)
            sizes.append(len(encoded))
        results[device] = {
            "mean_encode_ms": statistics.mean(timings),
            "median_encode_ms": statistics.median(timings),
            "min_encode_ms": min(timings),
            "max_encode_ms": max(timings),
            "megapixels_per_second": 1.048576 / (statistics.mean(timings) / 1000),
            "mean_output_bytes": statistics.mean(sizes),
            "samples_ms": timings,
            "samples_bytes": sizes,
        }
        print(
            f"{device.upper()} 1024x1024: "
            f"mean={results[device]['mean_encode_ms']:.1f} ms, "
            f"median={results[device]['median_encode_ms']:.1f} ms, "
            f"{results[device]['megapixels_per_second']:.1f} MP/s",
            flush=True,
        )

    results["speedup_cpu_over_gpu"] = (
        results["cpu"]["mean_encode_ms"] / results["gpu"]["mean_encode_ms"]
    )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-max-dimension",
        type=int,
        default=8192,
        help="skip an image when either dimension exceeds this value (default: 8192)",
    )
    parser.add_argument(
        "--device-repeats",
        type=int,
        default=5,
        help="steady-state repetitions for the synthetic GPU/CPU comparison (default: 5)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "benchmark_results.json",
        help="JSON report path",
    )
    args = parser.parse_args()

    image_dir = ROOT / "test_imgs"
    started = time.perf_counter()
    print(f"NVENC supported: {nvavif_py.is_supported()}", flush=True)
    with tempfile.TemporaryDirectory(
        prefix=".benchmark-",
        dir=str(Path(__file__).resolve().parent),
    ) as validation_dir:
        samples, skipped, failures = run_batch(
            image_dir,
            args.skip_max_dimension,
            Path(validation_dir),
        )
        device_comparison = run_device_comparison(args.device_repeats)

    opaque = [sample for sample in samples if not sample["alpha"]]
    alpha = [sample for sample in samples if sample["alpha"]]
    total_pixels = sum(
        sample["effective_width"] * sample["effective_height"] for sample in samples
    )
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "module": str(Path(nvavif_py.__file__).resolve()),
        "configuration": {
            "image_dir": str(image_dir),
            "skip_max_dimension": args.skip_max_dimension,
            "batch_device": "auto",
        },
        "batch": {
            "summary": summarize(samples, total_pixels),
            "opaque_summary": summarize(opaque, sum(s["effective_width"] * s["effective_height"] for s in opaque)),
            "alpha_summary": summarize(alpha, sum(s["effective_width"] * s["effective_height"] for s in alpha)),
            "tested": samples,
            "skipped": skipped,
            "failures": failures,
        },
        "device_comparison": device_comparison,
        "wall_clock_seconds": time.perf_counter() - started,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Report written to {args.output}", flush=True)
    print(
        f"BATCH tested={len(samples)} skipped={len(skipped)} failures={len(failures)} "
        f"aggregate={report['batch']['summary']['aggregate_megapixels_per_second']:.1f} MP/s",
        flush=True,
    )

    if failures:
        raise SystemExit("\n".join(failures))


if __name__ == "__main__":
    main()
