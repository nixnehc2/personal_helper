<#
.SYNOPSIS
Test one email with the current Agent and shared Temporary Memory transaction.
.EXAMPLE
.\test_single_email.ps1
.EXAMPLE
.\test_single_email.ps1 -Number 1 -Reprocess
.EXAMPLE
.\test_single_email.ps1 -Email 'C:\mail\example.eml' -ContinueChat
#>
[CmdletBinding()]
param(
    [string]$Email,
    [ValidateRange(0, 100000)][int]$Number = 0,
    [string]$SourceDirectory = (Join-Path $PSScriptRoot 'tmp\email-test\source'),
    [string]$MemoryRoot = (Join-Path $PSScriptRoot 'tmp\email-test\memory'),
    [switch]$List,
    [switch]$Reprocess,
    [switch]$AuthoredByUser,
    [switch]$ContinueChat,
    [switch]$ParseOnly,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$exitCode = 0
$transcribing = $false
$pushed = $false
$oldEncoding = $env:PYTHONIOENCODING
try {
    if ($Email -and $Number -gt 0) { throw 'Use either -Email or -Number, not both.' }
    if ($List -and ($Email -or $Number -gt 0)) { throw '-List cannot be combined with an email selection.' }
    if ($ParseOnly -and ($Reprocess -or $AuthoredByUser -or $ContinueChat)) {
        throw '-ParseOnly cannot be combined with import or chat options.'
    }
    if (-not $Email) {
        $sourcePath = (Resolve-Path -LiteralPath $SourceDirectory).Path
        $emails = @(Get-ChildItem -LiteralPath $sourcePath -Filter '*.eml' -File | Sort-Object Name)
        if ($emails.Count -eq 0) { throw "No .eml files found in $sourcePath" }
        if ($List -or $Number -eq 0) {
            for ($i = 0; $i -lt $emails.Count; $i++) {
                Write-Host ('[{0}] {1}' -f ($i + 1), $emails[$i].Name)
            }
        }
        if ($List) { return }
        if ($Number -eq 0) {
            $selection = Read-Host 'Email number to import (blank cancels)'
            if ([string]::IsNullOrWhiteSpace($selection)) { return }
            if (-not [int]::TryParse($selection, [ref]$Number)) { throw 'Invalid email number.' }
        }
        if ($Number -lt 1 -or $Number -gt $emails.Count) { throw 'Email number is out of range.' }
        $emailPath = $emails[$Number - 1].FullName
    }
    else {
        # Resolve relative paths against the caller location, before changing directory.
        $emailPath = (Resolve-Path -LiteralPath $Email).Path
    }
    if (-not (Test-Path -LiteralPath $emailPath -PathType Leaf) -or [IO.Path]::GetExtension($emailPath) -ine '.eml') {
        throw 'Select an existing .eml file.'
    }
    $memoryPath = (Resolve-Path -LiteralPath $MemoryRoot).Path
    if (-not (Test-Path -LiteralPath (Join-Path $memoryPath 'AGENT.md') -PathType Leaf)) {
        throw 'Memory root must contain AGENT.md.'
    }
    $pythonArguments = @('-m', 'agent.email_cli')
    if ($ParseOnly) {
        $pythonArguments += @('parse', $emailPath)
    }
    else {
        $pythonArguments += @('--root', $memoryPath, 'ingest', $emailPath)
        if ($Reprocess) { $pythonArguments += '--reprocess' }
        if ($AuthoredByUser) { $pythonArguments += '--authored-by-user' }
    }
    Write-Host "Email:  $emailPath"
    Write-Host "Memory: $memoryPath"
    if ($DryRun) {
        Write-Host ('Dry run; no model call. Arguments: ' + ($pythonArguments | ConvertTo-Json -Compress))
        if ($ContinueChat) { Write-Host 'Then open agent.main with the same Memory root.' }
        return
    }
    Get-Command python -ErrorAction Stop | Out-Null
    $env:PYTHONIOENCODING = 'utf-8'
    Push-Location -LiteralPath $PSScriptRoot
    $pushed = $true
    $logs = Join-Path $PSScriptRoot 'tmp\email-test\logs'
    New-Item -ItemType Directory -Path $logs -Force | Out-Null
    $logFile = Join-Path $logs ('single-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '.txt')
    Start-Transcript -LiteralPath $logFile | Out-Null
    $transcribing = $true
    if (-not $ParseOnly) {
        Write-Host 'Import calls your configured model. Raw email is archived; derived Memory stays Temporary.'
        Write-Host 'Only type yes at the Runtime prompt to commit. no keeps Temporary; self/ has extra review.'
        Write-Host 'Already archived mail is skipped unless -Reprocess is supplied.'
    }
    # Keep stdin connected to the terminal for genuine user approval (no pipe or automatic yes).
    & python @pythonArguments
    $exitCode = $LASTEXITCODE
    if (-not $ParseOnly) {
        Write-Host "Import process exit code: $exitCode (zero does not mean Memory was committed)."
        if ($ContinueChat) {
            Write-Host 'Continue with feedback, ask to show/commit changes, or type /cancel to discard.'
            & python -m agent.main --root $memoryPath
            if ($LASTEXITCODE -ne 0) { $exitCode = $LASTEXITCODE }
        }
        else {
            Write-Host ('Continue later: python -m agent.main --root "' + $memoryPath + '"')
        }
    }
    Write-Host "Log: $logFile"
}
catch {
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    $exitCode = 1
}
finally {
    if ($transcribing) { Stop-Transcript | Out-Null }
    if ($pushed) { Pop-Location }
    $env:PYTHONIOENCODING = $oldEncoding
}
exit $exitCode
