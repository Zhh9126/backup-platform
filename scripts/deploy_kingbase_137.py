#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""金仓 KingbaseES 一键部署脚本（目标机：192.168.220.137）。

流程：挂载 ISO → 清理残留 → 静默安装（InstallAnywhere -i silent，
授权文件 license_39892_0.dat）→ initdb 初始化实例 → 启动 → 连通验证。

前置条件（已在 137 上完成）：
- kingbase 用户存在（id 51001）
- /root/KingbaseES_V009R001C001B0030_Lin64_install.iso（V9R1 安装介质）
- /root/license_39892_0.dat（正式授权文件）
- /opt/Kingbase、/data/kingbase 目录属主 kingbase

用法：
    python3 scripts/deploy_kingbase_137.py          # 完整部署
    python3 scripts/deploy_kingbase_137.py --verify # 仅连通验证
"""
import sys
import time

import paramiko

HOST, USER, PWD = "192.168.220.137", "root", "Zhh@190226"
ISO = "/root/KingbaseES_V009R001C001B0030_Lin64_install.iso"
LICENSE = "/root/license_39892_0.dat"
KB_PASS = "Kingbase@123"
PORT = 54321


def run(cli, cmd, timeout=120, quiet=False):
    _, out, err = cli.exec_command(cmd, timeout=timeout)
    o = out.read().decode(errors="replace")
    e = err.read().decode(errors="replace")
    if not quiet:
        print(f"$ {cmd[:80]}")
        if o.strip():
            print(o.strip()[:800])
        if e.strip():
            print("[stderr]", e.strip()[:300])
    return o


def main():
    verify_only = "--verify" in sys.argv
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, 22, USER, PWD, timeout=10)

    if verify_only:
        run(cli, f"su - kingbase -c \"ksql -U system -d test -p {PORT} -c 'SELECT version()'\" 2>&1 | head -3")
        cli.close()
        return

    # 1) 挂载 ISO + 清理残留
    run(cli, f"umount /mnt/kbiso2 2>/dev/null; mkdir -p /mnt/kbiso2 && "
             f"mount -o loop,ro '{ISO}' /mnt/kbiso2 && ls /mnt/kbiso2 | head -8")
    run(cli, "rm -rf /opt/Kingbase/ES/V9 /data/kingbase && "
             "mkdir -p /opt/Kingbase/ES /data/kingbase && "
             "chown -R kingbase:kingbase /opt/Kingbase /data/kingbase")

    # 2) 静默安装（InstallAnywhere：-i silent -f response）
    #    注意：IATEMPDIR 必须指向大容量磁盘（/tmp 为 tmpfs 会空间不足）；
    #    安装器拒绝 root 运行，必须 kingbase 用户；2.6GB 解压耗时较长。
    resp = f"""INSTALLER_UI=silent
CHOSEN_INSTALL_SET=Full
KINGBASE_INSTALL_PATH=/opt/Kingbase/ES/V9
KB_DATA_PATH=/data/kingbase
KB_LICENSE_PATH={LICENSE}
DB_PORT={PORT}
DB_USER=system
DB_PASSWD={KB_PASS}
DB_ENCODING=UTF8
DB_CASE_SENSITIVE=YES
"""
    sftp = cli.open_sftp()
    with sftp.open("/tmp/kb_silent.properties", "w") as f:
        f.write(resp)
    run(cli, "echo 'Kingbase@123' > /tmp/kbpw && chown kingbase /tmp/kbpw", quiet=True)
    run(cli, "su - kingbase -c 'export IATEMPDIR=/opt/kbtmp; "
             "cd /mnt/kbiso && nohup sh setup.sh -i silent "
             "-f /tmp/kb_silent.properties > /tmp/kb_install.log 2>&1 &' "
             "&& echo install-started", quiet=True)

    # 3) 轮询等待安装完成（最长 15 分钟）
    print("等待安装完成（最长 15 分钟）...")
    for i in range(45):
        time.sleep(20)
        o = run(cli, "ls /opt/Kingbase/ES/V9/Server/bin/initdb 2>/dev/null && "
                     "grep -c 'Complete' /tmp/kb_install.log 2>/dev/null", quiet=True)
        if "initdb" in o and i > 5:
            # 安装程序结束后进程退出才继续
            o2 = run(cli, "ps aux | grep '[i]nstall.bin' | wc -l", quiet=True)
            if o2.strip() == "0":
                print(f"安装完成（第 {i+1} 次轮询）")
                break
    run(cli, "ls /opt/Kingbase/ES/V9/license.dat 2>/dev/null && "
             "date -r /opt/Kingbase/ES/V9/license.dat", quiet=True)

    # 4) initdb 初始化实例（V9 initdb 无 --mode 参数）
    run(cli, "su - kingbase -c '/opt/Kingbase/ES/V9/Server/bin/initdb "
             "-D /data/kingbase --encoding=UTF8 -U system "
             "--pwfile=/tmp/kbpw' 2>&1 | tail -3", timeout=280)

    # 5) 启动 + 连通验证
    run(cli, f"echo 'port={PORT}' >> /data/kingbase/kingbase.conf && "
             f"chown kingbase /data/kingbase/kingbase.conf && "
             f"su - kingbase -c '/opt/Kingbase/ES/V9/Server/bin/sys_ctl "
             f"-D /data/kingbase -l /tmp/kb.log start' 2>&1 | tail -2")
    time.sleep(3)
    run(cli, f"su - kingbase -c \"ksql -U system -d test -p {PORT} "
             f"-c 'SELECT version()'\" 2>&1 | head -3")

    # 6) license 状态
    run(cli, "tail -3 /tmp/kb.log 2>/dev/null | grep -i license || echo 'license 无报错'")
    cli.close()
    print("完成。若 version() 输出正常，金仓已就绪。")


if __name__ == "__main__":
    main()
