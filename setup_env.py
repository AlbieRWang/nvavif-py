"""One-command environment setup. Idempotent — run it any time, from anywhere.

    uv run python setup_env.py

What it does:
1. uv sync — pillow/numpy plus the dev group (maturin, delvewheel, psutil,
   pynvml). The project itself is NOT installed by uv ([tool.uv]
   package = false), so syncing can never shadow the wheel again.
2. Ensure a delvewheel-repaired wheel exists for the current interpreter in
   dist/repaired-current/ (repairing from dist-local/ if needed). The
   repaired wheel bundles the FFmpeg DLLs, so `import nvavif_py` works with
   no manual add_dll_directory.
3. Force-reinstall that wheel into the root .venv (the only nvavif_py the
   venv should ever contain).
4. Smoke test: import from the repo root, GPU probe, encode + decode
   roundtrip on a tiny array.

Run everything else via `uv run python uvtest/<script>.py` as usual.
"""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPAIRED_DIR = ROOT / "dist" / "repaired-current"
LOCAL_DIR = ROOT / "dist-local"


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("+", " ".join(str(c) for c in cmd))
    result = subprocess.run(cmd, cwd=ROOT, **kwargs)
    if result.returncode != 0:
        sys.exit(f"FAILED ({result.returncode}): {' '.join(str(c) for c in cmd)}")
    return result


def python_tag() -> str:
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def find_wheel(directory: Path, tag: str) -> Path | None:
    if not directory.is_dir():
        return None
    matches = sorted(directory.glob(f"nvavif_py-*-{tag}-{tag}-*.whl"))
    return matches[-1] if matches else None


def ensure_repaired_wheel(tag: str) -> Path:
    wheel = find_wheel(REPAIRED_DIR, tag)
    source = find_wheel(LOCAL_DIR, tag)
    if wheel and (not source or wheel.stat().st_mtime >= source.stat().st_mtime):
        print(f"repaired wheel up to date: {wheel.relative_to(ROOT)}")
        return wheel
    if not source:
        sys.exit(
            f"No wheel for {tag} in {LOCAL_DIR} or {REPAIRED_DIR}.\n"
            "Build one first: build.bat"
        )
    # A fresh maturin build is newer than the last repair output (or no
    # repair exists yet) — same-version wheels must NOT be reused, or stale
    # binaries get reinstalled silently.
    if wheel:
        wheel.unlink()
    REPAIRED_DIR.mkdir(parents=True, exist_ok=True)
    run(
        [
            sys.executable,
            "-m",
            "delvewheel",
            "repair",
            "--add-path",
            f"{ROOT / 'ffmpeg-out' / 'bin'};{ROOT / 'msys64' / 'mingw64' / 'bin'}",
            "-w",
            str(REPAIRED_DIR),
            str(source),
        ]
    )
    wheel = find_wheel(REPAIRED_DIR, tag)
    if not wheel:
        sys.exit("repair produced no wheel")
    return wheel


def smoke_test() -> None:
    import numpy as np

    import nvavif_py as nv

    print(f"nvavif_py from: {nv.__file__}")
    assert "site-packages" in nv.__file__.lower(), "not importing the venv wheel!"
    print(f"GPU available: {nv.is_supported()}")

    rng = np.random.default_rng(0)
    # 256x256 stays above the NVENC minimum frame size, so the smoke test
    # exercises the real GPU path instead of triggering the CPU fallback.
    img = (rng.random((256, 256, 3)) * 255).astype(np.uint8)
    data = nv.encode_file(img, cq=20)
    back = nv.decode_file(_write_tmp(data))
    assert back.shape == (256, 256, 3) and back.dtype == np.uint8
    print(f"roundtrip OK: {len(data)} bytes encoded, decoded {back.shape}")


def _write_tmp(data: bytes) -> str:
    tmp = ROOT / "uvtest" / "out" / "setup_env_roundtrip.avif"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(data)
    return str(tmp)


def main() -> None:
    print(f"python {platform.python_version()} ({python_tag()}) at {sys.executable}")
    run(["uv", "sync"])
    wheel = ensure_repaired_wheel(python_tag())
    run(
        [
            "uv",
            "pip",
            "install",
            "--force-reinstall",
            "--no-deps",
            str(wheel),
        ]
    )
    smoke_test()
    print("\nENV OK — run scripts with: uv run python uvtest/<script>.py")


if __name__ == "__main__":
    main()
