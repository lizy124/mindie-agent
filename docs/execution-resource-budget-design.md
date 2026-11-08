# 执行资源预期、代码有界性与规格匹配设计

[返回文档中心](README.md) · [设计原则与 PR 自查清单](design-principles.md) · [架构与领域边界](architecture.md)

本文定义执行资源约束的职责归属、当前代码事实与后续接线目标。**设备忙闲（Busy/Idle）、可用容量（Capacity Headroom）、并发预约/租约授予（Reservation / Lease Grant）与内核级内存硬限制（Hard Limit）各自回答完全不同的问题**——绝不能把其中任意一项的单元测试或组件实现冒充为其余各项的端到端验收。

## 一、“当前代码 / 目标设计 / 未验收”实现边界

以 2026-10-24 源码审查基线（`remote-dev` `b91874e` 及配套 PR [`remote-dev #44`](https://github.com/mindie-agent/remote-dev/pull/44)、`coordinator` `7a252d0` 及配套 PR [`coordinator #64`](https://github.com/mindie-agent/coordinator/pull/64)）为准，各能力的真实状态严格区分如下（本文不代表已安装宿主插件已合入或启用全部下述变更）：

| 能力维度 | 当前代码（已实现） | 目标设计（待接线） | 未验收边界（不得夸大宣称） |
| --- | --- | --- | --- |
| **工作负载规格与放置判定** | `remote_dev/core/host_spec.py` 提供 `WorkloadExpectation`、`HostHardwareSpec`、`normalize_remote_probe_spec`、`evaluate_placement` 与本机跨进程 `reserve_local_resources`（SQLite WAL） | `sim` / `profiler` 等组件在各自真实 CLI 入口按模型层数、Token/事件规模与 Trace 大小动态计算预期工作集并调用放置判定 | 基础库存在不等于所有业务入口均已接入；`sim` / `profiler` 真实 CLI 尚未统一接入动态估算与自动分流 |
| **远端集群容量与放置准入** | `coordinator` 的 `fleet_probe` 采集远端主机内存、核数与卡/Die 观测；`placement.py`（`hardware_shortfall`、`select_runtimes`、`can_prepare`）对显式声明 `cpu_cores`/`memory_mb`/`hbm_mb` 的请求按同次放置的 `host_specs` 快照校验容量 | 将逐设备 HBM 余量校验下沉到真正发放物理 Phy-ID 的主机租约队列（`HostQueue`） | `RuntimePool` 的 `prepared-root` 目录行本身不存动态容量；放置阶段的快照校验只是**候选可行性筛查**，不等于编译完成后的启动期显存保留 |
| **编译并发与每槽份额** | 主机队列限制并发编译槽位数，构建脚本独立计算 worker 数；两侧在 Linux 下已读取 cgroup CPU 配额 | 由主机队列在授予 `CompileLease` 时一次性计算并下发每槽内存/CPU 份额，`PreparationProcess` 与 `MAX_JOBS` / `CMAKE_BUILD_PARALLEL_LEVEL` 统一消费同一上限 | 目前内存侧仍只读宿主机 `MemAvailable`，且“槽位数”与“每槽 worker 数”各自按同一份整机可用内存计算，尚未实现单次授予的乘积预算闭环 |
| **进程树所有权与退出回收** | Windows Job Object（`KILL_ON_JOB_CLOSE`）、POSIX 进程组 owner、Linux 远端 subreaper 与 `/proc` 进程树排空回执 | 保持现有所有权与退出排空链路，与内存限额解耦 | **进程所有权与超时排空只证明子进程可被追踪和终止，不证明运行期内存峰值有上限** |
| **进程树内存硬限制（Hard Limit）** | Windows `OwnedProcess(max_memory_mb=...)` 支持在显式传参时设置 Job Memory Limit | Linux 执行器按需接入真实已授权的 cgroup v2 `memory.max`；macOS 如实报告 `observed-only` | **当前不存在跨平台统一的进程树内存硬上限**：Windows 仅在显式传参时生效，POSIX `RLIMIT_AS` 仅限单进程虚拟地址空间而非进程树 RSS，macOS 无等价内核机制 |

已有规格判定库不是第二个调度器或常驻守护进程：组件声明工作负载预期 $\to$ 放置层按实测容量做候选筛查 $\to$ 实际执行主机的既有队列负责租约授予 $\to$ 执行入口消费授予结果。

## 二、任务预期与主机/设备容量事实

工作负载在启动前根据业务输入声明 CPU 核数、主机内存、物理执行单元数与新增显存需求。`TIER_PRESETS` 归属 `remote_dev/core/host_spec.py`，文档不维护第二份默认值表；预设档位仅作缺省起点，不能替代模型尺寸、Trace 规模或单 worker 编译峰值的真实核算：

- **组件工作集估算（目标设计，待接入）**：`sim` 与 `profiler` 应在各自真实 CLI/API 入口按模型层数、Token/事件数、明细级别（`--trace-detail`）与 Trace 文件体积估算活跃工作集。该估算属于待接入设计，不在文档中引用不存在的 `a3_host_budget.py` 或将规划接口写成已上线 API。
- **编译 Worker 峰值**：编译请求的 `per_worker_memory_mb` 必须覆盖 C++ 编译器与链接器峰值，不能把进程数或 NPU 数量直接当成内存预算。
- **零权重控制面与有界工作集**：元数据探测路径只读取 `config.json` 与 `safetensors` 头部，不加载模型权重、不初始化 `torch_npu`/CANN 上下文；大文件流式读取，长循环只保留活跃状态与有界预览，原始日志复用既有落盘与分片机制。

`HostHardwareSpec` 表示单次容量观测：`cpu_cores` 为物理核数，`usable_cpu_cores` 为扣除当前负载后的并发建议核数；主机可用内存（`mem_usable_mb`）与容器/cgroup 剩余配额需分别识别，不能把宿主机物理总内存当成容器内可用内存。

### HBM 容量、忙闲状态与物理 Die 计数

在设备层面，必须严格区分 **物理可租用单元数（Die vs. Card）**、**设备忙闲（Busy/Idle）** 与 **实测可用 HBM（`total - used`）**：

- **物理可租用单元（Die）对齐**：在统一执行规格（`normalize_remote_probe_spec`）中，`npu_count` 与 `free_npus` 一律按**可独立租用的物理 Die** 计数（昇腾 A3 双 Die 卡展开为 2 个 `dies` 单元，与 `ASCEND_RT_VISIBLE_DEVICES` 的 `Phy-ID` `0..15` 一一对应；单 Die 的 A2/A5 或无 `dies` 列表的旧格式行保持每卡 1 个单元）。当探测结果包含 `free_phy_ids` 时以其为准（空列表 `[]` 即明确的 `0` 可用 Die，优先于卡级 `free_npus`）。集群卡级计数仍可用于 UI 展示，但不能直接充当 A3 双 Die 的执行容量。
- **实测 `total - used` 计入可用 HBM，缺测绝不冒充空闲**：空闲单元的可用 HBM 严格按各设备实测的 `max(0, hbm_total_mb - hbm_used_mb)` 计算；若任一单元缺少 `hbm_total_mb` 或 `hbm_used_mb` 观测值，则置 `vram_free_observed = False` 且该单元不贡献可用 HBM（绝不把标称总容量当成可用容量）。同时，**设备处于 `idle`（`busy=False`，无活跃计算进程/可被租用）不等于物理 HBM 已用量为零**。
- **不将已用 HBM 笼统归因为固件，也不硬扣 `2–3 GiB` 固定常数**：空闲设备上的实测 `hbm_used_mb` 可能来自固件、驱动或未释放的运行时上下文，**仅凭 `npu-smi` 汇总读数并不能证明全部已用量都是固件保留**；且不同机型、驱动与容器环境的基线占用各不相同，因此直接采用各设备实测的 `total - used`，**绝不在代码或规则中硬扣 `2–3 GiB` 等拍脑袋固定值**。
- **卡级补充遥测的保守边界**：针对 `npu-smi` 摘要显示 `0 / 0` 的行，卡级补充查询（`npu-smi info -t usages`）仅在该卡能唯一对应单个可租用单元（单 Die 卡或无 Die 行的旧格式）时回填；多 Die 卡的卡级总量绝不均摊或臆测拆分到各个 Die，保持 `vram_free_observed = False` 并按“缺测不计入可用”处理。补充遥测只增加占用证据、绝不抹除已观测到的进程占用事实；卡级与 Die 级空闲清单同源派生，避免“卡已忙而 Phy-ID 仍列为空闲”。
- **避免重复扣减基线**：设备当前已用 HBM 已经在 `free = total - used` 中扣除；任务的 `Expected VRAM`（`vram_mb` / `hbm_mb`）只核算本次任务新增的模型权重、KV Cache、激活值、通信缓冲与运行时分配，不重复扣除设备固有基线。
- **主机级 HBM 总和 vs. 逐设备授予**：主机级 `vram_free_mb`（所有空闲 Die 的剩余 HBM 之和）仅提供主机级候选可行性筛查（例如两个各剩 `48 GiB` 的空闲 Die 合计 `96 GiB`，并不代表单个需要 `64 GiB` 的单 Die 任务能放下）；逐 Die 的最终显存可行性必须由实际设备租约授予者按选中的具体 `Phy-ID` 判定。

## 三、分流判定与实际授予

```mermaid
flowchart LR
    W[组件的资源预期] --> P[按真实本机与远端规格判定]
    H[主机与设备容量观测] --> P
    P --> L[本机满足时本地执行]
    P --> R[远端满足时选择候选主机]
    P --> X[返回不足或缺测的具体维度]
    R --> Q[既有主机队列授予设备与执行份额]
    L --> E[既有执行入口消费授予结果]
    Q --> E
    E --> C[进程所有权、超时与排空]
```

本机满足请求时可以本地执行，包括高配 Windows/macOS/Linux 工作站；不按操作系统名称决定放置。需要远端专属环境或设备时再选择匹配的远端。内存不足一名 worker 时应等待、分流或拒绝，不能仅用 `max(1, ...)` 强行启动。

coordinator 的环境 catalog 负责兼容环境和构建 donor；容量来自同次放置决策的 fleet 快照。显式声明 CPU/内存/HBM 下限时，每个候选主机取一次观测并传给 donor 选择、容器开通检查和后续兼容性判断。未声明这些约束的旧作业不增加探测。探测失败沿用现有 waiting/cache-miss 结果并保留主机错误，不伪装成空集群。

这是候选可行性检查，不能冒充资源保留。编译期间容量可能变化；真正启动设备负载时，已有主机队列仍是当前设备状态和租约的唯一授予者。后续逐设备 HBM 约束应接到这个现有授予点，消费当前观测与请求，不再建立第二套分配账本，也不以准备开始时的旧快照保证启动时显存充足。

规格判定本身是纯算术；采集 fleet 可能需要 SSH。不能把网络探测连同分流都宣称为“毫秒级”。单次 SSH 的 30s 上限不是整次放置的上限：一次探测可能包含主查询、可选的补充查询与缓存写入，候选主机又是串行的，因此不存在一个已证明的外部总时限，也不应由候选数乘单次上限充当该时限。任何外层期限都必须覆盖这些阶段；短于已知内层预算的期限会把 live 探测中途掐断。缓存观测保留其年龄，不能把 cache hit 改写为实时采集。

采样时长落在哪条路径上也要分清。守护进程模式（`_async_progress`）在后台进度工作线程上推进，`status`/`target`/`tail` 以非阻塞方式读取已持久化的事实并报告观测新鲜度，不被探测阻塞；同进程同步调用则要等探测返回。不能因此宣称探测对所有调用零开销，也不能宣称它会阻塞状态查询。

## 四、编译槽位只分配一次预算

当前缺口是“主机槽位数 × 每槽 worker 数”分别按同一份整机内存计算。目标是在现有主机队列授予编译租约时给出每槽份额，下游直接使用，不在每个构建脚本里重新证明整机容量。

1. 实际执行主机读取 CPU 可用配额和内存 headroom；Linux 同时考虑 cgroup CPU 配额、`memory.max - memory.current` 与宿主 `MemAvailable`。本地开发机的 `MAX_JOBS` 不能冒充远端规格。
2. 初期按已配置的并发槽位数静态分份额，避免后续槽位进入时使早先授予失效。对于同质 worker，需满足 `slots * jobs_per_slot <= floor(usable_memory / worker_peak)`，同时受 CPU 配额约束；不够一名 worker 时不授予。
3. `CompileLease` 复用既有授予回执携带份额，`PreparationProcess` 将其传给构建环境。`MAX_JOBS`、`MINDIE_BUILD_JOBS`、`CMAKE_BUILD_PARALLEL_LEVEL` 消费同一个上限；显式降低可以保留，不能绕过授予上限。
4. 在授予到消费的链路与兼容行为完成前，不先删除现有 worker 上限。旧组件无法提供份额时明确说明保护能力未提供，不声称乘积预算已经成立。

这是一项待实施的协议接线，不是已完成的全局资源隔离。是否需要动态份额，留给静态方案的实机观测回答。

## 五、进程回收与内存硬限额分开验收

Windows Job Object 可以对关联进程设置作业级内存限制（仅当显式传入 `OwnedProcess(max_memory_mb=...)` 时启用）；POSIX 进程组与 Linux subreaper 提供进程树所有权、超时取消和后代排空。[Job Memory Limit](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information) 与单进程限制不同；POSIX [RLIMIT_AS](https://docs.python.org/3/library/resource.html#resource.RLIMIT_AS) 仅限制单进程虚拟地址空间，不能作为整个子进程树物理内存（RSS）硬上限的证明。**因此，绝不能宣称当前代码已在跨平台（Windows / Linux / macOS）上实现了统一的进程树内存硬上限。**

Linux 的整树内存硬限额应使用实际已部署且具备写权限的 [cgroup v2 内存控制器](https://docs.kernel.org/admin-guide/cgroup-v2.html#memory-interface-files)；macOS 在没有等价内核机制时明确报告 `observed-only` 能力。调用方可按任务需要的执行能力选择目标，不以操作系统名称代替能力探测。任何实现都不能把“调用方传入了 `max_memory_mb` 参数”等同于“操作系统内核已经对该进程树强制执行了内存硬限额”。

清理和记账错误保持可见，但不覆盖已确认的业务结果；外部写的不确定性沿用现有 receipt 语义，不为本地探测或分流创建永久故障状态。

## 六、分期与判别性验收

| 阶段 | 归属与实际改动 | 能证明什么 |
| --- | --- | --- |
| 容量语义 | remote-dev 统一物理 Die/空闲 HBM；coordinator parser 保留实测 HBM，忙闲单独判定 | 真实 A2/A3/A5 golden capture 的单位与可用量正确；不证明任务已实机运行 |
| 放置接线 | coordinator 从真实 catalog 与 fleet seam 执行约束，无约束零新增 probe | TaskClient→service→RuntimePool 的满足/不足/探测失败路径与一次观测复用 |
| 编译份额 | 既有主机队列→CompileLease→PreparationProcess→编译器 | 单测证明乘积上限与接线；实机再验证并发编译的进程数、峰值内存和退出回收 |
| 组件入口 | sim/profiler 在实际 CLI 接入工作集估算与规格分流 | 低规格/高规格主机的分流及不足一个 worker 时不启动；不是全模型仿真精度验收 |
| 内存执行能力 | 各平台既有进程 owner 接入真实内核能力 | Windows/Linux/macOS 分别报告已实现的 hard-limit 或 observed-only；超时回收与内存限制分别验证 |

真实主机验证使用隔离状态目录，不扰动现有工作负载。CPU 单测、容器边界、NPU 执行与模型收益分别记录；没有实机证据的阶段保持待验收。
