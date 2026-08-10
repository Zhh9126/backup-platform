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
                else:
                    ok, detail = False, f"未知渠道: {ctype}"
                self.logger.info("通知[%s/%s]: ok=%s %s", ctype, event, ok, detail)
                db.add_log("INFO", "notify",
                           f"{ctype} {event} -> ok={ok} {detail}")
            except Exception as e:
                self.logger.error("通知失败[%s]: %s", ctype, e)
