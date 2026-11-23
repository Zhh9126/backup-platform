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


def _role_conns(cfg: dict):
    """按 active_role 返回 (当前主, 当前备) 的明文连接配置。

    failover_config 的 primary_conn/standby_conn 是稳定的拓扑定义
    （"原始主" / "原始备"），切换后不物理交换——否则 GTID 分叉检测、
    failback 回切语义会随之漂移。新增 active_role 标记当前承载写入的
    一方，切换成功后原子翻转。健康探测、DNS 期望 IP 全部以当前主为准
    （2026-11-23 容灾审查 P0-1：此前切换后监控仍探测旧主，自动容灾空转）。
    """
    active = (cfg.get("active_role") or "primary")
    if active == "standby":
        return (_decrypt_conn(cfg.get("standby_conn") or {}),
                _decrypt_conn(cfg.get("primary_conn") or {}))
    return (_decrypt_conn(cfg.get("primary_conn") or {}),
            _decrypt_conn(cfg.get("standby_conn") or {}))


def _commit_switch_topology(link: dict, cfg: dict, active_role: str,
                            primary_host: str, dr_host: str) -> dict:
    """切换成功后原子写入 active_role + 站点字段，避免"角色已换配置未换"。"""
    new_cfg = dict(cfg)
    new_cfg["active_role"] = active_role
    new_cfg["last_role_change_at"] = db.now_iso()
    db.execute(
        "UPDATE disaster_links SET failover_config=?, status='active', "
        "primary_site=?, dr_site=?, updated_at=? WHERE id=?",
        (json.dumps(new_cfg, ensure_ascii=False),
         primary_host or "", dr_host or "", db.now_iso(), link.get("id")))
    return {"active_role": active_role, "primary": primary_host, "dr": dr_host}


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


# 提升时必须成功的语句：只读解除失败意味着"提升后的主库仍不可写"，
# 绝不能被当作可忽略错误吞掉后继续切流量（2026-11-23 容灾审查 P0-4）。
_REQUIRED_PROMOTE_STMTS = ("SET GLOBAL read_only=0", "SET GLOBAL super_read_only=0")


def _promote_mysql(host, port, user, password):
    """MySQL 备库提升为主：停复制 + 清残留复制配置 + 关只读。返回执行的语句清单。

    STOP/RESET REPLICA 在非复制实例上报错可忽略；但只读解除失败必须
    抛错中止——否则 DNS 把流量引到只读节点，全量写失败。
    """
    conn = native_conn.connect("mysql", host, port, None, user, password, timeout=8)
    stmts = []
    try:
        cur = conn.cursor()
        for stmt in ("STOP REPLICA", "RESET REPLICA ALL",
                     "SET GLOBAL read_only=0",
                     "SET GLOBAL super_read_only=0"):
            try:
                cur.execute(stmt)
                stmts.append((stmt, "ok"))
            except Exception as e:
                if stmt in _REQUIRED_PROMOTE_STMTS:
                    raise FailoverError(
                        "提升失败：%s 执行失败（%s）——节点仍处于只读，"
                        "禁止继续切换流量" % (stmt, str(e)[:120])) from e
                # STOP/RESET REPLICA 在非复制实例上会报错——视为已停，不阻断提升。
                # RESET REPLICA ALL 清掉提升后残留的复制元数据（否则新主上会
                # 留一条 No/No 的僵尸复制配置行，170/171 切换演练实测暴露）
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
        # 转从即恢复只读（+super）：防止降级过程中/之后出现第二个写入点
        # （split-brain 防线；failback 双向使用——旧主本就 frozen，现主
        # 降级时由此上锁。170/171 容灾 E2E 实测暴露只读未恢复）
        for stmt in ("SET GLOBAL super_read_only=1", "SET GLOBAL read_only=1"):
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
        # 等待追平（最多 60s）。注意：Seconds_Behind=NULL 表示 IO 线程尚未
        # 连上（复制没跑起来），**不能当成已追平**——此前 `behind == 0 or
        # behind is None` 在 SQL=Yes/IO=Connecting 窗口内立即误判追平，
        # 切换期写入实际未到达旧主（170/171 容灾 E2E 实测暴露）
        deadline = time.time() + 60
        behind = None
        io_sql = ""
        stable = 0            # 连续满足条件的采样次数
        while time.time() < deadline:
            cur.execute("SHOW REPLICA STATUS")
            row = cur.fetchone()
            if row:
                cols = [c[0] for c in cur.description]
                d = dict(zip(cols, row))
                io_ok = (d.get("Replica_IO_Running") or d.get("Slave_IO_Running")) == "Yes"
                sql_ok = (d.get("Replica_SQL_Running") or d.get("Slave_SQL_Running")) == "Yes"
                behind = d.get("Seconds_Behind_Source")
                io_sql = "%s/%s" % (d.get("Replica_IO_Running"),
                                    d.get("Slave_SQL_Running"))
                # 复制线一旦报错（1236 源端 binlog 已 purge / 1236 GTID 不兼容等），
                # 等多久都不会自愈——立即如实失败，别耗满 60s。
                last_err = (d.get("Last_IO_Error") or d.get("Last_SQL_Error") or "").strip()
                if last_err:
                    raise FailoverError(
                        "rejoin 失败：复制线报错「%s」——源端缺少所需 binlog/GTID，"
                        "自动追平不可行，需用备份重建对端后重试" % last_err[:300])
                if io_ok and sql_ok and behind == 0:
                    # 刚 START REPLICA 的瞬间 IO 会短暂显示 Yes（TCP 已通，尚未开始
                    # 拉日志），要求连续 2 次采样稳定才算真追平（2026-11-14 实测：
                    # 单次采样导致 0.5s 内误报 caught_up，几秒后即 1236）
                    stable += 1
                    if stable >= 2:
                        return {"caught_up": True, "seconds_behind": behind,
                                "samples": stable}
                else:
                    stable = 0
            time.sleep(2)
        raise FailoverError(
            "旧主 rejoin 追平失败（60s 内未连续稳定达到 IO/SQL=Yes/Yes 且 behind=0，"
            "当前 %s，behind=%s）——复制未真正追平，拒绝继续回切以防数据缺失"
            % (io_sql or "无状态", behind))
    finally:
        try:
            conn.close()
        except Exception:
            pass


