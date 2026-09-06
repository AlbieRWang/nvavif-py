# 代码分析与优化方案评估

> 基于 nvavif_py 0.1.0 源码（1602 行 Rust + PyO3）及 `uvtest/benchmark.py` 实测数据。

本文的优化目标是把输入图片转换为压缩率较高且尽量保真的图片，同时保持 RGB/RGBA 信息正确，并控制编码成本。浏览器只是透明度和标准解码路径的一个验证工具，不是格式选择的前置限制；压缩率、画质保真度、alpha 正确性和编码速度才是主要评价指标。使用层面的最终目的（2026-09-05 明确）：**调用 NVIDIA GPU 加速海量图片（混合内容图库，普通图与透明 PNG 混杂）的批量压缩，透明通道不丢失，透明图路径不被 CPU alpha 编码拖垮**。

> **度量方法说明（2026-09-06）**：文中所有 "SSIM" 均指 **8×8 分块、亮度平面、降采样后的 SSIM**（`uvtest/quality_metrics.py`），它有两个已知盲区——**不含色度维度**（4:2:0 的色度渗色测不到），以及 **RGBA 必须先合成到背景色**（透明像素下方的 RGB 无意义，不合成会把不可见差异算进指标）。人眼导向的对比请用 `uvtest/compare_perceptual.py`（LPIPS + 合成白底后的 ΔE2000/色度 PSNR）。2026-09-06 对透明集的人工核验结论：**亮度 SSIM 0.925 的图在正常观感下不可辨**（合成后 LPIPS 0.003~0.005），SSIM 是偏保守的回归检测器，不是观感预测器；涉及格式的最终取舍以人工核验 + 感知指标为准，SSIM 数字仅用于同一路径内的回归比较。

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
- 透明图的颜色走 GPU，alpha 走 CPU，两者通过 `std::thread::scope` 并行（批量脚本自 2026-09-06 起默认把真透明图整体路由 WebP，见 `--transparent-format`）
- YUV444 输入在本机 AV1 NVENC 上不可用（probe 无 444 输入格式，profile=1 修码流无效），请求 444 必然回落 CPU 慢速档；批量管线默认 420 8-bit 不受影响
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

**I4：超大图路由 WebP（新增 2026-09-06，已落地）**

WebP 尺寸上限 16383，可覆盖多数 >8192 图（当前语料最大边 11648）。全尺寸实测（4 张超大图，单线程 vs rav1e 28 核）：

| 编码 | 单张耗时 | 体积 | 亮度 SSIM（4× 下采样，8×8 块；见文首度量说明） |
|---|---:|---:|---:|
| AVIF cq20（rav1e speed 7，28 核） | 36~39 s | 2.18~4.24 MB | 0.9943~0.9952 |
| WebP q80 | 2~13 s（method=2） | 0.69~2.27 MB | 0.9908~0.9924 |

**I4 落地结果（2026-09-06）**：`compress_dir.py` 新增 `--oversize-format webp`（默认 avif 不变）+ `--oversize-webp-quality`（默认 80）。两个额外发现：1. **libwebp `method>=3` 在部分大图内容上爆炸**（73.jpg：method=4 要 42 s，method=2 只要 2.3 s，体积只差 10%）——超大图路由固定用 `method=2`；
2. 超大图 >16383（WebP 格式上限）仍走 AVIF CPU 回退。

最终全量（79 张 / 989.8 MP，`--oversize-format webp` + 透明图默认 WebP）：**205.5 s 基线 → 6.7~7.6 s（27~31×），130~147 MP/s**，零失败；输出 110.5 MB（对比超大图走 AVIF 时的 119.1 MB）。画质代价：超大图 WebP q80 亮度 SSIM ~0.991 vs AVIF cq20 ~0.995（量级均属视觉无损，见文首度量说明），如需极限保真用 `--oversize-format avif`。**2026-09-06 起超大图路由默认即 webp**（avif 保留为显式的高保真选项）。

