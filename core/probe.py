# -*- coding: utf-8 -*-
"""
数据库连通性探测：在巡检 / 数据同步前，对源/目标库做一次轻量级连通性检查。

零额外依赖：优先用各库 Python 驱动探测（与迁移/同步引擎同源，Docker 镜像
零安装原则下无需命令行客户端），CLI 仅作回退。
- 返回 (True, msg)  表示连接正常
- 返回 (False, msg) 表示连接失败（应触发告警）
- 返回 (None, msg)  表示无法判定（驱动/客户端均缺失 / 类型未实现探测），视为“未知/警告”
"""
import importlib
import os
import shutil
import subprocess

import core.db as db


def _run(cmd: list, env: dict = None, timeout: int = 12, stdin_text: str = ""):
    try:
        proc = subprocess.run(
            cmd, env=env, input=(stdin_text.encode("utf-8") if stdin_text else None),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout)
        return (proc.returncode,
                proc.stdout.decode("utf-8", "ignore"),
                proc.stderr.decode("utf-8", "ignore"))
    except subprocess.TimeoutExpired:
        return -1, "", "命令执行超时"
    except FileNotFoundError as e:
        return -2, "", f"命令不存在: {e}"


def _try_import(*names):
    """按序惰性导入首个可用模块（T06：信创驱动全部为可选依赖）。

    Args:
        *names: 候选模块名，按优先级排列。

    Returns:
        ``(module | None, 原因)``。**绝不抛异常**。
    """
    for name in names:
        try:
            return importlib.import_module(name), ""
        except ImportError:
            continue
        except Exception as e:  # pragma: no cover - 驱动内部异常
            return None, f"{name} 导入失败: {e}"
    return None, "未安装 " + " / ".join(names)


def _first_client(*names) -> str:
    """返回 PATH 中首个存在的命令行客户端名；都不存在返回空串。"""
    for name in names:
        if shutil.which(name):
            return name
    return ""


# 错误文本收敛参数：驱动（oracledb/psycopg2 等）的报错常常分多行，
# 关键定位信息（at host / port / ORA-xxxxx）往往在尾部。早期实现用 [:160]
# 硬截断，会把 "at host "192.168.204.20" port 1521" 切在 IP 中间，用户无法判读
# （现场反馈：任务表单「测试连接」提示显示不全）。改为保留完整文本，仅在
# 极端超长时保留 头 + 尾，中间给出省略标记。
_ERR_MAX = 1200
_ERR_HEAD = 780
_ERR_TAIL = 360


def _clip(text, limit: int = _ERR_MAX) -> str:
    """把驱动/CLI 的错误文本收敛成可读文本，保留尾部关键信息。"""
    s = str(text or "").strip()
    if not s:
        return ""
    if len(s) <= limit:
        return s
    tail = _ERR_TAIL if limit >= _ERR_TAIL * 2 else limit // 3
    head = limit - tail
    omitted = len(s) - head - tail
    return (s[:head].rstrip() + "\n…（中间省略 %d 个字符）…\n" % omitted
            + s[-tail:].lstrip())


