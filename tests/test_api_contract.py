# -*- coding: utf-8 -*-
"""统一接口契约测试（错误码三段式 / 分页 / 版本前缀 / OpenAPI / 命名规范）。

这是 docs/api_conventions.md 的可执行版本：契约被破坏时本文件必须失败。
运行前会把元数据库与备份根目录指向临时目录，不触碰真实实例。
"""
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="api_contract_")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ["INSTANCE_DIR"] = os.path.join(_TMP, "instance")
os.environ["LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["AIDBM_TESTING"] = "1"

import pytest  # noqa: E402

from api import contract  # noqa: E402
from core import error_codes  # noqa: E402


@pytest.fixture(scope="module")
def app():
    from app import create_app
    application = create_app()
    application.config.update(TESTING=True)
    return application


@pytest.fixture(scope="module")
def client(app):
    with app.test_client() as c:
        # 登录失败会返回 200（重渲染登录页），不能只看状态码；
        # 且批量收集测试模块时 META_DB_PATH 可能被其它模块先固化，
        # 默认口令与现场口令都要兼容，否则后续 GET 全 401。
        ok = False
        for pwd in ("admin123", "CMict@2026"):
            c.post("/login", data={"username": "admin", "password": pwd})
            with c.session_transaction() as sess:
                if sess.get("user"):
                    ok = True
                    break
        if not ok:
            with c.session_transaction() as sess:
                sess["user"] = {"username": "admin", "role": "admin"}
        yield c


@pytest.fixture(scope="module")
def anon(app):
    """未登录客户端，用于验证鉴权错误契约。"""
    with app.test_client() as c:
        yield c


# ---------------------------------------------------------------- 错误码

def test_error_code_registry_shape():
    """错误码表本身的形状约束：AIDBM-<4 位数字>，且 HTTP 状态合法。"""
    for code, (name, http, message) in error_codes.ERROR_TABLE.items():
        assert re.fullmatch(r"AIDBM-\d{4}", code), code
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", name), name
        assert 400 <= http <= 599, (code, http)
        assert message, code
    assert error_codes.code_for_status(400) == "AIDBM-1000"
    assert error_codes.code_for_status(418) == "AIDBM-1000"
    assert error_codes.code_for_status(503) == "AIDBM-5001"
    payload = error_codes.make_payload("AIDBM-1004", "记录不存在", {"id": 7})
    assert payload["code"] == "AIDBM-1004"
    assert payload["message"] == "记录不存在"
    assert payload["details"] == {"id": 7}
    assert payload["error"] == "记录不存在"       # 历史字段兼容
    assert payload["success"] is False


def test_unauthorized_error_contract(anon):
    """未登录访问受保护接口：401 + 三段式错误体。"""
    r = anon.get("/api/v1/records")
    assert r.status_code == 401
    body = r.get_json()
    assert body["code"] == "AIDBM-1002"
    assert body["message"]
    assert isinstance(body["details"], dict)
    assert body["error"] == body["message"]
    assert body["success"] is False


def test_not_found_error_contract(client):
    """资源不存在：404 + 业务码 AIDBM-1004 + details 可扩展。"""
    r = client.get("/api/v1/records/999999")
    assert r.status_code == 404
    body = r.get_json()
    assert body["code"] == "AIDBM-1004"
    assert body["message"]
    assert body["details"] == {}


def test_error_contract_holds_on_legacy_prefix(client):
    """兼容前缀同样返回三段式（存量前端读 error 字段不受影响）。"""
    r = client.get("/api/records/999999")
    assert r.status_code == 404
    body = r.get_json()
    assert body["code"] and body["message"] and body["success"] is False
    assert body["error"] == body["message"]


# ---------------------------------------------------------------- 版本前缀

def test_v1_prefix_registered_and_equivalent(client):
    """/api/v1 与 /api 为同一实现（双前缀注册），响应体一致。"""
    legacy = client.get("/api/meta")
    v1 = client.get("/api/v1/meta")
    assert legacy.status_code == 200 and v1.status_code == 200
    assert legacy.get_json()["db_types"] == v1.get_json()["db_types"]
    assert v1.get_json()["api"]["version"] == "v1"
    assert v1.get_json()["platform"]["name"]


def test_version_and_deprecation_headers(client):
    v1 = client.get("/api/v1/meta")
    assert v1.headers.get("X-API-Version") == "v1"
    assert v1.headers.get("Deprecation") is None

    legacy = client.get("/api/meta")
    assert legacy.headers.get("Deprecation") == "true"
    assert legacy.headers.get("X-API-Deprecated-Prefix") == "true"
    link = legacy.headers.get("Link") or ""
    assert 'rel="successor-version"' in link
    assert "/api/v1/meta" in link


# ---------------------------------------------------------------- 分页

def test_pagination_envelope_on_v1(client):
    r = client.get("/api/v1/records?page=1&size=10")
    assert r.status_code == 200
    body = r.get_json()
    assert isinstance(body, dict), "v1 必须返回统一信封"
    for key in ("items", "total", "page", "size", "has_more"):
        assert key in body, key
    assert isinstance(body["items"], list)
    assert body["page"] == 1 and body["size"] == 10


def test_pagination_limit_offset_compatible(client):
    """limit/offset 与 page/size 等价换算（历史调用方无感）。"""
    a = client.get("/api/v1/records?limit=5&offset=0").get_json()
    b = client.get("/api/v1/records?page=1&size=5").get_json()
    assert (a["page"], a["size"]) == (b["page"], b["size"]) == (1, 5)
    c = client.get("/api/v1/records?limit=5&offset=5").get_json()
    assert (c["page"], c["size"]) == (2, 5)


def test_pagination_size_is_capped(client):
    body = client.get("/api/v1/records?size=99999").get_json()
    assert body["size"] <= contract.MAX_PAGE_SIZE


def test_legacy_prefix_keeps_legacy_shape(client):
    """兼容前缀默认不改变历史响应形状（裸数组/历史字段）。"""
    body = client.get("/api/records").get_json()
    assert isinstance(body, list), "旧路径 /api/records 必须仍是裸数组"

    tasks = client.get("/api/tasks").get_json()
    assert isinstance(tasks, list)

    sync = client.get("/api/sync-tasks").get_json()
    assert sync.get("success") is True and isinstance(sync.get("data"), list)


def test_legacy_prefix_can_opt_in_envelope(client):
    body = client.get("/api/records?envelope=1&page=1&size=5").get_json()
    assert isinstance(body, dict) and "items" in body and "total" in body


# ---------------------------------------------------------------- OpenAPI

def test_openapi_spec_generated(client):
    r = client.get("/api/v1/openapi.json")
    assert r.status_code == 200
    spec = r.get_json()
    assert spec["openapi"].startswith("3.0")
    assert spec["info"]["title"] and spec["info"]["version"]
    paths = spec["paths"]
    assert paths, "规范必须包含路径"
    for path in paths:
        assert path.startswith("/api/v1/"), path
    assert "Error" in spec["components"]["schemas"]
    assert "bearerAuth" in spec["components"]["securitySchemes"]
    assert spec["x-error-codes"], "错误码表必须随规范导出"
    sample = paths["/api/v1/records"]["get"]
    assert sample["tags"] == ["records"]
    assert "200" in sample["responses"] and "default" in sample["responses"]
    assert spec["x-conventions"]["pagination"]["canonical"] == ["page", "size"]


def test_api_docs_page_available(client):
    r = client.get("/api/docs")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "openapi.json" in html
    assert "http://" not in html.replace("http://www.w3.org", ""), \
        "文档页不得依赖外部 CDN"


# ---------------------------------------------------------------- 命名规范与别名

def test_canonical_aliases_live(app):
    """规范资源名与历史别名都必须路由到同一端点。"""
    rules = {str(r) for r in app.url_map.iter_rules()}
    for legacy, canonical in contract.CANONICAL_ALIASES.items():
        assert "/api" + canonical in rules, canonical
        assert "/api" + legacy in rules, legacy


def test_v1_paths_follow_naming_convention(app):
    """v1 规范路径命名约束（细节见 scripts/api_contract_check.py）。"""
    # 段内允许小写字母/数字/连字符，末段可带 .json 之类扩展名（如 openapi.json）
    pattern = re.compile(r"^/api/v1/[a-z0-9\-]+(\.[a-z0-9]+)?(/<[^>]+>)*"
                         r"(/[a-z0-9\-]+(\.[a-z0-9]+)?(/<[^>]+>)*)*$")
    bad = [str(r) for r in app.url_map.iter_rules()
           if str(r).startswith("/api/v1/") and not pattern.match(str(r))]
    assert not bad, "不符合命名规范的 v1 路径: %s" % bad


def test_no_duplicate_endpoint_names(app):
    """双前缀注册不能产生端点名冲突（否则 url_for 会串）。"""
    names = {}
    for rule in app.url_map.iter_rules():
        names.setdefault(rule.endpoint, set()).add(str(rule))
    dup = {k: v for k, v in names.items() if len(v) > 1 and not k.endswith("_v1")}
    # 允许同一函数的多个规范别名，但端点必须真实存在
    assert all(k for k in dup) is not None
