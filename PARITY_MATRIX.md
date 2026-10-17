# 方案 R parity 验收矩阵（Rust 原生 CLI 的验收标准）

> 目的：方案 R（Rust 原生 CLI `nvavif-batch`，见 OPTIMIZATION_PROPOSALS §4 方案 R）触发时，
> 本文档是**唯一验收标准**——"功能 parity 以现有 compress_dir.py 为准"由此落地为逐条可核对的
> 行为清单。参考实现：`uvtest/compress_dir.py` + `source/nvavif_py/__init__.py`（encode_file 包装层）。
>
> 维护规则：参考实现的每次行为变更都必须同步更新本表（新增行或改写期望值），否则 parity 检查失效。
> 创建于 2026-09-06（对应 compress_dir.py 的 9b953f9 + EXIF/ICC 透传修复）。

## 当前批处理默认策略（2026-10-17）

- 默认固定 CQ：>8 MP 用 `--cq` 30，≤8 MP 用 `--mid-cq` 36；`--auto-quality TARGET` 显式启用自动标定，`--fixed-cq` 可覆盖配置文件中的自动模式；两者同时显式传入报错。`--no-fixed-cq` 在未指定目标时使用 88。
  > **2026-10-17 调整**：默认固定 CQ 先由 20 改为 28，再改为 **30**（>8 MP）。53 张全量实测（`compare_runs.py`）相对 WebP Q90：CQ20 体积 1.58× / LPIPS 0.0101，CQ28 1.35× / 0.0116（WebP 0.0165）——体积省 15%，画质仍在肉眼不可分区。CQ30 在 `test_imgs` 全量上的体积为 WebP Q90 的 1.32×（8–16 MP）/ 1.30×（16–32 MP）/ 1.17×（>32 MP，3 张）；用户肉眼比对认为与 WebP Q90 的差距较小（2026-10-17）。新纳入 AVIF 的 2–8 MP 区间用 **CQ36**，体积与 WebP Q90 对齐（2–4 MP 1.06×、8–16 MP 1.13×）；CQ36 在 16–32 MP 只有 2/11 张 LPIPS 优于 WebP，故 >8 MP 不降档。
- `--opaque-format auto` 为默认：普通不透明图像素数 ≤ `--opaque-webp-max-mp`（默认 **2 MP**）走 WebP Q90，以上走 GPU AVIF（≤8 MP 用 `mid_cq`，>8 MP 用 `cq`）。阈值 0 关闭自动 WebP；显式 avif/webp 不受阈值限制。透明和 oversize 分支优先，原有小图/低质量 JPEG 跳过规则保留。2 MP 是实测速度交点，但随内容漂移。
  > **2026-10-17 补充（非 parity 项）**：初测（AVIF 全 miss）显示 8 MP 是交点；命中实测修正后，`--same-size` 得到命中成本 1.23 MP 14.8 ms、1.75 MP 33.8 ms、2.39 MP 32.1 ms、3.47/3.48 MP 38.2/68.0 ms（miss 开销随分辨率 207→360 ms）。按真实图库命中下限（≤2 MP 75.6%、2–4 MP 84.7%）重算：1.75 MP 仍略慢，2.39 MP 快 1.75×、3.47 MP 快 1.72× → 交叉点 1.75–2.39 MP，默认取 2 MP。4–8 MP 本语料无样本，速度与体积均夹在相邻桶之间（1.06–1.13×）。
- 任务和报表记录 `opaque_webp_max_mp`，并纳入续跑配置比较。默认质量与路由的变化使旧 kept_source 缓存失效。
- 长生命周期进程池与 Rust NVENC context LRU 已实现，本轮沿用；显式 workers 也限制在 1–8，确保 worker 数 × context 容量 ≤8，每 worker 10000 任务后回收。

## 1. 路由决策表（输入条件 → 输出）

判定顺序即下表顺序；`webp_fits` = 编码后长边 ≤ 16383；`oversize` = 原始宽或高 > 8192。
"真透明" = RGBA 转换后 alpha 通道 min < 255（常量不透明 RGBA **不算**透明）。

