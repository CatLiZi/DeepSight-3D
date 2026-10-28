$ErrorActionPreference = 'Stop'
$backupRoot = $PSScriptRoot
$projectRoot = [IO.Path]::GetFullPath((Join-Path $backupRoot '..\..'))
$target = [IO.Path]::GetFullPath((Join-Path $projectRoot 'src\locator_demo\web\static'))
$expected = [IO.Path]::GetFullPath('C:\Users\SLAI\Desktop\DeepSight-3D\positioning_robots\src\locator_demo\web\static')
if (-not $target.Equals($expected, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Restore target is outside this project.'
}
$manifest = Get-Content -LiteralPath (Join-Path $backupRoot 'manifest.json') -Raw | ConvertFrom-Json
foreach ($entry in $manifest.files.PSObject.Properties) {
    $file = Join-Path (Join-Path $backupRoot 'static') $entry.Name
    if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.Value) {
        throw ('Backup checksum mismatch: ' + $entry.Name)
    }
}
Get-ChildItem -LiteralPath (Join-Path $backupRoot 'static') | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $target -Recurse -Force
}
Write-Host 'R30 UI restored. Refresh the control page with Ctrl+F5.'
