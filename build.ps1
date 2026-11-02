# Windows x86-64: build both helpers and clients locally, with no Python or CI.
param([switch]$Check, [switch]$Installer)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
$oldLinker = $env:CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER
$oldRequire = $env:GUAC_BRIDGE_REQUIRE_WINDOWS_AGENT
function Invoke-Native([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE" }
}
try {
    if ($env:OS -ne 'Windows_NT') { throw 'Use make build on Linux.' }
    Invoke-Native rustup @('target','add','x86_64-unknown-linux-musl','x86_64-pc-windows-msvc')
    $env:CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER = 'rust-lld'
    Invoke-Native cargo @('build','--locked','--release','--no-default-features','--bin','guac-bridge-agent','--target','x86_64-unknown-linux-musl')
    New-Item -ItemType Directory -Force crates/guac-bridge/assets | Out-Null
    Copy-Item target/x86_64-unknown-linux-musl/release/guac-bridge-agent crates/guac-bridge/assets/guac-bridge-agent-linux-x86_64 -Force
    Invoke-Native cargo @('build','--locked','--release','--no-default-features','--bin','guac-bridge-agent','--target','x86_64-pc-windows-msvc')
    Copy-Item target/x86_64-pc-windows-msvc/release/guac-bridge-agent.exe crates/guac-bridge/assets/guac-bridge-agent-windows-x86_64.exe -Force
    $env:GUAC_BRIDGE_REQUIRE_WINDOWS_AGENT = '1'
    Invoke-Native cargo @('build','--locked','--release','--bins','--examples','--target','x86_64-pc-windows-msvc')
    # Existing installer/tests expect host binaries in target/release.
    New-Item -ItemType Directory -Force target/release | Out-Null
    foreach ($name in @('guac-bridge','guac-ssh-client','guac-llm-client','guac-bridge-agent')) {
        Copy-Item "target/x86_64-pc-windows-msvc/release/$name.exe" "target/release/$name.exe" -Force
    }
    New-Item -ItemType Directory -Force target/release/examples | Out-Null
    Copy-Item target/x86_64-pc-windows-msvc/release/examples/* target/release/examples -Recurse -Force
    if ($Check) {
        Invoke-Native cargo @('fmt','--all','--','--check')
        Invoke-Native cargo @('clippy','--locked','--all-targets','--target','x86_64-pc-windows-msvc','--','-D','warnings')
        Invoke-Native cargo @('test','--locked','--target','x86_64-pc-windows-msvc')
    }
    New-Item -ItemType Directory -Force dist | Out-Null
    $version = (Select-String -Path Cargo.toml -Pattern '^version\s*=\s*"([^"]+)"').Matches.Groups[1].Value
    if (!$version) { throw 'Cannot read Cargo workspace version' }
    Compress-Archive -Path target/release/guac-bridge.exe,target/release/guac-ssh-client.exe,target/release/guac-llm-client.exe,LICENSE,README.md -DestinationPath "dist/guac-bridge-$version-windows-x86_64.zip" -Force
    if ($Installer) {
        $iscc = "${env:ProgramFiles(x86)}/Inno Setup 6/ISCC.exe"
        if (!(Test-Path $iscc)) { throw 'Install Inno Setup 6 to build the installer.' }
        Invoke-Native $iscc @("/DVersion=$version",'installer/guac-bridge.iss')
    }
    Write-Host 'Build complete: target/release and dist'
} finally {
    $env:CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER = $oldLinker
    $env:GUAC_BRIDGE_REQUIRE_WINDOWS_AGENT = $oldRequire
    Pop-Location
}
