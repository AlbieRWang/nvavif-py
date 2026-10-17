"""Quantify the copy cost the single-copy pixel path removes.

Times, interleaved per image (so machine state cancels), the operations the
previous pipeline paid and the ones it now pays:
  old pixel buffer = np.array(img).tobytes()
  new pixel buffer = img.tobytes()
  old-only extra   = ImageOps.exif_transpose(img)  (orientation 1 -> a copy)
"""
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

import nvavif_py as nv

SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "test_imgs")
REPS = int(sys.argv[2]) if len(sys.argv) > 2 else 5
MIN_MP = float(sys.argv[3]) if len(sys.argv) > 3 else 8.0


def median_ms(fn, reps=REPS):
    times = []
    for _ in range(reps):
        t = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t) * 1e3)
    times.sort()
    return times[len(times) // 2]


rows = []
for path in sorted(p for p in SRC.iterdir() if p.suffix.lower() in SUFFIXES):
    try:
        with Image.open(path) as im:
            if im.width * im.height < MIN_MP * 1e6:
                continue
            im = nv._ensure_srgb(im)
            if im.getexif().get(0x0112, 1) != 1:
                continue
            if im.mode != 'RGB':
                continue
            im.load()
            # Prime both buffers once so the first allocation is not counted.
            im.tobytes()
            np.array(im).tobytes()
            ImageOps.exif_transpose(im)

            t_new = median_ms(lambda: im.tobytes())
            t_old = median_ms(lambda: np.array(im).tobytes())
            t_exif = median_ms(lambda: ImageOps.exif_transpose(im))
            rows.append((path.name, t_old, t_new, t_exif))
    except Exception as exc:  # noqa: BLE001
        print("skip", path.name, exc)

if not rows:
    sys.exit("no eligible images")

old = np.array([r[1] for r in rows])
new = np.array([r[2] for r in rows])
exif = np.array([r[3] for r in rows])
print(f"images: {len(rows)}  reps: {REPS}")
print(f"  old pixel buffer  median {np.median(old):6.2f} ms  mean {old.mean():6.2f}")
print(f"  new pixel buffer  median {np.median(new):6.2f} ms  mean {new.mean():6.2f}")
print(f"  exif_transpose    median {np.median(exif):6.2f} ms  mean {exif.mean():6.2f}")
saved = (old - new) + exif
print(f"  saved per image   median {np.median(saved):6.2f} ms  mean {saved.mean():6.2f}")
for name, o, n, e in rows:
    print(f"    {name[:44]:44s} old {o:6.2f}  new {n:6.2f}  exif {e:6.2f}")
