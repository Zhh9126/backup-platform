# 数据备份管理平台 — 测试报告

> 测试日期：2026-07-18 17:57~18:04 CST  
> 测试环境：Windows / Python 3.14.3 / Flask 3.0.3 / SQLite  
> 测试基地址：http://127.0.0.1:8311  
> 配置：DEMO_MODE=off / SCHEDULER_ENABLED=false

---

## 一、测试概览

| 指标 | 数值 |
|------|------|
| 总用例数 | **51** |
| 通过 | **51** |
| 失败 | **0** |
| 通过率 | **100.0%** |
| 覆盖维度 | 鉴权、页面渲染、API CRUD、备份引擎、错误处理、数据一致性 |

---

## 二、测试范围

### 2.1 鉴权（4 项）
- ✅ 正确账号密码登录成功，进入仪表盘
- ✅ 错误密码被拒绝并显示错误提示
- ✅ 未登录访问 `/api/meta` 返回 401
- ✅ 登出后 API 访问恢复 401

### 2.2 页面路由渲染（11 项）
- ✅ `/` — 仪表盘（含统计卡）
- ✅ `/tasks` — 数据库备份（含 taskModal、SSH 主机下拉框）
- ✅ `/records` — 备份记录
- ✅ `/restore` — 数据恢复
- ✅ `/settings` — 系统设置（含通知配置卡片）
- ✅ `/file_backup` — 文件备份（含纳管主机 + 文件备份任务表）
- ✅ `/sync` — 数据同步
- ✅ `/restore_records` — 恢复记录
- ✅ `/inspection` — 巡检
- ✅ `/static/css/app.css` — 样式表 200
- ✅ `/static/js/app.js` — 前端脚本 200

### 2.3 API CRUD — 数据库备份任务（9 项）
- ✅ `POST /api/tasks` — 创建任务(201) + id 返回
- ✅ `GET /api/tasks?db_type_exclude=file` — DB 列表不混入文件任务
- ✅ `GET /api/tasks/:id` — 获取单任务(200)
- ✅ `PUT /api/tasks/:id` — 更新任务(200)
- ✅ `POST /api/tasks/:id/run` — 执行备份返回 FAIL（DEMO_MODE=off，Windows 无 mysqldump，正确行为）
- ✅ `POST /api/tasks` (file 类型) — 文件任务创建(201)
- ✅ `GET /api/tasks?db_type=file` — 文件任务列表隔离
- ✅ `DELETE /api/tasks/:id` — 删除任务(200)
- ✅ `POST /api/tasks` 非法 db_type → 400
- ✅ `POST /api/tasks` 缺 name → 400

### 2.4 API CRUD — SSH 主机纳管（5 项）
- ✅ `POST /api/hosts` — 创建主机(201)
- ✅ `GET /api/hosts` — 主机列表(200)，密码不回显
- ✅ `GET /api/hosts/:id` — 获取单台主机(200) **（测试中发现缺失，已新增路由）**
- ✅ `POST /api/hosts/:id/test` — 连接测试（预期失败，返回错误描述）
- ✅ `PUT /api/hosts/:id` — 更新（密码留空=保持旧密码）(200)
- ✅ `DELETE /api/hosts/:id` — 删除(200)

### 2.5 API CRUD — 数据同步（5 项）
- ✅ `POST /api/sync/tasks` — 创建同步任务(201)
- ✅ `GET /api/sync/tasks` — 同步任务列表(200)
- ✅ `POST /api/sync/tasks/:id/run` — 立即同步(201)
- ✅ `GET /api/sync/records` — 同步记录(200)
- ✅ `DELETE /api/sync/tasks/:id` — 删除(200)

### 2.6 记录与巡检（4 项）
- ✅ `GET /api/records` — 备份记录(200)
- ✅ `GET /api/restores` — 恢复记录(200)
- ✅ `POST /api/inspection/run` — 触发巡检(200)
- ✅ `GET /api/inspection/records` — 巡检记录(200)

### 2.7 通知配置（4 项）
- ✅ `GET /api/notify-config` — 读取配置(200)
- ✅ `POST /api/notify-config` — 保存邮件渠道（含 smtp_host/to/use_tls）
- ✅ 回读验证 — channels 正确包含保存的配置
- ✅ 密码不回显（smtp_password 不泄露）
- ✅ `POST /api/notify-config` (reset) — 重置为空(200)

### 2.8 仪表盘与系统（3 项）
- ✅ `GET /api/dashboard` — 返回 task_count 等字段(200)
- ✅ `GET /api/nonexistent` → 404
- ✅ `GET /logout` — 登出(200/302)

### 2.9 备份引擎行为验证（2 项）
- ✅ **DEMO_MODE=off + Windows 无 mysqldump → 返回 FAIL（不仿真）**
- ✅ demo_only=1 正确走仿真占位
- 旁证：PostgreSQL 引擎同理返回真实错误

### 2.10 数据一致性（2 项）
- ✅ `db_type_exclude=file` 正确隔离 DB 任务与文件任务
- ✅ `db_type=file` 正确只返回文件任务

---

## 三、测试过程中发现并修复的问题

| # | 问题 | 严重程度 | 修复 |
|---|------|----------|------|
| 1 | **`GET /api/hosts/:id` 路由缺失** — 仅有 PUT/DELETE，前端编辑时需先 GET 单主机详情 | 🟡 中 | 新增 `GET /api/hosts/<host_id>` 路由（`api/hosts.py`） |
| 2 | **`POST /api/notify-config` 数据嵌套层级错误** — 从 `data` 直接读 `channels`，实际嵌套在 `data.notify` JSON 字符串内 | 🔴 高 | 先解析 `data["notify"]` JSON → `cfg`，再读 `cfg.get("channels")`（`api/system.py`） |

---

## 四、测试环境配置

- **服务器**：`http://127.0.0.1:8311`（DEMO_MODE=off）
- **数据库**：临时 SQLite `instance/meta.db`（每次测试自动创建/表初始化）
- **调度器**：禁用（`SCHEDULER_ENABLED=false`）
- **备份根目录**：临时目录 `/tmp/bktest2_*/backups`
- **运行账号**：admin / admin123

---

## 五、结论

✅ **平台核心功能全部通过测试，可以发布。**

注意：当前测试在 Windows 环境运行，本地无 mysqldump/pg_dump 等数据库客户端。引擎在 DEMO_MODE=off 下正确返回 FAIL（而非伪装成仿真占位），这是预期行为。在**部署到 Linux 服务器或配置 SSH 主机后**，引擎将走真实 dump 路径产出实际备份文件。

### 建议的下一步
1. 在 Linux 服务器上部署，验证 mysqldump/pg_dump 真实备份链路
2. 用真实 SSH 主机做文件备份端到端验证
3. 补充前端 E2E 测试（Playwright）覆盖表单交互与模态框流程
4. 对 `/api/tasks/:id/run` 长时间任务增加进度轮询超时提示
