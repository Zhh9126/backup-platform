# -*- coding: utf-8 -*-
"""Agent 能力矩阵：平台侧期望清单与目标机实际自报的比对。

## 为什么需要

用户诉求是「**只安装一次，所有功能都支持，不区分类型**」。但"安装成功"≠"能力齐全"：

- 旧的 ELF 单文件发布包是**能力全量之前**的快照，装上去 exec/文件传输能用，
  块级 CDP / BMR / 应用整机 / 扇区雕刻四个插件能力**全都没有**；
- 目标机缺 python3 时只能退回 ELF 单文件，于是"同一个 Agent、不同能力集"，
  用户完全看不出差别；
- 插件能力还取决于目标机内核/文件系统（xfs vs ext4、cbt 是否可用等），
  Agent 会自报 supported + reason——平台必须把这份结论**落库并展示**，
  否则就是"装完不知道能干什么"。

因此平台侧维护一份**期望能力清单**，安装后逐项核对：
缺哪项、为什么缺，一目了然；能力随平台代码自动变多，装一次即可用全。

## 期望清单

- features：Agent 协议层能力（执行、流式执行、文件收发、目录操作、摘要、
  取消、断点续传、插件调用）；
- plugins：统一插件框架下的四类块级/整机能力。
"""

# 协议层能力（Agent /capabilities 的 features 字段）
EXPECTED_FEATURES = (
    "exec",            # 命令执行（返回码 + 输出）
    "exec-stream",     # 流式执行（长任务 + 进度 + 取消）
    "file-put",        # 文件上传（SFTP 替代）
    "file-get",        # 文件下载（断点续传）
    "fs",              # 目录遍历 / 删除 / 改名
    "fs-digest",       # 端到端 sha256 对账
    "cancel",          # 协作式取消
    "resume",          # 断点续传
    "plugin",          # 插件调用入口
    "self-update",     # 自更新（装一次即可跟随平台版本）
)

FEATURE_LABELS = {
    "exec": "命令执行",
    "exec-stream": "流式执行",
    "file-put": "文件上传",
    "file-get": "文件下载",
    "fs": "目录操作",
    "fs-digest": "端到端摘要对账",
    "cancel": "协作式取消",
    "resume": "断点续传",
    "plugin": "插件调用入口",
    "self-update": "自动更新（装一次即跟随平台版本）",
}

# 插件能力（Agent /capabilities 的 plugins 字段）
EXPECTED_PLUGINS = (
    "block-cdp",       # 卷级 CDP（位图 / CBT / 秒级 RPO）
    "bmr",             # 裸机磁盘镜像 + 可启动救援 ISO
    "app-dr",          # 应用级整机快照（一致性冻结/解冻）
    "block-carve",     # 扇区级签名雕刻
)

PLUGIN_LABELS = {
    "block-cdp": "卷级持续数据保护（CDP）",
    "bmr": "裸机镜像与救援介质（BMR）",
    "app-dr": "应用级整机快照（App-DR）",
    "block-carve": "扇区级数据雕刻取证",
}


def _plugin_reason(entry: dict) -> str:
    if not isinstance(entry, dict):
        return ""
    if entry.get("supported"):
        return ""
    return str(entry.get("reason") or "目标机环境不支持（Agent 未给出原因）")


def evaluate(caps: dict) -> dict:
    """把 Agent 自报的 /capabilities 与期望清单比对，返回可展示的矩阵。

    返回结构::

        {
          "total": 13, "ok": 11,
          "features": {"exec": True, ...},
          "plugins": {"block-cdp": {"supported": True, "reason": ""}, ...},
          "missing": [{"item": "block-cdp", "kind": "plugin", "reason": "..."}],
        }
    """
    caps = caps or {}
    features = set(caps.get("features") or [])
    plugins = {p.get("name"): p for p in (caps.get("plugins") or [])
               if isinstance(p, dict)}
    out_features, missing = {}, []
    for name in EXPECTED_FEATURES:
        ok = name in features
        out_features[name] = ok
        if not ok:
            missing.append({"item": name, "kind": "feature",
                            "label": FEATURE_LABELS.get(name, name),
                            "reason": "Agent 未自报该能力（版本较旧，建议在平台重装一次）"})
    out_plugins = {}
    for name in EXPECTED_PLUGINS:
        entry = plugins.get(name)
        supported = bool(entry and entry.get("supported"))
        reason = "" if supported else _plugin_reason(entry or {})
        if entry is None:
            reason = "Agent 未内置该插件（版本过旧，需重新安装/升级 Agent）"
        out_plugins[name] = {"supported": supported, "reason": reason,
                             "version": (entry or {}).get("version") or "",
                             "label": PLUGIN_LABELS.get(name, name)}
        if not supported:
            missing.append({"item": name, "kind": "plugin",
                            "label": PLUGIN_LABELS.get(name, name),
                            "reason": reason})
    total = len(EXPECTED_FEATURES) + len(EXPECTED_PLUGINS)
    ok_count = total - len(missing)
    return {
        "total": total,
        "ok": ok_count,
        "missing": missing,
        "features": out_features,
        "plugins": out_plugins,
        "agent_version": caps.get("version") or "",
        "distribution": caps.get("distribution") or "",
        "python": caps.get("python") or "",
        "os": caps.get("os") or "",
        "arch": caps.get("arch") or "",
        "enc": bool(caps.get("enc")),
        "summary": ("能力齐全（%d/%d）" % (ok_count, total) if not missing
                    else "能力 %d/%d，缺 %d 项：%s" % (
                        ok_count, total, len(missing),
                        "、".join(m["label"] for m in missing[:4]))),
    }


def summarize(matrix: dict) -> str:
    """一行话结论（用于日志与安装结果消息）。"""
    if not matrix:
        return "能力未知"
    return str(matrix.get("summary") or "")
