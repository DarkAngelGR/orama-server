#!/usr/bin/env bash
# Orama installer. Run on the server:
#   curl -fsSL https://raw.githubusercontent.com/DarkAngelGR/orama-server/main/install.sh | sudo bash
#
# Settings (all optional), passed like: curl ... | sudo ORAMA_DATA_ROOT=/srv/media bash
#   ORAMA_DATA_ROOT    the folder for movies, TV and downloads (default /data, UGOS /volume1/data)
#   ORAMA_CONFIG_ROOT  Orama's own folder (default /opt/orama, UGOS /volume1/docker/orama)
#   ORAMA_PUID / ORAMA_PGID  the user and group that own the files (default: you)
#   ORAMA_VERSION      the Orama version to install
#   ORAMA_BUILD_LOCAL=1 builds the images from a checkout (ORAMA_SRC, default this folder)
#   DRY_RUN=1          print what would be done instead of doing it
#
# Everything below is a function; the last line runs `main`, so a partly downloaded
# script does nothing.
set -Eeuo pipefail

ORAMA_DEFAULT_VERSION="0.1.0"
ORAMA_VERSION="${ORAMA_VERSION:-$ORAMA_DEFAULT_VERSION}"
OS_RELEASE_FILE="${OS_RELEASE_FILE:-/etc/os-release}"
PASSWD_FILE="${PASSWD_FILE:-/etc/passwd}"
DOCKER_BIN="${DOCKER_BIN:-docker}"
SS_BIN="${SS_BIN:-ss}"
DRY_RUN="${DRY_RUN:-0}"

INSTALL_URL="https://raw.githubusercontent.com/DarkAngelGR/orama-server/main/install.sh"
IMAGE_REPO="ghcr.io/darkangelgr"
DOCKER_SOCK="/var/run/docker.sock"
RENDER_NODE="/dev/dri/renderD128"
DOCKER_KEY_FINGERPRINT="9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
DOCKER_KEYRING="/etc/apt/keyrings/docker.asc"
DOCKER_APT_LIST="/etc/apt/sources.list.d/docker.list"
CORE_PROJECT="orama-core"

# ---------------------------------------------------------------- output helpers

say() { printf '%s\n' "$*"; }
note() { printf '%s\n' "$*" >&2; }
# Stop with a plain-language reason. Goes to stderr so it shows even inside $(...).
fail() {
  printf '✗ %s\n' "$*" >&2
  exit 1
}

is_dry_run() { [[ "$DRY_RUN" == 1 ]]; }

# Run a command, or under DRY_RUN=1 print it (shell-quoted) instead.
run() {
  if is_dry_run; then
    local quoted
    printf -v quoted '%q ' "$@"
    say "+ ${quoted% }"
  else
    "$@"
  fi
}

# Write `content` to `path` with `mode`, or under DRY_RUN=1 print it.
write_file() {
  local path=$1 mode=$2 content=$3
  if is_dry_run; then
    say "+ write $path ($mode): $content"
  else
    printf '%s\n' "$content" > "$path"
    chmod "$mode" "$path"
  fi
}

# ---------------------------------------------------------------- platform