# --------------------- GTID 集合运算（分叉判定用） ---------------------
# 注意：@@gtid_executed 返回「uuid:区间」文本，早期实现直接按文本切分做差集，
# 导致「旧主 1-6 ⊂ 现主 1-184」这种正常包含关系也被判成分叉（"u:1-6" != "u:1-184"），
# failback 会被 100% 误拦。这里按 GTID 语义解析成区间后再判包含。

def _parse_gtid_set(text: str) -> dict:
    """把 gtid_executed 文本解析为 {uuid: [(start, end), ...]}。"""
    result = {}
    for line in (text or "").replace(",", "\n").split("\n"):
        line = line.strip()
        if not line or ":" not in line:
            continue
        uuid, _, intervals = line.partition(":")
        uuid = uuid.strip()
        if not uuid:
            continue
        spans = result.setdefault(uuid, [])
        for seg in intervals.split(":"):
            seg = seg.strip()
            if not seg:
                continue
            if "-" in seg:
                a, _, b = seg.partition("-")
                try:
                    spans.append((int(a), int(b)))
                except (TypeError, ValueError):
                    continue
            else:
                try:
                    n = int(seg)
                    spans.append((n, n))
                except (TypeError, ValueError):
                    continue
    return result


def _merge_spans(spans: list) -> list:
    """合并重叠/相邻区间，用于「是否被覆盖」判断。"""
    if not spans:
        return []
    ordered = sorted(spans)
    merged = [list(ordered[0])]
    for a, b in ordered[1:]:
        if a <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def _subtract_span(span: tuple, covered: list) -> list:
    """返回 span 中未被 covered（已合并区间）覆盖的剩余片段。"""
    a, b = span
    remain = []
    cursor = a
    for x, y in covered:
        if y < cursor:
            continue
        if x > b:
            break
        if x > cursor:
            remain.append((cursor, min(b, x - 1)))
        cursor = max(cursor, y + 1)
        if cursor > b:
            break
    if cursor <= b:
        remain.append((cursor, b))
    return remain


def gtid_missing(old_map: dict, new_map: dict) -> list:
    """返回 old 中不被 new 覆盖的区间 [(uuid, start, end), ...]。

    old = 旧主已执行事务，new = 现主已执行事务。空结果表示旧主是现主子集
    （无分叉），可安全 rejoin。
    """
    missing = []
    for uuid, spans in (old_map or {}).items():
        covered = _merge_spans((new_map or {}).get(uuid) or [])
        for span in _merge_spans(spans):
            for a, b in _subtract_span(span, covered):
                missing.append((uuid, a, b))
    return missing


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

