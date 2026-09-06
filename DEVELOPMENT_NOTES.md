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

**项目目的（2026-09-05 明确）**：本项目在原 nvavif_py 库基础上的使用目标是**调用 NVIDIA GPU 加速海量图片的批量压缩**——图库是混合内容，普通图和透明 PNG 都有——要求输出体积小、画质保真、RGBA 透明通道不丢失，且透明图路径不能因为 CPU alpha 编码而拖垮整体吞吐。后续所有优化决策以"压缩率、画质、alpha 正确性、编码速度"四个指标为准。

## 2. 最终结论

当前版本对正常尺寸图片是可用的：

- 普通 RGB/JPEG/PNG 默认路径可以使用 NVENC GPU 编码。
- 没有可用 NVENC 时，`device="auto"` 会切换到 rav1e CPU 编码。
- alpha 已改为符合 AVIF 规范的独立 YUV400/monochrome AV1 辅助流。
- `decode_file()` 已能读取辅助 alpha 流并返回 RGBA NumPy 数组。
- Pillow 与浏览器均已验证透明 AVIF 的实际解码合成正确。注意验证范围（2026-09-05 补充实测）：Windows 看图软件对 AVIF/HEIC 的 alpha 一律不合成，透明区显示为黑色——透明 AVIF、标准 libheif alpha HEIC、双 item 全 GPU HEIC 全部如此；Photoshop 不支持这些格式。透明图的可靠查看环境是浏览器和 Pillow/libheif 生态，看图软件黑底是查看器限制，不是文件缺陷。
- 8/10-bit、YUV420/YUV444、f32 输入、Pillow 插件、奇数尺寸等功能测试通过。

仍然存在的已知限制：

- 当前 NVENC AV1 路径的实测最大宽度和高度都是 `8192`。超过任一轴时，GPU 不能编码原始尺寸。
- 超过尺寸限制的图片目前在 `auto` 模式下会回落到 CPU，测试脚本暂时直接跳过它们。
- alpha 的 YUV400 编码目前使用 rav1e CPU，因为当前 `av1_nvenc` 路径没有安全的 monochrome/YUV400 输入接口。2026-09-05 已把 alpha 固定为 rav1e 高速档（speed 9，与颜色 preset 解耦，见 11.4）：10 张透明测试图总耗时 93.7s → 9.8s（约 9.6×，0.65 → 6.25 MP/s），代价是文件增大 10~25%，alpha MAE 仍在 0.1~0.7/255 量级。透明图剩余瓶颈仍是 CPU alpha，但已接近可投产水平。
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

