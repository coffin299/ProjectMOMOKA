# Host GUI 用 electron.exe の残留プロセスだけを停止する（他 Electron アプリは対象外）
$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$exe = Join-Path $root "node_modules\electron\dist\electron.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    exit 0
}
$target = (Resolve-Path -LiteralPath $exe).Path
Get-CimInstance Win32_Process -Filter "Name='electron.exe'" |
    Where-Object { $_.ExecutablePath -and ($_.ExecutablePath -ieq $target) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
exit 0
