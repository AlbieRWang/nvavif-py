# 本地工具链清单与必要性评估

> 基于 2026-08-04 至 2026-09-05 的构建与验证环境快照。

---

## 1. 项目内构建产物

| 目录 | 大小 | 用途 | 必要性 |
|---|---|---|---|
| `msys64/` | **~3.3 GB** | MSYS2 环境，含 MinGW64 GCC、clang、NASM、Git、coreutils、dav1d 等 | **建议保留**。重新搭建需要重新下载和编译，且 CI 中不可复用。若长期不再修改代码可清理。 |
| `ffmpeg-out/` | ~16 MB | FFmpeg 8.1 开发产物（headers、`.lib`、DLLs） | **建议保留**。Cargo 构建依赖 `FFMPEG_DIR` 指向此处，缺失则无法重新编译 Rust 扩展。 |
| `FFmpeg/` | 源目录 | FFmpeg 8.1 源码 | **可清理**。构建完成后已无用途，产物已安装到 `ffmpeg-out/`。 |
| `dav1d/` | 源目录 | dav1d AV1 解码器源码 | **可清理**。同上，构建产物已安装到 `ffmpeg-out/`。 |
| `nv-codec-headers/` | 源目录 | NVIDIA NVENC headers | **可清理**。FFmpeg 构建时已编译进产物。 |
| `uvtest/` | ~370 MB | 本地 Python 验证环境，含 `.venv`、wheel 安装、测试脚本、benchmark 结果、gallery 输出 | **建议保留**。`uvtest/out/batch_gallery/` 保存了 75 张压缩对照结果，`.venv` 内含本地构建的 wheel 和全部依赖。`uvtest/benchmark.py`、`export_test_set.py`、`test_nvavif.py` 是长期可复用的测试脚本。 |
| `test_imgs/` | 图片集 | 79 张测试图片（JPG/PNG/GIF） | **建议保留**。作为持续验证的基准数据集。 |

---

## 2. Rust 工具链（用户主目录，非项目内）

| 路径 | 说明 | 必要性 |
|---|---|---|
| `~/.cargo/bin/` | rustc, cargo, rustfmt, clippy, rust-analyzer, miri 等 | **保留**。Rust 项目编译依赖，与系统无关，清理后需重新安装。 |
| `~/.rustup/` | 默认 stable toolchain | **保留**。同上。 |
| `~/.cargo/registry/` | Cargo 包缓存 | **保留**。避免重新下载依赖。 |

> Rust 安装在 `~/.cargo` 和 `~/.rustup`，不污染项目目录，也未被 `.gitignore` 覆盖，属于正常全局工具链。

---

## 3. 已清理/不应保留的内容

| 内容 | 状态 |
|---|---|
| `C:\Users\ricar\AppData\Local\Temp\nvavif-py-*` | **已清理**。验证环境从早期 C 盘临时目录迁移到 `O:\Project\Media\nvavif-py\uvtest\`。 |
| `src/_nvavif_py.pyd`（旧 .pyd） | **已清理**。`maturin develop` 遗留的旧扩展文件，已覆盖安装 wheel 后删除。 |
| `uvtest/out/batch_gallery/gallery_preview.png` | 可清理。浏览器验证时的截图。 |
| `uvtest/out/_bench_tmp/` | 已自动清理。benchmark 脚本运行后自行删除。 |
| `uvtest/out/_export_tmp/` | 已自动清理。export 脚本运行后自行删除。 |
| MSYS2 `home/Ricardo/` 缓存 | `cc2utJQm`、`cc4A6BF1` 等编译临时目录 | 可清理。MSYS2 编译时自动生成的临时缓存，`ffmpeg-out/` 产物已不受影响。 |

---

## 4. 磁盘占用汇总

| 范围 | 大小 |
|---|---|
| 项目内构建工具链（msys64 + ffmpeg-out + FFmpeg + dav1d + nv-codec-headers） | **~3.3 GB** |
| uvtest 环境 | **~370 MB** |
| Rust 全局工具链（~/.cargo + ~/.rustup） | **未估算**（估计 1–2 GB） |

如果未来确认不再修改 nvavif_py 源码：

- **最小可保留**：`ffmpeg-out/`（16 MB）+ `uvtest/`（~370 MB），总计 ~386 MB
- **可删除**：`msys64/`（3.3 GB）、`FFmpeg/`、`dav1d/`、`nv-codec-headers/`，回收约 3.3 GB+
- Rust 全局工具链与本项目解耦，可独立决定保留或清理

---

## 5. 各目录删除后影响说明

| 删除目录 | 对当前功能的影响 |
|---|---|
| `msys64/` | 无法重新编译 Rust 扩展。已安装的 wheel 不受影响，但源码更新后无法重建。 |
| `ffmpeg-out/` | 同上，Cargo 构建报 FFmpeg not found。 |
| `FFmpeg/`、`dav1d/`、`nv-codec-headers/` | 无影响。仅为构建源，产物已在 `ffmpeg-out/`。 |
| `uvtest/` | 丢失所有测试脚本、benchmark 结果、gallery 输出和验证环境。重新验证需重建。 |
| `test_imgs/` | 丢失验证基准数据集。 |
| `~/.cargo/`、`~/.rustup/` | 无法编译任何 Rust 项目，需全局重新安装。 |

---

## 6. `.gitignore` 状态

已在 `.gitignore` 中添加以下排除规则：

```gitignore
msys64/
FFmpeg/
dav1d/
nv-codec-headers/
ffmpeg-out/
uvtest/
uv.lock
test_imgs/
.cortexkit/
```

这些大体积构建产物和测试环境均不进入版本控制。`DEVELOPMENT_NOTES.md` 和 `TOOLCHAIN_INVENTORY.md` 作为项目文档建议保留在仓库中。