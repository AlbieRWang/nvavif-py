# nvavif_py 开发、安装与可行性验证记录

## 1. 文档信息

- 记录日期：2026-08-04
- 项目：`nvavif_py`
- 平台：Windows x64
- 测试 GPU：NVIDIA RTX 4070
- NVIDIA 驱动：610.88
- Python：`3.13`
- Rust：`rustc 1.97.1`
- FFmpeg：`8.1`
- 主要 Rust 依赖：PyO3 `0.27`、`ffmpeg-next 8.1`、`avif-serialize 0.8.8`、`rav1e 0.7.1`、`dav1d`

本文记录本次从 PyPI 可行性验证、源码问题定位、Windows 本地编译、运行时 DLL 排查，到最终批量功能和性能测试的完整过程。

## 2. 最终结论

当前版本对正常尺寸图片是可用的：

- 普通 RGB/JPEG/PNG 默认路径可以使用 NVENC GPU 编码。
- 没有可用 NVENC 时，`device="auto"` 会切换到 rav1e CPU 编码。
- alpha 已改为符合 AVIF 规范的独立 YUV400/monochrome AV1 辅助流。
- `decode_file()` 已能读取辅助 alpha 流并返回 RGBA NumPy 数组。
- 浏览器对透明 AVIF 的实际合成已经验证通过。
- 8/10-bit、YUV420/YUV444、f32 输入、Pillow 插件、奇数尺寸等功能测试通过。

仍然存在的已知限制：

- 当前 NVENC AV1 路径的实测最大宽度和高度都是 `8192`。超过任一轴时，GPU 不能编码原始尺寸。
- 超过尺寸限制的图片目前在 `auto` 模式下会回落到 CPU，测试脚本暂时直接跳过它们。
- alpha 的 YUV400 编码目前使用 rav1e CPU，因为当前 `av1_nvenc` 路径没有安全的 monochrome/YUV400 输入接口。这会成为透明大图的主要性能瓶颈。
- YUV444 的 GPU 路径依赖具体 FFmpeg/NVENC 运行时能力。当前本机 `auto` 模式可以在 GPU 拒绝时回落到 CPU 并成功输出；不能把当前机器上的 YUV444 视为稳定的显式 GPU 路径。

## 3. 项目架构

### 3.1 编码路径

Python API 由 PyO3 暴露，核心实现在 `src/lib.rs`：

1. `encode_file()` 读取路径、Pillow 对象、NumPy 数组或原始 bytes。
2. Rust 侧进行 dtype、尺寸、EXIF、ICC、EXIF orientation 和奇数尺寸处理。
3. 使用 Rayon 并行转换为 NV12/P010 或 YUV444P/YUV444P16LE。
4. 正常颜色流优先交给 FFmpeg 的 `av1_nvenc`。
5. NVENC 不可用或当前输入不受支持时，`auto` 切换到 rav1e。
6. 使用 `avif-serialize` 将颜色 AV1 流、可选 alpha AV1 流、CICP 和 EXIF 封装成 AVIF。

### 3.2 alpha 路径

AVIF 的 alpha 不是普通 NV12/YUV420 颜色流，而是独立的 monochrome/YUV400 AV1 item。

因此当前透明图片的流程是：

- 颜色流：NVENC GPU。
- alpha 流：rav1e CPU，`ChromaSampling::Cs400`。
- 两条流并行编码，避免 alpha 编码完全串行阻塞颜色编码。
- 容器中设置正确的 alpha 辅助流元数据。

使用 NV12 编码 alpha、然后只修改容器元数据是不符合格式的，虽然某些解码器可能勉强读取，但浏览器可能把整张图片当成透明。这是之前浏览器透明度错误的根本原因。

### 3.3 解码路径

`decode_file()` 使用 FFmpeg/dav1d 解码所有视频流：

- 第一个视频流作为颜色流，转换为 RGB。
- 其它视频流作为候选 alpha 流。
- 读取 alpha 流的 luma plane。
- 将 RGB 和 alpha 合并为 `H x W x 4` 的 NumPy 数组。
- 没有 alpha 时仍返回 `H x W x 3`。

## 4. 本次发现并修复的问题

