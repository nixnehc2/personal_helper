$ErrorActionPreference = "Continue"
$env:PYTHONIOENCODING = "utf-8"

# 项目根目录
$projectRoot = $PSScriptRoot
Set-Location $projectRoot

# 实验目录
$run = Join-Path $projectRoot "tmp\email-test"
$kb = Join-Path $run "memory"
$source = Join-Path $run "source"
$logs = Join-Path $run "logs"

# 创建日志目录
New-Item -ItemType Directory -Path $logs -Force | Out-Null

# 检查目录
if (-not (Test-Path $kb)) {
    Write-Host "ERROR: memory directory does not exist:"
    Write-Host $kb
    exit 1
}

if (-not (Test-Path $source)) {
    Write-Host "ERROR: source directory does not exist:"
    Write-Host $source
    exit 1
}

# 获取所有 eml
$emails = @(
    Get-ChildItem -Path $source -Filter "*.eml" -File |
    Sort-Object Name
)

if ($emails.Count -eq 0) {
    Write-Host "No .eml files found."
    exit 0
}

# 日志
$logFile = Join-Path $logs (
    "batch-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".txt"
)

Start-Transcript -Path $logFile

Write-Host ""
Write-Host "========================================"
Write-Host "Email batch ingest test"
Write-Host "========================================"
Write-Host "Memory:"
Write-Host $kb
Write-Host ""
Write-Host "Source:"
Write-Host $source
Write-Host ""
Write-Host "Email count: $($emails.Count)"
Write-Host "========================================"

$successCount = 0
$failedCount = 0
$index = 0

foreach ($email in $emails) {

    $index++

    Write-Host ""
    Write-Host "========================================"
    Write-Host "[$index/$($emails.Count)] $($email.Name)"
    Write-Host "========================================"

    try {

        & python -m agent.email_cli `
            --root $kb `
            ingest $email.FullName

        if ($LASTEXITCODE -eq 0) {

            $successCount++

            Write-Host ""
            Write-Host "[SUCCESS] $($email.Name)"

        }
        else {

            $failedCount++

            Write-Host ""
            Write-Host "[FAILED] $($email.Name)"
            Write-Host "Exit code: $LASTEXITCODE"
        }
    }
    catch {

        $failedCount++

        Write-Host ""
        Write-Host "[EXCEPTION] $($email.Name)"
        Write-Host $_.Exception.Message
    }
}

Write-Host ""
Write-Host "========================================"
Write-Host "Batch completed"
Write-Host "========================================"
Write-Host "Total:   $($emails.Count)"
Write-Host "Success: $successCount"
Write-Host "Failed:  $failedCount"
Write-Host ""
Write-Host "Memory directory:"
Write-Host $kb
Write-Host ""
Write-Host "Log:"
Write-Host $logFile
Write-Host "========================================"

Stop-Transcript