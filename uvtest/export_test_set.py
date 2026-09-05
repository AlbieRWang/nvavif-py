"""Export the supported test images as AVIF files with a browsable gallery."""

from __future__ import annotations

import argparse
import html
import json
import os
import time
from pathlib import Path
from urllib.parse import quote

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / "test_imgs"

for dll_dir in (ROOT / "ffmpeg-out" / "bin", ROOT / "msys64" / "mingw64" / "bin"):
    if dll_dir.is_dir() and hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(dll_dir))

import nvavif_py

Image.MAX_IMAGE_PIXELS = None


def file_url(path: Path, base: Path) -> str:
    relative = Path(os.path.relpath(path, base)).as_posix()
    return quote(relative, safe="/")


def write_gallery(output_dir: Path, records: list[dict], skipped: list[dict]) -> None:
    alpha_count = sum(1 for r in records if r.get("alpha"))
    opaque_count = len(records) - alpha_count
    cards: list[str] = []
    for record in records:
        source = Path(record["source"])
        avif_path = output_dir / record["output"]
        source_url = file_url(source, output_dir)
        avif_url = file_url(avif_path, output_dir)
        alpha_flag = record.get("alpha", False)
        badge = ' <span class="badge alpha">ALPHA</span>' if alpha_flag else ' <span class="badge opaque">OPAQUE</span>'
        data_attr = "alpha" if alpha_flag else "opaque"
        cards.append(
            f"""<article class="card" data-type="{data_attr}">
  <h2>{html.escape(source.name)}{badge}</h2>
  <p>{record['width']}x{record['height']} | {record['bytes']:,} bytes | encode {record['encode_ms']:.1f} ms | decode {record['decode_ms']:.1f} ms</p>
  <div class="pair">
    <figure><figcaption>Original</figcaption><a href="{source_url}"><img loading="lazy" src="{source_url}" alt="Original {html.escape(source.name)}"></a></figure>
    <figure><figcaption>AVIF</figcaption><a href="{avif_url}"><img loading="lazy" src="{avif_url}" alt="AVIF {html.escape(source.name)}"></a></figure>
  </div>
</article>"""
        )

    skipped_html = "".join(
        f"<li>{html.escape(item['name'])}: {item['width']}x{item['height']}</li>"
        for item in skipped
    )
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>nvavif_py test set</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin: 0; padding: 24px; background: #15171a; color: #edf0f2; font: 14px/1.45 system-ui, sans-serif; }}
  header {{ max-width: 1500px; margin: 0 auto 16px; }}
  h1 {{ margin: 0 0 6px; font-size: 26px; }}
  h2 {{ margin: 0; font-size: 15px; overflow-wrap: anywhere; }}
  p {{ margin: 5px 0 14px; color: #aeb6bf; }}
  .filters {{ display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }}
  .filter-btn {{ padding: 6px 16px; border: 1px solid #30363d; background: #202428; color: #edf0f2; border-radius: 4px; cursor: pointer; font: inherit; font-size: 13px; }}
  .filter-btn:hover {{ background: #2a2f35; }}
  .filter-btn.active {{ background: #0078d4; border-color: #0078d4; color: #fff; }}
  .grid {{ max-width: 1500px; margin: 0 auto; display: grid; grid-template-columns: repeat(auto-fit, minmax(520px, 1fr)); gap: 16px; }}
  .card {{ padding: 14px; border: 1px solid #30363d; background: #202428; border-radius: 6px; transition: opacity 0.2s; }}
  .card.hidden {{ display: none; }}
  .pair {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
  figure {{ margin: 0; min-width: 0; }}
  figcaption {{ margin-bottom: 5px; color: #aeb6bf; font-size: 12px; }}
  img {{ display: block; width: 100%; height: 300px; object-fit: contain; background: #0b0c0d; }}
  a {{ color: inherit; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px; font-weight: 600; margin-left: 8px; vertical-align: middle; }}
  .badge.alpha {{ background: #c8a400; color: #1a1a1a; }}
  .badge.opaque {{ background: #2d8a3e; color: #fff; }}
  .skipped {{ max-width: 1500px; margin: 28px auto 0; color: #aeb6bf; }}
</style>
</head>
<body>
<header>
  <h1>nvavif_py completed test set</h1>
  <p>Tested: {len(records)} | Skipped over NVENC dimension limit: {len(skipped)} | Device: auto</p>
  <p>Opaque: {opaque_count} | Alpha: {alpha_count}</p>
</header>
<nav class="filters">
  <button class="filter-btn active" data-filter="all">All ({len(records)})</button>
  <button class="filter-btn" data-filter="opaque">Opaque ({opaque_count})</button>
  <button class="filter-btn" data-filter="alpha">Alpha ({alpha_count})</button>
</nav>
<main class="grid">
{''.join(cards)}
</main>
<section class="skipped">
  <h2>Skipped images</h2>
  <ul>{skipped_html or '<li>None</li>'}</ul>
</section>
<script>
  document.querySelectorAll('.filter-btn').forEach(btn => {{
    btn.addEventListener('click', () => {{
      document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const filter = btn.dataset.filter;
      document.querySelectorAll('.card').forEach(card => {{
        if (filter === 'all' || card.dataset.type === filter) {{
          card.classList.remove('hidden');
        }} else {{
          card.classList.add('hidden');
        }}
      }});
    }});
  }});
</script>
</body>
</html>
"""
    (output_dir / "index.html").write_text(page, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-dimension",
        type=int,
        default=8192,
        help="skip an image when either dimension exceeds this value",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "out" / "batch_gallery",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict] = []
    skipped: list[dict] = []
    failures: list[str] = []
    files = sorted(path for path in SOURCE_DIR.iterdir() if path.is_file())

    for index, source in enumerate(files, 1):
        with Image.open(source) as image:
            width, height = image.size
            has_alpha = "A" in image.getbands() or "transparency" in image.info

        if width > args.max_dimension or height > args.max_dimension:
            skipped.append({"name": source.name, "width": width, "height": height})
            print(f"[{index:02d}/{len(files)}] SKIP {source.name}: {width}x{height}", flush=True)
            continue

        output_name = f"{len(records) + 1:03d}.avif"
        output_path = args.output_dir / output_name
        encode_started = time.perf_counter()
        try:
            encoded = nvavif_py.encode_file(source, device="auto")
            output_path.write_bytes(encoded)
        except Exception as error:
            failures.append(f"{source.name}: encode failed: {error}")
            print(f"[{index:02d}/{len(files)}] FAIL {source.name}: {error}", flush=True)
            continue
        encode_ms = (time.perf_counter() - encode_started) * 1000

        decode_started = time.perf_counter()
        try:
            with Image.open(output_path) as output_image:
                output_image.load()
                pillow_mode = output_image.mode
                output_size = output_image.size
            decoded = nvavif_py.decode_file(output_path)
            expected_channels = 4 if has_alpha else 3
            if decoded.ndim != 3 or decoded.shape[2] != expected_channels:
                raise RuntimeError(
                    f"decoded shape {decoded.shape}, expected {expected_channels} channels"
                )
        except Exception as error:
            failures.append(f"{source.name}: validation failed: {error}")
            print(f"[{index:02d}/{len(files)}] FAIL {source.name}: {error}", flush=True)
            continue
        decode_ms = (time.perf_counter() - decode_started) * 1000

        record = {
            "source": str(source),
            "name": source.name,
            "output": output_name,
            "width": width,
            "height": height,
            "alpha": has_alpha,
            "bytes": len(encoded),
            "encode_ms": encode_ms,
            "decode_ms": decode_ms,
            "pillow_mode": pillow_mode,
            "output_size": list(output_size),
            "decoded_shape": list(decoded.shape),
        }
        records.append(record)
        print(
            f"[{index:02d}/{len(files)}] OK {source.name}: "
            f"{encode_ms:.1f} ms, {len(encoded):,} bytes",
            flush=True,
        )

    manifest = {
        "source_dir": str(SOURCE_DIR),
        "output_dir": str(args.output_dir),
        "device": "auto",
        "max_dimension": args.max_dimension,
        "tested": records,
        "skipped": skipped,
        "failures": failures,
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_gallery(args.output_dir, records, skipped)
    print(f"Gallery: {args.output_dir / 'index.html'}", flush=True)
    print(f"Manifest: {args.output_dir / 'manifest.json'}", flush=True)
    print(
        f"tested={len(records)} skipped={len(skipped)} failures={len(failures)}",
        flush=True,
    )

    if failures:
        raise SystemExit("\n".join(failures))


if __name__ == "__main__":
    main()
