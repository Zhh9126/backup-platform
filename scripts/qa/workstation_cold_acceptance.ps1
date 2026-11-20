$ErrorActionPreference='Stop'
if(-not $env:AIDBM_VM_GUEST_PASSWORD) { throw 'AIDBM_VM_GUEST_PASSWORD required' }
$tool='D:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe'
$source='D:\CodexVMRecovery\vm-real-20261120\restored\backup.vmx'
$root='D:\CodexVMRecovery\vm-cold-20261120'
if(Test-Path $root) { throw 'Acceptance path already exists; refusing overwrite' }
New-Item -ItemType Directory -Path $root | Out-Null
$log=Join-Path $root 'acceptance.log'
function Note([string]$message) { ('{0:o} {1}' -f (Get-Date),$message)|Tee-Object -FilePath $log -Append }
function Native([string]$exe,[string[]]$arguments) {
 $result=& $exe @arguments 2>&1
 if($LASTEXITCODE -ne 0) { throw ($result -join ' ') }
 return $result
}
Note 'Preparing canary in the isolated first-round restored guest'
foreach($name in @('payload.bin','canary.db','expected.json')) {
 $hostFile=Join-Path $root $name
 Native 'C:\Windows\System32\OpenSSH\scp.exe' @('-o','BatchMode=yes',('root@192.168.109.128:/root/backup-platform-private/build/vm-real-20261120/'+$name),$hostFile)|Out-Null
 Native $tool @('-gu','root','-gp',$env:AIDBM_VM_GUEST_PASSWORD,'copyFileFromHostToGuest',$source,$hostFile,('/root/backup-platform-private/build/vm-real-20261120/'+$name))|Out-Null
}
Native $tool @('-gu','root','-gp',$env:AIDBM_VM_GUEST_PASSWORD,'runProgramInGuest',$source,'/bin/sync')|Out-Null
Note 'Guest data synced; cleanly shutting down the isolated test source'
Native $tool @('stop',$source,'soft')|Out-Null
$backup=Join-Path $root 'backup'
Note 'Creating standalone full clone of powered-off test VM'
Native $tool @('clone',$source,(Join-Path $backup 'backup.vmx'),'full','-cloneName=aidbm-cold-backup-20261120')|Out-Null
$archive=Join-Path $root 'vm-backup.tar.gz'
Note 'Archiving consistent full backup'
Native 'C:\Windows\System32\tar.exe' @('-czf',$archive,'-C',$backup,'.')|Out-Null
$hash=(Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
Note ('Backup SHA256 '+$hash)
Native 'C:\Windows\System32\OpenSSH\scp.exe' @('-o','BatchMode=yes',$archive,'root@192.168.109.128:/root/backup-platform-private/build/vm-real-20261120/vm-cold-backup.tar.gz')|Out-Null
Note 'Consistent VM backup stored in Linux repository'
$download=Join-Path $root 'downloaded.tar.gz'
Native 'C:\Windows\System32\OpenSSH\scp.exe' @('-o','BatchMode=yes','root@192.168.109.128:/root/backup-platform-private/build/vm-real-20261120/vm-cold-backup.tar.gz',$download)|Out-Null
if((Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash.ToLowerInvariant() -ne $hash) { throw 'Archive roundtrip mismatch' }
$restored=Join-Path $root 'restored'
New-Item -ItemType Directory -Path $restored | Out-Null
Note 'Extracting downloaded archive to a separate recovery directory'
Native 'C:\Windows\System32\tar.exe' @('-xzf',$download,'-C',$restored)|Out-Null
$target=Join-Path $restored 'backup.vmx'
$encoding=[Text.Encoding]::Default
$text=[IO.File]::ReadAllText($target,$encoding)
$text=[regex]::Replace($text,'(?m)^displayName\s*=.*','displayName = "aidbm-cold-restored-20261120"')
$text=[regex]::Replace($text,'(?m)^ethernet\d+\.startConnected\s*=.*\r?\n?','')
$text=[regex]::Replace($text,'(?m)^uuid.action\s*=.*\r?\n?','')
$text += "`r`nethernet0.startConnected = `"FALSE`"`r`nuuid.action = `"create`"`r`n"
[IO.File]::WriteAllText($target,$text,$encoding)
Note 'Booting isolated recovered VM'
Native $tool @('start',$target,'nogui')|Out-Null
$ready=$false
for($i=0;$i -lt 90;$i++) {
 $probe=& $tool -gu root -gp $env:AIDBM_VM_GUEST_PASSWORD fileExistsInGuest $target '/root/backup-platform-private/build/vm-real-20261120/expected.json' 2>&1
 if($LASTEXITCODE -eq 0) { $ready=$true;break }
 Start-Sleep -Seconds 3
}
if(-not $ready) { throw 'Restored guest did not boot in 270s' }
foreach($name in @('payload.bin','canary.db','expected.json')) {
 Native $tool @('-gu','root','-gp',$env:AIDBM_VM_GUEST_PASSWORD,'copyFileFromGuestToHost',$target,('/root/backup-platform-private/build/vm-real-20261120/'+$name),(Join-Path $root ('recovered-'+$name)))|Out-Null
}
$expected=Get-Content -Raw (Join-Path $root 'expected.json')|ConvertFrom-Json
foreach($name in @('payload.bin','canary.db')) {
 $actual=(Get-FileHash -LiteralPath (Join-Path $root ('recovered-'+$name)) -Algorithm SHA256).Hash.ToLowerInvariant()
 if($actual -ne $expected.files.$name) { throw ('Recovered guest checksum mismatch: '+$name) }
}
if((Get-FileHash -LiteralPath (Join-Path $root 'expected.json')).Hash -ne (Get-FileHash -LiteralPath (Join-Path $root 'recovered-expected.json')).Hash) { throw 'Manifest mismatch' }
Note 'Recovered random payload, SQLite database and manifest all match'
Native $tool @('stop',$target,'soft')|Out-Null
Note 'Recovered VM shut down cleanly'
$result=@{status='passed';test='vm-cold-20261120';consistency='clean-shutdown';archive_sha256=$hash;archive_bytes=(Get-Item $archive).Length;network_disconnected=$true;guest_booted=$true;files_verified=@('payload.bin','canary.db','expected.json');restored_vmx=$target;original_server_kept_running=$true;channel='Native VMware Workstation vmrun; platform Workstation adapter not available';finished=(Get-Date).ToString('o')}
$result|ConvertTo-Json -Depth 5|Set-Content -Encoding UTF8 (Join-Path $root 'result.json')
foreach($name in @('result.json','recovered-canary.db','acceptance.log')) {
 Native 'C:\Windows\System32\OpenSSH\scp.exe' @('-o','BatchMode=yes',(Join-Path $root $name),('root@192.168.109.128:/root/backup-platform-private/build/vm-real-20261120/cold-'+$name))|Out-Null
}
Note 'Consistent VM backup and recovery acceptance passed'
