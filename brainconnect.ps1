# Convenience wrapper so `.\brainconnect ...` works from the repo root on
# Windows without activating the venv. Prefers the repo venv's console script.
$ErrorActionPreference = "Stop"
$venvBc = Join-Path $PSScriptRoot ".venv\Scripts\brainconnect.exe"
if (Test-Path $venvBc) {
    & $venvBc @args
} else {
    brainconnect @args
}
exit $LASTEXITCODE
