# Test run (DRY_RUN): 5 sample drafts, one per pillar. Nothing is published.
# The key is entered as hidden input and is kept only in this PowerShell session.
# Usage:  & .\scripts\run_samples.ps1        (add -Send to also send samples to the coordinator)
param([switch]$Send)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not $env:ANTHROPIC_API_KEY) {
    $secure = Read-Host "Anthropic API key (input is hidden)" -AsSecureString
    $env:ANTHROPIC_API_KEY = [System.Net.NetworkCredential]::new("", $secure).Password
}
if (-not $env:ANTHROPIC_API_KEY) { Write-Host "No key entered - stopping."; exit 2 }

$python = "python"
if (Test-Path ".\.venv\Scripts\python.exe") { $python = ".\.venv\Scripts\python.exe" }
elseif (Test-Path "C:\Program Files\LibreOffice\program\python.exe") {
    $python = "C:\Program Files\LibreOffice\program\python.exe"
    $env:PYTHONPATH = ".deps;."
}

$argsList = @("-m", "vpo_news", "samples")
if ($Send) { $argsList += "--send" }
& $python @argsList
Write-Host "Exit code: $LASTEXITCODE"
