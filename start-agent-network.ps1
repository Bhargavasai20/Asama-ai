$ErrorActionPreference = "Stop"
$pythonPath = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
if (-not (Test-Path $pythonPath)) {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $pythonPath = $pythonCommand.Source
    } else {
        throw "Python 3.10+ was not found. Install Python and restart PowerShell."
    }
}
$appPath = Join-Path $PSScriptRoot "agent-network\server.py"
& $pythonPath $appPath
