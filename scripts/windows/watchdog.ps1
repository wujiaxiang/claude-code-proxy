# Watchdog：检测代理端口挂掉自动重启
# 用法：schtasks 每 2 分钟触发 powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -File watchdog.ps1
# 检测方式：.NET TcpClient 异步连接，2 秒超时（比 VBS COM 对象可靠）

$ErrorActionPreference = "Stop"
$port = 8082
$ip = "127.0.0.1"
$projectDir = "c:\Users\Administrator\claude-code-proxy-main"
$logFile = "$projectDir\watchdog.log"

function Log-Msg {
    param([string]$msg)
    $time = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$time $msg" | Out-File -FilePath $logFile -Append -Encoding UTF8
}

# 检测端口：用 .NET TcpClient 异步连接，超时 2 秒
$portOpen = $false
$client = $null
try {
    $client = New-Object System.Net.Sockets.TcpClient
    $iar = $client.BeginConnect($ip, $port, $null, $null)
    $success = $iar.AsyncWaitHandle.WaitOne(2000)
    if ($success) {
        # EndConnect 在连接失败时会抛异常
        $client.EndConnect($iar)
        $portOpen = $true
    }
} catch {
    $portOpen = $false
} finally {
    if ($client -ne $null) {
        try { $client.Close() } catch {}
    }
}

if ($portOpen) {
    # 端口正常，静默退出
    exit 0
}

# 端口没监听，写日志并重启
Log-Msg "[WATCHDOG] proxy down (port $port not listening), restarting..."

# 调用 start_proxy.vbs 重启代理（vbs 隐藏窗口 + 调用 bat）
# 不能直接 Start-Process bat —— PowerShell Start-Process 调 bat 不工作
# wscript.exe start_proxy.vbs 是验证过最可靠的启动方式
Start-Process -FilePath "wscript.exe" -ArgumentList "$projectDir\start_proxy.vbs"

# 等待 25 秒后再次检测，确认重启成功
# 代理启动需调用 QClaw 上游做 startup diag，约需 10-15 秒，故等待 25 秒
Start-Sleep -Seconds 25
$portOpen2 = $false
try {
    $client2 = New-Object System.Net.Sockets.TcpClient
    $iar2 = $client2.BeginConnect($ip, $port, $null, $null)
    $success2 = $iar2.AsyncWaitHandle.WaitOne(2000)
    if ($success2) {
        $client2.EndConnect($iar2)
        $portOpen2 = $true
    }
    $client2.Close()
} catch {
    $portOpen2 = $false
}

if ($portOpen2) {
    Log-Msg "[WATCHDOG] proxy restarted successfully"
} else {
    Log-Msg "[WATCHDOG] proxy still down after restart attempt"
}
