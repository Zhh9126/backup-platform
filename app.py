# -*- coding: utf-8 -*-
"""
Flask 应用主程序。

职责：
- 初始化元数据数据库（SQLite）
- 注册 REST API 蓝图
- 提供页面路由（仪表盘 / 任务 / 记录 / 恢复 / 设置 / 登录）
"""
from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify)

import config
import core.db as db
from auth import login_required
from api import api_bp


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates",
                static_folder="static")
    app.secret_key = config.SECRET_KEY
    app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024 * 1024  # 20GB（安装包可达 4GB+）
    app.config["BACKUP_ROOT"] = str(config.BACKUP_ROOT)
    db.init_schema()
    app.register_blueprint(api_bp)

    # ------------------------- 鉴权 -------------------------
    @app.route("/login", methods=["GET", "POST"])
    def login_page():
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            u = request.form.get("username") or data.get("username") or ""
            p = request.form.get("password") or data.get("password") or ""
            if u == config.WEB_USERNAME and p == config.WEB_PASSWORD:
                session["user"] = u
                session.permanent = True
                if request.headers.get("Content-Type", "").startswith("application/json"):
                    return jsonify({"ok": True})
                return redirect(url_for("dashboard_page"))
            if request.headers.get("Content-Type", "").startswith("application/json"):
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

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host=config.WEB_HOST, port=config.WEB_PORT, debug=False)
