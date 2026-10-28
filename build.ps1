# 打包 DiskViz 为可分发的 exe（开源版，不做代码加密）
#   .\build.ps1
# 产物：dist\DiskViz.zip

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$PyInstaller = Join-Path $Root ".venv\Scripts\pyinstaller.exe"
if (-not (Test-Path $PyInstaller)) {
    $PyInstaller = "pyinstaller"   # 退回全局
}

$OutDir = Join-Path $Root "build_out"
if (Test-Path $OutDir) { Remove-Item -Recurse -Force $OutDir }
New-Item -ItemType Directory -Force $OutDir | Out-Null
New-Item -ItemType Directory -Force (Join-Path $OutDir "dist") | Out-Null

Write-Host "[1/3] 1_Scan.exe（请求管理员权限，MFT 需要）" -ForegroundColor Cyan
& $PyInstaller --noconfirm --onefile --windowed --uac-admin --name 1_Scan `
    --hidden-import psutil --hidden-import winreg --hidden-import sqlite3 `
    --distpath (Join-Path $OutDir "dist") --workpath (Join-Path $OutDir "work") `
    --specpath $OutDir `
    (Join-Path $Root "1_Scan.py")

Write-Host "[2/3] 2_AI.exe" -ForegroundColor Cyan
& $PyInstaller --noconfirm --onefile --windowed --name 2_AI `
    --hidden-import sqlite3 --hidden-import openai `
    --distpath (Join-Path $OutDir "dist") --workpath (Join-Path $OutDir "work") `
    --specpath $OutDir `
    (Join-Path $Root "2_AI.py")

Write-Host "[3/3] DiskViz.exe（主程序，同样请求管理员权限，避免子进程再弹 UAC）" -ForegroundColor Cyan
& $PyInstaller --noconfirm --onedir --windowed --uac-admin --name DiskViz `
    --icon (Join-Path $Root "DiskViz.png") `
    --add-data "$Root\static;static" `
    --add-data "$($OutDir)\dist\1_Scan.exe;." `
    --add-data "$($OutDir)\dist\2_AI.exe;." `
    --add-data "$Root\app_routes.py;." `
    --add-data "$Root\config.py;." `
    --add-data "$Root\mft.py;." `
    --add-data "$Root\DiskViz.png;." `
    --hidden-import flask --hidden-import psutil --hidden-import openai `
    --hidden-import winreg --hidden-import sqlite3 --hidden-import pystray `
    --hidden-import PIL --hidden-import PIL.Image --hidden-import PIL.ImageDraw `
    --distpath (Join-Path $OutDir "dist") --workpath (Join-Path $OutDir "work") `
    --specpath $OutDir `
    (Join-Path $Root "app.py")

$Exe = Join-Path $OutDir "dist\DiskViz\DiskViz.exe"
if (-not (Test-Path $Exe)) { throw "打包失败：找不到 $Exe" }

New-Item -ItemType Directory -Force (Join-Path $Root "dist") | Out-Null
$Zip = Join-Path $Root "dist\DiskViz.zip"
Compress-Archive -Path (Join-Path $OutDir "dist\DiskViz\*") -DestinationPath $Zip -Force

Write-Host ""
Write-Host "完成：$Zip" -ForegroundColor Green
Write-Host "（构建中间目录 build_out 可以删掉）" -ForegroundColor Gray
