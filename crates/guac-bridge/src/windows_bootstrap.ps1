$ErrorActionPreference='Stop'
$e=[char]27;$b=[char]7
function S($m){$d=[Text.Encoding]::UTF8.GetBytes('@NONCE@:'+$m+"`n");for($j=0;$j -lt $d.Length;$j+=120){[Console]::Write("$e]482202;2;GBWB:"+[Convert]::ToBase64String($d,$j,[Math]::Min(120,$d.Length-$j))+"$b$e]482203;$b")}}
function H($p){$s=[Security.Cryptography.SHA256]::Create();try{([BitConverter]::ToString($s.ComputeHash([IO.File]::ReadAllBytes($p)))).Replace('-','').ToLowerInvariant()}finally{$s.Dispose()}}
$t=$null;$i=[IntPtr]::Zero;$o=[IntPtr]::Zero;[uint32]$im=0;[uint32]$om=0
try {
Add-Type 'using System;using System.Runtime.InteropServices;public class GBW{[DllImport("kernel32.dll",CharSet=CharSet.Unicode)]public static extern IntPtr CreateFile(string p,uint a,uint s,IntPtr z,uint c,uint f,IntPtr h);[DllImport("kernel32.dll")]public static extern bool GetConsoleMode(IntPtr h,out uint m);[DllImport("kernel32.dll")]public static extern bool SetConsoleMode(IntPtr h,uint m);[DllImport("kernel32.dll")]public static extern bool SetStdHandle(int n,IntPtr h);}'
$i=[GBW]::CreateFile('CONIN$',3221225472,3,[IntPtr]::Zero,3,0,[IntPtr]::Zero);$o=[GBW]::CreateFile('CONOUT$',3221225472,3,[IntPtr]::Zero,3,0,[IntPtr]::Zero)
if(![GBW]::GetConsoleMode($i,[ref]$im) -or ![GBW]::GetConsoleMode($o,[ref]$om)){throw 'An SSH console is required'}
if(![GBW]::SetConsoleMode($i,128) -or ![GBW]::SetConsoleMode($o,($om -bor 4))){throw 'Cannot configure console'}
[void][GBW]::SetStdHandle(-10,$i);[void][GBW]::SetStdHandle(-11,$o)
$w=New-Object IO.StreamWriter([Console]::OpenStandardOutput());$w.AutoFlush=$true;[Console]::SetOut($w)
S 'PREPARED'
if($env:PROCESSOR_ARCHITECTURE -ne 'AMD64'){throw 'Windows x86-64 is required'}
$h='@HASH@';$a=@MANUAL@
if(!$a){
$c=Join-Path $env:LOCALAPPDATA 'guac-bridge\helpers';[void][IO.Directory]::CreateDirectory($c);S ('LOCATION:'+$c)
$a=Join-Path $c ($h+'.exe')
if(([IO.File]::Exists($a)) -and (H $a) -eq $h){S 'CACHED'}else{
$t=Join-Path $c ('.upload.'+[Guid]::NewGuid().ToString('N'));S 'UPLOAD'
$d=New-Object byte[] @COUNT@;$f=[Console]::OpenStandardInput();$j=0
while($j -lt $d.Length){$n=$f.Read($d,$j,[Math]::Min(8192,$d.Length-$j));if($n -eq 0){throw 'Upload ended early'};$j+=$n}
[IO.File]::WriteAllBytes($t,[Convert]::FromBase64String([Text.Encoding]::ASCII.GetString($d)));S 'VERIFY'
if((H $t) -ne $h){throw 'Helper checksum mismatch'}
if([IO.File]::Exists($a)){[IO.File]::Replace($t,$a,$null)}else{[IO.File]::Move($t,$a)};$t=$null
}}
S 'STARTING'
& $a --stdio --windows-console @OPTIONS@
if($LASTEXITCODE -ne 0){throw ('Helper exited with code '+$LASTEXITCODE)}
}catch{S ('ERROR:'+$_.Exception.Message);exit 1}finally{
if($t){[IO.File]::Delete($t)}
if($i -ne [IntPtr]::Zero){[void][GBW]::SetConsoleMode($i,$im)}
if($o -ne [IntPtr]::Zero){[void][GBW]::SetConsoleMode($o,$om)}
}
exit 0
