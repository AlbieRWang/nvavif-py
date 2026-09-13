# nvavif-py 项目审查记录(2026-09-13)

> 审查范围:`src/lib.rs`(Rust 核心)、`source/nvavif_py/__init__.py`(Python 包装层)、
> `uvtest/compress_dir.py`(批处理工具)、构建/CI/打包、其余 uvtest 脚本(略读)、
> 与既有文档(DEVELOPMENT_NOTES / OPTIMIZATION_PROPOSALS / PARITY_MATRIX)的交叉核对。
>
> 方法:通读源码 + 在当前环境(RTX 4070, Python 3.13.12, Pillow 12.3.0, numpy 2.5.1,
> wheel 与源码同步重建于 09-06 16:20)运行探针脚本实证。标注说明:【实证】= 已用探针验证;
> 【代码】= 源码通读得出;【文档】= 文档交叉核对得出。
>
> **状态标记(2026-09-13 两轮实施后更新):✅ = 已修复/已落地并通过验证;⏸ = 搁置
> (原因见条目);🚫 = 明确不做(含理由);📄 = 已按"仅文档化"处理。**
> 实施记录见第 0.5 节,最终台账见第 7 节。

---

## 0.5 实施记录(2026-09-13 当晚)

按"很确定 + 高优先 + 小改动"标准实施了一批,全部改动已通过:`cargo check`、
`cargo test --release`(alpha 回归 + NVENC 会话复用测试,2 passed)、wheel 重建 +
`setup_env.py` 重装 + 探针复验、`compress_dir.py` 30 张冒烟跑(路由正确)。

**已改文件:**

| 文件 | 改动 |
|---|---|
| `src/lib.rs` | A1/A9 入参校验(0 维度、channels∈{3,4}、缓冲区长度精确匹配 → `ValueError`);A2 `qp` 钳制 `max(1)`;A4 `ReaderF32` 增加 `tone_map` 门控(ACES 仅在 max>1 时应用);B2 `estimate_cq` 五处 stderr 并入 `NVAVIF_DEBUG_TIMING`;A2 伴生 GPU 回落 WARN 改为每进程一次(`warn_gpu_fallback_once`);A6 解码支持 GRAY8/GRAY10LE(广播为 RGB)+ `YUVA422P10LE`;签名默认值对齐文档(`preset=7`、`EIGHT_BIT`、`YUV420`)并新增 `tone_map` 参数 |
| `source/nvavif_py/__init__.py` | f32 输入计算 `tone_map = nanmax > 1.0` 传入底层;`_ensure_srgb` 裸 `except:` 收窄;`decode_file` docstring 注明只读 |
| `source/nvavif_py/_nvavif_py.pyi` | 整体重写:返回 `tuple[bytes, int]`、默认值与实际一致、枚举改为 pyclass 形态 |
| `uvtest/compress_dir.py` | B1 透明度像素检查按 header `has_alpha` 短路;新增 `--preset`(1–7,默认 7)透传 AVIF 路由并写入报表;main 入口 stdout/stderr `reconfigure(utf-8, replace)` |
| `Cargo.toml` | 移除未使用依赖 `rgb`、`imgref` |
| `pyproject.toml` | `license = MIT`、classifiers 去掉 PyPy、补 Python 3.10–3.14 |
| `README.md` | with_cq 入表;ACES 门控行为描述;NVENC 硬件边界(8192 上限 + 实测最小 尺寸);解码只读;Pillow 仅 save;动图拍扁与 DecompressionBomb 提示 |
| `uvtest/README.md` | 由 0 字节补齐为脚本索引 |
| `.github/workflows/CI.yml` | Linux wheel 增加 auditwheel repair(`--plat manylinux_2_28_x86_64`);新增 `smoke-linux` 冒烟 job(装 wheel + CPU roundtrip) |

**探针复验结果(修复后):**
- `(H,W,1)` / `(H,W,2)` → `ValueError: unsupported channel count ...`(原 Rust panic);
- `cq=0` / `cq=1` 1MP 编码 106–209 ms → **GPU 路径**(原先 cq=0 必落 CPU);
- f32 LDR(`img/255.0`)encode→decode mean 127.5→126.3(纯量化噪声;修复前 127.3→134.8 被 ACES 提亮);HDR(×5)仍走 tonemap;
- auto_cq 子进程 stderr 诊断行 0 条(修复前每图 5 行);
- NVENC 最小尺寸实测(RTX 4070):**宽 ≥130、高 ≥66**(128×130 失败,130×128 成功,130×66 成功、130×65 失败)——已写进 README;
- GPU 回落 WARN 每进程只打 1 行;
- 批处理冒烟 30 张:不透明 JPEG→AVIF、透明 PNG→WebP、9504px 超大 JPEG→WebP,报表含 `preset: 7`。

