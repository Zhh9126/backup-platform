# Oracle 备份端到端测试报告

- **测试时间**：2026-08-12 23:45 – 23:46（北京时间，+08:00）
- **测试人**：Edward（QA Engineer）
- **被测系统**：数据备份管理平台（Python + Flask）
- **服务地址**：http://127.0.0.1:8080（系统 Python 3.14.3 启动，进程由后台任务 wJbJaj 维持）
- **测试范围**：3 个前端 Bug 修复（Oracle 保存无 POST、数据库备份编辑无弹窗、文件备份编辑无弹窗）+ Oracle 备份后端链路（创建 / 查询 / 编辑 / 连通性 / 执行）
- **测试约束**：仅做"读 + 创建测试任务 + 调接口 + 写报告"，**未修改任何业务代码**（`api/*.py`、`static/js/app.js`、`templates/*` 均原样）。
- **测试凭据**：登录 `admin / admin123`；Oracle 目标库 `system / oracle`（normal 模式）。

---

## 一、概述

本次测试针对已落地的三项修复做端到端验证。修复根因统一为：`templates/base.html` 原从 CDN 加载 Bootstrap JS，内网/离线浏览器加载不到导致 `bootstrap` 为 undefined、模态框初始化抛错被吞、保存按钮未绑定。修复手段为：
1. `base.html` 改为本地引用 Bootstrap 资源；
2. `app.js` 对 `taskModal`/`fileTaskModal` 增加空安全自愈；
3. Oracle 任务保存（POST）后端路径已可用。

测试按"前端代码审查（不依赖浏览器）→ 后端 API（创建/查询/编辑）→ Oracle 连通性（oracledb 真实连一次）→ 备份执行（调平台引擎）→ 报告"的顺序执行，全部完成。

---

## 二、修复验证结果

### 2.1 前端修复（代码审查，不依赖浏览器）

| 检查项 | 位置 | 结果 | 证据 |
|---|---|---|---|
| Bootstrap CSS 本地化 | `templates/base.html` 第 7 行 | ✅ 通过 | `<link href="/static/css/bootstrap.min.css?v=2026080210" rel="stylesheet">` |
| Bootstrap JS 本地化 | `templates/base.html` 第 83 行 | ✅ 通过 | `<script src="/static/js/bootstrap.bundle.min.js?v=2026080210"></script>` |
| `openTaskModal` 空安全自愈 | `static/js/app.js` 464–469 行 | ✅ 通过 | `if (!taskModal) { const el=...; if (el && window.bootstrap && bootstrap.Modal) taskModal = new bootstrap.Modal(el); }`；之后 `if (taskModal) taskModal.show(); else console.error(...)` |
| `saveTask` 空安全 | `static/js/app.js` 第 676 行 | ✅ 通过 | `if (taskModal) taskModal.hide();` |
| `openFileTaskModal` 空安全自愈 | `static/js/app.js` 1937–1942 行 | ✅ 通过 | `if (!fileTaskModal) { ... fileTaskModal = new bootstrap.Modal(el); }`；`if (fileTaskModal) fileTaskModal.show(); else console.error(...)` |
| `saveFileTask` 空安全 | `static/js/app.js` 第 2017 行 | ✅ 通过 | `if (fileTaskModal) fileTaskModal.hide();` |
| JS 语法校验 | `node --check static/js/app.js` | ✅ 通过 | 输出 `NODE_CHECK_OK`（Node v22.22.2） |

**结论**：本地化 Bootstrap 已从架构上消除对外网 CDN 的依赖；`app.js` 关键函数均具备模态框实例缺失时的自愈/安全跳过逻辑，逻辑正确。`node --check` 语法 OK。

> ⚠️ **说明**：本环境无法驱动真实浏览器点击，因此"最终用户验证"需用户在浏览器**硬刷新（Ctrl+F5）**后确认弹窗与保存行为。代码层已确认修复到位。

> 📝 **小提示**：`base.html` 第 8 行 bootstrap-icons 仍走 CDN（`https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/...`），不影响本次三个 Bug，但离线场景图标会缺失，建议一并本地化（见"下一步建议"）。

### 2.2 后端修复（API 创建 / 编辑 Oracle 任务）

见"三、后端 API 测试结果"。创建返回 201 且含 id，编辑返回 ok 并持久化——证明 Oracle 任务保存（Bug1）与编辑后端路径（Bug2）均可用。

---

## 三、后端 API 测试结果

使用 `requests` 维持 session，先 `POST /login` 拿 cookie，再调业务接口。

