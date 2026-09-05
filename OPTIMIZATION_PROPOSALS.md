# 代码分析与优化方案评估

> 基于 nvavif_py 0.1.0 源码（1602 行 Rust + PyO3）及 `uvtest/benchmark.py` 实测数据。

本文的优化目标是把输入图片转换为压缩率较高且尽量保真的图片，同时保持 RGB/RGBA 信息正确，并控制编码成本。浏览器只是透明度和标准解码路径的一个验证工具，不是格式选择的前置限制；压缩率、画质保真度、alpha 正确性和编码速度才是主要评价指标。使用层面的最终目的（2026-09-05 明确）：**调用 NVIDIA GPU 加速海量图片（混合内容图库，普通图与透明 PNG 混杂）的批量压缩，透明通道不丢失，透明图路径不被 CPU alpha 编码拖垮**。

---

## 1. 当前架构总览

```
输入 RGB/RGBA (u8/u16/f32)
       │
       ▼
build_yuv() ────────── rayon 并行 RGB→YUV420/444 转换 ──────────┐
       │                                                         │
       ▼                                                         ▼
   color_yuv (NV12/P010)                                  alpha_yuv (YUV400)
       │                                                         │
       ├──────────── device="auto" ──────────────┐               │
       │                                         │               │
       ▼ GPU (NVENC)                        ▼ CPU (rav1e)         ▼ CPU (rav1e Cs400)
  encode_av1_frame_gpu()              encode_av1_frame_cpu()   encode_av1_frame_cpu()
  (av1_nvenc 编码器)                  (颜色 fallback)           (alpha，无 GPU 路径)
       │                                         │                   │
       ▼                                         ▼                   ▼
   color_av1                                 color_av1           alpha_av1
       │                                         │                   │
       └────────────── avif-serialize ──────────┘──┘──────────────────┘
                           │
                           ▼
                     AVIF bitstream
```

**关键特征**：

- `auto` 模式先用 NVENC，失败则自动回落 rav1e
- 透明图的颜色走 GPU，alpha 走 CPU，两者通过 `std::thread::scope` 并行
- YUV444 GPU 路径因 NVENC profile 问题会 fallback 到 CPU
- `is_hardware_supported()` 缓存探测结果，避免重复试错

---

## 2. 性能瓶颈诊断

### 2.1 透明图 CPU Alpha 编码（主要瓶颈）

**根因**：AVIF 规范要求 alpha 是独立的 monochrome (YUV400) AV1 流。当前 FFmpeg 的 `av1_nvenc` 编码器只支持 NV12 / P010 / YUV444 输入，不暴露 YUV400 / GRAY16 输入路径。

**实测影响**：

| 指标 | 不透明图 | 透明图 |
|---|---|---|---|
| 单图平均编码 | 286 ms | 4810 ms |
| 批量吞吐 | 34.5 MP/s | 2.0 MP/s |
| 最慢单图 | 16.9 s | 16.9 s |

**问题本质**：GPU 做颜色，CPU 做 alpha，并行后总耗时取决于较慢方（CPU alpha），所以 GPU 优势被浪费。

### 2.2 YUV 提取（CPU rayon）

`extract_yuv420` / `extract_yuv444` 使用 rayon `par_chunks_mut` 并行逐行转换，对 8K 图片约 50–200 ms。这不是瓶颈，但在透明图中被重复两次（颜色 + alpha 各调一次），浪费约 50% 的提取时间。

### 2.3 NVENC 尺寸限制

实测 8192×8192 为 RTX 4070 NVENC AV1 的上限。超限图（如 11648×8736）直接拒绝，`auto` 模式回落 CPU。

### 2.4 串行单图处理

当前无批量 API。一次调用 `encode_avif()` 只处理一张图，Python 侧循环调用时无法利用 NVENC 的帧间 pipeline 空闲期。

---

## 3. 不透明 vs 透明 完整对比

### 3.1 测试集构成

