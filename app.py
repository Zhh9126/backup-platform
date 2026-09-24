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
from core import error_codes
from auth import login_required
# 注意：api/__init__.py 已注册 api_bp 的全局鉴权/CSRF 钩子（必须在嵌套蓝图注册前声明）
from api import api_bp
from api import contract as api_contract

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
    # 0) 日志基础设施最先初始化：目录可写性兜底 / 轮转 / 崩溃落盘 / 脱敏 / 启动横幅。
    #    这样后续任何初始化失败都能在日志里看到原因（可执行文件与容器场景尤其重要）。
    try:
        from core import logging_setup
        logging_setup.init_logging("AIDBM")
    except Exception as _e:  # 日志不可用也不能阻止服务启动
        print(f"[日志] 初始化失败（降级为控制台输出）: {_e}", flush=True)

    app = Flask(__name__, template_folder="templates",
                static_folder="static")
    app.secret_key = config.SECRET_KEY
    app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024 * 1024  # 20GB（安装包可达 4GB+）
    # 会话安全：HttpOnly + SameSite=Lax 缓解 CSRF；会话超时按配置（默认 8 小时）
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
    app.config["PERMANENT_SESSION_LIFETIME"] = _dt.timedelta(seconds=config.SESSION_TIMEOUT)
    db.init_schema()
    # RBAC 启动种子：users 表为空时建首个 admin（兼容老部署）
    try:
        from core import rbac as _rbac
        u = _rbac.seed_admin_if_empty()
        if u:
            print(f"[startup] rbac: 已就绪内置管理员 {u.get('username')} (role={u.get('role')})")
    except Exception as e:
        print(f"[startup] rbac 初始化失败（不影响主流程）: {e}")
    # 本地备份存储位置（L1 落点）：界面配置 > 环境变量/config.json > 默认（程序目录）
    config.load_backup_root_from_db()
    # 可插拔数据库适配器：把 db_adapters 表中所有 enabled=1 的项注入引擎注册表
    try:
        from core import db_adapters
        n = db_adapters.register_all()
        if n:
            print(f"[startup] db_adapters: 已注册 {n} 个自定义适配器")
    except Exception as e:
        print(f"[startup] db_adapters 初始化失败（不影响内置引擎）: {e}")
    # 首屏注入 META：把「数据库类型 / 显示名 / 默认端口」直接渲染进页面。
    # 修复的真实缺陷：META 原先只由 app.js 在 DOMContentLoaded 里 await
    # /api/meta 后填充，比它先执行的页面脚本（static/js/sync.js 用
    # BKP.fillDbTypeSelect）拿到空数组，导致「数据同步」的类型下拉常年为空、
    # 「数据迁移」只能硬编码两个类型。渲染期注入后首屏即可用，且与接口同源
    # （都走 api.system.build_meta，不会两处漂移）。
    @app.context_processor
    def _inject_bkp_meta():
        try:
            from api.system import build_meta
            return {"bkp_meta": build_meta()}
        except Exception as e:  # noqa: BLE001 - 注入失败不能影响页面渲染
            print(f"[startup] bkp_meta 注入失败（前端将回退到 /api/meta）: {e}")
            return {"bkp_meta": {}}

    app.config["BACKUP_ROOT"] = str(config.get_backup_root())
    _root_info = config.backup_root_info()
    _store_log = db.get_logger("app")
    _store_log.info("[存储] 本地备份目录: %s（来源：%s）",
                    _root_info["path"], _root_info["source_label"])
    if _root_info["persistence"]["level"] == "warn":
        _store_log.warning("[存储] %s", _root_info["persistence"]["message"])
    # Docker 部署检查：备份目录落在容器可写层（无卷挂载）→ 数据随容器销毁
    try:
        from core import platform_env
        if platform_env.in_docker():
            _mounts = platform_env._parse_mountinfo()
            if not any(
                    _root_info["path"].rstrip("/") == m.rstrip("/")
                    or _root_info["path"].rstrip("/").startswith(m.rstrip("/") + "/")
                    for m, _src in _mounts):
                _store_log.warning(
                    "[存储] 检测到 Docker 容器内运行，但备份目录 %s 未挂载卷——"
                    "容器删除/重建后备份将全部丢失！请 docker run 时加 "
                    "-v <宿主机目录>:/data，或把备份存储位置改到已挂载目录。",
                    _root_info["path"])
    except Exception as _pe:  # noqa: BLE001 - 检查失败不影响启动
        _store_log.info("[存储] Docker 环境检查跳过: %s", _pe)

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

    # API 双前缀注册（差异 G5）：
    #   /api/v1  规范路径（契约冻结，见 docs/api_conventions.md）
    #   /api     兼容路径（响应带 Deprecation/Link 头，行为完全一致）
    # 同一个蓝图注册两次，视图函数只有一份实现，不存在两套逻辑漂移。
    app.register_blueprint(api_bp)
    app.register_blueprint(api_bp, url_prefix=api_contract.V1_PREFIX, name="api_v1")

    # ------------------------- 全局 API 异常处理 -------------------------
    # 统一把未捕获异常转为 JSON（/api 路径），避免裸 HTML 500；
    # 唯一约束冲突 → 409，其余 → 500 + 可读信息（含日志），页面路由不受影响。
    # 响应体统一为三段式（code/message/details，保留历史 error 字段）。
    @app.errorhandler(Exception)
    def _global_error_handler(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            if request.path.startswith("/api/"):
                code = error_codes.code_for_status(e.code or 500)
                return jsonify(error_codes.make_payload(
                    code, e.description or e.name)), e.code
            return e
        try:
            app.logger.exception("未处理异常 [%s %s]: %s",
                                 request.method, request.path, e)
        except Exception:
            pass
        msg = str(e) or e.__class__.__name__
        if e.__class__.__name__ == "IntegrityError" or "UNIQUE constraint" in msg \
                or "Duplicate entry" in msg:
            return jsonify(error_codes.make_payload(
                "AIDBM-1006", f"数据冲突（记录已存在或唯一字段重复）：{msg[:200]}")), 409
        return jsonify(error_codes.make_payload(
            "AIDBM-5001", f"服务器内部错误: {msg[:300]}")), 500

    # ------------------------- 鉴权 -------------------------
    @app.route("/login", methods=["GET", "POST"])
    def login_page():
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            u = (request.form.get("username") or data.get("username") or "").strip()
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
            # RBAC：优先 users 表（PBKDF2），回退 config 内置账号
            user_row = None
            try:
                import core.rbac as rbac
                # 启动种子（首次部署 users 表为空时建超管）
                rbac.seed_admin_if_empty()
                user_row = rbac.get_by_username(u, include_disabled=False)
                if user_row:
                    if not rbac.verify_password(p, user_row.get("password_hash") or ""):
                        user_row = None
            except Exception:
                user_row = None
            # 回退：config 内置账号（仅当 users 表无同名用户）
            if not user_row:
                try:
                    import core.rbac as rbac
                    if rbac.verify_builtin(u, p):
                        user_row = {"id": 0, "username": u,
                                    "display_name": u or "内置管理员",
                                    "role": "admin"}
                except Exception:
                    pass
            if user_row:
                _LOGIN_ATTEMPTS.pop(ip, None)
                # 标准化 session：dict 结构便于权限校验
                session["user"] = {
                    "id": user_row.get("id", 0),
                    "username": user_row.get("username") or u,
                    "display_name": user_row.get("display_name") or u,
                    "role": user_row.get("role") or "admin",
                }
                session.permanent = True
                # 记录登录（仅 DB 用户；内置账号无 id）
                try:
                    if user_row.get("id"):
                        import core.rbac as rbac
                        rbac.record_login(user_row["id"], ip)
                except Exception:
                    pass
                if is_json:
                    return jsonify({"ok": True, "user": session["user"]})
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

    @app.route("/object_storage")
    @login_required
    def object_storage_page():
        """对象存储备份（一级菜单）。

        与「数据库备份 / 文件备份」并列：对象存储此前只有后端引擎与接口，
        备份入口藏在通用任务表单的类型下拉里，用户找不到、也看不到桶级能力
        （桶浏览、预扫描、对象清单）。本页把这条链路单独呈现。
        """
        return render_template("object_storage.html", page="object_storage")

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

    @app.route("/agentless")
    @login_required
    def agentless_page():
        """无 Agent 接入面板：任务本次通道/侵入等级 + 目标端免装取证。"""
        return render_template("agentless.html", page="agentless")

    @app.route("/alert")
    @login_required
    def alert_page():
        return render_template("alert.html", page="alert")

    @app.route("/datamining")
    @login_required
    def datamining_page():
        return render_template("datamining.html", page="datamining")

    @app.route("/realtime")
    @login_required
    def realtime_page():
        """实时备份（CDC + CDP 合一）：PITR 恢复点时间轴 + 行级变更捕获与回放。"""
        return render_template("realtime.html", page="realtime")

    @app.route("/rt-timeline")
    @login_required
    def rt_timeline_page():
        """兼容旧入口：实时备份已与 CDC 合并到 /realtime（保留 task_id 等深链参数）。"""
        args = request.args.to_dict(flat=True)
        args.setdefault("tab", "rt")
        return redirect(url_for("realtime_page", **args))

    @app.route("/plugins")
    @login_required
    def plugins_page():
        """备份依赖插件管理（一键安装 xtrabackup / percona / mariabackup / pgbackrest 等）。"""
        return render_template("plugins.html", page="plugins")

    @app.route("/vm")
    @login_required
    def vm_page():
        """虚拟机备份：虚拟化平台纳管 → 保护策略 → 恢复点（PITR）→ 还原 / 克隆 / 恢复验证。"""
        return render_template("vm.html", page="vm")

    @app.route("/db-adapters")
    @login_required
    def db_adapters_page():
        """兼容旧入口：数据库类型已并入「数据库备份」页的「数据库类型」标签页。"""
        args = request.args.to_dict(flat=True)
        args["tab"] = "dbtypes"
        return redirect(url_for("tasks_page", **args))

    @app.route("/users")
    @login_required
    def users_page():
        """用户与角色管理（RBAC）。"""
        return render_template("users.html", page="users")

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

    @app.route("/cdc")
    @login_required
    def cdc_page():
        """兼容旧入口：CDC 已合并到 /realtime 的「CDC 变更捕获与回放」标签页。"""
        args = request.args.to_dict(flat=True)
        args["tab"] = "cdc"
        return redirect(url_for("realtime_page", **args))

    @app.route("/data-compare")
    @login_required
    def data_compare_page():
        """数据对比：验证恢复后的数据与原生产库的一致性。"""
        return render_template("data_compare.html", page="data-compare")

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host=config.WEB_HOST, port=config.WEB_PORT, debug=False)
