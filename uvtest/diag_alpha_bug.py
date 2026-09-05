"""Diagnose the alpha decode bug (DEVELOPMENT_NOTES 11.4).

For each suspect image: encode with encode_file defaults, then compare the
alpha plane of the encoded AVIF as recovered by three readers:

1. PIL source          — ground truth alpha of the input PNG
2. nvavif_py.decode_file — our dav1d pipeline
3. ffmpeg CLI          — independent reference decode of the alpha (auxl) stream

If ffmpeg also disagrees with the source, the alpha AV1 stream itself is bad
(encode-side); if only our decoder disagrees, the bug is in our decode path.

Usage: uv run python uvtest/diag_alpha_bug.py [image ...]
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

import nvavif_py as nv

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "uvtest" / "out" / "alpha_bug"


def _find_ffmpeg() -> str:
    local = ROOT / "ffmpeg-out" / "bin" / "ffmpeg.exe"
    if local.is_file():
        return str(local)
    found = shutil.which("ffmpeg")
    if found:
        return found
    sys.exit("no ffmpeg.exe found (ffmpeg-out/bin or PATH)")


FFMPEG = _find_ffmpeg()

DEFAULT_SUSPECTS = [
    "0-16 07-57-11.png",
    "13 07-56-48-9236.png",
    "0-27 07-57-11.png",  # known-good control
]


def ffmpeg_alpha(avif: Path) -> np.ndarray:
    """Decode the alpha item (stream 0:v:1) to a grayscale PNG via ffmpeg."""
    png = OUT / "ref_alpha.png"
    subprocess.run(
        [str(FFMPEG), "-y", "-loglevel", "error", "-i", str(avif),
         "-map", "0:v:1", "-frames:v", "1", str(png)],
        check=True,
    )
    return np.array(Image.open(png).convert("L"))


def diagnose(name: str) -> None:
    src_path = ROOT / "test_imgs" / name
    OUT.mkdir(parents=True, exist_ok=True)
    avif = OUT / (Path(name).stem + ".avif")

    src = np.array(Image.open(src_path))
    data = nv.encode_file(src_path, cq=20)
    avif.write_bytes(data)

    ours = nv.decode_file(avif)
    ref = ffmpeg_alpha(avif)

    src_a = src[:, :, 3].astype(int)
    our_a = ours[:, :, 3].astype(int)
    ref_a = ref.astype(int)

    our_err = np.abs(our_a - src_a)
    ref_err = np.abs(ref_a - src_a)
    print(f"\n== {name}")
    print(f"   src alpha: uniq={len(np.unique(src_a))} range=({src_a.min()},{src_a.max()})")
    print(f"   ours  vs src: MAE={our_err.mean():7.2f} max={our_err.max():3d}  "
          f"({(our_err > 8).mean() * 100:.1f}% px off)")
    print(f"   ffmpeg vs src: MAE={ref_err.mean():7.2f} max={ref_err.max():3d}  "
          f"({(ref_err > 8).mean() * 100:.1f}% px off)")
    verdict = "ENCODE-side (stream is bad)" if ref_err.mean() > 1 else (
        "DECODE-side (ours only)" if our_err.mean() > 1 else "clean")
    print(f"   verdict: {verdict}")


if __name__ == "__main__":
    names = sys.argv[1:] or DEFAULT_SUSPECTS
    for n in names:
        diagnose(n)
