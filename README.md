# nvavif_py

**Hardware-accelerated AVIF encoding/decoding for Python, powered by NVIDIA NVENC — with automatic CPU fallback.**

`nvavif_py` leverages the AV1 hardware encoder (NVENC) available on modern NVIDIA GPUs to convert images into the AVIF (AV1 Image File Format) at exceptional speed.

Built as a native Rust extension via [PyO3](https://pyo3.rs/), it bridges the NVENC AV1 encoder with the `avif-serialize` crate to produce standards-compliant AVIF files, uses `rav1e` as a built-in CPU software encoder fallback, `dav1d` as fastest cpu-decoder and `rayon` for parallelized pixel preprocessing — all from a simple Python API.


## Key Features

*   **Hardware Accelerated:** Uses `av1_nvenc` for lightning-fast encoding.
*   **Automatic CPU Fallback:** When NVENC is unavailable, transparently switches to the built-in multithreaded `rav1e` software encoder — no extra packages, no code changes needed.
*   **High Bit Depth:** Support for 8-bit and 10-bit color depths.
*   **Chroma Flexibility:** Support for YUV420 (standard) and YUV444 (high fidelity/text).
*   **Color Matrix Control:** BT.601, BT.709, and BT.2020 matrix selection with proper CICP metadata in the AVIF container.
*   **Alpha Support:** Correctly encodes transparency via a secondary AV1 auxiliary plane with independent quality control.
*   **Auto-CQ (SSIM-guided quality):** Automatically finds the optimal quantization level to hit a target SSIM perceptual quality score, using a two-probe secant approximation.
*   **Device Selection:** Explicit `device="gpu"`, `device="cpu"`, or `device="auto"` routing — pin encoding to CPU even when a GPU is present.
*   **Multi-format Input:** Accepts `uint8`, `uint16`, and `float32` NumPy arrays, including HDR data. Float input with values above 1.0 is tone-mapped via the ACES Filmic operator; `[0, 1]` float input (e.g. `img / 255.0`) passes through unchanged.
*   **GPU Tensor Support:** Direct ingestion of PyTorch and CuPy GPU tensors — automatically migrated to CPU without user intervention.
*   **EXIF Embedding:** Raw EXIF metadata can be injected directly into the AVIF container.
*   **Pillow Plugin:** Auto-registers as a Pillow save handler — use `img.save("out.avif")` directly.
*   **Smart Preprocessing:**
    *   Parallel YUV conversion via Rust/Rayon.
    *   Automatic ICC profile → sRGB conversion.
    *   Automatic EXIF orientation handling.
    *   Automatic handling of NVENC's "even-dimension" requirement (auto-crops odd pixels).
    *   Graceful handling of all Pillow image modes (P, L, LA, I, F, RGBA, etc.).
    *   Automatic dtype normalization for NumPy arrays (`float16`, `float64`, `int32`, `bool` → `uint8`/`float32`).
*   **GIL-Free GPU Encoding:** Releases Python's GIL during the GPU encoding phase, enabling **true multithreaded parallelism** with `ThreadPoolExecutor`.
*   **Flexible Input:** Accepts PIL Images, NumPy arrays, file paths, and raw bytes.
*   **Numpy & PIL Integration:** Works out-of-the-box with `numpy` arrays and `Pillow` images.
*   **Zero Configuration:** Total "plug-and-play" experience.
*   **Ultra-Fast Decoding:** CPU-optimized AVIF GIL-free decoder powered by `dav1d` with custom fixed-point YUV→RGB conversion.
    *   **3–4× faster** than standard Pillow/pillow-avif-plugin.
    *   **Direct-to-NumPy:** Zero-copy architecture — decoded pixels go straight into NumPy arrays.
    *   **ML-Ready:** Optimized for PyTorch/TensorFlow DataLoaders with configurable threading.
    *   **Example:** 5184×3456px AVIF → NumPy array in **~80ms** (disk read + decode).

---

## Why Hardware Encoding? Why Fast Decoding?

Software AV1 encoders (libaom, rav1e, SVT-AV1) produce excellent results but are **CPU-intensive and slow**, especially at high quality settings. NVIDIA's NVENC offloads the entire encoding pipeline to dedicated silicon on the GPU. `nvavif_py` includes both paths — GPU and CPU — selectable at runtime:

| Metric           | Software (CPU)              | NVENC (GPU)                 |
|------------------|-----------------------------|-----------------------------|
| Encoding time    | Dozens of seconds per image | **Milliseconds per image**  |
| Encoding speed   | 1x                          | **200x**                    |
| CPU load         | 100% across cores           | **Near zero**               |
| Throughput       | 1-4 images/min              | **Thousands of images/min** |
| Power efficiency | High wattage                | **Minimal additional draw** |

The built-in CPU path (`rav1e`) is **also multi-threaded** and works on any machine — no GPU required. The GPU path is preferred automatically when NVENC is available.

This makes `nvavif_py` ideal for workloads where **throughput matters more than squeezing out the last byte of compression**, while remaining fully functional everywhere.

### Decoding Performance

| Metric            | Standard Pillow          | **nvavif_py.decode_file()** |
|-------------------|--------------------------|-----------------------------|
| Decode time       | 320ms (5184×3456px AVIF) | **~80ms**                   |
| Decode speed      | 1×                       | **4×**                      |
| Memory copies     | 3+ (codec → PIL → NumPy) | **1 (codec → NumPy)**       |
| YUV→RGB math      | Float (`swscale`)        | **Fixed-point (bit-shift)** |
| CPU vectorization | Partial                  | **Full (AVX2/NEON)**        |
| ML/AI ready       | No (slow)                | **Yes (optimized)**         |

---

## Hardware Requirements

| Requirement | Details                                                                     |
|-------------|-----------------------------------------------------------------------------|
| **GPU**     | NVIDIA **Ada Lovelace** (RTX 40X0) or **Blackwell** (RTX 50X0) and newer    |
| **Driver**  | NVIDIA driver with NVENC AV1 support (≥ 570.0 on Windows, ≥ 570.0 on Linux) |
| **OS**      | Linux (x86_64) or Windows (x64)                                             |

> **Note:**
>
> Older NVIDIA architectures (Turing, Ampere) support NVENC for H.264/HEVC but **do not** support AV1 encoding. The `is_supported()` function lets you check at runtime.
>
> **No compatible GPU? No problem.** `nvavif_py` automatically falls back to its built-in `rav1e` CPU encoder — zero configuration, zero extra dependencies.

---

## Decoding Performance

While `nvavif_py` is famous for GPU-accelerated **encoding**, it also includes the **fastest AVIF decoder available in the Python ecosystem**.

### Why CPU Decoding?

For **single-frame decoding** (the common case for images), CPU-based `dav1d` is **faster than GPU** (`NVDEC`) because:
- **No session initialization overhead** — GPU decoders expect video streams and have setup costs.
- **Lower latency** — Direct memory access without PCIe transfers.
- **Better CPU utilization** — Modern CPUs handle single-image decoding in microseconds.

### Decoder Architecture

| Component           | Implementation                                                                |
|---------------------|-------------------------------------------------------------------------------|
| **Demuxer**         | `libavformat` — minimal probing, zero-overhead container parsing.             |
| **AV1 Decoder**     | `dav1d` — the world's fastest AV1 decoder, written in hand-tuned C.           |
| **YUV→RGB**         | **Custom Rust converter** with fixed-point math (`i32` bit-shifts, no `f32`). |
| **Parallelization** | `rayon` — parallel row processing across all CPU cores.                       |
| **Output**          | Direct `numpy.ndarray` (zero-copy via `frombuffer`).                          |

### Performance Comparison

Decoding **5184×3456px AVIF** to `numpy.ndarray`:

| Library                     | Time      | Notes                                    |
|-----------------------------|-----------|------------------------------------------|
| **nvavif_py**               | **~80ms** | Includes disk I/O + decode + YUV→RGB     |
| Pillow (pillow-avif-plugin) | ~360ms    | Uses libaom (slow) + extra memory copies |
| OpenCV (`cv2.imread`)       | ~280ms    | Uses libavcodec + swscale (float math)   |
| imageio + av                | ~400ms    | Python overhead + PyAV wrapper           |

> **Speedup:** nvavif_py is **3–4× faster** than standard tools.

### Why It's Fast

1. **Fixed-Point Math:** YUV→RGB conversion uses integer arithmetic with bit-shifts (`>> 10`) instead of floating-point multiplications. This enables CPU vectorization (AVX2/NEON).

2. **Parallel Decoding:** `dav1d` uses SIMD and multi-threading internally. The `threads` parameter lets you control CPU core usage (set to `1` for DataLoader workers to avoid thrashing).

3. **Zero-Copy Pipeline:** Decoded YUV planes → Rust converter → NumPy buffer. No intermediate PIL Image or Python lists.

4. **Minimal Probing:** `libavformat` demuxer is configured with `probesize=4096` and `analyzeduration=0` to skip unnecessary format detection.

### Ideal for ML/AI Training Pipelines

When training neural networks, the DataLoader is often a bottleneck. AVIF offers **30–70% smaller datasets** than PNG/JPEG, but only if decoding is fast enough to saturate GPU training.

**nvavif_py's decoder is purpose-built for this:**

```python
from torch.utils.data import Dataset
import nvavif_py
import torch

class AVIFDataset(Dataset):
    def __init__(self, image_paths):
        self.paths = image_paths
    
    def __getitem__(self, idx):
        # Decode AVIF in 1–2 threads (avoid CPU contention with other workers)
        img = nvavif_py.decode_file(self.paths[idx], threads=2)
        return torch.from_numpy(img).permute(2, 0, 1).float() / 255.0
    
    def __len__(self):
        return len(self.paths)
```

**Result:** You can use AVIF datasets **without sacrificing training speed** — in many cases, decoding is **faster than PNG** due to smaller file sizes (less disk I/O).

---

## Installation

```bash
uv add nvavif_py
```

or

```bash
pip install nvavif_py
```

Pre-built wheels bundle all required libraries (NVENC, dav1d, rav1e) — no system-level installation needed.

---

## Quick Start

```python
import nvavif_py

# GPU is preferred automatically; CPU rav1e is used if NVENC is unavailable
print("GPU available:", nvavif_py.is_supported())

# Encode from a file path
avif_bytes = nvavif_py.encode_file("photo.png")

# Save the result
with open("photo.avif", "wb") as f:
    f.write(avif_bytes)
```

### Pillow Plugin (Zero-Config, works with CPU fallback too)

`nvavif_py` always registers itself as a Pillow **save** plugin regardless of GPU availability. No extra imports or setup required:

```python
from PIL import Image

img = Image.open("photo.png")

# Just save as .avif — nvavif_py handles the rest
img.save("photo.avif")

# With Pillow-style quality (0–100 scale, enables auto_cq automatically)
img.save("photo.avif", quality=80)

# With nvavif_py-native parameters
from nvavif_py import Chroma, ColorDepth, Device
img.save("photo.avif", cq=18, chroma=Chroma.YUV444, depth=ColorDepth.TEN_BIT)

# Force CPU encoding (skip NVENC even if available)
img.save("photo.avif", cq=20, device=Device.CPU)
```

> The plugin maps Pillow's `quality` parameter (0–100) to `nvavif_py`'s `target_ssim` scale and enables `auto_cq=True` automatically for perceptual quality targeting.
>
> **Save only:** the plugin does not register an AVIF *reader*. `Image.open("*.avif")` requires pillow-avif-plugin (or Pillow built with libavif); this library reads AVIF via `decode_file()`, which returns a NumPy array (`Image.fromarray` to get a PIL image back).

---

## API Reference

### `is_supported() → bool`

Checks whether the current system has a compatible GPU and driver for AV1 hardware encoding.

```python
if nvavif_py.is_supported():
    print("NVENC AV1 is ready")
else:
    print("NVENC unavailable — using built-in rav1e CPU encoder")
```

---

### `encode_file(input_source, ...) → bytes`

Encodes an image from a variety of sources into AVIF format.
Automatically selects the GPU (NVENC) or CPU (rav1e) encoder based on availability and the `device` parameter.

```python
nvavif_py.encode_file(
    input_source,            # See "Accepted Input Types" below
    cq=20,                   # Color quality: 0 (best) – 51 (smallest)
    auto_cq=False,           # Enable SSIM-guided automatic CQ selection
    target_quality=80.0,     # Target quality (0–100 scale or raw SSIM ≤ 1.0)
    alpha_cq=None,           # Alpha quality (auto-calculated if None)
    preset=NvencPreset.P7_MAX_QUALITY,  # quality tradeoff / gpu load
    depth=ColorDepth.EIGHT_BIT,         # 8-bit or 10-bit
    chroma=Chroma.YUV420,               # Chroma subsampling
    matrix=ColorMatrix.BT709,           # Color matrix for YUV conversion
    exif=None,                           # Raw EXIF bytes to embed in AVIF
    device=Device.AUTO,                  # "auto", "gpu", or "cpu"
    with_cq=False,                       # True → returns (bytes, effective_cq) tuple
)
```

#### Accepted Input Types

| Type                           | Description                                          |
|--------------------------------|------------------------------------------------------|
| `str` / `Path` / `os.PathLike` | File path to an image on disk                        |
| `bytes`                        | Raw image file bytes (PNG, JPEG, etc.)               |
| `io.BytesIO`                   | In-memory binary stream                              |
| `PIL.Image.Image`              | Pillow image object (any mode — auto-converted)      |
| `numpy.ndarray`                | NumPy array of shape `(H, W, C)` (any numeric dtype) |
| PyTorch `Tensor` (GPU/CPU)     | Auto-detached and moved to CPU via `.detach().cpu()` |
| CuPy `ndarray`                 | Auto-converted via `.get()`                          |

> **Caveats:** animated inputs (GIF, animated WebP) are flattened to their first frame — the library encodes still images only. Files above Pillow's ~178 MP "decompression bomb" guard raise `DecompressionBombError` when loaded from a path; the batch tool disables that guard for legitimate oversized photography.

#### Parameters

| Parameter        | Type             | Default          | Description                                                                                            |
|------------------|------------------|------------------|--------------------------------------------------------------------------------------------------------|
| `cq`             | `int`            | `20`             | Constant quality level. Lower = higher quality, larger file. Clamped 0–51.                             |
| `auto_cq`        | `bool`           | `False`          | When `True`, ignores `cq` and automatically selects the quantizer to hit `target_quality`.             |
| `target_quality` | `float`          | `80.0`           | Quality target. Values > 1.0 use a 0–100 scale; values ≤ 1.0 are treated as raw SSIM (e.g. `0.985`).   |

> The 0–100 scale maps to SSIM as `ssim = 1 − 0.5 · ((100 − q) / 100)²` (so 80 → 0.98, 85 → 0.98875, 90 → 0.995); the two-probe secant search then finds the cq that hits it. Values ≤ 1.0 bypass the mapping and are used as the SSIM target directly.
| `alpha_cq`       | `int \| None`    | `None`           | Quality for alpha channel. If `None`, defaults to `cq - 4` (slightly better than color plane).         |
| `preset`         | `NvencPreset`    | `P7_MAX_QUALITY` | NVENC preset (P1–P7). Higher = better compression, slightly slower. Also maps to rav1e speed on CPU.   |
| `depth`          | `ColorDepth`     | `EIGHT_BIT`      | Bit depth per channel: `EIGHT_BIT` or `TEN_BIT`.                                                       |
| `chroma`         | `Chroma`         | `YUV420`         | Chroma subsampling: `YUV420` or `YUV444`.                                                              |
| `matrix`         | `ColorMatrix`    | `BT709`          | YUV color matrix: `BT709` (HD), `BT601` (SD/legacy), or `BT2020` (wide gamut/HDR).                     |
| `exif`           | `bytes \| None`  | `None`           | Raw EXIF metadata bytes to embed in the AVIF container.                                                |
| `device`         | `Device \| str`  | `Device.AUTO`    | `"auto"` = prefer GPU; `"gpu"` = force GPU (raises if unavailable); `"cpu"` = force rav1e CPU encoder. |
| `with_cq`        | `bool`           | `False`          | When `True`, returns a `(bytes, cq)` tuple where `cq` is the effective color-plane CQ (the value chosen by auto-CQ when enabled). |

#### Automatic Preprocessing

When the input is a PIL Image or a file path/bytes, the following preprocessing is applied automatically:

| Step                      | Description                                                                                                 |
|---------------------------|-------------------------------------------------------------------------------------------------------------|
| **ICC → sRGB**            | Images with embedded ICC profiles are converted to sRGB.                                                    |
| **EXIF orientation**      | EXIF rotation/flip tags are applied and the image is normalized.                                            |
| **Mode conversion**       | Palette (P), Grayscale (L/LA), Integer (I), Float (F) → RGB/RGBA.                                           |
| **Even dimensions**       | Odd width/height is auto-cropped by 1 pixel (NVENC hardware requirement).                                   |
| **dtype normalization**   | `uint8` → native; `uint16` → native 16-bit path; `float32` → ACES tone-mapped 32-bit path; other → `uint8`. |
| **GPU tensor migration**  | PyTorch/CuPy GPU tensors are automatically moved to CPU NumPy before encoding.                              |

#### Quality Guidelines (`cq`)

| Range     | Use Case                                        |
|-----------|-------------------------------------------------|
| **0–10**  | Archival / near-lossless. Large files.          |
| **11–18** | Visually indistinguishable from the original.   |
| **20–30** | High efficiency — **optimal for web delivery**. |
| **31–51** | Aggressive compression.                         |

> **Note:** NVENC requires image dimensions to be **even numbers**.
> 
> Odd-dimensioned images are automatically cropped by 1 pixel on the right or bottom edge.
>
> **Hardware limits:** NVENC also enforces a maximum of 8192 pixels per axis
> and a small minimum frame size — measured on RTX 40: **width ≥ 130, height
> ≥ 66** (smaller images transparently take the CPU path). See
> `DEVELOPMENT_NOTES.md` §12 for measurements.

---

### Enumerations

#### `ColorDepth`

```python
from nvavif_py import ColorDepth

ColorDepth.EIGHT_BIT   # Standard 8-bit (sRGB content)
ColorDepth.TEN_BIT     # 10-bit (HDR, wide gamut, banding reduction)
```

#### `Chroma`

```python
from nvavif_py import Chroma

Chroma.YUV420  # 4:2:0 — Best compression. Ideal for photos and video frames.
Chroma.YUV444  # 4:4:4 — Full chroma resolution. Ideal for text, graphics, UI screenshots.
```

#### `ColorMatrix`

```python
from nvavif_py import ColorMatrix

ColorMatrix.BT709   # Rec. 709 — Standard for HD/modern content (recommended default)
ColorMatrix.BT601   # Rec. 601 — Standard for SD/legacy content
ColorMatrix.BT2020  # Rec. 2020 — Wide color gamut, HDR/UHDTV content
```

The selected matrix affects both the RGB → YUV conversion math and the CICP metadata written into the AVIF container (color primaries + matrix coefficients), ensuring decoders interpret colors correctly.

#### `NvencPreset`

```python
from nvavif_py import NvencPreset

NvencPreset.P1_LOW_QUALITY    # Minimal GPU usage, low efficiency
NvencPreset.P2_MEDIUM_LOW
NvencPreset.P3_MEDIUM
NvencPreset.P4_MEDIUM_HIGH
NvencPreset.P5_HIGH
NvencPreset.P6_VERY_HIGH
NvencPreset.P7_MAX_QUALITY    # Best compression (recommended for images)
```

> On the **CPU path** (rav1e), the preset maps to encoder speed: P7 → speed 4, P1 → speed 10. Lower speed = better compression, more CPU time.

#### `Device`

```python
from nvavif_py import Device

Device.AUTO   # Prefer GPU if NVENC is available, otherwise use CPU rav1e (default)
Device.GPU    # Force GPU encoding; raises ValueError if NVENC is not supported
Device.CUDA   # Alias for Device.GPU
Device.CPU    # Force CPU encoding via built-in rav1e, regardless of GPU availability
```

#### `DataType`

```python
from nvavif_py import DataType

DataType.U8   # 'u8'  — unsigned 8-bit integer NumPy arrays
DataType.U16  # 'u16' — unsigned 16-bit integer NumPy arrays (wider dynamic range)
DataType.F32  # 'f32' — 32-bit float NumPy arrays (HDR; ACES tone-mapping applied)
```

> `DataType` is inferred automatically by `encode_file()` from the NumPy array dtype — you only need it when calling the low-level `_nvavif_py.encode_avif()` directly.

---

### `decode_file(path, threads=0) → np.ndarray`

Decodes an AVIF file directly into a NumPy array using the ultra-fast `dav1d` decoder.

```python
import nvavif_py

img_array = nvavif_py.decode_file("photo.avif", threads=0)
# Returns: numpy.ndarray with shape (H, W, C), dtype=uint8
```

#### Parameters

| Parameter | Type          | Default | Description                                                                                                  |
|-----------|---------------|---------|--------------------------------------------------------------------------------------------------------------|
| `path`    | `str \| Path` | —       | Path to the `.avif` file.                                                                                    |
| `threads` | `int`         | `0`     | Number of decoder threads. `0` = auto (all cores). Set `1–2` for DataLoader workers to avoid CPU contention. |

#### Returns

| Type            | Description                                                                                         |
|-----------------|-----------------------------------------------------------------------------------------------------|
| `numpy.ndarray` | Decoded image with shape `(height, width, channels)`, dtype `uint8`. Channels: 3 (RGB) or 4 (RGBA). |

> **Note:** The returned array is a **read-only, zero-copy view**. Call `.copy()` on it if you need to modify pixels in place (some torchvision/OpenCV pipelines mutate input arrays).

#### Threading Recommendations

| Use Case                          | Recommended `threads` | Reason                                                      |
|-----------------------------------|-----------------------|-------------------------------------------------------------|
| **Single-image decoding**         | `0` (auto)            | Use all cores for maximum speed.                            |
| **PyTorch/TF DataLoader workers** | `1` or `2`            | Avoid CPU thrashing when multiple workers run in parallel.  |
| **Batch processing (loop)**       | `0` (auto)            | Each iteration uses full CPU, then releases.                |
| **Batch processing (parallel)**   | `1` or `2`            | Let outer parallelism (ThreadPoolExecutor) manage cores.    |

#### Supported Formats

The decoder handles **all standard AVIF/AV1 output configurations** automatically and converts to 8-bit RGB(A) NumPy arrays:

| Pixel Format    | Chroma | Bit Depth | Alpha | Notes                            |
|-----------------|--------|-----------|-------|----------------------------------|
| YUV420P         | 4:2:0  | 8-bit     | ✅     | Most common (photos, web images) |
| YUV422P         | 4:2:2  | 8-bit     | ✅     | Intermediate quality             |
| YUV444P         | 4:4:4  | 8-bit     | ✅     | Full chroma (graphics, text)     |
| YUV420P10LE     | 4:2:0  | 10-bit    | ❌     | HDR content                      |
| YUV422P10LE     | 4:2:2  | 10-bit    | ❌     | Professional video               |
| YUV444P10LE     | 4:4:4  | 10-bit    | ❌     | High-fidelity graphics           |
| YUVA444P10LE    | 4:4:4  | 10-bit    | ✅     | 10-bit with alpha channel        |

> **Note:** 10-bit output is automatically downscaled to 8-bit (`uint8`) for compatibility with standard image processing libraries.

---

## Usage Examples

### Basic File Conversion

```python
import nvavif_py

avif_data = nvavif_py.encode_file("input.png", cq=22)
with open("output.avif", "wb") as f:
    f.write(avif_data)
```

### High-Quality with Alpha Transparency

```python
avif_data = nvavif_py.encode_file(
    "logo_transparent.png",
    cq=12,
    alpha_cq=8,   # Preserve alpha with higher fidelity
)
```

### 10-bit HDR Encoding

```python
from nvavif_py import ColorDepth

avif_data = nvavif_py.encode_file(
    "hdr_photo.png",
    cq=18,
    depth=ColorDepth.TEN_BIT,
)
```

### YUV444 for Screenshots and Graphics

```python
from nvavif_py import Chroma

avif_data = nvavif_py.encode_file(
    "screenshot.png",
    cq=16,
    chroma=Chroma.YUV444,  # Preserves sharp text and color edges
)
```

### Color Matrix Selection

```python
from nvavif_py import ColorMatrix, ColorDepth

# Modern HD content (default)
avif_data = nvavif_py.encode_file("photo.png", matrix=ColorMatrix.BT709)

# Legacy SD content
avif_data = nvavif_py.encode_file("old_video_frame.png", matrix=ColorMatrix.BT601)

# Wide color gamut / HDR (BT.2020)
avif_data = nvavif_py.encode_file("hdr_photo.png", matrix=ColorMatrix.BT2020, depth=ColorDepth.TEN_BIT)
```

### Auto-CQ: SSIM-Guided Quality

`auto_cq=True` automatically finds the best quantizer to hit a target perceptual quality, without trial-and-error:

```python
import nvavif_py

# Target quality on 0–100 scale (maps to SSIM internally)
avif_data = nvavif_py.encode_file(
    "photo.png",
    auto_cq=True,
    target_quality=85.0,  # 85/100 quality
)

# Or pass raw SSIM directly (value ≤ 1.0)
avif_data = nvavif_py.encode_file(
    "photo.png",
    auto_cq=True,
    target_quality=0.985,  # raw SSIM target
)
```

> **How it works:** Two trial encodings of a 512×512 mosaic patch are performed (one at CQ 28, one at CQ 16 or 44 depending on target direction). A secant approximation estimates the CQ value that will hit the target SSIM. The final CQ is clamped to 0–51. A safeguard prevents runaway bitrate on noisy sources (e.g., heavy JPEG artifacts).

### Device Selection

```python
from nvavif_py import Device

# Default: GPU if available, CPU otherwise
avif_data = nvavif_py.encode_file("photo.png", device=Device.AUTO)

# Force GPU — raises ValueError if NVENC is not supported
avif_data = nvavif_py.encode_file("photo.png", device=Device.GPU)

# Force CPU (rav1e) — useful for benchmarking or CI environments
avif_data = nvavif_py.encode_file("photo.png", device=Device.CPU)
```

### HDR Float32 Input (ACES Tone-Mapping)

Float32 arrays with values outside the [0, 1] range (e.g. HDR render outputs, EXR data) are automatically tone-mapped using the ACES Filmic operator before encoding:

```python
import numpy as np
import nvavif_py
from nvavif_py import ColorDepth, ColorMatrix

# Simulate HDR data with super-bright highlights (values > 1.0)
hdr_data = np.random.rand(1080, 1920, 3).astype(np.float32) * 5.0

avif_data = nvavif_py.encode_file(
    hdr_data,
    depth=ColorDepth.TEN_BIT,
    matrix=ColorMatrix.BT2020,
    cq=16,
)
```

> **ACES Filmic formula:** `f(x) = (x*(2.51x + 0.03)) / (x*(2.43x + 0.59) + 0.14)`, clamped to [0, 1]. Preserves highlight detail without hard clipping.
>
> Tone mapping **only engages when the float data actually exceeds 1.0** (checked via `max()`). Standard SDR float input such as `img / 255.0` is encoded without any brightness modification.

### PyTorch / CuPy GPU Tensor Input

```python
import torch
import nvavif_py

# Works with GPU tensors directly — no manual .cpu() call needed
tensor = torch.rand(1080, 1920, 3, dtype=torch.float32).cuda()
avif_data = nvavif_py.encode_file(tensor, cq=20)

# Also works with CPU tensors
tensor_cpu = torch.rand(1080, 1920, 3, dtype=torch.uint8)
avif_data = nvavif_py.encode_file(tensor_cpu, cq=20)
```

### EXIF Embedding

```python
from PIL import Image
import nvavif_py

img = Image.open("photo.jpg")

# Extract raw EXIF from source image
exif_bytes = img.info.get("exif", b"")

avif_data = nvavif_py.encode_file(img, cq=20, exif=exif_bytes)
with open("photo_with_exif.avif", "wb") as f:
    f.write(avif_data)
```

> `encode_file()` also extracts EXIF from PIL Images automatically if they carry it in `img.info["exif"]` — no manual extraction needed in most cases.

### Pillow Plugin — Save Directly

```python
from PIL import Image

img = Image.open("photo.jpg")
img = img.resize((1920, 1080))

# Option 1: Pillow-style quality (0–100)
img.save("output.avif", quality=85)

# Option 2: nvavif_py-native parameters
from nvavif_py import Chroma, ColorDepth
img.save("output.avif", cq=18, depth=ColorDepth.TEN_BIT, chroma=Chroma.YUV444)

# Force CPU encoder via Pillow plugin
from nvavif_py import Device
img.save("output.avif", cq=20, device=Device.CPU)
```

### From a NumPy Array

```python
import numpy as np
import nvavif_py
from nvavif_py import ColorDepth

# Synthetic gradient image (H, W, C)
arr = np.zeros((1080, 1920, 3), dtype=np.uint8)
arr[:, :, 0] = np.linspace(0, 255, 1920, dtype=np.uint8)  # Red gradient
avif_data = nvavif_py.encode_file(arr, cq=20)

# uint16 arrays — 16-bit precision path (no downscaling to uint8)
arr_u16 = np.random.randint(0, 65535, (1080, 1920, 3), dtype=np.uint16)
avif_data = nvavif_py.encode_file(arr_u16, cq=20, depth=ColorDepth.TEN_BIT)

# Float arrays — ACES tone-mapping applied automatically
arr_float = np.random.rand(1080, 1920, 3).astype(np.float32) * 2.0
avif_data = nvavif_py.encode_file(arr_float, cq=20)
```

### From In-Memory Bytes

```python
import nvavif_py

# e.g., downloaded from a network request
image_bytes = download_image_from_url("https://example.com/photo.jpg")
avif_data = nvavif_py.encode_file(image_bytes, cq=25)
```

### Batch Conversion (Sequential)

```python
from pathlib import Path
import nvavif_py

input_dir = Path("images/")
output_dir = Path("avif_output/")
output_dir.mkdir(exist_ok=True)

for img_path in input_dir.glob("*.png"):
    avif_data = nvavif_py.encode_file(img_path, cq=22)
    (output_dir / img_path.with_suffix(".avif").name).write_bytes(avif_data)
    print(f"Converted {img_path.name}")
```

### Multi-Threaded Batch Conversion

`nvavif_py` releases Python's GIL during the GPU encoding phase. This means `ThreadPoolExecutor` achieves **true parallelism** — multiple NVENC sessions run simultaneously on the GPU hardware.

RTX 4090/5090 supports up to **8 concurrent NVENC sessions** and has **2 physical AV1 encoder chips**, so multiple threads can saturate the hardware:

```python
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import nvavif_py

input_dir = Path("images/")
output_dir = Path("avif_output/")
output_dir.mkdir(exist_ok=True)

image_paths = list(input_dir.glob("*.png"))

def convert_one(img_path: Path) -> str:
    avif_data = nvavif_py.encode_file(img_path, cq=22)
    out_path = output_dir / img_path.with_suffix(".avif").name
    out_path.write_bytes(avif_data)
    return img_path.name

# Entry-level and mid-range RTX GPUs have a single encoder chip and support up to 4 parallel NVENC sessions.
# High-end RTX GPUs have up to 8 parallel NVENC sessions:
with ThreadPoolExecutor(max_workers=8) as pool:
    futures = {pool.submit(convert_one, p): p for p in image_paths}
    for future in as_completed(futures):
        print(f"Converted {future.result()}")
```

> **How it works:** Each thread calls `encode_file()`, which prepares YUV data on the CPU (with GIL held), then releases the GIL and submits the frame to an independent NVENC session on the GPU. While the GPU is encoding, other Python threads are free to prepare and submit their own frames.

### Web Server Integration (FastAPI)

```python
from fastapi import FastAPI, UploadFile
from fastapi.responses import Response
import nvavif_py

app = FastAPI()

@app.post("/convert")
async def convert_to_avif(file: UploadFile):
    image_bytes = await file.read()
    avif_data = nvavif_py.encode_file(image_bytes, cq=24)
    return Response(content=avif_data, media_type="image/avif")
```

---

### Decoding AVIF Files

#### Basic Decoding

```python
import nvavif_py
import numpy as np

# Decode AVIF to NumPy array (auto-threading)
img = nvavif_py.decode_file("photo.avif")
print(img.shape, img.dtype)  # (1080, 1920, 3) uint8

# Display with matplotlib
import matplotlib.pyplot as plt
plt.imshow(img)
plt.show()
```

#### Save to PNG

```python
from PIL import Image
import nvavif_py

img = nvavif_py.decode_file("photo.avif")
Image.fromarray(img).save("photo.png")
```

#### Batch Decoding (Sequential)

```python
from pathlib import Path
import nvavif_py

avif_dir = Path("dataset/")
for avif_path in avif_dir.glob("*.avif"):
    img = nvavif_py.decode_file(avif_path)
    print(f"Loaded {avif_path.name}: {img.shape}")
```

#### Batch Decoding (Parallel)

```python
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import nvavif_py

avif_paths = list(Path("dataset/").glob("*.avif"))

def decode_one(path):
    # Use 1-2 threads per worker to avoid CPU contention
    return nvavif_py.decode_file(path, threads=2)

with ThreadPoolExecutor(max_workers=8) as pool:
    images = list(pool.map(decode_one, avif_paths))

print(f"Decoded {len(images)} images")
```

#### PyTorch DataLoader Integration

```python
from torch.utils.data import Dataset, DataLoader
import nvavif_py
import torch

class AVIFImageDataset(Dataset):
    def __init__(self, image_paths, transform=None):
        self.paths = image_paths
        self.transform = transform
    
    def __len__(self):
        return len(self.paths)
    
    def __getitem__(self, idx):
        # Decode with 1-2 threads (DataLoader uses multiple workers)
        img = nvavif_py.decode_file(self.paths[idx], threads=1)
        
        # Convert to PyTorch tensor (HWC → CHW)
        img = torch.from_numpy(img).permute(2, 0, 1).float() / 255.0
        
        if self.transform:
            img = self.transform(img)
        
        return img

# Usage
from pathlib import Path
paths = list(Path("dataset/").glob("*.avif"))
dataset = AVIFImageDataset(paths)
loader = DataLoader(dataset, batch_size=32, num_workers=4, shuffle=True)

for batch in loader:
    print(batch.shape)  # torch.Size([32, 3, H, W])
    break
```

#### TensorFlow/Keras Data Pipeline

```python
import tensorflow as tf
import nvavif_py
import numpy as np

def load_avif(path):
    path_str = path.numpy().decode('utf-8')
    img = nvavif_py.decode_file(path_str, threads=2)
    return img.astype(np.float32) / 255.0

def tf_load_avif(path):
    img = tf.py_function(load_avif, [path], tf.float32)
    img.set_shape([None, None, 3])  # (H, W, 3)
    return img

# Create dataset
file_paths = tf.data.Dataset.list_files("dataset/*.avif")
dataset = file_paths.map(tf_load_avif, num_parallel_calls=tf.data.AUTOTUNE)
dataset = dataset.batch(32).prefetch(tf.data.AUTOTUNE)

for batch in dataset.take(1):
    print(batch.shape)  # (32, H, W, 3)
```

---

## CPU Encoding (Built-In Fallback)

`nvavif_py` includes a **built-in multi-threaded CPU encoder** based on `rav1e`. It activates automatically when:

- No compatible NVIDIA GPU is detected (`device="auto"`)
- You explicitly request it (`device="cpu"` / `Device.CPU`)
- GPU encoding is forced but NVENC initialization fails

No additional packages are required. The CPU path is **always available** on all supported platforms.

### GPU vs Built-In CPU Comparison

| Feature                | NVENC (GPU)                 | rav1e (Built-In CPU)         |
|------------------------|-----------------------------|------------------------------|
| Encoder                | NVIDIA NVENC hardware       | rav1e software (Rust)        |
| Speed                  | ⚡ Milliseconds per image    | ~1–30 seconds per image      |
| Hardware required      | NVIDIA RTX 40xx/50xx        | **Any CPU**                  |
| CPU load during encode | Near zero                   | 100% across all cores        |
| Compression efficiency | Good                        | **Excellent (rav1e)**        |
| Best for               | Throughput, real-time       | Quality, portability, CI     |
| Threading control      | Up to 8 concurrent sessions | `preset` maps to rav1e speed |

```python
import nvavif_py
from nvavif_py import Device

# These are equivalent on a machine without NVENC:
nvavif_py.encode_file("photo.png", cq=20)                      # auto-detects, uses CPU
nvavif_py.encode_file("photo.png", cq=20, device=Device.CPU)   # explicit CPU
```

---

## Use Cases

### 🧠 ML/AI Training Pipelines
Compress dataset images or model output visualizations on-the-fly using GPU resources that would otherwise sit idle during data preprocessing.
Encode with GPU, decode with `dav1d` — both operations leave the GIL free for DataLoader parallelism.

Use AVIF datasets to reduce storage and disk I/O by 30–70% **without sacrificing training speed**. `nvavif_py.decode_file()` is optimized for DataLoader workflows — configure `threads=1` per worker to avoid CPU contention, and enjoy faster-than-PNG decoding due to smaller file sizes. Perfect for ImageNet-scale datasets, satellite imagery, medical imaging, and generative AI training data.

**Why AVIF for training?**
- **Smaller datasets** → Faster download, less cloud storage cost.
- **Faster disk I/O** → Smaller files = less time reading from SSD/NVMe.
- **No quality loss** → Visually lossless at CQ 12–18.
- **Decode speed** → nvavif_py is 3–4× faster than Pillow, matching or exceeding PNG decode times.
- **CPU-only environments** → The built-in rav1e encoder covers dataset preparation even without a GPU.

### 🗄️ Storage Optimization
Reduce cloud storage costs by converting image libraries from legacy formats to AVIF, achieving 30–70% storage savings.

### 🔥 Massive Image Processing
If you are converting millions of images for a web CDN, the GPU can process them in a fraction of the time required by a CPU cluster. With multi-threaded encoding, saturate all available NVENC sessions for maximum throughput.

### 🌐 Web Asset Pipelines
Integrate into your static site generator or CDN origin to serve AVIF images at a fraction of the size of JPEG/PNG, improving page load times and Core Web Vitals.

### 🖼️ Image Processing Services
Build high-throughput image conversion microservices that handle thousands of uploads per minute without saturating CPU resources.

### 📸 Photography Workflows
Batch-convert RAW/TIFF exports to AVIF for archival or web galleries, preserving quality with 10-bit depth and YUV444 chroma.

---

## License

This project is licensed under the **MIT License**.

Under the hood, this library utilizes for hardware interaction `ffmpeg-next` (WTFPL), FFmpeg NVENC headers (LGPL/GPL), `dav1d` (BSD 2-Clause "Simplified"), and `rav1e` (BSD 2-Clause) to deliver a fully bundled processing pipeline.