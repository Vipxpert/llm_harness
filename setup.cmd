:: # /llm_harness/setup.cmd
@echo off
setlocal EnableExtensions

title llm_harness setup
set "SOURCE_DIR=%~dp0"
set "TARGET_DIR=%USERPROFILE%\.llm_harness"
set "TEMP_DIR="
set "EXIT_CODE=0"

echo llm_harness setup
echo   source: %SOURCE_DIR%
echo   target: %TARGET_DIR%
echo.

for %%F in (harness.py workflow_commands.txt) do (
    if not exist "%SOURCE_DIR%%%F" (
        echo ERROR: %%F is missing. Put it in the same folder as this setup file: %SOURCE_DIR%
        goto :failed
    )
)

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%I"
if not defined STAMP (
    echo ERROR: PowerShell could not be started to read the date.
    goto :failed
)
set "TEMP_DIR=%TARGET_DIR%\.setup-tmp-%STAMP%"
set "BACKUP_DIR=%TARGET_DIR%\.backups\%STAMP%"

if not exist "%TARGET_DIR%" mkdir "%TARGET_DIR%"
if not exist "%TARGET_DIR%" (
    echo ERROR: Could not create %TARGET_DIR%
    goto :failed
)
mkdir "%TEMP_DIR%" 2>nul
if not exist "%TEMP_DIR%" (
    echo ERROR: Could not create the temporary folder %TEMP_DIR%
    goto :failed
)

copy /y "%SOURCE_DIR%harness.py" "%TEMP_DIR%\harness.py" >nul
if errorlevel 1 goto :failed
copy /y "%SOURCE_DIR%workflow_commands.txt" "%TEMP_DIR%\workflow_commands.txt" >nul
if errorlevel 1 goto :failed

set "EXISTING=0"
if exist "%TARGET_DIR%\harness.py" set "EXISTING=1"
if exist "%TARGET_DIR%\workflow_commands.txt" set "EXISTING=1"

echo.
echo Checking Codebase Memory (CBM)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $cbm=(Get-Command 'codebase-memory-mcp.exe' -ErrorAction SilentlyContinue).Source; if(-not $cbm){$candidate=Join-Path $env:LOCALAPPDATA 'Programs\codebase-memory-mcp\codebase-memory-mcp.exe'; if(Test-Path $candidate){$cbm=$candidate}}; try{$release=Invoke-RestMethod -Uri 'https://api.github.com/repos/DeusData/codebase-memory-mcp/releases/latest' -TimeoutSec 8; $latest=([string]$release.tag_name -replace '^v','')}catch{if($cbm){Write-Host 'CBM: GitHub unavailable or offline. Keeping the installed version.'}else{Write-Host 'CBM: GitHub unavailable or offline. Skipping CBM installation.'}; exit 0}; if($latest -notmatch '^\d+\.\d+\.\d+$'){Write-Host 'CBM: Latest version could not be identified. Leaving CBM unchanged.'; exit 0}; if($cbm){$output=(& $cbm --version 2>&1 | Out-String); $match=[regex]::Match($output,'\d+\.\d+\.\d+'); if(-not $match.Success){Write-Host 'CBM: Installed version could not be read safely. Leaving it unchanged.'; exit 0}; $installed=$match.Value; if([version]$installed -ge [version]$latest){Write-Host ('CBM: Installed version '+$installed+' is already at least latest '+$latest+'. Skipping.'); exit 0}; Write-Host ('CBM: Updating '+$installed+' to latest release '+$latest+'...')}else{Write-Host ('CBM: Not installed. Installing latest release '+$latest+'...')}; try{$installer=Join-Path '%TEMP_DIR%' 'cbm-install.ps1'; Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/main/install.ps1' -OutFile $installer -TimeoutSec 60; & $installer --skip-config; if($LASTEXITCODE -ne 0){Write-Warning 'CBM installer returned a failure code. Continuing with the existing CBM state.'}}catch{Write-Warning ('CBM installation/update failed: '+$_+'. Continuing with harness setup.')}"


if "%EXISTING%"=="1" (
    echo Existing harness installation detected. Upgrading...
    mkdir "%BACKUP_DIR%" 2>nul
    if errorlevel 1 goto :failed
    if exist "%TARGET_DIR%\harness.py" copy /y "%TARGET_DIR%\harness.py" "%BACKUP_DIR%\harness.py" >nul
    if errorlevel 1 goto :failed
    if exist "%TARGET_DIR%\workflow_commands.txt" copy /y "%TARGET_DIR%\workflow_commands.txt" "%BACKUP_DIR%\workflow_commands.txt" >nul
    if errorlevel 1 goto :failed
) else (
    echo No existing harness installation detected. Installing...
)

move /y "%TEMP_DIR%\harness.py" "%TARGET_DIR%\harness.py" >nul
if errorlevel 1 goto :failed
move /y "%TEMP_DIR%\workflow_commands.txt" "%TARGET_DIR%\workflow_commands.txt" >nul
if errorlevel 1 goto :failed
rd /s /q "%TEMP_DIR%" 2>nul

echo.
echo Setup successful.
echo Harness installed at: %TARGET_DIR%
if "%EXISTING%"=="1" echo Harness backup at: %BACKUP_DIR%
goto :end

:failed
set "EXIT_CODE=1"
echo.
echo ERROR: Harness setup failed. Read the messages above.
if defined TEMP_DIR if exist "%TEMP_DIR%" echo Temporary files may remain at: %TEMP_DIR%

:end
echo.
pause
exit /b %EXIT_CODE%