使用 NV12 编码 alpha、然后只修改容器元数据是不符合格式的，虽然某些解码器可能勉强读取，但也可能把整张图片当成透明。这是之前 alpha 解码错误的根本原因。

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
| 解码器显示透明图全透明 | alpha item 实际编码成 NV12/YUV420，但容器标记为 monochrome/YUV400 | alpha 改用 rav1e `Cs400`，颜色与 alpha 并行编码 | Pillow、Chrome、实际 RGBA 像素均验证通过 |
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
├─ source\       Python 包源码（source\nvavif_py\，maturin python-source 指向这里）
├─ uvtest\       测试脚本和性能报告（不单独建环境）
├─ dist-local\   maturin 构建产物（未修复 wheel，delvewheel 修复的输入）
├─ dist\repaired-current\   delvewheel 修复后的 wheel（打包 FFmpeg DLL，venv 只装这个）
├─ setup_env.py  环境一键固化脚本（幂等，见 §5.1）
└─ test_imgs\    样本图片
```

Rust 是例外：本次通过 rustup 安装后使用默认用户目录：

```text
C:\Users\ricar\.cargo\
C:\Users\ricar\.rustup\
```

Rust 本身不在项目目录，但 Cargo registry、MSYS2、FFmpeg、dav1d 和生成的 FFmpeg 产物均可复用，不需要每次重装。

### 5.1 环境一键固化（2026-09-05）

**任何环境异常，第一反应就是重跑这条命令，不要手动修：**

```bash
uv run python setup_env.py
```

它幂等完成四步：`uv sync`（pillow/numpy + dev 组：maturin、delvewheel、psutil、pynvml）→ 确保存在当前解释器对应的修复版 wheel（缺失则自动从 `dist-local\` delvewheel 修复到 `dist\repaired-current\`）→ 强制重装该 wheel 到根 `.venv` → 冒烟验证（根目录直接 import、GPU 探测、256×256 编解码回环）。全部通过打印 `ENV OK`。

配套的防复发配置（都在 `pyproject.toml`）：

- `[tool.uv] package = false`：uv 永不安装/卸载 nvavif_py 自身。此前 `uv run` 隐式 sync 会把 venv 里的 wheel 换成坏的 editable 安装（根目录源码包无 `.pyd`），是反复环境损坏的根源。
- `[dependency-groups] dev`：psutil/pynvml/maturin/delvewheel 进 lock，uv 精确 sync 不再误删。
- `python-source = "source"`：Python 包挪到 `source\nvavif_py\`，仓库根目录不再存在叫 `nvavif_py` 的目录——只要根目录进了 `sys.path`（`python -c`、pytest、IDE）就遮蔽 wheel 的问题从根上消除（§9 两次踩坑的根治）。
- 构建流程：`build.bat`（maturin build → `dist-local\`）→ `setup_env.py` 自动 delvewheel 修复 → venv。venv 里只应存在 `dist\repaired-current\` 的修复版 wheel。

## 6. 从 PyPI 验证安装

这是验证已发布 wheel 是否能正常工作的最短路径。**所有测试脚本统一使用仓库根目录的 `.venv`（uv 环境）；`uvtest` 只是脚本目录，不单独建环境**（2026-09-05 约定：曾因 uvtest 独立 venv 导致新旧 wheel 混用、基准结果失真，已删除）：

```powershell
Set-Location O:\Project\Media\nvavif-py
uv run python -c "import nvavif_py; print(nvavif_py.is_supported())"
```

验证脚本：

```powershell
uv run python uvtest\test_nvavif.py
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
| Python 测试 | 从仓库根目录用 `python -c` 启动时，根目录源码包（无 `.pyd`）shadow 了 venv 安装的 wheel | 统一根目录 `.venv`（见 §6），并按 §10 用 `uv run python uvtest\xxx.py` 运行——脚本方式下 `sys.path[0]` 是 `uvtest`，导入的是 venv 里的 wheel（2026-09-05 起不再单独从 uvtest 启动）。**2026-09-05 根治**：Python 包移至 `source\nvavif_py\`（`python-source`），根目录不再有同名包目录，遮蔽从根上不可能 |
| Python 测试（复发） | 根目录源码包内残留旧构建的 `_nvavif_py.cp313-win_amd64.pyd`（2026-09-05 二次踩坑：`python -c` 从根目录导入时旧 `.pyd` 反而"可用"，掩盖了 venv 新 wheel，I1 改动一度"无效"） | 已删除该构建残留物；约定：`source\nvavif_py\` 里**永远不要**留 `.pyd` 构建产物。配套根治见 §5.1（`package = false` + dev 组 + `setup_env.py` 一键固化） |
| 全量样本测试 | 超大图进入 rav1e 后耗时很长 | 本轮测试跳过宽或高大于 `8192` 的图片 |

## 10. 测试脚本与运行方法

### 10.0 命令行约定（2026-09-06）

- 工作目录在会话内持久：进入仓库根一次之后，后续命令**不要再重复 `cd` 到仓库根**，直接写相对路径（或确需跨目录时用绝对路径）。每个命令前缀 `cd <repo> &&` 属于历史噪音，新命令不应模仿。
- 临时测试一律建在 `uvtest/out/` 下的临时目录（如 `uvtest/out/_t_xxx`），用完即删；禁止把 `test_imgs/` 当可写目录。

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
uv run python uvtest\test_nvavif.py
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
uv run python uvtest\benchmark.py
```

报告：[`uvtest/benchmark_results.json`](O:/Project/Media/nvavif-py/uvtest/benchmark_results.json)

如需修改尺寸阈值或 CPU/GPU 对比次数：

