param(
    [Parameter(Mandatory = $true)]
    [string]$Manifest,

    [string]$OutputDir = ".\phase_linear_spatial_result",

    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectPython = Join-Path $Root ".venv-pc\Scripts\python.exe"
$Python = if (Test-Path -LiteralPath $ProjectPython) { $ProjectPython } else { "python" }
$Script = Join-Path $Root "scripts\phase_linear_spatial_experiment.py"

$Arguments = @(
    $Script,
    "--manifest", (Resolve-Path -LiteralPath $Manifest).Path,
    "--output-dir", $OutputDir
)
if ($CheckOnly) {
    $Arguments += "--check-only"
}

Write-Host "[python] $Python"
Write-Host "[manifest] $Manifest"
Write-Host "[output] $OutputDir"
& $Python @Arguments
if ($LASTEXITCODE -ne 0) {
    throw "Spatial phase-linear experiment failed with exit code $LASTEXITCODE"
}
