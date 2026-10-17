# 代码分析与优化方案台账

> 记录 nvavif_py 的性能方案：已落地、已否决、条件触发。历史估算与已被实测推翻的结论只在
> 否决理由中保留一句，避免重复投入。当前实现与运行手册见 `DEVELOPMENT_NOTES.md`，
> 验收规格见 `PARITY_MATRIX.md`。

> **度量口径（metric note）**：本文所有 "SSIM" 均指 8×8 分块、亮度平面、降采样后的
> SSIM（`uvtest/quality_metrics.py`）。两个盲区：不含色度（4:2:0 渗色测不到）；
> **RGBA 必须先合成白底**再进 SSIM（透明像素下方的 RGB 无意义）。分块 SSIM 同时报告
> **最差块数值/坐标**，alpha 报告 **worst-1% 均值**。格式取舍以 **LPIPS（alex）+
> 人工核验**为准，SSIM/PSNR 只作回归报警器；LPIPS 仅在同一网络与同类失真间可比。
> `compare_perceptual.py` 的 chroma PSNR 使用标准 BT.709 矩阵；ΔE2000 的 hue-mean
> 是简化实现，仅限同一路径内相对比较。

---

## 1. 当前架构与瓶颈

```
输入 RGB/RGBA (u8/u16/f32)
   │
   ▼
build_yuv() ── rayon RGB→YUV420/444（YUV444 在本机 NVENC 不可用）
   │
   ├─ 颜色流 ── GPU: av1_nvenc（auto 失败回落 rav1e）
   └─ alpha 流 ─ CPU: rav1e Cs400（无 GPU 路径）
   │
   ▼
avif-serialize → AVIF
```

关键事实：

- 真透明图总耗时 = `max(GPU 颜色, CPU alpha)`，CPU alpha 是结构性瓶颈。批量默认把真透明图
  整体路由 WebP（颜色 + alpha 全离 CPU），只有显式 `--transparent-format avif` 才回到双流。
- NVENC 上限 8192/轴、最小 130×66；WebP 上限 16383/轴。批量脚本据此路由，库本身不缩放。
- 批量默认不透明路由 `auto`：≤2 MP → WebP Q90；2–8 MP → AVIF CQ36；>8 MP → AVIF CQ30。
  2 MP 是实测速度交叉点（NVENC 上下文打开 ~80 ms）。
- 真透明 WebP q90 vs AVIF 双流：3.5 MB/5.0 s vs 6.1 MB/12.3 s；合成白底后颜色 SSIM
  0.997~0.9996 vs 0.998~0.9997（平手），alpha 前者严格无损。Windows 看图软件不合成
  AVIF/HEIC alpha（全黑底），WebP 无此问题——透明图默认 WebP 无画质代价。
- 解码（dav1d）远快于编码，不是当前瓶颈；`device="cpu"` 与超大图回退才走 rav1e。

---

## 2. 方案状态总表

| 方案 | 内容 | 状态 | 关键结论 |
|---|---|---|---|
| H | 进程级并行压缩管线 | 已落地 | 205.5 s → 45.0 s（4.57×）；NVENC 占用 11% → 100% |
| D | NVENC 尺寸预检查 | 已落地（并入路由） | 超限图提交前直接走对应路由，不再撞初始化失败 |
| I1 | 超大图 CPU 回退快速 preset | 已落地 | 49.4 s → 16.2 s（3.04×），SSIM −0.012 |
| I3 | 超大图显式缩放 | 已落地（opt-in） | `--oversize-max-edge 16383`，默认开启；101.7 MP 缩放 ~2.3 s |
| I4 | 超大图路由 WebP | 已落地（默认） | 36~39 s → 2~13 s；全量 205.5 s → 6.7~7.6 s |
| J | rav1e 线程额度切分 | 已落地（仅 alpha） | 颜色回退设上限稳定倒退；alpha 上限保留 |
| K | 长作业优先排序 | 已落地 | 本语料墙钟中性，无回退风险 |
| Q | NVENC 上下文 LRU 复用 | 已落地 | 打开实测 ~80 ms；同尺寸小图 25×；输出逐字节一致 |
| O | 评测指标精度 | 已落地 | BT.709 chroma、alpha worst-1%、SSIM 最差块坐标 |
| P | 全 WebP 管线对比 | 已实测 | 与 GPU AVIF 画质持平；速度 3.0× 快差、体积 1.57× 小 |
| A | alpha 下采样 | 条件触发 | 仅"透明图必须出 AVIF"时有价值；需验证兼容性 |
| B | NVENC 原生 monochrome | 否决（硬件） | 本机 `AV1_MONOCHROME_CAPABILITY=0` |
| B2/B3 | HEVC alpha / 双单层 HEIC | 否决（生态） | 容器可行，但查看器不合成 alpha，非通用路径 |
| C | NV12 伪装 YUV400 | 否决 | 不符合规范，跨解码器结果不可靠 |
| E | 库内批量 pipeline | 否决（前提消失） | H 已打满 NVENC，session 固定开销另由 Q 解决 |
| F | CUDA 加速 YUV 转换 | 否决 | YUV 转换不是瓶颈，引入 CUDA 依赖不划算 |
| G | NVDEC 解码 | 条件触发 | 解码 10× 但改造大；当前解码不是瓶颈 |
| I2 | AVIF grid 分块 | 条件触发 | 仅出现 >16383 图或必须 AVIF 超大图时重启 |
| L | auto_cq 探测成本 | 已证伪 | 试编码在 512×512 马赛克上，开销几十 ms |
| M | 选择性恢复 rav1e asm | 条件触发 | CPU 路径 +10~25%，但需上游修复 asm 损坏 |
| N | 自适应 worker 数 | 条件触发 | 需多图库数据支撑阈值 |
| R | Rust 原生 CLI | 条件触发 | 仅在单二进制/无人值守需求出现时做，只重写编排层 |

