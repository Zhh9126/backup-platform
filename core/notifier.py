# -*- coding: utf-8 -*-
"""
通知模块：备份成功/失败后通过 Webhook / 钉钉 / 企业微信 / 飞书 / 邮件 发送提醒。

通知渠道在 config.NOTIFY_DEFAULTS["channels"] 中配置（list of dict）。
零额外依赖：HTTP 用标准库 urllib，邮件用 smtplib。
"""
import json
import urllib.request
import urllib.error
from email.mime.text import MIMEText
from email.header import Header

import config
import core.db as db


class Notifier:
    def __init__(self, task: dict = None, logger=None):
        self.task = task
        self.logger = logger or db.get_logger("notify")
        cfg = dict(config.NOTIFY_DEFAULTS)
        # 优先使用数据库（system_config.notify）中保存的通知配置
        try:
            raw = db.get_system_config("notify")
            if raw:
                db_cfg = json.loads(raw)
                for k in ("enabled", "on_success", "on_failure", "channels"):
                    if k in db_cfg:
                        cfg[k] = db_cfg[k]
        except Exception:
            pass
        self.cfg = cfg

    def _post_json(self, url: str, payload: dict) -> (bool, str):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return True, f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code}: {e.read().decode('utf-8','ignore')[:200]}"
        except Exception as e:
            return False, str(e)

    def _send_webhook(self, ch: dict, title: str, text: str) -> (bool, str):
        payload = {"title": title, "text": text, "event": ch.get("event", "")}
        return self._post_json(ch["url"], payload)

    def _send_dingtalk(self, ch: dict, title: str, text: str) -> (bool, str):
        payload = {"msgtype": "markdown",
                   "markdown": {"title": title, "text": f"## {title}\n\n{text}"}}
        return self._post_json(ch["url"], payload)

    def _send_wechat(self, ch: dict, title: str, text: str) -> (bool, str):
        payload = {"msgtype": "markdown",
                   "markdown": {"content": f"# {title}\n{text}"}}
        return self._post_json(ch["url"], payload)

    def _send_feishu(self, ch: dict, title: str, text: str) -> (bool, str):
        payload = {"msg_type": "interactive",
                   "card": {"header": {"title": {"tag": "plain_text",
                                                  "content": title}},
                            "elements": [{"tag": "div",
                                          "text": {"tag": "lark_md",
                                                   "content": text}}]}}
        return self._post_json(ch["url"], payload)

    def _send_email(self, ch: dict, title: str, text: str,
                   html: str = None) -> (bool, str):
        """发送邮件。

        - html 为空：自动用 email_template.render_test_email() 生成简单版
        - html 不为空：发 multipart/alternative（HTML + 纯文本兜底）
        - 端口 465 用 SMTP_SSL，其他用 SMTP + 可选 STARTTLS
        """
        try:
            import smtplib
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText
            from email.header import Header

            host = ch["smtp_host"]
            port = int(ch.get("smtp_port", 25))
            use_tls = bool(ch.get("use_tls"))
            user = ch.get("smtp_user") or ""
            pw = ch.get("smtp_password") or ""
            from_addr = ch.get("from_addr") or user
            to_list = ch.get("to", []) or []

            # 组装邮件
            if html:
                msg = MIMEMultipart("alternative")
                msg.attach(MIMEText(text or "", "plain", "utf-8"))
                msg.attach(MIMEText(html, "html", "utf-8"))
            else:
                # 没传 html：渲染一个简单测试邮件版（无 html 时用纯文本）
                if not text:
                    text = "(空内容)"
                msg = MIMEText(text, "plain", "utf-8")
            msg["Subject"] = Header(title, "utf-8")
            msg["From"] = from_addr
            msg["To"] = ", ".join(to_list)

            if port == 465:
                with smtplib.SMTP_SSL(host, port, timeout=15) as s:
                    if user:
                        s.login(user, pw)
                    s.sendmail(from_addr, to_list, msg.as_string())
            else:
                with smtplib.SMTP(host, port, timeout=15) as s:
                    s.ehlo()
                    if use_tls:
                        s.starttls()
                        s.ehlo()
                    if user:
                        s.login(user, pw)
                    s.sendmail(from_addr, to_list, msg.as_string())
            return True, "mail sent"
        except smtplib.SMTPAuthenticationError as e:
            return False, f"认证失败：账号或密码错误（QQ/163/Gmail 需使用「授权码」）: {e}"
        except smtplib.SMTPConnectError as e:
            return False, f"无法连接 SMTP {ch.get('smtp_host')}:{ch.get('smtp_port', 25)}: {e}"
        except Exception as e:
            return False, str(e)

    def _send_sms(self, ch: dict, title: str, text: str) -> (bool, str):
        """短信渠道：通用 HTTP 短信网关（离线环境标准做法）。

        企业内网自建短信网关（短信猫/运营商行业网关）千差万别，这里不绑定
        任何厂商 API，而是用「URL/参数模板」适配任意网关：

        - gateway_url：网关地址，支持占位符 {phone} {title} {content}
          （URL 编码后替换，适合 GET 查询串）
        - method：GET（默认）或 POST
        - params：POST 时的 JSON body 模板（值为字符串，同样支持占位符；
          GET 时若提供则以查询串方式附加）
        - headers：可选自定义请求头（现场网关鉴权各异，如 X-API-Key）
        - phones：手机号列表（逗号分隔字符串或数组），逐号发送

        网关返回 HTTP 2xx 即判定成功（内容级回执各异，不做统一解析）。
        """
        import urllib.parse

        url = (ch.get("gateway_url") or "").strip()
        if not url:
            return False, "缺少网关地址 gateway_url"
        phones = ch.get("phones") or []
        if isinstance(phones, str):
            phones = [x.strip() for x in phones.replace("；", ",").split(",") if x.strip()]
        if not phones:
            return False, "缺少手机号 phones"
        method = (ch.get("method") or "GET").upper()
        headers = dict(ch.get("headers") or {})
        params = ch.get("params") or {}

        def _subst(v, phone):
            if not isinstance(v, str):
                return v
            return (v.replace("{phone}", urllib.parse.quote_plus(phone))
                     .replace("{title}", urllib.parse.quote_plus(title))
                     .replace("{content}", urllib.parse.quote_plus(text)))

        failures, oks = [], 0
        for phone in phones:
            try:
                if method == "POST":
                    body = json.dumps(
                        {k: _subst(v, phone) for k, v in params.items()}).encode("utf-8")
                    headers.setdefault("Content-Type", "application/json")
                    req = urllib.request.Request(url, data=body, headers=headers)
                else:
                    qs = urllib.parse.urlencode(
                        {k: _subst(v, phone) for k, v in params.items()})
                    full = url + ("&" if "?" in url else "?") + qs if qs else url
                    req = urllib.request.Request(full, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    if 200 <= resp.status < 300:
                        oks += 1
                    else:
                        failures.append(f"{phone}: HTTP {resp.status}")
            except urllib.error.HTTPError as e:
                failures.append(f"{phone}: HTTP {e.code} {e.read().decode('utf-8','ignore')[:120]}")
            except Exception as e:
                failures.append(f"{phone}: {str(e)[:150]}")
        if failures and oks == 0:
            return False, "短信全部发送失败: " + "; ".join(failures[:3])
        if failures:
            return True, f"短信部分成功 {oks}/{len(phones)}，失败: {'; '.join(failures[:3])}"
        return True, f"短信已发送 {oks}/{len(phones)}（网关 HTTP 2xx）"

    def notify(self, event: str, title: str, text: str,
               html: str = None) -> None:
        """event: 'success' | 'failure'

        - text: 纯文本（兜底用；无 html 时也是邮件正文）
        - html: 渲染好的 HTML（可选；不传则用纯文本邮件）
        """
        if not self.cfg.get("enabled"):
            return
        if event == "success" and not self.cfg.get("on_success"):
            return
        if event == "failure" and not self.cfg.get("on_failure"):
            return
        channels = self.cfg.get("channels") or []
        if not channels:
            # 默认「备份结束即通知」已开启；未配置渠道时不静默吞掉，
            # 留一条可追溯提示引导管理员去系统设置配 SMTP。
            self.logger.info("备份结束通知已启用但未配置渠道，跳过外发"
                             "（系统设置 → 通知配置）")
            db.add_log("INFO", "notify", "备份结束通知未配置渠道，未外发")
            return
        for ch in channels:
            ctype = ch.get("type")
            try:
                if ctype == "webhook":
                    ok, detail = self._send_webhook(ch, title, text)
                elif ctype == "dingtalk":
                    ok, detail = self._send_dingtalk(ch, title, text)
                elif ctype == "wechat":
                    ok, detail = self._send_wechat(ch, title, text)
                elif ctype == "feishu":
                    ok, detail = self._send_feishu(ch, title, text)
                elif ctype == "email":
                    ok, detail = self._send_email(ch, title, text, html=html)
                elif ctype == "sms":
                    ok, detail = self._send_sms(ch, title, text)
                else:
                    ok, detail = False, f"未知渠道: {ctype}"
                self.logger.info("通知[%s/%s]: ok=%s %s", ctype, event, ok, detail)
                db.add_log("INFO", "notify",
                           f"{ctype} {event} -> ok={ok} {detail}")
            except Exception as e:
                self.logger.error("通知失败[%s]: %s", ctype, e)

    def notify_async(self, event: str, title: str, text: str,
                     html: str = None) -> None:
        """备份结束通知异步发送：SMTP/Webhook 抖动不拖慢备份主链路。

        线程为 daemon，服务常驻期间可正常送达；通知失败只记日志，绝不
        改变备份结果。调用方仍可同步调用 notify() 做即时测试。
        """
        import threading

        def _run():
            try:
                self.notify(event, title, text, html=html)
            except Exception as e:
                try:
                    self.logger.error("异步通知异常[%s]: %s", event, e)
                except Exception:
                    pass

        threading.Thread(target=_run, daemon=True, name="aidbm-notify").start()
