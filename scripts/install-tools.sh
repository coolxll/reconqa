#!/usr/bin/env bash
set -euo pipefail

# ReconQA external CLI installer (ProjectDiscovery tools + environment checks)
# Usage:
#   ./scripts/install-tools.sh [TARGET_BIN_DIR]
#   ./scripts/install-tools.sh [TARGET_BIN_DIR] --only <tool>
#   ./scripts/install-tools.sh --only <tool>
# Default target directory: ./bin

BIN_DIR="./bin"
ONLY_TOOL=""
POSITIONAL_SET=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --only)
      if [[ $# -lt 2 || -z "$2" ]]; then
        echo "Error: --only requires a tool name" >&2
        exit 1
      fi
      ONLY_TOOL="$2"
      shift 2
      ;;
    --only=*)
      ONLY_TOOL="${1#*=}"
      shift
      ;;
    -h|--help)
      echo "Usage: $0 [TARGET_BIN_DIR] [--only <subfinder|urlfinder|tlsx|katana|httpx|nuclei>]"
      exit 0
      ;;
    -*)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
    *)
      if [[ $POSITIONAL_SET -eq 0 ]]; then
        BIN_DIR="$1"
        POSITIONAL_SET=1
      else
        echo "Unexpected positional argument: $1" >&2
        exit 1
      fi
      shift
      ;;
  esac
done

mkdir -p "$BIN_DIR"
BIN_DIR="$(cd "$BIN_DIR" && pwd)"

OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) ARCH="amd64" ;;
  aarch64|arm64) ARCH="arm64" ;;
  *)
    echo "Unsupported architecture: $ARCH" >&2
    exit 1
    ;;
esac

if [[ "$OS" != "linux" && "$OS" != "darwin" ]]; then
  echo "Unsupported OS: $OS (supported: linux, darwin)" >&2
  exit 1
fi

PD_TOOLS=(
  "subfinder"
  "urlfinder"
  "tlsx"
  "katana"
  "httpx"
  "nuclei"
)

if [[ -n "$ONLY_TOOL" ]]; then
  VALID=0
  for t in "${PD_TOOLS[@]}"; do
    if [[ "$t" == "$ONLY_TOOL" ]]; then
      VALID=1
      break
    fi
  done
  if [[ $VALID -eq 0 ]]; then
    echo "Unsupported tool for --only: ${ONLY_TOOL}. Allowed: ${PD_TOOLS[*]}" >&2
    exit 1
  fi
  TARGET_TOOLS=("$ONLY_TOOL")
else
  TARGET_TOOLS=("${PD_TOOLS[@]}")
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

install_pd_tool() {
  local tool="$1"
  local strict="${2:-0}"
  local repo="projectdiscovery/${tool}"

  if [[ -x "${BIN_DIR}/${tool}" ]]; then
    echo "[OK] ${tool} already installed at ${BIN_DIR}/${tool}"
    return 0
  fi

  echo "[INFO] Resolving latest release for ${repo} (${OS}/${ARCH})..."
  local api_url="https://api.github.com/repos/${repo}/releases/latest"
  local release_json
  if ! release_json="$(curl -fsSL --connect-timeout 10 --max-time 30 "$api_url")"; then
    echo "[WARN] Failed to query GitHub releases for ${tool}." >&2
    [[ "$strict" -eq 1 ]] && return 1 || return 0
  fi

  local download_url
  download_url="$(python3 -c '
import json, sys
data = json.load(sys.stdin)
os_name, arch_name = sys.argv[1], sys.argv[2]
for asset in data.get("assets", []):
    name = asset.get("name", "")
    if f"_{os_name}_{arch_name}.zip" in name:
        print(asset.get("browser_download_url", ""))
        break
' "$OS" "$ARCH" <<< "$release_json")"

  if [[ -z "$download_url" ]]; then
    echo "[WARN] No matching asset for ${tool} (${OS}/${ARCH})." >&2
    [[ "$strict" -eq 1 ]] && return 1 || return 0
  fi

  local zip_path="${TMP_DIR}/${tool}.zip"
  local extract_dir="${TMP_DIR}/${tool}_extract"
  mkdir -p "$extract_dir"

  echo "[INFO] Downloading ${tool}..."
  if ! curl -fsSL --connect-timeout 10 --max-time 120 -o "$zip_path" "$download_url"; then
    echo "[WARN] Failed to download ${tool}." >&2
    [[ "$strict" -eq 1 ]] && return 1 || return 0
  fi

  python3 -c '
import sys, zipfile
with zipfile.ZipFile(sys.argv[1], "r") as zf:
    zf.extractall(sys.argv[2])
' "$zip_path" "$extract_dir"

  local bin_path
  bin_path="$(find "$extract_dir" -type f -name "$tool" | head -n 1)"
  if [[ -n "$bin_path" ]]; then
    install -m 0755 "$bin_path" "${BIN_DIR}/${tool}"
    echo "[OK] Installed ${tool} -> ${BIN_DIR}/${tool}"
    return 0
  else
    echo "[WARN] Binary '${tool}' not found inside release archive." >&2
    [[ "$strict" -eq 1 ]] && return 1 || return 0
  fi
}

echo "=== Installing ProjectDiscovery tool(s) into ${BIN_DIR} ==="
STRICT_MODE=0
[[ -n "$ONLY_TOOL" ]] && STRICT_MODE=1

for tool in "${TARGET_TOOLS[@]}"; do
  install_pd_tool "$tool" "$STRICT_MODE"
done

if [[ -z "$ONLY_TOOL" ]]; then
  echo ""
  echo "=== Checking additional optional tools ==="
  check_cmd() {
    local name="$1"
    local hint="$2"
    if command -v "$name" >/dev/null 2>&1; then
      echo "[OK] ${name}: $(command -v "$name")"
    else
      echo "[MISSING] ${name} — ${hint}"
    fi
  }

  check_cmd "k6" "Install from https://grafana.com/docs/k6/latest/set-up/install-k6/"
  check_cmd "mvn" "Install Apache Maven 3.9+ or run generated tests via docker/Dockerfile.java-runner"
  check_cmd "allure" "Install Allure CLI (https://allurereport.org/docs/install/) or use java-runner container"
  check_cmd "docker" "Install Docker Engine to run containerized java-runner / mitmproxy / ZAP"

  echo ""
  echo "Done. Run 'reconqa doctor' to inspect full tool availability."
fi