**真透明图 WebP vs AVIF 双流的画质对比（2026-09-06，10 张硬边缘内容）**：WebP q90 3.5 MB / 6.6 s / alpha MAE 0（无损）/ 颜色 SSIM **0.925~0.997**；AVIF 双流 6.1 MB / 13.2 s / alpha MAE ~0.03 / 颜色 SSIM **0.9975~0.9997**。WebP 的 SSIM 天花板是结构性的（VP8 强制 4:2:0 + 块结构），q95/q98 只把 0.9254 提到 0.9265、体积反涨 1.7×。**选型结论**：速度/体积/alpha 严格无损/查看器兼容性（Windows 看图软件不合成透明 AVIF，全黑底）→ WebP；颜色极限保真（硬边缘图稿）→ `--transparent-format avif`。照片类内容两者 SSIM 差距会小得多。**人工核验（2026-09-06）**：SSIM 最低（0.925）的图稿在正常观感下与源 PNG 无可见差异，需放大多倍才能看出边缘差异——分块 SSIM 对高频/硬边缘差异是偏保守的检测器，实际默认路由保持 WebP。**感知指标核验（2026-09-06，`uvtest/compare_perceptual.py`）**：合成到白底后 LPIPS 全部 0.003~0.005（<0.05 即不可辨），与人工结论一致；此前 SSIM/LPIPS 的"高差异"主要来自两个度量陷阱——只在亮度平面算（色度渗色漏检/误检）以及透明像素下方无意义 RGB 的干扰（RGBA 指标必须先合成）。另一个反转：因 WebP 的 alpha 严格无损而 AVIF alpha 有损（MAE ~0.05），合成白底后的边缘 ΔE2000 与色度 PSNR 反而多数图 WebP 更优。

**生产化（2026-09-06）**：`compress_dir.py` 全部路由/质量参数可调，支持 `--config <json>`（CLI 传参优先，未知键报错）与 `--write-config` 生成模板；新增透明图自动无损档（`--webp-lossless-max-mb`，默认 1 MB：小图形源走无损 WebP——实测与 q90 有损同体积零损失；大图稿保持有损）及 `--webp-method`（默认 4）/`--oversize-webp-method`（默认 2）/`--alpha-rav1e-threads`（默认 0=核数/worker）/`--color-rav1e-threads`（默认 0=不设限）。

**判断**：先做 I1（10 行换长尾 3~5 倍）；I2 视图库中 >8192 图的实际占比决定。

**I1 落地结果（2026-09-05）**：`src/lib.rs` 新增 `OVERSIZE_RAV1E_PRESET = 4`（rav1e speed 7）与 `cpu_color_preset()` 路由——**仅当宽或高超过 NVENC 上限 8192** 时 CPU 回退使用快速 preset，普通图的瞬时 NVENC 失败仍保持原 preset 画质。实测（`uvtest/test_oversize_preset.py`，01.jpg 101.7MP，cq=20）：**49.4 s → 16.2 s（3.04×）**，文件 -13.5%，SSIM 0.9355 → 0.9233（-0.012）。画质敏感场景可用 `auto_cq` 按目标 SSIM 自动补偿，或显式 `device="cpu"` 保留慢速高质量档。

### 方案 J：rav1e 线程额度切分（新增 2026-09-06）

**问题**：方案 H 多进程并行后，每个 worker 的 rav1e（alpha 及超大图回退）都用 `with_threads(0)` 抢占全部核心。实测多张超大图并行时单张从独占的 16 s 涨回 ~40 s——进程间的核竞争抵消了rav1e 的多线程收益。

**思路**：`encode_av1_frame_cpu` 改为从环境变量 `NVAVIF_RAV1E_THREADS` 读取线程池上限（0/未设 = 全核，单进程行为不变）。`compress_dir.py` 按 `cores/workers` 设置该变量，池 worker（spawn）自动继承。同一张透明图内颜色+alpha 两个 context 各占一份额度，合计约等于核数。

**代价**：`lib.rs` 约 20 行；单张 CPU 编码变慢（线程变少），但并行墙钟时间改善。

**推荐等级**：★★★☆ 与方案 K 同属"并行后收尾"。

**落地结果（2026-09-06，含两轮负结果）**：`src/lib.rs` 新增 `rav1e_threads()`，从环境变量读取线程池上限（默认 0 = 全核，单进程行为不变），alpha（monochrome）读 `NVAVIF_RAV1E_THREADS`、颜色 CPU 编码读 `NVAVIF_RAV1E_THREADS_COLOR`。实测迭代（全量 79 张）：

| 配置 | 墙钟 | 结论 |
|---|---:|---|
| 基线（无上限，文件名序） | 45.0 s | 单次记录 |
| 一切按 cores/workers=3 上限 | 64.6 s | **明确倒退** |
| 颜色按 cores/超大图数=7 上限 | 54.8 s | 仍然倒退 |
| 仅 alpha 上限 3（最终版） | 39.7 / 51.1 / 45.6 / 51.0 s（4 次） | 与基线在噪声内 |

**关键教训**：超大图编码的总 CPU 工作量不变，操作系统分时对 4×28 线程的争用打包得足够好（单张 16→40 s 的延迟回归不影响墙钟）；任何低于 cores/实际并发数的硬上限都会让长尾任务吃不饱核、闲置其余核心，是稳定倒退。同配置重复运行的机器噪声高达 ±25%（39.7~51.1 s），单次对比无法分辨 <25% 的差异。最终保留：alpha 上限（多而短的 alpha 任务在多 worker 下的超额订阅保护），颜色回退**不设上限**。

