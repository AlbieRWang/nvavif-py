# nvavif_py 开发、安装与运维记录

## 1. 文档信息

- 更新：2026-10-17
- 平台：Windows x64；测试 GPU：NVIDIA RTX 4070；驱动：610.88
- Python 3.13；Rust 1.97.1；FFmpeg 8.1
- 主要 Rust 依赖：PyO3 0.27、ffmpeg-next 8.1、avif-serialize 0.8.8、rav1e 0.7.1、dav1d

本文是**当前实现与运维手册**，只保留仍然成立的事实、决策及其理由。详细的历史测量过程已从正文移除，
需要追溯时看 git 历史。配套文档的分工：

| 文档 | 角色 |
|---|---|
| `PARITY_MATRIX.md` | 批处理行为的**唯一验收规格**（路由、元数据、文件系统、报表、CLI） |
| `uvtest/README.md` | 测试/基准脚本索引与测量方法 |
| `OPTIMIZATION_PROPOSALS.md` | 优化方案台账（已落地 / 已否决 / 条件触发） |
| `REVIEW_FINDINGS_2026-09-13.md` | 已闭环的审查台账（A/B/C/D/E 条目与裁决理由） |

**项目目的（2026-09-05 明确）**：用 NVIDIA GPU 加速海量图片的批量压缩。图库是混合内容，
普通图与透明 PNG 都有；要求输出体积小、画质保真、RGBA 透明不丢失，且透明路径不被 CPU alpha
编码拖垮。评价指标为压缩率、画质、alpha 正确性、编码速度。

---

## 2. 当前结论

- **批量入口**是 `uvtest/compress_dir.py`。默认策略见 `PARITY_MATRIX.md` §1，核心是：
  ≤2 MP 不透明图走 WebP Q90；2–8 MP 走 AVIF CQ36；>8 MP 走 AVIF CQ30；真透明走 WebP；
  超过 8192 走 WebP（>16383 先缩放到 16383，否则跳过并保留源）；JPEG 估计质量 <85 保留源；
  keep-smaller / copy-skipped 默认开启。
- **库 API 可用**：普通 RGB/JPEG/PNG 默认走 NVENC；无 NVENC 时 `device="auto"` 回落 rav1e；
  透明 AVIF 的颜色走 GPU、alpha 走 rav1e 的独立 YUV400 流；`decode_file()` 用 dav1d 解码。
- **8/10-bit、YUV420/YUV444、f32、Pillow 插件、奇数尺寸**均有回归覆盖；YUV444 的显式 GPU
  路径取决于运行时 capability（本机不可用）。
- **已知边界**：NVENC 每轴 8192、最小尺寸 130×66；WebP 上限 16383；alpha 无 GPU 编码；
  AVIF 容器不能带 ICC/XMP；Windows 看图软件不合成 AVIF/HEIC 的 alpha。详见 §12、§14
  和 `PARITY_MATRIX.md` §8。
- **输出元数据**：AVIF 透传源 EXIF（去 Orientation）；WebP/PNG 透传 EXIF + ICC + XMP；
  批量输出保持源的 mtime 与创建时间。详见 §20。

---

## 3. 项目架构

### 3.1 编码路径

Python API 由 PyO3 暴露，核心在 `src/lib.rs`：

1. `encode_file()` 接受路径、Pillow 对象、NumPy 数组或原始 bytes。
2. 包装层做模式归一、ICC→sRGB（恒等时跳过）、方向转正、奇数尺寸裁 1px、EXIF 归一化。
3. Rust 侧用 rayon 并行转换为 NV12/P010 或 YUV444P/YUV444P16LE。
4. 颜色流优先交给 FFmpeg 的 `av1_nvenc`；不可用或当前输入不支持时，`auto` 切换 rav1e。
5. `avif-serialize` 将颜色流、可选 alpha 流、CICP 和 EXIF 封装为 AVIF。

### 3.2 alpha 路径

AVIF alpha 是独立的 monochrome/YUV400 AV1 item，不是普通 NV12/YUV420 流：

- 颜色流：NVENC（GPU）。
- alpha 流：rav1e `ChromaSampling::Cs400`（CPU），固定 speed 9。
- 两条流并行编码；容器写入正确的 alpha 辅助流元数据。

用 NV12 编码 alpha 再改容器元数据不符合规范：部分解码器会正确裁剪，部分会把整张图当透明。

### 3.3 解码路径

`decode_file()` 用 FFmpeg/dav1d 解码：

- 第一个视频流作为颜色流，转 RGB。
- 其余视频流作为候选 alpha 流，读取 luma plane。
- 合并为 `H×W×4`；无 alpha 时返回 `H×W×3`。
- 支持 GRAY8/GRAY10（广播为 RGB）与 YUVA422P10LE；忽略 `color_range`（自家产物恒为 full-range）。

---

## 4. 已修复问题台账

只列仍会影响理解的根因；历史复现细节见 git 记录。

