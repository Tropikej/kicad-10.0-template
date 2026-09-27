<#
.SYNOPSIS
  Runs the KiBot pipeline locally (Windows) in the same docker image as the CI.

.DESCRIPTION
  Needs Docker Desktop running. All the arguments are forwarded to
  kibot_launch.sh, executed inside the container.

  .\run_kibot.ps1 [kibot_launch.sh OPTIONS]   Generate the outputs (see .\run_kibot.ps1 --help)
  .\run_kibot.ps1 --shell                     Open an interactive shell in the container
  .\run_kibot.ps1 --serve [PORT]              Browse the outputs on http://localhost:PORT
  .\run_kibot.ps1 --setup-libs                Add/update the shared libraries of
                                              kibot_settings.yaml as git submodules
  .\run_kibot.ps1 --update-libs               Update the libraries to the latest
                                              commit of their branch

  The image is read from kibot_settings.yaml (docker_image), it can be
  overridden with the KIBOT_IMAGE environment variable.

.EXAMPLE
  .\run_kibot.ps1 -v CHECKED -r blender
#>

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$settingsFile = Join-Path $PSScriptRoot 'kibot_settings.yaml'

# Read a `key: value` entry from kibot_settings.yaml
function Get-Setting([string]$Key) {
    $line = Select-String -Path $settingsFile -Pattern "^${Key}:\s*([^#\s]+)" | Select-Object -First 1
    if ($line) { return $line.Matches[0].Groups[1].Value.Trim('"', "'") }
    return ''
}

# Shared libraries: lib_<VAR>_url / lib_<VAR>_path / lib_<VAR>_branch
function Get-LibVars {
    Select-String -Path $settingsFile -Pattern '^lib_(\w+)_(url|path):' |
        ForEach-Object { $_.Matches[0].Groups[1].Value } | Select-Object -Unique
}
function Get-LibPath([string]$Var) {
    $path = Get-Setting "lib_${Var}_path"
    if (-not $path) {
        $url = Get-Setting "lib_${Var}_url"
        $path = 'lib/' + ([IO.Path]::GetFileName($url) -replace '\.git$', '')
    }
    return ($path -replace '^\./', '')
}
# Name of the git submodule using a path (empty if none)
function Get-SubmoduleName([string]$Path) {
    if (-not (Test-Path .gitmodules)) { return '' }
    foreach ($line in (git config -f .gitmodules --get-regexp '^submodule\..*\.path$')) {
        $key, $value = $line -split ' ', 2
        if ($value -eq $Path) { return ($key -replace '^submodule\.', '' -replace '\.path$', '') }
    }
    return ''
}

function Invoke-Git {
    & git @args
    if ($LASTEXITCODE -ne 0) { throw "git $args failed" }
}

function Set-Libs([bool]$Update) {
    $found = $false
    foreach ($var in Get-LibVars) {
        $url = Get-Setting "lib_${var}_url"
        $path = Get-LibPath $var
        $branch = Get-Setting "lib_${var}_branch"
        if (-not $url) { continue }  # Library already in the project, nothing to fetch
        $found = $true
        $name = Get-SubmoduleName $path
        if (-not $name) {
            Write-Host "Adding library `${$var}: $url -> $path"
            if ($branch) { Invoke-Git submodule add -b $branch -- $url $path }
            else { Invoke-Git submodule add -- $url $path }
            continue
        }
        $current = git config -f .gitmodules --get "submodule.$name.url"
        if ($current -ne $url) {
            Write-Host "Library `${$var}: URL changed to $url"
            Invoke-Git submodule set-url -- $path $url
        }
        if ($branch) { Invoke-Git submodule set-branch --branch $branch -- $path }
        if ($Update) {
            Write-Host "Updating library `${$var} ($path) to the latest commit"
            Invoke-Git submodule update --init --remote --recursive -- $path
        }
    }
    if (-not $found) {
        Write-Host 'No library with a lib_<VAR>_url entry in kibot_settings.yaml'
        return
    }
    if (-not $Update) {
        # Check out the commits recorded in the project (--update-libs moved them)
        Invoke-Git submodule update --init --recursive
    }
    Write-Host "`nDone. Commit the changes to use them in CI, i.e.:"
    Write-Host '  git add .gitmodules lib; git commit -m "Update libraries"'
}

switch ($args[0]) {
    '--setup-libs' { Set-Libs $false; exit 0 }
    '--update-libs' { Set-Libs $true; exit 0 }
}

$projectDir = $PSScriptRoot
# Nested docker (act, dev containers...): path of the project seen by the daemon
if ($env:KIBOT_PROJECT_HOST_DIR) { $projectDir = $env:KIBOT_PROJECT_HOST_DIR }
# Cache for the 3D models downloaded by KiBot: docker volume or host directory
$cache3d = if ($env:KIBOT_3D_CACHE_DIR) { $env:KIBOT_3D_CACHE_DIR } else { 'kibot_3d_models_cache' }

$image = if ($env:KIBOT_IMAGE) { $env:KIBOT_IMAGE } else { Get-Setting 'docker_image' }
if (-not $image) { throw 'docker_image not found in kibot_settings.yaml' }

$dockerArgs = @(
    'run', '--rm',
    '--volume', "${projectDir}:/project",
    '--workdir', '/project',
    '--volume', "${cache3d}:/root/.cache/kibot_3d_models",
    '--env', 'NO_AT_BRIDGE=1'
)
# Define the KiCad path variables of the shared libraries in the container
foreach ($var in Get-LibVars) {
    $path = Get-LibPath $var
    if (-not (Test-Path $path) -or -not (Get-ChildItem $path -Force | Select-Object -First 1)) {
        Write-Warning "library `${$var} missing in '$path'. Run: .\run_kibot.ps1 --setup-libs (or git submodule update --init --recursive)"
    }
    $dockerArgs += '--env', "$var=/project/$path"
}
if (-not [Console]::IsInputRedirected -and -not [Console]::IsOutputRedirected) {
    $dockerArgs += '-it'
}

switch ($args[0]) {
    '--shell' {
        & docker @dockerArgs --entrypoint /bin/bash $image
    }
    '--serve' {
        $port = if ($args.Count -gt 1) { $args[1] } else { 8000 }
        Write-Host "Serving the outputs on http://localhost:$port (Ctrl+C to stop)"
        & docker @dockerArgs -p "${port}:${port}" --entrypoint python3 $image -m http.server $port
    }
    default {
        & docker @dockerArgs --entrypoint /bin/bash $image ./kibot_launch.sh @args
    }
}
exit $LASTEXITCODE
