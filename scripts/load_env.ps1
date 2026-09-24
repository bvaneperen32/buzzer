<#
.SYNOPSIS
    Load the project's .env variables into the current PowerShell session.

.DESCRIPTION
    The Windows venv has no equivalent of the .env auto-loading that
    scripts/setup_venv.sh patches into the bash activate script, so Windows
    developers need to load .env explicitly. Run this once per terminal
    session, after activating .venv:

        .venv\Scripts\Activate.ps1
        .\scripts\load_env.ps1

    Variables are set for this session only; processes started from it
    (python, pytest, alembic) inherit them. Closing the terminal clears them.

.PARAMETER Path
    Path to the env file. Defaults to .env in the repository root.

.PARAMETER Quiet
    Suppress the summary of loaded variable names.

.NOTES
    Values are never printed — only variable names — so this is safe to run
    with a terminal shared on screen.
#>
[CmdletBinding()]
param(
    [string] $Path,
    [switch] $Quiet
)

$ErrorActionPreference = 'Stop'

if (-not $Path) {
    $repoRoot = Split-Path -Parent $PSScriptRoot
    $Path = Join-Path $repoRoot '.env'
}

if (-not (Test-Path -LiteralPath $Path)) {
    throw "No env file found at '$Path'. Copy .env.template to .env and fill it in first."
}

$loaded = @()
$lineNo = 0

foreach ($line in (Get-Content -LiteralPath $Path)) {
    $lineNo++

    # Skip blank lines and whole-line comments.
    if ($line -match '^\s*$' -or $line -match '^\s*#') { continue }

    # NAME=value, with an optional leading "export " for files shared with bash.
    if ($line -match '^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
        $name  = $matches[1]
        $value = $matches[2].Trim()

        # Strip one matched pair of surrounding quotes, if present. Without
        # this, ADMIN_PASSWORD="secret" would include the quote characters.
        if ($value.Length -ge 2 -and
            (($value.StartsWith('"')  -and $value.EndsWith('"')) -or
             ($value.StartsWith("'")  -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }

        Set-Item -Path "env:$name" -Value $value
        $loaded += $name
    }
    else {
        Write-Warning "Skipped ${Path}:${lineNo} - not a NAME=value line."
    }
}

if (-not $Quiet) {
    Write-Host "Loaded $($loaded.Count) variables from $Path" -ForegroundColor Green
    Write-Host ($loaded -join ', ') -ForegroundColor DarkGray
}

# Trailing '#' comments on a value line are NOT stripped: a value like
# ADMIN_PASSWORD=pa#ssword is legal, and silently truncating it would be far
# worse than keeping a stray comment. Put comments on their own line.