| 问题 | 根因 | 修复 |
|---|---|---|
| YUV444 GPU 编码失败 | FFmpeg profile 写成字符串 `high` | 改为整数 `profile="1"` 并同步 chroma 元数据 |
| 透明图解码全透明 | alpha item 实际是 NV12，容器却标成 YUV400 | alpha 改用 rav1e `Cs400`，与颜色并行 |
| `decode_file()` 只返回 3 通道 | 未读取 AVIF 辅助 alpha 流 | 枚举第二个流、提取 luma、合并 RGBA |
| `is_supported()` 误报 | 只打开编码器，未真正送帧 | 编码一帧 256×256 NV12 并缓存结果 |
| auto 模式 GPU 运行时失败即中断 | 打开成功不等于能送当前尺寸 | `auto` 在颜色失败时回落 CPU；`gpu` 保留错误 |
| 超大图慢速 CPU 回退 | 继承最慢 preset | 仅 >8192 回退用 rav1e speed 7；批量默认改走 WebP |
| alpha 偶发静默损坏 | rav1e `asm` 的 SIMD 内核在 Cs400 硬边缘内容上出错 | 去掉 rav1e `asm` feature，CDEF/LRF 保留；回归固化在 `alpha_dbg_tests` |
| f32 输入被无条件 ACES 提亮 | 所有 float 都过 ACES | 仅 `max > 1.0` 时色调映射；LDR float 原样通过 |
| `cq=0` 静默回落 CPU | NVENC 拒绝 qp=0 | 统一 `qp.max(1)`，不再触发回落 |
| 灰度/错误 shape 数组 panic | 按 `len/px_count` 推 channels 未校验 | 包装层拒绝 `c ∉ {3,4}`，Rust 层校验尺寸/长度 |
| 畸形 EXIF 让整个 AVIF 打不开 | HEIF Exif item 载荷格式与 JPEG/WebP 不同 | 统一归一化；无法识别则丢弃；去 Orientation |
| 批处理每图多解码一次探测透明度 | header 已有 `has_alpha` 却未用 | 无 alpha 的源直接短路像素检查 |

---

## 5. Windows 本地安装布局

```text
O:\Project\Media\nvavif-py\
├─ msys64\                  MSYS2 / MinGW64 / NASM / clang（重新编译需要）
├─ FFmpeg\                  FFmpeg 8.1 源码（产物已在 ffmpeg-out）
├─ dav1d\                   dav1d 源码（产物已在 ffmpeg-out）
├─ nv-codec-headers\        NVENC headers（已编进产物）
├─ ffmpeg-out\              headers / import libs / DLL / dav1d 安装产物
├─ source\nvavif_py\        Python 包源码（maturin python-source）
├─ uvtest\                  测试脚本、报告、临时输出（不单独建环境）
├─ dist-local\              maturin 构建产物（未修复 wheel）
├─ dist\repaired-current\   delvewheel 修复后的 wheel（venv 只装这个）
├─ .venv\                   根目录唯一环境
├─ setup_env.py             环境一键固化（幂等）
└─ test_imgs\               样本图片
```

Rust 在用户目录（`~/.cargo`、`~/.rustup`），不在项目内。Cargo registry、MSYS2、FFmpeg、
dav1d 和 `ffmpeg-out` 可复用，不需要每次重装。

### 5.1 环境一键固化

任何环境异常，第一反应是重跑，不要手修：

```bash
uv run python setup_env.py
```

它幂等完成四步：`uv sync` → 确保存在当前解释器对应的修复版 wheel（缺失则自动从 `dist-local\`
delvewheel 修复到 `dist\repaired-current\`）→ 强制重装该 wheel 到根 `.venv` → 冒烟验证
（根目录 import、GPU 探测、256×256 编解码回环）。全部通过打印 `ENV OK`。

`pyproject.toml` 中的防复发配置：

- `[tool.uv] package = false`：uv 永不安装/卸载 nvavif_py 自身，避免把 venv 里的 wheel
  换成无 `.pyd` 的 editable 安装。
- `[dependency-groups] dev`：psutil/pynvml/maturin/delvewheel 进 lock，`uv sync` 不会误删。
- `python-source = "source"`：根目录不再有同名包，`sys.path` 遮蔽 wheel 的问题从根上消除。
- 构建流程：`build.bat`（maturin build → `dist-local\`）→ `setup_env.py` 自动 delvewheel 修复 → venv。

**新机器（干净 clone）**：`dist-local\`、`dist\`、`target\` 均不入库；目标机执行一次
`build.bat --out dist-local`（冷编译几分钟）再 `uv run python setup_env.py`，打印 `ENV OK` 即就绪。
工具链依赖仓库内的 `msys64\` 与 `ffmpeg-out\`（迁移时整目录拷贝）；FFmpeg/pkg-config/LIBCLANG/NASM
变量由 `build.bat` 导出。`.cargo/config.toml` 不入库（曾把本机路径带进 CI），需要直接 `uv run`
构建时先从 `.cargo/config.toml.example` 复制并改成本机路径；只跑 `build.bat` 不需要。

### 5.2 工具链清理

2026-10-17 快照（大小为主观近似）：

| 目录 | 大小 | 结论 |
|---|---:|---|
| `msys64/` | ~3.3 GB | 要重新编译就保留；确定不再改源码可删 |
| `ffmpeg-out/` | ~16 MB | 保留；Cargo 构建依赖 |
| `FFmpeg/`、`dav1d/`、`nv-codec-headers/` | 源码 | 可删；产物已在 `ffmpeg-out/` |
| `uvtest/` | ~370 MB | 保留脚本和 `out/_key_reports/`；`out/` 其余可删 |
| `test_imgs/` | 图片集 | 保留，作为回归基准 |
| `~/.cargo`、`~/.rustup` | 1–2 GB | 与项目解耦，独立决定 |

`.gitignore` 已排除 `msys64/`、`FFmpeg/`、`dav1d/`、`nv-codec-headers/`、`ffmpeg-out/`、`uvtest/`、
`uv.lock`、`test_imgs/`、`.cortexkit/` 和本机 `.cargo/config.toml`。删除上面这些目录不会影响
已安装的 wheel；只影响重新编译和重新验证的能力。

---

## 6. 从 PyPI 验证安装

所有测试统一使用仓库根 `.venv`；`uvtest` 只是脚本目录，不单独建环境（独立 venv 曾导致新旧
wheel 混用、基准失真）：

```powershell
uv run python -c "import nvavif_py; print(nvavif_py.is_supported())"
uv run python uvtest\test_nvavif.py
```

发布 wheel 由 CI 的 delvewheel/auditwheel 绑定 FFmpeg、dav1d 和 MinGW runtime，用户通常不需要
自己安装 FFmpeg 开发环境。

---

## 7. 从源码构建的依赖安装

### 7.1 Rust

```powershell
rustc --version
cargo --version
```

本次安装时网络曾受阻，使用了本机代理 `127.0.0.1:2801`。代理只注入下载子进程，不写入持久环境。

### 7.2 MSYS2

项目内 MSYS2 使用 `MINGW64` 环境（CI 也是 `msystem: MINGW64`，不能在纯 MSYS shell 中构建 FFmpeg）。
需要：`git`、`make`、`nasm`、`mingw-w64-x86_64-gcc`、`pkgconf`、`meson`、`ninja`、`clang`、`diffutils`。

- `nasm`：rav1e 和 dav1d 的汇编构建。
- `clang`：为 bindgen 提供 `libclang.dll`。
- `pkgconf`：让 FFmpeg 找到本地 dav1d。
- `meson`/`ninja`：构建 dav1d。

### 7.3 nv-codec-headers、dav1d 和 FFmpeg

构建顺序：安装 `nv-codec-headers` → Meson/Ninja 构建 dav1d 到 `ffmpeg-out` →
从 FFmpeg `release/8.1` 构建精简共享库。

FFmpeg 必须包含：`av1_nvenc`、`libdav1d` 解码器、AV1 parser、MOV/AVIF demuxer、file/pipe protocol。
CI 的 configure 选项位于 `.github/workflows/CI.yml`，与本机一致的核心是：

```bash
./configure \
  --prefix=$FFMPEG_OUT \
  --enable-shared --disable-static --disable-doc --disable-programs \
  --disable-everything \
  --enable-ffnvcodec --enable-libdav1d --enable-decoder=libdav1d \
  --enable-nvenc --enable-encoder=av1_nvenc --enable-parser=av1 \
  --enable-demuxer=mov --enable-protocol=file --enable-protocol=pipe \
  --extra-cflags="-I$FFMPEG_OUT/include" \
  --extra-ldflags="-L$FFMPEG_OUT/lib"
