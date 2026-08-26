# -*- coding: utf-8 -*-
"""真实浏览器测试：通过 Playwright 驱动 Chromium，走完整「新建物理备份任务 → 保存」流程，
捕获 console 错误、toast、网络请求，直接给出 PASS/FAIL。"""
import sys, time, json
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8080"

def main():
    console_errors = []
    network_errors = []
    api_responses = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        page.on("console", lambda msg: console_errors.append(f"[{msg.type}] {msg.text}") if msg.type in ("error", "warning") else None)
        page.on("pageerror", lambda exc: console_errors.append(f"[pageerror] {exc}"))
        page.on("response", lambda r: api_responses.append((r.request.method, r.url, r.status))
                if "/api/" in r.url and r.request.method == "POST" else None)

        # 1) 登录
        page.goto(BASE + "/login", wait_until="load")
        page.fill("input[name=username]", "admin")
        page.fill("input[name=password]", "admin123")
        page.click("button[type=submit]")
        page.wait_for_url(BASE + "/", timeout=10000)
        print("[1] 登录 OK")

        # 2) 进入任务页
        page.goto(BASE + "/tasks", wait_until="load")
        page.wait_for_selector("#newTaskBtn", timeout=10000)
        print("[2] 进入 /tasks OK")

        # 3) 点新建任务
        page.click("#newTaskBtn")
        page.wait_for_selector("#taskModal.show", timeout=5000)
        print("[3] 新建模态框弹出")

        # 4) 填表（业务系统、任务名、Oracle、物理、全量）
        page.fill("#t_biz_system", "oracle核心系统")
        page.fill("#t_name", f"playwright测试-物理备份-{int(time.time())}")
        # db_type 用 select（如果存在）
        if page.query_selector("#t_db_type"):
            page.select_option("#t_db_type", "oracle")
        if page.query_selector("#t_backup_mode"):
            page.select_option("#t_backup_mode", "physical")
        if page.query_selector("#t_backup_type"):
            page.select_option("#t_backup_type", "full")
        page.fill("#t_host", "192.168.220.129")
        page.fill("#t_port", "1521")
        page.fill("#t_username", "system")
        page.fill("#t_password", "oracle")
        page.fill("#t_db_name", "orcl11g")
        # 全量调度选星期一（如果存在）
        chk = page.query_selector("#t_full_days input[type=checkbox][value='0']")
        if chk: chk.check()
        print("[4] 表单填写完成")

        # 5) 点保存
        page.click("#taskSaveBtn")
        time.sleep(3)  # 等 toast / 网络
        print("[5] 已点保存")

        # 6) 抓 toast 与 console
        toasts = page.eval_on_selector_all(
            ".toast, [class*=toast], [class*=Toast]",
           "els => els.map(e => e.innerText).filter(Boolean)"
        )
        # 抓所有可见的 alert / danger 文案
        alerts = page.eval_on_selector_all(
            "[class*=alert], [class*=danger]",
            "els => els.map(e => e.innerText).filter(t => t && t.trim()).slice(-5)"
        )
        print(f"\n=== TOASTS ===\n{toasts}")
        print(f"\n=== ALERTS ===\n{alerts}")
        print(f"\n=== CONSOLE ERRORS ({len(console_errors)}) ===")
        for e in console_errors[-15:]:
            print(" ", e)
        print(f"\n=== POST /api 响应 ===")
        for m, u, s in api_responses[-10:]:
            print(f"  {m} {u} -> {s}")

        # 7) 通过 API 确认任务是否真创建了
        import requests
        s = requests.Session()
        s.post(BASE+"/login", data={"username":"admin","password":"admin123"}, timeout=10)
        ts = s.get(BASE+"/api/tasks", timeout=10).json()
        names = [t.get("name","") for t in ts]
        created = [n for n in names if "playwright测试" in n]
        print(f"\n=== 通过 API 查到的 playwright 任务 ===\n  {created}")

        # 截图
        page.screenshot(path="tests/playwright_save_test.png", full_page=True)
        print("\n[截图] tests/playwright_save_test.png")

        # 清理
        for t in ts:
            if "playwright测试" in t.get("name",""):
                s.delete(f"{BASE}/api/tasks/{t['id']}", timeout=10)
                print(f"[清理] 删除 id={t['id']}")

        browser.close()

        # 判定
        has_save_failed = any("保存失败" in t for t in toasts) or any("保存失败" in a for a in alerts)
        has_not_iterable = any("not iterable" in t for t in toasts) or any("not iterable" in a for a in alerts)
        if created and not has_save_failed and not has_not_iterable:
            print("\n✅ PASS: 任务真实创建，无保存失败 toast")
            sys.exit(0)
        elif created and has_save_failed:
            print(f"\n⚠️ PARTIAL: 任务创建了（{created}），但前端仍弹保存失败 toast — 浏览器可能用了缓存旧 JS")
            sys.exit(1)
        else:
            print("\n❌ FAIL: 任务未创建 或 toast 显示错误")
            sys.exit(2)

if __name__ == "__main__":
    main()