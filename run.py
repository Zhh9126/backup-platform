# -*- coding: utf-8 -*-
"""
启动入口：初始化并启动 Web 服务与后台调度器。

用法：
    python run.py
或：
    gunicorn -w 2 -b 0.0.0.0:8080 run:app
"""
import sys

# 灾备校验与恢复须在应用、调度器和元数据库均不可用时仍能执行。
if __name__ == "__main__" and sys.argv[1:2] == ["catalog"]:
    from core.catalog_cli import main as catalog_main

    sys.exit(catalog_main(sys.argv[2:]))

import config
import core.db as db
from app import create_app
from core import scheduler

app = create_app()


def _acquire_app_lock():
    """进程级单实例锁（内核 flock）。

    必须在 ``start_scheduler()`` **之前**抢：调度器会拉起实时备份守护并抢
    ``rt_supervisor.lock``。如果先启动调度器再绑端口，第二个 run.py 会先抢走
    实时守护锁、然后才因端口占用失败退出——形成「HTTP 在一个进程、实时守护在
    另一个进程」的分裂，两边都自认为正常。
    2026-11-14 实测：127461 占 8080 无守护，128831 持 rt 锁但无 HTTP，
    112 个实时任务的实际捕获状态无从判断。

    Returns:
        持有的 fd（保持到进程退出）；抢不到返回 None（调用方应直接退出）。
    """
    import os
    try:
        import fcntl
    except ImportError:          # 非 POSIX：不做单实例约束
        return True
    inst = getattr(config, "INSTANCE_DIR", None) or os.path.dirname(
        getattr(config, "META_DB_PATH", "instance/meta.db"))
    try:
        os.makedirs(str(inst), exist_ok=True)
        path = os.path.join(str(inst), "app.lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                os.close(fd)
            except OSError:
                pass
            return None
        try:
            os.ftruncate(fd, 0)
            os.write(fd, ("%s\n" % os.getpid()).encode("utf-8"))
        except OSError:
            pass
        return fd
    except OSError:
        return True          # 锁设施不可用时不阻断启动


def main():
    # 日志基础设施（幂等；create_app 中已初始化，这里确保 run.py 独立启动也可用）
    from core import logging_setup, oplog
    log_dir = logging_setup.init_logging("AIDBM")
    logger = db.get_logger("run")
    logger.info("启动入口 run.py 开始执行，日志目录: %s", log_dir)

    # 单实例守卫：抢不到锁说明已有实例在跑（端口/守护都在它那里），本进程
    # 必须干净退出，绝不能继续启动调度器制造「守护与 HTTP 分裂」。
    _app_lock = _acquire_app_lock()
    if _app_lock is None:
        logger.warning("检测到已有 AIDBM 实例在运行（instance/app.lock 被占用），"
                       "本进程退出以避免双进程：HTTP 与实时守护分离会让两边都看起来正常")
        db.add_log("WARNING", "system",
                   "启动被拒绝：已有实例持有单实例锁（instance/app.lock）")
        return
    # 操作日志保留策略：清理过期文件，避免长期运行占满磁盘
    try:
        removed = oplog.purge_old()
        if removed:
            logger.info("已清理 %s 个过期操作日志目录（保留 %s 天）",
                        removed, oplog.OPLOG_RETENTION_DAYS)
    except Exception as e:
        logger.warning("操作日志清理失败（不影响启动）: %s", e)

    scheduler.start_scheduler()
    # M4 摆渡收件箱后台 worker（离线环境增量包自动入库，10 分钟周期）
    try:
        from core import ferry_inbox
        ferry_inbox.start_bg_worker()
    except Exception as _e:
        import logging
        logging.getLogger("core.ferry").warning("摆渡收件箱启动失败: %s", _e)
    # 启动日志里明确写出备份落点：未部署对象存储时这是用户唯一能确认的位置
    _root = config.backup_root_info()
    _locs = logging_setup.log_locations()
    db.add_log("INFO", "system",
               f"AIDBM 启动，监听 {config.WEB_HOST}:{config.WEB_PORT}；"
               f"本地备份目录 {_root['path']}（{_root['source_label']}）；"
               f"日志目录 {_locs['log_dir']}（{_locs['log_dir_source']}）")
    if _root["persistence"]["level"] == "warn":
        db.add_log("WARNING", "system", f"备份目录持久化风险：{_root['persistence']['message']}")
    if not _locs.get("writable"):
        db.add_log("WARNING", "system",
                   f"日志目录不可写，已降级到 {_locs['log_dir']}，请配置持久化目录 LOG_DIR")
    # threaded：内置服务器必须多线程，否则批量任务提交/批量触发备份时请求排队超时。
    app.run(host=config.WEB_HOST, port=config.WEB_PORT, debug=False,
            threaded=config.WEB_THREADED)


if __name__ == "__main__":
    main()