| 类别 | 数量 | 占全部 79 张 | 占可处理 75 张 |
|---|---:|---:|---:|
| 不透明（包括 alpha 全为 255 的 RGBA） | 65 | 82.3% | 86.7% |
| 真透明（至少一个 alpha < 255） | 10 | 12.7% | 13.3% |
| 跳过（宽或高 >8192） | 4 | 5.1% | — |
| 合计 | 79 | 100% | 100% |

### 3.2 算法与参数对比

| 项目 | 不透明图（颜色流） | 透明图（颜色流） | 透明图（alpha 流） |
|---|---|---|---|
| 编码器 | NVENC `av1_nvenc` | NVENC `av1_nvenc` | rav1e CPU |
| 编码模式 | `rc=constqp`, `qp=20` | `rc=constqp`, `qp=20` | `quantizer=51` (20×255/100) |
| Preset | `preset=p6` | `preset=p6` | `speed=5` (from preset 6) |
| Tune | `tune=hq` | `tune=hq` | — |
| GOP / 帧模式 | `gop=0` (intra-only) | `gop=0` (intra-only) | `key_interval=1` |
| B-frames | `bf=0` | `bf=0` | — |
| 输入格式 | P010LE (10-bit) | P010LE (10-bit) | YUV400 (monochrome) |
| Chroma | Cs420 (YUV420) | Cs420 (YUV420) | Cs400 |
| 线程数 | 驱动管理 | 驱动管理 | `with_threads(0)` → 全部 CPU 核心 |
| 执行并行 | 单线程 | `thread::scope` 与 alpha 并行 | `thread::scope` 与颜色并行 |
| YUV 提取 | rayon CPU, 1 次 | rayon CPU, 2 次（颜色+alpha） | 与颜色同一 reader |

### 3.3 alpha 误判修复后的性能

修复前的 benchmark 把 11 张“RGBA 但 alpha 全为 255”的图片误算进透明组，导致它们额外走了一次 CPU rav1e alpha 编码。对这 11 张图片单独对比如下：

| 指标 | 修复前（11 张） | 修复后（11 张） | 改善 |
|---|---:|---:|---:|
| 总编码时间 | 41.58 s | 3.75 s | **减少 91.0%** |
| 编码吞吐 | 3.36 MP/s | 37.23 MP/s | **11.09×** |
| 像素总量 | 139.54 MP | 139.54 MP | — |

按旧 benchmark 的其余 64 张耗时保持不变推算，75 张可处理图片的总编码时间约从 `116.36 s` 降至 `78.53 s`，整体吞吐约从 `6.31 MP/s` 提升至 `9.35 MP/s`，约 **1.48×**。这是替换计算，不是重新跑 75 张全量测试。

这项修复只影响原本误走 alpha 路径的 11 张图片：普通 RGB/不透明 RGBA 现在都只走 GPU 颜色流；10 张真正透明的图片仍然需要 CPU rav1e 编码 alpha，透明图片的结构性瓶颈没有改变。

### 3.4 GPU vs CPU 对比（1024×1024，5 次重复）

| | GPU (NVENC) | CPU (rav1e) | 倍率 |
|---|---|---|---|
| 平均编码 | 98.8 ms | 6672.7 ms | **67.6×** |
| 吞吐 | 10.6 MP/s | 0.157 MP/s | **67.6×** |
| 输出大小 | 1,379,186 B | 991,382 B | GPU 大 ~39% (tune=hq) |

### 3.5 瓶颈根源

真正透明图的总耗时等于 `max(GPU 颜色编码, CPU alpha 编码)`。由于 CPU alpha 编码比 GPU 颜色慢 20–50 倍，GPU 的时间仍可能被完全浪费。

- 不透明图：只做 GPU 颜色编码，当前 11 张修复样本平均约 341ms/张
- 真透明图：GPU 颜色约 300ms，CPU alpha 仍可能需要数秒，并行后总耗时取决于 CPU alpha

对真透明图而言，瓶颈仍是 CPU alpha，不是 GPU 颜色；对误判的全不透明 RGBA，去掉无意义的 alpha 流已经消除了该瓶颈。