class _FailoverEngineCore:
    """一键容灾切换 / 回切编排（带 RTO 计时与步骤级日志）。"""

    def __init__(self, link: dict):
        self.link = dict(link)
        self.cfg = _load_config(link)
        self.primary = _decrypt_conn(self.cfg.get("primary_conn") or {})
        self.standby = _decrypt_conn(self.cfg.get("standby_conn") or {})
        self.dns_cfg = self.cfg.get("dns") or {}
        self.steps = []
        # 进度回调（容灾编排层注入）：每完成一步立刻回调，前端可实时看进度
        self.on_step = None
        # 切换范围（业务切换/DNS 切换两步拆分）：True 时 failover/failback
        # 跳过 DNS 步骤——仅做业务层主备切换（DNS 牵引由 dns_only 单独执行）
        self.skip_dns = False
        # 中止信号（编排层注入 threading.Event）：切换执行中可由用户中止。
        # 判据在**步骤边界**检查——正在执行的步骤（SSH/数据库操作）不强杀，
        # 该步骤跑完即停止后续步骤（诚实边界：不承诺步骤内中断）。
        self.cancel_event = None

    def _step(self, name, fn):
        t0 = time.monotonic()
        try:
            detail = fn() or {}
            step = {"name": name, "ok": True,
                    "ms": int((time.monotonic() - t0) * 1000), "detail": detail}
            self.steps.append(step)
            self._notify(step)
        except Exception as e:
            step = {"name": name, "ok": False,
                    "ms": int((time.monotonic() - t0) * 1000),
                    "detail": {"error": str(e)[:200]}}
            self.steps.append(step)
            self._notify(step)
            raise
        # 中止判据放在 try/except 之外（步骤边界）：当前步骤已成功落账，
        # 再抛中止异常不会被上面的 except 误记为「步骤失败」。
        if self.cancel_requested():
            raise FailoverError("切换已被用户中止（步骤「%s」完成后停止）" % name)
        return detail

    def cancel_requested(self) -> bool:
        """是否收到中止请求（编排层注入 threading.Event，可为空）。"""
        ev = self.cancel_event
        if ev is None:
            return False
        try:
            return bool(ev.is_set())
        except Exception:
            return False

    def _notify(self, step: dict) -> None:
        if not self.on_step:
            return
        try:
            self.on_step(step)
        except Exception:
            pass          # 进度上报失败不影响切换主流程

    # ---- failover：主库故障 → 备库提升 + DNS 切换 ----
    def failover(self, force: bool = False) -> dict:
        if not self.primary or not self.standby:
            raise FailoverError("链路缺少 failover_config（primary_conn/standby_conn）")
        t0 = time.monotonic()
        pc, sc = _role_conns(self.cfg)   # 以 active_role 判定当前主/备（切换后不失效）

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

        # 步骤 0.5：Fencing（围栏）——业界 failover 三步法 detect → fence →
        # redirect 中不可省略的一步：即便主库可达（force 场景，如原主假死
        # 后恢复），也必须先冻结其写入（read_only=1 + super_read_only=1），
        # 保证提升备库前后**同一时刻只有一个写入点**，防止 split-brain
        # 数据分叉（调研：PRadeep failover 三步法 / Patroni 硬 fencing）。
        def _fence():
            r = _probe_mysql(pc.get("host"), pc.get("port"),
                             pc.get("user"), pc.get("password"), timeout=5)
            if not r["reachable"]:
                # 不可达 ≠ 已冻结：网络单向分区/主机假死时，旧主可能仍在接写。
                # 生产环境唯一可靠的 fencing 是 STONITH，平台做不到就必须把
                # 确认闸门交还人工——绝不静默当"无需围栏"放过（P0-3）。
                if not force:
                    raise FailoverError(
                        "旧主 %s:%s 不可达，平台无法执行 read_only 围栏——"
                        "网络分区/假死场景下旧主可能仍在写入，直接提升会造成双写。"
                        "请人工确认旧主已断电/隔离后使用 force=true 重试，"
                        "或先恢复旧主可达性。" % (pc.get("host"), pc.get("port")))
                return {"fenced": "unreachable_force（人工 force 越过围栏，"
                                  "须自行确认旧主已隔离）", "force_override": True}
            conn = native_conn.connect("mysql", pc.get("host"), pc.get("port"),
                                       None, pc.get("user"), pc.get("password"), timeout=5)
            try:
                cur = conn.cursor()
                cur.execute("SET GLOBAL read_only=1")
                cur.execute("SET GLOBAL super_read_only=1")
                conn.commit()
                cur.close()
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            return {"fenced": "旧主已置 read_only=1 + super_read_only=1（写入冻结）"}
        self._step("Fencing 旧主写入冻结", _fence)

        # 步骤 1：备库提升为主
        self._step("备库提升为主库", lambda: {
            "stmts": _promote_mysql(sc.get("host"), sc.get("port"),
                                    sc.get("user"), sc.get("password"))})

        # 步骤 2：DNS 切换（scope=business 即 skip_dns=True 时跳过，
        # 由「仅 DNS 切换」步骤单独执行——业务切换与 DNS 牵引两步可见可拆）
        domain = self.dns_cfg.get("domain")
        if self.skip_dns:
            self._step("DNS 切换（跳过：仅业务切换，DNS 由单独步骤执行）",
                       lambda: {"skipped": True, "domain": domain or ""})
        elif domain:
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
        # 业务导向（容灾以业务为最终目标，数据库只是底层）：数据库切换
        # 完成后，应用/中间件层需要人工跟进的事项清单随结果返回
        result = {"direction": "failover", "to": sc.get("host"),
                  "steps": self.steps, "rto_sec": rto, "at": db.now_iso(),
                  "manual_checklist": [
                      "确认应用连接池已重连到新主（域名解析已切换，客户端需重试重连）",
                      "重启或重载依赖该库的缓存/队列中间件连接（如 Redis/Nginx upstream）",
                      "通知业务方进行状态与一致性确认（对标政务云方案 60s 检查步骤）",
                      "确认后尽快在原主侧重新配置反向复制，保持持续保护",
                  ]}
        self._save_history(result)
        # 切换成功后翻转 active_role（原子落库）——否则自动监控仍探测旧主，
        # 新主故障时永不触发下一次切换（P0-1）。
        new_role = "standby" if (self.cfg.get("active_role") or "primary") == "primary" else "primary"
        result["topology"] = _commit_switch_topology(
            self.link, self.cfg, new_role,
            sc.get("host"), pc.get("host"))
        return result

    # ---- 仅 DNS 切换（业务切换已完成时的 DNS 牵引 / 补偿执行）----
    def switch_dns_only(self, direction: str = "failover") -> dict:
        """只做 DNS 牵引，不碰数据库主备角色。

        direction=failover：域名指向备端（现承载切到容灾环境）；
        direction=failback：域名指向原主端（回切牵引）。
        """
        domain = self.dns_cfg.get("domain")
        if not domain:
            raise FailoverError("链路未配置 DNS（domain），无法执行 DNS 切换")
        target = (self.standby if direction == "failover" else self.primary)
        if not (target or {}).get("host"):
            raise FailoverError("切换目标连接缺少 host，无法执行 DNS 切换")
        provider = _dns_provider(self.cfg)
        t0 = time.monotonic()
        r = self._step("DNS 切换（%s → %s）" % (domain, target.get("host")),
                       lambda: self._dns_set_and_verify(provider, domain,
                                                        target.get("host")))
        rto = round(time.monotonic() - t0, 1)
        return {"direction": direction, "scope": "dns_only",
                "domain": domain, "to": target.get("host"),
                "steps": self.steps, "rto_sec": rto, "at": db.now_iso()}

    @staticmethod
    def _dns_set_and_verify(provider, domain: str, host: str) -> dict:
        r = provider.set_record(domain, host)
        time.sleep(2)
        resolved = _resolve_domain(domain)
        if resolved != host:
            raise FailoverError("DNS 切换后解析未生效: %s != %s" % (resolved, host))
        r["resolved"] = resolved
        return r

    # ---- failback：旧主追平后提升，DNS 切回 ----
    def failback(self) -> dict:
        if not self.primary or not self.standby:
            raise FailoverError("链路缺少 failover_config")
        t0 = time.monotonic()
        # pc = 配置中的原主（回切目标），sc = 当前承载写入的一方。
        # 用角色感知取 sc：failover 后 active_role=standby，若仍写死
        # self.standby 就会把"已被提升的备库"当现主重复提升（P0-1）。
        pc = self.primary
        sc, _ = _role_conns(self.cfg)
        # replication_user 密码同为密文存储，必须解密——否则密文被当明文
        # 传给 CHANGE REPLICATION SOURCE，MySQL 8 报 3056（超 32 字符）
        # （170/171 容灾 E2E 实测暴露）
        repl = _decrypt_conn(self.cfg.get("replication_user") or {})

        # 步骤 0：GTID 分叉检测——旧主 rejoin 前必须确认其已执行事务集是
        # 现主的子集（即旧主在切换后**没有**出现独立写入）。存在分叉时
        # 如实拒绝并提示处置策略，绝不静默丢弃数据（业界惯例：pg_rewind
        # 小分歧/全量重建大分歧，均需人工确认）。
        def _divergence_check():
            def gtid_text(c):
                conn = native_conn.connect("mysql", c.get("host"), c.get("port"),
                                           None, c.get("user"), c.get("password"), timeout=8)
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT @@GLOBAL.gtid_executed")
                    v = cur.fetchone()[0] or ""
                    cur.close()
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
                return v
            old_map = _parse_gtid_set(gtid_text(pc))
            new_map = _parse_gtid_set(gtid_text(sc))
            # 事务区间级比对：旧主的已执行事务必须全部被现主覆盖。
            # 旧主有而现主缺失的事务 = 切换后旧主上的独立写入 = 真分叉。
            missing = gtid_missing(old_map, new_map)
            if missing:
                shown = ", ".join("%s:%d-%d" % (u[:8], a, b) for u, a, b in missing[:5])
                raise FailoverError(
                    "检测到旧主 %s 存在现主缺失的事务（%s%s）——"
                    "切换后旧主上可能发生了独立写入（数据分叉）。"
                    "请人工核对后选择：a) 丢弃旧主分歧数据并重试；"
                    "b) 导出分歧数据合并到现主。当前已拒绝自动回切，防止数据丢失。"
                    % (pc.get("host"), shown, " 等 %d 段" % len(missing) if len(missing) > 5 else ""))
            return {"old_uuids": len(old_map), "new_uuids": len(new_map),
                    "missing_txn_spans": 0, "divergence": "none"}
        self._step("GTID 分叉检测", _divergence_check)

        # 步骤 1：旧主以从库身份追平现主增量
        self._step("旧主追平现主增量", lambda: _rejoin_as_replica(
            pc.get("host"), pc.get("port"), pc.get("user"), pc.get("password"),
            sc.get("host"), sc.get("port"),
            repl.get("user") or "repl", repl.get("password") or ""))

        # 步骤 1.5：Fencing 现主（回切同样不能省）——在提升旧主前先冻结当前
        # 写入点，否则 pc 提升后 sc 仍可写，出现分钟级双写窗口（P0-2）。
        def _fence_current():
            r = _probe_mysql(sc.get("host"), sc.get("port"),
                             sc.get("user"), sc.get("password"), timeout=5)
            if not r["reachable"]:
                raise FailoverError(
                    "现主 %s:%s 不可达，无法执行 fencing（回切会让旧主与新主同时可写）。"
                    "请人工确认现主已隔离后重试。" % (sc.get("host"), sc.get("port")))
            _set_read_only(sc, True)
            return {"fenced": True, "note": "回切前现主 read_only=1 + super_read_only=1"}
        self._step("Fencing 现主写入冻结（回切防双写）", _fence_current)

        # 步骤 2：提升旧主为主
        self._step("提升旧主为主库", lambda: {
            "stmts": _promote_mysql(pc.get("host"), pc.get("port"),
                                    pc.get("user"), pc.get("password"))})

        # 步骤 3：现主降级为从（指向旧主，保持持续保护）
        self._step("现主转为从库", lambda: _rejoin_as_replica(
            sc.get("host"), sc.get("port"), sc.get("user"), sc.get("password"),
            pc.get("host"), pc.get("port"),
            repl.get("user") or "repl", repl.get("password") or ""))

        # 步骤 4：DNS 切回（skip_dns=True 时跳过，同 failover 口径）
        domain = self.dns_cfg.get("domain")
        if self.skip_dns:
            self._step("DNS 切回（跳过：仅业务回切，DNS 由单独步骤执行）",
                       lambda: {"skipped": True, "domain": domain or ""})
        elif domain:
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
                  "steps": self.steps, "rto_sec": rto, "at": db.now_iso(),
                  "manual_checklist": [
                      "确认应用连接池已重连回原主（域名已切回）",
                      "重启/重载中间件连接（缓存/Nginx upstream）",
                      "通知业务方进行状态与一致性确认",
                      "确认反向复制（现主→原主）运行正常，保持持续保护",
                  ]}
        self._save_history(result)
        # 回切后当前主恢复为配置中的原主 → active_role 归位 primary
        result["topology"] = _commit_switch_topology(
            self.link, self.cfg, "primary", pc.get("host"), sc.get("host"))
        return result

    def _save_history(self, result: dict):
        # 必须从库实时重取历史再插入——同一引擎实例连续执行切换+回切时，
        # self.link 是初始化快照，用它会覆盖掉前一阶段刚写入的历史条目
        # （切换演练实测暴露的 lost-update）。
        row = db.query_one("SELECT failover_history FROM disaster_links WHERE id=?",
                           (self.link.get("id"),))
        raw = (row.get("failover_history") if row else None) or "[]"
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


