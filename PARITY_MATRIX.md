# 方案 R parity 验收矩阵（Rust 原生 CLI 的验收标准）

> 目的：方案 R（Rust 原生 CLI `nvavif-batch`，见 OPTIMIZATION_PROPOSALS §4 方案 R）触发时，
> 本文档是**唯一验收标准**——"功能 parity 以现有 compress_dir.py 为准"由此落地为逐条可核对的
> 行为清单。参考实现：`uvtest/compress_dir.py` + `source/nvavif_py/__init__.py`（encode_file 包装层）。
>
> 维护规则：参考实现的每次行为变更都必须同步更新本表（新增行或改写期望值），否则 parity 检查失效。
> 创建于 2026-09-06（对应 compress_dir.py 的 9b953f9 + EXIF/ICC 透传修复）。

## 1. 路由决策表（输入条件 → 输出）

判定顺序即下表顺序；`webp_fits` = 编码后长边 ≤ 16383；`oversize` = 原始宽或高 > 8192。
"真透明" = RGBA 转换后 alpha 通道 min < 255（常量不透明 RGBA **不算**透明）。

| # | 输入条件 | 输出 | 关键参数 |
|---|---|---|---|
| R1 | 真透明 & `--transparent-format webp`(默认) & webp_fits | 整图 WebP | q=`--webp-quality`(90)，method=`--webp-method`(4)；源 ≤ `--webp-lossless-max-mb`(1MB) 时无损 |
| R2 | 真透明 & `--transparent-format png` | 整图 PNG | 无损 |
| R3 | 真透明 & (`--transparent-format avif` 或 >16383 且未缩放) | AVIF 双流（GPU 颜色 + CPU alpha） | cq/auto_cq 同 R7 |
| R4 | oversize & `--oversize-format webp`(默认) & webp_fits | 整图 WebP | q=`--oversize-webp-quality`(80)，method=`--oversize-webp-method`(2) |
| R5 | oversize & (>16383 或 `--oversize-format avif`) | AVIF CPU 回退 | `OVERSIZE_RAV1E_PRESET`=speed 7 |
| R6 | `--oversize-max-edge N` 且长边 > N 且路由为 webp | LANCZOS 等比缩到 N 后按 R1/R2/R4 走 | 逐图记录 `resized_from`；AVIF 路由永不缩放 |
| R7 | 其余（不透明、常量不透明 RGBA、非 oversize） | GPU AVIF | cq=`--cq`(20) 或 auto_cq=`--auto-quality` |
| R8 | `--opaque-format webp` 且非 oversize & webp_fits | 整图 WebP | q=`--opaque-webp-quality`(90)，method=`--webp-method` |
| R9 | JPEG 且估计 IJG 质量 < `--min-jpeg-quality`(85) | 跳过（`skipped_quality_jpeg`），默认拷贝原样 | 量化表估计失败(q=0)不跳过 |
| R10 | GIF 或动图 WebP（头信息探测） | 跳过（`skipped_animated`），默认拷贝原样 | 静帧管线会拍扁动图 |
| R11 | `--keep-smaller`(默认 on) 且输出 ≥ 源大小 | 保留源（`action=kept_source`），不写输出 | in-place 时临时文件删除、源不动 |

注意：R6 的缩放尺寸必须基于**转正后**的像素尺寸（见 M2）——90° 方向标记的长边在缩放前后会交换宽高。

## 2. 元数据与像素正确性（M 系列）

| # | 行为 | 期望 |
|---|---|---|
| M1 | 源带 EXIF 方向标记（任意路由） | 像素物理转正（`exif_transpose` 语义）；输出 EXIF 不含方向标记（或整个 EXIF 缺省），看图软件不再二次旋转 |
| M2 | 转正导致宽高交换 | 报表 `width/height/megapixels` 为**编码后**尺寸；`resized_from` 为转正后、缩放前的尺寸 |
| M3 | 源带 ICC profile，走 WebP/PNG 路由 | ICC 原样嵌入输出（不转像素、不丢配置）；无损 WebP 像素逐字节不变 |
| M4 | 源带 ICC profile，走 AVIF 路由 | 像素先转换到 sRGB（`_ensure_srgb`/ImageCms 语义）；avif-serialize 不能嵌 profile（当前上游能力边界） |
| M5 | AVIF 路由的原始 EXIF 字节 | **不透传**（现产品决策：丢弃；如改为主流程决策点，改此行并同步 Python 版） |
| M6 | 奇数宽/高（AVIF 路由） | 按 NVENC 偶数约束裁 1px |
| M7 | Pillow 解码炸弹保护 | 关闭（超大摄影图为合法输入） |
| M8 | 调色板/L/IA/LA/F 等模式的输入 | 按参考实现的模式转换矩阵归一到 RGB/RGBA（见 encode_file wrapper） |
| M9 | auto_cq 逐图标定结果留档 | AVIF 行记录 `cq` = 实际使用的颜色面 CQ（auto 时为标定值，固定 CQ 时等于请求值；`encode_file(with_cq=True)` 获取） |

## 3. 文件系统行为（F 系列）