| # | 输入条件 | 输出 | 关键参数 |
|---|---|---|---|
| R1 | 真透明 & `--transparent-format webp`(默认) & webp_fits | 整图 WebP | q=`--webp-quality`(90)，method=`--webp-method`（默认 4；oversize 时取 `min(…,2)`）；源 ≤ `--webp-lossless-max-mb`(1MB) 时无损 |
| R2 | 真透明 & `--transparent-format png` | 整图 PNG | 无损 |
| R3 | 真透明 & (`--transparent-format avif` 或 >16383 且未缩放) | `skipped_slow`，保留源 | 不进入 CPU alpha 或超限 AVIF；不得转 RGB 丢 alpha |
| R4 | oversize & `--oversize-format webp`(默认) & webp_fits | 整图 WebP | q=`--oversize-webp-quality`(80)，method=`min(--oversize-webp-method,2)`（默认 2） |
| R5 | oversize & (缩放后仍 >16383 或 `--oversize-format avif`) | `skipped_slow`，保留源 | 头信息即可判定，无需整图解码；批处理不进入 rav1e 彩色回退 |
| R6 | `--oversize-max-edge N` 且长边 > N 且路由为 webp | LANCZOS 等比缩到 N 后按 R1/R2/R4 走 | 逐图记录 `resized_from`；AVIF 路由永不缩放 |
| R7a | 不透明/常量不透明 RGBA、非 oversize、`--opaque-format auto` 且像素数 ≤ `--opaque-webp-max-mp`(2 MP) | 整图 WebP | q=`--opaque-webp-quality`(90)，method=`--webp-method`（默认 4）；阈值 0 关闭自动 WebP |
| R7b | 不透明/常量不透明 RGBA、非 oversize、(`auto` 且 > 阈值，或显式 `--opaque-format avif`) | GPU AVIF | cq=`--mid-cq`(36)（≤8 MP）或 `--cq`(30)（>8 MP）；`--auto-quality TARGET` 时逐图标定并忽略两者 |
| R8 | 显式 `--opaque-format webp` 且非 oversize & webp_fits | 整图 WebP | q=`--opaque-webp-quality`(90)，method=`--webp-method`（默认 4）；不透明源以 **RGB** 编码，不再引入全不透明 alpha 平面（2026-09-13）；真透明走 R1 仍保留 alpha |
| R9 | JPEG 且估计 IJG 质量 < `--min-jpeg-quality`(85) | 跳过（`skipped_quality_jpeg`），默认拷贝原样 | 量化表估计失败(q=0)不跳过 |
| R10 | GIF 或动图 WebP（头信息探测） | 跳过（`skipped_animated`），默认拷贝原样 | 静帧管线会拍扁动图 |
| R11 | `--keep-smaller`(默认 on) 且输出 ≥ 源大小 | 保留源（`action=kept_source`），不写输出 | in-place 时临时文件删除、源不动 |
| R12 | 宽 < `NVENC_MIN_WIDTH`(130) 或高 < `NVENC_MIN_HEIGHT`(66)（RTX 4070 实测，2026-09-13） | 扫描阶段跳过（`skipped_small`），`--copy-skipped` 时照常拷贝 | 不再为亚最小尺寸源走 CPU rav1e 回落（GPU 优先策略）；宽/高 =0（头不可读）不跳过 |
| R13 | 非 `--overwrite`，流式记录的源/目标绝对路径、续跑版本、有效编码/筛选配置及源 `size + mtime_ns` 均匹配 | 最新 `kept_source` 直接跳过，计入 `summary.rerun_skipped_prev_kept`；启用 copy-skipped 时缺失/过期副本只补拷贝 | 配置或源变化、旧版缺指纹记录、最新 failed 均重新处理，不受旧复制输出的 exists 判断遮蔽；workers/线程数/copy-skipped/limit 不使编码结论失效 |

批处理快速策略（2026-10-17）：WebP 非 oversize 路由用请求值（默认 4），oversize 实际 method = min(请求值, 2)（method≥3 在大图上会爆炸）；AVIF 一律严格 `device=gpu`，显式 cpu 的 AVIF 任务记 skipped_slow；GPU 失败记 failed 并按 copy-skipped 保留源，不重试 CPU。未知尺寸直接 skipped_slow。默认透明 WebP、低体积源自动无损、auto-CQ 标定保留；auto-CQ 的 GPU 失败同样不回退 rav1e。skipped_slow 含 name/reason/src_bytes/source_fingerprint，计入 summary.kept_source_bigger，可按 R13 复用，续跑版本为 2。先做 exists/续跑筛选，仅剩余候选探测图片头，保留 >64 候选的并行探测与长任务优先排序。