# ---------------------------------------------------------------------------
# 应用级容灾接入：链路级并发护栏 + 切换后业务验证（默认零破坏）
# ---------------------------------------------------------------------------
import threading as _threading  # noqa: E402

_LINK_LOCKS = {}
_LOCK_GUARD = _threading.Lock()


def _acquire_link_lock(link_id: int):
    """手动向导与自动监控可同时触发切换，链路级非阻塞锁防双切换。"""
    with _LOCK_GUARD:
        lk = _LINK_LOCKS.setdefault(link_id, _threading.Lock())
    if not lk.acquire(blocking=False):
        raise FailoverError("链路 %s 已有切换/回切在执行，请勿并发触发" % link_id)
    return lk


def _attach_business_verification(result: dict, cfg: dict, phase: str,
                                  link_id: int) -> dict:
    from core import dr_app_ha
    v = dr_app_ha.run_post_switch_verification(
        cfg, phase=phase, decrypt_conn=_decrypt_conn)
    result["business_verification"] = v
    result["business_ok"] = bool(v.get("ok"))
    if v.get("configured") and not v.get("ok"):
        result["message"] = v.get("message")
        db.add_log("ERROR", "dr_failover",
                   "链路 %s %s：%s" % (link_id, phase, v.get("message")))
    return result