**第二轮实施(同日,按用户指示"继续按建议的修改、GPU 优先、失败即跳过、不添分支"):**
- `src/lib.rs`:B3 420 色度 2×2 box 平均(8/10-bit 两段,无新分支);B4 方差扫描 Rayon 并行。
  **B5(CPU 10-bit 去交织并行)按用户指示裁掉**——纯 CPU 回落路径不再投入。
- `uvtest/compress_dir.py`:新增小图跳过(`NVENC_MIN_WIDTH=130`/`NVENC_MIN_HEIGHT=66`,
  B9/C 系补充);B8 非透明 WebP 路由保留 RGB;C2 重跑跳过上一轮 kept_source
  (校验 stream header 的 src、`--overwrite` 禁用);顺带修复零任务重跑时
  `interrupted` 未初始化的既有崩溃;报表加 `skipped_small`/`rerun_skipped_prev_kept`。
- `README.md`:E3 映射公式一行。
- 验证:B3 生效性 A/B 探针 + 5 真实图 cq=20 逐字节一致(零回归);auto_cq 25MP 1.43 s;
  13 图冒烟两轮(小图跳过 / kept_source 复跑跳过 / 零任务 report 落盘);
  不透明 WebP 无 alpha、透明 WebP alpha 完好;cargo test 2/2 通过。
- 新增探针脚本 `uvtest/_b3_metric.py`(MAE/SSIM/体积基线对比,可复用作回归)。

**第三轮实施(同日,对剩余 ⏸ 逐项裁决"是否要做"):**
- **做**:C3 路由测试(`uvtest/test_compress_dir_routing.py`,12 项检查全过、
  失败退出非 0,不引 pytest 依赖);C4 长路径运维记录(DEVELOPMENT_NOTES §18);
  D3 删除过期 cp314 wheel(本机无 3.14,重建无意义)。
- **明确不做**:B5/B6/B7(GPU 优先/微优化/解码已够快)、A7(仅影响第三方
  limited-range AVIF 解码,触发条件已写明)、A10(保持文档化,不加 API 表面)。
- 全部 ⏸ 清零。

**CI 实跑结果(同日 push 后;此前该工作流仅 09-06 跑过一次且失败,从未绿过):**
- Linux job 失败根因:**被提交的 `.cargo/config.toml`** 用 `[env]` 把本机 Windows 路径
  (`FFMPEG_DIR='O:\Project\...'`)带进了 manylinux docker,ffmpeg-sys-next 按它找头文件
  必然失败(日志:`cargo:rustc-link-search=native=O:\Project\...`);
- Windows job 失败根因:bindgen 找不到 `libclang.dll`(本地靠 config.toml 的
  `LIBCLANG_PATH` 指向 msys64,CI 上不存在)。
- **修复(第三轮追加)**:`.cargo/config.toml` 移出 git 跟踪(本机保留,新增
  `.cargo/config.toml.example` 模板 + .gitignore 条目,DEVELOPMENT_NOTES 5.1/17 同步);
  CI 两个 job 显式注入 `FFMPEG_DIR`/`LIBCLANG_PATH`(Linux=/usr 与 /usr/lib64,
  Windows=workspace/ffmpeg-out 与 runner 自带的 `C:\Program Files\LLVMin`)。

**遗留注意事项:**
- CI 修复后的运行仍在观察中(运行记录见 GitHub Actions)。

---

## 0. 总体结论

项目完成度很高:核心编码/解码路径正确,alpha 规范性问题已根治,批处理工具的路由、
长跑加固、报表体系相当完善,文档(开发笔记/提案/对齐矩阵)质量在同类项目里少见。
既有文档里已记录的问题(NVENC 8192 轴上限、YUV444 GPU 不稳、alpha 仅 CPU、
avif-serialize 不能嵌 ICC、rav1e asm bug 等)**不在此重复立项**,见第 6 节。

本轮新发现 **11 项正确性/健壮性问题、9 项性能机会、7 项构建/CI/文档补充**(全部闭环)。
审查时点名的"最值得先做的五件事"(1 灰度 panic、2 cq=0 静默回落、3 批处理
重复解码探测、4 auto_cq stderr 无开关、5 Linux wheel 未 auditwheel)——
前 4 项与第 5 项的 CI 修复均已于 2026-09-13 两轮实施中落地,详见第 0.5 节与
各条目标记;唯一待外部确认的是 CI 的 auditwheel/smoke 需真 CI 实跑。

