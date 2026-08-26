# -*- coding: utf-8 -*-
"""
Flask 应用主程序。

职责：
- 初始化元数据数据库（SQLite）
- 注册 REST API 蓝图
- 提供页面路由（仪表盘 / 任务 / 记录 / 恢复 / 设置 / 登录）
"""
import os
import hmac
import time
import datetime as _dt

from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify)

import config
import core.db as db
from auth import login_required
# 注意：api/__init__.py 已注册 api_bp 的全局鉴权/CSRF 钩子（必须在嵌套蓝图注册前声明）
from api import api_bp

# ------------------------- 登录安全状态（内存限流） -------------------------
# ip -> [连续失败次数, 首次失败时间戳]
_LOGIN_ATTEMPTS = {}


def _login_locked(ip: str) -> bool:
    rec = _LOGIN_ATTEMPTS.get(ip)
    if not rec:
        return False
    count, first = rec
    if count >= config.LOGIN_MAX_FAILS:
        if time.time() - first < config.LOGIN_LOCK_MINUTES * 60:
            return True
        _LOGIN_ATTEMPTS.pop(ip, None)  # 锁定窗口过期，清零
    return False


def _register_login_fail(ip: str):
    rec = _LOGIN_ATTEMPTS.setdefault(ip, [0, time.time()])
    if time.time() - rec[1] > config.LOGIN_LOCK_MINUTES * 60:
        rec[0] = 0
        rec[1] = time.time()
    rec[0] += 1


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates",
                static_folder="static")
    app.secret_key = config.SECRET_KEY
    app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024 * 1024  # 20GB（安装包可达 4GB+）
    app.config["BACKUP_ROOT"] = str(config.BACKUP_ROOT)
    # 会话安全：HttpOnly + SameSite=Lax 缓解 CSRF；会话超时按配置（默认 8 小时）
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
    app.config["PERMANENT_SESSION_LIFETIME"] = _dt.timedelta(seconds=config.SESSION_TIMEOUT)
    db.init_schema()

    @app.after_request
    def _security_headers(resp):
        # 安全响应头（CSP 允许同源 + 内联脚本/样式，兼顾现有页面）
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-XSS-Protection", "1; mode=block")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "font-src 'self' data:; connect-src 'self'",
        )
        return resp

    app.register_blueprint(api_bp)

    # ------------------------- 鉴权 -------------------------
    @app.route("/login", methods=["GET", "POST"])
    def login_page():
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            u = request.form.get("username") or data.get("username") or ""
            p = request.form.get("password") or data.get("password") or ""
            ip = request.remote_addr or "unknown"
            is_json = request.headers.get("Content-Type", "").startswith("application/json")
            # 暴力破解防护：连续失败达到上限后锁定该 IP
            if _login_locked(ip):
                remain = config.LOGIN_LOCK_MINUTES
                if is_json:
                    return jsonify({"error": f"登录失败次数过多，IP 已被锁定，请 {remain} 分钟后再试"}), 429
                return render_template("login.html",
                                       error=f"登录失败次数过多，IP 已被锁定，请 {remain} 分钟后再试")
            # 常量时间比较，避免时序侧信道
            ok = (hmac.compare_digest(u, config.WEB_USERNAME)
                  and hmac.compare_digest(p, config.WEB_PASSWORD))
            if ok:
                _LOGIN_ATTEMPTS.pop(ip, None)
                session["user"] = u
                session.permanent = True
                if is_json:
                    return jsonify({"ok": True})
                return redirect(url_for("dashboard_page"))
            _register_login_fail(ip)
            if is_json:
                return jsonify({"error": "用户名或密码错误"}), 401
            return render_template("login.html", error="用户名或密码错误")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login_page"))

    # ------------------------- 页面 -------------------------
    @app.route("/")
    @login_required
    def dashboard_page():
        return render_template("dashboard.html", page="dashboard")

    @app.route("/tasks")
    @login_required
    def tasks_page():
        return render_template("tasks.html", page="tasks")

    @app.route("/records")
    @login_required
    def records_page():
        return render_template("records.html", page="records")

    @app.route("/restore")
    @login_required
    def restore_page():
        return render_template("restore.html", page="restore")

    @app.route("/settings")
    @login_required
    def settings_page():
        return render_template("settings.html", page="settings")

    @app.route("/logs")
    @login_required
    def logs_page():
        return render_template("logs.html", page="logs")

    @app.route("/file_backup")
    @login_required
    def file_backup_page():
        return render_template("file_backup.html", page="file_backup")

    @app.route("/sync")
    @login_required
    def sync_page():
        return render_template("sync.html", page="sync")

    @app.route("/restore_records")
    @login_required
    def restore_records_page():
        return render_template("restore_records.html", page="restore_records")

    @app.route("/deploy")
    @login_required
    def deploy_page():
        return render_template("deploy.html", page="deploy")

    @app.route("/vdb")
    @login_required
    def vdb_page():
        return render_template("vdb.html", page="vdb")

    @app.route("/drills")
    @login_required
    def drills_page():
        return render_template("drills.html", page="drills")

    @app.route("/inspection")
    @login_required
    def inspection_page():
        return render_template("inspection.html", page="inspection")

    @app.route("/storage")
    @login_required
    def storage_page():
        return render_template("storage.html", page="storage")

    @app.route("/protection")
    @login_required
    def protection_page():
        return render_template("protection.html", page="protection")

    @app.route("/migration")
    @login_required
    def migration_page():
        return render_template("migration.html", page="migration")

    @app.route("/clone")
    @login_required
    def clone_page():
        return render_template("clone.html", page="clone")

    @app.route("/dr-link")
    @login_required
    def dr_link_page():
        return render_template("drlink.html", page="dr-link")

    @app.route("/agent")
    @login_required
    def agent_page():
        return render_template("agent.html", page="agent")

    @app.route("/alert")
    @login_required
    def alert_page():
        return render_template("alert.html", page="alert")

    @app.route("/datamining")
    @login_required
    def datamining_page():
        return render_template("datamining.html", page="datamining")

    @app.route("/rt-timeline")
    @login_required
    def rt_timeline_page():
        """准 CDP 实时备份时间轴（PITR 选点与恢复）。"""
        return render_template("rt_timeline.html", page="rt_timeline")

    @app.route("/plugins")
    @login_required
    def plugins_page():
        """备份依赖插件管理（一键安装 xtrabackup / percona / mariabackup / pgbackrest 等）。"""
        return render_template("plugins.html", page="plugins")

    @app.route("/operations")
    @login_required
    def operations_page():
        """运维运营分析：超长备份 / 超频备份统计与阈值配置、Excel 导出。"""
        return render_template("operations.html", page="operations")

    @app.route("/restore-verify")
    @login_required
    def restore_verify_page():
        """恢复校验策略与恢复测试报告。"""
        return render_template("restore_verify.html", page="restore-verify")

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host=config.WEB_HOST, port=config.WEB_PORT, debug=False)
