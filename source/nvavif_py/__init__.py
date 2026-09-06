import io
import os
from enum import IntEnum, Enum
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageOps

from ._nvavif_py import encode_avif, is_hardware_supported, ColorDepth, Chroma, ColorMatrix, decode_avif as _decode_avif_rs

InputType = str | bytes | Path | os.PathLike[str] | os.PathLike[bytes] | io.BytesIO | Image.Image | np.ndarray


class NvencPreset(IntEnum):
    """
    NVENC encoder presets.

    These affect compression efficiency and GPU hardware load.
    For static images, MAX_QUALITY (P7) is recommended.

    Attributes:
        P1_LOW_QUALITY (int): Low quality preset (1).
        P2_MEDIUM_LOW (int): Medium-low quality preset (2).
        P3_MEDIUM (int): Medium quality preset (3).
        P4_MEDIUM_HIGH (int): Medium-high quality preset (4).
        P5_HIGH (int): High quality preset (5).
        P6_VERY_HIGH (int): Very high quality preset (6).
        P7_MAX_QUALITY (int): Maximum quality preset (7).
    """
    P1_LOW_QUALITY = 1
    P2_MEDIUM_LOW = 2
    P3_MEDIUM = 3
    P4_MEDIUM_HIGH = 4
    P5_HIGH = 5
    P6_VERY_HIGH = 6
    P7_MAX_QUALITY = 7


class DataType(str, Enum):
    """
    enumeration of supported data type
    of the input pixel buffer as string constants.

    Attributes:
        U8: unsigned 8-bit integer format identifier.
        U16: unsigned 16-bit integer format identifier.
        F32: 32-bit floating-point format identifier.
    """
    U8 = 'u8'
    U16 = 'u16'
    F32 = 'f32'


class Device(str, Enum):
    """
    enumeration of supported computing devices.

    Attributes:
        GPU: hardware acceleration via graphics processing unit.
        CUDA: alias for GPU acceleration.
        CPU: central processing unit execution.
        AUTO: automatic device selection based on availability.
    """
    GPU = 'gpu'
    CUDA = 'gpu'
    CPU = 'cpu'
    AUTO = 'auto'

def is_supported() -> bool:
    """
    Checks if the current system supports AV1 hardware encoding.

    Note:
        Support requires NVIDIA Ada Lovelace (RTX 40xx) or Blackwell (RTX 50xx) architecture.

    Returns:
        bool: True if AV1 hardware encoding is supported, otherwise False.
    """
    try:
        return is_hardware_supported()
    except Exception:
        return False


def _ensure_srgb(img: Image.Image) -> Image.Image:
    """
    Converts the input image to the sRGB color space if an embedded ICC profile is present.

    If the image contains color profile metadata, it uses ImageCms to perform a transformation
    from the source profile to a standard sRGB profile. If no profile is found, the
    original image is returned without modification.

    Args:
        img (Image.Image): PIL image instance to be checked and potentially converted.

    Returns:
        Image.Image: image transformed to the sRGB color space or the original image
        if no ICC profile was detected.

    Raises:
        PyCMSError: if the color profile conversion fails due to invalid or corrupt ICC data.
    """
    icc = img.info.get('icc_profile')
    if icc:
        try:
            src_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            srgb_profile = ImageCms.createProfile('sRGB')
            return ImageCms.profileToProfile(img, src_profile, srgb_profile)
        except:
            pass
    return img


def _register_pillow_plugin():
    """
    registers avif format support within the pillow library.

    adds handlers for the .avif extension, image/avif mime type, and the internal saver function to the global pillow registry.
    """

    def _save_avif(im: Image.Image, fp, filename, **kwargs):
        """
        Encodes and writes Pillow image data to a file in avif format.

        Args:
            im (Image.Image): pillow image instance to be encoded.
            fp (file-like): output stream or file-like object.
            filename (str | Path): destination file name.
            **kwargs (Any): encoding options such as quality, speed, and metadata settings.

        Note:
            if 'quality' is passed, it is mapped to 'target_quality' and 'auto_cq' defaults to true. exif and icc_profile data are extracted from the source image info if not explicitly provided in arguments.

        Returns:
            None: results are written directly to the file-like object.
        """
        if 'quality' in kwargs:
            q = kwargs.pop('quality')
            if 'auto_cq' not in kwargs:
                kwargs['auto_cq'] = True
            if 'target_quality' not in kwargs:
                kwargs['target_quality'] = q

        if 'exif' not in kwargs and 'exif' in im.info:
            kwargs['exif'] = im.info['exif']
            if hasattr(kwargs['exif'], 'tobytes'):
                kwargs['exif'] = kwargs['exif'].tobytes()

        valid_keys = {'cq', 'auto_cq', 'target_quality', 'alpha_cq', 'preset', 'depth', 'chroma', 'matrix', 'exif', 'device'}
        encode_kwargs = {k: v for k, v in kwargs.items() if k in valid_keys}

        avif_data = encode_file(im, **encode_kwargs)
        fp.write(avif_data)

    Image.register_save('AVIF', _save_avif)
    Image.register_extension('AVIF', '.avif')
    Image.register_mime('AVIF', 'image/avif')