---

## 4. 优化方案

### 方案 A：Alpha 下采样（低风险，中等收益）

**思路**：alpha 视觉信息量远低于颜色，下采样 2× 或 4× 后编码，再在解码时缩放回来。

```rust
// 伪代码
let (aw, ah) = if alpha_subsample == 2 { (w/2, h/2) } else { (w, h) };
// 对 alpha 做 2x2 平均池化
// 用 rav1e Cs400 编码 aw×ah 的 alpha
// 在 AVIF 容器中记录 alpha 缩放比例（通过 alpha item 的 clip 属性或自定义属性）
```

**效果估算**：2× 下采样 → alpha 编码耗时降低约 4×，3000×3000 图从 ~5s 降到 ~1.2s。

**代价**：需要自定义解码路径处理 alpha 缩放；非标准操作，`Pillow` 等第三方可能无法正确解码缩放后的 alpha。

**推荐等级**：★★☆ 适合快速验证方向，但 AVIF 规范支持有限。

### 方案 B：NVENC 原生 monochrome（高风险，大收益）

**思路**：绕过 FFmpeg 的 `av1_nvenc`，直接调用 NVENC SDK 的 C API。当前 NVENC 头文件没有 `NV_ENC_BUFFER_FORMAT_YUV400` 枚举，但提供了 `NV_ENC_CAPS_SUPPORT_MONOCHROME` 能力查询和 `NV_ENC_CONFIG::monoChromeEncoding=1` 会话选项。需要先确认目标驱动对 AV1 monochrome 的实际支持，以及 monochrome 会话接受的输入 buffer 格式。

```rust
// 需要新增依赖
// nv-codec-headers（已有）
// nvEncodeAPI64.dll（Windows）/ libnvidia-encode.so（Linux）
// 查询 SUPPORT_MONOCHROME，设置 monoChromeEncoding，
// 然后调用 CreateSession → InitializeEncoder → EncodePicture → DestroySession
```

**理论收益**：如果目标 GPU/驱动支持并且 native session 可以产出兼容的 monochrome AV1，alpha 才有机会接近颜色流的 GPU 编码速度。之前的 `50x` 和 `~100ms` 只是根据普通 GPU/CPU 测试推算，不能作为本项目的实测结果。

**当前机器实测结果**（RTX 4070，当前 NVIDIA 驱动）：

```text
$ bash uvtest/probe_nvenc_monochrome.sh
NVENC_SESSION=ready
AV1_GUID=present
AV1_MONOCHROME_CAPABILITY=0
AV1_INPUT_FORMATS=NV12,YV12,IYUV,YUV420_10BIT,...
AV1_MONOCHROME_RESULT=not_reported
```

这表示当前硬件/驱动组合支持 AV1 NVENC，但没有报告 AV1 monochrome 能力。因此本机不能通过方案 B 把 AVIF alpha 安全地改成 GPU 编码。probe 脚本已于 2026-09-05 随实验清理删除；如需在其他 NVIDIA GPU 或驱动版本复测，可按本节步骤重写。

#### 方案 B 的 FFmpeg 强制实验结果

除了 native API capability probe，还用一个固定脚本（已随 2026-09-05 实验清理删除）验证了绕过库、直接把灰度 alpha 帧交给 CUDA/NVENC 的结果：

```bash
uv run python uvtest/force_gpu_alpha_experiment.py
```

在 RTX 4070、`1024x1024`、`preset=p6`、`constqp/qp=20` 条件下：

- 直接输入 `gray` 失败，FFmpeg 最终选择 `YUV444P`，随后 NVENC 报 `YUV444P not supported`；显式输出 `gray` 时则先提示自动选择 `gbrp`，结果相同。
- 先转 `yuv420p` 可以在约 `230 ms` 内编码，但 `ffprobe` 识别出的码流是普通 AV1 `Main/yuv420p`，不是 YUV400。因此这不能作为 AVIF alpha item。
- 先转 `yuv444p` 仍然被当前 RTX 4070 NVENC 拒绝。
- 项目当前 `device="gpu"` 透明图参考耗时约 `2.4 s`，其中 GPU 只负责颜色，CPU rav1e 仍负责 alpha；这与“全 GPU”不是一回事。

