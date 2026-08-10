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
    db.add_log("INFO", "system",
               f"备份管理平台启动，监听 {config.WEB_HOST}:{config.WEB_PORT}")
    app.run(host=config.WEB_HOST, port=config.WEB_PORT, debug=False)


if __name__ == "__main__":
    main()
