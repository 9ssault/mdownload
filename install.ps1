<#
.SYNOPSIS
    mdownload - Windows setup.  Idempotent: re-run to upgrade in place.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1

.NOTES
    Creates a venv at %LOCALAPPDATA%\mdownload\venv, installs the package
    editable into it, and puts that venv's Scripts folder on the user PATH so
    `mdownload` works in new shells.
#>
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Say($m) { Write-Host $m }
function Die($m) { Write-Host "mdownload: $m" -ForegroundColor Red; exit 1 }

$venv = if ($env:MDOWNLOAD_VENV) { $env:MDOWNLOAD_VENV } else { Join-Path $env:LOCALAPPDATA "mdownload\venv" }
$vpy = Join-Path $venv "Scripts\python.exe"

function Find-Python {
    foreach ($c in @(@("py", "-3"), @("python"), @("python3"))) {
        $exe = $c[0]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $pre = @($c | Select-Object -Skip 1)
        try {
            $v = & $exe @pre -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $v) { continue }
            & $exe @pre -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" 2>$null
            if ($LASTEXITCODE -ne 0) { Die "python 3.11+ required, found $v ($exe)" }
            return ,@($exe, $pre)
        } catch { continue }
    }
    Die "no Python 3 found - install it with: winget install Python.Python.3.12"
}

if (-not (Test-Path $vpy)) {
    $py = Find-Python
    $exe = $py[0]; $rest = @($py | Select-Object -Skip 1)
    Say "==> venv $venv"
    & $exe @rest -m venv $venv
    if ($LASTEXITCODE -ne 0) { Die "venv creation failed" }
}

Say "==> installing mdownload"
& $vpy -m pip install --quiet --upgrade pip
& $vpy -m pip install --quiet --upgrade -e .
if ($LASTEXITCODE -ne 0) { Die "pip install failed" }

$scripts = Join-Path $venv "Scripts"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $userPath) { $userPath = "" }
if ($userPath -notlike "*$scripts*") {
    Say "==> adding $scripts to the user PATH"
    [Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(";") + ";" + $scripts).TrimStart(";"), "User")
    Say "    open a new terminal for 'mdownload'"
}
$env:Path = "$scripts;$env:Path"

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Say "    NOTE: ffmpeg missing - muxing audio fails without it"
    Say "      winget install --id Gyan.FFmpeg -e      (then open a new terminal)"
}

$js = @("deno", "node", "bun") | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue }
if (-not $js) {
    Say "    NOTE: no JavaScript runtime - YouTube returns images only"
    Say "      winget install --id OpenJS.NodeJS.LTS -e"
}

Say "==> self-test"
& (Join-Path $scripts "mdownload.exe") --self-test
if ($LASTEXITCODE -ne 0) { Die "--self-test failed" }
Say "done.  Run: mdownload"