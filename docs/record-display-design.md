# 记录展示统一与搜索 · 架构设计

> 增量需求（基于 `docs/record-display-prd.md`）。本文档由交付总监在架构师子代理不可用时代为归档已实现的设计，仅描述变更部分。

## 1. 实现方案与框架选型

- 复用既有 Flask + SQLite(meta.db) + Bootstrap5 单页体系，**无新增第三方依赖**。
- 展示层：在 `list_records` / `list_restores` 返回行中**新增 JOIN 字段**（任务名、归一化 IP、中文类型/方式），前端用统一格式化函数渲染，后端不再做 HTML 拼接，前端统一负责展示。
- 搜索：后端新增单 `keyword` 参数（对 `tasks.name` 与 `tasks.host` 做 `LIKE`），前端按归一化 `host_ip` 二次过滤（支持「本地」不被吞）。

## 2. 文件清单（变更）

| 文件 | 改动性质 | 说明 |
|------|---------|------|
| `config.py` | 修改 | `DB_DISPLAY_NAMES` 补 `"file":"文件"`；新增 `BACKUP_TYPE_DISPLAY_NAMES`（全量/增量/差异） |
| `core/models.py` | 修改 | 新增 `normalize_host_ip()`、`_db_type_display()`、`_backup_type_display()`；`list_records()` / `list_restores()` 加 `keyword` + `LEFT JOIN backup_tasks` + 五要素填充 |
| `api/records.py` | 修改 | `/api/records` 透传 `keyword` |
| `api/restore.py` | 修改 | `/api/restores` 透传 `keyword`；`/records/enriched` 补充 `host_ip`/`backup_type_display` |
| `static/js/app.js` | 修改 | 新增 `fmtRecordLabel()` / `fmtBizCell()`；改造 6 个入口（records 表、restore 页下拉、dataMining 下拉、clone 页下拉、restore_records 表、initRestoreRecords 搜索） |
| `templates/records.html` | 修改 | 加搜索框；表头改为「业务系统/备份类型/备份方式/备份时间」 |
| `templates/restore_records.html` | 修改 | 加搜索框；表头加「备份方式」 |
| `templates/restore.html` | 修改 | 加 `r_search` / `r_search_ip` 双搜索框与五要素表头 |

## 3. 数据契约（接口）

`list_records` / `list_restores` 行对象新增字段：

```json
{
  "id": 123,
  "task_name": "订单库全备",
  "host_ip": "192.168.220.150",
  "db_type_display": "MySQL",
  "backup_type_display": "全量",
  "started_at": "2026-08-01T09:00:00"
}
```

归一化规则 R1（`normalize_host_ip`）：先剥离 `user@` 前缀，再剥离 `:port` 后缀；空值返回 `-`；`本地` 原样保留。

## 4. 调用流程（时序）

```
前端搜索框 oninput
  → GET /api/records?keyword=xxx
  → models.list_records(keyword)
       → LEFT JOIN backup_tasks 取 name/host
       → normalize_host_ip(host) → host_ip
       → 中文映射 → db_type_display / backup_type_display
  → 返回五要素 JSON
前端 fmtBizCell / fmtRecordLabel 渲染
```

## 5. 任务列表（实现顺序）

1. `config.py` 补映射（已完成）
2. `core/models.py` 加辅助函数 + JOIN 查询（已完成）
3. `api/records.py` + `api/restore.py` 透传 keyword（已完成）
4. `static/js/app.js` 新增格式化函数 + 改造 6 入口（已完成）
5. 三个模板加搜索框与表头（已完成）
6. `tests/test_record_display.py` 单测（本次补）
7. QA 回归验收（待执行）

## 6. 共享知识（跨文件约定）

- 业务系统 = `tasks.name` + 保留 `#id` 前缀（用户拍板，#id 为安全底线不可移除）。
- 类型/方式一律汉化：`full→全量`、`incremental→增量`、`differential→差异`；`file→文件`。
- 后端只给 `host_ip`（已归一化），前端不得再用正则自行解析 `tasks.host`。

## 7. 待明确事项

- 恢复记录页（`/restore_records`）的「备份时间」来自 `backup_records.started_at`（通过 `record_id` 关联），非 `restore_records` 自身时间——已按此实现。
- 若后续需按「备份方式」独立筛选，可复用同一 `keyword` 通道，无需新增字段。
