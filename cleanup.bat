@echo off
setlocal
set "ANCHORBOT_CLEANUP_ROOT=%~dp0"
powershell.exe -NoProfile -Command ^
  "$ErrorActionPreference = 'Stop'; try {" ^
  "$root = [IO.Path]::GetFullPath($env:ANCHORBOT_CLEANUP_ROOT).TrimEnd([IO.Path]::DirectorySeparatorChar); $prefix = $root + [IO.Path]::DirectorySeparatorChar;" ^
  "if (!(Test-Path -LiteralPath (Join-Path $root 'anchorbot/launcher.py')) -or !(Test-Path -LiteralPath (Join-Path $root 'pyproject.toml'))) { throw 'Run cleanup.bat from the AnchorBot project folder.' };" ^
  "$pidFile = Join-Path $root 'app.pid'; if (Test-Path -LiteralPath $pidFile) { $botPid = (Get-Content -LiteralPath $pidFile -Raw).Trim(); if ($botPid -match '^\d+$' -and (Get-Process -Id ([int]$botPid) -ErrorAction SilentlyContinue)) { throw 'Stop the bot before running cleanup.' } };" ^
  "$venvPrefix = Join-Path $root '.venv\'; $active = Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($venvPrefix, [StringComparison]::OrdinalIgnoreCase) }; if ($active) { throw 'Close the bot and any programs using its virtual environment before running cleanup.' };" ^
  "function Remove-Generated([string]$relative) { $path = [IO.Path]::GetFullPath((Join-Path $root $relative)); if (!$path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Cleanup target is outside the project.' }; if (Test-Path -LiteralPath $path) { $resolved = (Resolve-Path -LiteralPath $path).Path; if (!$resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Resolved cleanup target is outside the project.' }; Remove-Item -LiteralPath $resolved -Recurse -Force; Write-Host ('Removed ' + $relative) } };" ^
  "foreach ($name in @('.convex', '.pytest_cache', '.ruff_cache', '.venv', 'node_modules', 'anchorbot.egg-info', 'build', 'dist', 'convex/_generated', '__pycache__')) { Remove-Generated $name };" ^
  "foreach ($name in @('anchorbot', 'tests', 'scripts')) { $folder = Join-Path $root $name; if (Test-Path -LiteralPath $folder) { $caches = @(Get-ChildItem -LiteralPath $folder -Directory -Filter '__pycache__' -Recurse -Force | Sort-Object { $_.FullName.Length } -Descending); foreach ($cache in $caches) { Remove-Generated ($cache.FullName.Substring($prefix.Length)) } } };" ^
  "foreach ($name in @('deploy', 'scripts')) { $folder = Join-Path $root $name; if ((Test-Path -LiteralPath $folder) -and !(Get-ChildItem -LiteralPath $folder -Force)) { Remove-Generated $name } };" ^
  "Write-Host 'Cleanup complete. Source, config, logs, and bot data were preserved. Run run.bat or run.sh to reinstall dependencies and start the bot.'; exit 0" ^
  "} catch { Write-Host ('Cleanup failed: ' + $_.Exception.Message); exit 1 }"
set "taskExitCode=%errorlevel%"
pause
exit /b %taskExitCode%
