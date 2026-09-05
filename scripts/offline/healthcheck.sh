#!/usr/bin/env bash
# 离线环境预检（部署前在目标机上执行）：输出 PASS/FAIL 报告
set -uo pipefail
PASS=0; FAIL=0
chk() { # chk <描述> <命令>
  if eval "$2" >/dev/null 2>&1; then
    echo "PASS  $1"; PASS=$((PASS+1))
  else
    echo "FAIL  $1 （$2）"; FAIL=$((FAIL+1))
  fi
}
echo "==== 备份平台离线部署预检 $(date '+%F %T') ===="
chk "root 权限"                '[ "$(id -u)" = 0 ]'
chk "磁盘可用 ≥ 20GB（/opt）"  '[ $(df -BG --output=avail /opt | tail -1 | tr -dc 0-9) -ge 20 ]'
chk "Python ≥ 3.9"            'python3 -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)"'
chk "Docker（容器模式可选）"    'command -v docker && docker info'
chk "SSH 客户端"               'command -v ssh'
chk "SSH 服务（agentless 备份源）" 'ss -tln 2>/dev/null | grep -q ":22 "'
chk "curl（健康检查用）"        'command -v curl'
chk "tar/gzip（离线包解压）"    'command -v tar && command -v gzip'
chk "时区已配置"               'test -f /etc/localtime'
chk "内网 NTP 可达（可选）"     'timeout 2 bash -c "echo > /dev/udp/$(grep -hs "^server" /etc/chrony.conf /etc/ntp.conf 2>/dev/null | head -1 | awk "{print \$2}")/123" 2>/dev/null'
echo "----"
echo "结果: PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]] && echo "✅ 环境满足部署条件" || echo "⚠️ 存在失败项，请处理后重试（Docker/NTP 为可选项）"
exit $FAIL
