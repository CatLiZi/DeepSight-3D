param([switch]$VerifyOnly)
$ErrorActionPreference = 'Stop'
$backupRoot = $PSScriptRoot
$projectRoot = [IO.Path]::GetFullPath((Join-Path $backupRoot '..\..'))
$target = [IO.Path]::GetFullPath((Join-Path $projectRoot 'src\locator_demo\web\static'))
if (-not $target.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or
    -not (Test-Path -LiteralPath (Join-Path $projectRoot 'run_server.py')) -or
    -not (Test-Path -LiteralPath $target -PathType Container)) {
    throw 'Restore target is outside this project.'
}
$manifest = Get-Content -LiteralPath (Join-Path $backupRoot 'manifest.json') -Raw | ConvertFrom-Json
foreach ($entry in $manifest.files.PSObject.Properties) {
    if ([IO.Path]::GetFileName($entry.Name) -ne $entry.Name) {
        throw ('Invalid backup filename: ' + $entry.Name)
    }
    $file = Join-Path (Join-Path $backupRoot 'static') $entry.Name
    if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.Value) {
        throw ('Backup checksum mismatch: ' + $entry.Name)
    }
}
if ($VerifyOnly) {
    Write-Host 'R30 UI backup checksums are valid. No files changed.'
    return
}
foreach ($entry in $manifest.files.PSObject.Properties) {
    $file = Join-Path (Join-Path $backupRoot 'static') $entry.Name
    Copy-Item -LiteralPath $file -Destination $target -Force
}
Write-Host 'R30 UI restored. Refresh the control page with Ctrl+F5.'
