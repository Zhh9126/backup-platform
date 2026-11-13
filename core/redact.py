# -*- coding: utf-8 -*-
"""日志/审计里的口令脱敏。

备份恢复的远端命令大量依赖命令行传口令（mysqldump -p'xxx'、psql 的
PGPASSWORD、ksql/gsql 的 -W、redis-cli -a、达梦 USERID=u/pw@host 与
disql 'u/"pw"@host'、sqlcmd 的 -P/SQLCMDPASSWORD、连接串
``mysql://user:pw@host/db``……）。这些命令会被写进操作日志、恢复
detail_log、系统日志，导致口令以明文长期落盘——合规审计会直接判高危。

本模块把所有已知形态的口令参数替换为 ``***``，供 db.add_log / 引擎日志
/ 跨主机恢复日志统一调用。

设计要点：**先脱敏再截断**。若先 ``cmd[:200]`` 再脱敏，口令正好被截断
一半时正则匹配不到，明文反而漏出去。
"""
import re

# 形如 -p 'secret' / -a "secret" / -W 'secret' / -P "secret" / --password='secret'
_ARG_SECRET = re.compile(r"(-p|-a|-W|-P|--password)(\s*)(['\"])(.*?)\3")
# 环境变量式：PGPASSWORD='secret' / MYSQL_PWD=secret / REDISCLI_AUTH=...
_ENV_SECRET = re.compile(
    r"(PGPASSWORD|KINGBASE_PASSWORD|MYSQL_PWD|SQLCMDPASSWORD|REDISCLI_AUTH)"
    r"\s*=\s*(['\"]?)([^\s'\";]+)\2")
# USERID='user/pw@host'（JDBC / 达梦）；口令部分允许含 @，贪婪到最后一个 @
_USERID = re.compile(r"(USERID\s*=\s*)(['\"]?)([^'\"@/\s]+)/([^'\"\s]+)@")
# disql 'user/"pw"@host:port'（达梦常用写法，口令在双引号里）
_DISQL = re.compile(r"(['\"])([A-Za-z0-9_$#]+)/\"([^\"]{1,128})\"@")
# 连接串：mysql://user:pw@host/db
_URL = re.compile(r"(://[^:/\s@]+:)([^@\s/]+)(@)")
# password=xxx / pwd=xxx（配置文件、URL 参数）
_KV = re.compile(r"(?i)\b(password|pwd|passwd)(\s*=\s*)([^\s&'\";]+)")

# 只有命中这些提示词才跑正则，避免高频日志调用的无谓开销
_HINTS = ("password", "PASSWORD", "PASSWD", "PWD=", "PWD:", "MYSQL_PWD",
          "PGPASSWORD", "KINGBASE_PASSWORD", "SQLCMDPASSWORD",
          "REDISCLI_AUTH", "USERID", "-p ", "-p'", "-p\"", "-a ",
          "-W ", "-P ", "://", "/\"", "disql")


def redact(text) -> str:
    """把文本中的明文口令替换为 ***；无敏感提示时原样返回（零开销快路径）。"""
    s = str(text if text is not None else "")
    if not s:
        return s
    if not any(h in s for h in _HINTS):
        return s
    s = _ARG_SECRET.sub(lambda m: "%s%s%s***%s" % (m.group(1), m.group(2),
                                                   m.group(3), m.group(3)), s)
    s = _ENV_SECRET.sub(lambda m: "%s=%s***%s" % (m.group(1), m.group(2),
                                                  m.group(2)), s)
    s = _USERID.sub(lambda m: "%s%s%s/***@" % (m.group(1), m.group(2),
                                               m.group(3)), s)
    s = _DISQL.sub(lambda m: '%s%s/"***"@' % (m.group(1), m.group(2)), s)
    s = _URL.sub(lambda m: "%s***%s" % (m.group(1), m.group(3)), s)
    s = _KV.sub(lambda m: "%s%s***" % (m.group(1), m.group(2)), s)
    return s


def redact_cmd(cmd, limit: int = None) -> str:
    """先脱敏再按 limit 截断（顺序很重要，见模块 docstring）。"""
    out = redact(cmd)
    if limit and len(out) > limit:
        return out[:limit]
    return out