def _probe_via_driver(module, connect, probe_sql, ok_msg: str):
    """用 Python 驱动执行一次轻量查询验证连通性。

    Args:
        module: 已导入的驱动模块（仅用于判空）。
        connect: 无参可调用对象，返回连接。
        probe_sql: 探活语句；可传候选序列（如 DM7 的 SELECT 1 / FROM DUAL），
                   逐条尝试、任一成功即通过，全部失败才保留最后一条真实错误。
        ok_msg: 成功时的中文提示。

    Returns:
        ``(True/False, 消息)``；驱动缺失时返回 ``(None, "")`` 交由调用方回退 CLI。
    """
    if module is None:
        return None, ""
    try:
        conn = connect()
    except Exception as e:
        return False, f"连接失败: {_clip(e)}"
    sqls = (probe_sql,) if isinstance(probe_sql, str) else tuple(probe_sql)
    try:
        cur = conn.cursor()
        last_err = None
        try:
            for sql in sqls:
                try:
                    cur.execute(sql)
                    cur.fetchall()
                    return True, ok_msg
                except Exception as e:
                    last_err = e
            return False, f"探活查询失败: {_clip(last_err)}"
        finally:
            try:
                cur.close()
            except Exception:
                pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _probe_mysql(h, p, u, pw, db_name, t):
    """MySQL/MariaDB 连通性：pymysql 驱动优先，回退 mysql CLI。

    Docker 镜像按零安装原则只烘焙 Python 驱动、不含 mysql 客户端，
    故驱动优先（与 T06 oracle/kingbase/达梦 一致），CLI 仅作回退。
    """
    module, reason = _try_import("pymysql")
    if module is not None:
        def _connect():
            return module.connect(host=h, port=int(p or 3306), user=u,
                                  password=pw, database=db_name or None,
                                  connect_timeout=max(3, int(t)),
                                  charset="utf8mb4")
        ok, msg = _probe_via_driver(module, _connect, "SELECT 1",
                                    "MySQL 连接正常（pymysql）")
        if ok is not None:
            return ok, msg
    env = os.environ.copy()
    if pw:
        env["MYSQL_PWD"] = pw
    # --no-defaults 屏蔽 /root/.my.cnf 等残留 [client] password——
    # 配置文件优先级高于 MYSQL_PWD，会导致用错密码连接（Access denied）
    rc, out, err = _run(
        ["mysql", "--no-defaults", "-h", h, "-P", str(p or 3306), "-u", u,
         "--connect-timeout", str(t), "-e", "SELECT 1"], env, t + 5)
    return (rc == 0, "MySQL 连接正常" if rc == 0 else (err.strip() or "连接失败"))


def _probe_postgresql(h, p, u, pw, db_name, t):
    """PostgreSQL 连通性：psycopg2 驱动优先，回退 psql/pg_isready。

    Docker 镜像无 psql/pg_isready 客户端，驱动优先保证容器内可探测。
    """
    module, reason = _try_import("psycopg2")
    if module is not None:
        ok, msg = _probe_via_driver(
            module,
            lambda: module.connect(host=h, port=int(p or 5432), user=u,
                                   password=pw, dbname=db_name or "postgres",
                                   connect_timeout=max(3, int(t))),
            "SELECT 1", "PostgreSQL 连接正常（psycopg2）")
        if ok is not None:
            return ok, msg
    env = os.environ.copy()
    if pw:
        env["PGPASSWORD"] = pw
    rc, out, err = _run(
        ["pg_isready", "-h", h, "-p", str(p or 5432), "-U", u, "-t", str(t)],
        env, t + 5)
    # 0=接受连接 1=拒绝 2=无响应 3=未尝试
    if rc == 0:
        return True, "PostgreSQL 可接受连接"
    return False, (out + err).strip() or "连接被拒绝"


def _probe_redis(h, p, u, pw, db_name, t):
    """Redis 连通性：redis 模块驱动优先，回退 redis-cli（零安装原则）。"""
    module, reason = _try_import("redis")
    if module is not None:
        try:
            client = module.Redis(
                host=h, port=int(p or 6379), password=(pw or None),
                db=int(db_name or 0), socket_connect_timeout=max(3, int(t)),
                socket_timeout=max(3, int(t)))
            try:
                ok = bool(client.ping())
                return ok, ("Redis 连接正常（redis 驱动）" if ok
                            else "Redis PING 无响应")
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        except Exception as e:
            return False, f"连接失败: {_clip(e)}"
    cmd = ["redis-cli", "-h", h, "-p", str(p or 6379)]
    if pw:
        cmd += ["-a", pw]
    cmd += ["PING"]
    rc, out, err = _run(cmd, None, t + 5)
    ok = rc == 0 and "PONG" in out.upper()
    return ok, (out.strip() or err.strip() or "连接失败")