结论：方案 C（NV12/YUV420 伪装 YUV400）和“强制灰度送 NVENC”都不能得到可依赖的标准 AVIF alpha。当前硬件不应继续投入 AV1 native monochrome 封装实现；只在另一台机器的 `NV_ENC_CAPS_SUPPORT_MONOCHROME=1` 时重新评估方案 B。

#### 方案 B2：NVENC HEVC alpha layer + HEIF（当前机器可编码，容器待解决）

这条路径与 AV1 monochrome 不同。NVIDIA NVENC API 对 HEVC 提供 `NV_ENC_CAPS_SUPPORT_ALPHA_LAYER_ENCODING` 和 `NV_ENC_CONFIG_HEVC::enableAlphaLayerEncoding`，输出同时包含 HEVC 基础层和 alpha 层。当前 RTX 4070 的原生 probe 结果：

```text
HEVC_GUID=present
HEVC_ALPHA_CAPABILITY=1
HEVC_ALPHA_INPUT_FORMAT=ARGB
HEVC_ALPHA_ENCODE=NV_ENC_SUCCESS (0) total_bytes=128131 alpha_bytes=11890
```

测试输入是 `500x500` 的真实透明 PNG，不是合成的无 alpha 图。NAL 解析得到 layer ID 0 和 layer ID 1，说明 alpha 确实进入硬件输出。完整实验当时由以下脚本复现（已随 2026-09-05 实验清理删除，结果记录保留在本节及 DEVELOPMENT_NOTES §14.1）：

```bash
uv run python uvtest/test_hevc_alpha_image.py
uv run python uvtest/inspect_hevc_layers.py uvtest/out/gpu_alpha_experiment/nvenc_hevc_alpha_probe.h265
```

HEIF 封装存在独立阻塞：libheif 1.23.1 的标准 alpha 路径是主图加 `auxl` alpha item，且其源码暂不支持 layered HEVC 的 `lhv1` item。NVIDIA 的 HEVC alpha 输出不能直接交给当前 libheif，也不能通过普通 `hevc_nvenc` CLI 选项自动完成。需要支持 `lhv1` 的 HEIF writer/reader，或自定义 BMFF 封装和验证链路。

**判断**：layered HEVC 路线在本机已证明 GPU 能编 alpha，但容器生态为零，保留为历史结论。可行的封装路线见下方案 B3。

#### 方案 B3：双单层 HEVC + 标准 HEIC 双 item（2026-09-05 已验证容器层可行）

绕开 layered 输出：用两条普通单层 `hevc_nvenc` 流——颜色走 NV12；alpha 用 `alphaextract` 把 alpha 平面提取为 luma、色度填 128、全范围（`yuvj420p(pc)`）——封装为标准 HEIC 的 primary `hvc1` + `auxl` alpha item，alpha item 标 `auxC urn:mpeg:hevc:2015:auxid:1` 和 `colr matrix_coefficients=0`。全程没有 `lhv1`，不需要 NVENC alpha layer 能力。

实测（500x500 真透明 PNG，QP26）：

| 读取方 | 结果 |
|---|---|
| libheif（pillow-heif） | RGBA 且 alpha 平均误差 `0.015`（max 6），颜色 MAE 2.1 |
| FFmpeg | 双流解码正确，alpha luma 平均误差 `0.015` |
| Windows WIC Microsoft HEIF Decoder | 打开并解码成功，与 libheif 参考件行为一致 |

编码双流全部 GPU 共约 `0.56s`（含进程启动），同图当前 AVIF 路径（GPU 颜色 + CPU rav1e alpha）为 `1.07s`；alpha 流仅 2.5 KB。封装器与验证脚本（`build_hevc_alpha_heic.py`、`test_hevc_dual_heic.py`）已于 2026-09-05 清理，封装规范与踩坑记录见 DEVELOPMENT_NOTES §14.2。

