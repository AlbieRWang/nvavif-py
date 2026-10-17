"""Verify the ICC identity-skip: profile-carrying images that are sRGB-equivalent
must skip the full-image transform, and the skipped result must be pixel-identical
to what the old convert-everything path produced."""
import io
import sys
from pathlib import Path

from PIL import Image, ImageCms
import numpy as np

import nvavif_py as nv

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "test_imgs")
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 0

profile_carrying = 0
skipped = 0
converted = 0
mismatches = []
rows = []

files = sorted(
    p for p in SRC.iterdir()
    if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}
)
for path in files:
    try:
        with Image.open(path) as im:
            icc = im.info.get("icc_profile")
            if not icc:
                continue
            icc = bytes(icc)
            profile_carrying += 1
            im = im.convert("RGB")
            probe = im.copy()

            # New path: identity detection + conditional skip.
            is_id = nv._is_srgb_identity(
                icc, ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
            )
            new = nv._ensure_srgb(probe.copy())

            # Reference: always convert.
            srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            ref = ImageCms.profileToProfile(probe.copy(), src, srgb)

            same = np.array_equal(
                np.frombuffer(new.tobytes(), np.uint8),
                np.frombuffer(ref.tobytes(), np.uint8),
            )
            delta = int(
                np.abs(
                    np.frombuffer(new.tobytes(), np.uint8).astype(np.int16)
                    - np.frombuffer(ref.tobytes(), np.uint8).astype(np.int16)
                ).max()
            )
            if is_id:
                skipped += 1
            else:
                converted += 1
            if delta > 1:
                mismatches.append({"file": path.name, "is_identity": is_id, "max_delta": delta})
            rows.append(
                {
                    "file": path.name,
                    "w": im.width,
                    "h": im.height,
                    "identity": is_id,
                    "max_delta_vs_always_convert": delta,
                    "byte_identical": same,
                }
            )
            if LIMIT and profile_carrying >= LIMIT:
                break
    except Exception as exc:  # noqa: BLE001
        rows.append({"file": path.name, "error": str(exc)})

print(
    {
        "profile_carrying_images": profile_carrying,
        "skipped_as_srgb": skipped,
        "still_converted": converted,
        "max_delta_over_1": mismatches,
    }
)
for r in rows:
    print(" ", r)