def _probe_mongodb(h, p, u, pw, db_name, t):
    """MongoDB 连通性：pymongo 驱动优先，回退 mongosh/mongo CLI。"""
    module, reason = _try_import("pymongo")
    if module is not None:
        try:
            from urllib.parse import quote_plus
            cred = f"{quote_plus(u)}:{quote_plus(pw or '')}@" if u else ""
            uri = f"mongodb://{cred}{h}:{p or 27017}/{db_name or ''}"
            client = module.MongoClient(
                uri, serverSelectionTimeoutMS=max(3000, int(t) * 1000),
                connectTimeoutMS=max(3000, int(t) * 1000))
            try:
                client.admin.command("ping")
                return True, "MongoDB 连接正常（pymongo）"
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        except Exception as e:
            return False, f"连接失败: {_clip(e)}"
    uri = (f"mongodb://{u}:{pw}@{h}:{p}/{db_name or ''}" if pw
           else f"mongodb://{h}:{p}/{db_name or ''}")
    rc, out, err = _run(
        ["mongosh", "--quiet", "--eval", "db.runCommand({ping:1})", uri],
        None, t + 10)
    if rc != 0 and shutil.which("mongo"):
        rc, out, err = _run(
            ["mongo", "--quiet", "--eval", "db.runCommand({ping:1})", uri],
            None, t + 10)
    ok = rc == 0 and "ok" in out.lower()
    return ok, (out.strip() or err.strip() or "连接失败")


def _probe_oracle(h, p, u, pw, db_name, t):
    """Oracle 连通性（T06）：驱动 → JDBC → sqlplus 三层通道，兼顾 11g/12c+。

    python-oracledb 的 **thin 模式不支持 Oracle 11.2 及更早版本**（DPY-3010），
    因此 11g 现场若只走驱动通道，会被误判成「连接失败/平台不支持」。
    正确语义：thin 被版本挡住属于「通道不适用」，此时自动切 JDBC（ojdbc +
    JRE 随平台离线包内置，零客户端安装）再回退 sqlplus（数据库自带）。

    驱动与各通道都缺失时返回 ``(None, ...)``——「未知」而非「失败」，不误报警。
    """
    service = db_name or "ORCL"
    dsn = f"{h}:{p or 1521}/{service}"
    module, reason = _try_import("oracledb", "cx_Oracle")
    thin_blocked = ""
    if module is not None:
        ok, msg = _probe_via_driver(
            module,
            lambda: module.connect(user=u, password=pw, dsn=dsn),
            "SELECT 1 FROM DUAL",
            f"Oracle 连接正常（{module.__name__}）")
        if ok is True:
            return True, msg
        if ok is False:
            from core import native_conn
            if not native_conn.thin_version_blocked(msg):
                # 真实失败（密码错 ORA-01017 / 服务名错 ORA-12514 等），
                # 这条报错才是用户要看的真原因，直接返回
                return False, msg
            thin_blocked = msg

    # 兜底 1：JDBC（11g 的唯一直连通道；平台离线包内置 ojdbc + JRE）
    jok, jmsg = _probe_via_jdbc("oracle", h, p, u, pw, t, service)
    if jok:
        note = "（11g 不支持 thin 模式，已自动改走 JDBC）" if thin_blocked else ""
        return True, f"Oracle 连接正常：JDBC 通道{note}"
    jdbc_err = jmsg

    client = _first_client("sqlplus")
    if not client:
        if "ORA-" in (jdbc_err or "").upper():
            # JDBC 已与 Oracle 实例交互（ORA-01017 密码错 / ORA-12514 服务名错 /
            # ORA-28000 锁定等）→ 这是明确的连接失败，不能报「无法确认」
            return False, _clip(jdbc_err)
        if thin_blocked:
            return None, (
                "Oracle 11g（11.2 及更早）不支持 thin 模式，且平台侧 JDBC / "
                f"sqlplus 通道均不可用（{jdbc_err}），无法从平台侧验证连接；"
                "备份/恢复走 SSH + 数据库自带 expdp/impdp/rman，不受此影响")
        return None, (f"缺少 oracledb/cx_Oracle 驱动与 sqlplus 客户端（{reason}），"
                      f"JDBC 通道亦不可用（{jdbc_err}），无法验证连接")
    # 密码经 stdin 传入，绝不出现在命令行（共享知识 #16）
    script = f"connect {u}/{pw}@{dsn}\nselect 1 from dual;\nexit\n"
    rc, out, err = _run([client, "-S", "-L", "/nolog"], None, t + 10, script)
    text = (out + err)
    if rc == 0 and "ORA-" not in text.upper():
        note = "（11g 不支持 thin 模式，已回退 sqlplus）" if thin_blocked else ""
        return True, f"Oracle 连接正常：sqlplus 通道{note}"
    return False, (_clip(text) or f"连接失败（JDBC: {_clip(jdbc_err)}）")


