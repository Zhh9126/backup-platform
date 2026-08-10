# -*- coding: utf-8 -*-
"""
邮件 HTML 模板生成器。

设计原则：
- 兼容主流邮箱（QQ/163/Gmail/Outlook/企业微信邮箱）
- 全部用内联 CSS（外部 <style>/<head> 在很多客户端被剥除）
- 移动端友好：max-width 600、padding 适中、字号可读
- 状态色用 Tailwind 风：ok=#059669, warn=#d97706, fail=#dc2626
- 表格布局（不用 div 嵌套，部分客户端不渲染 div float/flex）

支持的场景：
1. 备份巡检告警（inspection.alert）
2. 备份任务成功 / 失败（backup.result）
3. 测试邮件（test.message）
"""
import html
from datetime import datetime


# ---- 状态色 / 图标映射 ----
_STATUS_META = {
    "ok":   {"color": "#059669", "bg": "#ecfdf5", "border": "#a7f3d0",
             "icon": "✓", "label": "正常"},
    "pass": {"color": "#059669", "bg": "#ecfdf5", "border": "#a7f3d0",
             "icon": "✓", "label": "正常"},
    "warn": {"color": "#d97706", "bg": "#fffbeb", "border": "#fcd34d",
             "icon": "⚠", "label": "警告"},
    "fail": {"color": "#dc2626", "bg": "#fef2f2", "border": "#fca5a5",
             "icon": "✕", "label": "异常"},
    "running": {"color": "#2563eb", "bg": "#eff6ff", "border": "#bfdbfe",
                "icon": "⟳", "label": "进行中"},
    "error":  {"color": "#dc2626", "bg": "#fef2f2", "border": "#fca5a5",
               "icon": "✕", "label": "失败"},
    "success": {"color": "#059669", "bg": "#ecfdf5", "border": "#a7f3d0",
                "icon": "✓", "label": "成功"},
    "simulated": {"color": "#6366f1", "bg": "#eef2ff", "border": "#c7d2fe",
                  "icon": "⊙", "label": "仿真"},
    "skipped": {"color": "#6b7280", "bg": "#f3f4f6", "border": "#d1d5db",
                "icon": "⊘", "label": "跳过"},
    "failed": {"color": "#dc2626", "bg": "#fef2f2", "border": "#fca5a5",
               "icon": "✕", "label": "失败"},
}

_BADGE_META = {
    "ok":   {"color": "#065f46", "bg": "#d1fae5"},
    "pass": {"color": "#065f46", "bg": "#d1fae5"},
    "warn": {"color": "#92400e", "bg": "#fef3c7"},
    "fail": {"color": "#991b1b", "bg": "#fee2e2"},
    "running": {"color": "#1e40af", "bg": "#dbeafe"},
    "error":  {"color": "#991b1b", "bg": "#fee2e2"},
    "success": {"color": "#065f46", "bg": "#d1fae5"},
    "simulated": {"color": "#3730a3", "bg": "#e0e7ff"},
    "skipped": {"color": "#374151", "bg": "#e5e7eb"},
    "failed": {"color": "#991b1b", "bg": "#fee2e2"},
}


def _esc(s) -> str:
    """HTML 转义。None/数字 都安全。"""
    if s is None:
        return ""
    return html.escape(str(s))


def _fmt_time(iso_str: str) -> str:
    """把 ISO 时间格式化为 '2026-07-20 16:45:00'。"""
    if not iso_str:
        return "-"
    s = str(iso_str)
    # 简单替换 T 为空格，去掉毫秒和时区
    if "T" in s:
        s = s.replace("T", " ")
    if "." in s:
        s = s.split(".")[0]
    if "+" in s:
        s = s.split("+")[0]
    if s.endswith("Z"):
        s = s[:-1]
    return s


def _status_meta(status: str) -> dict:
    s = (status or "").lower()
    return _STATUS_META.get(s, {
        "color": "#6b7280", "bg": "#f3f4f6", "border": "#d1d5db",
        "icon": "·", "label": s or "未知",
    })


def _badge_meta(level: str) -> dict:
    """检查项级别 badge: ok/warn/fail"""
    s = (level or "").lower()
    return _BADGE_META.get(s, {"color": "#374151", "bg": "#e5e7eb"})


