param(
    [string]$OutputDirectory = (Join-Path $PSScriptRoot "runs\servers"),
    [int]$ContextLength = 8192,
    [int]$SinglePort = 11435,
    [int]$ReservedPort = 11436
)

$ErrorActionPreference = "Stop"

if ($ContextLength -lt 2048) {
    throw "ContextLength must be at least 2048"
}
if ($SinglePort -eq $ReservedPort) {
    throw "Dedicated ports must be distinct"
}

$occupied = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in @($SinglePort, $ReservedPort) }
if ($occupied) {
    $description = ($occupied | ForEach-Object { "$($_.LocalPort):pid=$($_.OwningProcess)" }) -join ", "
    throw "Refusing to replace occupied dedicated port(s): $description"
}

$ollama = (Get-Command ollama -ErrorAction Stop).Source
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$stamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")

function Start-HeraOllamaServer {
    param(
        [string]$Name,
        [int]$Port,
        [int]$Parallel
    )

    $environmentNames = @(
        "OLLAMA_HOST",
        "OLLAMA_NUM_PARALLEL",
        "OLLAMA_MAX_LOADED_MODELS",
        "OLLAMA_CONTEXT_LENGTH",
        "OLLAMA_KEEP_ALIVE",
        "OLLAMA_DEBUG"
    )
    $saved = @{}
    foreach ($environmentName in $environmentNames) {
        $item = Get-Item -Path "Env:$environmentName" -ErrorAction SilentlyContinue
        $saved[$environmentName] = if ($null -eq $item) { $null } else { $item.Value }
    }

    $stdout = Join-Path $OutputDirectory "$Name.$stamp.stdout.log"
    $stderr = Join-Path $OutputDirectory "$Name.$stamp.stderr.log"
    try {
        $env:OLLAMA_HOST = "127.0.0.1:$Port"
        $env:OLLAMA_NUM_PARALLEL = "$Parallel"
        $env:OLLAMA_MAX_LOADED_MODELS = "1"
        $env:OLLAMA_CONTEXT_LENGTH = "$ContextLength"
        $env:OLLAMA_KEEP_ALIVE = "30m"
        $env:OLLAMA_DEBUG = "1"
        $process = Start-Process -FilePath $ollama -ArgumentList @("serve") `
            -RedirectStandardOutput $stdout -RedirectStandardError $stderr `
            -WindowStyle Hidden -PassThru
    }
    finally {
        foreach ($environmentName in $environmentNames) {
            if ($null -eq $saved[$environmentName]) {
                Remove-Item -Path "Env:$environmentName" -ErrorAction SilentlyContinue
            }
            else {
                Set-Item -Path "Env:$environmentName" -Value $saved[$environmentName]
            }
        }
    }

    return [ordered]@{
        name = $Name
        pid = $process.Id
        host = "http://127.0.0.1:$Port"
        port = $Port
        parallel = $Parallel
        context_length = $ContextLength
        stdout = $stdout
        stderr = $stderr
    }
}

$single = Start-HeraOllamaServer -Name "single" -Port $SinglePort -Parallel 1
$reserved = Start-HeraOllamaServer -Name "reserved" -Port $ReservedPort -Parallel 2
$servers = @($single, $reserved)

foreach ($server in $servers) {
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    $ready = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        if (-not (Get-Process -Id $server.pid -ErrorAction SilentlyContinue)) {
            throw "$($server.name) Ollama server exited before becoming ready"
        }
        try {
            $version = Invoke-RestMethod -Uri "$($server.host)/api/version" -TimeoutSec 2
            $server.version = $version.version
            $ready = $true
            break
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not $ready) {
        throw "$($server.name) Ollama server did not become ready within 60 seconds"
    }
}

$state = [ordered]@{
    launched_utc = [DateTime]::UtcNow.ToString("o")
    executable = $ollama
    model_server_policy = "single=parallel1; reserved=parallel2"
    servers = $servers
}
$statePath = Join-Path $OutputDirectory "servers.current.json"
$state | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $statePath -Encoding UTF8
$state | ConvertTo-Json -Depth 6