**判断（最终，2026-09-05）**：容器层三方（libheif/FFmpeg/WIC 解码）验证通过，文件本身合规。但查看环境实测：Windows 看图软件对所有 HEIC/AVIF 的 alpha 一律不合成（本方案文件、标准 libheif 参考件、nvavif 透明 AVIF 全部黑底；WIC 查询一律返回 `Bgr32`），浏览器又不支持 HEIC。也就是说透明 HEIC 在日常查看环境里没有任何正确显示的场景——本方案与方案 B2 一样**不能作为通用透明图生产路径**，仅适用于 libheif 生态（服务端/自研管线）。透明图提速的正道回到降低 AVIF CPU alpha 成本：rav1e 速度参数/线程调优（改动最小）、方案 A 下采样（需兼容性验证）。

#### Intel UHD 770 / Raptor Lake 对照

从 Intel 官方 `oneVPL-intel-gpu` 与 `media-driver` 当前 Git 源码核对，AV1 encoder capability 配置提供 `NV12`、`P010`、`AYUV`、`Y410`，没有 `YUV400`；media-driver 的 AV1 输入表也只有 `NV12`/`P010`。源码中的 `YUV400` 只用于 JPEG 或解码/通用数据结构。故 i7-14700K 核显不能据现有源码视为可直接编码 AVIF monochrome alpha，普通 AV1 编码能力与 monochrome alpha 能力必须分开判断。

**代价**：
- 需要自行实现 NVENC 原生 API 封装，约 200–400 行 Rust
- 需要处理 Windows/Linux 平台差异
- 需要处理 NVENC session 生命周期和 GPU 显存管理
- 维护成本显著增加
- 需要验证 NVIDIA 各驱动版本的 monochrome 支持稳定性

**推荐等级**：★★★ 仅适用于 capability probe 报告支持的硬件；当前 RTX 4070 不能实施。

### 方案 C：GPU 编码 Alpha 为 NV12 + 容器修正（中等风险）

**思路**：将 alpha 复制填充为 NV12 格式（U=V=128），用 NVENC 编码后，在 AVIF 容器中将该项标记为 monochrome。部分解码器会正确裁剪，部分不会。

```rust
// 伪代码
let fake_nv12_alpha = pad_alpha_to_nv12(&alpha_yuv);
let encoded = encode_av1_frame_gpu(width, height, &fake_nv12_alpha, ...);
// AVIF 序列化时设置 alpha 项的 chroma subsampling 为 monochrome
```

**效果估算**：同方案 B 速度提升，但解码正确性不确定。

**代价**：不符合 AVIF 规范，不同解码器可能产生不同结果，不能作为可靠的图片转换输出。

**推荐等级**：★☆☆ 不建议，有跨解码器兼容性风险。

### 方案 D：NVENC 尺寸预检查（低风险，避免浪费）

**思路**：在 `encode_av1_frame_gpu` 入口处检查宽高，超过 8192 直接返回错误而非进入 NVENC 初始化。

```rust
fn encode_av1_frame_gpu(...) -> PyResult<Vec<u8>> {
    if width > 8192 || height > 8192 {
        return Err(PyRuntimeError::new_err(
            format!("Dimensions {}x{} exceed NVENC max 8192x8192", width, height)
        ));
    }
    // ...
}
```

**效果**：减少一次无意义的 NVENC 初始化失败和 fallback 延迟。

**推荐等级**：★★★★ 立即可做，零风险。

### 方案 E：批量 GPU Pipeline（中等风险，大收益）

**思路**：新增 `encode_avif_batch` API，内部批量提交多张图片到同一个 NVENC session，利用 NVENC 的多线程 pipeline 和帧间零拷贝特性。

```rust
#[pyfunction]
fn encode_avif_batch(
    py: Python<'_>,
    images: &[(Vec<u8>, usize, usize, String, i32)],  // (pixels, w, h, dtype, cq)
    device: &str,
) -> PyResult<Vec<Py<PyBytes>>>
```