def _check_html(level: str, label: str, msg: str) -> str:
    """单条检查项的 HTML。"""
    m = _badge_meta(level)
    return (
        f'<tr><td style="padding:6px 0;font-size:13px;color:#374151;vertical-align:top;">'
        f'<span style="display:inline-block;min-width:42px;padding:2px 8px;'
        f'font-size:11px;font-weight:600;color:{m["color"]};background:{m["bg"]};'
        f'border-radius:4px;text-align:center;margin-right:8px;">{_esc(level)}</span>'
        f'<strong style="color:#111827;">{_esc(label)}</strong> '
        f'<span style="color:#6b7280;">{_esc(msg)}</span></td></tr>'
    )


def _task_card(task: dict, accent_color: str) -> str:
    """单个任务卡片 HTML。"""
    name = _esc(task.get("name") or task.get("task_name") or "未知任务")
    db_type = _esc(task.get("db_type") or "-")
    status = (task.get("status") or "warn").lower()
    sm = _status_meta(status)
    checks_html = ""
    for c in (task.get("checks") or []):
        # 兼容: [label, level, msg] 或 dict
        if isinstance(c, (list, tuple)) and len(c) >= 3:
            label, level, msg = c[0], c[1], c[2]
        else:
            label = c.get("label", "·")
            level = c.get("level", "info")
            msg = c.get("msg", "")
        checks_html += _check_html(level, label, msg)
    if not checks_html:
        detail = _esc(task.get("detail") or "（无明细）")
        checks_html = (
            f'<tr><td style="padding:6px 0;font-size:13px;color:#6b7280;">{detail}</td></tr>'
        )
    return f"""
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
       style="border:1px solid {sm['border']};border-radius:6px;margin-bottom:12px;background:#ffffff;">
  <tr>
    <td style="background:{sm['bg']};padding:12px 16px;border-bottom:1px solid {sm['border']};border-radius:6px 6px 0 0;">
      <span style="display:inline-block;width:22px;height:22px;line-height:22px;text-align:center;
                   background:{sm['color']};color:#ffffff;border-radius:50%;font-size:12px;font-weight:700;">{sm['icon']}</span>
      <strong style="margin-left:8px;font-size:15px;color:#111827;">{name}</strong>
      <span style="display:inline-block;margin-left:8px;padding:2px 8px;font-size:11px;
                   background:#dbeafe;color:#1e40af;border-radius:4px;">{db_type}</span>
      <span style="display:inline-block;margin-left:8px;padding:2px 8px;font-size:11px;
                   font-weight:600;color:{sm['color']};background:#ffffff;border:1px solid {sm['color']};border-radius:4px;">{_esc(sm['label'])}</span>
    </td>
  </tr>
  <tr>
    <td style="padding:12px 16px;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
        {checks_html}
      </table>
    </td>
  </tr>
</table>"""


def _section_heading(text: str) -> str:
    return (
        f'<div style="font-size:14px;font-weight:600;color:#374151;'
        f'margin:18px 0 10px 0;padding-bottom:6px;border-bottom:1px solid #e5e7eb;">'
        f'{_esc(text)}</div>'
    )


def _kv_table(rows: list) -> str:
    """键值对表格：[('键', '值'), ...]"""
    cells = "".join(
        f'<tr><td style="padding:4px 12px 4px 0;font-size:13px;color:#6b7280;'
        f'vertical-align:top;white-space:nowrap;">{_esc(k)}</td>'
        f'<td style="padding:4px 0;font-size:13px;color:#111827;'
        f'word-break:break-all;">{_esc(v)}</td></tr>'
        for k, v in rows
    )
    return f'<table role="presentation" cellpadding="0" cellspacing="0" border="0">{cells}</table>'


# =============== 公共 API ===============