### 方案 K：长作业优先任务排序（新增 2026-09-06）

**问题**：任务按文件名顺序提交，最慢的图（超大图 CPU 回退、透明图）若排在队尾，收尾时段只有一个 worker 在跑，其余空闲——典型的 LPT（longest-processing-time）调度缺失。

**思路**：主进程用 Pillow 头信息（零解码成本）估算每张图的相对代价：基础 = 像素量；宽或高 >8192 ×200（全 CPU 路径，实测 ~0.16 s/MP vs GPU ~0.0003 s/MP）；真透明 ×8（额外一次 CPU alpha 编码）。按代价降序提交。

**代价**：`compress_dir.py` 约 30 行，无库改动，纯编排层。

**推荐等级**：★★★★ 立即可做，零风险。

**落地结果（2026-09-06）**：`compress_dir.py` 新增 `probe_cost()`（Pillow 头信息估算：像素量基础，超大 ×200、真透明 ×8），任务列表按 cost 降序后提交；同时算出 n_oversize / n_alpha_avif 供方案 J 的额度计算使用，报表含 `rav1e_threads_alpha_per_worker` 字段。本语料上墙钟与基线在噪声内（超大图仅 4 张，8 worker 下无论如何都会在第一波被领走），无回退风险，保留。

### 方案 L：auto_cq 探测成本（新增 2026-09-06，已证伪——收益趋近于零）

**原假设**：`--auto-quality` 路径每张图要做锚点试编码 + 动态探测 + 正式编码，约 3× 编码成本。

**读码证伪（2026-09-06）**：两次试编码不在全分辨率图上，而是在 `prepare_trial_frame_8bit` 拼出的 **512×512 区域马赛克**上（`trial_dim=512`，`src/lib.rs` `estimate_cq`）。每次试编码 = 小图编码（GPU 几 ms / CPU speed 10 几十 ms）+ dav1d 解码 + SSIM，总计约几十毫秒，相对全图编码（0.1~40 s）可忽略。随后按两点割线外推 CQ，全分辨率正式编码仅一次。

**结论**：auto_cq 的额外成本已经是 O(小常数)，无可挖的优化空间。除非未来把试编码改成全分辨率（没有必要），本方案关闭。

### 方案 M：有选择地恢复 rav1e asm（新增 2026-09-06）

**问题**：修复 asm 静默损坏（DEVELOPMENT_NOTES 11.4）的方式是整个 crate 关掉 `asm` 特性，CPU 编码为此付出 **10~25% 速度**。

**已知事实**：损坏只出现在 monochrome (Cs400) + 硬边缘遮罩内容，根因在 nasm 构建的某个 SIMD kernel（cdef.rs:95 debug_assert 可复现触发）；颜色流（Cs420/Cs444）的输出此前经 ffmpeg 逐像素校验一致。

**思路**（任选其一）：
1. 向 rav1e 上游定位并修复/绕开具体 kernel，之后整体恢复 `asm`；
2. 只在 Cs400 编码路径禁用受影响的 SIMD 入口（如上游暴露 per-kernel 开关，或 fork 打补丁）；
3. 双版本构建：颜色用带 asm 的 rav1e、alpha 用无 asm 的 rav1e——crate 同名不可行，需 vendor 改名，维护成本高。

**收益**：仍在 CPU 上的路径（超大图回退、透明图 alpha AVIF、`device="cpu"`）直接提速 10~25%。

**推荐等级**：★★☆ 需上游协作或 fork；优先在 rav1e issue 跟踪器上报告 11.4 的复现用例。

### 方案 N：按图库构成自适应 worker 数（新增 2026-09-06）

**问题**：文档现行经验是"透明占比 >30% 时 worker = 核数/4"，但靠人工判断。worker 数与图库构成（透明/超大占比）共同决定 CPU 竞争程度，固定默认值在某些混合库上偏大。

**思路**：`compress_dir.py` 已有 `probe_cost()` 的头信息扫描，可顺带统计透明/超大占比：透明或超大占比 >30% 时自动下调 worker（如核数/4）并相应调高单 worker 线程额度；纯不透明图库保持现行默认。

**代价**：约 15 行 Python，无库改动。

**推荐等级**：★★☆ 需要多组图库的实测数据支撑阈值，先用 J/K 的全量报表积累经验再定。

---

## 5. 优先级排序