**关键优化**：
- NVENC session 复用，避免反复 open/close（每次 open 约 5–20 ms）
- YUV 提取和 GPU 编码流水并行：线程 A 处理第 N 张的 YUV，同时线程 B 提交第 N-1 张到 GPU
- 减少 Python GIL 进入次数

**效果估算**：批量 10 张 1024×1024 不透明图，从 10 × 99ms = 990ms 降到约 300ms（含 session 初始化和 pipeline 重叠）。

**推荐等级**：★★★ 对批量场景有明显收益，实现约 200–300 行。

### 方案 F：CUDA / cuFFT 加速 YUV 提取（高风险，有限收益）

**思路**：将 RGB→YUV 转换移到 GPU（CUDA kernel 或 cuFFT-based 矩阵变换）。

**效果估算**：对大分辨率图片（8K）有 5–10× 加速。

**代价**：
- 需要 CUDA toolkit 和 nvcc 编译环境
- 需编写 CUDA kernel 或绑定 CUDA API
- 对 1024×1024 以下图片，rayon CPU 已足够快，收益不明显
- 跨平台问题（CUDA 只在 NVIDIA）

**推荐等级**：★☆☆ YUV 提取不是主要瓶颈，性价比低。

### 方案 G：NVDEC 硬件解码（中等风险，大收益）

**思路**：解码路径从 libdav1d (CPU) 切换到 `nvdec` + `av1_nvdec`（GPU），通过 ffmpeg-next 的 `av1_nvdec` 解码器。

```rust
// 在 yuv_to_rgb_parallel 之前，用 av1_nvdec 替代 libdav1d
let codec = ffmpeg::decoder::find_by_name("av1_nvdec")
    .ok_or_else(|| ...)?;
```

**效果估算**：解码从 ~30ms/图（4800×3200）降到 ~5ms，10× 加速。

**代价**：
- 解码也需要 GPU 支持检测
- 当前解码器路径是自定义的 `yuv_to_rgb_parallel`，不是标准 FFmpeg 解码管道，改造涉及较大重构
- 需要测试 NVDEC AV1 在所有驱动版本上的可用性

**推荐等级**：★★☆ 收益明显，但解码路径改造复杂。

### 方案 H：进程级并行压缩管线（新增 2026-09-05，低风险，当前最大收益）

**实测依据**（2026-09-05 全量基准，`uvtest/out/compressed/compress_report.json`，79 张 / 1099.5 MP / 205.5 s）：单进程串行下 NVENC 编码器平均占用仅 **0.1%**（峰值 11%），GPU 整体平均 **9%**，CPU 平均 774%/2800%（28 逻辑核）。GPU 和 CPU 双侧都大面积空闲——瓶颈不是算力，是串行调度。

**思路**：不动 Rust 库，纯上层编排。`compress_dir.py` 改用 `ProcessPoolExecutor`，每张图独立调用 `encode_file()`：

- 每个 worker 自行注册 `ffmpeg-out\bin` / `msys64\mingw64\bin` DLL 目录（现有脚本逻辑搬进 worker 初始化）。
- **worker 数上限受 NVENC 并发会话数约束**（消费级驱动 RTX 40 系 ≈ 8）。取 `min(8, 核数/2)` 起步；NVENC 会话创建失败的 worker 自动降级 CPU，不中断。
- 透明图多的图库要下调 worker 数：alpha 的 rav1e 已用满多核（`threads=0`），跨图并行会互相抢核。经验起点：透明占比 >30% 时 worker = 核数/4。
- 超大图（>8192）提交前按尺寸直接路由（顺带落实方案 D 的预检查，避免每次先撞一次 NVENC 报错）。
- 报表兼容：worker 上报逐图 rows，主进程聚合写 `compress_report.json`；资源采样用 psutil 遍历进程树求和。

