# Create or replace the "HRS" task in Task Scheduler. Run it from the HRS
# folder, beside HRS.exe, signed in as the Windows account that will run HRS:
#
#     powershell -ExecutionPolicy Bypass -File .\install_task.ps1
#
# That account must have run HRS.exe --setup, because Credential Manager
# keeps secrets per account, and must stay signed in: the task runs only
# while it is, so Chrome has a screen (headless: false). A locked screen is
# fine.
#
# The task starts HRS.exe --email at -Start and again every -EveryMinutes
# until -Until. HRS itself waits for the email until graph.check_until, so
# the first start normally does the whole day; the later starts are skipped
# while it runs, and say "Already ran today" once it has finished. They only
# matter if a run stops early, for example on a network blip, so the next
# one tries again. Keep -Until equal to graph.check_until in config.yaml.

param(
    [string]$Start = "05:30",
    [string]$Until = "08:00",
    [int]$EveryMinutes = 10,
    [string]$TaskName = "HRS"
)

$ErrorActionPreference = "Stop"

$exe = Join-Path $PSScriptRoot "HRS.exe"
if (-not (Test-Path $exe)) {
    Write-Host "HRS.exe is not in this folder. Put this script beside HRS.exe and run it there."
    exit 1
}
if (-not (Test-Path (Join-Path $PSScriptRoot "config.yaml"))) {
    Write-Host "No config.yaml beside HRS.exe. Copy config.example.yaml to config.yaml and fill it in first."
    exit 1
}

$startTime = [datetime]::ParseExact($Start, "HH:mm", $null)
$untilTime = [datetime]::ParseExact($Until, "HH:mm", $null)
$window = $untilTime - $startTime
if ($window.TotalMinutes -lt $EveryMinutes) {
    Write-Host "-Until must be at least $EveryMinutes minutes after -Start."
    exit 1
}

$user = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute $exe -Argument "--email" -WorkingDirectory $PSScriptRoot

# A daily trigger cannot be given a repetition directly, so it borrows one
# from a one-time trigger. StopAtDurationEnd stays false: at -Until, a run
# still searching must be left to finish, not killed.
$trigger = New-ScheduledTaskTrigger -Daily -At $startTime
$repeat = New-ScheduledTaskTrigger -Once -At $startTime `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
    -RepetitionDuration $window
$trigger.Repetition = $repeat.Repetition
$trigger.Repetition.StopAtDurationEnd = $false

# Interactive: runs in the signed-in session, with no stored password.
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

# IgnoreNew: a start while HRS is still running is skipped, never queued.
# The time limit covers the wait for the email plus a long list of people.
# StartWhenAvailable: a computer that was off at -Start runs HRS when it is
# back; HRS then checks for today's email once and runs if it is there.
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ($window + (New-TimeSpan -Hours 2)) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -WakeToRun

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description "Searches CCIS for today's booking sheet. Runs HRS.exe --email." `
    -Force | Out-Null

Write-Host "Task '$TaskName' saved for $user."
Write-Host "It runs HRS.exe --email daily at $Start, then every $EveryMinutes minutes until $Until."
Write-Host "Set graph.check_until: `"$Until`" and graph.check_every_minutes: $EveryMinutes in config.yaml to match."
Write-Host "Try it now: Start-ScheduledTask -TaskName $TaskName"