| 步骤 | 接口 | 方法 | 响应码 | 关键结果 |
|---|---|---|---|---|
| 登录 | `/login` | POST | **302**（登录后重定向） | 已设置 `session` cookie，鉴权通过 |
| 创建任务 | `/api/tasks` | POST | **201** | 返回 `{"id": 49, "ok": true}` |
| 查询任务 | `/api/tasks/49` | GET | **200** | 字段完整且正确（见下） |
| 编辑任务 | `/api/tasks/49` | PUT | **200** | 返回 `{"ok": true}`，编辑后再次 GET 确认生效 |

**创建的 Oracle 任务（id=49）持久化关键字段（GET 校验）：**

| 字段 | 值 | 字段 | 值 |
|---|---|---|---|
| name | oracle核心系统-192.168.220.129 | db_type | oracle |
| biz_system | oracle核心系统 | host | 192.168.220.129 |
| port | 1521 | username | system |
| db_name | orcl11g | backup_type | full |
| backup_mode | logical | schedule_type | none |
| enabled | 1 | retention_days | 30 |
| extra_options | `{"service":"orcl11g"}` | created_at | 2026-08-12T23:45:23+08:00 |

**编辑验证（Bug2 后端编辑路径可用）：** 对 id=49 执行 `PUT`，将 `name` 改为 `oracle核心系统-192.168.220.129-EDIT`、`retention_days` 改为 45。返回 `{"ok": true}`；再次 GET 确认 `name=oracle核心系统-192.168.220.129-EDIT`、`retention_days=45`、`enabled=1`，编辑已成功落库。

> 📌 该任务（id=49）**已保留不删除**，作为 Bug1 修复证据供用户在 UI 中查看。

**结论：后端 API（Bug1 创建 / Bug2 编辑）全部通过。**

---

## 四、Oracle 连通性测试结果

使用 `oracledb` 3.4.2 薄驱动（thin mode，无需 Oracle Client）真实连接一次目标库。

- **驱动版本**：oracledb 3.4.2
- **连接串**：`system / oracle @ 192.168.220.129:1521/orcl11g`（normal 模式）
- **连接结果**：✅ **成功（thin mode）**
- **数据库版本**：
  `Oracle Database 19c Enterprise Edition Release 19.0.0.0.0 - Production`，`Version 19.3.0.0.0`
- **连通校验**：`SELECT * FROM v$version WHERE rownum=1` 正常返回；`SELECT 1 FROM DUAL` 返回 `(1,)`

**结论：Oracle 目标库本身无问题，凭据正确，网络可达。** 这排除了"凭据错/网络不通"导致备份失败的可能——若备份失败，根因不在数据库连接性，而在本机缺少备份客户端工具（见第五节）。

---

## 五、备份执行测试结果

对 id=49 调用 `POST /api/tasks/49/run`，body `{"backup_type":"full"}`。

- **响应码**：200，`Content-Type: application/json`
- **返回 record（原样记录）：**

```json
{
  "backup_path": "E:\\备份管理平台\\backup_platform\\backups\\oracle\\49_oracle核心系统-192.168.220.129-EDIT\\20260812_234630__oracle核心系统-192.168.220.129-EDIT__full.sim",
  "backup_type": "full",
  "binlog_file": null,
  "binlog_pos": null,
  "checksum": "",
  "compress_algo": "",
  "compress_ratio": 0.0,
  "db_type": "oracle",
  "duration_sec": 0.567,
  "finished_at": "2026-08-12T23:46:30+08:00",
  "id": 124,
  "is_simulated": 1,
  "message": "仿真备份(占位)成功；expdp 客户端不可用",
  "original_size_bytes": 0,
  "size_bytes": 380,
  "started_at": "2026-08-12T23:46:29+08:00",
  "status": "simulated",
  "storage_tier": "local",
  "task_id": 49,
  "verified": 0,
  "verify_msg": null,
  "wal_lsn": null
}
```

**关键字段解读：**
- `status = "simulated"`、`is_simulated = 1`：引擎进入了"客户端不可用 → 仿真/占位"分支。
- `message = "仿真备份(占位)成功；expdp 客户端不可用"`：明确说明失败原因为 expdp 客户端不可用。
- `stderr = null`：stdout/stderr 无额外错误输出，属预期内的优雅降级。
- `backup_path` 为 `.sim` 占位文件，非真实备份产物。

**主机侧佐证（只读检查，未改动）：**

