#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AIDBM 错误日志自助归因分析器（可在任意部署现场直接运行）。

为什么需要它
------------
平台出错时，现场运维手上只有一份 ``logs/error.log``，几百上千行看不出
"到底哪一件事在反复失败、是不是同一个根因"。本工具把原始日志压成三张表：

1. **错误分布**——按级别 / 按 logger 归类，先看清是哪一类功能在报错；
2. **模板聚类**——把时间、任务号、IP、端口、路径、UUID 等可变片段归一化，
   把"同一条错误的不同次出现"合并成一条（这是刷屏型错误的识别关键）；
3. **处置建议**——命中内置特征库时给出根因与排查动作（不猜、不下结论，
   只给"下一步查什么"）。

用法::

    # 分析默认路径（<程序目录>/logs/error.log）
    python3 scripts/diag/analyze_error_log.py

    # 分析指定文件（其他平台部署把 error.log 拷过来即可）
    python3 scripts/diag/analyze_error_log.py /path/to/error.log

    # 只看最近 500 行、按模板看前 15 类、输出 JSON
    python3 scripts/diag/analyze_error_log.py -n 500 --top 15 --json

约束：仅标准库、完全离线、可在客户现场裸跑；不修改任何日志文件。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

# --------------------------------------------------------------------------- #
# 日志行解析
# --------------------------------------------------------------------------- #
# 平台日志格式：2026-11-26 10:35:22 [ERROR] rt.cdc: [rt.cdc] task=670 ...
LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s+"
    r"\[(?P<level>[A-Z]+)\]\s+"
    r"(?P<logger>[^:]+):\s*"
    r"(?P<msg>.*)$"
)

# 可变片段归一化：数字 / 十六进制 / IP / 端口 / 路径 / UUID / 引号串
_NORM_RULES = [
    # paramiko 的 "Exception (client): xxx" 与 "SSHException: xxx" 是同一事件
    # 的两行表述，归一后聚类才是"事件数"而不是"行数"
    (re.compile(r"^Exception \(client\):\s*"), ""),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<时间>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<IP>"),
    (re.compile(r"(?<![\w.])/[\w./\-]{3,}(?=[\s'\"，。；、）)]|$)"), "<路径>"),
    (re.compile(r"\b[0-9a-fA-F]{12,}\b"), "<HEX>"),
    (re.compile(r"\b\d{4,}\b"), "<数字>"),
    (re.compile(r"\b\d+\b"), "<数字>"),
    (re.compile(r"'[^']*'"), "'<值>'"),
    (re.compile(r'"[^"]*"'), '"<值>"'),
]


def normalize(msg: str) -> str:
    """把消息里的可变片段替换成占位符，得到"错误模板"。"""
    out = msg
    for rx, rep in _NORM_RULES:
        out = rx.sub(rep, out)
    return out.strip()[:400]