# The value of `key` in the os-release file (last one wins), quotes removed.
os_field() {
  local key=$1 line value=""
  [[ -r "$OS_RELEASE_FILE" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" == "$key="* ]] || continue
    value=${line#*=}
    if [[ "$value" == \"*\" || "$value" == \'*\' ]]; then
      value=${value:1:${#value}-2}
    fi
  done < "$OS_RELEASE_FILE"
  printf '%s' "$value"
}

# Prints ubuntu, debian, ugos or unsupported (and then stops).
detect_platform() {
  local key value id
  # UGOS reports ID=debian, so look for its markers first.
  for key in NAME PRETTY_NAME ID_LIKE VARIANT; do
    value=$(os_field "$key")
    if [[ "${value,,}" == *ugos* ]]; then
      say ugos
      return 0
    fi
  done
  id=$(os_field ID)
  case "${id,,}" in
    ubuntu | debian)
      say "${id,,}"
      return 0
      ;;
  esac
  say unsupported
  value=$(os_field PRETTY_NAME)
  note "✗ Sorry, Orama only works on Ubuntu, Debian and UGOS Pro." \
    "This computer runs ${value:-a system Orama does not recognise}, so nothing was installed."
  return 1
}

require_x86_64() {
  local arch
  arch=$(uname -m)
  [[ "$arch" == x86_64 ]] ||
    fail "Orama needs a computer with an x86-64 (Intel or AMD) processor; this one is $arch. Nothing was installed."
}

require_root() {
  [[ "$EUID" -eq 0 ]] ||
    fail "The installer needs administrator rights. Run it again with sudo: curl -fsSL $INSTALL_URL | sudo bash"
}

# ---------------------------------------------------------------- docker

docker_works() {
  "$DOCKER_BIN" version > /dev/null 2>&1 && "$DOCKER_BIN" compose version > /dev/null 2>&1
}

# Stop unless `file` holds exactly one key: Docker's published signing key.
check_docker_key() {
  local file=$1 home fingerprints
  home=$(mktemp -d)
  fingerprints=$(gpg --homedir "$home" --batch --show-keys --with-colons "$file" 2> /dev/null |
    awk -F: '$1 == "pub" { want = 1; next } $1 == "fpr" && want { print $10; want = 0 }') || true
  rm -rf "$home"
  [[ "$fingerprints" == "$DOCKER_KEY_FINGERPRINT" ]] ||
    fail "The Docker signing key that was downloaded is not the one Docker publishes, so Docker was not installed. Please try again later."
}

ensure_docker() {
  local platform=$1 codename key_download
  if docker_works; then
    return 0
  fi
  if [[ "$platform" == ugos ]]; then
    fail "Docker is not running on this NAS. Please install/enable Docker from the UGOS App Center, then run this again."
  fi

  say "Installing Docker from Docker's official repository..."
  codename=$(os_field VERSION_CODENAME)
  [[ -n "$codename" ]] || codename=$(os_field UBUNTU_CODENAME)
  [[ -n "$codename" ]] ||
    fail "Cannot tell which $platform release this is, so Docker cannot be installed automatically. Please install Docker yourself, then run this again."

  key_download="${DOCKER_KEYRING}.download"
  run env DEBIAN_FRONTEND=noninteractive apt-get update
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl gnupg
  run install -m 0755 -d "$(dirname "$DOCKER_KEYRING")"
  run curl -fsSL "https://download.docker.com/linux/$platform/gpg" -o "$key_download"
  if is_dry_run; then
    say "+ check_docker_key $key_download (must be fingerprint $DOCKER_KEY_FINGERPRINT)"
  else
    check_docker_key "$key_download"
  fi
  run install -m 0644 "$key_download" "$DOCKER_KEYRING"
  run rm -f "$key_download"
  write_file "$DOCKER_APT_LIST" 0644 \
    "deb [arch=amd64 signed-by=$DOCKER_KEYRING] https://download.docker.com/linux/$platform $codename stable"
  run env DEBIAN_FRONTEND=noninteractive apt-get update
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y \
    docker-ce docker-ce-cli containerd.io docker-compose-plugin
  run systemctl enable --now docker
  if ! is_dry_run && ! docker_works; then
    fail "Docker was installed but is not working yet. Restart the computer, then run this again."
  fi
}

# ---------------------------------------------------------------- ids, gids, ports

# Prints "uid:gid" for the user who will own the files.
install_user() {
  local platform=$1 uid="" gid="" found
  if [[ -n "${SUDO_UID:-}" && -n "${SUDO_GID:-}" && "${SUDO_UID}" != 0 && "${SUDO_GID}" != 0 ]]; then
    uid=$SUDO_UID gid=$SUDO_GID
  elif [[ "$platform" == ugos ]]; then
    found=$(awk -F: '$3 >= 1000 && $3 < 60000 { print $3 ":" $4; exit }' "$PASSWD_FILE" 2> /dev/null) || true
    if [[ -n "$found" ]]; then
      uid=${found%%:*} gid=${found#*:}
    fi
  fi
  if [[ -z "$uid" && -z "${ORAMA_PUID:-}" ]]; then
    uid=1000 gid=1000
    note "Note: could not tell which user you are, so files will belong to user 1000, group 1000." \
      "To choose others, run again with ORAMA_PUID=<user id> ORAMA_PGID=<group id>."
  fi
  uid=${ORAMA_PUID:-$uid}
  gid=${ORAMA_PGID:-$gid}
  [[ "$uid" =~ ^[0-9]+$ && "$gid" =~ ^[0-9]+$ ]] ||
    fail "The user and group must be numbers (got '$uid' and '$gid'). Check ORAMA_PUID and ORAMA_PGID."
  if [[ "$uid" == 0 || "$gid" == 0 ]]; then
    fail "Orama's files cannot belong to root (user $uid, group $gid). Run the installer with sudo from your own account, or set ORAMA_PUID and ORAMA_PGID to your user and group ids."
  fi
  say "$uid:$gid"
}

# The numeric group id owning `path`, or "none" if it does not exist.
numeric_gid_of() {
  stat -c %g -- "$1" 2> /dev/null || say none
}

# "1" if this computer has NVIDIA tools, else "0".
has_nvidia() {
  local tool
  for tool in nvidia-smi nvidia-container-cli nvidia-ctk; do
    if command -v "$tool" > /dev/null 2>&1; then
      say 1
      return 0
    fi
  done
  say 0
}

# Ports something already listens on (TCP), sorted, comma-separated.
taken_ports() {
  if ! command -v "$SS_BIN" > /dev/null 2>&1; then
    note "Note: cannot list the ports already in use ('ss' is missing); Orama will pick its usual ones."
    say ""
    return 0
  fi
  "$SS_BIN" -Htln | awk '{ n = split($4, a, ":"); if (a[n] ~ /^[0-9]+$/) print a[n] }' |
    sort -un | paste -sd, - || true
}

# ---------------------------------------------------------------- folders

# Sets DATA_ROOT and CONFIG_ROOT: env overrides, else the platform defaults; canonical.
resolve_roots() {
  local platform=$1 data config
  if [[ "$platform" == ugos ]]; then
    data=/volume1/data config=/volume1/docker/orama
  else
    data=/data config=/opt/orama
  fi
  DATA_ROOT=$(realpath -m -- "${ORAMA_DATA_ROOT:-$data}")
  CONFIG_ROOT=$(realpath -m -- "${ORAMA_CONFIG_ROOT:-$config}")
  # Docker Compose passes folders to Docker as "source:target", so ":" cannot work.
  [[ "$DATA_ROOT" != *:* ]] ||
    fail "The data folder $DATA_ROOT has a \":\" in its path, which Docker cannot use. Choose another with ORAMA_DATA_ROOT."
  [[ "$CONFIG_ROOT" != *:* ]] ||
    fail "The Orama folder $CONFIG_ROOT has a \":\" in its path, which Docker cannot use. Choose another with ORAMA_CONFIG_ROOT."
}

prepare_config_root() {
  if [[ -e "$CONFIG_ROOT" || -L "$CONFIG_ROOT" ]]; then
    [[ -d "$CONFIG_ROOT" ]] || fail "$CONFIG_ROOT exists but is not a folder. Move it away or choose another with ORAMA_CONFIG_ROOT."
    return 0
  fi
  run mkdir -p -- "$CONFIG_ROOT"
  run chmod 0755 -- "$CONFIG_ROOT"
}

# Create the data folder only if it is missing; an existing one is never changed.
prepare_data_root() {
  if [[ -e "$DATA_ROOT" || -L "$DATA_ROOT" ]]; then
    [[ -d "$DATA_ROOT" ]] || fail "$DATA_ROOT exists but is not a folder. Move it away or choose another with ORAMA_DATA_ROOT."
    return 0
  fi
  run mkdir -p -- "$DATA_ROOT"
  run chown "$PUID:$PGID" -- "$DATA_ROOT"
  run chmod 0755 -- "$DATA_ROOT"
}

# Compares an earlier setup file with this run's values. Exit 0 same, 1 different, 2 unreadable.
# Args: file data_root config_root puid pgid install_url.
# shellcheck disable=SC2016  # Python source, not shell
SETUP_CHECK_PY='
import json, os, shlex, stat, sys
path, data_root, config_root, puid, pgid, url = sys.argv[1:7]
LIMIT = 1024 * 1024  # a real setup is a few KiB
try:
    # The file is writable by Orama (PUID) and we run as root: never follow a link, never
    # block on a FIFO, read only a regular file, and only so much of it.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise ValueError
        raw = f.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError
    saved = json.loads(raw.decode("utf-8"))
    fields = [
        ("data folder", "ORAMA_DATA_ROOT", saved["data_root"], data_root),
        ("Orama folder", "ORAMA_CONFIG_ROOT", saved["config_root"], config_root),
        ("user id", "ORAMA_PUID", saved["puid"], int(puid)),
        ("group id", "ORAMA_PGID", saved["pgid"], int(pgid)),
    ]
    if not all(type(s) is type(n) for _, _, s, n in fields):
        raise ValueError
except Exception:
    sys.exit(2)

def shown(value):
    """Printable values as they are; anything with control characters escaped."""
    text = str(value)
    return text if text.isprintable() else repr(text)

diff = [f for f in fields if f[2] != f[3]]
for label, env, old, new in diff:
    print(f"    {label}: saved {shown(old)}, this run {shown(new)}  ({env})")
if diff:
    if all(str(old).isprintable() for _, _, old, _ in diff):
        print("  To keep the saved settings, run the installer again with them, for example:")
        sets = " ".join(f"{env}={shlex.quote(str(old))}" for _, env, old, _ in diff)
        print(f"    curl -fsSL {url} | sudo {sets} bash")
    else:
        print("  To keep the saved settings, run the installer again with the variables above")
        print("  set to the saved values.")
sys.exit(1 if diff else 0)
'

# Stop if an earlier setup in CONFIG_ROOT was made with other folders or ids.
check_existing_setup() {
  local state="$CONFIG_ROOT/orama" name="" file out status=0
  for file in setup.json setup.draft.json; do
    if [[ -e "$state/$file" || -L "$state/$file" ]]; then
      name=$file
      break
    fi
  done
  [[ -n "$name" ]] || return 0

  if command -v python3 > /dev/null 2>&1; then
    out=$(python3 -I -c "$SETUP_CHECK_PY" "$state/$name" \
      "$DATA_ROOT" "$CONFIG_ROOT" "$PUID" "$PGID" "$INSTALL_URL") || status=$?
  else
    out=$("$DOCKER_BIN" run --rm --user 0:0 --network none \
      --mount "$(mount_arg "$state" /state ro)" --entrypoint python "$ORAMA_IMAGE" \
      -I -c "$SETUP_CHECK_PY" "/state/$name" \
      "$DATA_ROOT" "$CONFIG_ROOT" "$PUID" "$PGID" "$INSTALL_URL") || status=$?
  fi
  case "$status" in
    0) return 0 ;;
    1) fail "Orama is already set up in $CONFIG_ROOT with different settings, so nothing was changed:
$out" ;;
    *) fail "Cannot read the earlier Orama settings in $state/$name. Fix or remove that file, then run this again." ;;
  esac
}

# ---------------------------------------------------------------- images and start

# A docker --mount value binding `source` at `target` (CSV-quoted, so any path works).
mount_arg() {
  local source=${1//\"/\"\"} target=${2//\"/\"\"} value
  value="type=bind,\"source=$source\",\"target=$target\""
  if [[ "${3:-}" == ro ]]; then
    value+=",readonly"
  fi
  say "$value"
}

# Folder holding the Orama source for a local build.
orama_source_dir() {
  local src="${ORAMA_SRC:-}" self="${BASH_SOURCE[0]:-}"
  if [[ -z "$src" ]]; then
    [[ -n "$self" && -f "$self" ]] ||
      fail "A local build needs the Orama source folder: set ORAMA_SRC=/path/to/orama-server."
    src=$(dirname -- "$self")
  fi
  src=$(realpath -m -- "$src")
  [[ -f "$src/docker/orama.Dockerfile" && -f "$src/docker/agent.Dockerfile" ]] ||
    fail "$src does not look like the Orama source folder (no docker/orama.Dockerfile). Set ORAMA_SRC to it."
  say "$src"
}

# Sets ORAMA_IMAGE and AGENT_IMAGE, pulling them (or building them with ORAMA_BUILD_LOCAL=1).
get_images() {
  local src
  ORAMA_IMAGE="$IMAGE_REPO/orama:$ORAMA_VERSION"
  AGENT_IMAGE="$IMAGE_REPO/orama-agent:$ORAMA_VERSION"
  if [[ "${ORAMA_BUILD_LOCAL:-0}" == 1 ]]; then
    src=$(orama_source_dir)
    say "Building the Orama images from $src..."
    run "$DOCKER_BIN" build -f "$src/docker/orama.Dockerfile" -t "$ORAMA_IMAGE" "$src" ||
      fail "Building the Orama image failed (see above)."
    run "$DOCKER_BIN" build -f "$src/docker/agent.Dockerfile" -t "$AGENT_IMAGE" "$src" ||
      fail "Building the Orama Agent image failed (see above)."
  else
    say "Downloading Orama $ORAMA_VERSION..."
    run "$DOCKER_BIN" pull "$ORAMA_IMAGE" ||
      fail "Could not download $ORAMA_IMAGE. Check the internet connection and try again."
    run "$DOCKER_BIN" pull "$AGENT_IMAGE" ||
      fail "Could not download $AGENT_IMAGE. Check the internet connection and try again."
  fi
}

# Writes Orama's core settings (as root, in a throwaway container); sets ORAMA_PORT.
# Args: platform docker_gid render_gid|none nvidia(0|1) taken_ports
run_bootstrap() {
  local platform=$1 docker_gid=$2 render_gid=$3 nvidia=$4 taken=$5 out
  local cmd=(
    "$DOCKER_BIN" run --rm --user 0:0 --network none
    --mount "$(mount_arg "$CONFIG_ROOT" "$CONFIG_ROOT")"
    "$ORAMA_IMAGE" python -m orama.cli bootstrap
    --platform "$platform" --data-root "$DATA_ROOT" --config-root "$CONFIG_ROOT"
    --puid "$PUID" --pgid "$PGID" --docker-gid "$docker_gid" --render-gid "$render_gid"
    --nvidia "$nvidia" --taken-ports "$taken"
    --orama-image "$ORAMA_IMAGE" --agent-image "$AGENT_IMAGE"
  )
  if is_dry_run; then
    run "${cmd[@]}"
    ORAMA_PORT="<port>"
    return 0
  fi
  out=$("${cmd[@]}") || fail "Orama could not prepare its settings (the reason is above). Nothing was started."
  out=${out##*$'\n'}
  [[ "$out" =~ ^[0-9]+$ ]] || fail "Orama prepared its settings but did not say which port it uses. Nothing was started."
  ORAMA_PORT=$out
}

start_core() {
  say "Starting Orama..."
  run "$DOCKER_BIN" compose -p "$CORE_PROJECT" --project-directory "$CONFIG_ROOT" \
    -f "$CONFIG_ROOT/core.compose.yaml" up -d --wait ||
    fail "Orama did not start. To see why, run: sudo docker compose -p $CORE_PROJECT logs"
}

print_address() {
  local port=$1 ip address=localhost
  for ip in $(hostname -I 2> /dev/null || true); do
    if [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ && "$ip" != 127.* ]]; then
      address=$ip
      break
    fi
  done
  say "✓ Orama is ready. Open http://$address:$port in your browser."
}

# ---------------------------------------------------------------- main

main() {
  local platform ids docker_gid render_gid nvidia taken
  platform=$(detect_platform)
  require_x86_64
  require_root
  say "Installing Orama $ORAMA_VERSION on $platform."

  ensure_docker "$platform"
  ids=$(install_user "$platform")
  PUID=${ids%%:*}
  PGID=${ids#*:}
  resolve_roots "$platform"
  say "Media folder: $DATA_ROOT. Orama folder: $CONFIG_ROOT. Owner: user $PUID, group $PGID."

  get_images
  check_existing_setup
  docker_gid=$(numeric_gid_of "$DOCKER_SOCK")
  [[ "$docker_gid" != none ]] ||
    fail "Docker is installed but $DOCKER_SOCK is missing, so Orama cannot manage apps. Make sure Docker is running, then run this again."
  render_gid=$(numeric_gid_of "$RENDER_NODE")
  nvidia=$(has_nvidia)
  taken=$(taken_ports)

  prepare_config_root
  run_bootstrap "$platform" "$docker_gid" "$render_gid" "$nvidia" "$taken"
  prepare_data_root
  start_core
  print_address "$ORAMA_PORT"
}

if [[ "${BASH_SOURCE[0]:-$0}" != "$0" ]]; then return 0; fi
main "$@"