def run_failover(link_id: int, force: bool = False) -> dict:
    lk = _acquire_link_lock(link_id)
    try:
        link = db.query_one("SELECT * FROM disaster_links WHERE id=?", (link_id,))
        if not link:
            raise FailoverError("容灾链路不存在: %s" % link_id)
        link = dict(link)
        cfg = _load_config(link)
        res = FailoverEngine(link).failover(force=force)
        return _attach_business_verification(res, cfg, "failover", link_id)
    finally:
        lk.release()


def run_failback(link_id: int) -> dict:
    lk = _acquire_link_lock(link_id)
    try:
        link = db.query_one("SELECT * FROM disaster_links WHERE id=?", (link_id,))
        if not link:
            raise FailoverError("容灾链路不存在: %s" % link_id)
        link = dict(link)
        cfg = _load_config(link)
        res = FailoverEngine(link).failback()
        return _attach_business_verification(res, cfg, "failback", link_id)
    finally:
        lk.release()


# ---------------------------------------------------------------------------
# 自动切换策略 + 健康快照（P1-1，对标 DSG 智能导航切换 / 甘肃政务云 §4.4.3）
# ---------------------------------------------------------------------------

AUTO_POLICY_DEFAULTS = {
    "enabled": False,                # 自动监控总开关（默认关，不改变存量行为）
    "check_interval_sec": 30,        # 探测间隔
    "consecutive_failures": 3,       # 连续 N 次探测失败触发
    "require_manual_confirm": True,  # true=只告警不切换（默认）；false=自动切换
}


def auto_policy(cfg: dict) -> dict:
    """提取自动切换策略（缺省补全），调用方传入 _load_config 结果。"""
    out = dict(AUTO_POLICY_DEFAULTS)
    auto = cfg.get("auto") if isinstance(cfg, dict) else None
    if isinstance(auto, dict):
        for k in out:
            if k in auto and auto[k] is not None:
                out[k] = auto[k]
    out["check_interval_sec"] = max(10, int(out["check_interval_sec"] or 30))
    out["consecutive_failures"] = max(1, int(out["consecutive_failures"] or 3))
    return out


def _replica_status(host, port, user, password, timeout=5) -> dict:
    """备库复制状态：io/sql 线程与延迟（兼容 5.7 Slave_* / 8.0 Replica_*）。"""
    conn = None
    try:
        conn = native_conn.connect("mysql", host, port, None, user,
                                   password, timeout=timeout)
        cur = conn.cursor()
        cur.execute("SHOW REPLICA STATUS")
        row = cur.fetchone()
        if not row:
            return {"reachable": True, "is_replica": False, "io": "-", "sql": "-", "behind": None}
        cols = [c[0] for c in cur.description]
        d = dict(zip(cols, row))
        io = d.get("Replica_IO_Running") or d.get("Slave_IO_Running") or "-"
        sql = d.get("Replica_SQL_Running") or d.get("Slave_SQL_Running") or "-"
        behind = d.get("Seconds_Behind_Source")
        if behind is None:
            behind = d.get("Seconds_Behind_Master")
        return {"reachable": True, "is_replica": True, "io": io, "sql": sql,
                "behind": behind}
    except Exception as e:
        return {"reachable": False, "is_replica": False, "io": "-", "sql": "-",
                "behind": None, "error": str(e)[:160]}
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def health_snapshot(link_id: int) -> dict:
    """链路健康快照（自动监控与切换向导预检共用数据源）。

    返回 {link_id, primary, standby, dns, checked_at}：
    - primary:  {reachable, error, host, port}
    - standby:  {reachable, io, sql, behind, is_replica, error, host, port}
    - dns:      {domain, resolved_ip, expected_ip, in_sync}（未配置 DNS 时各项为空）
    """
    from core import models as _models
    link = _models.get_disaster_link(link_id)
    if not link:
        raise FailoverError("容灾链路不存在: %s" % link_id)
    cfg = _load_config(dict(link))
    # 以 active_role 判定当前主/备：切换成功后角色翻转，监控必须跟着走，
    # 否则探测旧只读主永远"健康"，新主故障被漏报（P0-1）。
    p, s = _role_conns(cfg)
    if not p.get("host"):
        raise FailoverError("链路缺少 failover_config.primary_conn，无法探测")
    snap = {"link_id": link_id, "link_name": link.get("name"),
            "checked_at": db.now_iso(), "primary": None, "standby": None,
            "dns": None, "app": None}
    # 主库
    pr = _probe_mysql(p.get("host"), int(p.get("port") or 3306),
                      p.get("user") or p.get("username") or "root",
                      p.get("password") or "")
    pr.update({"host": p.get("host"), "port": p.get("port")})
    snap["primary"] = pr
    # 备库（复制状态）
    if s.get("host"):
        st = _replica_status(s.get("host"), int(s.get("port") or 3306),
                             s.get("user") or s.get("username") or "root",
                             s.get("password") or "")
        st.update({"host": s.get("host"), "port": s.get("port")})
        snap["standby"] = st
    # DNS 一致性：域名解析 IP 是否指向主库
    dns_cfg = cfg.get("dns") or {}
    domain = dns_cfg.get("domain")
    if domain:
        expected = p.get("host") or ""
        try:
            resolved = _resolve_domain(domain)
        except Exception as e:
            resolved = ""
            snap["dns"] = {"domain": domain, "resolved_ip": "",
                           "expected_ip": expected, "in_sync": False,
                           "error": str(e)[:120]}
        else:
            snap["dns"] = {"domain": domain, "resolved_ip": resolved,
                           "expected_ip": expected,
                           "in_sync": bool(resolved and resolved == expected)}
    # 应用级容灾：平台侧出站探活业务健康端点（未配置时 ok=True，零破坏）
    from core import dr_app_ha
    snap["app"] = dr_app_ha.probe_app(cfg.get("app_probe") or {})
    return snap


