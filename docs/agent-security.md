# Agent 执行器安全说明（2026-11-02 安全评审落地）

## 传输安全模型

| 层 | 机制 | 说明 |
|----|------|------|
| 鉴权 | `X-AIDBM-Token` 常量比较 | 每台主机独立 token（平台加密存储） |
| 完整性 | **HMAC-SHA256 必选** | 明文请求无 sig 一律 403；AES 信封内数据天然受信 |
| 机密性 | AES-256-GCM（cryptography 可用时） | 请求体与 JSON 响应全程加密 |
| 完整性兜底 | AES 封装失败时降级明文**但强制响应内嵌 sig** | 平台校验失败即报错，绝不静默 |
| 路径白名单 | `--allow-dirs` | 文件操作仅限白名单目录（默认 /tmp 等） |
| 请求体上限 | 16MB（413） | 防 OOM；大文件走 512KB 分块 + sha256 断点续传 |

## 版本要求
- agent ≥ **1.1.0**（caps 自报 `hmac: required`；平台探活时校验）
- cryptography 缺失的系统：机密性降级（明文 + HMAC），完整性/鉴权不降级；
  建议安装 `pip install cryptography` 恢复全加密

## 生产部署红线
1. **TLS**：执行器本身提供 HTTP；生产环境必须置于 nginx TLS 反代之后
   （`location /agent/ { proxy_pass http://127.0.0.1:8466; }`），或确保端口
   仅内网可达（`iptables/安全组` 限制来源为平台机 IP）。
2. **最小权限**：agent 以非 root 运行时，备份目标目录需对该用户可读写；
   白名单按需收敛，勿开放 `/`。
3. **token 即权限**：泄露等于该主机的 shell 执行权——仅存平台加密元库，
   不入日志（平台侧日志统一脱敏），泄露后立即在「Agent 管理」删除注册并重装换发。

## 变更保障流程（防再发）
任何 agent/通道改动后，交付前必须执行：
```
pytest tests/test_agent_exec.py -q        # 单测（含安全四项）
python scripts/qa/agent_smoke.py          # 全部已注册 agent 真机冒烟（必须全绿）
```
冒烟覆盖：探活/能力自报/exec 同步/流式/文件往返（gzip）/断点续传/
无 sig 403/坏 sig 403/响应签名校验。任何一台失败即阻断交付。