---

## 1. 正确性 / 健壮性

### A1.【✅】【实证·P1】灰度 `(H, W, 1)` 数组输入 → Rust panic
- 位置:`src/lib.rs:1230-1239`(`encode_avif` 按 `len/px_count` 推 channels,不校验)、
  `src/lib.rs:127-137`(`ReaderU8::read` 固定读 `idx+1`、`idx+2`)。
- 复现:`nv.encode_file(np.zeros((64,64,1), np.uint8))` →
  `PanicException: index out of bounds: the len is 4096 but the index is 4096`。
- 影响:任何 `(H,W,1)`/`(H,W,2)` 数组(常见于灰度预处理输出)直接炸出 PanicException,
  而不是 `ValueError`;在 `ProcessPoolExecutor` 里表现为难以理解的 worker 崩溃。
- 建议:包装层在 shape 校验处(`__init__.py:284-285`)拒绝 `c not in (3,4)` 并给出
  明确错误(灰度可自动 broadcast 到 RGB);Rust 层对 `channels` 做 debug_assert 或
  提前返回错误。

### A2.【✅】【实证·P1】`cq=0` 使 NVENC 初始化失败,静默回落 CPU
- 位置:`src/lib.rs:679-692`(`rc=constqp, qp=0`)。
- 复现:stderr 出现
  `InitializeEncoder failed: invalid param (8): Lossless Coding mode not supported`,
  然后是 `WARN: NVENC color encode failed ... falling back to CPU`,输出由 rav1e 生成
  (正确但慢 2 个数量级,且批量场景不易察觉)。
- 影响:README 把 cq 0–10 描述为"archival / near-lossless"且默认走 GPU;实际上 cq=0
  在 RTX 40 系上永远走 CPU。cq=1 是否等价于"最接近无损"也未验证。
- 建议(三选一):① 对 `qp=0` 映射 NVENC 的 `lossless=1` 模式(需验证 AV1 支持);
  ② 在 `open_gpu_encoder` 前 clamp 到 1 并在文档注明;③ 至少在 README/NOTES 写明
  "cq=0 走 CPU"。另外此失败路径每次调用都打 WARN(不走 `WARNED_CPU` 一次性门控),
  长跑日志会刷屏。

### A3.【✅】【实证·P2】NVENC 最小尺寸限制未处理、未文档化
- 复现:64×64 输入 → `Frame dimensions are less than the minimum supported value`
  → 静默 CPU 回落。文档(DEVELOPMENT_NOTES §12)只记录了 8192 上限。
- 建议:实测出 AV1 NVENC 的最小宽/高(可能为 145×49 或 128×128 一类),
  写进 §12;`is_hardware_supported` 的 256×256 探针不受影响,但小图用户会困惑
  "为什么我的 GPU 没用上"。

### A4.【✅】【实证·P2】f32 输入无条件 ACES 色调映射,与 README 矛盾
- 位置:`src/lib.rs:163-182`(`ReaderF32::read` 对每个通道应用 ACES,无论数值范围);
  README(HDR Float32 一节)写的是"values outside the [0,1] range are automatically
  tone-mapped"。
- 复现:标准 LDR float 图(mean 127.3)encode→decode 后 mean 134.8(+6%),
  中间调被 ACES 曲线系统性提亮。任何把归一化 float(如 `img/255.0`)交给库的用户
  都会拿到亮度被改的输出,且无提示。
- 建议(二选一):① 探测 `max > 1.0`(或参数 `tone_map=auto/off/aces`)后才应用 ACES;
  ② 保持行为,把 README 的描述改成"所有 float 输入都会经过 ACES"。推荐 ①,
  `img/255.0` 是社区最常见写法。

### A5.【📄】【实证·P2】`decode_file` 返回只读数组
- 位置:`source/nvavif_py/__init__.py:380`(`np.frombuffer` 对 bytes 建视图,天然只读)。
- 影响:用户/下游库(torchvision transforms、cv2)对像素原地修改会抛
  `ValueError: assignment destination is read-only`;DataLoader 里若 transform 链含
  原地操作会报错且不易定位。
- 建议:README 说明,或直接 `np.frombuffer(...).reshape(...)` 后由调用方决定;更友好的
  是提供 `writable=False` 参数(默认保持零拷贝),或文档显著标注。

