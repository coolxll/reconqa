#!/usr/bin/env bash
set -euo pipefail

# ReconQA external CLI installer (ProjectDiscovery tools + environment checks)
# Usage: ./scripts/install-tools.sh [TARGET_BIN_DIR]
# Default target directory: ./bin

BIN_DIR="${1:-./bin}"
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

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

install_pd_tool() {
  local tool="$1"
  local repo="projectdiscovery/${tool}"

  if [[ -x "${BIN_DIR}/${tool}" ]]; then
    echo "[OK] ${tool} already installed at ${BIN_DIR}/${tool}"
    return 0
  fi

  echo "[INFO] Resolving latest release for ${repo} (${OS}/${ARCH})..."
  local api_url="https://api.github.com/repos/${repo}/releases/latest"
  local release_json
  if ! release_json="$(curl -fsSL --connect-timeout 10 --max-time 30 "$api_url")"; then
    echo "[WARN] Failed to query GitHub releases for ${tool}. Skipping." >&2
    return 0
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
    echo "[WARN] No matching asset for ${tool} (${OS}/${ARCH}). Skipping." >&2
    return 0
  fi

  local zip_path="${TMP_DIR}/${tool}.zip"
  local extract_dir="${TMP_DIR}/${tool}_extract"
  mkdir -p "$extract_dir"

  echo "[INFO] Downloading ${tool}..."
  if ! curl -fsSL --connect-timeout 10 --max-time 120 -o "$zip_path" "$download_url"; then
    echo "[WARN] Failed to download ${tool}. Skipping." >&2
    return 0
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
  else
    echo "[WARN] Binary '${tool}' not found inside release archive." >&2
  fi
}

echo "=== Installing ProjectDiscovery tools into ${BIN_DIR} ==="
for tool in "${PD_TOOLS[@]}"; do
  install_pd_tool "$tool"
done

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