注意：R6 的缩放尺寸必须基于**转正后**的像素尺寸（见 M2）——90° 方向标记的长边在缩放前后会交换宽高。

## 2. 元数据与像素正确性（M 系列）

| # | 行为 | 期望 |
|---|---|---|
| M1 | 源带 EXIF 方向标记（任意路由） | 像素物理转正（`exif_transpose` 语义）；输出 EXIF 不含方向标记（或整个 EXIF 缺省），看图软件不再二次旋转 |
| M2 | 转正导致宽高交换 | 报表 `width/height/megapixels` 为**编码后**尺寸；`resized_from` 为转正后、缩放前的尺寸 |
| M3 | 源带 ICC profile，走 WebP/PNG 路由 | ICC 原样嵌入输出（不转像素、不丢配置）；无损 WebP 像素逐字节不变 |
| M4 | 源带 ICC profile，走 AVIF 路由 | 像素转换到 sRGB；与 sRGB 等效的 profile（探针最大偏差 ≤1 LSB）**跳过**整图转换，像素保持原样（2026-10-14 起）；avif-serialize 不能嵌 profile（当前上游能力边界） |
| M5 | AVIF 路由的原始 EXIF 字节 | **透传**（2026-10-14 起）：Exif item payload = 4 字节大端 TIFF 头偏移 + TIFF（剥掉 JPEG/WebP 的 `Exif\0\0` 前缀）；像素已物理转正，故移除 Orientation 标记；布局无法识别则整块丢弃（libavif 遇畸形 item 会以 "Invalid Exif payload" 拒绝**整个文件**，宁可无 EXIF） |
| M6 | 奇数宽/高（AVIF 路由） | 按 NVENC 偶数约束裁 1px |
| M7 | Pillow 解码炸弹保护 | 关闭（超大摄影图为合法输入） |
| M8 | 调色板/L/IA/LA/F 等模式的输入 | 按参考实现的模式转换矩阵归一到 RGB/RGBA（见 encode_file wrapper） |
| M9 | auto_cq 逐图标定结果留档 | AVIF 行记录 `cq` = 实际使用的颜色面 CQ（auto 时为标定值，固定 CQ 时等于请求值；`encode_file(with_cq=True)` 获取） |
| M10 | 各路由的元数据承载 | WebP/PNG：EXIF + ICC + XMP 原样嵌入（宽色域保 profile）。AVIF：仅 EXIF（去 Orientation）；**ICC/XMP 容器装不下**（avif-serialize 只暴露 `set_exif`，且像素已转 sRGB），缺失项逐图记入 report 行的 `dropped_meta`（无缺失则省略） |

> M4 补充（2026-10-14）：lcms 把 8-bit 样本经 16-bit PCS 往返，真正 sRGB 变体在探针上仍有极少数样本偏 1 LSB（实测 13056 个探针中 32 个），故恒等判据取"最大偏差 ≤1 LSB"而非逐字节相等。实测 18 张带 profile 的源中 17 张判为 sRGB 并跳过转换（唯一例外为 Skia profile，探针偏差 116）；跳过后的像素相对"总是转换"路径 16/17 逐字节相同，1 张仅 1 个通道样本偏 1 LSB。

> M1 补充（2026-10-17，非 parity 项）：方向转正改为按需调用——方向为 1 时不再走 `exif_transpose`（它此时只做一次全图 `copy()`）；偶数尺寸 RGB 源的像素缓冲直接从 PIL 交给 Rust，省掉两次中间拷贝。剥离方向标记的语义不变，`encode_file` 的输出对全部 78 张语料逐字节不变，故 M1 的可观测行为未变，此处仅记录实现约束（未来 Rust CLI 重写无需照搬这两项）。

## 3. 文件系统行为（F 系列）