### A6.【✅(GRAY 部分)】【代码·P2】解码路径不支持单色(GRAY8)与 identity(RGB)AVIF 变体
- 位置:`src/lib.rs:1488-1499`(`yuv_to_rgb_parallel` 的像素格式 match 表)。
- 事实:① 第三方编码器(avifenc `-y`、灰度照片库)产出的单色 AVIF,dav1d 输出
  `GRAY8`,不在表内 → `Unsupported: GRAY8` 直接报错;② identity matrix(mc=0)的
  "lossless RGB" AVIF(dav1d 输出 GBRP)同样不支持;③ 若文件的辅助 alpha 流被
  `.best(Video)` 选中(第三方文件流序不保证),同样因 GRAY8 报错。
- 影响:主要影响**解码外部来源 AVIF** 的互操作性(自家产物不受影响)。
  对"ML 管线里可能遇到任意 AVIF"的定位而言是个补集缺口。
- 建议:color 路径支持 GRAY8/GRAY10(broadcast 成 3 通道),可顺手把
  `YUVA422P10LE` 补进 alpha 支持表(`extract_alpha_plane` 其实已支持 gray 系,
  `src/lib.rs:1683-1702`)。

### A7.【🚫(暂不做,触发条件明确)】【代码·P3】解码忽略 `color_range` 标志
- 位置:`src/lib.rs:1527-1538` 固定按 full-range 数学(offset 128 / 512)转换。
- 事实:AVIF 静图绝大多数是 full range,但来自视频管线的有限范围文件(或显式
  `fullRange=0` 的容器)会被整体提亮/压暗一档。`decoded.color_range()` 可读取,
  当前未用。
- **裁决(2026-09-13 第三轮)**:暂不做。自家编码产物全程 full-range 一致;
  仅当把本库当"通用 AVIF 查看器"解码第三方 limited-range 文件时才成为真问题,
  届时按 `decoded.color_range()` 分支做 limited→full 展开(需专项样本验证)。

### A8.【✅】【文档·P1】`_nvavif_py.pyi` 存根已过时(误导 IDE 用户)
- `source/nvavif_py/_nvavif_py.pyi:68-83` 与实际绑定不符:
  - 返回类型写 `bytes`,实际返回 `tuple[bytes, int]`(2026-09-06 起);
  - `target_ssim` 默认 0.992 vs 实际 0.985;`preset` 7 vs 6;
    `depth/chroma` EIGHT_BIT/YUV420 vs 实际 TenBit/YUV444。
- 顺带:stub 把枚举声明成 `IntEnum`,而运行时是 PyO3 pyclass(不可当 int 用)。
  低层默认值本身与 README 不一致也是隐患(PyO3 层 `preset=6`/`TenBit`/`YUV444`,
  全靠包装层覆盖;建议把 Rust 侧默认值改成与文档一致,防止直接调用 `_nvavif_py`
  的用户拿到意外结果)。

### A9.【✅】【代码·P3】低层 API 缺少基本入参防护
- `width/height=0` → `pixels.len()/0` 除零 panic;`len` 与 `width*height*dtype` 不匹配时
  静默按 floor 计算 channels(可能产出错误图像而非报错);奇数尺寸在 Rust 层无检查
  (NV12 4:2:0 对奇数维度的行为未定义,目前靠包装层裁边)。
- 建议:`encode_avif` 入口统一校验 `width/height>0`、`len` 精确匹配、`width/height`
  为偶数(或在此裁边),错误信息指向包装层语义。

### A10.【🚫(保持文档化)】【代码·P3】Pillow 插件只注册了 save,没有注册 decode
- `source/nvavif_py/__init__.py:114-157`:未注册 AVIF 的打开/解码 handler。
  装了本库但没装 pillow-avif-plugin 的用户,`Image.open("x.avif")` 直接失败
  (除非 Pillow 自带 libavif 构建)。README 未写明这个不对称。
- **裁决(2026-09-13 第三轮)**:不做 open handler——增加 API 表面积与分支;
  README 已明示"读取用 decode_file / 保存可用 Pillow 插件"。`_save_avif` 静默
  丢弃未知 kwargs 同样接受现状。

### A11.【✅(部分)】【代码·P3】杂项健壮性
- `_ensure_srgb`(`__init__.py:109`)裸 `except:` 会吞 `KeyboardInterrupt`,改
  `except Exception`。
- `encode_file` 对动图输入(GIF 等)静默拍扁为第一帧——批处理工具已有跳过策略,
  但库级 README 无警告。