---

## 3. 已落地方案

### H：进程级并行管线（2026-09-05）

单进程串行下 NVENC 平均占用仅 0.1%、GPU 9%，瓶颈是调度不是算力。`compress_dir.py` 改为
`ProcessPoolExecutor`：每 worker 独立注册 DLL、按 `min(8, 核数/2)` 起步、NVENC 会话失败
自动降级 CPU、逐图 rows 汇总进报表。全量 79 张 **205.5 s → 45.0 s（4.57×）**，不透明冒烟
99 MP/s。次级问题（多 worker 抢核）由 J 处理。

### D：尺寸预检查

超限图在提交前按尺寸路由（批处理的 oversize 分支），不再先撞一次 NVENC 初始化失败。

### I1：超大图 CPU 回退快速 preset（2026-09-05）

仅当宽或高 >8192 时 CPU 回退使用 `OVERSIZE_RAV1E_PRESET = 4`（rav1e speed 7），普通图的
瞬时 NVENC 失败保持原 preset。这是**库层**行为；批处理不做 AVIF CPU 回退。101.7 MP 实测
**49.4 s → 16.2 s（3.04×）**，文件 −13.5%，SSIM 0.9355 → 0.9233。

### I3：显式缩放（2026-09-06，默认开启）

`--oversize-max-edge N`（默认 16383）：长边超限的源图用 LANCZOS 等比缩放后走 WebP 整体
路由，报表逐图记录 `resized_from`。101.7 MP 缩放约 2.3 s，相对 40 s 级 CPU 回退可忽略。
AVIF 路由永不缩放。同时修复了 >16383 透明图直接尝试 WebP 报错的问题，以及 JSON `null`
配置值触发 `float(None)` 崩溃。

### I4：超大图路由 WebP（2026-09-06，默认）

| 编码 | 单张耗时 | 体积 | 亮度 SSIM |
|---|---:|---:|---:|
| AVIF cq20（rav1e speed 7，28 核） | 36~39 s | 2.18~4.24 MB | 0.9943~0.9952 |
| WebP q80（method=2） | 2~13 s | 0.69~2.27 MB | 0.9908~0.9924 |

两个约束：libwebp **method≥3 在部分大图内容上爆炸**（73.jpg：42 s vs 2.3 s，体积只差 10%），
超大图固定 method=2；>16383 既超出 WebP 上限、批处理也不做 AVIF CPU 回退，因此跳过并保留源
（由 I3 显式缩放后才能纳入）。最终全量
**205.5 s → 6.7~7.6 s（27~31×）**，输出 110.5 MB、零失败。画质代价为 SSIM ~0.991 vs
~0.995，均属视觉无损；极限保真用 `--oversize-format avif`。

### J：rav1e 线程额度切分（2026-09-06）

`NVAVIF_RAV1E_THREADS`（alpha）/ `NVAVIF_RAV1E_THREADS_COLOR`（颜色回退）。全量实测：

| 配置 | 墙钟 | 结论 |
|---|---:|---|
| 基线（无上限） | 45.0 s | 单次记录 |
| 全部 cores/workers=3 | 64.6 s | 稳定倒退 |
| 颜色 cores/超大图数=7 | 54.8 s | 稳定倒退 |
| 仅 alpha 上限 3（最终版） | 39.7 / 51.1 / 45.6 / 51.0 s | 噪声内 |

**负结果**：低于实际并发数的硬上限会让长尾任务吃不饱核，是稳定倒退；保留 alpha 上限
（多而短的 alpha 任务防超额订阅），颜色回退不设上限。同配置重复噪声达 ±25%，
单次对比无法分辨 <25% 的差异。

