#!/usr/bin/env bash
# Runs KiBot for this project. Meant to be executed inside the KiBot docker
# image (KiCad 10), either:
#   - locally, through ./run_kibot.sh (Linux/macOS/WSL) or .\run_kibot.ps1 (Windows)
#   - in GitHub Actions (.github/workflows/ci.yaml)
# It can also be used on a host where KiCad 10 and KiBot are installed.

set -o pipefail

# ANSI color codes
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

cd "$(dirname "$0")" || exit 1

settings_file="kibot_settings.yaml"
kibot_config="kibot_yaml/kibot_main.yaml"
pid_file="/tmp/kibot_server.pid"

# Read a `key: value` entry from kibot_settings.yaml
get_setting() {
    sed -n "s/^$1:[[:space:]]*\([^#]*\).*/\1/p" "$settings_file" 2>/dev/null | head -n1 | sed 's/[[:space:]]*$//; s/^["'\'']//; s/["'\'']$//'
}

# Default options
variant="$(get_setting variant)"
variant="${variant:-CHECKED}"
revision=""
render_engine=""
costs_flag=false
check_flag=true
server_flag=false
server_port=8000
log_dir=""
stackup=""
force_flag=false
extra_args=()

function display_help() {
    echo -e "USAGE"
    echo -e "  ./kibot_launch.sh [OPTIONS] [-- EXTRA_KIBOT_ARGS]"
    echo
    echo -e "OPTIONS"
    echo -e "  -v, --variant VARIANT       Project status / variant: DRAFT, PRELIMINARY, CHECKED,"
    echo -e "                              RELEASED, or an assembly variant name."
    echo -e "                              Default: 'variant' in $settings_file (currently: $variant)."
    echo -e "  --version VERSION           Revision to print in the documents. Default: computed"
    echo -e "                              from CHANGELOG.md (e.g. '1.0.0+ (Unreleased)')."
    echo -e "  -r, --render ENGINE         3D render engine: kicad (fast) or blender (photo-realistic)."
    echo -e "                              Default: RENDER_ENGINE in $kibot_config."
    echo -e "  --costs                     Only generate the XLSX costs BoM (KiCost, needs API keys in"
    echo -e "                              kibot_yaml/kicost_config_local.yaml)."
    echo -e "  --log-dir DIR               Store the KiBot logs in DIR."
    echo -e "  --skip-checks               Don't run the manufacturing checks after the generation."
    echo -e "  --stackup NAME|list         Apply a stackup profile of kibot_resources/stackups to the PCB"
    echo -e "                              (copper layers count, names and physical stackup) and exit."
    echo -e "                              Close the board in KiCad first. 'list' shows the profiles."
    echo -e "  --force                     With --stackup: remove copper layers even if they are used."
    echo -e "  --server [PORT]             Start an HTTP server to browse the outputs (default: 8000)."
    echo -e "  --stop-server               Stop the running HTTP server."
    echo -e "  --init [OPTIONS]            Set the project metadata and rename the project files, asks"
    echo -e "                              when not given: --project, --board, --company, --designer,"
    echo -e "                              --name (KiCad files name), -y (don't ask)."
    echo -e "  -h, --help                  Display this help message."
    echo
    echo -e "RUNNER OPTIONS (run_kibot.sh / run_kibot.ps1 only)"
    echo -e "  --shell                     Interactive shell in the KiBot container."
    echo -e "  --serve [PORT]              Browse the outputs on http://localhost:PORT (default: 8000)."
    echo -e "  --setup-libs                Add/update the shared libraries (lib_<VAR>_url in"
    echo -e "                              $settings_file) as git submodules."
    echo -e "  --update-libs               Move the libraries to the latest commit of their branch."
    echo -e "  --check-libs                Fail if a declared library is missing (run_kibot.sh)."
    echo
    echo -e "EXAMPLES"
    echo -e "  ./kibot_launch.sh                        Run with the variant from $settings_file."
    echo -e "  ./kibot_launch.sh -v DRAFT               Schematic PDF, netlist and BoM only."
    echo -e "  ./kibot_launch.sh -v CHECKED -r blender  All outputs, ERC/DRC, Blender renders."
    echo -e "  ./kibot_launch.sh --costs                XLSX costs spreadsheet in Manufacturing/Assembly."
    echo -e "  ./kibot_launch.sh -v EXAMPLE             Assembly variant, outputs in Variants/."
    echo -e "  ./kibot_launch.sh --server 8080          Browse the outputs on http://localhost:8080."
    echo -e "  ./kibot_launch.sh --stackup jlcpcb_6l    Switch the PCB to the JLCPCB 6 layers stackup."
    echo
    echo -e "VARIANT DESCRIPTIONS"
    echo -e "  DRAFT:       only schematic in progress, generates schematic PDF, netlist and BoM"
    echo -e "  PRELIMINARY: generates both schematic and PCB documents, but no ERC/DRC"
    echo -e "  CHECKED:     generates both schematic and PCB documents, with ERC/DRC"
    echo -e "  RELEASED:    similar to CHECKED, used for releases (automatic on tags in CI)"
    echo -e "  Other:       assembly variants, run like RELEASED, outputs saved in Variants/"
    exit 0
}