| 问题 | 原因 | 修复 | 当前状态 |
|---|---|---|---|
| YUV444 GPU 编码直接失败 | 原代码把 FFmpeg `profile` 设置为字符串 `high`，而 `av1_nvenc` 的 AV1 profile 选项使用整数值 | 对 YUV444 设置 `profile="1"`，并同步正确的 chroma metadata | `auto` 可回落 CPU；显式 GPU 仍取决于当前 NVENC 能力 |
| 浏览器显示透明图全透明 | alpha item 实际编码成 NV12/YUV420，但容器标记为 monochrome/YUV400 | alpha 改用 rav1e `Cs400`，颜色与 alpha 并行编码 | Pillow、Chrome、实际 RGBA 像素均验证通过 |
| `decode_file()` 只返回 3 通道 | 解码器只读取颜色视频流，没有读取 AVIF 的辅助 alpha 流 | 枚举第二个视频流、提取 luma、合并 RGBA | 透明图返回 4 通道 |
| `is_supported()` 误报 GPU 可用 | 原探测只打开编码器，没有真正送入一帧 | 改为编码一帧 `256x256` NV12 测试帧并缓存结果 | 能验证实际 NVENC 初始化和首帧编码 |
| auto GPU 运行时失败直接中断 | 编码器打开成功不代表当前尺寸或参数一定能送帧 | `device="auto"` 在颜色编码失败时回落 CPU；`device="gpu"` 保留显式错误 | 普通图片和不支持的输入都能明确处理 |
| 超大图片触发慢速 CPU fallback | NVENC 限制的是宽高轴，不是单纯总像素数 | 当前 benchmark 跳过宽或高超过 `8192` 的图片 | 生产路径暂时仍保留 auto CPU fallback |

## 5. Windows 本地安装布局

为了避免把大型开发依赖散落到系统目录，本次构建相关文件保留在项目目录：

```text
O:\Project\Media\nvavif-py\
├─ msys64\       MSYS2、MinGW64 工具链、NASM、clang 和包缓存
├─ FFmpeg\       FFmpeg 8.1 源码
├─ dav1d\        dav1d 源码
├─ ffmpeg-out\   FFmpeg headers、import libs、DLL 和 dav1d 安装产物
├─ uvtest\       uv 验证环境、测试脚本和性能报告
└─ test_imgs\    样本图片
```

Rust 是例外：本次通过 rustup 安装后使用默认用户目录：

```text
C:\Users\ricar\.cargo\
C:\Users\ricar\.rustup\
```

Rust 本身不在项目目录，但 Cargo registry、MSYS2、FFmpeg、dav1d 和生成的 FFmpeg 产物均可复用，不需要每次重装。

## 6. 从 PyPI 验证安装

这是验证已发布 wheel 是否能正常工作的最短路径。环境放在项目的 `uvtest`，没有放到 `C:\` 临时目录：

```powershell
Set-Location O:\Project\Media\nvavif-py\uvtest
uv add nvavif_py
uv run python -c "import nvavif_py; print(nvavif_py.is_supported())"
```

验证脚本：

```powershell
.venv\Scripts\python.exe .\test_nvavif.py
```

已发布 wheel 的优点是通过 CI 的 `delvewheel` 绑定 FFmpeg 和 MinGW runtime DLL，用户通常不需要自己安装 FFmpeg 开发环境。

## 7. 从源码构建的依赖安装

### 7.1 Rust

安装并确认：

```powershell
rustc --version
cargo --version
```

本次安装过程中网络下载曾遇到问题，使用了本机代理 `127.0.0.1:2801`。代理只注入到下载子进程，没有写入持久终端环境或系统环境变量。以后遇到相同问题时也应保持这个原则。

### 7.2 MSYS2

项目内 MSYS2 使用 `MINGW64` 环境。CI 的关键配置也是 `msystem: MINGW64`，不能在纯 MSYS shell 中构建 FFmpeg。

需要的主要工具包括：

```text
git
make
nasm
mingw-w64-x86_64-gcc
mingw-w64-x86_64-pkgconf
mingw-w64-x86_64-meson
mingw-w64-x86_64-ninja
mingw-w64-x86_64-clang
diffutils
```

关键用途：

- `nasm`：rav1e 和 dav1d 的汇编构建。
- `mingw-w64-x86_64-clang`：为 Rust bindgen 提供 `libclang.dll`。
- `pkgconf`：让 FFmpeg 找到本地 dav1d 和其它依赖。
- `meson`/`ninja`：构建 dav1d。

### 7.3 nv-codec-headers、dav1d 和 FFmpeg

构建顺序：

1. 安装 `nv-codec-headers`，提供 NVENC API headers。
2. 使用 Meson/Ninja 构建并安装 dav1d 到 `ffmpeg-out`。
3. 从 FFmpeg `release/8.1` 分支构建精简的共享库。

FFmpeg 必须包含这些功能：

```text
av1_nvenc encoder
libdav1d decoder
AV1 parser
MOV/AVIF demuxer
file protocol
pipe protocol
```

CI 中使用的核心 configure 选项位于 `.github/workflows/CI.yml`，Windows 构建对应以下配置：

```bash
./configure \
  --prefix=$FFMPEG_OUT \
  --enable-shared \
  --disable-static \
  --disable-doc \
  --disable-programs \
  --disable-everything \
  --enable-ffnvcodec \
  --enable-libdav1d \
  --enable-decoder=libdav1d \
  --enable-nvenc \
  --enable-encoder=av1_nvenc \
  --enable-parser=av1 \
  --enable-demuxer=mov \
  --enable-protocol=file \
  --enable-protocol=pipe \
  --extra-cflags="-I$FFMPEG_OUT/include" \
  --extra-ldflags="-L$FFMPEG_OUT/lib"
