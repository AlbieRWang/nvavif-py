"""Confirm the single-copy pixel path hands Rust exactly the bytes the old
np.array-based path produced, for every supported image in a directory."""
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

import nvavif_py as nv

SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}
SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "test_imgs")


def to_mode(img):
    if img.mode == 'P':
        return img.convert('RGBA' if 'transparency' in img.info else 'RGB')
    if img.mode in ('L', 'I', 'F'):
        return img.convert('RGB')
    if img.mode == 'LA':
        return img.convert('RGBA')
    if img.mode not in ('RGB', 'RGBA'):
        return img.convert('RGBA' if getattr(img, 'has_alpha', False) else 'RGB')
    return img


def old_pixels(path):
    with Image.open(path) as im:
        img = to_mode(ImageOps.exif_transpose(nv._ensure_srgb(im)))
        return img.size, np.array(img).tobytes()


def new_pixels(path):
    with Image.open(path) as im:
        img = nv._ensure_srgb(im)
        if img.getexif().get(0x0112, 1) != 1:
            img = ImageOps.exif_transpose(img)
        img = to_mode(img)
        if img.mode == 'RGB' and img.width % 2 == 0 and img.height % 2 == 0:
            return img.size, img.tobytes()
        return img.size, np.array(img).tobytes()


checked = 0
bad = []
for path in sorted(p for p in SRC.iterdir() if p.suffix.lower() in SUFFIXES):
    try:
        os_size, op = old_pixels(path)
        ns_size, npb = new_pixels(path)
        checked += 1
        if os_size != ns_size or op != npb:
            bad.append({"file": path.name, "size": (os_size, ns_size), "bytes_eq": op == npb})
    except Exception as exc:  # noqa: BLE001
        bad.append({"file": path.name, "error": str(exc)})

print({"checked": checked, "mismatches": bad})
