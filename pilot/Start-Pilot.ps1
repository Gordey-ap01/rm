[CmdletBinding()]
param(
    [switch]$NoBrowser
)

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
    # Do not let a developer shell select another Compose file, project, profile,
    # or implicit env file. The invocation below supplies all of those explicitly.
    $composeOverrides = @(
        "COMPOSE_FILE",
        "COMPOSE_PROJECT_NAME",
        "COMPOSE_PROFILES",
        "COMPOSE_ENV_FILES",
        "COMPOSE_DISABLE_ENV_FILE",
        # Shell variables otherwise take precedence over values from --env-file.
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

function New-RandomToken([int]$ByteCount = 32) {
    $bytes = New-Object byte[] $ByteCount
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }
    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function Read-PilotManifest([string]$ManifestPath) {
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        Fail "Pilot manifest is missing: $ManifestPath"
    }

    try {
        $manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    }
    catch {
        Fail "Pilot manifest is not valid JSON: $ManifestPath"
    }

    if ($manifest.format_version -ne 1) {
        Fail "Unsupported pilot manifest format. Expected format_version 1."
    }
    if ([string]$manifest.source_commit -notmatch "^[0-9a-f]{40}$") {
        Fail "Pilot manifest source_commit must be a full lowercase Git SHA-1."
    }
    if ([string]$manifest.project_name -notmatch "^rm-pilot-[0-9a-f]{12}$") {
        Fail "Pilot manifest project_name is invalid."
    }
    if ($manifest.project_name -ne ("rm-pilot-" + $manifest.source_commit.Substring(0, 12))) {
        Fail "Pilot manifest project_name does not match source_commit."
    }
    if ([string]$manifest.training_date -notmatch "^\d{4}-\d{2}-\d{2}$") {
        Fail "Pilot manifest training_date must be an ISO date."
    }
    try {
        [void][DateTime]::ParseExact(
            [string]$manifest.training_date,
            "yyyy-MM-dd",
            [Globalization.CultureInfo]::InvariantCulture
        )
    }
    catch {
        Fail "Pilot manifest training_date is not a real ISO date."
    }
    if ($manifest.application_url -ne "http://127.0.0.1:18000") {
        Fail "Pilot manifest application_url must be http://127.0.0.1:18000."
    }

    Test-PilotPackageIntegrity -Manifest $manifest -KitRoot $kitRoot

    return $manifest
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

function Ensure-PilotEnvironment([string]$EnvironmentPath, [string]$ImageTag) {
    if (Test-Path -LiteralPath $EnvironmentPath -PathType Leaf) {
        return
    }
    if (Test-Path -LiteralPath $EnvironmentPath) {
        Fail "Pilot environment path is not a file: $EnvironmentPath"
    }

    $contents = @(
        "RM_PILOT_DB_PASSWORD=$(New-RandomToken)",
        "RM_PILOT_PASSWORD=$(New-RandomToken)",
        "DJANGO_SECRET_KEY=$(New-RandomToken 48)",
        "PILOT_IMAGE_TAG=$ImageTag"
    ) -join [Environment]::NewLine
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($EnvironmentPath, $contents + [Environment]::NewLine, $utf8NoBom)
    Write-Host "Created local pilot credentials: $EnvironmentPath" -ForegroundColor Yellow
}

function Read-PilotEnvironment([string]$EnvironmentPath, [string]$ExpectedImageTag) {
    if (-not (Test-Path -LiteralPath $EnvironmentPath -PathType Leaf)) {
        Fail "Pilot environment file is missing: $EnvironmentPath"
    }

    $allowedNames = @("RM_PILOT_DB_PASSWORD", "RM_PILOT_PASSWORD", "DJANGO_SECRET_KEY", "PILOT_IMAGE_TAG")
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $EnvironmentPath) {
        if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith("#")) {
            continue
        }
        if ($line -notmatch "^([A-Z0-9_]+)=(.+)$") {
            Fail "Pilot environment file has an invalid line."
        }
        $name = $matches[1]
        if ($allowedNames -notcontains $name -or $values.ContainsKey($name)) {
            Fail "Pilot environment file has an unexpected or duplicate setting."
        }
        $values[$name] = $matches[2]
    }
    foreach ($name in $allowedNames) {
        if (-not $values.ContainsKey($name)) {
            Fail "Pilot environment file is missing $name."
        }
    }
    if ($values["PILOT_IMAGE_TAG"] -ne $ExpectedImageTag) {
        Fail "Pilot environment file belongs to a different source version."
    }
}

function Get-PilotContainerId([string[]]$ComposeBase, [string]$Service) {
    $containerId = (Invoke-PilotDocker -DockerArguments ($ComposeBase + @("ps", "--all", "-q", $Service))) | Select-Object -Last 1
    if ([string]::IsNullOrWhiteSpace($containerId)) {
        Fail "Docker Compose did not create the $Service container."
    }
    return $containerId.Trim()
}

