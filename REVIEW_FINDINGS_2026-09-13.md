# nvavif-py 项目审查台账（2026-09-13）

> 审查范围：`src/lib.rs`、`source/nvavif_py/__init__.py`、`uvtest/compress_dir.py`、
> 构建/CI/打包，及与既有文档的交叉核对。方法：通读源码 + 探针实证。
> **状态：闭环归档**。实施细节见 `DEVELOPMENT_NOTES.md`；已知限制见其 §11.4/§12/§16；
> 后续方案见 `OPTIMIZATION_PROPOSALS.md`。

标注：✅ 已修复并验证；🚫 明确不做（理由见条目）；📄 仅文档化；
【实证】探针验证；【代码】源码通读；【文档】交叉核对。

闭环结论：新发现 11 项正确性/健壮性、9 项性能机会，以及一批构建/CI/文档补充，
全部落地或裁决；CI 于 2026-09-13 首次全绿（run 34760722418）。

---

## 1. 正确性 / 健壮性

| ID | 级别 | 问题 | 处理 |
|---|---|---|---|
| A1 | P1【实证】 | `(H,W,1)`/`(H,W,2)` 数组 → Rust 索引越界 panic | ✅ 包装层拒绝 `c ∉ {3,4}`，Rust 层校验尺寸/长度并返回 `ValueError` |
| A2 | P1【实证】 | `cq=0` 使 NVENC 初始化失败并静默回落 CPU（慢两个数量级） | ✅ Rust 层 `qp` 钳制到 ≥1；回落 WARN 改为每进程一次 |
| A3 | P2【实证】 | NVENC 最小尺寸未处理/未文档化 | ✅ 实测 RTX 4070 最小 **130×66**，写入 README；批处理直接跳过更小的源 |
| A4 | P2【实证】 | f32 输入无条件 ACES，LDR float 中间调被提亮 ~6% | ✅ 仅 `max > 1.0` 时 tone map，`tone_map` 入参透传 |
| A5 | P2【实证】 | `decode_file` 返回只读数组，原地修改抛异常 | 📄 README 注明只读 |
| A6 | P2【代码】 | 解码不支持 GRAY8/GRAY10 与 `YUVA422P10LE` | ✅ GRAY 广播为 RGB、补 422 10-bit alpha；identity matrix（mc=0）未做，遇到再补 |
| A7 | P3【代码】 | 解码忽略 limited-range 标志（自家产物全 full-range） | 🚫 仅影响第三方 limited-range 文件，触发时按 `decoded.color_range()` 展开 |
| A8 | P1【文档】 | `_nvavif_py.pyi` 存根过时（返回类型/默认值/枚举形态） | ✅ 整体重写；Rust 默认值对齐文档（preset 7、8-bit、YUV420、`tone_map`） |
| A9 | P3【代码】 | 低层 API 无尺寸/长度防护（0 维度除零、长度不匹配静默截断） | ✅ 与 A1 同一入口校验 |
| A10 | P3【代码】 | Pillow 只注册 save，未注册 AVIF open | 🚫 不增加 API 表面；README 已明示读取用 `decode_file` |
| A11 | P3【代码】 | 裸 `except:` 吞 `KeyboardInterrupt` 等杂项 | ✅ 收窄为 `except Exception`；动图拍扁与 DecompressionBomb 已文档化 |

探针复验要点：`cq=0/1` 回到 GPU 路径；LDR float 编解码均值 127.5→126.3（仅量化噪声），
HDR（×5）仍 tone map；auto_cq 诊断行默认静默；GPU 回落 WARN 每进程 1 行。

---

## 2. 性能机会

| ID | 级别 | 问题 | 处理 |
|---|---|---|---|
| B1 | P1【代码】 | worker 对每张图多做一次全量解码探透明（header 已有 `has_alpha`） | ✅ 按 header `has_alpha` 短路；省 200–400 ms/12MP 图 |
| B2 | P2【代码】 | auto_cq 每次调用无条件打印 5 行 stderr | ✅ 并入 `NVAVIF_DEBUG_TIMING` 门控，默认静默 |
| B3 | P2【代码】 | 4:2:0 色度用单像素采样（无平均） | ✅ 8/10-bit 两段改 2×2 box 平均；真实图 cq=20 输出逐字节一致 |
| B4 | P3【代码】 | auto_cq 方差扫描串行 | ✅ rayon 按补丁行并行，顺序稳定；25 MP auto_cq 全程 1.43 s |
| B5 | P3【代码】 | CPU 10-bit 去交织是标量循环 | 🚫 纯 CPU 回落路径不再投入（GPU 优先策略） |
| B6 | P3【代码】 | 每像素 f32 除法/浮点 round-trip | 🚫 微优化不可测，改动触及所有 reader 热路径 |
| B7 | P3【代码】 | 带 alpha 解码重复开文件 2–3 次 | 🚫 解码远快于编码，为省 open 改流式路由不划算 |
| B8 | P3【代码】 | 不透明图也被转成 RGBA 再走 WebP | ✅ 仅透明路由保留 alpha，其余直接 RGB |
| B9 | P3【实测】 | <128 px 小图静默走 CPU | ✅ README 记录实测下限；批处理扫描阶段跳过，报表记 `skipped_small` |