# --------------------------------------------------------------------------- #
# 特征库：命中即给根因与排查动作（只给方向，不臆断结论）
# --------------------------------------------------------------------------- #
RULES = [
    (r"Error reading SSH protocol banner|protocol banner",
     "SSH 握手被拒（sshd 未认证连接数超限 / 中间设备拦截 / 对端 sshd 未就绪）",
     "① 该错误几乎都是**并发建连**打满 sshd 的 MaxStartups（现场样本：连续多天"
     "每天 02:00 定时任务集中触发，每天 6 次）；② 处置：把同一时刻的备份任务"
     "错开执行、确认对端 sshd 正常；③ 必要时调大 /etc/ssh/sshd_config 的 "
     "MaxStartups 后重启 sshd；④ 平台侧已做「同主机握手串行化 + 指数退避"
     "（带抖动）」，升级后同一主机并发握手收敛为 1，握手失败会自动重连重试"),
    (r"暂不支持真实克隆",
     "克隆：库型未纳入真实克隆引擎（现场部署版本落后）",
     "① 当前版本已支持 mysql/mariadb/postgresql/kingbase/dameng/oracle 的真实"
     "克隆；② 出现该文案说明现场跑的是**旧版本**，请更新到最新离线包/镜像后重试；"
     "③ 目标端需要什么见克隆页「克隆前置条件（目标端需要什么）」卡片"),
    (r"Java Virtual Machine is not running",
     "平台侧 JVM 未运行（JDBC 通道不可用，Oracle 11g 的 LogMiner 依赖它）",
     "① 确认平台已安装 JRE/JDK 8+ 并设置 JAVA_HOME；② 首次启动失败常因内存不足"
     "或架构不匹配，看平台 error.log 里的 JVM 启动堆栈；③ 重启平台进程让 JVM 重新"
     "初始化；④ 或改用 oracledb thin 直连（仅支持 12.1+）"),
    (r"expdp 不可用|impdp 不可用|rman 不可用",
     "Oracle 客户端未找到（本机回退分支的误导性提示）",
     "① expdp/impdp/rman 在**数据库服务器**上：需纳管 SSH 主机，并以数据库 OS 用户"
     "（db_os_user，默认 oracle）探测；② 若该提示来自本机回退，说明远端通道先失败，"
     "请看同一次任务里的远端错误；③ 升级新版本后错误会同时给出远端与本机两侧原因"),
    (r"incompatible server version.*does not support streaming",
     "平台侧流复制客户端版本低于源库服务端",
     "① 流复制要求客户端主版本 ≥ 服务端主版本；② 在**平台侧**安装与服务端同主"
     "版本或更高的 pg_receivewal（数据库服务器无需安装任何工具）；"
     "③ 注意客户端的 glibc 需求 ≤ 平台 glibc；④ 平台已支持同族版本择优与"
     "英文报错中文化，升级后此类问题会自动给出中文指引"),
    (r"70028|Create SOCKET connection failure|连接失败.*达梦|dmPython.*连接",
     "达梦数据库实例未启动 / 端口不通",
     "① 目标机上 dmserver 进程是否在跑；② dm.ini 的 PORT_NUM（不能用命令行 -port 传端口）；"
     "③ 防火墙与平台到 5236/5238 的连通性；④ 实例起来后重新触发实时捕获"),
    (r"SSH transport dead|SSHException|SSH banner|Unable to negotiate|No existing session",
     "SSH 通道异常（长会话被掐断 / 握手失败）",
     "① 大文件改走分离式后台打包（远端 tar 不随会话退出被杀）；"
     "② 检查中间设备/防火墙的会话时长上限与 MaxStartups；③ 平台侧已对握手抖动加重试"),
    (r"errno 28|No space left on device|ENOSPC|磁盘空间不足",
     "磁盘空间不足",
     "① 清理备份根目录的历史产物；② 检查备份产物与工作目录是否同盘；"
     "③ 备份前空间预检未通过时先扩容"),
    (r"1045|Access denied|认证失败|密码错误|ORA-01017|authentication failed",
     "数据库认证失败",
     "① 核对任务里的账号口令（口令含 @ 需按库型要求加引号/用 --no-defaults）；"
     "② 平台机上的 ~/.my.cnf 可能覆盖环境变量口令；③ 口令过期（Oracle ORA-28002）"),
    (r"connection refused|连接被拒绝|timeout|超时",
     "网络或服务不可达 / 操作超时",
     "① 确认目标端口监听与服务进程；② 确认平台到目标的路由与安全组；"
     "③ 长耗时操作提高超时阈值（大文件 sha 校验已按体积自动缩放）"),
    (r"command not found|not found|No such file or directory|rc=127|缺少.*工具",
     "客户端工具缺失 / 路径不对",
     "① 工具必须在平台侧（数据库服务器不装任何备份工具）；"
     "② 在任务的「高级选项 → 工具路径」指定客户端 bin 目录；"
     "③ openGauss/金仓等非默认路径需显式指定"),
    (r"rt\.cdc|实时.*启动失败|LogMiner.*失败|DM_LOGMNR",
     "实时保护（CDC）守护启动失败",
     "① 看任务「实时备份」页的 last_error 与健康灯；② 确认源库实例与归档/日志捕获权限；"
     "③ 平台不降级仿真，失败即停（可按需临时关闭该实时任务止损）"),
    (r"备份盘可用空间|低于保护阈值|可用空间 \d+GB",
     "备份盘可用空间低于保护阈值（任务被安全阻断）",
     "① 清理备份根目录历史产物 / 扩容备份盘；② 阈值可在任务或系统配置中调整；"
     "③ 阻断是保护行为（不会写出坏产物），不必改代码"),
    (r"resource_budget|并发预算|并发.*被阻断",
     "并发预算占满，任务被排队保护",
     "① 调大 BACKUP_PARALLEL_* 或等待在途任务结束；② 该类已是 WARNING 不再刷 ERROR"),
    (r"FileExistsError|锁|lock",
     "单实例锁残留 / 抢占冲突",
     "① 平台改为内核级文件锁（Windows 用 msvcrt.locking），进程退出自动释放；"
     "② 历史版本残留的锁文件可直接删除；③ 实时看门狗每 5 分钟自动接管"),
    (r"UNIQUE constraint|duplicate key|1062|唯一",
     "重复数据 / 重复写入",
     "① 迁移或同步的重跑策略（append 模式会重复写）；② 增量同步水位是否已持久化"),
    (r"ModuleNotFoundError|ImportError|No module named",
     "运行环境缺少依赖",
     "① 确认用的是平台自带虚拟环境/离线包内的解释器；② 离线环境禁止在线 pip 安装，"
     "依赖必须随离线包提供"),
]