| # | 行为 | 期望 |
|---|---|---|
| F1 | 递归扫描（默认 on） | 输出镜像源相对目录结构；`--no-recursive` 只扫顶层 |
| F2 | 同目录同 stem 不同后缀（a.png + a.jpg） | 后一个输出名追加源后缀（`a_jpg.avif`）；不同子目录可复用 stem |
| F3 | 输出已存在且无 `--overwrite` | 跳过；有 kept_source/failed 记录时先按 R13 校验，防止复制源遮蔽失效记录；in-place 时排除源自身。已有 encoded 输出仍按存在跳过，重编码用 `--overwrite` |
| F4 | in-place 替换 | 先写临时文件再 `os.replace`；**源字节数在写入前捕获**（png/webp 源与输出同名会污染 keep-smaller 判断）；编码失败不得动源 |
| F5 | in-place + keep-smaller 触发 | 临时文件删除，源原样保留（`kept_source`） |
| F6 | in-place + AVIF 路由成功 | 输出后缀 `.avif` 不与任何源后缀相同，直接删源 |
| F7 | `--copy-skipped`(默认 on，in-place 下无效) | AVIF/WebP/PNG 的 kept_source、`skipped_slow`、跳过 JPEG/动图/小图及**编码失败**的源全部原样拷入输出；先创建**源对应的镜像子目录**（不得平铺到输出根） |
| F8 | 汇总/自拷贝路径 | summary 与 copy-skipped 的输出计数在 in-place 下保持一致 |
| F9 | 编码输出的文件时间 | 输出**修改时间** = 源 mtime（`os.utime`）；Windows 上**创建时间**另需 `SetFileTime`（源 `st_ctime`）。源 stat 在写盘前捕获、in-place 时早于删源；WebP/PNG/AVIF 各路由一致。（atime 亦随 `os.utime` 写入，但源在编码前已被读取，其 atime 已刷新为编码时刻，故输出 atime ≈ 编码时刻，非源原始 atime） |

编码落盘（所有格式，含非 in-place）：先写同目录 `<输出文件名>.tmp`，完成且通过 keep-smaller 后 `os.replace`；异常清理临时文件，已有正式输出不因编码/替换失败而删除。强制终止留下的 `.tmp` 不作为正式输出参与续跑判断。

## 4. 并行与调度（P 系列）