# ---------------------------------------------------------------------------
# 计划性切换（graceful switchover）+ 容灾切换演练（switch drill）
#
# 与 failover(force) 的本质区别：failover 是「主库已挂/疑似挂」时的
# 故障切换（立即提升，容忍最后一段未送达事务丢失）；计划性切换是
# 主库健康前提下的**优雅换主**——先冻结旧主写入、等备库完全追平
# （RPO=0）再提升，零数据丢失。这是切换演练/换季维护的标准路径。
# ---------------------------------------------------------------------------

_PROBE_DB = "dr_switch_probe"
_PROBE_TBL = "probe"


def _fetch_gtid_executed(conn_cfg: dict) -> str:
    conn = native_conn.connect("mysql", conn_cfg.get("host"),
                               conn_cfg.get("port"), None,
                               conn_cfg.get("user"), conn_cfg.get("password"),
                               timeout=8)
    try:
        cur = conn.cursor()
        cur.execute("SELECT @@GLOBAL.gtid_executed")
        v = cur.fetchone()[0] or ""
        cur.close()
        return v
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _wait_standby_catchup(primary: dict, standby: dict,
                          timeout: int = 60) -> dict:
    """计划性切换核心安全步骤：等待备库 GTID 追平旧主。

    判据：备库已执行事务集 ⊇ 旧主已执行事务集（gtid_missing(primary, standby)
    为空）。超时抛 FailoverError——**追不平绝不提升**（RPO=0 的前提）。
    """
    deadline = time.time() + timeout
    s_map = {}
    while True:
        # 每轮重取主库 GTID：只读化如果失效（权限/绕过），等待窗口内的
        # 新事务不在首轮快照里，会被误判已追平而提升、丢数据（P1-16）。
        p_map = _parse_gtid_set(_fetch_gtid_executed(primary))
        s_map = _parse_gtid_set(_fetch_gtid_executed(standby))
        missing = gtid_missing(p_map, s_map)
        if not missing:
            return {"caught_up": True,
                    "primary_uuids": len(p_map), "standby_uuids": len(s_map)}
        if time.time() > deadline:
            raise FailoverError(
                "备库 %s 追平超时（%ds）：主库仍有 %d 段事务未到达备库——"
                "追不平不提升（RPO 保护）。请检查复制链路后重试。"
                % (standby.get("host"), timeout, len(missing)))
        time.sleep(2)


def _conn_of(conn_cfg: dict):
    return native_conn.connect("mysql", conn_cfg.get("host"),
                               conn_cfg.get("port"), None,
                               conn_cfg.get("user"), conn_cfg.get("password"),
                               timeout=8)


def _set_read_only(conn_cfg: dict, on: bool):
    conn = _conn_of(conn_cfg)
    try:
        cur = conn.cursor()
        cur.execute("SET GLOBAL read_only=%d" % (1 if on else 0))
        cur.execute("SET GLOBAL super_read_only=%d" % (1 if on else 0))
        conn.commit()
        cur.close()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _write_probe(conn_cfg: dict, link_id: int, marker: str) -> dict:
    """新主写入探针：往探针库写一行标记——真实写路径验证（可写性 + 事务落盘）。
    幂等：库/表不存在则建，存在则清空重写。"""
    conn = _conn_of(conn_cfg)
    try:
        cur = conn.cursor()
        cur.execute("CREATE DATABASE IF NOT EXISTS %s" % _PROBE_DB)
        cur.execute("USE %s" % _PROBE_DB)
        cur.execute(
            "CREATE TABLE IF NOT EXISTS %s ("
            " id BIGINT PRIMARY KEY AUTO_INCREMENT,"
            " link_id INT NOT NULL, marker VARCHAR(64) NOT NULL,"
            " written_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)" % _PROBE_TBL)
        cur.execute("TRUNCATE TABLE %s" % _PROBE_TBL)
        cur.execute(
            "INSERT INTO %s (link_id, marker) VALUES (%%s, %%s)" % _PROBE_TBL,
            (link_id, marker))
        conn.commit()
        cur.close()
        return {"written": True, "marker": marker}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _read_probe(conn_cfg: dict, marker: str) -> dict:
    conn = _conn_of(conn_cfg)
    try:
        cur = conn.cursor()
        cur.execute("SELECT marker FROM %s.%s" % (_PROBE_DB, _PROBE_TBL))
        rows = [r[0] for r in cur.fetchall()]
        cur.close()
        return {"found": marker in rows, "rows": rows[:5]}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _drop_probe(conn_cfg: dict) -> dict:
    conn = _conn_of(conn_cfg)
    try:
        cur = conn.cursor()
        cur.execute("DROP DATABASE IF EXISTS %s" % _PROBE_DB)
        conn.commit()
        cur.close()
        return {"dropped": True}
    finally:
        try:
            conn.close()
        except Exception:
            pass


