[CmdletBinding()]
param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$releaseRoot = Join-Path $projectRoot "release"
$iconPath = Join-Path $releaseRoot "smart-photo-triage.ico"
$distPath = Join-Path $releaseRoot "dist"
$workPath = Join-Path $releaseRoot "build"
$specPath = Join-Path $releaseRoot "spec"
$docsData = "$(Join-Path $projectRoot 'docs');docs"

Set-Location $projectRoot
New-Item -ItemType Directory -Force -Path $releaseRoot, $distPath, $workPath, $specPath | Out-Null
& $Python -m pip install "Pillow>=10.4,<13" "pywebview>=5.4,<7" pyinstaller
if ($LASTEXITCODE -ne 0) { throw "Unable to install desktop build dependencies." }
& $Python tools/create_windows_icon.py $iconPath
if ($LASTEXITCODE -ne 0) { throw "Unable to create the Windows icon." }
& $Python -m PyInstaller --noconfirm --onedir --windowed --name "Smart Photo Triage" --icon $iconPath --collect-submodules webview --add-data $docsData --distpath $distPath --workpath $workPath --specpath $specPath src/smart_photo_triage/desktop.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

Write-Host "Desktop application created: $(Join-Path $distPath 'Smart Photo Triage\Smart Photo Triage.exe')"
$nsis = "C:\Program Files (x86)\NSIS\makensis.exe"
if (Test-Path $nsis) {
    $installerName = "Smart-Photo-Triage-Setup-1.2.1-$(Get-Date -Format 'yyyyMMdd-HHmmss').exe"
    & $nsis "/DOUTPUT_FILE=$installerName" installer\SmartPhotoTriage.nsi
    if ($LASTEXITCODE -ne 0) { throw "NSIS installer build failed." }
    Write-Host "NSIS installer created: $(Join-Path $releaseRoot "installer\$installerName")"
} else {
    Write-Host "NSIS was not found. Build installer with installer\SmartPhotoTriage.nsi."
}
