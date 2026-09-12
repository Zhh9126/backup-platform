# -*- coding: utf-8 -*-
"""
启动入口：初始化并启动 Web 服务与后台调度器。

用法：
    python run.py
或：
    gunicorn -w 2 -b 0.0.0.0:8080 run:app
"""
import config
import core.db as db
from app import create_app
from core import scheduler

app = create_app()


def main():
    # 日志基础设施（幂等；create_app 中已初始化，这里确保 run.py 独立启动也可用）
    from core import logging_setup, oplog
    log_dir = logging_setup.init_logging("AIDBM")
    logger = db.get_logger("run")
    logger.info("启动入口 run.py 开始执行，日志目录: %s", log_dir)
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
    app.run(host=config.WEB_HOST, port=config.WEB_PORT, debug=False)


if __name__ == "__main__":
    main()
