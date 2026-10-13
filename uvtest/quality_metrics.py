"""Shared image-quality metrics for the codec comparison scripts.

The block-wise SSIM here (8x8 blocks, uniform weights, same shape as the
Rust-side calculate_ssim) catches local artifacts that a single global-window
SSIM averages away — this matters on hard-edged content, where WebP's VP8
block structure lives. All luma metrics run on a box-downscaled image so
100 MP sources stay fast; alpha MAE always runs at full resolution.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None  # oversized sources are legitimate inputs here


def luma_box(img: Image.Image, factor: int = 2) -> np.ndarray:
    """Luma plane of `img`, box-downscaled by `factor` (keeps SSIM cheap)."""
    w, h = img.size
    small = img.convert("L").resize((max(1, w // factor), max(1, h // factor)), Image.BOX)
    return np.asarray(small, dtype=np.float64)


def _ssim_block_map(ref: np.ndarray, test: np.ndarray, block: int = 8) -> np.ndarray:
    """Per-tile SSIM map, shape (tiles_y, tiles_x)."""
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    by = ref.shape[0] // block * block
    bx = ref.shape[1] // block * block
    r = ref[:by, :bx].reshape(by // block, block, bx // block, block)
    s = test[:by, :bx].reshape(by // block, block, bx // block, block)
    mu_r, mu_s = r.mean(axis=(1, 3)), s.mean(axis=(1, 3))
    var_r, var_s = r.var(axis=(1, 3)), s.var(axis=(1, 3))
    cov = (r * s).mean(axis=(1, 3)) - mu_r * mu_s
    num = (2 * mu_r * mu_s + c1) * (2 * cov + c2)
    den = (mu_r**2 + mu_s**2 + c1) * (var_r + var_s + c2)
    return num / den


def ssim_block_stats(ref: np.ndarray, test: np.ndarray, block: int = 8) -> tuple[float, float, int, int]:
    """(mean, worst, worst_x, worst_y) over non-overlapping `block` tiles.

    `worst` is the single lowest-scoring tile; its origin is in *plane*
    coordinates, so callers that downscaled the plane must scale it back
    (compare_runs uses factor=4). Localized artifacts average away in the
    mean, so the location is what makes a regression report actionable.
    """
    m = _ssim_block_map(ref, test, block)
    iy, ix = np.unravel_index(int(np.argmin(m)), m.shape)
    return float(m.mean()), float(m[iy, ix]), int(ix * block), int(iy * block)


def ssim_blocks(ref: np.ndarray, test: np.ndarray, block: int = 8) -> float:
    """Mean SSIM over non-overlapping `block` x `block` blocks of two luma planes."""
    return ssim_block_stats(ref, test, block)[0]


def alpha_mae(ref_a: np.ndarray, test_a: np.ndarray) -> float:
    """Mean absolute error of an alpha plane (0-255 scale, full resolution)."""
    return float(np.abs(ref_a.astype(np.int16) - test_a.astype(np.int16)).mean())


def alpha_mae_worst1(ref_a: np.ndarray, test_a: np.ndarray, pct: float = 1.0) -> float:
    """MAE over the worst `pct`% of alpha pixels (0-255 scale, full resolution).

    The full-plane mean dilutes exactly the errors that matter: an alpha
    artifact is 20+/255 on a thin edge and still reads ~0.05 as a mean.
    Mirrors the dE2000 worst-1% convention.
    """
    err = np.abs(ref_a.astype(np.int16) - test_a.astype(np.int16)).ravel()
    k = max(1, int(round(err.size * pct / 100.0)))
    if k >= err.size:
        return float(err.mean())
    return float(np.partition(err, err.size - k)[err.size - k:].mean())


def alpha_plane(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("RGBA").getchannel("A"), dtype=np.int16)
