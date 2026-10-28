# Build DiskViz into a distributable exe bundle (open-source version, no obfuscation)
#   .\build.ps1
# Output: dist\DiskViz.zip

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$PyInstaller = Join-Path $Root ".venv\Scripts\pyinstaller.exe"
if (-not (Test-Path $PyInstaller)) {
    # fall back to whatever pyinstaller is on PATH
    $PyInstaller = "pyinstaller"
}

$OutDir = Join-Path $Root "build_out"
if (Test-Path $OutDir) { Remove-Item -Recurse -Force $OutDir }
New-Item -ItemType Directory -Force $OutDir | Out-Null
New-Item -ItemType Directory -Force (Join-Path $OutDir "dist") | Out-Null

Write-Host "[1/3] 1_Scan.exe (UAC admin: MFT needs it)" -ForegroundColor Cyan
$s1 = @(
    "--noconfirm", "--onefile", "--windowed", "--uac-admin", "--name", "1_Scan",
    "--hidden-import", "psutil", "--hidden-import", "winreg", "--hidden-import", "sqlite3",
    "--distpath", (Join-Path $OutDir "dist"),
    "--workpath", (Join-Path $OutDir "work"),
    "--specpath", $OutDir,
    (Join-Path $Root "1_Scan.py")
)
& $PyInstaller @s1
if ($LASTEXITCODE -ne 0) { throw "1_Scan.exe build failed" }

Write-Host "[2/3] 2_AI.exe" -ForegroundColor Cyan
$s2 = @(
    "--noconfirm", "--onefile", "--windowed", "--name", "2_AI",
    "--hidden-import", "sqlite3", "--hidden-import", "openai",
    "--distpath", (Join-Path $OutDir "dist"),
    "--workpath", (Join-Path $OutDir "work"),
    "--specpath", $OutDir,
    (Join-Path $Root "2_AI.py")
)
& $PyInstaller @s2
if ($LASTEXITCODE -ne 0) { throw "2_AI.exe build failed" }

Write-Host "[3/3] DiskViz.exe (main app, UAC admin so children never re-prompt)" -ForegroundColor Cyan
$s3 = @(
    "--noconfirm", "--onedir", "--windowed", "--uac-admin", "--name", "DiskViz",
    "--icon", (Join-Path $Root "DiskViz.png"),
    "--add-data", "$Root\static;static",
    "--add-data", "$OutDir\dist\1_Scan.exe;.",
    "--add-data", "$OutDir\dist\2_AI.exe;.",
    "--add-data", "$Root\app_routes.py;.",
    "--add-data", "$Root\config.py;.",
    "--add-data", "$Root\mft.py;.",
    "--add-data", "$Root\DiskViz.png;.",
    "--hidden-import", "flask", "--hidden-import", "psutil", "--hidden-import", "openai",
    "--hidden-import", "winreg", "--hidden-import", "sqlite3", "--hidden-import", "pystray",
    "--hidden-import", "PIL", "--hidden-import", "PIL.Image", "--hidden-import", "PIL.ImageDraw",
    "--distpath", (Join-Path $OutDir "dist"),
    "--workpath", (Join-Path $OutDir "work"),
    "--specpath", $OutDir,
    (Join-Path $Root "app.py")
)
& $PyInstaller @s3
if ($LASTEXITCODE -ne 0) { throw "DiskViz.exe build failed" }

$Exe = Join-Path $OutDir "dist\DiskViz\DiskViz.exe"
if (-not (Test-Path $Exe)) { throw "Build failed: $Exe not found" }

New-Item -ItemType Directory -Force (Join-Path $Root "dist") | Out-Null
$Zip = Join-Path $Root "dist\DiskViz.zip"
Compress-Archive -Path (Join-Path $OutDir "dist\DiskViz\*") -DestinationPath $Zip -Force

Write-Host ""
Write-Host "Done: $Zip" -ForegroundColor Green
Write-Host "(build_out is intermediate, safe to delete)" -ForegroundColor Gray
