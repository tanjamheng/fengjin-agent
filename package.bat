@echo off
cd /d "%~dp0"
title Build Fengjin AI Release

REM ============================================================
REM  镜像 — 仅在脚本内生效，无论是否翻墙都能快速下载
REM ============================================================
set ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
set ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/
set ELECTRON_CACHE=frontend\.cache\electron
if not exist "%ELECTRON_CACHE%" mkdir "%ELECTRON_CACHE%"

echo.
echo     =*= Fengjin AI - Release Builder =*=
echo.

REM ============================================================
REM  1. Build frontend
REM ============================================================
echo  [1/4] Building frontend...
cd frontend
call npm run build
if errorlevel 1 (
    echo    ERROR: Frontend build failed
    cd ..
    pause
    exit /b 1
)
cd ..
echo    OK

REM ============================================================
REM  2. Package Electron exe
REM ============================================================
echo  [2/4] Packaging Electron exe...
cd frontend

REM === 补丁：修复 electron-builder 捆绑 7za 的符号链接 bug       ===
REM === 只在未打补丁时执行；npm install 后自动重新打              ===
set SEVEN_DIR=node_modules\7zip-bin\win\x64
set SEVEN_INDEX=node_modules\7zip-bin\index.js
set BUILDER_UTIL=node_modules\builder-util\out\util.js

if not exist "%SEVEN_DIR%\7za.cmd" (
    echo    Patching 7za to handle symlink errors...

    REM 1. 重命名 7za.exe → 7za_real.exe
    if exist "%SEVEN_DIR%\7za.exe" rename "%SEVEN_DIR%\7za.exe" 7za_real.exe >nul

    REM 2. 创建 7za.cmd wrapper：调用真实 7za，并保留真实退出码
    echo @echo off> "%SEVEN_DIR%\7za.cmd"
    echo "%%~dp07za_real.exe" %%*>> "%SEVEN_DIR%\7za.cmd"
    echo exit /b %%ERRORLEVEL%%>> "%SEVEN_DIR%\7za.cmd"

    REM 3. 修改 7zip-bin 指向 .cmd
    powershell -NoProfile -Command ^
      "$txt = Get-Content '%SEVEN_INDEX%' -Raw;" ^
      "$txt = $txt -replace '7za\.exe', '7za.cmd';" ^
      "[System.IO.File]::WriteAllText('%SEVEN_INDEX%', $txt)" >nul 2>&1

    REM 4. 修改 builder-util 的 exec 函数：shell:true 跑 .cmd
    powershell -NoProfile -Command ^
      "$txt = Get-Content '%BUILDER_UTIL%' -Raw;" ^
      "if ($txt -notmatch 'needsShell') {" ^
      "  $old = 'maxBuffer: 1000 * 1024 * 1024,';" ^
      "  $new = 'shell: file.endsWith(''.cmd'') || file.endsWith(''.bat''), maxBuffer: 1000 * 1024 * 1024,';" ^
      "  $txt = $txt -replace [regex]::Escape($old), $new;" ^
      "  [System.IO.File]::WriteAllText('%BUILDER_UTIL%', $txt);" ^
      "}" >nul 2>&1

    REM 5. 删掉 chmod 调用（Windows 不需要且会报 ENOENT）
    powershell -NoProfile -Command ^
      "$f = 'node_modules\builder-util\out\7za.js';" ^
      "$txt = Get-Content $f -Raw;" ^
      "$txt = $txt -replace 'const fs_extra_1 = require\(\""fs-extra\""\);', '// patched';" ^
      "$txt = $txt -replace 'await \(0, fs_extra_1\.chmod\)\([^)]+\);', '// patched chmod';" ^
      "[System.IO.File]::WriteAllText($f, $txt)" >nul 2>&1

    echo    OK
)

call npx electron-builder --win portable
if errorlevel 1 (
    echo    ERROR: Electron packaging failed
    cd ..
    pause
    exit /b 1
)
cd ..
echo    OK

REM ============================================================
REM  3. Create distribution folder
REM ============================================================
echo  [3/4] Creating distribution folder...

REM Get version from package.json
for /f "tokens=2 delims=:," %%a in ('findstr "version" frontend\package.json') do (
    set VERSION=%%a
)
set VERSION=%VERSION:"=%
set VERSION=%VERSION: =%

set RELEASE_DIR=release\风堇AI-v%VERSION%
if exist "%RELEASE_DIR%" rmdir /s /q "%RELEASE_DIR%"
mkdir "%RELEASE_DIR%"

REM Find the exe
for /r "frontend\dist" %%f in (*.exe) do (
    copy "%%f" "%RELEASE_DIR%\风堇AI.exe" >nul
    echo    Copied: %%f
    goto :exe_done
)
:exe_done

REM Copy project files
echo    Copying project files...

xcopy "src" "%RELEASE_DIR%\src\" /E /I /Q >nul
xcopy "config" "%RELEASE_DIR%\config\" /E /I /Q >nul
copy "requirements.txt" "%RELEASE_DIR%\" >nul
copy ".env.example" "%RELEASE_DIR%\" >nul
copy "README.md" "%RELEASE_DIR%\" >nul 2>nul
copy "LICENSE" "%RELEASE_DIR%\" >nul 2>nul
copy "THIRD_PARTY_ASSETS.md" "%RELEASE_DIR%\" >nul 2>nul

REM Create empty directories
mkdir "%RELEASE_DIR%\models" 2>nul
mkdir "%RELEASE_DIR%\data\sessions" 2>nul
mkdir "%RELEASE_DIR%\data\chroma" 2>nul
mkdir "%RELEASE_DIR%\data\memory" 2>nul
mkdir "%RELEASE_DIR%\logs" 2>nul

echo    OK

REM ============================================================
REM  4. Package as portable zip
REM ============================================================
echo  [4/4] Creating zip...

powershell -Command "Compress-Archive -Path '%RELEASE_DIR%' -DestinationPath 'release\风堇AI-v%VERSION%-portable.zip' -Force"
if errorlevel 1 (
    echo    WARNING: Zip creation via PowerShell failed
    echo    The release folder is ready at: %RELEASE_DIR%
) else (
    echo    OK
)

echo.
echo    ====================================
echo    Release build complete!
echo.
echo    Folder: %RELEASE_DIR%
echo    Zip:    release\风堇AI-v%VERSION%-portable.zip
echo    ====================================
echo.
pause
