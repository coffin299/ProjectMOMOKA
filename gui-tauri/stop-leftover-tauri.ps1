# Host GUI 用 gui-tauri.exe の残留プロセスだけを停止する
$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$candidates = @(
    (Join-Path $root "src-tauri\target\release\gui-tauri.exe"),
    (Join-Path $root "src-tauri\target\debug\gui-tauri.exe")
)

Get-CimInstance Win32_Process -Filter "Name='gui-tauri.exe'" |
    ForEach-Object {
        $exePath = $_.ExecutablePath
        if (-not $exePath) { return }
        $resolved = [string]$exePath
        foreach ($target in $candidates) {
            if (-not (Test-Path -LiteralPath $target)) { continue }
            $want = (Resolve-Path -LiteralPath $target).Path
            if ($resolved.Equals($want, [System.StringComparison]::OrdinalIgnoreCase)) {
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
                break
            }
        }
    }
