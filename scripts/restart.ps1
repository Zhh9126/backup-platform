# 备份平台服务重启脚本（UTF-8）
$ErrorActionPreference = "SilentlyContinue"

# 停止旧服务
Get-NetTCPConnection -LocalPort 8080 | ForEach-Object {
    Stop-Process -Id $_.OwningProcess -Force
}
Start-Sleep -Seconds 2

# 启动新服务（依赖执行时工作目录为项目根，避免脚本内出现中文路径）
$root = $PWD.Path
Set-Location -LiteralPath $root
Start-Process python -ArgumentList "run.py" -WindowStyle Hidden -WorkingDirectory $root
