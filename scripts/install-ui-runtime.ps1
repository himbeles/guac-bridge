# CI prerequisite only. The runtime is never copied to a wheel, portable archive or installer.
$ErrorActionPreference = 'Stop'
$runtime = Get-AppxPackage -Name Microsoft.WindowsAppRuntime.2 |
    Where-Object { [version]$_.Version -ge [version]'2.4.0.0' }
if ($runtime) { Write-Host 'A compatible shared Windows App Runtime is installed.'; exit 0 }
$installer = Join-Path $env:RUNNER_TEMP 'WindowsAppRuntimeInstall-x64.exe'
Invoke-WebRequest 'https://aka.ms/windowsappsdk/2.5/2.5.1/windowsappruntimeinstall-x64.exe' -OutFile $installer
$signature = Get-AuthenticodeSignature $installer
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notlike '*Microsoft Corporation*') {
    throw 'Windows App Runtime installer signature verification failed'
}
$process = Start-Process $installer -ArgumentList '--quiet' -Wait -PassThru
if ($process.ExitCode -ne 0 -and $process.ExitCode -ne 3010) {
    throw "Windows App Runtime installation failed: $($process.ExitCode)"
}
$runtime = Get-AppxPackage -Name Microsoft.WindowsAppRuntime.2 |
    Where-Object { [version]$_.Version -ge [version]'2.4.0.0' }
if (-not $runtime) { throw 'The required Windows App Runtime framework package is still missing' }