---

## 3. 批处理工具

| ID | 问题 | 处理 |
|---|---|---|
| C1 | 缺少 preset/alpha 等旋钮透传 | ✅ 新增 `--preset`（1–7，默认 7）并写入报表；depth/chroma/matrix 保持默认 |
| C2 | 重跑时上一轮 `kept_source` 被重复编码 | ✅ 读上次 stream header 复跑跳过（`--overwrite` 禁用该行为）；顺带修复零任务重跑 `interrupted` 未初始化的崩溃 |
| C3 | 路由决策无自动测试 | ✅ `uvtest/test_compress_dir_routing.py`，12 项断言、失败退出非 0、无 pytest 依赖 |
| C4 | Windows 长路径风险未记录 | ✅ 写入 `DEVELOPMENT_NOTES.md` §18 |
| C5 | 输出重定向到 cp936 文件时 `UnicodeEncodeError` 中断 | ✅ 入口 `stdout/stderr.reconfigure(utf-8, replace)` |

---

## 4. 构建 / CI / 打包

| ID | 级别 | 问题 | 处理 |
|---|---|---|---|
| D1 | P1 | Linux wheel 未 auditwheel repair，运行时依赖系统 FFmpeg | ✅ CI 加 `auditwheel repair --exclude libcuda/libnvidia-encode`，共享库已捆绑 |
| D2 | P2 | CI 无测试/冒烟 job | ✅ 新增 `smoke-linux`：装 wheel 后无系统 FFmpeg 依赖做 CPU 回环 |
| D3 | P3 | 过期 cp314 wheel（早于 ctx LRU） | ✅ 删除（本机无 3.14 环境，重建无意义） |
| D4 | P3 | `rgb`、`imgref` 未使用依赖 | ✅ 移除 |
| D5 | P3 | pyproject 声明 PyPy 但从未验证 | ✅ 删除该 classifier，补 license 与 Python 3.10–3.14 |
| D6 | P3 | `uvtest/README.md` 为空文件 | ✅ 补齐为脚本索引 |

CI 首轮真实运行暴露的两个环境问题：被提交的 `.cargo/config.toml` 把本机 Windows 路径
带进 manylinux docker；Windows job 找不到 `libclang.dll`。修复：该文件移出跟踪（保留
`.cargo/config.toml.example`），CI 显式注入 `FFMPEG_DIR`/`LIBCLANG_PATH`。此后 sdist /
windows / linux / smoke-linux 全部通过，为该工作流首次全绿。

---

## 5. 文档补充

| ID | 处理 |
|---|---|
| E1 | ✅ `with_cq` 进入 README API 表；新增批处理入口一节（`compress_dir.py` 是事实上的主应用） |
| E2 | ✅ README 补 NVENC 最小尺寸、`cq=0` 行为、float ACES 条件、解码只读、动图拍扁、Pillow 只 save |
| E3 | ✅ `target_quality` 0–100 → SSIM 映射公式写入 README，与实现逐字核对 |

---

## 6. 明确不做（5 项）

- **A7**：解码忽略 limited-range。自家产物全 full-range；仅解码第三方文件时触发，届时再做。
- **A10**：Pillow open handler。增加 API 表面积与分支；README 已给出替代路径。
- **B5**：CPU 10-bit 去交织。纯 CPU 回落不再投入，保持原状。
- **B6**：整数域 YUV 转换。微优化无法测出差异，却触及所有热路径。
- **B7**：alpha 解码重复 open。解码已足够快，改造收益不抵消复杂度。

---

## 7. 后续

本审查无遗留项。新的性能方向见 `OPTIMIZATION_PROPOSALS.md`（全部条件触发）；
行为变更必须同步 `PARITY_MATRIX.md`，它同时是未来 Rust CLI 的验收基准。
