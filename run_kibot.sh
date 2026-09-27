#!/usr/bin/env bash
# Runs the KiBot pipeline locally in the same docker image as the CI.
# Linux / macOS / WSL / Git Bash. On Windows PowerShell use run_kibot.ps1.
#
# USAGE
#   ./run_kibot.sh [kibot_launch.sh OPTIONS]   Generate the outputs (see ./run_kibot.sh --help)
#   ./run_kibot.sh --shell                     Open an interactive shell in the container
#   ./run_kibot.sh --serve [PORT]              Browse the outputs on http://localhost:PORT
#   ./run_kibot.sh --setup-libs                Add/update the shared libraries of
#                                              kibot_settings.yaml as git submodules
#   ./run_kibot.sh --update-libs               Update the libraries to the latest
#                                              commit of their branch
#   ./run_kibot.sh --check-libs                Fail if a library is missing (used by the CI)
#   ./run_kibot.sh --diagrams [--fit]          Export Diagrams/*.drawio and import them in
#                                              the schematic sheets
#
# The image is read from kibot_settings.yaml (docker_image), it can be
# overridden with the KIBOT_IMAGE environment variable.

set -e
cd "$(dirname "$0")"
project_dir="$(pwd)"
settings_file="kibot_settings.yaml"

# Read a `key: value` entry from kibot_settings.yaml
get_setting() {
    sed -n "s/^$1:[[:space:]]*\([^#[:space:]]*\).*/\1/p" "$settings_file" | head -n1 | sed "s/^[\"']//; s/[\"']\$//"
}

# Shared libraries: lib_<VAR>_url / lib_<VAR>_path / lib_<VAR>_branch
lib_vars() {
    sed -nE 's/^lib_([A-Za-z0-9_]*)_(url|path):.*/\1/p' "$settings_file" | awk '!seen[$0]++'
}
lib_path() {
    local path url
    path="$(get_setting "lib_$1_path")"
    if [[ -z "$path" ]]; then
        url="$(get_setting "lib_$1_url")"
        path="lib/$(basename "${url%.git}")"
    fi
    echo "${path#./}"
}
# Name of the git submodule using a path (empty if none)
submodule_name() {
    [[ -f .gitmodules ]] || return 0
    git config -f .gitmodules --get-regexp '^submodule\..*\.path$' | awk -v p="$1" '$2 == p { n=$1; sub(/^submodule\./, "", n); sub(/\.path$/, "", n); print n }'
}

setup_libs() {
    local update="$1" var url path branch name current found=false
    for var in $(lib_vars); do
        url="$(get_setting "lib_${var}_url")"
        path="$(lib_path "$var")"
        branch="$(get_setting "lib_${var}_branch")"
        [[ -z "$url" ]] && continue  # Library already in the project, nothing to fetch
        found=true
        name="$(submodule_name "$path")"
        if [[ -z "$name" ]]; then
            echo "Adding library \${$var}: $url -> $path"
            git submodule add ${branch:+-b "$branch"} -- "$url" "$path"
            continue
        fi
        current="$(git config -f .gitmodules --get "submodule.$name.url")"
        if [[ "$current" != "$url" ]]; then
            echo "Library \${$var}: URL changed to $url"
            git submodule set-url -- "$path" "$url"
        fi
        if [[ -n "$branch" ]]; then
            git submodule set-branch --branch "$branch" -- "$path"
        fi
        if [[ "$update" == true ]]; then
            echo "Updating library \${$var} ($path) to the latest commit${branch:+ of $branch}"
            git submodule update --init --remote --recursive -- "$path"
        fi
    done
    if [[ "$found" != true ]]; then
        echo "No library with a lib_<VAR>_url entry in $settings_file"
        return 0
    fi
    if [[ "$update" != true ]]; then
        # Check out the commits recorded in the project (--update-libs moved them)
        git submodule update --init --recursive
    fi
    echo
    echo "Done. Commit the changes to use them in CI, i.e.:"
    echo "  git add .gitmodules lib && git commit -m \"Update libraries\""
}

check_libs() {
    local var path missing=0
    for var in $(lib_vars); do
        path="$(lib_path "$var")"
        if [[ -z "$(ls -A "$path" 2>/dev/null)" ]]; then
            echo "Error: library \${$var} missing in '$path'." >&2
            if [[ -n "$(get_setting "lib_${var}_url")" && -z "$(submodule_name "$path")" ]]; then
                echo "       It is not a git submodule yet: run ./run_kibot.sh --setup-libs and commit." >&2
            fi
            missing=1
        else
            echo "Library \${$var}: $path"
        fi
    done
    return $missing
}