### K：长作业优先排序（2026-09-06）

`probe_cost()` 用 Pillow 头信息估代价：像素量基础，超大 ×200，真透明 ×8，按降序提交。
本语料墙钟中性（超大图仅 4 张，首波即被领走），无回退风险。

### P：全 WebP 管线对比（2026-09-06）

同一份配置、只改 `--opaque-format` / `--auto-quality`，清空输出重跑：

| 指标 | 不透明 → GPU AVIF cq20 | auto_cq 90 | 全 WebP q90 |
|---|---:|---:|---:|
| 墙钟 | 7.9 s（125.9 MP/s） | 11.4 s | 24.0 s（慢 3.0×） |
| 编码输出 | 110.5 MB | 104.8 MB | **73.9 MB**（小 1.57×） |
| 合成白底 SSIM | 0.9972 | 同 cq20 | 0.9969 |
| LPIPS（最差 8 张） | 0.0035 | 0.0041 | 0.0037 |

结论：两者画质持平，只在速度与体积间取舍。auto_cq 在 target 90 时体积仅比 cq20 小 1%。
**方法论教训**：首轮"AVIF 0.9973 vs WebP 0.9896"是陈旧输出污染（按后缀猜文件取到了旧
`.avif`）——跑批前必须清空输出，跨格式对比一律按报表配对（`compare_runs.py` 已改）。

### Q：NVENC 上下文 LRU 复用（2026-09-06）

实测推翻"5~20 ms/开"的估计：**av1_nvenc 上下文打开约 80 ms**，占 512×512 单图墙钟 ~75%；
auto_cq 每图 3 次（2 次试编码 + 1 次全分辨率）。实现为进程内 LRU，key 覆盖尺寸/像素格式/
preset/cq 全部打开选项，容量 `NVAVIF_CTX_CACHE`（默认 1，批量设 `max(1, 8//workers)`）。

- 同尺寸小图 107.9 → 4.3 ms/张（**25×**）；auto_cq（容量 ≥3）335 → 108 ms（3.1×），
  同尺寸 354 → 22.5 ms（15.7×）；尺寸互异 + 固定 cq 语料零收益零伤害，输出逐字节一致。
- **教训 1**：miss 路径必须**先驱逐再开**。"先开后驱逐"让每 worker 瞬时持 2 个会话，
  8 worker 超 8 会话预算，最大图静默回落 CPU（1.65 s → 70 s，输出变小易被误读为压缩率改善）。
- **教训 2**：常驻上下文是拿显存换时间；小显存机器需下调 worker/容量。详见
  `DEVELOPMENT_NOTES.md` §16。

### O：评测指标精度（2026-10-14）

chroma PSNR 换 BT.709；`alpha_mae_worst1()` 并列输出；`ssim_block_stats()` 返回最差块
数值与源像素坐标。mean 类数值语义未变，历史报表可直接比对；只有 chroma PSNR 换矩阵后
数值变化，且未被任何归档结论引用。ΔE2000 的 hue-mean 按辅助指标定位保留。

附带发现：最差块诊断当场暴露 `compare_runs.py` 未合成白底（最差块 −0.26，55 张均值从
0.9972 被拉到 0.9898），已修正；`compare_transparent.py` 同步修正后，WebP 的"结构性
劣势"作废（见 §1）。

---

## 4. 条件触发与已否决方案

### 方案 A：alpha 下采样（条件触发）

2×/4× 下采样后编码 alpha，理论 alpha 耗时降 4×。代价是需要自定义解码路径处理缩放，
非标准操作，第三方解码器可能无法正确还原。**裁决**：批量默认透明图走 WebP 后，A 仅在
"透明图必须出 AVIF"时有价值；届时先验证浏览器/libheif/Pillow 三方的解码一致性。

### 方案 B：NVENC 原生 monochrome（否决，硬件）

NVENC SDK 有 `monoChromeEncoding` 与 `NV_ENC_CAPS_SUPPORT_MONOCHROME`，但本机 RTX 4070
probe 返回 `AV1_MONOCHROME_CAPABILITY=0`，FFmpeg 强制实验也无法产出 YUV400 码流
（gray 输入被转 YUV444 后拒绝；转 yuv420p 得到的是普通 Main/yuv420p）。
Intel UHD 770 的 AV1 输入表同样只有 NV12/P010/AYUV/Y410。
**仅在另一台机器 probe 报告 capability=1 时重新评估**。

### 方案 B2/B3：HEVC alpha layer / 双单层 HEIC（否决，生态）