function Wait-PilotHealth([string[]]$ComposeBase, [string]$Service, [int]$Attempts = 30) {
    $containerId = Get-PilotContainerId -ComposeBase $ComposeBase -Service $Service
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        $state = (Invoke-NativeDocker -DockerArguments @("inspect", "--format", "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}|{{.State.ExitCode}}", $containerId)) | Select-Object -Last 1
        $parts = $state.Trim().Split("|")
        if ($parts.Length -eq 3 -and $parts[1] -eq "healthy") {
            return
        }
        if ($parts.Length -ne 3 -or $parts[0] -eq "exited" -or $parts[1] -eq "unhealthy") {
            Fail "$Service did not become healthy (state: $state)."
        }
        Start-Sleep -Seconds 2
    }
    Fail "Timed out waiting for $Service to become healthy."
}

function Wait-PilotCompletion([string[]]$ComposeBase, [string]$Service, [int]$Attempts = 30) {
    $containerId = Get-PilotContainerId -ComposeBase $ComposeBase -Service $Service
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        $state = (Invoke-NativeDocker -DockerArguments @("inspect", "--format", "{{.State.Status}}|{{.State.ExitCode}}", $containerId)) | Select-Object -Last 1
        $parts = $state.Trim().Split("|")
        if ($parts.Length -eq 2 -and $parts[0] -eq "exited" -and $parts[1] -eq "0") {
            return
        }
        if ($parts.Length -ne 2 -or ($parts[0] -eq "exited" -and $parts[1] -ne "0")) {
            Fail "$Service failed while preparing pilot volumes (state: $state)."
        }
        Start-Sleep -Seconds 1
    }
    Fail "Timed out waiting for $Service to prepare pilot volumes."
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

if (-not (Test-Path -LiteralPath $composePath -PathType Leaf)) {
    Fail "Pilot Compose file is missing: $composePath"
}

$lockHandle = $null
try {
    try {
        $lockHandle = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    }
    catch [IO.IOException] {
        Fail "Another Start-Pilot.ps1 process is already preparing this kit."
    }

    $manifest = Read-PilotManifest -ManifestPath $manifestPath
    $imageTag = $manifest.source_commit.Substring(0, 12)
    Ensure-PilotEnvironment -EnvironmentPath $environmentPath -ImageTag $imageTag
    Read-PilotEnvironment -EnvironmentPath $environmentPath -ExpectedImageTag $imageTag

    Assert-LocalDockerDesktopContext
    Invoke-NativeDocker -DockerArguments @("version", "--format", "{{.Server.Version}}")
    Invoke-NativeDocker -DockerArguments @("compose", "version")

    Assert-PilotProjectOwner -ProjectName $manifest.project_name -KitRoot $kitRoot -ComposePath $composePath

    $composeBase = @("compose", "--env-file", $environmentPath, "--project-directory", $kitRoot, "-p", $manifest.project_name, "-f", $composePath)
    $appImage = "rm-pilot-web:$imageTag"
    $localImages = @(Invoke-NativeDocker -DockerArguments @("image", "ls", "--format", "{{.Repository}}:{{.Tag}}"))
    if ($localImages -notcontains "postgres:17") {
        Write-Host "postgres:17 is not cached locally; Docker will download it now." -ForegroundColor Yellow
        Invoke-PilotDocker -DockerArguments ($composeBase + @("pull", "db"))
    }
    if ($localImages -notcontains $appImage) {
        Write-Host "This source image is not cached. Building it may download python:3.12-slim and Python packages; complete this while Internet access is available before the visit." -ForegroundColor Yellow
        Invoke-PilotDocker -DockerArguments ($composeBase + @("build", "web"))
    }
    Invoke-PilotDocker -DockerArguments ($composeBase + @("up", "-d", "db"))
    Wait-PilotHealth -ComposeBase $composeBase -Service "db"
    Invoke-PilotDocker -DockerArguments ($composeBase + @("up", "-d", "volume-init"))
    Wait-PilotCompletion -ComposeBase $composeBase -Service "volume-init"

    $prepareCommand = "python manage.py migrate --noinput && python manage.py collectstatic --noinput && python manage.py seed_pilot --date $($manifest.training_date)"
    Invoke-PilotDocker -DockerArguments ($composeBase + @("run", "--rm", "--no-deps", "web", "sh", "-ec", $prepareCommand))
    Invoke-PilotDocker -DockerArguments ($composeBase + @("up", "-d", "web"))
    Wait-PilotHealth -ComposeBase $composeBase -Service "web"

    Write-Host "Pilot is running at $($manifest.application_url)" -ForegroundColor Green
    Write-Host "Credentials are stored locally in $environmentPath" -ForegroundColor Yellow
    if (-not $NoBrowser) {
        Start-Process -FilePath $manifest.application_url
    }
}
finally {
    if ($null -ne $lockHandle) {
        $lockHandle.Dispose()
    }
}
