$ErrorActionPreference = 'Stop'
$installer = (Get-ChildItem dist/*-setup.exe | Select-Object -First 1).FullName
$expectedVersion = python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"
$versionInfo = (Get-Item $installer).VersionInfo
if ($versionInfo.ProductVersion.Trim([char[]]@(0, 32)) -ne $expectedVersion) { throw "Incorrect installer ProductVersion: $($versionInfo.ProductVersion)" }
if ((@($versionInfo.FileMajorPart, $versionInfo.FileMinorPart, $versionInfo.FileBuildPart, $versionInfo.FilePrivatePart) -join '.') -ne "$expectedVersion.0") { throw "Incorrect installer FileVersion: $($versionInfo.FileVersion)" }
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
# An update must release the installed executable held by the tray automatically.
$tray = Start-Process -FilePath "$dest/guac-bridge.exe" -ArgumentList 'tray', '--background' -PassThru
Start-Sleep -Milliseconds 500
if ($tray.HasExited) { throw 'Tray failed to start for update test' }
Run-Setup '/TASKS=addtopath'
if (-not $tray.WaitForExit(15000)) { throw 'Installer did not stop the old tray before replacing binaries' }
Uninstall
if ([Environment]::GetEnvironmentVariable('Path','User').Split(';') -contains $dest) { throw 'Uninstall did not remove the PATH entry it added' }
Run-Setup '/MERGETASKS=!addtopath'
if ([Environment]::GetEnvironmentVariable('Path','User').Split(';') -contains $dest) { throw 'Installer added PATH despite disabled task' }
Uninstall
