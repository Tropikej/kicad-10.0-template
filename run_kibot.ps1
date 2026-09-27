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
  .\run_kibot.ps1 --diagrams [--fit]          Export Diagrams/*.drawio and import them in
                                              the schematic sheets

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

# Diagrams: export Diagrams/*.drawio to PNG with the draw.io exporter image
# (headless draw.io), then kibot_launch.sh --diagrams imports them in the
# sheets. The exporter sometimes hangs at start: time limit and retries.
if ($args[0] -eq '--diagrams') {
    $drawioImage = Get-Setting 'drawio_image'
    if (-not $drawioImage) { $drawioImage = 'rlespinasse/drawio-export:v4.60.0' }
    $boardsLine = Select-String -Path $settingsFile -Pattern '^boards:\s*([^#]*)' | Select-Object -First 1
    $boards = @()
    if ($boardsLine) { $boards = @($boardsLine.Matches[0].Groups[1].Value -split '[\s,]+' | Where-Object { $_ }) }
    if (-not $boards) { $boards = @('.') }
    foreach ($b in $boards) {
        if (-not (Get-ChildItem -Path (Join-Path $b 'Diagrams') -Filter '*.drawio' -ErrorAction SilentlyContinue)) { continue }
        Remove-Item -Recurse -Force (Join-Path $b 'Diagrams/export') -ErrorAction SilentlyContinue
        $ok = $false
        foreach ($try in 1..3) {
            & docker run --rm --volume "${projectDir}:/data" --workdir /data `
                --env DRAWIO_DESKTOP_COMMAND_TIMEOUT=60s $drawioImage `
                -f png --scale 3 --border 20 --transparent --remove-page-suffix "$b/Diagrams"
            if ($LASTEXITCODE -eq 0) { $ok = $true; break }
            Write-Warning "draw.io export failed or timed out (try $try/3)"
        }
        if (-not $ok) { throw "draw.io export of $b/Diagrams failed" }
    }
}
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
