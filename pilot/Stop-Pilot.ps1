[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Fail([string]$Message) {
    throw $Message
}

function Invoke-NativeDocker([string[]]$DockerArguments) {
    $dockerOverrides = @("DOCKER_HOST", "DOCKER_CONTEXT")
    $previousValues = @{}
    foreach ($name in $dockerOverrides) {
        $previousValues[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
        [Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
    try {
        & docker --context desktop-linux @DockerArguments
        if ($LASTEXITCODE -ne 0) {
            Fail "Docker command failed (exit code $LASTEXITCODE): docker $($DockerArguments -join ' ')"
        }
    }
    finally {
        foreach ($name in $dockerOverrides) {
            [Environment]::SetEnvironmentVariable($name, $previousValues[$name], "Process")
        }
    }
}

function Invoke-PilotDocker([string[]]$DockerArguments) {
    $composeOverrides = @(
        "COMPOSE_FILE",
        "COMPOSE_PROJECT_NAME",
        "COMPOSE_PROFILES",
        "COMPOSE_ENV_FILES",
        "COMPOSE_DISABLE_ENV_FILE",
        "RM_PILOT_DB_PASSWORD",
        "RM_PILOT_PASSWORD",
        "DJANGO_SECRET_KEY",
        "PILOT_IMAGE_TAG"
    )
    $previousValues = @{}
    foreach ($name in $composeOverrides) {
        $previousValues[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
        [Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
    try {
        Invoke-NativeDocker -DockerArguments $DockerArguments
    }
    finally {
        foreach ($name in $composeOverrides) {
            [Environment]::SetEnvironmentVariable($name, $previousValues[$name], "Process")
        }
    }
}

function Test-PilotPackageIntegrity([object]$Manifest, [string]$KitRoot) {
    if ($null -eq $Manifest.file_sha256) {
        Fail "Pilot manifest is missing file_sha256."
    }

    $entries = @($Manifest.file_sha256.PSObject.Properties | Where-Object { $_.MemberType -eq "NoteProperty" })
    if ($entries.Count -eq 0) {
        Fail "Pilot manifest file_sha256 is empty."
    }

    $kitPrefix = $KitRoot.TrimEnd("\") + "\"
    foreach ($entry in $entries) {
        $relativePath = [string]$entry.Name
        $expectedHash = [string]$entry.Value
        if ([string]::IsNullOrWhiteSpace($relativePath) -or
            [IO.Path]::IsPathRooted($relativePath) -or
            $relativePath -match "(^|[\\/])\.\.?(?:[\\/]|$)") {
            Fail "Pilot manifest contains an unsafe file_sha256 path."
        }
        if ($expectedHash -notmatch "^[0-9a-f]{64}$") {
            Fail "Pilot manifest file_sha256 contains an invalid SHA-256 value."
        }

        $nativeRelativePath = $relativePath.Replace("/", "\")
        $candidatePath = [IO.Path]::GetFullPath((Join-Path $KitRoot $nativeRelativePath))
        if (-not $candidatePath.StartsWith($kitPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            Fail "Pilot manifest file_sha256 path resolves outside the kit."
        }

        $pathParts = $nativeRelativePath.Split("\")
        $checkedPath = $KitRoot
        for ($index = 0; $index -lt $pathParts.Length; $index++) {
            if ([string]::IsNullOrWhiteSpace($pathParts[$index])) {
                Fail "Pilot manifest file_sha256 contains an invalid path."
            }
            $checkedPath = Join-Path $checkedPath $pathParts[$index]
            if (-not (Test-Path -LiteralPath $checkedPath)) {
                Fail "Pilot package file is missing: $relativePath"
            }
            $item = Get-Item -LiteralPath $checkedPath -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                Fail "Pilot package file_sha256 cannot traverse a symbolic link: $relativePath"
            }
            if ($index -lt ($pathParts.Length - 1) -and -not $item.PSIsContainer) {
                Fail "Pilot manifest file_sha256 contains a non-directory path component."
            }
        }
        if ($item.PSIsContainer) {
            Fail "Pilot manifest file_sha256 entry must name a regular file: $relativePath"
        }

        $actualHash = (Get-FileHash -LiteralPath $candidatePath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $expectedHash) {
            Fail "Pilot package file hash does not match manifest: $relativePath"
        }
    }
}

function Test-SameResolvedPath([string]$ActualPath, [string]$ExpectedPath) {
    try {
        $actualResolved = (Resolve-Path -LiteralPath $ActualPath -ErrorAction Stop).Path
    }
    catch {
        return $false
    }
    return [string]::Equals(
        $actualResolved.TrimEnd("\"),
        $ExpectedPath.TrimEnd("\"),
        [StringComparison]::OrdinalIgnoreCase
    )
}

function Assert-PilotProjectOwner([string]$ProjectName, [string]$KitRoot, [string]$ComposePath) {
    $containerIds = @(Invoke-NativeDocker -DockerArguments @(
        "ps", "--all", "--filter", "label=com.docker.compose.project=$ProjectName", "--format", "{{.ID}}"
    ))
    $containerIds = @($containerIds | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | ForEach-Object { $_.Trim() })
    if ($containerIds.Count -eq 0) {
        $projectVolumes = @(Invoke-NativeDocker -DockerArguments @(
            "volume", "ls", "--filter", "label=com.docker.compose.project=$ProjectName", "--format", "{{.Name}}"
        ))
        if (@($projectVolumes | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count -gt 0) {
            Fail "Training volumes remain without containers; automatic recovery stopped to protect data, use the original kit and seek recovery support."
        }
        return
    }
    $inspectionJson = (@(Invoke-NativeDocker -DockerArguments (@("inspect") + $containerIds)) -join [Environment]::NewLine)
    try {
        $containers = @(ConvertFrom-Json -InputObject $inspectionJson | ForEach-Object { $_ })
    }
    catch {
        Fail "Docker returned invalid container inspection JSON."
    }
    if ($containers.Count -ne $containerIds.Count) {
        Fail "Docker returned an incomplete project container inspection."
    }
    foreach ($container in $containers) {
        $labels = $container.Config.Labels
        if ($null -eq $labels -or -not (Test-SameResolvedPath -ActualPath ([string]$labels.'com.docker.compose.project.working_dir') -ExpectedPath $KitRoot)) {
            Fail "Compose project $ProjectName belongs to another pilot kit; refusing to manage it."
        }
        $configFiles = @(([string]$labels.'com.docker.compose.project.config_files').Split(",") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        if ($configFiles.Count -ne 1 -or -not (Test-SameResolvedPath -ActualPath $configFiles[0].Trim() -ExpectedPath $ComposePath)) {
            Fail "Compose project $ProjectName uses another Compose file; refusing to manage it."
        }
    }
}

function Assert-LocalDockerDesktopContext {
    $contextJson = (@(Invoke-NativeDocker -DockerArguments @("context", "inspect", "desktop-linux")) -join [Environment]::NewLine)
    try {
        $contexts = @(ConvertFrom-Json -InputObject $contextJson | ForEach-Object { $_ })
    }
    catch {
        Fail "Docker returned invalid context inspection JSON."
    }
    if ($contexts.Count -ne 1 -or ([string]$contexts[0].Endpoints.docker.Host) -notmatch "^npipe:.*dockerDesktopLinuxEngine$") {
        Fail "The desktop-linux Docker context is not the local Docker Desktop Linux engine."
    }
}

$pilotRootItem = Get-Item -LiteralPath $PSScriptRoot -Force
if (($pilotRootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    Fail "Pilot scripts cannot run from a symbolic-link directory."
}
$pilotRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$kitRootItem = Get-Item -LiteralPath (Join-Path $pilotRoot "..") -Force
if (($kitRootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    Fail "Pilot kit cannot run from a symbolic-link directory."
}
$kitRoot = $kitRootItem.FullName
$manifestPath = Join-Path $kitRoot "pilot-manifest.json"
$composePath = Join-Path $pilotRoot "compose.yaml"
$environmentPath = Join-Path $pilotRoot ".pilot-local.env"
$lockPath = Join-Path $pilotRoot ".pilot-start.lock"

$lockHandle = $null
try {
    try {
        $lockHandle = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    }
    catch [IO.IOException] {
        Fail "Start-Pilot.ps1 is preparing this kit; wait for it before stopping the pilot."
    }

    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        Fail "Pilot manifest is missing: $manifestPath"
    }
    if (-not (Test-Path -LiteralPath $composePath -PathType Leaf)) {
        Fail "Pilot Compose file is missing: $composePath"
    }
    if (-not (Test-Path -LiteralPath $environmentPath -PathType Leaf)) {
        Fail "Pilot environment file is missing: $environmentPath"
    }

    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    }
    catch {
        Fail "Pilot manifest is not valid JSON: $manifestPath"
    }

    if ($manifest.format_version -ne 1 -or [string]$manifest.source_commit -notmatch "^[0-9a-f]{40}$") {
        Fail "Pilot manifest has an unsupported format or invalid source_commit."
    }
    if ($manifest.project_name -notmatch "^rm-pilot-[0-9a-f]{12}$" -or $manifest.project_name -ne ("rm-pilot-" + $manifest.source_commit.Substring(0, 12))) {
        Fail "Pilot manifest project_name does not match source_commit."
    }
    if ($manifest.application_url -ne "http://127.0.0.1:18000") {
        Fail "Pilot manifest application_url is invalid."
    }
    Test-PilotPackageIntegrity -Manifest $manifest -KitRoot $kitRoot

    Assert-LocalDockerDesktopContext
    Assert-PilotProjectOwner -ProjectName $manifest.project_name -KitRoot $kitRoot -ComposePath $composePath
    $composeBase = @("compose", "--env-file", $environmentPath, "--project-directory", $kitRoot, "-p", $manifest.project_name, "-f", $composePath)
    Invoke-PilotDocker -DockerArguments ($composeBase + @("stop"))
    Write-Host "Stopped pilot project $($manifest.project_name). Its PostgreSQL and application volumes were preserved." -ForegroundColor Green
}
finally {
    if ($null -ne $lockHandle) {
        $lockHandle.Dispose()
    }
}