B2：RTX 4070 的 `HEVC_ALPHA_CAPABILITY=1`，native probe 能把 alpha 编进硬件输出，但
libheif 1.23.1 不支持 layered HEVC 的 `lhv1` item，容器层无解。
B3：改用两条单层 `hevc_nvenc`（颜色 NV12 + alpha 作 luma、色度 128）+ 标准 HEIC 双 item，
libheif/FFmpeg/WIC 三方解码全部通过，编码 0.56 s。**但查看环境实测：Windows 看图软件对
所有 HEIC/AVIF alpha 一律不合成，浏览器不支持 HEIC**——没有正确显示场景，仅适用于
libheif 生态。规范与踩坑见 `DEVELOPMENT_NOTES.md` §14。

### 方案 C：NV12 伪装 YUV400（否决）

U=V=128 编码后把容器项标为 monochrome，部分解码器裁剪、部分不裁剪；不符合 AVIF 规范，
不能作为可靠输出。

### 方案 E：库内批量 pipeline（否决，前提消失）

H 落地后 NVENC 峰值已达 100%，"GPU 空闲窗口"的前提不成立；session 固定开销由 Q 以更小
切口解决，流水线重叠也已由多进程在进程间实现。

### 方案 F：CUDA 加速 YUV 提取（否决）

YUV 转换不是瓶颈（8K 约 50–200 ms），引入 CUDA toolkit、kernel 与平台限制不划算。

### 方案 G：NVDEC 硬件解码（条件触发）

解码 10× 的估算成立，但当前 dav1d 解码不是瓶颈（编码慢一个数量级以上），且解码路径是
自定义 `yuv_to_rgb_parallel`，改造涉及较大重构。等解码成为实际瓶颈再评估。

### 方案 I2：AVIF grid 分块（条件触发）

>8192 时切块逐块 NVENC，按 grid 规范拼回单文件。`avif-serialize` 不支持 grid，需自写
> 封装（~300 行）。I4 落地后超大图走 WebP 只需 2~13 s，增量收益缩水为"几秒/张"。
重启条件：语料出现 **>16383** 的图，或业务要求超大图必须出高保真 AVIF。

### 方案 L：auto_cq 探测成本（证伪）

试编码发生在 512×512 区域马赛克上（`prepare_trial_frame_8bit`），每次几十毫秒，
相对全图编码可忽略；正式编码只有一次。无优化空间，关闭。

### 方案 M：选择性恢复 rav1e asm（条件触发）

`asm` 静默损坏 monochrome + 硬边缘码流（`DEVELOPMENT_NOTES.md` §11.4），整个 crate 关闭
`asm` 使 CPU 编码付出 10~25% 速度。默认批量几乎不触碰 rav1e，收益只在超大图 >16383、
显式 AVIF alpha、`device="cpu"` 与库用户。最低成本动作是向上游提交复现用例；fork/vendor
路线仅在明确 CPU 密集需求时评估。

### 方案 N：自适应 worker 数（条件触发）

用 `probe_cost()` 顺带统计透明/超大占比，超过阈值自动下调 worker。约 15 行，但阈值需要
多组图库实测支撑；当前默认值在既有语料上未出现问题。

### 方案 R：Rust 原生 CLI（条件触发）

动机是**部署形态**（单 .exe、零 Python 依赖），不是速度。只重写编排层（walkdir、image
解码、webp 编码、serde config/report、rayon），Rust 编码核心不动；报表 schema 与 Python
版一致，`compare_runs.py` 可直接做质量回归。**验收基准已固化为 `PARITY_MATRIX.md`**。
代价 ~800–1500 行 Rust + Windows libwebp 构建；EXIF/ICC 透传是独立工作项。
明确不做常驻 daemon（多进程已吃掉编排收益）。

---

## 5. 结论

- **不透明图批量压缩：生产可用**。默认路由 ≤2 MP WebP、2–8 MP CQ36、>8 MP CQ30；
  79 张全量在 2026-09-06 路由下为 6.7~7.6 s，GPU 路径相对 CPU 快 67.6×。
- **真透明图：生产可用**。默认整体路由 WebP，速度/体积/alpha 无损均优于 AVIF 双流；
  AVIF alpha 仅在显式要求时使用，CPU alpha 仍是那里的结构性瓶颈。
- **超大图：生产可用**。默认 WebP（≤16383）；超出时由 I3 缩放到 16383，否则跳过并保留源
  （批处理不做 AVIF CPU 回退）。
- **无未落地的性能项**。剩余方案全部是条件触发，触发条件见 §4；已否决方案的理由保留在
  各自小节，勿重复投入。
- 已知测量噪声：同配置重复运行墙钟可差 ±25%。任何小于该量级的"优化"都需多次重复取
  中位数，或同构建、同会话、背靠背 A/B 才能判定。