class SwitchPlannedMixin:
    """计划性切换两阶段。挂在 FailoverEngine 上（复用 _step/_dns_provider/
    _save_history 与链路锁）。"""

    def switch_planned(self) -> dict:
        """阶段一：优雅换主（旧主冻结 → 追平 → 提升备库 → DNS → 写入探针 → 旧主 rejoin）。"""
        if not self.primary or not self.standby:
            raise FailoverError("链路缺少 failover_config（primary_conn/standby_conn）")
        t0 = time.monotonic()
        _phase0 = len(self.steps)      # 阶段步骤切片起点（self.steps 跨阶段累积）
        pc, sc = self.primary, self.standby

        # 步骤 0：预检——计划性切换要求主库健康；主库不可达请走故障切换
        def _probe_primary():
            r = _probe_mysql(pc.get("host"), pc.get("port"),
                             pc.get("user"), pc.get("password"))
            if not r["reachable"]:
                raise FailoverError(
                    "主库 %s:%s 不可达——这不是计划性切换的场景，"
                    "请使用故障切换（failover，force=true）" % (pc.get("host"), pc.get("port")))
            r2 = _probe_mysql(sc.get("host"), sc.get("port"),
                              sc.get("user"), sc.get("password"))
            if not r2["reachable"]:
                raise FailoverError("备库 %s:%s 不可达，无法切换"
                                    % (sc.get("host"), sc.get("port")))
            return {"primary": "reachable", "standby": "reachable"}
        self._step("切换预检（主备可达）", _probe_primary)

        # 步骤 1：复制健康（备库必须正在复制）
        def _repl_health():
            st = _replica_status(sc.get("host"), sc.get("port"),
                                 sc.get("user"), sc.get("password"))
            if not st.get("is_replica"):
                raise FailoverError(
                    "备库 %s 未配置复制（SHOW REPLICA STATUS 为空）——没有持续保护的链路不允许切换"
                    % sc.get("host"))
            if not (str(st.get("io")).startswith("Yes") and str(st.get("sql")).startswith("Yes")):
                raise FailoverError("备库复制线程不健康 IO=%s SQL=%s"
                                    % (st.get("io"), st.get("sql")))
            return st
        self._step("复制健康检查", _repl_health)

        # 步骤 2：旧主只读化（写入冻结点——此后旧主不再产生新事务）
        def _fence_primary():
            _set_read_only(pc, True)
            return {"fenced": True, "note": "read_only=1 + super_read_only=1"}
        self._step("旧主进入只读（写入冻结）", _fence_primary)

        # 步骤 3：等待备库追平（RPO=0 前提；失败自动解除只读回滚）
        try:
            self._step("等待备库追平（RPO=0）",
                       lambda: _wait_standby_catchup(pc, sc, timeout=60))
        except Exception:
            try:
                _set_read_only(pc, False)
                self._step("回滚：解除旧主只读", lambda: {"rolled_back": True})
            except Exception:
                pass
            raise

        # 步骤 4：备库提升
        self._step("备库提升为主库", lambda: {
            "stmts": _promote_mysql(sc.get("host"), sc.get("port"),
                                    sc.get("user"), sc.get("password"))})

        # 步骤 5：DNS 切换 + 解析回读
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

        rto = round(time.monotonic() - t0, 1)
        result = {"direction": "switch_planned", "to": sc.get("host"),
                  "steps": self.steps[_phase0:], "rto_sec": rto, "at": db.now_iso()}
        self._save_history(result)
        db.execute(
            "UPDATE disaster_links SET status='active', primary_site=?, dr_site=?, "
            "updated_at=? WHERE id=?",
            (self.link.get("dr_site") or sc.get("host"),
             self.link.get("primary_site") or pc.get("host"),
             db.now_iso(), self.link.get("id")))
        return result

    def switch_probe_write(self, marker: str) -> dict:
        """阶段一收尾：新主写入探针（独立方法便于演练编排单独记录步骤）。"""
        detail = self._step("新主写入探针（%s）" % marker,
                            lambda: _write_probe(self.standby, self.link.get("id"), marker))
        return detail

    def switch_rejoin(self) -> dict:
        """阶段一收尾：旧主以从库身份重新指向新主并追平（保持持续保护）。"""
        pc, sc = self.primary, self.standby
        repl = _decrypt_conn(self.cfg.get("replication_user") or {})
        return self._step("旧主重新指向新主（转从+追平）", lambda: _rejoin_as_replica(
            pc.get("host"), pc.get("port"), pc.get("user"), pc.get("password"),
            sc.get("host"), sc.get("port"),
            repl.get("user") or "repl", repl.get("password") or ""))

    def switch_back_planned(self, probe_marker: str = "") -> dict:
        """阶段二：优雅回切（现主冻结 → 追平 → 提升旧主 → 探针核对 → 降级 → DNS 回 → 终态核对）。"""
        pc, sc = self.primary, self.standby      # 配置口径：primary=原主
        t0 = time.monotonic()
        _phase0 = len(self.steps)

        # 步骤 0：预检
        def _probe_both():
            r = _probe_mysql(pc.get("host"), pc.get("port"), pc.get("user"), pc.get("password"))
            if not r["reachable"]:
                raise FailoverError("原主 %s:%s 不可达，无法回切" % (pc.get("host"), pc.get("port")))
            r2 = _probe_mysql(sc.get("host"), sc.get("port"), sc.get("user"), sc.get("password"))
            if not r2["reachable"]:
                raise FailoverError("现主 %s:%s 不可达，无法回切" % (sc.get("host"), sc.get("port")))
            return {"primary": "reachable", "standby": "reachable"}
        self._step("回切预检（双方可达）", _probe_both)

        # 步骤 1：现主只读化（切换回写冻结点）
        def _fence_current():
            _set_read_only(sc, True)
            return {"fenced": True, "note": "read_only=1 + super_read_only=1"}
        self._step("现主进入只读（写入冻结）", _fence_current)

        # 步骤 2：等待原主（现为从库）追平现主
        try:
            self._step("等待原主追平（RPO=0）",
                       lambda: _wait_standby_catchup(sc, pc, timeout=60))
        except Exception:
            try:
                _set_read_only(sc, False)
                self._step("回滚：解除现主只读", lambda: {"rolled_back": True})
            except Exception:
                pass
            raise

        # 步骤 3：提升原主
        self._step("提升原主为主库", lambda: {
            "stmts": _promote_mysql(pc.get("host"), pc.get("port"),
                                    pc.get("user"), pc.get("password"))})

        # 步骤 4：探针回读核对——切换期写入的标记行必须已在原主上（复制闭环硬证据）
        if probe_marker:
            def _probe_check():
                r = _read_probe(pc, probe_marker)
                if not r.get("found"):
                    raise FailoverError(
                        "探针标记 %s 未出现在原主上——切换窗口数据未随复制闭环，禁止继续"
                        % probe_marker)
                return r
            self._step("探针数据闭环核对（切换窗口写入已回达原主）", _probe_check)

        # 步骤 5：现主降级为从（指向原主，保持持续保护）
        repl = _decrypt_conn(self.cfg.get("replication_user") or {})
        self._step("现主转为从库（指向原主）", lambda: _rejoin_as_replica(
            sc.get("host"), sc.get("port"), sc.get("user"), sc.get("password"),
            pc.get("host"), pc.get("port"),
            repl.get("user") or "repl", repl.get("password") or ""))

        # 步骤 6：DNS 切回
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

        # 步骤 7：探针库清理（在恢复后的主库删除，随复制到达从库）
        if probe_marker:
            self._step("探针库清理", lambda: _drop_probe(pc))

        # 步骤 8：拓扑终态核对（原主可写 + 新从库复制健康）
        def _final_assert():
            st = _replica_status(sc.get("host"), sc.get("port"),
                                 sc.get("user"), sc.get("password"))
            problems = []
            if not st.get("is_replica"):
                problems.append("现从库未配置复制")
            elif not (str(st.get("io")).startswith("Yes") and str(st.get("sql")).startswith("Yes")):
                problems.append("复制线程 IO=%s SQL=%s" % (st.get("io"), st.get("sql")))
            if problems:
                raise FailoverError("拓扑终态异常: %s" % "; ".join(problems))
            return {"standby_replication": "%s/%s" % (st.get("io"), st.get("sql")),
                    "behind": st.get("behind"), "topology": "restored"}
        self._step("拓扑终态核对", _final_assert)

        rto = round(time.monotonic() - t0, 1)
        result = {"direction": "switch_back", "to": pc.get("host"),
                  "steps": self.steps[_phase0:], "rto_sec": rto, "at": db.now_iso()}
        self._save_history(result)
        db.execute(
            "UPDATE disaster_links SET status='active', primary_site=?, dr_site=?, "
            "updated_at=? WHERE id=?",
            (self.link.get("primary_site") or pc.get("host"),
             self.link.get("dr_site") or sc.get("host"),
             db.now_iso(), self.link.get("id")))
        return result


