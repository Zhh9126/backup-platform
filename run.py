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
    scheduler.start_scheduler()
    # M4 摆渡收件箱后台 worker（离线环境增量包自动入库，10 分钟周期）
    try:
        from core import ferry_inbox
        ferry_inbox.start_bg_worker()
    except Exception as _e:
        import logging
        logging.getLogger("core.ferry").warning("摆渡收件箱启动失败: %s", _e)
    db.add_log("INFO", "system",
               f"AIDBM 启动，监听 {config.WEB_HOST}:{config.WEB_PORT}")
    app.run(host=config.WEB_HOST, port=config.WEB_PORT, debug=False)


if __name__ == "__main__":
    main()