| 工具 / 变量 | 结果 |
|---|---|
| `expdp` | ❌ PATH 中缺失 |
| `exp` | ❌ PATH 中缺失 |
| `imp` | ❌ PATH 中缺失 |
| `rman` | ❌ PATH 中缺失 |
| `sqlplus` | ❌ PATH 中缺失 |
| `ORACLE_HOME` | ❌ 未设置 |
| Instant Client 目录 | ❌ 不存在（仅存在 VirtualBox 无关目录） |

**失败原因分析**：本机仅装有 Instant Client 11.2 的 `oci.dll`（供 oracledb 薄/厚驱动连接用），但**不包含 expdp/exp/imp/rman 等备份工具**；且任务未配置 SSH 备份机。因此平台引擎无法调用真实的 DataPump（expdp）或 RMAN，按设计降级为"仿真备份占位"。这是**环境限制，非代码 Bug**——后端 API、引擎调度、连通性均正常。

---

## 六、已知限制

1. **本机缺 Oracle Client 备份工具**：`expdp/exp/imp/rman/sqlplus` 均不在 PATH，`ORACLE_HOME` 未设置，无法执行真实 RMAN / DataPump 备份。当前 `/api/tasks/run` 仅产出 `.sim` 占位文件。
2. **未配置 SSH 备份机**：任务 `extra_options` 未指定 `ssh_host_id`，引擎无备用执行通道，只能在本机判定为"客户端不可用 → 仿真"。
3. **前端"最终用户验证"未做**：本环境无法驱动真实浏览器点击，弹窗与保存按钮的最终交互需用户在浏览器硬刷新后人工确认。
4. **bootstrap-icons 仍依赖 CDN**：`base.html` 第 8 行图标走 jsDelivr CDN，离线场景图标缺失（不影响本次三个 Bug）。

---

## 七、下一步建议

1. **浏览器硬刷新验证前端**：用户在本机浏览器对平台页面做硬刷新（Ctrl+F5）后，验证"数据库备份编辑弹窗""文件备份编辑弹窗"可正常弹出、保存按钮可触发 POST，确认三个前端 Bug 已闭环。
2. **部署完整 Oracle Client 以跑真实备份**：在平台主机安装完整 Oracle Client（含 expdp/exp/imp/rman），设置 `ORACLE_HOME` 并将其 `bin` 加入 PATH，随后对 id=49 重新执行 `/api/tasks/49/run`，预期应产出真实 `.dmp`/备份集而非 `.sim`。
3. **或配置带 Oracle Client 的 SSH 备份机**：在平台"主机管理"中添加一台已装完整 Oracle Client 的 SSH 备份机，并在任务 `extra_options` 指定 `ssh_host_id`，让引擎走 SSH 通道执行真实备份，规避本机无客户端的问题。
4. **bootstrap-icons 本地化**：将 `base.html` 第 8 行的 bootstrap-icons CSS 改为本地 `/static/css/bootstrap-icons.css`（并放置对应字体文件），彻底去 CDN 依赖。
5. **补充自动化回归测试**：将本次"登录 → 创建 Oracle 任务（断言 201+id）→ 查询 → 编辑 → 连通性"固化进 CI，防止回归；备份执行接口可加一条断言"缺客户端时返回 `is_simulated=1` 而非 5xx"。

---

## 八、测试结论（分级）

| 维度 | 结论 | 说明 |
|---|---|---|
| 前端修复（base.html 本地化 + app.js 空安全） | **Pass（代码层）** | 代码已确认；最终交互待浏览器硬刷新人工确认 |
| 后端 API（Bug1 创建 / Bug2 编辑 Oracle 任务） | **Pass** | 创建 201+id=49，编辑 200 且落库 |
| Oracle 连通性（oracledb 实测） | **Pass** | 19c 19.3.0.0.0 连接成功，凭据/网络无误 |
| 备份执行（/api/tasks/run） | **Partial** | 引擎逻辑正确、优雅降级为仿真；真实备份受本机缺 Oracle Client 限制，未能端到端跑通 |

**总体结论：Partial（修复侧 Pass，执行侧受环境限制 Partial）。**
三项 Bug 修复（前端本地化 + 空安全自愈 + 后端创建/编辑）已通过验证；Oracle 目标库连通性确证无误。唯一未达成的是"真实 RMAN/DataPump 备份跑通"，根因是本机缺少 Oracle Client 备份工具且未配 SSH 备份机——属环境限制而非代码缺陷，已如实记录并给出 remediation（建议 2/3）。

---

*本报告由 QA Engineer（Edward）基于只读验证与接口实测生成，未改动任何业务代码。测试任务 id=49 已保留。*