| # | 行为 | 期望 |
|---|---|---|
| F1 | 递归扫描（默认 on） | 输出镜像源相对目录结构；`--no-recursive` 只扫顶层 |
| F2 | 同目录同 stem 不同后缀（a.png + a.jpg） | 后一个输出名追加源后缀（`a_jpg.avif`）；不同子目录可复用 stem |
| F3 | 输出已存在且无 `--overwrite` | 跳过；in-place 时必须排除源自身（png/webp 源与输出同 stem 同目录） |
| F4 | in-place 替换 | 先写临时文件再 `os.replace`；**源字节数在写入前捕获**（png/webp 源与输出同名会污染 keep-smaller 判断）；编码失败不得动源 |
| F5 | in-place + keep-smaller 触发 | 临时文件删除，源原样保留（`kept_source`） |
| F6 | in-place + AVIF 路由成功 | 输出后缀 `.avif` 不与任何源后缀相同，直接删源 |
| F7 | `--copy-skipped`(默认 on，in-place 下无效) | kept_source/跳过 JPEG/动图/**编码失败**的源全部原样拷入输出；失败源拷入**源对应的镜像子目录**（不得平铺到输出根） |
| F8 | 汇总/自拷贝路径 | summary 与 copy-skipped 的输出计数在 in-place 下保持一致 |

## 4. 并行与调度（P 系列）

| # | 行为 | 期望 |
|---|---|---|
| P1 | worker 数 | 默认 min(8, 核数/2)，硬上限 = NVENC 会话数预算；每 worker 任意时刻 ≤1 个 NVENC 会话。上下文 LRU 复用（方案 Q 已落地）：容量 = max(1, 8//workers) 经 `NVAVIF_CTX_CACHE` 下发，miss 路径必须**先驱逐再开**，恒有 workers × 容量 ≤ 8 |
| P2 | 提交顺序 | 头信息代价降序（LPT）：像素量基础，oversize ×200，真透明 ×8 |
| P3 | rav1e 线程额度 | alpha 读 `NVAVIF_RAV1E_THREADS`（默认 cores/workers），颜色回退读 `NVAVIF_RAV1E_THREADS_COLOR`（默认不设限——实测设限倒退，方案 J） |
| P4 | 单 worker 失败 | 不中断整批，计入 `summary.failures`，源按 F7 处理 |

## 5. 报表 schema（`compress_report.json`）

顶层：`generated_at_utc, src, dst, cq, auto_quality, device, workers, min_jpeg_quality,
keep_smaller, copy_skipped, in_place, transparent_format, webp_quality, webp_method,
webp_lossless_max_mb, opaque_format, opaque_webp_quality, oversize_format,
oversize_webp_quality, oversize_webp_method, oversize_max_edge,
alpha_rav1e_threads, color_rav1e_threads, summary, resource,
skipped_quality_jpeg[], skipped_animated[], images[]`

逐图行（encoded）：`name, action, format, cq, mode, alpha, lossless, width, height, megapixels,
src_bytes, out_bytes, ratio, bits_per_pixel, encode_s, mp_per_s` + 可选 `resized_from`。
逐图行（kept_source）：`name, action, format, cq, src_bytes, avif_bytes, encode_s`。
（`cq` 仅 AVIF 路由行存在 = 实际使用的颜色面 CQ；WebP/PNG 行无此字段。）
summary 键：`total, encoded, kept_source_bigger, skipped_low_quality_jpeg, skipped_animated,
resized, elapsed_s, total_megapixels, aggregate_mp_per_s, encoded_src_mb, encoded_out_mb,
encoded_ratio, final_store_mb, failures`。

**parity 的机器可验证部分**：同一语料、同一配置下，Rust 版与 Python 版的报表除时间/字节数/资源采样外，
`images[].name` 集合、逐图 `format`、`action`、`resized_from` 有无必须完全一致（路由决策一致性）；
质量回归用 `uvtest/compare_runs.py` 按报表配对（勿按后缀猜文件）。

## 6. CLI 面（完整清单，`--write-config` 生成的键集）

src, recursive, dst, cq, fixed-cq, auto-quality, device, workers, limit, min-jpeg-quality,
keep-smaller, copy-skipped, in-place, transparent-format, webp-quality, webp-method,
webp-lossless-max-mb, oversize-format, oversize-webp-quality, oversize-webp-method,
opaque-format, opaque-webp-quality, oversize-max-edge, alpha-rav1e-threads,
color-rav1e-threads, overwrite, report, config, write-config

约定：CLI 传参 > config 文件 > 默认值；config 未知键必须报错；JSON null = 用默认。
**CLI 默认值与 `compress_config.json` 对齐（2026-09-06）**：`auto-quality` 默认 88（`--fixed-cq`
退回固定 `--cq` 基准模式），`oversize-max-edge` 默认 16383（0 = 关，不缩放）；裸跑 = 生产行为。

## 7. 已知边界（parity 目标之外，显式记录）

- 动图（GIF/动图 WebP）不支持，跳过并计数。
- YUV444 输入在本机 AV1 NVENC 不可用（必然 CPU 回退），批量管线默认 420 不受影响。
- 评测指标简化口径见 OPTIMIZATION_PROPOSALS 文首度量说明（chroma PSNR/ΔE2000 的简化实现）。
