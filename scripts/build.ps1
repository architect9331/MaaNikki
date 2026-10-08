param(
    [ValidateSet('Check', 'Runtime', 'Build', 'Package')]
    [string]$Action = 'Package',
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$originalPath = $env:PATH
$originalRustupHome = $env:RUSTUP_HOME
$originalCargoHome = $env:CARGO_HOME

function Invoke-BuildStep {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Build step failed with exit code ${LASTEXITCODE}: $Program"
    }
}

Push-Location -LiteralPath $projectRoot
try {
    Invoke-BuildStep $Python @('-c', 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ is required on the build host"')
    Invoke-BuildStep $Python @('scripts/release.py', 'check')
    if ($Action -eq 'Check') { return }
    if ($Action -eq 'Runtime') {
        $runtimeOutput = '.build/runtime-' + [guid]::NewGuid().ToString('N')
        Invoke-BuildStep $Python @('scripts/release.py', 'prepare', '--output', $runtimeOutput)
        return
    }

    # Use a project-local toolchain when present; never change the user's system PATH.
    $localCargoBin = Join-Path $projectRoot '.build/cargo/bin'
    if (Test-Path -LiteralPath (Join-Path $localCargoBin 'cargo.exe')) {
        $env:RUSTUP_HOME = Join-Path $projectRoot '.build/rustup'
        $env:CARGO_HOME = Join-Path $projectRoot '.build/cargo'
        $env:PATH = $localCargoBin + ';' + $env:PATH
    }
    $expectedNode = 'v' + (Get-Content -LiteralPath '.node-version' -Raw).Trim()
    $actualNode = (& node --version).Trim()
    if ($LASTEXITCODE -ne 0 -or $actualNode -ne $expectedNode) {
        throw "Use Node.js $expectedNode for this build (found $actualNode)."
    }
    Invoke-BuildStep 'cargo' @('--version')
    Push-Location -LiteralPath (Join-Path $projectRoot 'client')
    try {
        Invoke-BuildStep 'npx.cmd' @('--yes', 'pnpm@10.28.0', 'install', '--frozen-lockfile')
        Invoke-BuildStep 'npx.cmd' @('--yes', 'pnpm@10.28.0', 'build:app')
    } finally {
        Pop-Location
    }
    if ($Action -eq 'Package') {
        Invoke-BuildStep $Python @('scripts/release.py', 'package')
    }
} finally {
    $env:PATH = $originalPath
    $env:RUSTUP_HOME = $originalRustupHome
    $env:CARGO_HOME = $originalCargoHome
    Pop-Location
}
