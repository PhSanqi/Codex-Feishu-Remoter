param(
    [string]$OutputDirectory = 'desktop_dist'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root '.tmp\desktop-build-venv'
$out = if ([System.IO.Path]::IsPathRooted($OutputDirectory)) { $OutputDirectory } else { Join-Path $root $OutputDirectory }
$work = Join-Path $root '.tmp\pyinstaller'
$ui = Join-Path $root 'm3_control\dist'
$icon = Join-Path $root 'assets\cfr_icon_light.ico'
$protocol = Join-Path $root 'docs\reference\CFR_AND_BROKER_COEXISTENCE_ROUTING_CONTRACT_v1.2.md'
$constraints = Join-Path $root 'scripts\windows-desktop-constraints.txt'

if (-not (Test-Path $icon -PathType Leaf)) {
    throw "CFR desktop icon is missing: $icon"
}

Set-Location $root
Push-Location 'm3_control'
try {
    npm.cmd ci
    if ($LASTEXITCODE -ne 0) {
        throw "npm ci failed with exit code $LASTEXITCODE"
    }
    npm.cmd run build
    if ($LASTEXITCODE -ne 0) {
        throw "Control Center build failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

Remove-Item $venv -Recurse -Force -ErrorAction SilentlyContinue
python -m venv $venv
$python = "$venv\Scripts\python.exe"
& $python -m pip install --upgrade 'pip==26.2.1'
if ($LASTEXITCODE -ne 0) {
    throw "pip bootstrap failed with exit code $LASTEXITCODE"
}
& $python -m pip install -c $constraints --build-constraint $constraints -e '.[feishu,desktop]' pyinstaller
if ($LASTEXITCODE -ne 0) {
    throw "desktop dependency install failed with exit code $LASTEXITCODE"
}
& $python -m pip check
if ($LASTEXITCODE -ne 0) {
    throw "desktop dependency consistency check failed with exit code $LASTEXITCODE"
}

Remove-Item $out -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name CFR `
    --icon $icon `
    --specpath $work `
    --paths (Join-Path $root 'src') `
    --add-data "$ui;m3_control\dist" `
    --add-data "$protocol;docs\reference" `
    --collect-all webview `
    --collect-all keyring `
    --collect-all lark_channel `
    --collect-all lark_oapi `
    --copy-metadata lark-channel-sdk `
    --distpath $out `
    --workpath $work `
    (Join-Path $root 'scripts\run_cfr_desktop.py')

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

Write-Host "CFR Desktop EXE: $out\CFR.exe"
