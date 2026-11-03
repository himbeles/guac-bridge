# Build a private WinUI runtime payload for the Windows installer.
param([string]$Target)
$ErrorActionPreference = 'Stop'
Push-Location (Split-Path $PSScriptRoot)
try {
    $arguments = @('build', '--locked', '--release', '--features', 'bundled-ui',
        '--bin', 'guac-bridge', '--bin', 'guac-ssh-client', '--bin', 'guac-llm-client')
    $profile = 'target/release'
    if ($Target) {
        $arguments += @('--target', $Target)
        $profile = "target/$Target/release"
    }
    & cargo @arguments
    if ($LASTEXITCODE -ne 0) { throw 'Bundled UI build failed' }

    # Use a dedicated staging directory, excluding Cargo outputs and server helpers.
    $payload = 'target/installer-payload'
    if (Test-Path $payload) { Remove-Item $payload -Recurse -Force }
    New-Item -ItemType Directory -Force $payload | Out-Null
    foreach ($name in @('guac-bridge', 'guac-ssh-client', 'guac-llm-client')) {
        Copy-Item "$profile/$name.exe" $payload
    }
    Get-ChildItem $profile -File | Where-Object { $_.Extension -in '.dll', '.pri' } |
        Copy-Item -Destination $payload
    Get-ChildItem $profile -Directory | Where-Object {
        $_.Name -match '^[a-z]{2,3}(-[a-z0-9]+)+$' -or $_.Name -eq 'microsoft.ui.xaml'
    } | Copy-Item -Destination $payload -Recurse

    foreach ($name in @('Microsoft.WindowsAppRuntime.dll', 'Microsoft.UI.Xaml.dll',
        'Microsoft.UI.Windowing.dll', 'Microsoft.Web.WebView2.Core.dll', 'resources.pri')) {
        if (!(Test-Path "$payload/$name")) { throw "Missing bundled UI runtime file: $name" }
    }
    # Include the original Microsoft redistribution licenses with the installed files.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $cache = Join-Path $env:LOCALAPPDATA 'windows-reactor-setup/temp'
    foreach ($package in @('Microsoft.WindowsAppSDK.Runtime.2.4.0',
        'Microsoft.Web.WebView2.1.0.4078.44')) {
        $archive = [IO.Compression.ZipFile]::OpenRead((Join-Path $cache "$package.nupkg"))
        try {
            $entry = $archive.Entries | Where-Object {
                $_.FullName -match '(^|/)(license|license.txt|license.md)$'
            } | Select-Object -First 1
            if (!$entry) { throw "Missing redistribution license: $package" }
            [IO.Compression.ZipFileExtensions]::ExtractToFile(
                $entry, (Join-Path (Resolve-Path $payload) "$package-LICENSE.txt"), $true)
        } finally { $archive.Dispose() }
    }
    $version = (Select-String -Path Cargo.toml -Pattern '^version\s*=\s*"([^"]+)"').Matches.Groups[1].Value
    if (!$version) { throw 'Cannot read Cargo workspace version' }
    $iscc = "${env:ProgramFiles(x86)}/Inno Setup 6/ISCC.exe"
    if (!(Test-Path $iscc)) { throw 'Install Inno Setup 6 to build the installer.' }
    & $iscc "/DVersion=$version" installer/guac-bridge.iss
    if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed' }
    $setup = Get-Item "dist/guac-bridge-$version-windows-x86_64-setup.exe"
    $installedBytes = (Get-ChildItem $payload -Recurse -File | Measure-Object Length -Sum).Sum
    $baseline = 10349518 # Published v2.2.0 installer, before bundling.
    $report = [ordered]@{
        version = $version
        installer_bytes = $setup.Length
        previous_installer_bytes = $baseline
        increase_bytes = $setup.Length - $baseline
        installed_payload_bytes = $installedBytes
        runtime = 'private Windows App Runtime 2.4.0 x64'
    }
    $report | ConvertTo-Json | Set-Content 'dist/installer-size.json'
    $summary = "Installer: {0:N2} MB (v2.2.0: {1:N2} MB; increase: {2:N2} MB). Installed payload: {3:N2} MB." -f (
        $setup.Length / 1e6), ($baseline / 1e6), (($setup.Length - $baseline) / 1e6), ($installedBytes / 1e6)
    Write-Host $summary
    if ($env:GITHUB_STEP_SUMMARY) { $summary | Add-Content $env:GITHUB_STEP_SUMMARY }
} finally { Pop-Location }