| # | 行为 | 期望 |
|---|---|---|
| P1 | worker 数 | 默认 min(8, 核数/2)，硬上限 = NVENC 会话数预算；每 worker 任意时刻 ≤1 个 NVENC 会话。上下文 LRU 复用（方案 Q 已落地）：容量 = max(1, 8//workers) 经 `NVAVIF_CTX_CACHE` 下发，miss 路径必须**先驱逐再开**，恒有 workers × 容量 ≤ 8 |
| P2 | 提交顺序 | 头信息代价降序（LPT）：像素量基础，oversize ×200，真透明 ×8；代价相等时再按 `(width, height)` 降序，使同尺寸任务连续（避免同 MP 不同尺寸打断 worker 的上下文缓存） |
| P3 | rav1e 线程额度 | alpha 读 `NVAVIF_RAV1E_THREADS`（默认 cores/workers），颜色回退读 `NVAVIF_RAV1E_THREADS_COLOR`（默认不设限——实测设限倒退，方案 J） |
| P4 | 单 worker 失败 | 不中断整批，计入 `summary.failures`，源按 F7 处理 |
| P5 | 百万级长跑编排 | 头探针线程池并行（>64 文件时启用）；滑动窗口提交（在飞 ≤ 4×workers，主进程内存与语料规模无关）；逐行流式报表 `<report>.stream.jsonl`（中断/崩溃留逐图记录）；采样器 >2 万样本后间隔倍增（上限 5 s）；worker 每 10000 任务重生。Ctrl+C 中断仍写完整格式 .json 报表，顶层多 `"interrupted": true`，重跑同参数即续传 |

## 5. 报表 schema（`compress_report.json`）

顶层：`generated_at_utc, src, dst, cq, mid_cq, auto_quality, device, preset, workers,
min_jpeg_quality, keep_smaller, copy_skipped, in_place, transparent_format, webp_quality,
webp_method, webp_lossless_max_mb, opaque_format, opaque_webp_quality, opaque_webp_max_mp,
oversize_format, oversize_webp_quality, oversize_webp_method, oversize_max_edge,
alpha_rav1e_threads, color_rav1e_threads, ctx_cache_per_worker, summary, resource,
skipped_quality_jpeg[], skipped_animated[], skipped_small[], images[]` + 可选 `interrupted`
（P5 中断标记）。
附属文件：`<report>.stream.jsonl`——追加式多轮日志，每轮任务前写 run 元信息（绝对 `src/dst`、`resume_version`、`resume_config`），之后每完成一图一行并刷新 Python 缓冲（不逐图 fsync）。逐行读取、同名最后记录生效，截断/畸形 JSON 行忽略；新轮前换行隔离残缺尾行。无任务时保留原日志。
成功处理行附带 `source_fingerprint: {size, mtime_ns}`；失败行 `{"index, name, action: "failed", error"}`（P5）覆盖此前 kept_source 结论。指纹不读取文件内容：同大小且 mtime 被刻意还原的内容变化无法检出，此时用 `--overwrite`。

逐图行（encoded）：`name, action, format, cq, mode, alpha, lossless, width, height, megapixels,
src_bytes, out_bytes, ratio, bits_per_pixel, encode_s, mp_per_s` + 可选 `resized_from`。
逐图行（kept_source）：`name, action, format, cq, src_bytes, avif_bytes, encode_s`。
（`cq` 仅 AVIF 路由行存在 = 实际使用的颜色面 CQ；WebP/PNG 行无此字段。）
summary 键：`total, encoded, kept_source_bigger, skipped_low_quality_jpeg, skipped_animated,
skipped_small, rerun_skipped_prev_kept, resized, elapsed_s, total_megapixels,
aggregate_mp_per_s, encoded_src_mb, encoded_out_mb, encoded_ratio, final_store_mb, failures`。

**parity 的机器可验证部分**：同一语料、同一配置下，Rust 版与 Python 版的报表除时间/字节数/资源采样外，
`images[].name` 集合、逐图 `format`、`action`、`resized_from` 有无必须完全一致（路由决策一致性）；
质量回归用 `uvtest/compare_runs.py` 按报表配对（勿按后缀猜文件）。

## 6. CLI 面（完整清单，`--write-config` 生成的键集）

src, recursive, dst, cq, mid-cq, auto-quality, fixed-cq, device, preset, workers, limit,
min-jpeg-quality, keep-smaller, copy-skipped, in-place, transparent-format, webp-quality,
webp-method, webp-lossless-max-mb, oversize-format, oversize-webp-quality,
oversize-webp-method, opaque-format, opaque-webp-quality, opaque-webp-max-mp,
oversize-max-edge, alpha-rav1e-threads, color-rav1e-threads, overwrite, report, config,
write-config

约定：CLI 传参 > config 文件 > 默认值；config 未知键必须报错；JSON null = 用默认。
**CLI 默认值与 `compress_config.json` 对齐（2026-10-17）**：固定 CQ 为默认（`cq` 30、`mid-cq` 36、
`opaque-webp-max-mp` 2、`oversize-max-edge` 16383）；`auto-quality` 默认关闭（null），
`--no-fixed-cq` 在未指定目标时用 88；裸跑 = 生产行为。

## 7. 诊断计时（2026-10-14）

`NVAVIF_DEBUG_TIMING=1` 时，库输出 Python 输入预处理、数组归一化/字节化、RGB→YUV、NVENC context 新建/命中/淘汰/关闭、主机帧分配拷贝、send/receive 的毫秒计时；未启用时不输出这些诊断。返回值、编码参数和路由保持不变。`uvtest/measure_ctx_overhead.py` 用严格 GPU 子进程运行，探测预热不计入逐图统计，记录原始事件与 hit/miss 数量；缺少必需阶段时失败，不能将旧 wheel 的缺失计时记为零。

这些是主机墙钟观测：主机帧拷贝不是 GPU 上传，send/receive 包含 FFmpeg/驱动、上传、编码和同步，不能拆称纯 GPU 时间。Python input 含解码、ICC/方向/模式处理及 NumPy 转换，并另列这三个子阶段（已包含在 input 中，不得重复加和）；native other 是未分配余量，auto-CQ 时还含校准工作。各重复轮次的阶段均值分别取中位数，不保证逐列加和严格等于总计。与生产并行吞吐分开解读。

## 8. 已知边界（parity 目标之外，显式记录）

- 动图（GIF/动图 WebP）不支持，跳过并计数。
- YUV444 输入在本机 AV1 NVENC 不可用（必然 CPU 回退），批量管线默认 420 不受影响。
- 评测指标口径见 DEVELOPMENT_NOTES §19（chroma PSNR 为标准 BT.709 矩阵；ΔE2000 的 hue-mean 为简化实现）。
