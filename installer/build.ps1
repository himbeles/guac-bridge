# Build a private WinUI runtime payload for the Windows installer.
param([string]$Target)
$ErrorActionPreference = 'Stop'
Push-Location (Split-Path $PSScriptRoot)
try {
    $arguments = @('build', '--locked', '--release', '--features', 'bundled-ui',
        '--bin', 'guac-bridge')
    $binaryDirectory = 'target/release'
    if ($Target) {
        $arguments += @('--target', $Target)
        $binaryDirectory = "target/$Target/release"
    }
    & cargo @arguments
    if ($LASTEXITCODE -ne 0) { throw 'Bundled UI build failed' }

    # Use a dedicated staging directory, excluding Cargo outputs and server helpers.
    $payload = 'target/installer-payload'
    if (Test-Path $payload) { Remove-Item $payload -Recurse -Force }
    New-Item -ItemType Directory -Force $payload | Out-Null
    Copy-Item "$binaryDirectory/guac-bridge.exe" $payload
    Get-ChildItem $binaryDirectory -File | Where-Object { $_.Extension -in '.dll', '.pri' } |
        Copy-Item -Destination $payload
    Get-ChildItem $binaryDirectory -Directory | Where-Object {
        $_.Name -match '^[a-z]{2,3}(-[a-z0-9]+)+$' -or $_.Name -eq 'microsoft.ui.xaml'
    } | Copy-Item -Destination $payload -Recurse

    foreach ($name in @('Microsoft.WindowsAppRuntime.dll', 'Microsoft.UI.Xaml.dll',
        'Microsoft.UI.Windowing.dll', 'Microsoft.Web.WebView2.Core.dll', 'resources.pri')) {
        if (!(Test-Path "$payload/$name")) { throw "Missing bundled UI runtime file: $name" }
    }
    Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class GuacBuildManifest {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr LoadLibraryEx(string path, IntPtr reserved, uint flags);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr FindResource(IntPtr module, IntPtr name, IntPtr type);
    [DllImport("kernel32.dll")] public static extern IntPtr LoadResource(IntPtr module, IntPtr resource);
    [DllImport("kernel32.dll")] public static extern IntPtr LockResource(IntPtr resource);
    [DllImport("kernel32.dll")] public static extern uint SizeofResource(IntPtr module, IntPtr resource);
    [DllImport("kernel32.dll")] public static extern bool FreeLibrary(IntPtr module);
}
'@
    $module = [GuacBuildManifest]::LoadLibraryEx((Resolve-Path "$payload/guac-bridge.exe"), [IntPtr]::Zero, 2)
    if ($module -eq [IntPtr]::Zero) { throw 'Cannot inspect executable manifest' }
    try {
        $resource = [GuacBuildManifest]::FindResource($module, [IntPtr]1, [IntPtr]24)
        if ($resource -eq [IntPtr]::Zero) { throw 'Missing embedded application manifest' }
        $size = [GuacBuildManifest]::SizeofResource($module, $resource)
        $data = [GuacBuildManifest]::LockResource([GuacBuildManifest]::LoadResource($module, $resource))
        $bytes = New-Object byte[] $size
        [Runtime.InteropServices.Marshal]::Copy($data, $bytes, 0, $bytes.Length)
        $xml = [Text.Encoding]::UTF8.GetString($bytes).TrimEnd([char]0).TrimStart([char]0xFEFF)
        $xml | Set-Content target/installer-manifest.xml
        [xml]$document = $xml
        $description = $document.SelectSingleNode("//*[local-name()='description']")
        $classes = $document.SelectNodes("//*[local-name()='activatableClass']").Count
        Write-Host "Embedded runtime marker: $($description.InnerText); activation classes: $classes"
        if ($description.InnerText -ne 'windows-reactor-self-contained' -or $classes -eq 0) {
            throw 'The executable lost its private WinUI activation manifest'
        }
    } finally { [void][GuacBuildManifest]::FreeLibrary($module) }
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
