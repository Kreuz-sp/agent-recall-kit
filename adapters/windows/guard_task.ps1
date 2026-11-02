# 可选：Windows 计划任务，每 N 分钟跑一次 `she-memory-ctl ensure`。
#
# 安装 Python 包**不会**自动注册这个任务；必须显式执行本脚本。撤销也不会停掉正在跑的服务。
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File guard_task.ps1 `
#       -Action Install -ConfigPath D:\my-data\she-memory.json -EveryMinutes 10
#   powershell -NoProfile -ExecutionPolicy Bypass -File guard_task.ps1 -Action Show
#   powershell -NoProfile -ExecutionPolicy Bypass -File guard_task.ps1 -Action Remove
#
# 说明：这里只注册「每 N 分钟」的触发器。Windows 上「登录时触发」在普通权限下注册会报
# Access is denied（实测），所以不默认加；需要登录即起，可另在启动文件夹放一份快捷方式。
param(
    [ValidateSet('Install', 'Remove', 'Show', 'Run')]
    [string]$Action = 'Show',
    [string]$ConfigPath,
    [int]$EveryMinutes = 10,
    [string]$TaskName = 'SheMemoryService',
    [string]$Ctl = ''
)

if ($Action -ne 'Remove' -and -not $Ctl) {
    $found = Get-Command she-memory-ctl -ErrorAction SilentlyContinue
    if ($found) { $Ctl = $found.Source } else { $Ctl = 'python' }
}

switch ($Action) {
    'Remove' {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        Write-Output "已删除计划任务 $TaskName（正在运行的服务不受影响）"
    }
    'Run' {
        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        Write-Output "已触发 $TaskName"
    }
    'Show' {
        $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Output ("任务    : {0}{1}" -f $t.TaskPath, $t.TaskName)
        Write-Output ("状态    : {0}" -f $t.State)
        Write-Output ("上次    : {0} 结果 0x{1:X}" -f $info.LastRunTime, $info.LastTaskResult)
        Write-Output ("下次    : {0}" -f $info.NextRunTime)
        foreach ($a in $t.Actions) { Write-Output ("动作    : {0} {1}" -f $a.Execute, $a.Arguments) }
    }
    'Install' {
        if (-not $ConfigPath) { throw '需要 -ConfigPath <she-memory.json>' }
        if ($Ctl -eq 'python') {
            $action = New-ScheduledTaskAction -Execute 'python' `
                -Argument ('-X utf8 -m she_memory.tools.memctl ensure --config "{0}"' -f $ConfigPath)
        } else {
            $action = New-ScheduledTaskAction -Execute $Ctl -Argument ('ensure --config "{0}"' -f $ConfigPath)
        }
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
            -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
            -RepetitionDuration (New-TimeSpan -Days 3650)
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
        $user = '{0}\{1}' -f $env:USERDOMAIN, $env:USERNAME
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
            -User $user -RunLevel Limited -Force -ErrorAction Stop `
            -Description 'Memory service guard: run ensure every N minutes (no-op when healthy).' | Out-Null
        Write-Output ("已注册 {0}：每 {1} 分钟 ensure 一次，用户 {2}，不允许重叠" -f $TaskName, $EveryMinutes, $user)
    }
}