# Project initialization: metadata and project files name
if [[ "$1" == --init ]]; then
    shift
    exec python3 kibot_resources/scripts/init_project.py "$@"
fi

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --variant|-v)
            if [[ -n $2 && $2 != -* ]]; then variant="$2"; shift
            else echo -e "${YELLOW}Warning: --variant|-v requires a value.${NC}"; exit 1; fi
            ;;
        --version)
            if [[ -n $2 && $2 != -* ]]; then revision="$2"; shift
            else echo -e "${YELLOW}Warning: --version requires a value.${NC}"; exit 1; fi
            ;;
        --render|-r)
            if [[ $2 == kicad || $2 == blender ]]; then render_engine="$2"; shift
            else echo -e "${YELLOW}Warning: --render|-r requires 'kicad' or 'blender'.${NC}"; exit 1; fi
            ;;
        --skip-checks)
            check_flag=false
            ;;
        --costs)
            costs_flag=true
            ;;
        --stackup)
            if [[ -n $2 && $2 != -* ]]; then stackup="$2"; shift
            else echo -e "${YELLOW}Warning: --stackup requires a profile name or 'list'.${NC}"; exit 1; fi
            ;;
        --force)
            force_flag=true
            ;;
        --log-dir)
            if [[ -n $2 && $2 != -* ]]; then log_dir="$2"; shift
            else echo -e "${YELLOW}Warning: --log-dir requires a value.${NC}"; exit 1; fi
            ;;
        --server)
            server_flag=true
            if [[ -n $2 && $2 != -* ]]; then server_port="$2"; shift; fi
            ;;
        --stop-server)
            if [[ -f $pid_file ]] && kill -0 "$(cat $pid_file)" 2>/dev/null; then
                echo -e "${GREEN}Stopping HTTP server with PID $(cat $pid_file)...${NC}"
                kill "$(cat $pid_file)"
                rm -f $pid_file
                exit 0
            fi
            echo -e "${YELLOW}No server is running.${NC}"
            rm -f $pid_file
            exit 1
            ;;
        -h|--help)
            display_help
            ;;
        --)
            shift
            extra_args=("$@")
            break
            ;;
        *)
            echo -e "${YELLOW}Warning: Unrecognized argument: $1${NC}"
            display_help
            ;;
    esac
    shift
done

# HTTP server to browse the outputs
if [[ "$server_flag" == true ]]; then
    if [[ -f $pid_file ]] && kill -0 "$(cat $pid_file)" 2>/dev/null; then
        echo -e "${YELLOW}A server is already running on PID $(cat $pid_file). Stop it first with --stop-server.${NC}"
        exit 1
    fi
    echo -e "${GREEN}Starting HTTP server on port $server_port...${NC}"
    python3 -m http.server "$server_port" &
    echo $! > $pid_file
    sleep 1
    echo -e "${GREEN}Server running. Navigate to: http://localhost:$server_port${NC}"
    exit 0