def _probe_kingbase(h, p, u, pw, db_name, t):
    """KingbaseES 连通性（T06）：ksycopg2 / psycopg2 驱动优先，回退 ksql。"""
    module, reason = _try_import("ksycopg2", "psycopg2")
    if module is not None:
        ok, msg = _probe_via_driver(
            module,
            lambda: module.connect(host=h, port=p or 54321, user=u,
                                   password=pw, dbname=db_name or "test",
                                   connect_timeout=max(3, int(t))),
            "SELECT 1",
            f"KingbaseES 连接正常（{module.__name__}）")
        if ok is not None:
            return ok, msg

    client = _first_client("ksql", "sys_psql", "psql")
    if not client:
        # 零安装原则：平台自带 JDBC 驱动（金仓兜底 PG 驱动），缺 CLI 时兜底
        return _probe_via_jdbc("kingbase", h, p, u, pw, t, db_name)
    env = os.environ.copy()
    if pw:
        env["KINGBASE_PASSWORD"] = pw
        env["PGPASSWORD"] = pw
    rc, out, err = _run(
        [client, "-h", h, "-p", str(p or 54321), "-U", u,
         "-d", db_name or "test", "-tAc", "SELECT 1"], env, t + 5)
    if rc == 0 and "1" in (out or ""):
        return True, f"KingbaseES 连接正常（{client}）"
    return False, (_clip(out + err) or "连接失败")


def _probe_panwei(h, p, u, pw, db_name, t):
    """磐维 PanWeiDB 连通性：PG 协议兼容，psycopg2 驱动优先，回退 gsql。"""
    module, reason = _try_import("psycopg2")
    if module is not None:
        ok, msg = _probe_via_driver(
            module,
            lambda: module.connect(host=h, port=int(p or 12800), user=u,
                                   password=pw, dbname=db_name or "postgres",
                                   connect_timeout=max(3, int(t))),
            "SELECT 1", "磐维 PanWeiDB 连接正常（psycopg2）")
        if ok is not None:
            return ok, msg

    client = _first_client("gsql", "psql")
    if not client:
        return False, (reason or "未找到 psycopg2 驱动，且服务器上无 gsql 客户端")
    env = os.environ.copy()
    if pw:
        env["PGPASSWORD"] = pw
    rc, out, err = _run(
        [client, "-h", h, "-p", str(p or 12800), "-U", u,
         "-d", db_name or "postgres", "-tAc", "SELECT 1"], env, t + 5)
    if rc == 0 and "1" in (out or ""):
        return True, f"磐维 PanWeiDB 连接正常（{client}）"
    return False, (_clip(out + err) or "连接失败")


def _probe_dameng(h, p, u, pw, db_name, t):
    """达梦 连通性（T06）：dmPython 驱动优先，回退 disql。"""
    port = p or 5236
    module, reason = _try_import("dmPython", "dmpython")
    if module is not None:
        def _connect():
            try:
                return module.connect(user=u, password=pw, server=str(h),
                                      port=int(port))
            except TypeError:
                return module.connect(u, pw, f"{h}:{port}")
        ok, msg = _probe_via_driver(
            module, _connect, ("SELECT 1", "SELECT 1 FROM DUAL"),
            f"达梦 连接正常（{module.__name__}）")
        if ok is not None:
            return ok, msg

    client = _first_client("disql")
    if not client:
        # 零安装原则：平台自带 JDBC 驱动，缺原生驱动/CLI 时自动兜底
        return _probe_via_jdbc("dameng", h, p, u, pw, t)
    # 密码经 stdin 传入，不进命令行；DM7 老实例对无 FROM 的 select 1 兼容不一
    last_text = ""
    for sql in ("select 1;", "select 1 from dual;"):
        script = f"conn {u}/\"{pw}\"@{h}:{port}\n{sql}\nexit\n"
        rc, out, err = _run([client, "/nolog"], None, t + 10, script)
        text = (out + err)
        if rc == 0 and "-" not in text.split("\n")[0][:1] and "错误" not in text:
            return True, "达梦 连接正常（disql）"
        last_text = text
    return False, (_clip(last_text) or "连接失败")


