param(
    [string]$OutputPath = "deployment.zip"
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$outputFullPath = if ([System.IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath
} else {
    Join-Path $repoRoot $OutputPath
}

$excludeDirs = @(
    ".git",
    ".azure",
    ".claude",
    ".pytest_cache",
    "__pycache__",
    "venv",
    ".venv",
    "node_modules",
    "logs"
)

$excludeFiles = @(
    ".env",
    "deployment.zip"
)

$stagingRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("azure-ai-caller-deploy-" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $stagingRoot | Out-Null

try {
    $repoUri = New-Object System.Uri(($repoRoot.TrimEnd('\') + '\'))
    Get-ChildItem -Path $repoRoot -Force -Recurse | ForEach-Object {
        $itemUri = New-Object System.Uri($_.FullName)
        $relativePath = [System.Uri]::UnescapeDataString($repoUri.MakeRelativeUri($itemUri).ToString()).Replace('/', '\')
        $segments = $relativePath -split '[\\/]'

        if ($segments | Where-Object { $excludeDirs -contains $_ }) {
            return
        }
        if (-not $_.PSIsContainer -and ($excludeFiles -contains $_.Name -or $_.Name -like "*.pyc")) {
            return
        }

        $destination = Join-Path $stagingRoot $relativePath
        if ($_.PSIsContainer) {
            New-Item -ItemType Directory -Path $destination -Force | Out-Null
        } else {
            $destinationDir = Split-Path -Parent $destination
            New-Item -ItemType Directory -Path $destinationDir -Force | Out-Null
            Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
        }
    }

    if (Test-Path -LiteralPath $outputFullPath) {
        Remove-Item -LiteralPath $outputFullPath -Force
    }

    Compress-Archive -Path (Join-Path $stagingRoot "*") -DestinationPath $outputFullPath -Force
    Write-Host "Created $outputFullPath"
} finally {
    if (Test-Path -LiteralPath $stagingRoot) {
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force
    }
}
