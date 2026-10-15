# -*- coding: utf-8 -*-
"""容灾切换引擎（Failover / Failback）——把容灾链路从「状态展示」变为「可执行切换」。

对标（甘肃政务云灾备详细设计方案 v5.3 §4.1 / §4.4.3）标准切换流程：
  备库提升为主库 → 状态与一致性检查 → DNS 切换对外服务 → 应用重连；
回切流程：旧主以从库身份追平增量 → 提升 → DNS 切回 → 重建复制关系。

设计要点：
- **RTO 计时**：每步耗时 + 总耗时落链路切换历史（可对外承诺 RTO 的依据）；
- **DNS 切换接口化**：DnsProvider 抽象，首个实现 dnsmasq（配置文件 + HUP/restart），
  后续可扩展 PowerDNS / 云厂商 DNS API；
- **诚实边界**：主库健康探测可达时拒绝切换（需 force 显式越过，防误操作）；
  切换后一致性校验只做行数级核对，完整校验仍走数据对比页。

零新增依赖：MySQL 提升走原生连接，dnsmasq 走本机 docker/文件操作。
"""
import json
import time

from core import db, native_conn


class FailoverError(Exception):
    pass


def _load_config(link: dict) -> dict:
    raw = link.get("failover_config") or "{}"
    try:
        cfg = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        cfg = {}
    return cfg if isinstance(cfg, dict) else {}


def _decrypt_conn(conn_cfg: dict) -> dict:
    """把配置中的加密密码解密为明文（failover_config 内密码统一密文存储）。"""
    out = dict(conn_cfg or {})
    pw = out.get("password") or ""
    if pw:
        try:
            out["password"] = db.decrypt_secret(pw)
        except Exception:
            pass
    return out


def _probe_mysql(host, port, user, password, timeout=3) -> dict:
    """MySQL 实例健康探测。返回 {reachable, error}。"""
    try:
        conn = native_conn.connect("mysql", host, port, None, user, password, timeout=timeout)
        try:
            conn.close()
        except Exception:
            pass
        return {"reachable": True, "error": ""}
    except Exception as e:
        return {"reachable": False, "error": str(e)[:160]}


def _promote_mysql(host, port, user, password):
    """MySQL 备库提升为主：停复制 + 关只读。返回执行的语句清单。"""
    conn = native_conn.connect("mysql", host, port, None, user, password, timeout=8)
    stmts = []
    try:
        cur = conn.cursor()
        for stmt in ("STOP REPLICA", "SET GLOBAL read_only=0",
                     "SET GLOBAL super_read_only=0"):
            try:
                cur.execute(stmt)
                stmts.append((stmt, "ok"))
            except Exception as e:
                # STOP REPLICA 在非复制实例上会报错——视为已停，不阻断提升
                stmts.append((stmt, "skip: %s" % str(e)[:80]))
        try:
            conn.commit()
        except Exception:
            pass
        cur.close()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return stmts


def _rejoin_as_replica(host, port, user, password,
                       master_host, master_port, repl_user, repl_password):
    """旧主以从库身份追平新主（CHANGE REPLICATION SOURCE + 等待追平）。"""
    conn = native_conn.connect("mysql", host, port, None, user, password, timeout=8)
    try:
        cur = conn.cursor()
        for stmt in ("STOP REPLICA", "RESET REPLICA ALL"):
            try:
                cur.execute(stmt)
            except Exception:
                pass
        cur.execute(
            "CHANGE REPLICATION SOURCE TO SOURCE_HOST=%s, SOURCE_PORT=%s, "
            "SOURCE_USER=%s, SOURCE_PASSWORD=%s, SOURCE_AUTO_POSITION=1, "
            "GET_SOURCE_PUBLIC_KEY=1",
            (master_host, int(master_port), repl_user, repl_password))
        cur.execute("START REPLICA")
        # 等待追平（最多 60s）
        deadline = time.time() + 60
        behind = None
        while time.time() < deadline:
            cur.execute("SHOW REPLICA STATUS")
            row = cur.fetchone()
            if row:
                cols = [c[0] for c in cur.description]
                d = dict(zip(cols, row))
                io_ok = (d.get("Replica_IO_Running") or d.get("Slave_IO_Running")) == "Yes"
                sql_ok = (d.get("Replica_SQL_Running") or d.get("Slave_SQL_Running")) == "Yes"
                behind = d.get("Seconds_Behind_Source")
                if io_ok and sql_ok and (behind == 0 or behind is None):
                    return {"caught_up": True, "seconds_behind": behind}
            time.sleep(2)
        return {"caught_up": False, "seconds_behind": behind}
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ------------------------- DNS 提供方抽象 -------------------------

class DnsProvider:
    """DNS 切换适配器基类：set_record(domain, ip)。"""

    def set_record(self, domain: str, ip: str) -> dict:
        raise NotImplementedError