def encode_file(
        input_source: InputType,
        cq: int = 20,
        auto_cq: bool = False,
        target_quality: float = 80.0,
        alpha_cq: int | None = None,
        preset: NvencPreset = NvencPreset.P7_MAX_QUALITY,
        depth: ColorDepth = ColorDepth.EIGHT_BIT,
        chroma=Chroma.YUV420,
        matrix: ColorMatrix = ColorMatrix.BT709,
        exif: bytes | None = None,
        device: Device = Device.AUTO,
        with_cq: bool = False
) -> bytes | tuple[bytes, int]:
    """
    Encodes image data from various sources into AVIF format
    using hardware acceleration if NVENC available, or multy-threading CPU-encoder.

    Accepts an image as a file path, bytes, a PIL.Image object,
    or a numpy.ndarray and converts it into an AVIF-formatted byte string,
    supports direct GPU tensor migration for PyTorch and CuPy.
    Handles automatic color space transformation, EXIF orientation correction, and bit-depth
    normalization.

    Args:
        input_source (InputType): Image source; supports paths (str, Path),
            bytes (bytes, io.BytesIO), PIL.Image objects, and numpy.ndarrays (H, W, C),
            or GPU tensors (PyTorch/CuPy).
        cq (int, optional): Quality level (quantization parameter) from 0 (best)
            to 51 (worst). Defaults to 20, clamp: max(0, min(51, cq))
        target_quality (float, optional): quality perception target. values > 1.0
            represent a 0–100 scale; values <= 1.0 are treated as raw SSIM. default 80.0.
        alpha_cq (int | None, optional): Quality level for the alpha channel. If not
            specified (None), it is automatically calculated based on `cq`.
            defaults to None.
        auto_cq (bool, optional): enables automatic selection of cq to achieve
            the target SSIM. default is false.
        preset (NvencPreset, optional): NVENC encoding preset affecting the
            speed-to-quality ratio. Defaults to NvencPreset.P7_MAX_QUALITY.
        depth (ColorDepth, optional): Encoding color bit depth.
            defaults to ColorDepth.EIGHT_BIT.
        chroma (Chroma, optional): Chroma subsampling scheme.
            defaults to Chroma.YUV420.
        matrix (ColorMatrix, optional): color matrix coefficients for YUV conversion
           defaults to ColorMatrix.BT709.
        exif (bytes | None, optional): raw EXIF metadata to embed in the output image.
        device (str, optional): target processing device ("auto", "gpu", "cpu").
            default "auto".
        with_cq (bool, optional): when True, returns a ``(bytes, cq)`` tuple where
            ``cq`` is the effective color-plane CQ actually used (the value chosen
            by auto-CQ calibration when ``auto_cq`` is enabled). default False.
    Note:
        If NVENC is unavailable, the encoder transparently switches to a multithreaded CPU implementation.

        The NVENC hardware encoder requires the image width and height to be even.
        Odd-dimensioned images will be automatically cropped by 1 pixel on the
        corresponding side.

        The `target_quality` parameter acts as a universal quality perception scale:

        - if <= 1.0 is passed (for example 0.985) — it is considered raw SSIM
        - if > 1.0 is passed (for example 80) — the value is converted into SSIM via parabolic attenuation

        Recommended ranges for `cq`:

        - 11–18: Visually indistinguishable quality.
        - 20–30: High efficiency (optimal for web).

    Returns:
        bytes: The encoded AVIF image as a byte string. When ``with_cq`` is True,
            a tuple ``(bytes, cq)`` with the effective color-plane CQ instead.

    Raises:
        TypeError: If the `input_source` type is not supported.
        ValueError: If the input numpy array is not three-dimensional (H, W, C).
    """
    # Seamless support for PyTorch and CuPy (auto-migration from GPU to CPU)
    if hasattr(input_source, '__cuda_array_interface__') or (hasattr(input_source, 'device') and hasattr(input_source, 'cpu')):
        if hasattr(input_source, 'detach'):  # PyTorch Tensor
            input_source = input_source.detach().cpu().numpy()
        elif hasattr(input_source, 'get'):  # CuPy Array
            input_source = input_source.get()
        else:  # Generic __cuda_array_interface__
            # Use cupy as a bridge if available
            try:
                import cupy as cp
                input_source = cp.asnumpy(cp.asarray(input_source))
            except ImportError:
                pass

    # Convert any input to a numpy array
    if isinstance(input_source, np.ndarray):
        img_array = input_source
    else:
        is_opened_here = False
        if isinstance(input_source, Image.Image):
            img = input_source
        elif isinstance(input_source, (str, Path, os.PathLike, bytes, io.BytesIO)):
            if isinstance(input_source, bytes):
                input_source = io.BytesIO(input_source)
            img = Image.open(input_source)
            is_opened_here = True
        else:
            raise TypeError(f'Unsupported input type: {type(input_source)}')

        try:
            img = _ensure_srgb(img)
            img = ImageOps.exif_transpose(img)

            if img.mode == 'P':
                img = img.convert('RGBA' if 'transparency' in img.info else 'RGB')
            elif img.mode in ('L', 'I', 'F'):
                img = img.convert('RGB')
            elif img.mode == 'LA':
                img = img.convert('RGBA')
            elif img.mode not in ('RGB', 'RGBA'):
                img = img.convert('RGBA' if getattr(img, 'has_alpha', False) else 'RGB')

            img_array = np.array(img)
        finally:
            if is_opened_here and hasattr(img, 'close'):
                img.close()

    # Check dimensions (H, W, C)
    if len(img_array.shape) != 3:
        raise ValueError('Image must be a three-dimensional array (H, W, C)')

    if img_array.shape[2] == 4:
        alpha = img_array[:, :, 3]
        if np.issubdtype(img_array.dtype, np.integer):
            opaque_alpha = np.iinfo(img_array.dtype).max
        elif np.issubdtype(img_array.dtype, np.floating):
            opaque_alpha = 1.0
        else:
            opaque_alpha = 1

        # Constant opaque alpha needs no AVIF alpha item. Keeping it would
        # route an unnecessary monochrome stream through the alpha encoder.
        if np.all(alpha == opaque_alpha):
            img_array = img_array[:, :, :3]

    h, w, c = img_array.shape

    # Hardware constraint: width and height must be even. If not, crop by 1 pixel.
    if h % 2 != 0 or w % 2 != 0:
        new_h = h - (h % 2)
        new_w = w - (w % 2)
        img_array = img_array[:new_h, :new_w, :]
        h, w = new_h, new_w

    if img_array.dtype == np.uint8:
        dtype_str = DataType.U8
    elif img_array.dtype == np.uint16:
        dtype_str = DataType.U16
    elif np.issubdtype(img_array.dtype, np.floating):
        # Enforce 32-bit Float for NN tensors
        if img_array.dtype != np.float32:
            img_array = img_array.astype(np.float32)
        dtype_str = DataType.F32
    else:
        # Fallback for int32 / bool
        img_array = img_array.astype(np.uint8)
        dtype_str = DataType.U8

    # Quality Perception Scale
    if target_quality <= 1.0:
        target_ssim = float(target_quality)
    else:
        clamped_q = max(0.0, min(100.0, float(target_quality)))
        target_ssim = 1.0 - 0.5 * ((100.0 - clamped_q) / 100.0) ** 2

    # Call Rust function; the binding returns (bitstream, effective color CQ)
    data, chosen_cq = encode_avif(
        pixels=img_array.tobytes(),
        width=w,
        height=h,
        input_dtype=dtype_str,
        cq=max(0, min(51, cq)),
        auto_cq=auto_cq,
        target_ssim=target_ssim,
        alpha_cq=max(0, min(51, alpha_cq)) if alpha_cq is not None else alpha_cq,
        preset=preset,
        depth=depth,
        chroma=chroma,
        matrix=matrix,
        exif=exif,
        device=device
    )
    if with_cq:
        return data, chosen_cq
    return data


def decode_file(path: str | Path, threads: int = 0) -> np.ndarray:
    """
    Decodes an AVIF file into a numpy.ndarray using CPU-optimized dav1d.

    Ideal for high-performance ML pipelines (PyTorch/TensorFlow).

    Args:
        path (str | Path): Path to the .avif image.
        threads (int, optional): Number of threads for decoding.
            A value of 0 corresponds to automatic detection (all available cores).
            For batch processing (e.g., in a dataloader), a value of 1 or 2 is recommended
            to prevent CPU thrashing. Defaults to 0.

    Returns:
        np.ndarray: Decoded image as a numpy.ndarray with shape
            (height, width, channels) and uint8 data type.

    Raises:
        FileNotFoundError: If the file at the specified path is not found.
    """
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f'File "{path}" not found!')

    res = _decode_avif_rs(path.as_posix(), threads)

    # Direct memory interpretation
    arr = np.frombuffer(res["data"], dtype=np.uint8)
    return arr.reshape((res["height"], res["width"], res["channels"]))


_register_pillow_plugin()

__all__ = ['encode_file', 'decode_file', 'is_supported', 'ColorDepth', 'Chroma', 'ColorMatrix', 'NvencPreset', 'DataType', 'Device']