**预期收益**：GPU 路径 25.6 → 80~100+ MP/s；混合图库整体 4.8 → 15~25 MP/s；1TB 从 ~6.5 天缩到 1~2 天。

**代价**：~100 行 Python，不动库，零接口变化。

**推荐等级**：★★★ **首选**。GPU 空闲 90%+ 是实测数据，先吃满现有算力，再谈库内改造。

**落地结果（2026-09-05）**：已在 `uvtest/compress_dir.py` 实现（`ProcessPoolExecutor` + `--workers`，默认 `min(8, 核数/2)`；资源采样覆盖整个进程树）。全量 79 张实测 **205.5 s → 45.0 s（4.57×）**，5.35 → **24.4 MP/s**；8 张不透明图冒烟达 **99 MP/s**；NVENC 占用峰值从 11% 提到 100%。已观察到的次级问题：多张超大图并行时各 worker 的 rav1e `threads=0` 互相抢核，单张耗时从独占的 16 s 涨回 ~40 s（总墙钟仍大幅受益）——后续可按 worker 数切分 rav1e 线程额度。

### 方案 I：超大图（>8192）路径优化（新增 2026-09-05）

**现状**：全量基准中 4 张 101.7MP 超大图自动 CPU 回退，每张 39~50 s（约 2.2 MP/s），**占总时长 87%**。长尾决定整体。

**I1：CPU 回退颜色编码换快速 preset（~10 行，性价比最高）**

CPU 颜色路径目前继承 NVENC preset P7 → rav1e speed 4（很慢）。对 >8192 的回退路径改用更快的 speed（如 6，或同 alpha 思路固定 speed 9 可配）。预期单张 40 s → 8~15 s（3~5 倍），长尾时间占比从 87% 降到 ~60%。画质损失需按 `auto_cq`/SSIM 验证后再定档。与 `ALPHA_RAV1E_PRESET` 同一模式，改动约 10 行。

**I2：AVIF grid 分块（中等工作量，超大图全 GPU）**

>8192 时切成 ≤8192 的块，逐块 NVENC 编码（颜色 + alpha），按 AVIF grid 规范（`grid` derived item + `iref dimg`）拼回**单个文件**。浏览器和 libheif 对 grid 支持普遍（大图 AVIF 的标准做法）。

阻塞点：`avif-serialize 0.8.8` 不支持 grid（源码无 grid/dimg），需要自写 grid 封装。有 §14.2 手写 BMFF box 的成功经验，预估 ~300 行 Rust + 三方解码验证（浏览器/libheif/ffmpeg）。

收益：101.7MP 单张 40 s → ~4 s（10 倍）。仅在图库超大图占比高时值得。

**I3（不推荐）**：降采样绕过 8192 限制——隐式改变分辨率，破坏保真目标；如业务接受应由上层显式缩放后再入库。

**判断**：先做 I1（10 行换长尾 3~5 倍）；I2 视图库中 >8192 图的实际占比决定。

**I1 落地结果（2026-09-05）**：`src/lib.rs` 新增 `OVERSIZE_RAV1E_PRESET = 4`（rav1e speed 7）与 `cpu_color_preset()` 路由——**仅当宽或高超过 NVENC 上限 8192** 时 CPU 回退使用快速 preset，普通图的瞬时 NVENC 失败仍保持原 preset 画质。实测（`uvtest/test_oversize_preset.py`，01.jpg 101.7MP，cq=20）：**49.4 s → 16.2 s（3.04×）**，文件 -13.5%，SSIM 0.9355 → 0.9233（-0.012）。画质敏感场景可用 `auto_cq` 按目标 SSIM 自动补偿，或显式 `device="cpu"` 保留慢速高质量档。

---

## 5. 优先级排序

