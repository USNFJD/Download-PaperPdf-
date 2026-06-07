$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$bundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$pythonCandidates = @(
    $env:PYTHON,
    $bundledPython,
    "python",
    "py"
) | Where-Object { $_ }

$python = $null
foreach ($candidate in $pythonCandidates) {
    try {
        if ((Test-Path $candidate) -or (Get-Command $candidate -ErrorAction SilentlyContinue)) {
            & $candidate --version | Out-Null
            $python = $candidate
            break
        }
    } catch {
        continue
    }
}

if (-not $python) {
    throw "No usable Python was found. Install Python 3.11+ or set PYTHON to python.exe."
}

if (-not (Test-Path ".venv")) {
    & $python -m venv .venv
}

function Stop-OldAppProcesses {
    $resolvedRoot = (Resolve-Path -LiteralPath $root).Path
    $escapedRoot = [regex]::Escape($resolvedRoot)
    $appScript = [regex]::Escape((Join-Path $root "app.py"))
    $edgeProfilePath = Join-Path $root "data\edge-webview-profile"
    $escapedEdgeProfile = [regex]::Escape($edgeProfilePath)

    function Get-AppProcessMatches {
        $allProcesses = @(Get-CimInstance Win32_Process)
        $matchedIds = New-Object 'System.Collections.Generic.HashSet[int]'

        foreach ($process in $allProcesses) {
            $commandLine = [string]$process.CommandLine
            if (-not $commandLine) {
                continue
            }
            $isAppPython = (
                $process.Name -match '^(python|pythonw)\.exe$' -and
                ($commandLine -match $escapedRoot -or $commandLine -match $appScript)
            )
            $isAppWebView = (
                $process.Name -match '^(msedge|msedgewebview2)\.exe$' -and
                ($commandLine -match $escapedEdgeProfile -or $commandLine -match $escapedRoot)
            )
            if ($isAppPython -or $isAppWebView) {
                [void]$matchedIds.Add([int]$process.ProcessId)
            }
        }

        do {
            $addedChild = $false
            foreach ($process in $allProcesses) {
                if ($matchedIds.Contains([int]$process.ParentProcessId) -and -not $matchedIds.Contains([int]$process.ProcessId)) {
                    [void]$matchedIds.Add([int]$process.ProcessId)
                    $addedChild = $true
                }
            }
        } while ($addedChild)

        $allProcesses |
            Where-Object { $matchedIds.Contains([int]$_.ProcessId) } |
            Sort-Object ProcessId -Unique
    }

    $targets = @(Get-AppProcessMatches)

    $profileProcesses = @(Get-CimInstance Win32_Process |
        Where-Object {
            $commandLine = $_.CommandLine
            $commandLine -and $_.Name -match '^(msedge|msedgewebview2)\.exe$' -and $commandLine -match $escapedEdgeProfile
        } |
        Sort-Object ProcessId -Unique)

    $targets = @($targets + $profileProcesses | Sort-Object ProcessId -Unique)

    if (-not $targets) {
        Remove-Item -LiteralPath (Join-Path $root "data\app.lock") -Force -ErrorAction SilentlyContinue
        return
    }

    foreach ($target in $targets) {
        try {
            & taskkill /PID $target.ProcessId /T /F *> $null
        } catch {
            Stop-Process -Id $target.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }

    $deadline = (Get-Date).AddSeconds(8)
    do {
        Start-Sleep -Milliseconds 250
        $remaining = @(Get-AppProcessMatches)
    } while ($remaining -and (Get-Date) -lt $deadline)

    if ($remaining) {
        $pids = ($remaining | ForEach-Object { $_.ProcessId }) -join ", "
        throw "Old desktop/backend processes did not exit cleanly: $pids"
    }

    Remove-Item -LiteralPath (Join-Path $root "data\app.lock") -Force -ErrorAction SilentlyContinue
}

Stop-OldAppProcesses

function Clear-WebViewCache {
    $profileRoot = Join-Path $root "data\edge-webview-profile\EBWebView"
    if (-not (Test-Path -LiteralPath $profileRoot)) {
        return
    }
    $resolvedProfile = (Resolve-Path -LiteralPath $profileRoot).Path
    $cachePaths = @(
        "Default\Cache",
        "Default\Code Cache",
        "Default\GPUCache",
        "Default\DawnGraphiteCache",
        "Default\DawnWebGPUCache",
        "Default\Service Worker\CacheStorage",
        "GrShaderCache",
        "GraphiteDawnCache",
        "ShaderCache"
    )
    foreach ($relativePath in $cachePaths) {
        $target = Join-Path $profileRoot $relativePath
        if (-not (Test-Path -LiteralPath $target)) {
            continue
        }
        $resolvedTarget = (Resolve-Path -LiteralPath $target).Path
        if ($resolvedTarget.StartsWith($resolvedProfile, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $resolvedTarget -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

Clear-WebViewCache

& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt

function Test-WebView2Runtime {
    $clientId = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    $paths = @(
        "HKCU:\Software\Microsoft\EdgeUpdate\Clients\$clientId",
        "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$clientId",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$clientId"
    )
    foreach ($path in $paths) {
        try {
            $item = Get-ItemProperty -Path $path -ErrorAction Stop
            if ($item.pv) {
                return $true
            }
        } catch {
            continue
        }
    }
    return $false
}

if (-not (Test-WebView2Runtime)) {
    Write-Host "Microsoft Edge WebView2 Runtime was not found. Installing..."
    $installer = Join-Path $env:TEMP "MicrosoftEdgeWebView2Setup.exe"
    try {
        Invoke-WebRequest "https://go.microsoft.com/fwlink/p/?LinkId=2124703" -OutFile $installer
        Start-Process -FilePath $installer -ArgumentList "/silent", "/install" -Wait -WindowStyle Hidden
    } catch {
        Write-Warning "WebView2 Runtime install failed. The app will fall back to your system browser if needed."
    }
}

& ".\.venv\Scripts\python.exe" app.py
