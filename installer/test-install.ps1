param([switch]$WithoutSharedRuntime)
$ErrorActionPreference = 'Stop'
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class GuacInstallerNative {
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern IntPtr FindWindow(string className, string title);
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
}
'@
function Test-InstalledManager {
    $tray = Start-Process -FilePath "$dest/guac-bridge.exe" -ArgumentList 'tray' -PassThru
    try {
        $deadline = (Get-Date).AddSeconds(30)
        do {
            $title = 'Guac Bridge ' + [char]0x2014 + ' running bridges'
            $window = [GuacInstallerNative]::FindWindow($null, $title)
            if ($window -ne [IntPtr]::Zero) { break }
            if ($tray.HasExited) { throw 'Installed tray exited before the manager opened' }
            Start-Sleep -Milliseconds 100
        } while ((Get-Date) -lt $deadline)
        if ($window -eq [IntPtr]::Zero) {
            Get-Content "$env:LOCALAPPDATA/guac-bridge/data/manager.log" -ErrorAction SilentlyContinue
            throw 'Installed manager did not open without the shared runtime'
        }
        [uint32]$managerProcess = 0
        [void][GuacInstallerNative]::GetWindowThreadProcessId($window, [ref]$managerProcess)
        $xaml = (Get-Process -Id $managerProcess).Modules |
            Where-Object { $_.ModuleName -eq 'Microsoft.UI.Xaml.dll' }
        if (!$xaml -or $xaml.FileName -ne (Join-Path $dest 'Microsoft.UI.Xaml.dll')) {
            throw 'Manager did not load its private WinUI runtime from the install directory'
        }
    } finally {
        & "$dest/guac-bridge.exe" prepare-update
        if ($LASTEXITCODE -ne 0 -or !$tray.WaitForExit(20000)) { throw 'Cannot close installed manager and tray' }
    }
}

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
    if (Test-Path "$dest/Microsoft.WindowsAppRuntime.dll") { throw 'Uninstall left the private runtime behind' }
}
if ($WithoutSharedRuntime) {
    & target/release/guac-bridge.exe prepare-update
    if ($LASTEXITCODE -ne 0) { throw 'Cannot release running clients before runtime isolation' }
    Get-AppxPackage -Name Microsoft.WindowsAppRuntime.2 | ForEach-Object {
        Remove-AppxPackage -Package $_.PackageFullName -ErrorAction Stop
    }
    if (Get-AppxPackage -Name Microsoft.WindowsAppRuntime.2) { throw 'Shared UI runtime is still registered' }
}
Run-Setup '/TASKS=addtopath'
if (!(Test-Path "$dest/Microsoft.WindowsAppRuntime.dll")) { throw 'Installer omitted the private runtime' }
if ($WithoutSharedRuntime) { Test-InstalledManager }
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