def match_rule(template: str):
    for pat, cause, action in RULES:
        if re.search(pat, template, re.IGNORECASE):
            return cause, action
    return None, None


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def analyze(path: str, tail: int = 0, top: int = 12, spam: int = 20):
    if not os.path.exists(path):
        raise SystemExit(f"日志文件不存在：{path}")
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    if tail and tail > 0:
        lines = lines[-tail:]

    levels = Counter()
    loggers = Counter()
    by_template = defaultdict(lambda: {"count": 0, "first": "", "last": "",
                                       "logger": "", "level": "", "sample": ""})
    parsed = 0
    unparsed = 0
    hour_hist = Counter()          # 时间分布（MM-DD HH）——暴露周期性爆发
    cur = None                      # 当前事件（承接多行 Traceback）
    in_trace = False

    _exc_line_re = re.compile(
        r"^([A-Za-z_][\w.]*(?:Error|Exception|Failure|Warning|Interrupt|Exit))"
        r"(?::\s*(.*))?$")
    # 栈帧行：paramiko 用 logging 把堆栈**逐行**输出，每行都带时间戳+logger，
    # 因此"行级解析"会把一次握手失败拆成 20+ 个"模板"。这里按内容识别栈帧，
    # 归并成一次事件。
    _stack_line_re = re.compile(
        r"^\s*(?:File \"|\^+|Traceback|During handling|"
        r"(?:self|buf|sout|serr|raise|return|except|with|for|if|assert)\b)")

    def _flush():
        """把当前事件并入模板聚类。"""
        nonlocal cur
        if cur is None:
            return
        tpl = cur.get("template") or "Traceback（未捕获到异常类型行）"
        ent = by_template[tpl]
        ent["count"] += 1
        ent["first"] = ent["first"] or cur["ts"]
        ent["last"] = cur["ts"]
        ent["logger"] = cur["logger"]
        ent["level"] = cur["level"]
        if cur.get("sample"):
            ent["sample"] = cur["sample"][:400]
        hour_hist[cur["ts"][5:13]] += 1
        cur = None

    for raw in lines:
        line = raw.rstrip("\n")
        m = LINE_RE.match(line)
        if m:
            parsed += 1
            level = m.group("level")
            logger = (m.group("logger") or "").strip()
            msg = m.group("msg") or ""
            levels[level] += 1
            loggers[logger] += 1
            s = msg.strip()
            # ① 异常类型行：给当前 trace 定名（模板标识）
            em = _exc_line_re.match(s)
            if em:
                if cur is not None and not cur.get("template"):
                    cur["template"] = normalize(
                        ("%s: %s" % (em.group(1), em.group(2)))
                        if em.group(2) else em.group(1))
                    continue
                if cur is not None and em.group(2):
                    # 同一 trace 里的第二个异常类型（原始异常 + 包装异常）：
                    # 取信息量更大的那个作为标识（EOFError < SSHException: ...）
                    cand = normalize("%s: %s" % (em.group(1), em.group(2)))
                    if len(cand) > len(cur.get("template") or ""):
                        cur["template"] = cand
                    continue
                _flush()
                cur = {"ts": m.group("ts"), "logger": logger, "level": level,
                       "msg": msg, "sample": msg[:200],
                       "template": normalize(
                           ("%s: %s" % (em.group(1), em.group(2)))
                           if em.group(2) else em.group(1))}
                continue
            # ② 栈帧行：归并到当前事件（paramiko 逐行输出的堆栈）
            if _stack_line_re.match(s):
                if cur is None:
                    _flush()
                    cur = {"ts": m.group("ts"), "logger": logger, "level": level,
                           "msg": msg, "template": "", "sample": msg[:200]}
                elif len(cur.get("sample") or "") < 1200:
                    cur["sample"] = (cur.get("sample") or "") + " | " + s[:160]
                continue
            # ③ 普通事件
            _flush()
            cur = {"ts": m.group("ts"), "logger": logger, "level": level,
                   "msg": msg, "template": normalize(msg), "sample": msg[:200]}
            continue
        unparsed += 1
        # Traceback 续行并入当前事件：从异常类型行提炼标识，避免同一异常被
        # 拆成十几个"模板"（现场 paramiko banner 堆栈曾被拆成 21 个碎片）。
        if cur is not None and cur["level"] in ("ERROR", "CRITICAL"):
            s = line.strip()
            if not s:
                in_trace = False
                continue
            mm = _exc_line_re.match(s)
            if mm and not cur.get("template"):
                exc_type = mm.group(1)
                exc_msg = mm.group(2) or ""
                cur["template"] = normalize(
                    ("%s: %s" % (exc_type, exc_msg)) if exc_msg else exc_type)
            if len(cur.get("sample") or "") < 1200:
                cur["sample"] = (cur.get("sample") or "") + " | " + s[:200]
            continue
        in_trace = False
    _flush()

    ranked = sorted(by_template.items(), key=lambda kv: kv[1]["count"], reverse=True)
    spam_list = [(tpl, d) for tpl, d in ranked if d["count"] >= spam]

    return {
        "path": os.path.abspath(path),
        "file_size": os.path.getsize(path),
        "lines": len(lines),
        "parsed": parsed,
        "unparsed": unparsed,
        "levels": levels,
        "loggers": loggers,
        "ranked": ranked,
        "spam_list": spam_list,
        "hour_hist": hour_hist,
        "event_total": sum(d["count"] for _t, d in ranked),
    }


