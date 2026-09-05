import time
import traceback
from pathlib import Path

import numpy as np
from PIL import Image

import nvavif_py
from nvavif_py import Chroma, ColorDepth, Device, NvencPreset

ROOT = Path(__file__).resolve().parent.parent
TEST_IMG = ROOT / "test_imgs"
OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

def check_avif(data: bytes, label: str):
    ok_magic = data[:4] == b"\x00\x00\x00\x18" and data[4:12] == b"ftypavif"
    print(f"  [{label}] bytes={len(data):,} magic_ok={ok_magic}")
    return ok_magic

def roundtrip(src, label, **kwargs):
    try:
        t0 = time.perf_counter()
        data = nvavif_py.encode_file(src, **kwargs)
        enc_t = time.perf_counter() - t0
        check_avif(data, f"{label} encode {enc_t*1000:.0f}ms")

        out = OUT / (label.replace("/", "_") + ".avif")
        out.write_bytes(data)

        t0 = time.perf_counter()
        arr = nvavif_py.decode_file(out)
        dec_t = time.perf_counter() - t0
        print(f"  [{label}] decode {dec_t*1000:.0f}ms shape={arr.shape} dtype={arr.dtype}")

        pil = Image.open(out)
        pil.load()
        print(f"  [{label}] PIL open OK {pil.size} {pil.mode}")
        return data
    except Exception:
        print(f"  [{label}] FAILED")
        traceback.print_exc()
        return None

print("=== is_supported:", nvavif_py.is_supported(), "===")

print("\n-- JPG (default GPU) --")
roundtrip(TEST_IMG / "007jxjxk.jpg", "jpg_default")

print("\n-- PNG RGBA --")
png_with_alpha = None
for f in TEST_IMG.iterdir():
    if f.suffix.lower() == ".png":
        im = Image.open(f)
        if im.mode == "RGBA":
            png_with_alpha = f
            break
print("  alpha png found:", png_with_alpha)
if png_with_alpha:
    roundtrip(png_with_alpha, "png_rgba")

print("\n-- CPU (rav1e) encode/decode (small img) --")
small = np.random.default_rng(0).integers(0, 256, (256, 256, 3), dtype=np.uint8)
roundtrip(small, "cpu_small", device=Device.CPU)

print("\n-- chroma/depth matrix --")
for label, kw in [
    ("8bit_420", dict()),
    ("8bit_444", dict(chroma=Chroma.YUV444)),
    ("10bit_420", dict(depth=ColorDepth.TEN_BIT)),
    ("10bit_444", dict(depth=ColorDepth.TEN_BIT, chroma=Chroma.YUV444)),
]:
    roundtrip(small, label, cq=20, **kw)

print("\n-- auto_cq --")
roundtrip(TEST_IMG / "007jxjxk.jpg", "autocq", auto_cq=True, target_quality=90.0)

print("\n-- numpy u8 / f32 / RGBA --")
arr = np.random.default_rng(42).integers(0, 256, (512, 768, 3), dtype=np.uint8)
roundtrip(arr, "numpy_u8")
hdr = (np.random.default_rng(7).random((512, 768, 3)).astype(np.float32) * 4.0)
roundtrip(hdr, "numpy_f32", depth=ColorDepth.TEN_BIT)
rgba = np.random.default_rng(3).integers(0, 256, (300, 400, 4), dtype=np.uint8)
roundtrip(rgba, "numpy_rgba")

print("\n-- Pillow plugin --")
try:
    img = Image.open(TEST_IMG / "007jxjxk.jpg").resize((640, 400))
    out = OUT / "pillow_plugin.avif"
    img.save(out, quality=85)
    arr = nvavif_py.decode_file(out)
    print(f"  [pillow_plugin] OK {arr.shape}")
except Exception:
    traceback.print_exc()

print("\n-- odd dimensions --")
odd = np.random.default_rng(1).integers(0, 256, (333, 555, 3), dtype=np.uint8)
data = roundtrip(odd, "odd_dims")
if data:
    pil = Image.open(OUT / "odd_dims.avif")
    print("  odd->encoded dims:", pil.size)

print("\n-- timing --")
src = TEST_IMG / "00013.jpg"
for name, kw in [("gpu", {}), ("gpu_p1", {"preset": NvencPreset.P1_LOW_QUALITY})]:
    t0 = time.perf_counter()
    d = nvavif_py.encode_file(src, **kw)
    dt = time.perf_counter() - t0
    print(f"  {name}: {dt*1000:.0f}ms, {len(d):,} bytes")

print("\nDONE")