```powershell
uv run python uvtest\benchmark.py --skip-max-dimension 8192 --device-repeats 5
```

### 10.2.1 一条命令批量压缩

脚本：[`uvtest/compress_dir.py`](O:/Project/Media/nvavif-py/uvtest/compress_dir.py)

生产用途的直接入口：把一个文件夹里的全部图片压成 AVIF，透明 PNG 自动保留 alpha（GPU 颜色 + 快速 CPU alpha），**进程池并行编码**（每 worker 一个 NVENC 会话，`--workers` 默认 `min(8, 核数/2)`，NVENC 并发会话上限是硬顶），结束给汇总，失败的图继续跑完再报告。

```powershell
# 默认：test_imgs -> uvtest\out\compressed，cq=20，device=auto，8 workers
# 默认策略：JPEG 质量基线过滤（估质量 <85 保留源文件）+ 尺寸兜底（压完不小则保留源）
uv run python uvtest\compress_dir.py
# web 档质量 / 指定目录 / SSIM 目标质量 / 限并行度 / 关报表
uv run python uvtest\compress_dir.py --cq 26
uv run python uvtest\compress_dir.py --src 某目录 --dst 某目录
uv run python uvtest\compress_dir.py --auto-quality 80
uv run python uvtest\compress_dir.py --workers 4
uv run python uvtest\compress_dir.py --min-jpeg-quality 0 --no-keep-smaller   # 全部强压
uv run python uvtest\compress_dir.py --report none
```

**过滤策略（2026-09-05）**：目标是"效率最好、输出永不比源大"，分两层：

1. **事前过滤** `--min-jpeg-quality 85`：从 JPEG 量化表零成本估计源质量（IJG 逆映射，不解码像素）。低于基线的源 JPEG 已经比 cq 目标压得更狠，再编只会变大或白费时间，直接保留源文件。实测 23 张低质量 JPEG 全部零成本跳过，其中 22 张正是上一轮"压完变大"的图（96% 精确率，1 张误杀，阈值可调）。
2. **事后兜底** `--keep-smaller`：编码后若 AVIF 不小于源文件，不落盘、保留源（兜住事前漏网的高质量但难压的图，实测拦下 1 张）。

全量实测（79 张 / 1099.5 MP，默认 cq=20）：

| 版本 | 耗时 | 吞吐 | 存储 |
|---|---:|---:|---|
| 串行初版 | 228.8 s | 4.80 MP/s | 406.4 → 132.7 MB（3.06×） |
| 串行 + 资源报表 | 205.5 s | 5.35 MP/s | 132.7 MB（NVENC 占用 0.1%、GPU 9% → 催生方案 H/I1） |
| 并行 + I1（8 workers） | 45.0 s | 24.4 MP/s | 130.8 MB（3.11×） |
| **并行 + I1 + 过滤策略** | **25.7 s** | 38.5 MP/s（编码部分） | **124.2 MB（3.27×，省 69%），无任何输出比源大** |

不透明图冒烟 8 张达 99 MP/s。超大图（>8192）单张从 49 s 降到 16 s（方案 I1）；多张超大图并行时因 rav1e 抢核单张回升到 ~23 s，但总墙钟仍大幅受益。

每次运行默认在输出目录生成 `compress_report.json`：逐图（尺寸/模式/是否alpha/源大小/输出大小/压缩率/bpp/耗时/MP/s）+ 全程资源采样（CPU%/内存，含全部子进程；GPU%/NVENC 编码器占用%/显存）。后续性能优化以此为基线。

环境说明（2026-09-05 更新）：`uv run` 已可**不带 `--no-sync`** 直接使用——构建所需的 FFmpeg/pkg-config/LIBCLANG/NASM 变量固化在 `.cargo/config.toml` 的 `[env]`，`msys64\mingw64\bin` 已追加进用户 PATH（bindgen 的 `libclang.dll` 依赖同目录的 `libLLVM-22.dll`，普通 shell 没有该目录时加载失败，这是此前 `--no-sync` 约定的原因）。注意两点：uv 触发的是源码 editable 构建，修改 `src/*.rs` 后下次 `uv run` 会自动增量重编译；裸 `python -c` 导入仍需先注册 `ffmpeg-out\bin` DLL 目录（uvtest 脚本均已内置），用 `build.bat` 构建发布 wheel 的流程不变。