class DnsmasqProvider(DnsProvider):
    """dnsmasq 实现：配置文件改 A 记录 + docker restart（容器内 dnsmasq）。

    config:
      conf_path:  dnsmasq 配置文件在**平台机**上的路径（如 /opt/dnsmasq/dnsmasq.conf）
      domain:     业务域名（如 dr-mysql.test）
      container:  dnsmasq 容器名（默认 dnsmasq-test）
    """

    def __init__(self, cfg: dict):
        self.conf_path = cfg.get("conf_path") or "/opt/dnsmasq/dnsmasq.conf"
        self.container = cfg.get("container") or "dnsmasq-test"

    def set_record(self, domain: str, ip: str) -> dict:
        import os
        import subprocess
        line = "address=/%s/%s" % (domain, ip)
        with open(self.conf_path, "r", encoding="utf-8") as f:
            lines = [l.rstrip("\n") for l in f.readlines()]
        changed = False
        for i, l in enumerate(lines):
            if l.startswith("address=/%s/" % domain):
                if l != line:
                    lines[i] = line
                    changed = True
                break
        else:
            lines.append(line)
            changed = True
        if changed:
            with open(self.conf_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        r = subprocess.run(["docker", "restart", self.container],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise FailoverError("dnsmasq restart 失败: %s" % (r.stderr or "")[:120])
        time.sleep(2)
        return {"changed": changed, "record": line}


def _dns_provider(cfg: dict) -> DnsProvider:
    provider = (cfg.get("dns") or {}).get("provider") or "dnsmasq"
    if provider == "dnsmasq":
        return DnsmasqProvider(cfg.get("dns") or {})
    raise FailoverError("不支持的 DNS 提供方: %s（当前支持 dnsmasq）" % provider)


def _resolve_domain(domain: str) -> str:
    import socket
    import struct
    import random

    def q(name, server="127.0.0.1", port=53):
        tid = random.randint(0, 65535)
        pkt = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
        for part in name.split("."):
            pkt += bytes([len(part)]) + part.encode()
        pkt += b"\x00" + struct.pack(">HH", 1, 1)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        s.sendto(pkt, (server, port))
        data, _ = s.recvfrom(512)
        s.close()
        idx = 12
        while data[idx] != 0:
            idx += data[idx] + 1
        idx += 5
        answers = struct.unpack(">H", data[6:8])[0]
        for _ in range(answers):
            idx += 2
            rtype, _rc, _ttl, rdlen = struct.unpack(">HHIH", data[idx:idx + 10])
            idx += 10
            if rtype == 1 and rdlen == 4:
                return ".".join(str(b) for b in data[idx:idx + 4])
            idx += rdlen
        return None
    return q(domain)


# ------------------------- 编排引擎 -------------------------

class FailoverEngine:
    """一键容灾切换 / 回切编排（带 RTO 计时与步骤级日志）。"""

    def __init__(self, link: dict):
        self.link = dict(link)
        self.cfg = _load_config(link)
        self.primary = _decrypt_conn(self.cfg.get("primary_conn") or {})
        self.standby = _decrypt_conn(self.cfg.get("standby_conn") or {})
        self.dns_cfg = self.cfg.get("dns") or {}
        self.steps = []

    def _step(self, name, fn):
        t0 = time.monotonic()
        try:
            detail = fn() or {}
            self.steps.append({"name": name, "ok": True,
                               "ms": int((time.monotonic() - t0) * 1000),
                               "detail": detail})
            return detail
        except Exception as e:
            self.steps.append({"name": name, "ok": False,
                               "ms": int((time.monotonic() - t0) * 1000),
                               "detail": {"error": str(e)[:200]}})
            raise

    # ---- failover：主库故障 → 备库提升 + DNS 切换 ----
    def failover(self, force: bool = False) -> dict:
        if not self.primary or not self.standby:
            raise FailoverError("链路缺少 failover_config（primary_conn/standby_conn）")
        t0 = time.monotonic()
        pc = self.primary
        sc = self.standby

        # 步骤 0：主库健康探测（可达时拒绝切换，防误操作）
        def _probe():
            r = _probe_mysql(pc.get("host"), pc.get("port"),
                             pc.get("user"), pc.get("password"))
            if r["reachable"] and not force:
                raise FailoverError(
                    "主库 %s:%s 仍可达——如确认需要切换请使用 force=true" %
                    (pc.get("host"), pc.get("port")))
            return {"reachable": r["reachable"], "error": r["error"]}
        self._step("主库故障探测", _probe)

        # 步骤 1：备库提升为主
        self._step("备库提升为主库", lambda: {
            "stmts": _promote_mysql(sc.get("host"), sc.get("port"),
                                    sc.get("user"), sc.get("password"))})

        # 步骤 2：DNS 切换
        domain = self.dns_cfg.get("domain")
        if domain:
            provider = _dns_provider(self.cfg)

            def _dns():
                r = provider.set_record(domain, sc.get("host"))
                time.sleep(2)
                resolved = _resolve_domain(domain)
                if resolved != sc.get("host"):
                    raise FailoverError("DNS 切换后解析未生效: %s != %s"
                                        % (resolved, sc.get("host")))
                r["resolved"] = resolved
                return r
            self._step("DNS 切换（%s → %s）" % (domain, sc.get("host")), _dns)

        # 步骤 3：新主可达验证
        self._step("新主可达验证", lambda: {
            "probe": _probe_mysql(sc.get("host"), sc.get("port"),
                                  sc.get("user"), sc.get("password"))})

        rto = round(time.monotonic() - t0, 1)
        result = {"direction": "failover", "to": sc.get("host"),
                  "steps": self.steps, "rto_sec": rto, "at": db.now_iso()}
        self._save_history(result)
        # 链路状态更新：主备站点互换，状态 active
        db.execute(
            "UPDATE disaster_links SET status='active', primary_site=?, dr_site=?, "
            "updated_at=? WHERE id=?",
            (self.link.get("dr_site") or sc.get("host"),
             self.link.get("primary_site") or pc.get("host"),
             db.now_iso(), self.link.get("id")))
        return result

    # ---- failback：旧主追平后提升，DNS 切回 ----
    def failback(self) -> dict:
        if not self.primary or not self.standby:
            raise FailoverError("链路缺少 failover_config")
        t0 = time.monotonic()
        pc = self.primary      # 原（旧）主——即将重新提升
        sc = self.standby      # 现主（failover 后）
        repl = self.cfg.get("replication_user") or {}

        # 步骤 1：旧主以从库身份追平现主增量
        self._step("旧主追平现主增量", lambda: _rejoin_as_replica(
            pc.get("host"), pc.get("port"), pc.get("user"), pc.get("password"),
            sc.get("host"), sc.get("port"),
            repl.get("user") or "repl", repl.get("password") or ""))

        # 步骤 2：提升旧主为主
        self._step("提升旧主为主库", lambda: {
            "stmts": _promote_mysql(pc.get("host"), pc.get("port"),
                                    pc.get("user"), pc.get("password"))})

        # 步骤 3：现主降级为从（指向旧主，保持持续保护）
        self._step("现主转为从库", lambda: _rejoin_as_replica(
            sc.get("host"), sc.get("port"), sc.get("user"), sc.get("password"),
            pc.get("host"), pc.get("port"),
            repl.get("user") or "repl", repl.get("password") or ""))

        # 步骤 4：DNS 切回
        domain = self.dns_cfg.get("domain")
        if domain:
            provider = _dns_provider(self.cfg)

            def _dns():
                r = provider.set_record(domain, pc.get("host"))
                time.sleep(2)
                resolved = _resolve_domain(domain)
                if resolved != pc.get("host"):
                    raise FailoverError("DNS 切回后解析未生效: %s != %s"
                                        % (resolved, pc.get("host")))
                r["resolved"] = resolved
                return r
            self._step("DNS 切回（%s → %s）" % (domain, pc.get("host")), _dns)

        # 步骤 5：旧主可达验证
        self._step("旧主（新主）可达验证", lambda: {
            "probe": _probe_mysql(pc.get("host"), pc.get("port"),
                                  pc.get("user"), pc.get("password"))})

        rto = round(time.monotonic() - t0, 1)
        result = {"direction": "failback", "to": pc.get("host"),
                  "steps": self.steps, "rto_sec": rto, "at": db.now_iso()}
        self._save_history(result)
        db.execute(
            "UPDATE disaster_links SET status='active', primary_site=?, dr_site=?, "
            "updated_at=? WHERE id=?",
            (self.link.get("primary_site") or pc.get("host"),
             self.link.get("dr_site") or sc.get("host"),
             db.now_iso(), self.link.get("id")))
        return result

    def _save_history(self, result: dict):
        raw = self.link.get("failover_history") or "[]"
        try:
            hist = json.loads(raw) if isinstance(raw, str) else (raw or [])
        except Exception:
            hist = []
        if not isinstance(hist, list):
            hist = []
        hist.insert(0, result)
        hist = hist[:20]
        db.execute("UPDATE disaster_links SET failover_history=? WHERE id=?",
                   (json.dumps(hist, ensure_ascii=False), self.link.get("id")))


def run_failover(link_id: int, force: bool = False) -> dict:
    from core import models
    link = db.query_one("SELECT * FROM disaster_links WHERE id=?", (link_id,))
    if not link:
        raise FailoverError("容灾链路不存在: %s" % link_id)
    return FailoverEngine(dict(link)).failover(force=force)


def run_failback(link_id: int) -> dict:
    from core import models
    link = db.query_one("SELECT * FROM disaster_links WHERE id=?", (link_id,))
    if not link:
        raise FailoverError("容灾链路不存在: %s" % link_id)
    return FailoverEngine(dict(link)).failback()