make -j$(nproc)
make install
```

本次遇到过一次 `nv-codec-headers` 官方地址 TLS EOF，改用 GitHub 镜像重新下载后继续。该问题是下载链路问题，不是源代码问题。

## 8. Cargo 和 maturin 本地构建

`build.rs` 只在存在 `FFMPEG_DIR` 时注册 FFmpeg 的 `include`、`lib` 和 `bin` 路径。没有该变量时，Cargo 会找到 Rust 依赖，但无法正确链接本地 FFmpeg。

本地构建需要的进程级变量：

```text
FFMPEG_DIR=O:\Project\Media\nvavif-py\ffmpeg-out
FFMPEG_INCLUDE_DIR=O:\Project\Media\nvavif-py\ffmpeg-out\include
FFMPEG_LIB_DIR=O:\Project\Media\nvavif-py\ffmpeg-out\lib
PKG_CONFIG_PATH=O:\Project\Media\nvavif-py\ffmpeg-out\lib\pkgconfig
LIBCLANG_PATH=O:\Project\Media\nvavif-py\msys64\mingw64\bin
CLANG_PATH=O:\Project\Media\nvavif-py\msys64\mingw64\bin\clang.exe
PATH 中加入：
  C:\Users\ricar\.cargo\bin
  O:\Project\Media\nvavif-py\ffmpeg-out\bin
  O:\Project\Media\nvavif-py\msys64\usr\bin
  O:\Project\Media\nvavif-py\msys64\mingw64\bin
```

这些变量只应作用于 Cargo/maturin 子进程，不应写入系统环境变量。示意命令：

```powershell
$repo = 'O:\Project\Media\nvavif-py'
$ffmpeg = Join-Path $repo 'ffmpeg-out'
$msys = Join-Path $repo 'msys64'

$env:FFMPEG_DIR = $ffmpeg
$env:FFMPEG_INCLUDE_DIR = Join-Path $ffmpeg 'include'
$env:FFMPEG_LIB_DIR = Join-Path $ffmpeg 'lib'
$env:PKG_CONFIG_PATH = Join-Path $ffmpeg 'lib\pkgconfig'
$env:LIBCLANG_PATH = Join-Path $msys 'mingw64\bin'
$env:CLANG_PATH = Join-Path $msys 'mingw64\bin\clang.exe'
$env:PATH = "$env:USERPROFILE\.cargo\bin;$ffmpeg\bin;$msys\usr\bin;$msys\mingw64\bin;$env:PATH"

