from enum import IntEnum, Enum
from typing import Literal

import numpy as np
from PIL import Image


class ColorDepth(IntEnum):
    """
    Encoding color depth.

    Attributes:
        EIGHT_BIT (int): 8-bit color depth.
        TEN_BIT (int): 10-bit color depth.
    """
    EIGHT_BIT = 0
    TEN_BIT = 1


class Chroma(IntEnum):
    """
    Chroma Subsampling mode.

    YUV420: Standard mode (nvidia-native NV12 or P010).
            Color information is compressed by a factor of 4.
            Optimal for photos and videos.
    YUV444: Mode without loss of color resolution. Each pixel has its own color.
            Ideal for graphics, text, and art with fine details.

    Attributes:
        YUV420 (int): 4:2:0 mode.
        YUV444 (int): 4:4:4 mode.
    """
    YUV420 = 0
    YUV444 = 1


class ColorMatrix(IntEnum):
    """
    Enumeration of color transformation matrices

    Attributes:
        BT601: standard matrix for SDTV (Rec. 601).
        BT709: standard matrix for HDTV (Rec. 709).
        BT2020: standard matrix for  UHDTV (Rec. 2020).
    """
    BT601 = 0
    BT709 = 1
    BT2020 = 2


def is_hardware_supported() -> bool:
    """
    Checks if the current graphics card supports AV1 hardware encoding.

    Support requires an NVIDIA Ada Lovelace (RTX 40xx) or Blackwell (RTX 50xx)
    architecture GPU and above, along with the appropriate driver.

    Returns:
        bool: True if hardware encoding is supported, otherwise False.

    Note:
        This function is a Python binding for a Rust implementation.
    """
    ...


def encode_avif(
        pixels: bytes,
        width: int,
        height: int,
        input_dtype: Literal['u8', 'u16', 'f32'],
        cq: int = 20,
        auto_cq: bool = False,
        target_ssim: float = 0.992,
        alpha_cq: int | None = None,
        preset: int = 7,
        depth: ColorDepth = ColorDepth.EIGHT_BIT,
        chroma: Chroma = Chroma.YUV420,
        matrix: ColorMatrix = ColorMatrix.BT709,
        exif: bytes | None = None,
        device: Literal['auto', 'gpu', 'cpu'] = 'auto'
) -> bytes:
    """
    Encodes raw pixel data into an AVIF format image.

    Args:
        pixels (bytes): Raw pixel data to be encoded.
        width (int): Image width in pixels.
        height (int): Image height in pixels.
        input_dtype: (Literal['u8', 'u16', 'f32']): data type of the input pixel buffer
        cq (int): Quantization level for color channels (0-51), where 0 is lossless.
        auto_cq (bool, optional): enables automatic selection of cq to achieve the target SSIM.
        target_ssim (float, optional): quality perception target to logarithmic SSIM.
        alpha_cq (Optional[int]): Quantization level for the alpha channel, optional.
        preset (int): Encoding speed preset (1-7), where 7 is the most efficient/highest quality.
        depth (ColorDepth, optional): Encoding color bit depth.
        chroma (Chroma, optional): Chroma subsampling scheme.
        matrix (ColorMatrix, optional): color matrix coefficients for YUV conversion
        exif (bytes | None, optional): raw EXIF metadata to embed in the output image.
        device (str, optional): target processing device ("auto", "gpu", "cpu").

    Returns:
        bytes: A byte string containing the encoded AVIF image.

    Raises:
        ValueError: If the encoding parameters are invalid.

    Note:
        The NVENC hardware encoder requires the image width and height to be
        even numbers; otherwise, an encoding error will occur.
    """
    ...


def decode_avif(path: str, threads: int) -> dict:
    """
    Decodes an AVIF file into a numpy.ndarray using CPU-optimized dav1d.
    Returns

    Args:
        path (str): Path to the .avif image.
        threads (int, optional): Number of threads for decoding.
            A value of 0 corresponds to automatic detection (all available cores).
            For batch processing (e.g., in a dataloader), a value of 1 or 2 is recommended
            to prevent CPU thrashing. Defaults to 0.

    Returns:
        a dict with 'data' (bytes), 'width' (int), 'height' (int), 'channels' (int).
    """
    ...