- `Image.MAX_IMAGE_PIXELS` 未在库内调整,>178MP 走文件路径编码会触发
  DecompressionBombError(compress_dir 自行置 None),README 可提示。

---

## 2. 性能优化机会(均为新发现,不与 OPTIMIZATION_PROPOSALS 重复)

### B1.【✅】【代码·P1,收益最大】批处理 worker 每图多做一次全量解码探测透明度
- 位置:`uvtest/compress_dir.py:258-262`。`transparent_format != "avif"`(默认恒真)时,
  worker 里执行 `im.convert("RGBA").getchannel("A").getextrema()`——**对每张进入
  编码队列的图都做一次全量像素解码 + RGBA 转换**,包括所有不透明 JPEG。
  而主进程的 `probe_cost`(`compress_dir.py:196`)已经把 header 级 `has_alpha`
  放进了 task(`**probe` 合并于 `compress_dir.py:813`),worker 里根本没用它。
- 建议:`if task["has_alpha"]` 才做像素级 alpha 检查(header 声明无 alpha 的图
  不可能突然有 alpha;PNG tRNS/LA/PA 都被 header 检查覆盖)。
- 量级:12MP JPEG 全解码约 200-400ms/图;10 万张 → 约 6-11 个 worker·小时,纯属白扔。
- 同族优化:透明 WebP 路由同一任务里 `Image.open` 了两次(透明度探测一次、
  取 EXIF/ICC + convert 一次,`compress_dir.py:260,295`),可合并;AVIF 路由编码后
  又 `Image.open` 一次只为取 width/height/mode(`compress_dir.py:425`),probe 里已有。

### B2.【✅】【代码·P2】auto_cq 的诊断 stderr 无开关
- 位置:`src/lib.rs:1114-1121, 1125, 1138, 1146-1149`。`estimate_cq` 每次调用无条件
  `eprintln!` 5 行(Pass1/Pass2/Safeguard/Math Target)。批处理默认 auto_quality=88,
  即**每张图 5 行**,10 万张 = 50 万行 stderr,既是日志污染也是无谓 I/O
  (debug_timing 都知道用 `NVAVIF_DEBUG_TIMING` 门控,这里漏了)。
- 建议:并入 `debug_timing_enabled()`(或独立 `NVAVIF_QUIET`),默认静默。
  这与 §18"长跑加固"精神一致,但当时只加固了编排层。

### B3.【✅】【代码·P2,质量向】4:2:0 色度抽样用单像素(无平均)
- 位置:`src/lib.rs` `extract_yuv420` 8-bit 与 10-bit 两段:UV 已改为 2×2 box
  平均后再过矩阵(矩阵线性,先平均 RGB 等价且更省),Rayon 行并行结构不变,无新分支。
- 验证(2026-09-13 第二轮):
  1) 生效性探针——16×16 蓝底图,A 变体左上 2×2 整块红 vs B 变体仅 (0,0) 一像素红:
     旧"左上采样"下 A/B 色度必然相同;新实现 A=358B / B=366B 输出不同 → box 平均生效;
  2) 回归——5 张真实图(含 25MP)GPU cq=20 编码与改前**逐字节一致**(相邻像素色度差
     远小于 cq=20 量化步长),即真实语料上零回归、仅在色度锐边处受益。
  探针脚本留存于 `uvtest/_b3_metric.py`(MAE/SSIM/体积基线对比)。

### B4.【✅】【代码·P3】auto_cq 的 block_variance 扫描串行且 O(4·W·H)
- 位置:`src/lib.rs` `prepare_trial_frame_8bit` 邻近的方差扫描:已改为
  `into_par_iter().step_by(..).flat_map_iter(..)` 按补丁行并行,收集顺序与原
  行主序一致,排序结果稳定。
- 验证:25MP(4096×6141)auto_cq 全程 1.43 s(含 3 次 NVENC 试探 + 全图编码),
  选 cq 正常。

### B5.【🚫(按 GPU 优先策略不做)】【代码·P3】CPU 10-bit 路径的去交错/移位是标量循环
- 位置:`src/lib.rs` rav1e CPU 编码路径的 `y_unpad/u_unpad/v_unpad` 逐元素 `>>6`。
- **决策(2026-09-13,用户指示):项目定位是 GPU 大幅加速,纯 CPU 回落路径上的
  次要串行开销不再投入优化**;该代码仅在 NVENC 拒绝/无 GPU/超大图回落时执行,
  保持原状。

