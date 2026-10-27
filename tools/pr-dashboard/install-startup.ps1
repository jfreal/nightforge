# Start the PR dashboard at every log on, with no window, and keep it running.
#
#   powershell -ExecutionPolicy Bypass -File install-startup.ps1              # install and start now
#   powershell -ExecutionPolicy Bypass -File install-startup.ps1 -Uninstall   # remove it
#
# It adds one Task Scheduler task for the current user only. The task runs supervise.py
# with pythonw.exe; supervise.py restarts the server when it crashes or its code changes.

param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'
$TaskName = 'Nightforge PR dashboard'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($Uninstall) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -like '*pr-dashboard*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Confirm:$false }
    Write-Host "Removed '$TaskName' and stopped the dashboard."
    return
}

$py = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if ($null -eq $py) { throw 'pythonw.exe is not on PATH. It comes with the python.org installer.' }

# A copy started by hand (run.cmd, python server.py) would hold the port; stop it first.
Get-CimInstance Win32_Process |
    Where-Object { $_.Name -in 'python.exe', 'pythonw.exe' -and $_.CommandLine -like '*pr-dashboard*' -and
                   ($_.CommandLine -like '*server.py*' -or $_.CommandLine -like '*supervise.py*') } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Confirm:$false; Write-Host "Stopped the copy already running (pid $($_.ProcessId))." }

$action = New-ScheduledTaskAction -Execute $py.Source -Argument "`"$Here\supervise.py`"" -WorkingDirectory $Here
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# No time limit, keep going on battery, never two copies, and come back if the task itself dies.
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'Keeps tools\pr-dashboard running at http://127.0.0.1:8765/' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Installed '$TaskName'. It is running now and starts at every log on."
Write-Host 'Open http://127.0.0.1:8765/  (log: tools\pr-dashboard\dashboard.log)'