class FailoverEngine(SwitchPlannedMixin, _FailoverEngineCore):
    """完整容灾引擎 = 故障切换/回切（_FailoverEngineCore）
    + 计划性切换两阶段（SwitchPlannedMixin，优雅换主/自动回切）。

    现有 failover(force)/failback 路径行为零变化；计划性切换是主库健康
    前提下的零丢失换主通道（切换演练/换季维护标准路径）。
    """
    pass


def run_switch_drill(link_id: int) -> dict:
    """容灾切换演练：计划性切换 → 写入探针 → 自动回切 → 拓扑终态核对。

    全程复用链路级锁（与手动切换/自动切换互斥）；任一步失败立即中止，
    返回已执行步骤 + 当前健康快照，供人工介入恢复。
    """
    lk = _acquire_link_lock(link_id)
    eng = None
    try:
        link = db.query_one("SELECT * FROM disaster_links WHERE id=?", (link_id,))
        if not link:
            raise FailoverError("容灾链路不存在: %s" % link_id)
        link = dict(link)
        eng = FailoverEngine(link)
        marker = "drill-l%d-%d" % (link_id, int(time.time()))

        sw = eng.switch_planned()
        _n0 = len(eng.steps)           # 探针写入 + 转从追平两步随后执行
        eng.switch_probe_write(marker)
        eng.switch_rejoin()
        sw["steps"] = (sw.get("steps") or []) + eng.steps[_n0:]
        sb = eng.switch_back_planned(probe_marker=marker)

        return {"ok": True, "direction": "switch_drill", "link_id": link_id,
                "link_name": link.get("name"), "marker": marker,
                "switch": sw, "switchback": sb,
                "rto_switch_sec": sw.get("rto_sec"),
                "rto_switchback_sec": sb.get("rto_sec"),
                "total_sec": round((sw.get("rto_sec") or 0)
                                   + (sb.get("rto_sec") or 0), 1),
                "steps": (sw.get("steps") or []) + (sb.get("steps") or [])}
    except Exception as e:
        # 演练失败：如实返回失败上下文 + 当前拓扑快照（帮助人工恢复）
        snap = {}
        try:
            snap = health_snapshot(link_id)
        except Exception:
            pass
        steps = (eng.steps if eng is not None else [])
        return {"ok": False, "error": str(e)[:300], "steps": steps,
                "snapshot": snap, "link_id": link_id}
    finally:
        lk.release()