fi

# Stackup profile: edit the PCB and exit
if [[ -n "$stackup" ]]; then
    if [[ "$stackup" == list ]]; then
        exec python3 kibot_resources/scripts/set_stackup.py --list
    fi
    stackup_args=("$stackup")
    if [[ "$force_flag" == true ]]; then stackup_args+=(--force); fi
    exec python3 kibot_resources/scripts/set_stackup.py "${stackup_args[@]}"
fi

# The repository is often owned by another user than the one running the
# container: allow git to read it (non persistent, only for this process tree)
git_config=("safe.directory=*")
# Shared libraries as git submodules: KiRI checks out old commits and updates
# their submodules. Fetch them from the local copy (.git/modules/...), no
# network or credentials needed.
if [[ -f .gitmodules ]]; then
    git_config+=("protocol.file.allow=always")
    while read -r key _; do
        name="${key#submodule.}"
        name="${name%.url}"
        if [[ -d ".git/modules/$name" ]]; then
            git_config+=("submodule.$name.url=$PWD/.git/modules/$name")
        fi
    done < <(git config -f .gitmodules --get-regexp '^submodule\..*\.url$')
fi
export GIT_CONFIG_COUNT=${#git_config[@]}
for i in "${!git_config[@]}"; do
    export "GIT_CONFIG_KEY_$i=${git_config[$i]%%=*}"
    export "GIT_CONFIG_VALUE_$i=${git_config[$i]#*=}"
done

# Cache for the 3D models downloaded by KiBot
export KIBOT_3D_MODELS="${KIBOT_3D_MODELS:-$HOME/.cache/kibot_3d_models}"
mkdir -p "$KIBOT_3D_MODELS" 2>/dev/null

# Get revision if not specified
if [[ -z "$revision" ]]; then
    revision=$(python3 kibot_resources/scripts/get_changelog_version.py -f CHANGELOG.md)
    if [[ $? -ne 0 ]]; then
        echo -e "${YELLOW}Warning: Unable to determine version from CHANGELOG.md. Defaulting to empty revision.${NC}"
        revision=""
    fi
fi

# Output directory: assembly variants go in the Variants folder
case "$variant" in
    DRAFT|PRELIMINARY|CHECKED|RELEASED) output_dir="." ;;
    *) output_dir="Variants" ;;
esac

# Common KiBot arguments. --dont-stop: a failing output doesn't prevent the
# generation of the others, --fail-on-ignored: but we still return an error.
common_args=(-c "$kibot_config" -d "$output_dir" -g "variant=$variant" -E "REVISION=$revision"
             --dont-stop --fail-on-ignored)
if [[ -n "$render_engine" ]]; then
    common_args+=(-E "RENDER_ENGINE=$render_engine")
fi
# GIT_URL: auto -> URL of the git remote (https, without credentials)
if grep -qE "^  GIT_URL:[[:space:]]*['\"]?auto['\"]?[[:space:]]*(#.*)?$" "$kibot_config"; then
    git_url="$(git remote get-url origin 2>/dev/null)"
    git_url="$(echo "$git_url" | sed -E 's#^git@([^:]+):#https://\1/#; s#^ssh://git@([^/]+)/#https://\1/#; s#://[^@/]+@#://#; s#\.git$##')"
    common_args+=(-E "GIT_URL=$git_url")
fi