make -j$(nproc)
make install
```

`nv-codec-headers` 官方地址曾出现 TLS EOF，改用 GitHub 镜像即可；是下载链路问题，不是源码问题。

---

## 8. Cargo 和 maturin 本地构建

`build.rs` 只在存在 `FFMPEG_DIR` 时注册 FFmpeg 的 `include`、`lib`、`bin` 路径；没有该变量时
Cargo 能找到依赖但无法正确链接本地 FFmpeg。

本地构建需要的进程级变量（只作用于 Cargo/maturin 子进程，不写系统环境）：

```text
FFMPEG_DIR=<repo>\ffmpeg-out
FFMPEG_INCLUDE_DIR=<repo>\ffmpeg-out\include
FFMPEG_LIB_DIR=<repo>\ffmpeg-out\lib
PKG_CONFIG_PATH=<repo>\ffmpeg-out\lib\pkgconfig
LIBCLANG_PATH=<repo>\msys64\mingw64\bin
CLANG_PATH=<repo>\msys64\mingw64\bin\clang.exe
PATH += ~/.cargo/bin, ffmpeg-out\bin, msys64\usr\bin, msys64\mingw64\bin
```

```powershell
$repo = (Get-Location).Path
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

Windows FFmpeg 生成的 import library 名为 `avcodec.lib` 等，而 Cargo 探测需要 `libavcodec.lib`；
CI 在 maturin 前做了复制/重命名。未 repair 的本地 wheel 不含全部 DLL，测试时需把
`ffmpeg-out\bin` 和 `msys64\mingw64\bin` 加入进程 DLL 搜索路径（`uvtest/benchmark.py` 已用
`os.add_dll_directory()` 处理）。发布 wheel 由 CI 执行：

```powershell
delvewheel repair dist\*.whl --add-path "ffmpeg-out\bin;C:\msys64\mingw64\bin" -w dist\repaired
```

---

## 9. 编译过程中遇到的阻塞