### B6.【🚫(明确不做)】【代码·P3】每像素 f32 除法与浮点 round-trip
- 位置:`src/lib.rs:127-157`。ReaderU8/U16 把整数 `/255.0` 归一,随即乘回。
  全整数域计算可省一半浮点运算。**裁决(2026-09-13 第三轮)**:不做——微优化,
  YUV 转换瓶颈不在此,实测无法区分差异,改动却触及所有 reader 热路径。

### B7.【🚫(明确不做)】【代码·P3】带 alpha 的 AVIF 解码要开 2-3 次文件
- 位置:`src/lib.rs` 解码路径(probe + color 流 + alpha 流各一次全量重开)。
  **裁决(2026-09-13 第三轮)**:不做——解码本就远快于编码(数量级差距),
  为省 2 次 open 改流式 packet 路由不划算。

### B8.【✅】【代码·P3】WebP 全图路由把不透明图也转成 RGBA
- 位置:`uvtest/compress_dir.py`。`convert("RGBA")` 已改为仅 `transparent_route`
  时保留 alpha 平面,其余路由 `convert("RGB")`(变量改名 `enc_img`)。
- 验证:不透明 JPEG → `--opaque-format webp` 输出 `RGB` 无 alpha 平面;
  透明 PNG → 默认路由输出 `RGBA`、alpha extrema (0,255) 完好。

### B9.【✅(批处理跳过)】【实测观察·P3】小图(<128px?)静默走 CPU
- 64×64 探针显示 NVENC 拒绝最小尺寸以下帧(A3),auto 模式下无 WARN 以外的提示。
  README 的硬件限制段已注明实测最小尺寸(宽 ≥130 / 高 ≥66,RTX 4070)。
- **第二轮追加(2026-09-13,用户指示"GPU 优先、不为慢路径强行兼容")**:批处理
  在扫描阶段即跳过低于 `NVENC_MIN_WIDTH=130` / `NVENC_MIN_HEIGHT=66` 的源
  (记 `skipped_small` 进报表、`--copy-skipped` 时照常复制),不再为亚 100px
  的图标类源白白走 CPU rav1e。库层面保留 CPU 回落不动(API 兼容性)。
- 验证:11 图冒烟集 3 张小图(100×60 / 300×40 / 80×80)全部按预期跳过。

---

## 3. 批处理工具补充建议(compress_dir.py)

### C1.【✅(--preset)】【P2】缺 `--preset` / `--alpha-cq` / `--depth` / `--chroma` / `--matrix` 透传
- 位置:`compress_dir.py:397-402`。AVIF 路由只传 device/cq/auto_quality,
  preset 固定走包装层默认 P7(=rav1e speed 4,CPU 回落路径上就是最慢档;
  oversize 有专门 preset 4,但 8192 以内、GPU 失败回落 CPU 的图也吃 P7)。
  想要"快速出片"或"更高压缩"的用户没有任何旋钮。
- 建议:加 `--preset`(默认 7)、可选 `--alpha-cq`;depth/chroma/matrix 维持默认即可,
  但至少在 help 里说明批处理固定 8bit/420/BT709。

### C2.【✅】【P3】重跑时 `kept_source` 图会被再次完整编码
- 已实现:主循环前读取 `dst/<report>.stream.jsonl`(校验 header 的 `src` 与本次
  相同、`--overwrite` 时禁用),收集 `action=kept_source` 的名字,扫描阶段直接
  跳过;报表新增 `summary.skipped_small` / `summary.rerun_skipped_prev_kept`。
- 顺带修掉一个既有 bug:零任务重跑(全部已完成)时 `interrupted` 只在
  `if tasks:` 块内初始化,report 写出处 `UnboundLocalError` 崩溃——已上提初始化。
- 验证:13 图冒烟集第一轮产生 1 个 kept_source(solid2.png),第二轮输出
  `skip (kept as source by previous run)`,零任务轮 report 正常落盘。

### C3.【✅(轻量脚本版)】【P3】路由决策与 `estimate_jpeg_quality` 值得一个 pytest 套件
- 现有测试全是手动脚本(test_nvavif.py 无断言、失败也 exit 0)。
- **已实现(2026-09-13 第三轮)**:`uvtest/test_compress_dir_routing.py`——
  脚本式 assert(不引 pytest 依赖),失败退出非 0。12 项检查:quality 估计(50/95)、
  不透明 JPEG→AVIF、透明 PNG→WebP 且 alpha 保留、常量不透明 RGBA→AVIF 路由、
  不透明→WebP 无 alpha 平面(B8)、超大→WebP 不缩放、keep-smaller 确定性触发
  (132×70 纯色 + optimize,180B PNG 低于任意 cq 的 AVIF 地板)。全部通过,exit 0。