def render_inspection_alert(summary: dict, failures: list, triggered_by: str = "manual") -> str:
    """渲染巡检告警邮件 HTML。

    summary: {total, pass, warn, fail}
    failures: [{task_id, name, db_type, detail}, ...]
    """
    if not summary:
        summary = {"total": 0, "pass": 0, "warn": 0, "fail": 0}
    fail = summary.get("fail", 0)
    warn = summary.get("warn", 0)
    total = summary.get("total", len(failures))
    pass_count = summary.get("pass", total - fail - warn)
    trigger_label = "定时任务" if triggered_by == "schedule" else "手动触发"
    accent = "#dc2626" if fail else "#d97706" if warn else "#059669"
    title_prefix = "巡检发现异常" if fail else "巡检有警告" if warn else "巡检全部正常"
    title_icon = "✕" if fail else "⚠" if warn else "✓"

    # 任务卡片：把 fail 行转成结构化 checks
    task_cards = []
    for f in failures:
        # detail 是 "; [ok] xxxx; [warn] yyyy; [fail] zzz" 格式
        detail = f.get("detail", "") or ""
        checks = []
        for seg in detail.split(";"):
            seg = seg.strip()
            if not seg:
                continue
            m = seg.strip("[]")
            if m.startswith("ok]"):
                level, rest = "ok", m[3:].strip()
            elif m.startswith("warn]"):
                level, rest = "warn", m[5:].strip()
            elif m.startswith("fail]"):
                level, rest = "fail", m[5:].strip()
            else:
                level, rest = "info", seg
            # rest 形如 "label: msg"
            if ":" in rest:
                label, msg = rest.split(":", 1)
                checks.append({"label": label.strip(), "level": level, "msg": msg.strip()})
            else:
                checks.append({"label": rest, "level": level, "msg": ""})
        task_cards.append(_task_card({
            "name": f.get("name"),
            "db_type": f.get("db_type"),
            "status": "fail" if fail else "warn",
            "checks": checks,
        }, accent))

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f7fa;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f5f7fa;padding:24px 0;">
<tr><td align="center">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600"
       style="max-width:600px;background:#ffffff;border-radius:10px;overflow:hidden;
              box-shadow:0 4px 16px rgba(0,0,0,0.06);">

  <tr>
    <td style="background:linear-gradient(135deg,{accent} 0%,{_lighten(accent)} 100%);padding:24px 28px;">
      <div style="font-size:22px;color:#ffffff;font-weight:700;line-height:1.3;">
        <span style="display:inline-block;width:32px;height:32px;line-height:32px;text-align:center;
                     background:rgba(255,255,255,0.25);border-radius:50%;margin-right:10px;">{title_icon}</span>
        {title_prefix}
      </div>
      <div style="font-size:13px;color:rgba(255,255,255,0.92);margin-top:6px;">
        {trigger_label} · 异常 <strong style="color:#ffffff;">{fail}</strong> · 警告 {warn} · 正常 {pass_count} · 共 {total} 项
      </div>
    </td>
  </tr>

  <tr>
    <td style="padding:24px 28px 8px 28px;">
      { _section_heading("巡检概况") }
      { _kv_table([
          ("巡检时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
          ("触发方式", trigger_label),
          ("异常任务数", f"{fail} 项"),
      ]) }
    </td>
  </tr>

  <tr>
    <td style="padding:8px 28px 24px 28px;">
      { _section_heading(f"异常任务详情（{fail} 项）") }
      {''.join(task_cards) if task_cards else '<div style="text-align:center;color:#6b7280;padding:20px;">无</div>'}
      <div style="margin-top:18px;padding:14px 16px;background:#fffbeb;border:1px solid #fde68a;
                  border-radius:6px;font-size:13px;color:#92400e;line-height:1.6;">
        ⚠ 请尽快排查上述异常任务，避免数据保护出现盲区。
      </div>
    </td>
  </tr>

  <tr>
    <td style="background:#f9fafb;padding:14px 28px;text-align:center;
               font-size:12px;color:#9ca3af;border-top:1px solid #e5e7eb;">
      数据备份管理平台 · 自动巡检告警
    </td>
  </tr>

</table>
</td></tr>
</table>
</body></html>"""


def render_backup_result(task: dict, result: dict, trigger_label: str = "调度执行") -> str:
    """渲染备份成功/失败邮件 HTML。

    task: {name, db_type, host, port, db_name, backup_type}
    result: {status, size_bytes, duration_sec, message, backup_path}
    """
    status = (result.get("status") or "failed").lower()
    success = status in ("success", "simulated")
    accent = "#059669" if success else "#dc2626"
    title_icon = "✓" if success else "✕"
    title_text = f"备份{'成功' if success else '失败'}：{task.get('name','-')}"

    # 大小 / 耗时友好化
    size_b = result.get("size_bytes") or 0
    dur_s = result.get("duration_sec") or 0
    size_h = _human_size(size_b)
    dur_h = f"{dur_s:.1f} 秒" if dur_s else "-"

    # 状态色块
    sm = _status_meta(status)

    body_rows = [
        ("任务名称", task.get("name") or "-"),
        ("数据库类型", task.get("db_type") or "-"),
        ("目标", f"{task.get('host','')}:{task.get('port','')} / {task.get('db_name','')}"),
        ("备份类型", task.get("backup_type") or "-"),
        ("触发方式", trigger_label),
        ("状态", sm["label"]),
        ("耗时", dur_h),
        ("备份大小", size_h),
    ]
    if result.get("backup_path"):
        body_rows.append(("备份路径", result.get("backup_path")))
    if result.get("message"):
        msg = result["message"]
        if len(msg) > 800:
            msg = msg[:800] + "..."
        body_rows.append(("详细信息", msg))

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f7fa;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f5f7fa;padding:24px 0;">
<tr><td align="center">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600"
       style="max-width:600px;background:#ffffff;border-radius:10px;overflow:hidden;
              box-shadow:0 4px 16px rgba(0,0,0,0.06);">

  <tr>
    <td style="background:linear-gradient(135deg,{accent} 0%,{_lighten(accent)} 100%);padding:22px 28px;">
      <div style="font-size:20px;color:#ffffff;font-weight:700;line-height:1.3;">
        <span style="display:inline-block;width:30px;height:30px;line-height:30px;text-align:center;
                     background:rgba(255,255,255,0.25);border-radius:50%;margin-right:10px;">{title_icon}</span>
        {_esc(title_text)}
      </div>
    </td>
  </tr>

  <tr>
    <td style="padding:24px 28px;">
      { _kv_table(body_rows) }
    </td>
  </tr>

  <tr>
    <td style="background:#f9fafb;padding:14px 28px;text-align:center;
               font-size:12px;color:#9ca3af;border-top:1px solid #e5e7eb;">
      数据备份管理平台 · 备份任务通知
    </td>
  </tr>

</table>
</td></tr>
</table>
</body></html>"""


def render_test_email(meta: dict) -> str:
    """渲染测试邮件 HTML。"""
    rows = [
        ("发送时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("发件人", meta.get("from_addr") or meta.get("smtp_user") or ""),
        ("收件人", ", ".join(meta.get("to", []))),
        ("SMTP 主机", f"{meta.get('smtp_host')}:{meta.get('smtp_port', 25)}"),
        ("使用 TLS", "是" if meta.get("use_tls") else "否"),
    ]
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f7fa;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f5f7fa;padding:24px 0;">
<tr><td align="center">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600"
       style="max-width:600px;background:#ffffff;border-radius:10px;overflow:hidden;
              box-shadow:0 4px 16px rgba(0,0,0,0.06);">

  <tr>
    <td style="background:linear-gradient(135deg,#0d9488 0%,#14b8a6 100%);padding:22px 28px;">
      <div style="font-size:20px;color:#ffffff;font-weight:700;line-height:1.3;">
        <span style="display:inline-block;width:30px;height:30px;line-height:30px;text-align:center;
                     background:rgba(255,255,255,0.25);border-radius:50%;margin-right:10px;">✉</span>
        通知测试邮件
      </div>
      <div style="font-size:13px;color:rgba(255,255,255,0.92);margin-top:6px;">
        如果你看到这封邮件，说明通知配置正确。
      </div>
    </td>
  </tr>

  <tr>
    <td style="padding:24px 28px;">
      { _kv_table(rows) }
      <div style="margin-top:18px;padding:14px 16px;background:#ecfdf5;border:1px solid #a7f3d0;
                  border-radius:6px;font-size:13px;color:#065f46;line-height:1.6;">
        ✓ 备份告警 / 巡检异常会通过该渠道送达。请同时检查垃圾邮件箱。
      </div>
    </td>
  </tr>

  <tr>
    <td style="background:#f9fafb;padding:14px 28px;text-align:center;
               font-size:12px;color:#9ca3af;border-top:1px solid #e5e7eb;">
      数据备份管理平台 · 通知配置测试
    </td>
  </tr>

</table>
</td></tr>
</table>
</body></html>"""


# ---- 内部工具 ----
def _lighten(hex_color: str, ratio: float = 0.15) -> str:
    """把 hex 颜色提亮 ratio（用于渐变右端）。"""
    if not hex_color or not hex_color.startswith("#") or len(hex_color) != 7:
        return hex_color
    try:
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
        r = min(255, int(r + (255 - r) * ratio))
        g = min(255, int(g + (255 - g) * ratio))
        b = min(255, int(b + (255 - b) * ratio))
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return hex_color


def _human_size(n) -> str:
    """字节 → 人类可读。"""
    try:
        n = float(n or 0)
    except Exception:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    for u in units:
        if n < 1024:
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"