cargo check
maturin build --release --out dist-local
```

Windows FFmpeg 构建生成的 import library 名称可能是 `avcodec.lib` 等，而 Cargo 探测需要 `libavcodec.lib` 等名称。CI 在构建 maturin wheel 前执行了对应的 `.lib` 复制/重命名步骤。

本地 wheel 直接用 `maturin build` 生成时不一定包含所有 DLL，因此测试时需要把 `ffmpeg-out\bin` 和 `msys64\mingw64\bin` 加入进程 DLL 搜索路径。`uvtest/benchmark.py` 已通过 `os.add_dll_directory()` 自动处理这一步。

CI 的最终 Windows wheel 还会执行：

```powershell
pip install delvewheel
delvewheel repair dist\*.whl --add-path "ffmpeg-out\bin;C:\msys64\mingw64\bin" -w dist\repaired
```

因此发布 wheel 和本地未 repair wheel 的 DLL 行为不同，这是本次本地导入错误的来源之一。

## 9. 编译过程中遇到的阻塞

| 阶段 | 问题 | 解决 |
|---|---|---|
| 初始源码构建 | 默认 Rust 路径不存在，机器上当时没有可用 Rust toolchain | 按默认 rustup 位置安装 Rust，确认 `rustc 1.97.1` |
| FFmpeg 链接 | 系统中已有的 `ffmpeg.exe` 只有运行时用途，没有 Cargo 所需 headers/import libs | 在项目目录重新构建 FFmpeg 开发产物 |
| FFmpeg configure | 在纯 MSYS shell 中运行，CI 所需的 MINGW64 环境不成立 | 使用 `MSYSTEM=MINGW64`/MSYS2 MINGW64 shell |
| rav1e 构建 | 找不到 NASM | 将 `msys64\usr\bin` 放入 Cargo 子进程 PATH |
| bindgen | 找不到 `libclang.dll` | 安装 `mingw-w64-x86_64-clang` 并设置 `LIBCLANG_PATH` |
| ffmpeg-sys-next | link detection probe 失败，普通 Cargo 输出没有显示真正原因 | 使用 verbose Cargo 输出，补齐 FFmpeg lib/include/pkg-config 路径 |
| 本地 wheel 导入 | `_nvavif_py.pyd` 能找到，但 FFmpeg/MinGW DLL 无法加载 | 临时加入 `ffmpeg-out\bin` 和 `msys64\mingw64\bin` |
| Python 测试 | 从仓库根目录启动时，源码包内旧的 `.pyd` shadow 了 uvtest wheel | 从 `uvtest` 启动，确保使用 venv 安装的 wheel，并清理旧生成物 |
| 全量样本测试 | 超大图进入 rav1e 后耗时很长 | 本轮测试跳过宽或高大于 `8192` 的图片 |

## 10. 测试脚本与运行方法

### 10.1 定向功能回归

脚本：[`uvtest/test_nvavif.py`](O:/Project/Media/nvavif-py/uvtest/test_nvavif.py)

覆盖：

- 普通 JPG 默认路径
- RGBA PNG
- 显式 CPU rav1e
- 8-bit/10-bit YUV420/YUV444
- auto-CQ
- NumPy uint8、f32、RGBA
- Pillow plugin
- 奇数尺寸
- GPU preset timing

运行：

```powershell
Set-Location O:\Project\Media\nvavif-py\uvtest
.venv\Scripts\python.exe .\test_nvavif.py
```

### 10.2 固化批量测试和性能测试

脚本：[`uvtest/benchmark.py`](O:/Project/Media/nvavif-py/uvtest/benchmark.py)

该脚本会：

- 遍历 `test_imgs` 全部图片。
- 默认跳过宽或高超过 `8192` 的图片。
- 测量 encode 时间和 MP/s。
- 写入临时 AVIF 并验证 Pillow 是否能打开。
- 调用 `decode_file()` 验证解码 shape 和 alpha 通道数。
- 统计 encode/decode 的均值、中位数和 P95。
- 对固定的 `1024x1024` NumPy 图分别运行 GPU/CPU 对比。
- 写入逐图 JSON 报告。

运行：

```powershell
Set-Location O:\Project\Media\nvavif-py\uvtest
.venv\Scripts\python.exe .\benchmark.py
```

报告：[`uvtest/benchmark_results.json`](O:/Project/Media/nvavif-py/uvtest/benchmark_results.json)

如需修改尺寸阈值或 CPU/GPU 对比次数：

```powershell
.venv\Scripts\python.exe .\benchmark.py --skip-max-dimension 8192 --device-repeats 5
```

### 10.3 导出可查看的完整测试集

脚本：[`uvtest/export_test_set.py`](O:/Project/Media/nvavif-py/uvtest/export_test_set.py)

该脚本会将所有通过尺寸筛选的测试图片持久化为 AVIF，并生成原图/AVIF 对照页面、`manifest.json` 和逐图验证信息：

```powershell
.venv\Scripts\python.exe .\export_test_set.py
```

输出目录：[`uvtest/out/batch_gallery`](O:/Project/Media/nvavif-py/uvtest/out/batch_gallery)

直接打开 [`index.html`](O:/Project/Media/nvavif-py/uvtest/out/batch_gallery/index.html) 即可查看 75 张原图和压缩图的并排对照。4 张超过 `8192` 宽或高的图片会显示在页面底部的 skipped 列表中，不会被静默遗漏。

## 11. 最终测试数据

### 11.1 功能结果

```text
总文件数：79
实际测试：75
跳过超限：4
失败：0
透明图片：21
```

75 张正常尺寸图片均通过：

- encode
- Pillow 打开
- `decode_file()`
- RGB/RGBA 通道数检查

### 11.2 批量性能

75 张图片总计约 `733.92 MP`：

```text
编码总耗时：116.36 秒
解码总耗时：30.21 秒
混合编码吞吐：6.31 MP/s
单图编码中位数：346 ms
P95 编码耗时：6.95 秒
最大单图编码耗时：16.88 秒
```

按图片类型拆分：

| 类型 | 数量 | 编码吞吐 | 平均编码 | 中位数 | P95 |
|---|---:|---:|---:|---:|---:|
| 不透明 | 54 | 34.51 MP/s | 286 ms | 159 ms | 686 ms |
| 透明 | 21 | 1.99 MP/s | 4.81 s | 4.28 s | 12.69 s |

### 11.3 GPU/CPU 固定图对比

输入为同一个 `1024x1024` NumPy uint8 RGB 数组，重复 5 次并排除第一次初始化：

| 路径 | 平均编码 | 中位数 | 吞吐 |
|---|---:|---:|---:|
| GPU NVENC | 98.8 ms | 100.0 ms | 10.6 MP/s |
| CPU rav1e | 6672.7 ms | 6679.6 ms | 0.157 MP/s |

该样本上 GPU 约为 CPU 的 `67.6x`。透明图的整体速度不能直接使用这个倍数估算，因为 alpha 流仍然由 CPU rav1e 编码。

## 12. NVENC 尺寸限制

当前 RTX 4070 + FFmpeg 8.1 运行时实测：

```text
8192 x 256  -> GPU 成功
8194 x 256  -> GPU 失败
256 x 8192  -> GPU 成功
256 x 8194  -> GPU 失败
```

也就是宽度和高度各自不能超过 `8192`。限制不是总像素数：

- `9504x6336` 只有约 `44.8 MP`，但宽度超过限制，仍然失败。
- `8192x8192` 约 `67.1 MP`，在轴限制内。
- `11648x8736` 约 `101.8 MP`，两个轴都超过限制。

这是 NVENC 硬件限制，不是 AVIF 容器格式的总像素限制。当前库不会自动缩放原图；`auto` 会尝试 GPU，失败后保持原始尺寸改用 CPU。

## 13. 后续工作建议

当前优先级最高的性能问题是 alpha：

1. 研究 NVENC 是否能在目标 GPU/FFmpeg 组合中安全提供 Gray/YUV400 输入。
2. 如果不能，继续优化 rav1e alpha 路径或减少 alpha 编码的 CPU 成本。
3. 透明大图保持颜色 GPU、alpha CPU 并行，不要为了追求“全 GPU”而重新使用不符合 AVIF 规范的 NV12 alpha。
4. 在生产编码前增加 NVENC 尺寸 capability 检查，超过 `8192` 时直接选择 CPU，避免先触发一次 NVENC 错误。
5. 对 YUV444 显式 GPU 增加按运行时 capability 的测试和清晰错误信息。

暂时保留项目目录中的 `msys64`、FFmpeg 源码、dav1d 源码和 `ffmpeg-out`，后续修改 Rust 或重新打包 wheel 时可以直接复用。
