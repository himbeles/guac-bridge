$ErrorActionPreference = 'Stop'
$installer = (Get-ChildItem dist/*-setup.exe | Select-Object -First 1).FullName
$dest = Join-Path $env:RUNNER_TEMP 'guac-installer-test'
function Run-Setup($tasks) {
    $process = Start-Process -FilePath $installer -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=`"$dest`"", $tasks) -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "Installer exit: $($process.ExitCode)" }
    & "$dest/guac-bridge.exe" --version
    if ($LASTEXITCODE -ne 0) { throw 'Installed executable failed' }
}
function Uninstall {
    $process = Start-Process -FilePath "$dest/unins000.exe" -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "Uninstaller exit: $($process.ExitCode)" }
}
Run-Setup '/TASKS=addtopath'
if (-not ([Environment]::GetEnvironmentVariable('Path','User').Split(';') -contains $dest)) { throw 'PATH option did not add the install directory' }
Uninstall
if ([Environment]::GetEnvironmentVariable('Path','User').Split(';') -contains $dest) { throw 'Uninstall did not remove the PATH entry it added' }
Run-Setup '/MERGETASKS=!addtopath'
if ([Environment]::GetEnvironmentVariable('Path','User').Split(';') -contains $dest) { throw 'Installer added PATH despite disabled task' }
Uninstall