def render(rep: dict, top: int) -> str:
    out = []
    a = out.append
    a("=" * 78)
    a("AIDBM 错误日志归因报告")
    a("=" * 78)
    a(f"文件      : {rep['path']}")
    a(f"大小      : {rep['file_size'] / 1024.0:.1f} KB   总行数: {rep['lines']}")
    a(f"可解析行  : {rep['parsed']}    其他行(堆栈续行等): {rep['unparsed']}")
    a(f"归并事件  : {rep.get('event_total', 0)} 条（多行 Traceback 已按异常类型合并）")
    a("")

    a("-" * 78)
    a("一、按级别分布")
    a("-" * 78)
    total_err = sum(v for k, v in rep["levels"].items() if k in ("ERROR", "CRITICAL"))
    for lv, n in rep["levels"].most_common():
        pct = (n * 100.0 / max(1, sum(rep["levels"].values())))
        a(f"  {lv:<10} {n:>7} 行  {pct:5.1f}%")
    a(f"  → ERROR/CRITICAL 合计 {total_err} 行"
      f"（占全部日志 {total_err * 100.0 / max(1, sum(rep['levels'].values())):.1f}%）")
    a("")

    a("-" * 78)
    a("二、按功能来源（logger）分布 TOP 12")
    a("-" * 78)
    for lg, n in rep["loggers"].most_common(12):
        a(f"  {lg:<32} {n:>7} 行")
    a("")

    a("-" * 78)
    a("三、按错误模板聚类 TOP %d（同一模板 = 同一类错误的重复出现）" % top)
    a("-" * 78)
    for tpl, d in rep["ranked"][:top]:
        a(f"  [{d['count']:>6} 次] {d['level']:<8} {d['logger']}")
        a(f"           首次 {d['first']}   末次 {d['last']}")
        a(f"           模板: {tpl[:180]}")
        cause, action = match_rule(tpl)
        if cause:
            a(f"           ★ 根因方向: {cause}")
            a(f"             排查动作: {action}")
        a("")

    a("-" * 78)
    a("四、错误时间分布 TOP 10（暴露周期性爆发：定时任务集中触发 / 链路抖动）")
    a("-" * 78)
    hist = rep.get("hour_hist") or {}
    if hist:
        for hh, n in hist.most_common(10):
            a(f"  {hh}  {n:>5} 条事件")
        a("")
        top_hours = [h for h, _n in hist.most_common(3)]
        if len(top_hours) >= 2 and all(h[6:] == top_hours[0][6:] for h in top_hours):
            a(f"  ★ 高度规律：{len(hist)} 个时间点集中在每天 {top_hours[0][6:]} —— "
              "典型的**定时任务集中触发**（并发建连打满 sshd MaxStartups / "
              "同盘写入争用）。处置：错开该时点的任务，或提高并发/连接配额。")
            a("")
    else:
        a("  （无）")
        a("")

    if rep["spam_list"]:
        a("-" * 78)
        a("五、刷屏型错误（同一模板出现 ≥ 20 次，会淹没真实故障）")
        a("-" * 78)
        for tpl, d in rep["spam_list"][:10]:
            a(f"  {d['count']:>6} 次  {d['logger']:<26} {tpl[:140]}")
            cause, action = match_rule(tpl)
            if cause:
                a(f"           ★ {cause}")
                a(f"             {action}")
        a("")
        a("  说明：平台 v3.0.1 起内置「重复日志限频」（同模板去重 + 指数退避 + "
          "到期汇总），")
        a("        部署该版本后此类刷屏会自动收敛，无需人工删日志。")
    else:
        a("-" * 78)
        a("五、刷屏型错误：未发现（无同一模板 ≥ 20 次）")
        a("")

    a("=" * 78)
    a("说明：本报告只做归类与方向提示，不臆断结论；处置前请结合「备份记录 / "
      "操作日志 / 实时备份健康」页交叉核对。")
    a("=" * 78)
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="AIDBM 错误日志自助归因分析器")
    ap.add_argument("path", nargs="?", default=None, help="日志文件路径（默认 <程序目录>/logs/error.log）")
    ap.add_argument("-n", "--tail", type=int, default=0, help="只看最后 N 行")
    ap.add_argument("--top", type=int, default=12, help="模板聚类 TOP N（默认 12）")
    ap.add_argument("--spam", type=int, default=20, help="刷屏判定阈值（默认 20 次）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    if args.path:
        path = args.path
    else:
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(base, "logs", "error.log")

    rep = analyze(path, tail=args.tail, top=args.top, spam=args.spam)
    if args.json:
        print(json.dumps({
            "path": rep["path"],
            "lines": rep["lines"],
            "parsed": rep["parsed"],
            "levels": dict(rep["levels"]),
            "loggers": dict(rep["loggers"]),
            "templates": [
                {"count": d["count"], "level": d["level"], "logger": d["logger"],
                 "first": d["first"], "last": d["last"], "template": tpl,
                 "sample": d["sample"],
                 "root_cause": match_rule(tpl)[0]}
                for tpl, d in rep["ranked"][:args.top]
            ],
        }, ensure_ascii=False, indent=2))
    else:
        print(render(rep, args.top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
