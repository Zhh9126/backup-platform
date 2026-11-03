# AI 大模型数据备份能力调研与落地（2026-11-03）

> 输入：用户提供的「AI 工作负载七层状态模型 + 平台五维能力要求」框架。
> 结论前置：AIDBM 的底座（文件备份/去重/不可变/演练/数据库矩阵）覆盖了七层中的五层，
> **缺口集中在两处**——向量数据库的原生保护、跨层一致性恢复单元。本轮补齐这两处。

## 一、七层状态 × AIDBM 现有能力映射

| 层 | 资产 | AIDBM 承接（既有） | 本轮增量 |
|---|---|---|---|
| 1 模型权重与检查点 | .safetensors/.gguf（大而少变）；LoRA/微调产物（不可重来的 GPU 计算） | 文件备份（镜像/增量/源端去重 95.5% 实测节省）+ 不可变四层防护 + 保留锁定 | — |
| 2 训练与微调数据集 | 语料/清洗集/标注/prompt 日志（"以为还能再获取"） | 文件备份 + 不可变锁定（WORM 保留期）+ 记录页不可变徽章 | — |
| 3 向量索引 | HNSW/IVF 二进制快照（重建 = 数十小时 GPU + 数天流水线） | **空白**（只能当普通文件目录，活跃写入时不一致） | **Qdrant 引擎**（原生快照 API，零安装）+ **Chroma 引擎**（目录快照+sqlite 校验） |
| 4 关系型元数据与对象存储 | 用户/会话/审计库；PDF/音频/图片 | 全库型备份矩阵 + 对象存储桶级保护 | — |
| 5 编排与运行时配置 | K8s 清单/GPU 环境/路由规则 | 文件备份（含 exclude/增量） | — |
| 6 版本指针与血缘 | DVC .dvc / lakeFS 提交 / W&B run ID | 文件备份（小文件，配源端去重） | — |
| 7 Agent 状态与工具配置 | 任务状态/工具链/权限边界 | 文件/数据库备份 | — |

**跨层一致性（核心判断标准）**：既有平台以"单个任务"为粒度，各层各自备份、
时间点互不对齐——"各自可恢复、合在一起不可用"。本轮新增 **AI 保护计划**
（见 §三），把多层资产编入同一恢复单元，产出组恢复点（epoch）与层间偏差（drift）报告。

## 二、五维平台要求 × AIDBM 现状

| 要求 | AIDBM 现状 | 判定 |
|---|---|---|
| 1 原子化跨层一致性快照 | 无 → 本轮 AI 保护计划（同窗口协调 + epoch/drift 报告） | **部分达成**：协调一致 ≠ 存储层原子快照（后者需 FS/存储控制器一致性组，离线零 agent 架构不做，如实标注） |
| 2 WORM 不可变 + 恢复路径隔离 | 不可变四层（锁定/写保护/完整性基线/删除四路拦截）+ 恢复需验证码/受保护主机硬闸 | **达成**（应用层语义；物理不可变需 FS/S3 Object Lock，已如实标注） |
| 3 块级去重应对"大文件小变更" | 源端文件级去重（95.5% 实测节省）+ rsync delta 拉取 + global_dedup 块级记账 | **部分达成**：固定块对 tar.gz 失效的教训已用文件级+rsync 绕过；权重文件原地改写场景收益取决于传输通道（rsync -z 裸 tar 最优） |
| 4 向量库原生保护 | 空白 → 本轮 Qdrant 快照 API 全链路 + Chroma 目录快照 | **达成（Qdrant）/ 边界内达成（Chroma）**；Milvus 未做（需 milvus-backup 工具推送，见 §五） |
| 5 策略驱动 RPO/RTO + 恢复演练 | 每任务 cron/interval + 保护策略（core/important/general 三级）+ 真实恢复演练（MySQL/MariaDB 落库演练） | **部分达成**：向量库恢复演练（隔离环境拉起+检索质量验证）未做——Qdrant 可在同实例异名 collection 恢复后 count/status 校验，检索质量抽查需嵌入模型在场，列为后续 |

## 三、本轮落地（代码）

1. **`core/engines/qdrant.py`** — Qdrant 引擎（db_type=qdrant）：
   - 备份：REST `PUT /collections/{c}/snapshots?wait=true` → 流式下载 → 远端快照清理；
     db_name 留空 = 全部 collection，逗号分隔 = 指定集合；支持 api-key。
   - 恢复：`POST /collections/{target}/snapshots/upload?priority=snapshot`（异名恢复 = target_db）。
   - 校验：tar 结构解析（Qdrant snapshot 为 tar 归档）。
   - 零安装零 agent：纯 HTTP 直连 6333，不向被保护主机安装任何工具。
2. **`core/engines/chroma.py`** — Chroma 引擎（db_type=chroma）：目录 tar 快照 +
   sqlite integrity_check + manifest；恢复 = 目录回放（SSH 解包）。诚实边界：
   活跃写入窗口的一致性由调用方停写保证（Chroma 无在线快照 API）。
3. **`core/ai_protection.py` + `api/ai_backup.py`** — AI 保护计划（七层模型）：
   - 成员 = 既有备份任务（复用全部引擎能力），按层标注（weights/dataset/vector_index/…七层）；
   - `run_plan`：按序驱动成员备份 → 组恢复点 **epoch = min(成员成功时间)**、
     **drift = max-min**；drift > 阈值（默认 300s）→ WARNING 日志 + 报告告警；
   - 单成员失败不中断组（报告如实标注），ok=false；
   - API：/api/ai-plans CRUD + /run；前端 `/ai_backup` 页（新建/编辑/运行/报告/删除）。
4. 菜单「AI 模型备份」（/ai_backup）。

## 四、业界参照与差异化

| 参照 | 取舍 |
|---|---|
| milvus-backup（官方工具） | Milvus 保护的正解是推送 milvus-backup 二进制到目标执行（同 xtrabackup 推送模式）——列入后续，需真机 Milvus |
| Kasten K10（K8s 应用一致性） | K8s 场景：AIDBM 以 Kanister 式"任务编排"承接（保护计划成员可含 K8s 清单文件任务），不做 K8s 集群级快照（零 agent 边界） |
| pgvector/pgvectorscale | 归 PG 引擎既有保护（逻辑+物理+PITR），天然覆盖 |
| DVC/lakeFS | 只保护指针与提交历史（文件任务），不复制其数据块（对象存储任务承接） |
| Weights & Biases artifacts | 实验追踪 run 元数据以文件/DB 任务承接；不跟随 SaaS 模式 |

**AIDBM 领先点**（相对通用文件备份工具）：不可变保留锁、恢复点安全检测（熵/结构嗅探，
对权重文件勒索加密敏感）、池复制两地三中心（副本异地）、恢复演练真实化（落库+行数核对）、
AI 保护计划的 drift 量化报告。

## 五、诚实边界与后续

- **本轮未真机验证 Qdrant**（环境无法拉取镜像时如实标注；验证步骤：docker run qdrant →
  建任务（host=127.0.0.1, port=6333）→ 备份 → 删 collection → 恢复 → points_count 核对）。
- Milvus：需 milvus-backup 推送执行 + 真机 Milvus，未实现。
- 向量索引检索质量演练（恢复后 top-k 对比）需嵌入模型与查询集在场，列为 P2。
- 嵌入模型版本/分块规则不在向量库快照内（Qdrant 官方快照只含索引数据）——必须通过
  保护计划把记录这些元数据的资产编入同一恢复单元，报告会强制提示该绑定关系。