# KiBot saves the PCB/schematic while working (text variables, stackup
# drawing...). Keep the designer's files untouched, including uncommitted
# changes: snapshot them and restore them at exit. The text variables stored
# in the .kicad_pro are kept up to date on purpose.
snapshot_dir="$(mktemp -d)"
shopt -s nullglob
design_files=(./*.kicad_pcb ./*.kicad_sch)
shopt -u nullglob
if [[ ${#design_files[@]} -gt 0 ]]; then
    cp -p "${design_files[@]}" "$snapshot_dir/"
fi
function restore_design_files() {
    local f
    for f in "${design_files[@]}"; do
        cp -p "$snapshot_dir/$(basename "$f")" "$f"
    done
    rm -rf "$snapshot_dir"
}
trap restore_design_files EXIT

# Run KiBot: run_kibot <log name> [kibot options...] <outputs/groups...>
failed=0
function run_kibot() {
    local name="$1"; shift
    # KiBot needs the options before the output/group names (last arguments)
    local cmd=(kibot "${common_args[@]}" "${extra_args[@]}" "$@")
    if [[ -n "$log_dir" ]]; then
        mkdir -p "$log_dir"
        cmd=(kibot --log "$log_dir/kibot_$name.log" "${common_args[@]}" "${extra_args[@]}" "$@")
    fi
    echo -e "${GREEN}Running: ${cmd[*]}${NC}"
    "${cmd[@]}"
    local ret=$?
    if [[ $ret -ne 0 ]]; then
        echo -e "${RED}KiBot step '$name' failed (exit code $ret)${NC}"
        failed=$ret
    fi
    return $ret
}

# Variant specific flags. The fabrication/assembly notes are generated first,
# as the set_text_variables preflight includes them in the PCB documents.
# Folders fully regenerated by the PCB outputs: KiBot doesn't delete the files
# it no longer generates (i.e. Gerbers of renamed or removed layers after a
# stackup change), so start from empty folders
if [[ "$costs_flag" != true && "$variant" != DRAFT ]]; then
    for d in "Manufacturing/Fabrication/Gerbers" "Manufacturing/Fabrication/Drill Tables" "Manufacturing/JLCPCB"; do
        rm -rf "${output_dir:?}/$d"
    done
fi

if [[ "$costs_flag" == true ]]; then
    run_kibot costs --skip-pre erc,drc,draw_fancy_stackup \
        -E "KICOST_CONFIG=kibot_yaml/kicost_config_local.yaml" xlsx_bom
else
    case "$variant" in
        DRAFT)
            # README after the outputs: set_text_variables updated the project
            run_kibot outputs --skip-pre draw_fancy_stackup,erc,drc draft_group
            run_kibot readme --skip-pre set_text_variables,draw_fancy_stackup,erc,drc md_readme
            ;;
        PRELIMINARY)
            run_kibot notes --skip-pre all notes
            run_kibot outputs --skip-pre erc,drc all_group
            ;;
        CHECKED|RELEASED|*)
            run_kibot notes --skip-pre all notes
            run_kibot outputs all_group
            ;;
    esac
fi

# Manufacturing checks: Gerbers/drill, assembly files, PDF documents
if [[ "$costs_flag" != true && "$check_flag" == true ]]; then
    check_args=(-d "$output_dir")
    if [[ -n "$log_dir" ]]; then
        check_args+=(--markdown "$(pwd)/$log_dir/manufacturing_checks.md")
    fi
    echo -e "${GREEN}Running: manufacturing checks${NC}"
    python3 kibot_resources/scripts/check_manufacturing.py "${check_args[@]}"
    ret=$?
    if [[ $ret -ne 0 ]]; then
        echo -e "${RED}Manufacturing checks failed${NC}"
        failed=$ret
    fi
fi

# Remove the temporary project copies KiBot may leave behind
rm -f kibot_????????.kicad_{pcb,pro,prl,dru,sch} ./*.kicad_pcb-bak ./*.kicad_pro-bak ./~*.lck

# Give the generated files back to the host user (Linux hosts, see run_kibot.sh)
if [[ -n "$HOST_UID" && -n "$HOST_GID" && "$(id -u)" == "0" ]]; then
    chown -R "$HOST_UID:$HOST_GID" . 2>/dev/null
fi

exit $failed