case "$1" in
    --setup-libs)  setup_libs false; exit ;;
    --update-libs) setup_libs true; exit ;;
    --check-libs)  check_libs; exit ;;
esac

# Git Bash / MSYS on Windows: give docker a Windows path and disable the
# automatic path conversion of the arguments
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
        project_dir="$(pwd -W)"
        export MSYS_NO_PATHCONV=1
        ;;
esac

# Nested docker (act, dev containers...): the path of the project as seen by
# the docker daemon can be given in KIBOT_PROJECT_HOST_DIR
project_dir="${KIBOT_PROJECT_HOST_DIR:-$project_dir}"

# Diagrams: export Diagrams/*.drawio to PNG with the draw.io exporter image
# (headless draw.io), then kibot_launch.sh --diagrams imports them in the
# sheets. The exporter sometimes hangs at start: time limit and retries.
if [[ "$1" == --diagrams ]]; then
    drawio_image="$(get_setting drawio_image)"
    drawio_image="${drawio_image:-rlespinasse/drawio-export:v4.60.0}"
    boards="$(sed -n 's/^boards:[[:space:]]*\([^#]*\).*/\1/p' "$settings_file" | head -n1 | tr ',' ' ')"
    for b in ${boards:-.}; do
        [[ -n "$(ls "$b"/Diagrams/*.drawio 2>/dev/null)" ]] || continue
        rm -rf "$b/Diagrams/export"
        ok=false
        for try in 1 2 3; do
            if docker run --rm --volume "$project_dir:/data" --workdir /data \
                    --env DRAWIO_DESKTOP_COMMAND_TIMEOUT=60s "$drawio_image" \
                    -f png --scale 3 --border 20 --transparent --remove-page-suffix "$b/Diagrams"; then
                ok=true
                break
            fi
            echo "draw.io export failed or timed out (try $try/3)" >&2
        done
        if [[ "$ok" != true ]]; then
            echo "Error: draw.io export of $b/Diagrams failed" >&2
            exit 1
        fi
    done
fi

image="${KIBOT_IMAGE:-$(get_setting docker_image)}"
if [[ -z "$image" ]]; then
    echo "Error: docker_image not found in $settings_file" >&2
    exit 1
fi

tty_flags=()
if [[ -t 0 && -t 1 ]]; then tty_flags=(-it); fi

# Cache for the 3D models downloaded by KiBot: a docker volume, or a host
# directory given in KIBOT_3D_CACHE_DIR (used by the CI to save the cache)
cache_3d="${KIBOT_3D_CACHE_DIR:-kibot_3d_models_cache}"
if [[ -n "$KIBOT_3D_CACHE_DIR" ]]; then mkdir -p "$KIBOT_3D_CACHE_DIR"; fi

docker_args=(
    run --rm "${tty_flags[@]}"
    --volume "$project_dir:/project"
    --workdir /project
    --volume "$cache_3d:/root/.cache/kibot_3d_models"
    # Give the generated files back to the current user
    --env HOST_UID="$(id -u)"
    --env HOST_GID="$(id -g)"
    --env NO_AT_BRIDGE=1
)

# Define the KiCad path variables of the shared libraries in the container
for var in $(lib_vars); do
    path="$(lib_path "$var")"
    if [[ -z "$(ls -A "$path" 2>/dev/null)" ]]; then
        echo "Warning: library \${$var} missing in '$path'." >&2
        echo "         Run: ./run_kibot.sh --setup-libs (or git submodule update --init --recursive)" >&2
    fi
    docker_args+=(--env "$var=/project/$path")
done

case "$1" in
    --shell)
        exec docker "${docker_args[@]}" --entrypoint /bin/bash "$image"
        ;;
    --serve)
        port="${2:-8000}"
        echo "Serving the outputs on http://localhost:$port (Ctrl+C to stop)"
        exec docker "${docker_args[@]}" -p "$port:$port" --entrypoint python3 "$image" -m http.server "$port"
        ;;
    *)
        exec docker "${docker_args[@]}" --entrypoint /bin/bash "$image" ./kibot_launch.sh "$@"
        ;;
esac