### C4.【✅】【P3】Windows 长路径风险未记录
- 已写入 DEVELOPMENT_NOTES §18 运维注意(2026-09-13):开启 `LongPathsEnabled`
  (或 git `core.longpaths=true`)或保持层级浅;失败会计入 `failures` 不中断运行。

### C5.【✅】【P3】重定向输出时的编码崩溃风险
- `print()` 在输出重定向到文件时用 locale 编码(cp936),语料里含生僻字符文件名
  会 `UnicodeEncodeError` 中断主循环。stream.jsonl/报表已显式 utf-8。
  建议:入口处 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`
  (一行,零风险)。

---

## 4. 构建 / CI / 打包

### D1.【✅(待 CI 实跑确认)】【P1】Linux wheel 没有 auditwheel repair
- `.github/workflows/CI.yml` linux job:FFmpeg/dav1d 以 shared 方式装进 /usr,
  maturin 打包后**没有** auditwheel repair 步骤(Windows 有 delvewheel)。
  发出的 manylinux wheel 运行时依赖系统 `libavcodec.so.61` 等,且在
  ubuntu-latest(glibc 2.39)上构建却声明 manylinux 2_28,符号版本可能超约。
  README"Pre-built wheels bundle all required libraries"对 Linux 不成立。
- 建议:加 `auditwheel repair --exclude libcuda.so.1 --exclude libnvidia-encode.so.1`
  (CUDA 系必须排除,FFmpeg/dav1d LGPL 允许捆绑);或改 `--enable-static` 只留
  NVENC 动态依赖。

### D2.【✅(待 CI 实跑确认)】【P2】CI 无任何测试/冒烟 job
- build → publish 直通。CPU 路径(rav1e+dav1d)在无 GPU 的 runner 上完全可测:
  `import nvavif_py; encode_file(arr, device="cpu"); decode_file(...)`
  相当于 setup_env.py 的 smoke_test。加一个 5 分钟的 job 可拦住"发布出去的 wheel
  根本 import 不了"级别的事故(Linux wheel 尤其需要,可与 D1 联动验证)。

### D3.【✅(已删除)】【P3】`dist-local/nvavif_py-0.1.0-cp314` wheel 落后一个功能版本
- cp314 wheel 停在 09-05(早于 ctx LRU、长跑加固),是"装了就跑旧代码"的坑。
  **裁决(2026-09-13 第三轮)**:删除而非重建——本机无 3.14 环境(py 启动器只有
  3.12,venv 3.13),重建只会再造一个无人使用的过期产物。需要 3.14 支持时随
  CI 出包即可。

### D4.【✅】【P3】Cargo.toml 疑似未使用依赖
- `rgb`、`imgref` 在 `src/lib.rs` 中无引用(可能是 rav1e API 早期版本遗留)。
  `cargo udeps` 确认后可移除,减小 Cargo.lock 面积;不影响产物。

### D5.【✅】【P3】pyproject 细节
- classifiers 声明 PyPy,但 cdylib 扩展从未在 PyPy 上验证,建议删;
  `requires-python >= 3.10` 与 wrapper 顶层 `X | Y` 注解(3.10 OK)一致,无需动;
  顺带可补 `license = "MIT"` 字段与 `Programming Language :: Python :: 3` 系列。

### D6.【✅】【P3】`uvtest/README.md` 是 0 字节空文件
- 要么补一段(uvtest 各脚本一句话索引 + "out/ 可删"提示),要么删除。
  现状容易被当成"文档写好了但内容丢了"。

---

## 5. 文档补充(README / NOTES)

- E1.【✅(with_cq 部分)】【P2】`with_cq` 参数(09-06 新增的返回元组功能)未进 README API 表;
  "per-image CQ report"是批处理卖点,README 也完全没提 compress_dir 这个门面工具。
  建议给 `uvtest/compress_dir.py` 在 README 里一节(它现在是这个仓库事实上的
  主应用)。
- E2.【✅】【P3】README 缺:NVENC 最小尺寸(A3/B9)、cq=0 行为(A2)、float 输入
  ACES 的准确描述(A4)、解码输出只读(A5)、动图拍扁警告(A11)、
  Pillow 只注册 save(A10)。
- E3.【✅】【P3】`target_quality` 0–100 → SSIM 的映射公式
  已写入 README API 表下方:`ssim = 1 − 0.5 · ((100 − q) / 100)²`(80→0.98、
  85→0.98875、90→0.995),与 `__init__.py` 实现逐字核对一致;≤1.0 直接作为
  SSIM 目标。

---

## 6. 已知且已记录的事项(避免重复立项,供索引)

以下问题已在既有文档中记录,本轮**不再展开**,只列锚点:
- NVENC 8192 轴上限、超限回落 CPU(DEVELOPMENT_NOTES §2/§12;库不缩放,
  由 compress_dir `--oversize-*` 路由覆盖)。
- alpha 只能 rav1e CPU、`asm` feature 静默损坏 alpha 及其回归测试(§3.2/§11.4);
  恢复 asm 的前置条件(NASM 换源 + 重跑回归)未满足。
- YUV444 显式 GPU 路径取决于运行时 capability,本机必回落(§14)。
- avif-serialize 不能嵌 ICC、AVIF 路由不透传原始 EXIF(PARITY_MATRIX M4/M5)。
- NVENC 会话上限 8、ctx 打开 ~80ms、LRU 先驱逐再开(§16;lib.rs 已实现)。
- 提案池状态:已落地 H/I1/I3'(显式版)/I4/J/K/Q/D/P(实测未改默认);
  暂停/条件触发 A/B/E/G/I2/M/N/O/R;否决 C/F/L(见 OPTIMIZATION_PROPOSALS §5)。
- 文档待办两条:多次重复取中位数收窄 ±25% 噪声;向 rav1e 上游提交 11.4 复现用例。

---

## 7. 处理顺序(实施前的计划快照,已全部消化——状态见右列)

| # | 事项 | 量级 | 状态 |
|---|------|------|------|
| 1 | A1 灰度 panic 校验 + A9 入参防护 | 半小时 | ✅ 第一轮 |
| 2 | B2 auto_cq stderr 门控 | 10 分钟 | ✅ 第一轮 |
| 3 | A2 cq=0 处理(lossless 模式或 clamp+文档) | 1-2 小时(含验证) | ✅ 第一轮(一行 qp 钳制 + README) |
| 4 | B1 批处理透明度探测短路 | 10 分钟 | ✅ 第一轮 |
| 5 | A8 pyi 存根刷新 + Rust 默认值对齐 | 半小时 | ✅ 第一轮 |
| 6 | D1 auditwheel + D2 CI 冒烟 job | 2-3 小时 | ✅ 已写入 CI.yml,**待真 CI 实跑确认**(唯一遗留) |
| 7 | A4 ACES 行为决策(默认仅 HDR 才 tonemap) | 1 小时 + 回归 | ✅ 第一轮 |
| 8 | B3 420 box 平均 + compare_perceptual 验证 | 半天 | ✅ 第二轮(A/B 探针 + 逐字节回归替代 LPIPS) |
| 9 | A5/A10/A11 文档与解码头(README 批量补) | 1 小时 | ✅ 第一轮 |

### 审查项最终台账(截至 2026-09-13 第三轮结束,⏸ 清零)

- **已落地并通过验证(29 项)**:A1–A4、A6(GRAY)、A8、A9、A11(部分)、B1–B4、
  B8、B9、C1、C2、C3、C4、C5、D1、D2、D3、D4、D5、D6、E1、E3
  ——D1/D2 于 2026-09-13 经真实 CI 三轮修复后**全绿确认**(见上节);
- **明确不做(5 项,理由见条目)**:B5(GPU 优先策略)、B6(微优化不可测)、
  B7(解码已够快)、A7(仅影响第三方 limited-range 解码)、A10(不加 API 表面);
- **CI 实跑确认(2026-09-13,run 34760722418)**:sdist / windows(11m46s)/
  linux(4m34s,auditwheel 捆绑 FFmpeg 共享库)/ smoke-linux(装 wheel 无系统
  FFmpeg 依赖,CPU roundtrip `smoke OK: 62534 bytes, decoded (256, 256, 3)`)全部通过,
  为该工作流史上首次绿;审查闭环,无遗留。

---

*审查者备忘:2026-09-13 实施后,本文档转为"审查 + 实施记录"文档。若后续继续消化
⏸ 条目,建议按仓库惯例在 DEVELOPMENT_NOTES 增补章节、在 PARITY_MATRIX 同步行为
变更(其维护规则要求:compress_dir 行为变更必须同步本表)。*
