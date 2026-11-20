$ErrorActionPreference = 'Stop'
if (-not $env:AIDBM_VM_GUEST_PASSWORD) { throw 'Set AIDBM_VM_GUEST_PASSWORD for guest read verification' }
$vmrun = 'D:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe'
$source = 'D:\Users\zhh\Documents\Virtual Machines\CentOS 7 64 位\CentOS 7 64 位.vmx'
$testRoot = 'D:\CodexVMRecovery\vm-real-20261120'
$snapshot = 'aidbm-real-backup-20261120'
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
$log = Join-Path $testRoot 'acceptance.log'
function Note([string]$message) {
  ('{0:o} {1}' -f (Get-Date), $message) | Tee-Object -FilePath $log -Append
}
function Native([string]$exe,[string[]]$arguments) {
  $result = & $exe @arguments 2>&1
  if ($LASTEXITCODE -ne 0) { throw ('Native command failed: ' + ($result -join ' ')) }
  return $result
}
$backupDir=Join-Path $testRoot 'backup'
$restoreDir=Join-Path $testRoot 'restored'
if ((Test-Path $backupDir) -or (Test-Path $restoreDir)) { throw 'Existing backup/restore directory; refusing to overwrite' }
Note 'Checking named live snapshot before copying its immutable base disks'
$snaps=Native $vmrun @('listSnapshots',$source)
if (($snaps -join ' ') -notmatch [regex]::Escape($snapshot)) { throw 'Named snapshot missing' }
$sourceDir=Split-Path $source
$sourceConfig=[IO.File]::ReadAllText($source,[Text.Encoding]::Default)
if($sourceConfig -notmatch 'scsi0:0.fileName = "([^"]+-000001.vmdk)"') { throw 'Source does not reference the expected active snapshot delta' }
$baseDescriptor=Join-Path $sourceDir 'CentOS 7 64 位.vmdk'
$descriptor=[IO.File]::ReadAllText($baseDescriptor,[Text.Encoding]::Default)
New-Item -ItemType Directory -Path $backupDir | Out-Null
foreach($m in [regex]::Matches($descriptor,'(?m)^RW [0-9]+ SPARSE "([^"]+)"')) {
  $extent=$m.Groups[1].Value
  if([IO.Path]::GetFileName($extent) -ne $extent) { throw 'Unexpected extent path' }
  Copy-Item -LiteralPath (Join-Path $sourceDir $extent) -Destination (Join-Path $backupDir $extent)
}
Copy-Item -LiteralPath $baseDescriptor -Destination $backupDir
Copy-Item -LiteralPath $source -Destination (Join-Path $backupDir 'backup.vmx')
Copy-Item -LiteralPath (Join-Path $sourceDir 'CentOS 7 64 位.nvram') -Destination $backupDir
$backupConfig=Join-Path $backupDir 'backup.vmx'
$configText=[IO.File]::ReadAllText($backupConfig,[Text.Encoding]::Default)
$configText=[regex]::Replace($configText,'(?m)^scsi0:0.fileName = .*','scsi0:0.fileName = "CentOS 7 64 位.vmdk"')
[IO.File]::WriteAllText($backupConfig,$configText,[Text.Encoding]::Default)
Note 'Immutable snapshot disk backup completed; active delta was excluded'
$vmx = Join-Path $backupDir 'backup.vmx'
$content=[IO.File]::ReadAllText($vmx,[Text.Encoding]::Default)
# Backup is never booted; restored image must start disconnected.
$content=[regex]::Replace($content,'(?m)^ethernet\d+\.startConnected\s*=.*\r?\n?','')
$content += "`r`nethernet0.startConnected = `"FALSE`"`r`n"
[IO.File]::WriteAllText($vmx,$content,[Text.Encoding]::Default)
Note 'Creating portable backup archive'
$archive=Join-Path $testRoot 'vm-backup.tar.gz'
Native 'C:\Windows\System32\tar.exe' @('-czf',$archive,'-C',$backupDir,'.') | Out-Null
$hash=(Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
Note ('Archive SHA256 '+$hash)
Native 'C:\Windows\System32\OpenSSH\scp.exe' @('-o','BatchMode=yes',$archive,'root@192.168.109.128:/root/backup-platform-private/build/vm-real-20261120/vm-backup.tar.gz') | Out-Null
Note 'Backup stored on Linux repository'
$roundtrip=Join-Path $testRoot 'downloaded-backup.tar.gz'
Native 'C:\Windows\System32\OpenSSH\scp.exe' @('-o','BatchMode=yes','root@192.168.109.128:/root/backup-platform-private/build/vm-real-20261120/vm-backup.tar.gz',$roundtrip) | Out-Null
if ((Get-FileHash -LiteralPath $roundtrip -Algorithm SHA256).Hash.ToLowerInvariant() -ne $hash) { throw 'Repository roundtrip checksum mismatch' }
New-Item -ItemType Directory -Path $restoreDir | Out-Null
Native 'C:\Windows\System32\tar.exe' @('-xzf',$roundtrip,'-C',$restoreDir) | Out-Null
$restoredVmx=Join-Path $restoreDir 'backup.vmx'
$content=[IO.File]::ReadAllText($restoredVmx,[Text.Encoding]::Default)
$content=[regex]::Replace($content,'(?m)^displayName\s*=.*','displayName = "aidbm-restored-20261120"')
$content=[regex]::Replace($content,'(?m)^ethernet\d+\.startConnected\s*=.*\r?\n?','')
$content += "`r`nethernet0.startConnected = `"FALSE`"`r`nuuid.action = `"create`"`r`n"
[IO.File]::WriteAllText($restoredVmx,$content,[Text.Encoding]::Default)
Note 'Starting restored VM with disconnected network'
Native $vmrun @('start',$restoredVmx,'nogui') | Out-Null
$ready=$false
for($i=0;$i -lt 90;$i++) {
  $result=& $vmrun -gu root -gp $env:AIDBM_VM_GUEST_PASSWORD fileExistsInGuest $restoredVmx '/root/backup-platform-private/build/vm-real-20261120/expected.json' 2>&1
  if($LASTEXITCODE -eq 0) { $ready=$true; break }
  Start-Sleep -Seconds 3
}
if(-not $ready) { throw 'Restored guest did not expose canary within 270 seconds' }
Note 'Restored guest booted; reading canary files through existing VMware Tools'
foreach($name in @('payload.bin','canary.db','expected.json')) {
  Native $vmrun @('-gu','root','-gp',$env:AIDBM_VM_GUEST_PASSWORD,'copyFileFromGuestToHost',$restoredVmx,('/root/backup-platform-private/build/vm-real-20261120/'+$name),(Join-Path $testRoot ('recovered-'+$name))) | Out-Null
}
$expected=Get-Content -Raw (Join-Path $testRoot 'recovered-expected.json') | ConvertFrom-Json
foreach($name in @('payload.bin','canary.db')) {
  if((Get-FileHash -LiteralPath (Join-Path $testRoot ('recovered-'+$name)) -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected.files.$name) { throw ('Recovered guest data checksum mismatch: '+$name) }
}
Note 'Both recovered guest data files match pre-backup SHA256'
Native $vmrun @('stop',$restoredVmx,'soft') | Out-Null
Note 'Restored VM shut down cleanly; original source remains running'
$result=@{test='vm-real-20261120';status='passed';source_ip='192.168.109.128';snapshot=$snapshot;backup_archive=$archive;sha256=$hash;restored_vmx=$restoredVmx;network_disconnected=$true;guest_booted=$true;canary_files_verified=$true;original_vm_kept_running=$true;validation_channel='native vmrun and existing VMware Tools; platform Workstation adapter unavailable';finished=(Get-Date).ToString('o')}
$result | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 (Join-Path $testRoot 'result.json')
Native 'C:\Windows\System32\OpenSSH\scp.exe' @('-o','BatchMode=yes',(Join-Path $testRoot 'result.json'),'root@192.168.109.128:/root/backup-platform-private/build/vm-real-20261120/result.json') | Out-Null
Note 'Real VM recovery acceptance passed'
