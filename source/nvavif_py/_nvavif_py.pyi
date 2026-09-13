from typing import Literal

# NOTE: keep in sync with src/lib.rs (#[pyfunction] signatures and
# #[pyclass] enums). The enums are PyO3 pyclasses, NOT IntEnum — members
# cannot be used as ints. The Python-facing API lives in nvavif_py/__init__.py
# (encode_file / decode_file), which always passes explicit arguments.


class ColorDepth:
    EIGHT_BIT: ColorDepth
    TEN_BIT: ColorDepth


class Chroma:
    YUV420: Chroma
    YUV444: Chroma


class ColorMatrix:
    BT601: ColorMatrix
    BT709: ColorMatrix
    BT2020: ColorMatrix


def is_hardware_supported() -> bool:
    """True if the current GPU/driver supports AV1 NVENC encoding."""
    ...


def encode_avif(
    pixels: bytes,
    width: int,
    height: int,
    input_dtype: Literal["u8", "u16", "f32"],
    cq: int = 20,
    auto_cq: bool = False,
    target_ssim: float = 0.985,
    alpha_cq: int | None = None,
    preset: int = 7,
    depth: ColorDepth = ColorDepth.EIGHT_BIT,
    chroma: Chroma = Chroma.YUV420,
    matrix: ColorMatrix = ColorMatrix.BT709,
    exif: bytes | None = None,
    device: Literal["auto", "gpu", "cpu"] = "auto",
    tone_map: bool = True,
) -> tuple[bytes, int]:
    """Encode raw RGB(A) pixel data into an AVIF image.

    Returns a tuple ``(avif_bytes, effective_color_cq)`` — the second element
    is the calibrated CQ when auto_cq is enabled, otherwise the clamped cq.

    ``tone_map`` applies the ACES filmic curve to f32 input; the Python
    wrapper sets it False for SDR float sources (max <= 1.0).

    The buffer must be width*height*channels bytes exactly, with channels
    3 (RGB) or 4 (RGBA); violations raise ValueError.
    """
    ...


def decode_avif(path: str, threads: int = 0) -> dict:
    """Decode the first frame of an AVIF file with dav1d.

    Returns a dict with 'data' (bytes), 'width' (int), 'height' (int),
    'channels' (int: 3 = RGB, 4 = RGBA).
    """
    ...
