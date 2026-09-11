# Source this fragment from packaged shell hooks; never eval consumer values.
loopzero_env() {
    local prefix="${LOOPZERO_ENV_PREFIX:-LOOPZERO}" name
    [[ "$prefix" =~ ^[A-Z][A-Z0-9_]*$ ]] || return 2
    name="${prefix}_$1"
    printf '%s' "${!name-${2-}}"
}
loopzero_consumer() {
    local executable
    executable="$(loopzero_env "$1")"
    shift
    if [ -z "$executable" ] || [ ! -f "$executable" ] || [ -L "$executable" ] || [ ! -x "$executable" ]; then
        echo "Required approved consumer hook is unavailable (TODO A4)" >&2
        return 2
    fi
    "$executable" "$@"
}
