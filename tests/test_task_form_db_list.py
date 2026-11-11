# -*- coding: utf-8 -*-
"""任务向导第 2 步「拉取库列表」回归：新建任务无需先保存。

覆盖：
1. POST /api/tasks/list-databases 用表单参数直接探测（无 task_id、不落库）
2. 编辑态 task_id + 密码留空 → 沿用已存密文凭据
3. 缺 db_type / host → 400 结构化提示
4. 系统库过滤、type / via_jdbc 与 GET 端点结构一致
5. 权限映射：新 POST 端点保持只读语义豁免
6. 前端契约：不再拦截「请先保存任务」、步骤条可前进、缓存戳升级
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="form_db_list_")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["AIDBM_TESTING"] = "1"
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")

import pytest  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BODY = {
    "db_type": "mysql",
    "host": "192.0.2.10",
    "port": "3306",
    "username": "bkp",
    "password": "secret",
    "db_name": "",
}


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _task_count():
    import core.db as db
    row = db.query_one("SELECT COUNT(*) AS c FROM backup_tasks")
    return row["c"] if row else 0


def _make_task():
    import core.db as db
    return db.execute(
        "INSERT INTO backup_tasks (name, db_type, host, port, username, password, "
        "db_name, backup_mode, created_at, updated_at) "
        "VALUES (?, 'mysql', '192.0.2.20', 3306, 'u', ?, 'srcdb', 'logical', ?, ?)",
        ("form-db-list-task", db.encrypt_secret("stored-pw"),
         db.now_iso(), db.now_iso()))


@pytest.fixture(scope="module")
def app():
    from app import create_app
    a = create_app()
    a.config.update(TESTING=True)
    return a


@pytest.fixture(scope="module")
def client(app):
    with app.test_client() as c:
        r = c.post("/login", data={"username": "admin", "password": "admin123"})
        with c.session_transaction() as _sess:
            if not _sess.get("user"):
                _sess["user"] = {"username": "admin", "role": "admin"}
        assert r.status_code in (200, 302)
        yield c


@pytest.fixture
def mock_fetch(monkeypatch):
    """不连真实数据库：替换 _fetch_db_list，记录后端构造的临时 task。"""
    import api.tasks as tasks_api
    seen = {}

    def fake(db_type, task):
        seen["db_type"] = db_type
        seen["task"] = dict(task)
        return ["appdb", "mysql", "information_schema"], False, None

    monkeypatch.setattr(tasks_api, "_fetch_db_list", fake)
    return seen


def test_new_task_can_list_databases_without_saving(client, mock_fetch):
    """核心回归：新建任务（t_id 为空）也能直接拉库列表，且不产生任务记录。"""
    import core.db as db
    before = _task_count()
    r = client.post("/api/tasks/list-databases", json=BODY)
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data["databases"] == ["appdb"]       # mysql / information_schema 被过滤
    assert data["type"] == "databases"
    assert data["via_jdbc"] is False
    task = mock_fetch["task"]
    assert task["id"] == 0
    assert task["host"] == "192.0.2.10"
    assert task["db_type"] == "mysql"
    # 引擎/JDBC 按密文读取，解密后必须还是表单填的明文
    assert db.decrypt_secret(task["password"]) == "secret"
    assert _task_count() == before, "探测接口不应落库建任务"


def test_edit_task_reuses_stored_password(client, mock_fetch):
    """编辑态密码留空时沿用库中凭据，不把空密码传给引擎。"""
    import core.db as db
    tid = _make_task()
    r = client.post("/api/tasks/list-databases",
                    json=dict(BODY, password="", task_id=tid))
    assert r.status_code == 200, r.get_data(as_text=True)
    task = mock_fetch["task"]
    assert task["id"] == tid
    assert db.decrypt_secret(task["password"]) == "stored-pw"


@pytest.mark.parametrize("body,missing", [
    ({}, "数据库类型"),
    ({"db_type": "mysql"}, "主机地址"),
])
def test_missing_required_fields(client, body, missing):
    r = client.post("/api/tasks/list-databases", json=body)
    assert r.status_code == 400
    assert missing in r.get_json()["error"]


def test_pg_type_and_system_db_filter(client, monkeypatch):
    import api.tasks as tasks_api

    monkeypatch.setattr(
        tasks_api, "_fetch_db_list",
        lambda db_type, task: (["bizdb", "template0", "template1"], True, None))
    r = client.post("/api/tasks/list-databases",
                    json=dict(BODY, db_type="postgresql"))
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data["databases"] == ["bizdb"]
    assert data["type"] == "schemas"
    assert data["via_jdbc"] is True


def test_get_endpoint_keeps_original_contract(client, mock_fetch):
    """抽取公共 helper 后，编辑态 GET 端点响应结构不退化。"""
    tid = _make_task()
    r = client.get(f"/api/tasks/{tid}/list-databases")
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data["databases"] == ["appdb"]
    assert data["type"] == "databases"
    r404 = client.get("/api/tasks/999999/list-databases")
    assert r404.status_code == 404


def test_perm_map_treats_new_endpoints_as_read_only():
    from api import perm_map
    for path in ("/api/tasks/list-databases", "/api/tasks/list-tables"):
        perm, reason = perm_map.resolve(path, "POST")
        assert perm is None and reason == "exempt"


def test_new_task_can_list_tables_without_saving(client, monkeypatch):
    """「选择表」同款回归：按表单参数探测，不落库、不要求 task_id。"""
    import api.tasks as tasks_api
    seen = {}

    def fake(task, database, schema):
        seen["task"] = dict(task)
        seen["db"] = database
        return {"tables": ["t1", "t2"], "db": database, "count": 2}, 200

    monkeypatch.setattr(tasks_api, "_tables_payload", fake)
    before = _task_count()
    r = client.post("/api/tasks/list-tables", json=dict(BODY, db="appdb"))
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["tables"] == ["t1", "t2"]
    assert seen["db"] == "appdb"
    assert seen["task"]["id"] == 0
    assert _task_count() == before, "表清单探测不应落库建任务"


def test_form_tables_missing_fields(client):
    r = client.post("/api/tasks/list-tables", json={})
    assert r.status_code == 400
    assert "数据库类型" in r.get_json()["error"]


def test_get_tables_endpoint_keeps_contract(client, monkeypatch):
    import api.tasks as tasks_api
    monkeypatch.setattr(
        tasks_api, "_tables_payload",
        lambda task, database, schema: ({"tables": [], "db": database, "count": 0}, 200))
    tid = _make_task()
    r = client.get(f"/api/tasks/{tid}/list-tables?db=srcdb")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["db"] == "srcdb"
    assert client.get("/api/tasks/999999/list-tables").status_code == 404


def test_frontend_contract():
    js = _src("static/js/app.js")
    assert "请先保存任务" not in js
    assert 'api("POST", "/api/tasks/list-databases", body)' in js
    assert 'fetch("/api/tasks/list-tables"' in js
    assert 'closest(".wiz-step")' in js and "taskWizValidate(1)" in js
    html = _src("templates/tasks.html")
    assert "无需先保存任务" in html and "第 4 步" in html
    assert "选择表" in html
    import re
    base = _src("templates/base.html")
    # 只要求「升过戳」而不是写死某个版本：以后每次改 JS/CSS 升戳不应打破回归
    m_js = re.search(r"app\.js\?v=(\d+)", base)
    m_css = re.search(r"app\.css\?v=(\d+)", base)
    assert m_js and int(m_js.group(1)) >= 2026111008, "app.js 缓存戳未升"
    assert m_css and int(m_css.group(1)) >= 2026111006, "app.css 缓存戳未升"