| 优先级 | 方案 | 收益 | 风险 | 工作量 |
|---|---|---|---|---|
| ~~**P0**~~ ✅ | 方案 H：进程级并行管线 | **已落地：4.57×（205.5s→45.0s）** | 低（不动库） | ~100 行 |
| ~~**P0**~~ ✅ | 方案 D：尺寸预检查 | 由 I1 的尺寸路由覆盖 | 零 | — |
| ~~**P1**~~ ✅ | 方案 I1：超大图 CPU 回退快速 preset | **已落地：3.04×（49.4s→16.2s）** | 低（SSIM -0.012，可用 auto_cq 补偿） | ~10 行 |
| ~~**P0**~~ ✅ | 方案 J：rav1e 线程额度切分 | **已落地（2026-09-06）**：alpha 额度保留；颜色回退上限实测倒退，弃用（负结果入库） | 低 | ~20 行 |
| ~~**P0**~~ ✅ | 方案 K：长作业优先任务排序 | **已落地（2026-09-06）**：本语料墙钟中性，无回退风险，保留 | 零（纯编排） | ~30 行 |
| **P2** | 方案 A：Alpha 下采样 | alpha 4× 加速 | 低（需验证 AVIF 规范兼容性） | ~50 行 |
| ~~**P1**~~ ❌ | 方案 L：auto_cq 探测成本 | **已证伪（2026-09-06）**：试编码在 512×512 马赛克上，开销几十 ms，无空间 | — | — |
| **P2** | 方案 I2：AVIF grid 分块 | 超大图再 4~10× | 中 | ~300 行 |
| **P2** | 方案 E：库内批量 pipeline | 并行后再收尾 1.5~2× | 中 | ~200–300 行 |
| **P2** | 方案 M：选择性恢复 rav1e asm | CPU 路径 10~25% | 中（需上游协作或 fork） | 不定 |
| **P3** | 方案 N：按图库构成自适应 worker 数 | 混合库吞吐小幅提升 | 低 | ~15 行 |
| **P3** | 方案 B：NVENC 原生 monochrome | alpha 级别 GPU 加速 | 中高 | ~400 行 + 平台测试 |
| **P3** | 方案 G：NVDEC 解码 | 解码 10× 加速 | 中 | ~300 行重构 |

> 注：方案 A 的优先级在 `--transparent-format webp` 路由落地后实质下降——批量脚本中的透明图已默认可绕开 AVIF alpha 路径，A 仅在"透明图必须出 AVIF"时有价值。

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
3. ~~**可选微调**：多 worker 并行时超大图的 rav1e 抢核~~ ✅ 已落地（2026-09-06，方案 J + K）：`NVAVIF_RAV1E_THREADS` alpha 额度 + 头信息代价降序提交。**实测为墙钟中性**（本语料 4 次重复 39.7~51.1 s vs 单次基线 45.0 s，机器噪声 ±25% 掩盖小差异）；附带的重要负结果：给超大图颜色回退设线程上限是稳定倒退（45.0→54.8/64.6 s），勿再尝试。若需验证真实差异，需多次重复取中位数。
4. **透明图生产路径**：rav1e alpha 高速档已落地（2026-09-05，`ALPHA_RAV1E_PRESET` speed 9）：61.2 MP 透明测试集 93.7 s → 5.95~9.8 s（9.6~15.7×，0.65 → 6.25~10.3 MP/s），文件 +10~25%，alpha MAE 仍在 0.1~0.7/255 量级。**2026-09-06 起批量脚本默认把真透明图整体路由 WebP**（`--transparent-format` 默认 avif → webp，颜色+alpha 全离 CPU，3.5 MB/5.0 s vs 6.1 MB/12.3 s；alpha 全 255 的假透明 RGBA 仍走 GPU AVIF）。剩余方向：方案 A（alpha 下采样）仅在"透明图必须出 AVIF"时有价值。两张测试图 alpha 解码错误已修复（2026-09-06，根因是 rav1e `asm` 特性静默损坏单色码流，修复后全集 alpha MAE ≤0.054，无 asm 损失约 10~25% CPU 编码速度，见 DEVELOPMENT_NOTES 11.4；恢复路径见方案 M）。
5. **H+I1 落地后再评估**：方案 I2（grid 分块，需自写 grid 封装）与方案 E（库内批量 session）——多进程并行可能已吃掉大部分收益，按残余瓶颈决定。
6. **其他硬件**：只有 probe 报告 `AV1_MONOCHROME_CAPABILITY=1` 时，才实施方案 B。
7. **持续**：方案 G（解码加速）作为独立优化路径。方案 L 已证伪关闭（2026-09-06）：auto_cq 试编码在 512×512 马赛克上，开销可忽略。