| 优先级 | 方案 | 收益 | 风险 | 工作量 |
|---|---|---|---|---|
| ~~**P0**~~ ✅ | 方案 H：进程级并行管线 | **已落地：4.57×（205.5s→45.0s）** | 低（不动库） | ~100 行 |
| ~~**P0**~~ ✅ | 方案 D：尺寸预检查 | 由 I1 的尺寸路由覆盖 | 零 | — |
| ~~**P1**~~ ✅ | 方案 I1：超大图 CPU 回退快速 preset | **已落地：3.04×（49.4s→16.2s）** | 低（SSIM -0.012，可用 auto_cq 补偿） | ~10 行 |
| **P1** | 方案 A：Alpha 下采样 | alpha 4× 加速 | 低（需验证 AVIF 规范兼容性） | ~50 行 |
| **P2** | 方案 I2：AVIF grid 分块 | 超大图再 4~10× | 中 | ~300 行 |
| **P2** | 方案 E：库内批量 pipeline | 并行后再收尾 1.5~2× | 中 | ~200–300 行 |
| **P3** | 方案 B：NVENC 原生 monochrome | alpha 级别 GPU 加速 | 中高 | ~400 行 + 平台测试 |
| **P3** | 方案 G：NVDEC 解码 | 解码 10× 加速 | 中 | ~300 行重构 |

---

## 6. 结论

### 当前可用性

- **不透明图片批量压缩：生产可用**，65 张中包括 11 张修复后的全不透明 RGBA，均只走 GPU 颜色路径
- **真透明图片：功能正确但性能受限**，10 张中 alpha CPU 编码仍是主要瓶颈
- **超大图（>8192）：自动 CPU fallback**，正确但慢

### 是否可行（海量图片 GPU 加速）

**对不透明图片：已可行**。当前实现已对大量普通照片使用 NVENC，1024×1024 实测 99 ms，GPU 相对 CPU 快 67.6 倍。

**对真透明图片：需要进一步优化**。当前 CPU alpha 编码是结构性瓶颈，除非：
1. 接受 CPU alpha（当前方案，透明图 ~5s/张）
2. 实现方案 A（下采样，~1.2s/张）
3. 实现方案 B（NVENC 原生 monochrome，目标约 100ms/张，需实测确认）

**对超大图：建议 P0 尺寸预检查 + 上层应用自行缩放或分块**。库本身不应在编码路径中隐式改变分辨率。

### 推荐的下一步

1. ~~**立即做**：方案 H + 方案 D~~ ✅ 已落地（2026-09-05）：全量 79 张 **205.5 s → 45.0 s（4.57×，24.4 MP/s）**，不透明冒烟 99 MP/s。
2. ~~**紧随其后**：方案 I1~~ ✅ 已落地（2026-09-05）：超大图回退 **49.4 s → 16.2 s（3.04×）**，SSIM -0.012（`uvtest/test_oversize_preset.py` 可复验）。
3. **可选微调**：多 worker 并行时超大图的 rav1e 抢核（单张 16 s→40 s）——按 worker 数切分 `threads` 额度；以及 alpha 多的场景下调 worker 数（核数/4）。
4. **透明图生产路径**：rav1e alpha 高速档已落地（2026-09-05，`ALPHA_RAV1E_PRESET` speed 9）：61.2 MP 透明测试集 93.7 s → 5.95~9.8 s（9.6~15.7×，0.65 → 6.25~10.3 MP/s），文件 +10~25%，alpha MAE 仍在 0.1~0.7/255 量级，已投产可用。剩余方向：方案 A（alpha 下采样，需兼容性验证）。两张测试图 alpha 解码错误已修复（2026-09-06，根因是 rav1e `asm` 特性静默损坏单色码流，修复后全集 alpha MAE ≤0.054，无 asm 损失约 10~25% CPU 编码速度，见 DEVELOPMENT_NOTES 11.4）。
5. **H+I1 落地后再评估**：方案 I2（grid 分块，需自写 grid 封装）与方案 E（库内批量 session）——多进程并行可能已吃掉大部分收益，按残余瓶颈决定。
6. **其他硬件**：只有 probe 报告 `AV1_MONOCHROME_CAPABILITY=1` 时，才实施方案 B。
7. **持续**：方案 G（解码加速）作为独立优化路径。