# 未实现客户端级探测的库：判定为“未知”，不误报警
def _probe_unimplemented(_h, _p, _u, _pw, _db, _t):
    return None, "该类型未实现客户端探测，跳过"


def _probe_neo4j(h, p, u, pw, db_name, t):
    """Neo4j 连通性：cypher-shell 优先，回退 HTTP 事务接口（/db/<name>/tx/commit）。

    cypher-shell 缺失时用 Neo4j 官方 HTTP API 做一次只读的 Cypher 调用，
    不落任何数据、不改任何状态；两条路都不可用时返回「未知」，不误报警。
    """
    port = p or 7687
    database = db_name or "neo4j"
    client = _first_client("cypher-shell")
    if client:
        env = os.environ.copy()
        env["NEO4J_USERNAME"] = u or "neo4j"
        env["NEO4J_PASSWORD"] = pw or ""
        rc, out, err = _run(
            [client, "-a", f"bolt://{h}:{port}", "-d", database,
             "--non-interactive", "RETURN 1 AS ok;"], env, t + 10)
        if rc == 0:
            return True, "Neo4j 连接正常（cypher-shell，bolt）"
        return False, (_clip(out + err) or "Neo4j 连接失败（cypher-shell）")

    # HTTP 通道（Neo4j 默认 7473/7474；bolt 端口 -1 的常见部署 = 7474）
    http_port = 7474
    try:
        if int(port) in (7474, 7473):
            http_port = int(port)
    except (TypeError, ValueError):
        pass
    try:
        import base64
        import json as _json
        import urllib.request as _req

        url = f"http://{h}:{http_port}/db/{database}/tx/commit"
        payload = _json.dumps({
            "statements": [{"statement": "RETURN 1 AS ok"}]
        }).encode("utf-8")
        req = _req.Request(url, data=payload,
                           headers={"Content-Type": "application/json"})
        if u or pw:
            token = base64.b64encode(f"{u or 'neo4j'}:{pw or ''}".encode()).decode()
            req.add_header("Authorization", "Basic " + token)
        with _req.urlopen(req, timeout=max(3, int(t))) as resp:
            body = _json.loads(resp.read().decode("utf-8", "ignore"))
        if body.get("errors"):
            return False, "Neo4j 返回错误: " + _clip(body["errors"][0].get("message", ""))
        return True, f"Neo4j 连接正常（HTTP API :{http_port}，db={database}）"
    except Exception as e:
        return None, f"缺少 cypher-shell 且 HTTP 探测失败（{_clip(e, 400)}），无法验证连接"


def _guess_object_provider(endpoint: str) -> tuple[str, str]:
    """按端点域名猜 provider 与 region（探活入口拿不到 extra_options 时的兜底）。"""
    host = (endpoint or "").lower()
    if "myqcloud" in host:                      # cos.ap-guangzhou.myqcloud.com
        seg = host.split(".")
        return "cos", (seg[1] if len(seg) > 2 and seg[0] == "cos" else "")
    if "aliyuncs" in host:                      # oss-cn-hangzhou.aliyuncs.com
        if host.startswith("oss-"):
            return "oss", "cn-" + host[len("oss-"):].split(".")[0].replace("cn-", "")
        return "oss", ""
    if "myhuaweicloud" in host:
        seg = host.split(".")
        return "obs", (seg[1] if len(seg) > 2 and seg[0] == "obs" else "")
    if "amazonaws" in host:
        seg = host.split(".")
        return "s3", (seg[1] if len(seg) > 3 and seg[0] == "s3" else "")
    return "minio", ""


