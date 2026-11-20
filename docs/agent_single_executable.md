# Agent 单文件客户端

本轮参考 NetBackup VxUpdate 的 Precheck、Stage、Install 部署阶段，增加平台侧二进制包管理、目标兼容性检查、分阶段部署、完整性校验、启动探测与升级失败回滚。平台继续提供 SSH 无 Agent 通道及可选 Agent 通道。

## 客户端文件布局

/opt/aidbm-agent 安装目录在成功安装后只保留 aidbm-agent 一个 ELF 可执行文件。安装过程不再下发 Python 源文件、loader 或外置加密载荷。运行时和 cryptography 已打入可执行文件，目标机无需预装 Python 或 pip 包。

系统配置分别由 /etc/aidbm-agent/agent.env 和 /etc/systemd/system/aidbm-agent.service 管理，权限为 0600。令牌通过环境配置读取，不放入 ExecStart 命令行。暂存包和回滚副本位于 /var/lib/aidbm-agent，成功部署后清理，失败时保留回滚副本便于排查。单文件程序运行时由打包运行库使用系统临时目录展开依赖。

“单文件”指安装目录中的应用交付形态。系统服务、配置、日志和运行时临时文件仍由操作系统管理。二进制交付不构成无法逆向分析的保证。

## 构建与离线交付

在与目标 ABI 兼容的 Linux 构建机安装 requirements-dev.txt，再执行以下命令。

```bash
.venv/bin/python scripts/build_agent.py
```

生成的文件位于 dist/agents/linux-x86_64，包含 aidbm-agent 以及平台保存的 manifest.json。清单记录版本、架构、SHA256、文件大小、构建机 glibc 和源文件摘要。构建脚本也可在 aarch64 构建机生成相应架构，但本轮实际构建和验证范围为 Linux x86_64、glibc 2.17。

平台页面从 GET /api/agents/packages 读取发布包，从受权限保护的 GET /api/agents/package 下载单文件。离线打包脚本会收集已有的 dist/agents。容器和单文件平台部署须将该目录一并交付，放到平台运行根目录下的 dist/agents。

## 部署流程

Agent 管理页面在选定纳管 SSH 主机和端口后，可选择预检、暂存或安装。API 为 POST /api/agents/remote-install，phase 分别为 precheck、stage、install，省略时执行 install。

1. 预检确认 Linux、root SSH、systemd、架构、glibc 和暂存空间，核对平台侧包摘要。
2. 暂存通过 SFTP 上传二进制并校验远端 SHA256，完整包可以复用。预检和暂存阶段均不创建 Agent 注册记录。
3. 安装备份旧二进制、环境配置和服务配置，原子替换二进制，启动后核对令牌、版本、单文件形态和 AES 能力。校验成功后启用注册记录、设置开机启动并清理已知旧脚本。
4. 启动或探活失败时恢复旧文件、服务启停状态、端口和注册状态。旧二进制恢复同样使用临时文件加 rename，避免 Text file busy。回滚异常会在响应中列出。

部署锁按目标 systemd machine-id 建立，同一平台中不同主机别名也不能并行覆盖同一个 Agent。阶段执行时间超过租约时会终止后续安装。

手工安装命令接口仍可使用，先下载二进制并复制到 /tmp/aidbm-agent，再执行返回的脚本。需要完整探活和回滚时使用远端安装流程。

## 执行器与传输改进

- Agent 默认最多处理 32 个并发连接，可通过 --max-connections 调整至 1..256。达到上限时返回 503，闲置连接设有超时。
- 同步命令持续排空 stdout 与 stderr，内存只保留有界前缀。命令超时回收进程组。
- 执行审计记录命令摘要，避免直接记录命令中的密码。
- 客户端普通请求使用连接锁，chunked 数据流独占连接，结束或异常后关闭。提前 EOF 会报错。
- 非只读请求遇到连接中断时不自动重放，避免命令已执行后再次执行。
- 下载续传核对本地前缀与新字节的完整摘要。旧文件或摘要不符时自动执行一次全新下载。旧版无指纹能力或超出指纹阈值的文件采用全新下载及大小检查。
- 修复随机令牌使用标准 Base64 时偶发产生非法字符的问题。新令牌统一使用 URL-safe Base64，更新注册信息也校验令牌、端口和通道类型。

JSON 请求响应支持 AES-256-GCM。现有文件流接口和令牌认证仍沿用原传输协议，本轮未新增 TLS 证书分发、双向 TLS 或完整的 NetBackup 证书生命周期。已注册客户端仍需具有相应数据库原生工具和目录访问权限。

## 验证记录

本轮 87 项 Agent、载荷兼容、接口权限、启动和翻译相关回归通过。Linux 开发服务器被用作一次性真实客户端，已验证 systemd 预检、暂存、安装、约 2 MiB 文件往返、加密命令执行、同版本重装和强制探活失败后的版本回滚。安装目录实测仅包含 aidbm-agent。

真实客户端验收脚本为 scripts/qa/agent_binary_e2e.py，使用 AIDBM_TEST_SSH_PASSWORD 环境变量，目标须没有已有客户端。测试后移除本次创建的服务和配置。证据分别保存在 build/agent-test/live/result.json 和 build/agent-test/regression/result.xml。