| 阶段 | 问题 | 解决 |
|---|---|---|
| 初始构建 | 机器上没有 Rust toolchain | 按默认 rustup 位置安装 |
| FFmpeg 链接 | 系统 `ffmpeg.exe` 没有 headers/import libs | 在项目内重新构建 FFmpeg 开发产物 |
| FFmpeg configure | 纯 MSYS shell 不满足 CI 的 MINGW64 环境 | 使用 `MSYSTEM=MINGW64` |
| rav1e | 找不到 NASM | 把 `msys64\usr\bin` 加入 Cargo 子进程 PATH |
| bindgen | 找不到 `libclang.dll` | 安装 clang，设置 `LIBCLANG_PATH` |
| ffmpeg-sys-next | link probe 失败且无详细原因 | verbose Cargo 输出，补齐 FFmpeg lib/include/pkg-config |
| 本地 wheel 导入 | `.pyd` 能找到但 DLL 加载失败 | 临时加入 FFmpeg/MinGW DLL 目录；发布版由 delvewheel 解决 |
| 根目录导入 | 源码包遮蔽 venv wheel；残留 `.pyd` 掩盖新 wheel | 包移到 `source\nvavif_py\`，`package=false`；源码目录不留 `.pyd` |

---

## 10. 测试脚本与运行方法

完整脚本索引见 `uvtest/README.md`。本节只保留运行约定和常用入口。

### 10.0 命令行约定

- 工作目录在会话内持久：进入仓库根一次后，命令直接写相对路径。
- 临时测试建在 `uvtest/out/` 下，用完即删；禁止把 `test_imgs/` 当可写目录。
- 所有脚本用根 `.venv`：`uv run python uvtest\<script>.py`。

### 10.1 定向功能回归

`uvtest/test_nvavif.py`：普通 JPG、RGBA PNG、显式 CPU、8/10-bit × 420/444、auto-CQ、
NumPy u8/f32/RGBA、Pillow 插件、奇数尺寸、GPU preset timing。打印为主，无断言。

`uvtest/test_compress_dir_routing.py --fast`：带断言的路由回归，失败退出非 0。覆盖 JPEG 质量
估计、不透明/透明/oversize/keep-smaller 路由、alpha 平面行为、多轮续跑；使用微型图和编码替身，
不调用真实 AVIF 编码。

### 10.2 基准与批处理

`uvtest/benchmark.py`：遍历 `test_imgs`，测 encode/decode、写临时 AVIF 并验证、统计均值/中位数/P95，
输出 `benchmark_results.json`；默认跳过任一边 >8192 的源。

`uvtest/bench_opaque_formats.py`：走真实 `_encode_task` 路径比较 AVIF/WebP；详见 `uvtest/README.md`。

`uvtest/measure_ctx_overhead.py`：用 `NVAVIF_DEBUG_TIMING=1` 分解 Python 输入、RGB→YUV、context
新建/命中/淘汰、主机帧准备、send/receive；严格 GPU 子进程，预热不计入逐图统计。

### 10.2.1 一条命令批量压缩

`uvtest/compress_dir.py` 是生产入口。默认：`test_imgs` → `uvtest\out\compressed`，
≤2 MP 不透明图 WebP Q90，2–8 MP AVIF CQ36，>8 MP AVIF CQ30，真透明 WebP，
超过 8192 的源 WebP（>16383 先缩放到 16383，否则跳过并保留源），JPEG 估计质量 <85 保留源，
keep-smaller 与 copy-skipped 开启，workers = `min(8, 核数/2)`。

```powershell
uv run python uvtest\compress_dir.py
uv run python uvtest\compress_dir.py --src 某目录 --dst 某目录
uv run python uvtest\compress_dir.py --cq 26
uv run python uvtest\compress_dir.py --auto-quality 88
uv run python uvtest\compress_dir.py --workers 4
uv run python uvtest\compress_dir.py --min-jpeg-quality 0 --no-keep-smaller
uv run python uvtest\compress_dir.py --report none
```

输出目录默认生成 `compress_report.json`（逐图尺寸/模式/alpha/大小/压缩率/bpp/耗时/MP/s +
资源采样）和 `.stream.jsonl`。续跑、in-place、copy-skipped 等行为以 `PARITY_MATRIX.md` 为准。

### 10.3 导出可查看的测试集

`uvtest/export_test_set.py`：把通过尺寸筛选的测试图持久化为 AVIF，生成原图/AVIF 并排页面、
`manifest.json` 和逐图验证信息。输出目录 `uvtest/out/batch_gallery` 已删除，需要时重跑即可再生。

---

## 11. 性能基线

历史全量数字（cq=20、串行版本、旧路由）不再保留；当前默认路由的对比见 §21 与
`uvtest/README.md`。

### 11.1 语料与功能基线

`test_imgs` 共 79 个文件，其中 4 张任一边 >8192；格式对比用 53 张不透明源（533 MP）。
75 张正常尺寸图片均通过 encode、Pillow 打开、`decode_file()`、RGB/RGBA 通道检查。
透明集的可靠查看环境是浏览器和 Pillow/libheif；Windows 看图软件不合成 alpha。

### 11.2 批量性能与格式对比

53 张不透明源、8 worker、固定 CQ20（历史对比，当前默认见 §21）：AVIF 与 WebP 总墙钟与 Σ任务时间基本打平
（3.53–3.58 s vs 3.48–3.50 s），但逐图配对后分桶明显：<8 MP WebP 更快（0.24×），
≥8 MP AVIF 更快（1.36×）。命中 NVENC context 后交点下移到 1.75–2.39 MP，故默认阈值取 2 MP。
体积/画质（`compare_runs.py`，相对 WebP Q90）：CQ20 1.58× / LPIPS 0.0101；
CQ30 1.30–1.32×（8–32 MP）；CQ36 1.06–1.13×（2–16 MP）。CQ20/30/36 与 WebP Q90 不是对齐质量，
字节差不能单独当画质判定；最终以 LPIPS + 人工核验为准。

### 11.3 GPU/CPU 固定图对比

`1024×1024` NumPy uint8 RGB，5 次重复、排除首次初始化：GPU NVENC 98.8 ms（10.6 MP/s），
rav1e CPU 6672.7 ms（0.157 MP/s），约 **67.6×**；该样本 GPU 输出约大 39%（tune=hq）。
透明图不能直接用这个倍数估算，因为 alpha 仍走 CPU（批量默认已整体路由 WebP）。

### 11.4 alpha 速度调参与 rav1e asm 修复

`ALPHA_RAV1E_PRESET = 2`（rav1e speed 9），alpha 不继承颜色 preset（默认 P7 → speed 4/5）。
同 10 张透明图（61.2 MP）：基线 speed 5 约 93.7 s → speed 9 约 5.95–9.8 s（~10–15×），
文件增大 10–25%，alpha MAE 约 0.035–0.14/255。

alpha 曾出现两张图解码错误（MAE 75–153）。根因是 rav1e 0.7.1 的 NASM SIMD 内核在本机工具链下
对 Cs400 硬边内容产生静默损坏；`cdef.rs:95` 的 debug_assert 只是症状。修复方式是
`Cargo.toml` 去掉 rav1e `asm` feature（保留 `threading`），CDEF/LRF 照常。ffmpeg 独立解码与
`decode_file()` 错误一致，证明码流本身坏了。回归固化在 `src/lib.rs` 的 `alpha_dbg_tests`；
若未来恢复 asm，必须先换可信 NASM 构建并重跑该回归。

---

## 12. NVENC 尺寸限制

RTX 4070 + FFmpeg 8.1 实测：每轴上限 **8192**，不是总像素数。

```text
8192 x 256  -> 成功        8194 x 256  -> 失败
256 x 8192  -> 成功        256 x 8194  -> 失败
```

最小帧尺寸实测 **宽 ≥130、高 ≥66**（128×130 失败，130×66 成功，130×65 失败）。
库在 `auto` 下会把小图/超限图交给 CPU；批处理则在扫描阶段直接跳过小图（`skipped_small`），
超限图默认走 WebP，不再进入 rav1e。

超限回退的 CPU 颜色编码使用 `OVERSIZE_RAV1E_PRESET = 4`（rav1e speed 7，仅 >8192 生效）：
101.7 MP 单张 49.4 s → 16.2 s（3.04×），文件 -13.5%，SSIM -0.012。库本身不缩放；需要缩放时由
批处理的 `--oversize-max-edge` 显式完成。

---

## 13. 原生 NVENC monochrome 探测

本机（RTX 4070 + 当前驱动）结果：

```text
AV1_GUID=present
AV1_MONOCHROME_CAPABILITY=0
AV1_INPUT_FORMATS=NV12,YV12,IYUV,YUV420_10BIT,...
```

没有 AV1 monochrome 能力，方案 B 在本机不能安全实施。用 NV12 编码后伪装 YUV400 会有跨解码器
兼容性问题。换 GPU/驱动后只有在 `NV_ENC_CAPS_SUPPORT_MONOCHROME=1` 时重新评估。
详见 `OPTIMIZATION_PROPOSALS.md` 方案 B。

---

## 14. 强制 CUDA alpha 实验与 Intel 源码核对

### 14.1 RTX 4070 的 HEVC alpha layer

NVENC HEVC 提供 `NV_ENC_CAPS_SUPPORT_ALPHA_LAYER_ENCODING`。本机 probe：
`HEVC_ALPHA_CAPABILITY=1`，ARGB 输入编码成功，输出含基础层与 alpha 层（5 + 3 个 NAL）。
但 libheif 1.23.1 不支持 layered HEVC 的 `lhv1` item，现有容器生态接不上，保留为历史结论。

### 14.2 双单层 HEVC + 标准 HEIC 双 item

用两条普通单层 `hevc_nvenc`（颜色 NV12；alpha 用 `alphaextract` 为 luma，色度 128，全范围
`yuvj420p(pc)`），封装为标准 HEIC 的 primary `hvc1` + `auxl` alpha item，全程不用 `lhv1`。
libheif、FFmpeg、Windows WIC 三方解码验证通过，500×500 QP26 下双流全 GPU 约 0.56 s。

关键封装字节（重建时用）：

- `ipma`：`entry_count u32` + 每项 `item_ID u16 + association_count u8 + 关联表`，多写一个 u16 会全表错位。
- `hvcC` 的 chromaFormat 按 libheif 惯例是 `chroma_format | 0xFC`，4:2:0 写 `0xFD`；写 `0xFC` 会被读成 monochrome。
- 每个 item 的 `hvcC` 只放自己流里的 VPS/SPS/PPS；item 数据只放 slice NAL。
- `ftyp` major brand 用 `heic`。

**否决原因**：Windows 看图软件对所有 HEIC/AVIF 的 alpha 均不合成，浏览器不支持 HEIC——
透明 HEIC 在日常查看环境没有正确显示场景，只能用于 libheif 生态（服务端/自研管线）。

### 14.3 Intel UHD 770 对照

oneVPL/media-driver 当前 AV1 encoder 输入只有 NV12/P010/AYUV/Y410，没有 YUV400；
`YUV400` 只出现在 JPEG 或解码路径。i7-14700K 核显不能视为可编码 AVIF monochrome alpha。

---

## 15. 后续工作建议

默认批处理已经没有未落地的性能项；剩下都是**条件触发**，详见 `OPTIMIZATION_PROPOSALS.md`：

- 语料出现 >16383 且必须输出 AVIF → 方案 I2（AVIF grid）。
- 透明图必须输出 AVIF 且 CPU alpha 成本不可接受 → 方案 A。
- 要在另一台机器上追求全 GPU alpha → 先 probe `NV_ENC_CAPS_SUPPORT_MONOCHROME`（方案 B）。
- 确有单二进制/无人值守需求 → 方案 R，验收用 `PARITY_MATRIX.md`。
- 换掉 Pillow 单线程解码器（nvJPEG/TurboJPEG MT）理论上有空间，但未证伪也未实现。

运维侧保留：长路径开启 `LongPathsEnabled`、图库保持子目录结构、输出目录加 Defender 排除、
8192 级大图 × 高 worker 数时关注显存与静默 CPU 回退。

---

## 16. NVENC 上下文 LRU 复用

- **实测前提**：`av1_nvenc` 打开上下文约 **80 ms/次**，占 512×512 单图墙钟约 75%。
- **缓存设计**：`GPU_ENCODER_CACHE`（`Mutex<Vec<(GpuKey, Encoder)>>` 当 LRU）。key =
  `(宽, 高, 像素格式, preset, cq)` 五项精确匹配；constqp/preset 烧在会话里，任何打开选项不同都不可复用。
- **正确性**：`gop=0 + forced-idr` 下每帧都是关键帧，FFmpeg nvenc 对 AV1 硬编码 `repeatSeqHdr=1`；
  `ctx_reuse_tests::reused_session_output_matches_fresh_session` 断言复用会话第 2 帧与全新会话
  输出解码 MAE=0.0 且各自可独立解码。命中路径不 `send_eof`。
- **容量**：`NVAVIF_CTX_CACHE`（默认 1；0 禁用）。批处理设 `max(1, NVENC_SESSION_LIMIT // workers)`，
  恒有 `workers × 容量 ≤ 8`。
- **坑 1**：miss 必须先驱逐再开。先开后驱逐会让每 worker 瞬时持 2 个会话，8 worker 超 8 会话上限、
  显存翻倍，最大图 NVENC 初始化失败后静默回落 CPU（1.65 s → 70 s，输出变小，容易被误读成压缩率改善）。
- **坑 2**：常驻上下文是拿显存换时间（本机 12 GB 峰值 8.1 GB 无压力；小显存要下调 worker/容量）。
- **收益边界**：只对同尺寸密集语料和 auto_cq 试编码（固定 512×512）有效；尺寸互异 + 固定 cq
  的语料零收益零伤害。当前实测：同尺寸命中约 16.5 ms/张，两尺寸交替 evict+open 约 224 ms/张，
  每 worker 首张约 479 ms；小图上命中与不命中可差 8×。
- **注意**：进程首次 256×256 硬件探测也占一格缓存，容量 8 实际只装 7 个真实尺寸。

---

## 17. auto_cq 逐图 CQ 与边界

auto_cq 用两张 512×512 马赛克试编码 + 割线外推，再全分辨率编码一次；额外成本约几十毫秒，
不是 3× 全图编码（方案 L 已证伪）。逐图结果写入报表 `cq` 字段（`encode_file(with_cq=True)`）。

历史分布（41 张 AVIF、`--auto-quality 90`、目标 SSIM 0.995）：CQ16 14 张、19–45 14 张、
50–51 13 张。大照片被 safeguard 钉在 16，简单图形到 51。当时结论：下限 16 的 safeguard 已足够，
上限 51 无过度压缩证据，不需要新增 clamp；`--auto-quality 88` 在混合图库上存储更省。

**现状（2026-10-17 起）**：批量默认改为固定 CQ30/36 按像素分流，auto_cq 仅在显式
`--auto-quality TARGET` 时启用；固定 CQ 与自动标定的选择不再混用。库 API 仍保留 auto_cq。

---

## 18. 百万级长跑加固

单次运行 1M+ 源（10–20 h）时，编码核心无泄漏（会话缓存有界、逐图对象 RAII 释放），编排层已加固：

- **滑动窗口提交**：在飞 ≤ 4×workers（`wait(FIRST_COMPLETED)` 收割 + 补位），主进程内存与语料规模无关。
- **流式报表** `<report>.stream.jsonl`：逐行追加，中断/崩溃保留已完成部分；正常结束写完整 `.json`，
  Ctrl+C 时顶层 `"interrupted": true`，重跑同参数即续传。
- **采样器内存上界**：>2 万样本后间隔倍增（上限 5 s），避免长跑日志无限增长。
- **worker 定期重生**：`max_tasks_per_child=10000`，防慢泄漏/碎片化。
- **探针并行化**：>64 文件时用线程池做 header 探测，避免 1M 源先串行等 15–50 分钟。
- **续跑指纹**：源/目标绝对路径、续跑版本、有效配置、源大小 + `mtime_ns` 全部匹配才复用
  `kept_source`；指纹只 stat 不读内容，同大小且 mtime 被还原的变化检测不到，需 `--overwrite`。
- **Windows 长路径**：未开启系统长路径时，>260 字符路径可能在 `rglob`/写入时失败并计入
  `failures`（不中断）。开启 `LongPathsEnabled`（或 git `core.longpaths=true`），或保持目录层级浅。

---

## 19. 质量评估指标体系速览

本节是**指标口径的唯一事实源**；README、uvtest 脚本和其他文档只链接到这里。

| 层面 | 指标 | 实现/位置 | 用途 |
|---|---|---|---|
| 编码闭环（每图自动） | 8×8 块状 SSIM（仅亮度） | `src/lib.rs` `calculate_ssim` | auto_cq 校准到 `target_quality`（0–100，默认 80 → SSIM 0.98；≤1 直接当 SSIM）；试编码固定 512×512 |
| 感知评估（人工跑） | LPIPS（alex, torch）；ΔE2000 worst-1% 仅色度专项辅助 | `uvtest/compare_perceptual.py`、`compare_runs.py` | 最终裁判 = LPIPS + 人工核验 |
| 回归报警（自动） | SSIM（含最差块数值/坐标）/ PSNR / alpha MAE（含 worst-1%）/ chroma PSNR | `uvtest/quality_metrics.py`、`cargo test` | 同路径回归比较，不做观感预测 |

规则：

- 分块 SSIM 对高频/硬边缘**偏保守**，只当回归检测器；“看起来一样吗”以 LPIPS 为准（<0.05 ≈ 不可辨）。
- **ΔE2000 不是独立判断依据**：hue-mean 为简化实现，仅作同路径内相对比较；`compare_runs.py`
  默认关闭（成本约 3–6× LPIPS），仅在专项排查 4:2:0 色度渗色时开启。
- RGBA 算任何指标前必须**先合成到背景色**（当前统一白底）。不合成会把透明像素下方的无意义 RGB
  算进指标；曾导致最差块 SSIM -0.26、均值被从 0.9972 拉到 0.9898。
- LPIPS 数值仅在同一网络（alex）、同类失真间可比；torch 为 CPU 版，感知子集限 ≤4 MP 源。
- chroma PSNR 使用标准 BT.709 RGB→YCbCr 矩阵；alpha 同时报告 mean 与 worst-1%；
  分块 SSIM 报告最差块数值/坐标。mean 类数值算法未变，历史报表仍可比；仅 chroma PSNR 口径变化。
- VMAF 未使用（视频向指标，需 libvmaf），图片场景由 LPIPS 承担。
- **测量纪律**：不要跨会话 A/B（同一份代码不同天可差 60 ms/张，比多数优化收益还大）；
  必须同 wheel、同会话、背靠背，必要时用临时环境开关切旧路径。判定格式/路由用墙钟与 Σ任务时间，
  不要用单图中位数（WebP 分布右偏，中位数会给出反向结论）。小样本画质结论容易翻转，能全跑就全跑。

---

## 20. AVIF EXIF 透传与文件时间保持

### 20.1 EXIF

HEIF 的 Exif item 载荷 = **4 字节大端 TIFF 头偏移 + TIFF**，不是 JPEG/WebP 的
`b"Exif\0\0" + TIFF`。`avif-serialize` 原样写入；libavif 遇到畸形 item 会以
“Invalid Exif payload” 拒绝整个文件，所以布局无法识别时宁可丢弃。

- 库层 `_normalize_exif_payload()` 在调 Rust 前统一：剥 JPEG/WebP 前缀、补 4 字节偏移；
  经 `exif_transpose` 的输入去 Orientation；numpy 输入不转正、保留原标签。幂等。
- 批处理 `_source_exif_bytes()` 只读源头，做同样归一化；畸形返回 None。
- 回读校验要查**文件字节**：Pillow 的 AVIF 解码会把 Orientation 归一化成 1，不能信 `getexif()`。

**元数据承载**：WebP/PNG 透传 EXIF + ICC + XMP（WebP 走 `xmp=`，PNG 走 Adobe iTXt
`XML:com.adobe.xmp`）；AVIF 只能带 EXIF（avif-serialize 只暴露 `set_exif`），ICC/XMP 缺失时
逐图写入 `dropped_meta`。`photoshop`(IPTC)/`adobe`/`dpi` 各路由都不传。

### 20.2 文件时间

输出 mtime 用 `os.utime` 继承源；Windows 创建时间另需 `SetFileTime`
（`CreateFileW` + `FILE_WRITE_ATTRIBUTES`；FILETIME = 1601 起的 100 ns 计数）。源 stat 在写盘前
捕获，in-place 时早于删源；WebP/PNG/AVIF 各路由一致。atime 也会写入，但没有保持意义：源在编码前
必被读取，输出 atime ≈ 编码时刻。真正恢复的是 mtime 与创建时间。

> 库层修复需要重建 wheel 才在 `.venv` 生效：venv 装的是预构建 wheel，不从源码装（§5.1）。
> 批处理自带载荷构建，不受 wheel 状态影响。

---

## 21. 路由速度对比与默认阈值

**工具**：`uvtest/bench_opaque_formats.py --fixed-cq`，走真实 `_encode_task`（解码 + 预处理 +
编码 + 落盘），排除 alpha/动图/超尺寸源，按 avif→webp→webp→avif 交替抵消漂移。

53 张不透明源、533 MP、8 worker、固定 CQ20（历史对比）：

| 格式 | 墙钟 | Σ任务时间 | 输出 |
|---|---:|---:|---:|
| AVIF | 3.53–3.58 s | 24.4–24.8 s | 98.4 MB |
| WebP q90/m2 | 3.48–3.50 s | 25.2–25.3 s | 62.3 MB |

总计打平（1.02×），但逐图配对后分桶差异很大：

| 图幅 | webp/avif | 结论 |
|---|---:|---|
| <8 MP | 0.24 | WebP 快 4.2× |
| ≥8 MP | 1.36 | AVIF 快 1.36× |

### 21.1 上下文命中后的重测

这是 **AVIF 全 miss** 时的交点（8 MP）。命中 NVENC context 后，用 `--same-size` 直接测命中成本：
1.23 MP 14.8 ms、1.75 MP 33.8 ms、2.39 MP 32.1 ms、3.47 MP 38.2 ms、3.48 MP 68.0 ms；
miss 的 evict+open 随分辨率上升（2.39 MP 约 267 ms、3.5 MP 约 330–360 ms），不是常数。

按各桶真实命中下限重算期望成本：1.23 MP AVIF 慢 1.3×；1.75 MP 略慢；2.39 MP 起 AVIF 快
1.40–1.75×。**交叉点落在 1.75–2.39 MP，默认阈值取 2 MP**（保守取整）。4–8 MP 本语料无样本，
速度/体积都夹在相邻桶之间，随阈值走 AVIF。

**默认 CQ（2026-10-17 决定）**：>8 MP 用 `--cq 30`；≤8 MP（实际是 2–8 MP）用 `--mid-cq 36`。
相对 WebP Q90 的体积：CQ30 1.30–1.32×（8–32 MP），CQ36 1.06–1.13×（2–16 MP）。
CQ36 在 16–32 MP 只有 2/11 张 LPIPS 优于 WebP，故大图不降档。`--auto-quality` 启用时两者都不生效。

注意：

- 交点随内容漂移（libwebp 对内容比 NVENC 敏感），所以是**默认策略**而非物理常数。
- 这批对比只谈速度；CQ20/CQ30/CQ36 与 WebP Q90 不是对齐质量，字节差不能单独当画质判定。
- 判定用墙钟/Σ任务时间，不用单图中位数：WebP 分布右偏，中位数落在小图那半边，会得出“WebP 快 2.3×”的反向结论。

### 21.2 WebP method 策略（2026-10-17）

method≥3 在大图上触发穷举划分搜索：73.jpg（8736×11648）42 s vs method=2 的 2.3 s，输出只小 10%。
因此只对 oversize（>8192）封顶 `min(请求值, 2)`；≤8192 的透明/不透明路由保留请求值（默认 4）。

全批 A/B（test_imgs 79 张，method 2 vs 4）：墙钟 6.1–7.0 s vs 6.6–6.8 s（噪声内），输出
92.9 vs 92.7 MB；透明 WebP 路由 10.17 → 10.02 MB（−1.5%）。隔离编码测得 method 4 的 libwebp
CPU 工时约为 method 2 的 1.9×（透明）/2.7×（不透明 ≤2 MP），但混合图库中被 AVIF 任务并行吸收，
未体现为墙钟差异。

---

## 22. 底层加速现状

| 组件 | 是否多线程/加速 | 依据 |
|---|---|---|
| NVENC（AVIF 颜色流） | 硬件加速 | `av1_nvenc` |
| rav1e（alpha、CPU 颜色回退） | 多线程 | `NVAVIF_RAV1E_THREADS`，批量按 cores/workers 下发 |
| 进程池 | 8 进程并行 | `default_workers = min(8, cpu_count//2)` |
| libwebp 编码 | 单线程 | `PIL._webp` 只暴露 `WebPEncode`，无 `thread_level` |
| libjpeg-turbo 解码 | 单线程 | Pillow 不走 TurboJPEG 的 MT API |
| lcms 色彩转换 | 单线程 | ImageCms 无并行入口 |

**加进程已到顶**：同一批 53 张、8 vs 20 worker，墙钟 3.70–3.76 s → 3.01 s（1.24×），
但 Σ任务时间反而 +32%。本机 20 核 28 线程（8P + 12E）、L3 仅 33 MB；单张 24 MP 的 RGB 缓冲
72 MB，早已冲掉 L3，多进程只是撞同一堵内存带宽墙。默认 8 worker 恰好铺满 8 个 P 核。

跑批期间 CPU 约 24–37%，NVENC 编码引擎 44–79%（从未打满），AVIF 轮并行效率约 88%，
与 WebP 轮 91% 同级。**瓶颈在 CPU 侧（解码与内存带宽），不在 GPU**。

**解码地板**：同尺寸只改 JPEG 质量重存，q=95 53.8 ms、q=75 35.5 ms、q=50 32.0 ms、
q=25 30.1 ms——曲线在 ~30 ms 见底，是缓冲区分配 + 逐像素拷贝的地板。16 MP 源 `draft()` 到
1/2、1/4、1/8，耗时 117/111/97 ms vs 满尺寸 135 ms：输出像素少 64× 只快 28%，说明成本在
熵解码而非 IDCT/上采样。文件 I/O 不是因素（裸读 4.2 ms；内存解码 129.2 vs 磁盘 133.4 ms）。

换解码器也没有收益：NVDEC `mjpeg_cuvid` 在 3 张源里 2 张失败，唯一成功的一张墙钟与 CPU 持平；
libwebp 多线程（`-threads 1/4/8`）在 method=2 下不提速。progressive JPEG 明显更慢
（同 1.4 MB，基线 35.5 ms vs 渐进式 85.8 ms）。唯一未证伪的缺口是 nvJPEG，但需要自建绑定且
会绕过 PIL 的 ICC/方向处理、改变输出像素，不承诺收益。

---

## 23. 全库规模投影（外部索引抽样回放，2026-10-17）

不是实跑全库，而是从用户提供的 Everything 索引抽样估算。口径写清，避免以后当成实测引用。

**数据源**：外部索引共 10,514,415 行，其中图片 2,908,967 个、合计 4097.5 GB（4.10 TB）。

**方法**：按 `图幅桶 × 扩展名` 从索引聚合总量，每格抽 30 个真实文件（bmp/tif 10，
oversize jpg/jpeg 20），共 640 个样本；应用与批处理一致的扫描期过滤后，以 8 进程池
回放 `_encode_task`（`NVAVIF_CTX_CACHE=1`），比例按字节加权。样本为随机抽样，
比例用 bootstrap 给区间。

**投影结果**（当前默认路由 + CQ30/CQ36）：

| 桶 | 输入 | 输出 | 比例 | 编码墙钟 |
|---|---:|---:|---:|---:|
| >8 MP | 2141.6 GB | 1129.8 GB | 1.90× | 8.5 h |
| 2–8 MP | 993.7 GB | 305.4 GB | 3.25× | 7.0 h |
| ≤2 MP | 438.8 GB | 186.5 GB | 2.35× | 3.8 h |
| oversize（>8192） | 414.2 GB | 99.7 GB | 4.15× | 3.6 h |
| no-dims | 1.9 GB | 1.5 GB | 1.30× | ~0 |
| gif（原样拷贝） | 105.5 GB | 105.5 GB | 1.00× | 0 |
| below-min（<130×66，拷贝） | 1.7 GB | 1.7 GB | 1.00× | 0 |
| **合计** | **4097.5 GB** | **1830.0 GB** | **2.24×** | **23.0 h** |

只算实际参与编码的 3.99 TB 是 2.32×。整体比例的 bootstrap 95% 区间 **1.98–2.47**，
节省约 **2.27 TB**。

**为什么只有 2.24×**：大头是已经是有损的 JPEG——占输入字节 71%（2.92 TB → 1.49 TB，
1.96×），并占输出了 81%。PNG 0.94 TB → 0.13 TB（7.1×）虽然高，但基数小；WebP 源经
keep-smaller 基本保持 1.0×；GIF 0.105 TB 原样拷贝。>8 MP 的 JPEG 桶单独就有 1.76 TB、
仅 1.69×，它决定了总比例的上限。

**耗时口径**：任务工时 167.5 h ÷ 实测有效并行 7.30× = **23.0 h**（bootstrap 21.5–24.4 h，
8 worker）。**不含**全目录头部探测/扫描、copy-skipped 的 I/O、以及索引本身的读取时间；
真实端到端应另加这部分。

注意：

- 640 个样本的字节加权投影，内容分布与索引一致但仍是抽样；区间已给，不要当精确值。
- 比例是体积口径，不是画质判定；默认 CQ 的画质结论仍由用户人工比对。