def _probe_object_storage(h, p, u, pw, db_name, t):
    """对象存储连通性探测：真实发起一次列桶（签名/密钥/端点同时被验证）。"""
    from core.objectstore.providers import build_client, provider_meta
    provider, region = _guess_object_provider(h or "")
    meta = provider_meta(provider)
    try:
        cli = build_client({
            "provider": provider,
            "endpoint": h,
            "port": p,
            "access_key": u,
            "secret_key": pw,
            "region": region or meta.get("default_region") or "us-east-1",
            "verify_ssl": False,
            "retries": 0,
        })
        ok, msg = cli.test_connection()
        if not ok:
            return False, msg
        if db_name:
            exists = cli.bucket_exists(db_name)
            if not exists:
                return False, ("连接正常，但桶不存在: %s" % db_name)
            return True, "对象存储连接正常（%s，桶 %s 可访问）" % (provider, db_name)
        return True, "对象存储连接正常（%s）：%s" % (provider, msg)
    except Exception as e:
        return False, "对象存储连接失败: %s" % _clip(e)


# 值为 (必需客户端列表, 探测函数)。
# 客户端列表为空表示该探测函数自带「驱动优先 + CLI 回退 + 缺失即未知」逻辑（T06）。
# mysql/postgresql/redis/mongodb 的 CLI 为空列表：容器镜像零安装原则只含
# Python 驱动（pymysql/psycopg2/redis/pymongo），由探测函数内部驱动优先。
_PROBES = {
    "mysql": ([], _probe_mysql),
    # MariaDB 与 MySQL 协议兼容（pymysql 直连），复用同一探测实现
    "mariadb": ([], _probe_mysql),
    "postgresql": ([], _probe_postgresql),
    "redis": ([], _probe_redis),
    "mongodb": ([], _probe_mongodb),
    "oracle": ([], _probe_oracle),
    "kingbase": ([], _probe_kingbase),
    "panwei": ([], _probe_panwei),
    "dameng": ([], _probe_dameng),
    "neo4j": ([], _probe_neo4j),
    "object_storage": ([], _probe_object_storage),
}


def _probe_via_jdbc(db_type: str, h, p, u, pw, t, db_name: str = ""):
    """JDBC 兜底探测：驱动 jar 随平台离线包内置（零安装原则），
    dmPython/CLI 客户端缺失、Oracle 11g 不支持 thin 时的可靠通道。

    ``db_name`` 必须传真实库名/service：Oracle 的 URL 形如
    ``jdbc:oracle:thin:@//host:port/<service>``，service 为空会直接报
    ORA-12261（Easy Connect 语法错误）——历史实现恒传空串，导致 11g 走
    JDBC 兜底时必然失败。返回 (ok, msg)。
    """
    try:
        from core import jdbc
        conn = jdbc.connect(db_type, str(h), int(p or 0),
                            str(db_name or ""), str(u), str(pw or ""))
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM DUAL" if db_type in ("oracle", "dameng")
                        else "SELECT 1")
            cur.fetchone()
            cur.close()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return True, f"{db_type} 连接正常（JDBC）"
    except Exception as e:
        return False, f"JDBC 连接失败: {_clip(e)}"


def probe_db_connection(db_type: str, host: str, port, username: str,
                         password: str, db_name: str = "", timeout: int = 10):
    """返回 (ok: bool|None, message: str)。ok=None 表示未知。"""
    if not host:
        return False, "未配置主机地址"
    try:
        port = int(port or 0)
    except (TypeError, ValueError):
        port = 0
    entry = _PROBES.get(db_type)
    if not entry:
        return None, f"未知数据库类型: {db_type}"
    clients, fn = entry
    missing = [c for c in clients if not shutil.which(c)]
    if missing:
        return None, "客户端缺失(" + ",".join(missing) + ")，无法验证连接"
    try:
        return fn(host, port, username, password, db_name, timeout)
    except Exception as e:  # 探测本身异常，按未知处理，不误报失败
        return None, f"探测异常: {e}"
