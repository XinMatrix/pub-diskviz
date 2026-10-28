@echo off
chcp 65001 >nul
echo ============================================
echo   DiskViz MFT 扫描自检
echo   需要管理员权限，否则会自动降级成目录遍历
echo ============================================
echo.
py -B "%~dp0bench_mft.py" C: 100
echo.
echo 上面如果显示 [mft]，说明 MFT 内核工作正常。
pause