### 10.3 导出可查看的完整测试集

脚本：[`uvtest/export_test_set.py`](O:/Project/Media/nvavif-py/uvtest/export_test_set.py)

该脚本会将所有通过尺寸筛选的测试图片持久化为 AVIF，并生成原图/AVIF 对照页面、`manifest.json` 和逐图验证信息：

```powershell
uv run python uvtest\export_test_set.py
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
透明相关图片：21（旧 benchmark 分类，其中 11 张实际为 alpha 全 255 的不透明 RGBA；真实透明图片为 10 张）
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
| 不透明（旧分类） | 54 | 34.51 MP/s | 286 ms | 159 ms | 686 ms |
| 透明相关（旧分类，含 11 张误判） | 21 | 1.99 MP/s | 4.81 s | 4.28 s | 12.69 s |

后续 alpha 误判修复后，实际分类为 65 张不透明和 10 张真透明。11 张误判样本的编码总耗时从 41.58 s 降至 3.75 s，吞吐从 3.36 MP/s 提升至 37.23 MP/s，平均 11.09 倍。

压缩率（默认 `cq=20`、P7、8bit YUV420，源文件为原始图片字节）：75 张源文件合计 380.7 MB → AVIF 合计 117.8 MB，约 **3.2 倍**（平均 1.26 bpp；混合集中含 JPEG 源，已压缩过所以整体倍率被拉低）。透明 PNG 子集（调参前基准，21 张 131.3 MB → 16.3 MB）约 8 倍；alpha 提速档（speed 9）后文件约 +16%，透明 10 张 / 61.25 MP 实测 29.6 MB → 6.1 MB，约 **4.9 倍**、省 80%。

### 11.3 GPU/CPU 固定图对比

输入为同一个 `1024x1024` NumPy uint8 RGB 数组，重复 5 次并排除第一次初始化：

| 路径 | 平均编码 | 中位数 | 吞吐 |
|---|---:|---:|---:|
| GPU NVENC | 98.8 ms | 100.0 ms | 10.6 MP/s |
| CPU rav1e | 6672.7 ms | 6679.6 ms | 0.157 MP/s |

该样本上 GPU 约为 CPU 的 `67.6x`。透明图的整体速度不能直接使用这个倍数估算，因为 alpha 流仍然由 CPU rav1e 编码。

### 11.4 alpha 速度调参（2026-09-05）

`src/lib.rs` 新增 `ALPHA_RAV1E_PRESET`（=2，对应 rav1e speed 9），alpha 编码不再继承颜色 preset（默认 6 → speed 5）。同 10 张透明图（61.2 MP）、`encode_file` 默认参数 A/B：

| 配置 | 总耗时 | 吞吐 | 12MP 单图 | 12MP 文件大小 | alpha MAE（正常图） |
|---|---|---|---|---|---|
| 基线 speed 5 | 93.67 s | 0.65 MP/s | 11.3~26.3 s | 975 KB | 0.005~0.09 |
| speed 8 | 11.43 s | 5.36 MP/s | 1.6~2.8 s | 1,041 KB | 0.07~0.17 |
| speed 9（采用） | 5.95~9.8 s | 6.25~10.3 MP/s | 0.85~1.36 s | 1,143 KB | 0.035~0.14 |

speed 9 比 speed 8 再快约一倍，文件大约 10%，保真度相当，选为默认。alpha 量化器逻辑不变（`a_cq = cq - 4`）。

同时发现一个独立 bug：`0-16 07-57-11.png` 和 `13 07-56-48-9236.png` 两张图无论 Pillow 插件还是 `decode_file()` 解码出的 alpha 都与源图不符（MAE 75~153，max 255），且与 alpha 编码速度设置无关（基线和调参后同样出现）。

**已修复（2026-09-06），根因是 rav1e 的 `asm` 特性**： rav1e 0.7.1 的 NASM SIMD 内核在本机工具链（nasm-rs 自编译 NASM）下对硬边、大面积平坦的单色内容（二值 alpha 掩码）产生**静默损坏的重建像素**——Cs420 颜色路径同一内容正常，Cs400 alpha 路径触发；CDEF 的 `cdef.rs:95` debug_assert 是症状不是病因（debug 构建直接 panic，release 构建 assert 被编译掉、静默输出坏码流）。用 ffmpeg 独立解码与 `decode_file()` 错误完全一致，证明容器里的码流本身就是坏的；这也解释了浏览器中透明显示异常（浏览器如实合成坏 alpha）而 Windows 看图软件"黑底正常"（不合成 alpha）。

修复：`Cargo.toml` 中 rav1e 去掉 `asm` feature（`features = ["threading"]`），CDEF/LRF 照常开启。验证（一次性诊断脚本 `uvtest/diag_alpha_bug.py`，已随 2026-09-06 清理删除，复现用例固化在 lib.rs 回归测试中；3 张图）：两张问题图 alpha MAE 153.17/141.47 → **0.05/0.03**，ffmpeg 参考解码一致；10 张透明图全集 61.2 MP 单进程 10.88 s（5.63 MP/s，比 asm 版慢约 10~25%，可接受），最差 alpha MAE 0.054。回归测试固化在 `src/lib.rs` 的 `alpha_dbg_tests`（输入 fixture 原由 `diag_alpha_bug.py` 生成，脚本删除后 fixture 缺失时跳过）。若未来要恢复 asm，需先换可信 NASM 构建并重跑本回归。

脚本：`uvtest/bench_alpha_tuning.py`；报告：`uvtest/out/alpha_tuning/*.json`。

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

超限回退的 CPU 颜色编码不再继承最慢档 preset（2026-09-05，方案 I1）：`src/lib.rs` 中 `OVERSIZE_RAV1E_PRESET = 4`（rav1e speed 7）仅对宽或高超过 `8192` 的图生效，普通图的瞬时 NVENC 失败仍保持原 preset。实测 101.7MP 单张 49.4 s → 16.2 s（3.04×），文件 -13.5%，SSIM -0.012（复验脚本 `uvtest/test_oversize_preset.py`）；画质敏感场景用 `auto_cq` 或显式 `device="cpu"`。

## 13. 原生 NVENC monochrome 探测

为了确认透明 alpha 是否可以绕过 FFmpeg，直接使用 NVENC 原生 API 编码，曾新增固定测试脚本（2026-09-05 实验结束后已随实验清理删除，本节保留记录）：

```bash
bash uvtest/probe_nvenc_monochrome.sh
```

脚本使用项目内的 MSYS2 GCC 编译 `uvtest/nvenc_monochrome_probe.c`，动态加载系统的 `nvcuda.dll` 和 `nvEncodeAPI64.dll`，然后依次执行：

1. 创建 CUDA context 和 NVENC session。
2. 确认 AV1 codec GUID 是否存在。
3. 查询 `NV_ENC_CAPS_SUPPORT_MONOCHROME`。
4. 输出 AV1 支持的输入 buffer 格式。
5. 如果 capability 报告支持，再尝试初始化 monochrome session 并编码一帧。

本机结果：

```text
CUDA_CONTEXT=ready
NVENC_SESSION=ready
AV1_GUID=present
AV1_MONOCHROME_CAPABILITY=0
AV1_INPUT_FORMATS=NV12,YV12,IYUV,YUV420_10BIT,...
AV1_MONOCHROME_RESULT=not_reported
```

结论是当前 RTX 4070 + NVIDIA 驱动支持 AV1 NVENC，但没有报告 AV1 monochrome 能力。方案 B 在本机不能安全实施；继续用 NV12 编码后伪装成 YUV400 仍然会有跨解码器兼容性问题。实验脚本已删除；如需在更换 GPU 或驱动后复测，可按本节步骤重写（编译 C probe、查询 `NV_ENC_CAPS_SUPPORT_MONOCHROME`），不改变库的默认编码路径。

## 14. 强制 CUDA alpha 实验与 Intel 源码核对

为了验证“即使绕过库，也把 alpha 灰度帧强行送给 GPU”是否可行，曾新增固定脚本（已随 2026-09-05 实验清理删除）：

```bash
uv run python uvtest/force_gpu_alpha_experiment.py
```

脚本通过 FFmpeg 的 `-init_hw_device cuda=nv:0` 强制初始化 CUDA，并用 `av1_nvenc` 测试同一张 `1024x1024` alpha 灰度图：

| 测试 | 结果 | 说明 |
|---|---|---|
| `gray -> av1_nvenc` | 失败 | FFmpeg 最终选择 `YUV444P`，NVENC 报 `YUV444P not supported` |
| 显式输出 `gray` | 失败 | FFmpeg 报 `gray` 不兼容并自动选择 `gbrp`，随后 NVENC 仍报 `YUV444P not supported` |
| `gray -> yuv420p -> av1_nvenc` | 成功，约 `230 ms` | `ffprobe` 识别为 AV1 `Main/yuv420p`，不是 monochrome |
| `gray -> yuv444p -> av1_nvenc` | 失败 | 当前 RTX 4070 NVENC 报 `YUV444P not supported` |

项目 API 的同轮参考结果约为：`device="cpu"` `2.2 s`，`device="gpu"` `2.4 s`。后者的颜色流使用 GPU，但 alpha 仍然由 rav1e CPU 编码；两个输出经 Pillow 都是 RGBA，alpha 范围为 `0..255`。这不是全量 benchmark，只是用于确认 alpha 编码路径和硬件输入格式的短实验。

同时从官方 Git 源码核对了 Intel 路径：

- oneVPL GPU runtime 当前源码的 AV1 encoder capability 配置只有 `NV12`、`P010`、`AYUV`、`Y410`，没有 `YUV400`/monochrome。Raptor Lake-S 的 PCI ID 被映射到 ADL-S runtime，不能因此获得额外的 monochrome 编码能力。
- Intel media-driver 的 AV1 8-bit/10-bit 编码输入表只有 `NV12`/`P010`；表中 `YUV400` 只出现在 JPEG 输出说明中，不是 AV1 encoder 输入。
- 参考源码：
  - https://github.com/oneapi-src/oneVPL-intel-gpu/blob/master/_studio/mfx_lib/encode_hw/av1/agnostic/base/av1ehw_base_query_impl_desc.cpp
  - https://github.com/intel/media-driver/blob/master/docs/media_features.md#supported-encoding-input-format-and-max-resolution

因此，i7-14700K 的 UHD 770 即使可以通过 Intel QSV/oneVPL 编码普通 AV1，也没有源码依据表明它能编码 AVIF 所需的 monochrome alpha。不能把它当作当前 RTX 4070 的替代全 GPU alpha 路径；换 Intel GPU 时仍应先用实际 capability/query 和透明度解码测试确认。

### 14.1 RTX 4070 的 HEVC alpha layer 实测

由于项目目标是高压缩率和保真度，不应只因为 AV1 alpha 无法全 GPU 就排除其它图片容器。NVIDIA NVENC API 对 HEVC 提供独立的 alpha layer 能力：

- capability：`NV_ENC_CAPS_SUPPORT_ALPHA_LAYER_ENCODING`
- 配置项：`NV_ENC_CONFIG_HEVC::enableAlphaLayerEncoding`
- 输入格式：NV12、ARGB、ABGR 等；本次使用 `ARGB`
- 输出锁定信息：`NV_ENC_LOCK_BITSTREAM::alphaLayerSizeInBytes`

探针脚本（`probe_nvenc_monochrome.sh` + `nvenc_monochrome_probe.c`，已删除）通过环境变量把实际 ARGB 图像交给 HEVC encoder：

```text
NVENC_MAX_API_VERSION=13.1
HEVC_GUID=present
HEVC_ALPHA_CAPABILITY=1
HEVC_ALPHA_INPUT_FORMAT=ARGB
HEVC_ALPHA_ENCODE=NV_ENC_SUCCESS (0) total_bytes=128131 alpha_bytes=11890
```

对输出码流解析 NAL layer ID 的结果为 5 个基础层单元和 3 个 alpha 层单元；alpha 层的 IDR NAL 存在，大小为 `11886` 字节。这个结果证明当前 RTX 4070 可以由 GPU 同时生成 HEVC 基础层和 alpha 层，不是把灰度内容伪装成普通色彩视频。

结果文件（2026-09-05 实验清理时已删除，结论保留在本节）：

- `uvtest/out/gpu_alpha_experiment/hevc_alpha_image_result.json`
- `uvtest/out/gpu_alpha_experiment/nvenc_hevc_alpha_probe.h265`
- `uvtest/inspect_hevc_layers.py`
- `uvtest/test_hevc_alpha_image.py`

本机 `libheif 1.23.1` 的 CPU 基线命令也能将相同 RGBA 图像写成 HEIC，并生成主图加 alpha auxiliary image。但当前 libheif 源码明确将 layered HEVC item type `lhv1` 标记为尚未支持；而 NVIDIA HEVC alpha 输出正是 layered HEVC 结构。普通 `hevc_nvenc` 只暴露 RGBA 输入，不会自动打开 alpha layer 配置。因此，当前结果是“GPU HEVC alpha 编码可行，现有 libheif 不能直接封装/解码该 layered HEVC”，还不能把它直接接入生产转换路径。

下一步如果继续做 HEIF，需要实现或引入支持 `lhv1` 的封装/解码链路，并使用文件大小、SSIM/其它保真指标和编码时间，与当前 AVIF 结果做同源图片对比。单纯把 `.h265` 改名为 `.heic` 不构成有效的 HEIF 文件。layered 路线保留为历史结论；绕开 `lhv1` 的单层双 item 方案见 14.2，已验证容器层可行。

### 14.2 双单层 NVENC HEVC + 标准 HEIC 双 item 实验（2026-09-05）

14.1 的 layered 路线被容器生态卡死后，验证了第三条路：不使用 NVENC alpha layer，改用两条普通**单层** `hevc_nvenc` 流：

- 颜色流：NV12。
- alpha 流：FFmpeg `alphaextract` 把 alpha 平面提取为 luma，色度填中性 128。注意 `format=gray` 是取 RGB 亮度而非 alpha，会静默丢掉透明信息。
- 两条流都必须是全范围：`scale=out_range=pc` + `-color_range pc`，最终码流为 `yuvj420p(pc)`。有限范围会让 mc=0 读取端的 alpha 整体偏移约 16~27（实测平均 19.5）。

封装为标准 HEIC 双 item：primary `hvc1` + alpha `hvc1`，`iref auxl` 指向主图，alpha item 带 `auxC urn:mpeg:hevc:2015:auxid:1` 和 `colr nclx matrix_coefficients=0`（读取端据此把 4:2:0 码流当作 luma-only）。全程没有 `lhv1`。

封装实现踩过的坑（脚本已修正）：

- `ipma` 格式为 `entry_count u32` + 每项 `item_ID u16 + association_count u8 + 关联表`，多写一个 u16 会让全部属性关联错乱、文件被判无效。
- `hvcC` 的 chromaFormat 字节按 libheif 的惯例是 `chroma_format | 0xFC`，4:2:0 写 `0xFD`；写成 `0xFC` 会被读成 monochrome。
- 每个 item 的 `hvcC` 只放自己流里 layer 0 的 VPS/SPS/PPS；item 数据只放 slice NAL。
- `ftyp` major brand 用 `heic`，与 libheif 输出一致。

验证结果（500x500 真透明 PNG，QP26）：

| 读取方 | 结果 |
|---|---|
| FFmpeg（mov demuxer + hevc 解码） | 两个 `hvc1` 流均解析解码；alpha luma 平均误差 `0.015`（max 6）；颜色 MAE 2.2 |
| libheif（pillow-heif） | 返回 RGBA；alpha 平均误差 `0.015`（max 6）；颜色 MAE 2.1。mc=0 + 4:2:0 编码的 alpha item 被正确当作 luma-only |
| Windows WIC Microsoft HEIF Decoder | 成功打开并解码（FRAMES=1，500x500），与 libheif 参考件行为一致 |

WIC 注意事项：WPF `CopyPixels` 查询对 alpha HEIC 一律返回 `Bgr32` 全不透明——libheif 参考件和标准 pillow-heif 编码的 alpha HEIC 也一样。所以该测试只验证"能打开"，不验证透明合成。

性能（500x500，含进程启动开销）：颜色 0.30s + alpha 0.26s ≈ `0.56s`，双流全部 GPU。同图当前 AVIF 路径（GPU 颜色 + CPU rav1e alpha）为 `1.07s`，输出 78.3 KB vs 双 item HEIC 34.6 KB。两者 QP/CQ 设置不同，文件大小不能当作画质等价对比；alpha 流只占 2.5 KB。

风险与未决事项：MIAF 严格定义要求 alpha 辅助图为 monochrome 编码（libheif 参考件是 HEVC Rext gray）；本方案是“语义 monochrome（mc=0）、编码 4:2:0”。libheif、FFmpeg、Windows WIC 都接受，但商业解码器（Apple、具体图片软件）的兼容性需要人工打开目标软件确认。

查看环境实测结论（2026-09-05）：Windows 看图软件对 AVIF/HEIC 的 alpha **一律不合成**——本方案双 item HEIC、标准 libheif 编码的 `pillowheif_reference.heic`、以及 nvavif 输出的透明 AVIF，在看图软件里全部显示黑色背景（与 WIC 查询一律返回 `Bgr32` 的实测一致）；Photoshop 不支持这些格式；浏览器显示透明 AVIF 正确。黑底是查看器解码路径的限制，不是文件缺陷。

由此确定两条路线的实际可用范围：透明 AVIF（GPU 颜色 + CPU alpha）在浏览器/libheif 生态透明正确，但 alpha CPU 编码太慢（平均约 4.8s/张），作为生产路径必须先做 alpha 提速优化；全 GPU 双 item HEIC 编码快，但看图软件黑底、浏览器又不支持 HEIC，在日常查看环境中没有透明可见的场景，不能作为通用透明图输出，仅适用于 libheif 生态（服务端/自研管线）。

实验文件（脚本与输出已于 2026-09-05 清理，结论保留在本节）：

- `uvtest/test_hevc_dual_heic.py`（端到端：编码、封装、三方验证、计时）
- `uvtest/build_hevc_alpha_heic.py`（双 item BMFF 封装器）
- `uvtest/inspect_heic_boxes.py`（HEIF box 树检查）
- `uvtest/test_windows_heif_decoder.py`（Windows WIC 解码验证）
- `uvtest/out/gpu_alpha_experiment/nvenc_hevc_dual_hvc1.heic`
- `uvtest/out/gpu_alpha_experiment/dual_hevc_heic_result.json`

如需重建该路线，可按本节的封装规范（ipma/hvcC/auxC/colr 关键字节）与踩坑记录重写。

## 15. 后续工作建议

当前优先级最高的性能问题是 alpha：

1. 对 capability probe 报告支持的硬件，再研究 NVENC 原生 monochrome 会话和 AVIF alpha 流的完整封装。
2. 透明图生产提速：rav1e alpha 速度参数/线程调优优先（文件仍是标准 AVIF）；其次评估方案 A alpha 下采样的兼容性。14.2 的 HEIC 双 item 方案经看图软件实测透明不可见，保留为 libheif 生态备选，不作为通用输出后端。
3. 当前机器继续优化 rav1e alpha 路径或减少 alpha 编码的 CPU 成本。
4. 透明大图保持颜色 GPU、alpha CPU 并行，不要为了追求“全 GPU”而重新使用不符合 AVIF 规范的 NV12 alpha。
5. 在生产编码前增加 NVENC 尺寸 capability 检查，超过 `8192` 时直接选择 CPU，避免先触发一次 NVENC 错误。
6. 对 YUV444 显式 GPU 增加按运行时 capability 的测试和清晰错误信息。

暂时保留项目目录中的 `msys64`、FFmpeg 源码、dav1d 源码和 `ffmpeg-out`，后续修改 Rust 或重新打包 wheel 时可以直接复用。
