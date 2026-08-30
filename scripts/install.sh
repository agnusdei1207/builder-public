#!/bin/sh
# install.sh — one-line installer for the pentesting native binary.
#
#   curl -fsSL https://raw.githubusercontent.com/agnusdei1207/pentesting-public/main/scripts/install.sh | sh
#
# Downloads the latest published Linux x86_64 (musl) binary from GitHub Releases
# and installs it. Set PENTESTING_VERSION to pin a tag, PENTESTING_INSTALL_DIR to
# choose a directory. Only linux/amd64 has a native asset; other platforms should
# use the Docker image documented in the README.
set -eu

# The public release mirror that actually publishes the binary, matching the npm
# facade (lib/runtime.mjs). The dev repo agnusdei1207/pentesting is not the release
# host.
REPO="agnusdei1207/pentesting-public"
ASSET="pentesting-x86_64-unknown-linux-musl"
VERSION="${PENTESTING_VERSION:-latest}"

err() { echo "install: $*" >&2; }

# ---- platform check -------------------------------------------------------
os="$(uname -s 2>/dev/null || echo unknown)"
arch="$(uname -m 2>/dev/null || echo unknown)"
if [ "$os" != "Linux" ] || { [ "$arch" != "x86_64" ] && [ "$arch" != "amd64" ]; }; then
  err "no native binary for ${os}/${arch}."
  err "only linux/x86_64 ships a native asset; use the Docker image instead:"
  err "  docker run -it --rm ghcr.io/agnusdei1207/pentesting-public:latest"
  exit 1
fi

# ---- download tool --------------------------------------------------------
if command -v curl >/dev/null 2>&1; then
  fetch() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
  fetch() { wget -qO "$2" "$1"; }
else
  err "need curl or wget on PATH."
  exit 1
fi

if [ "$VERSION" = "latest" ]; then
  url="https://github.com/${REPO}/releases/latest/download/${ASSET}"
else
  url="https://github.com/${REPO}/releases/download/${VERSION}/${ASSET}"
fi

# ---- install directory ----------------------------------------------------
# Prefer a directory already on PATH and writable; fall back to ~/.local/bin.
if [ -n "${PENTESTING_INSTALL_DIR:-}" ]; then
  install_dir="$PENTESTING_INSTALL_DIR"
elif [ -w "/usr/local/bin" ] 2>/dev/null; then
  install_dir="/usr/local/bin"
else
  install_dir="${HOME}/.local/bin"
fi
mkdir -p "$install_dir"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT INT TERM

echo "install: downloading ${ASSET} (${VERSION})"
if ! fetch "$url" "$tmp"; then
  err "download failed: ${url}"
  err "check that release ${VERSION} publishes ${ASSET}."
  exit 1
fi

# A GitHub 404 page is HTML, not an ELF binary; reject it before installing.
if ! head -c 4 "$tmp" | od -An -tx1 2>/dev/null | grep -q "7f 45 4c 46"; then
  err "downloaded file is not a Linux binary; the asset may be missing for ${VERSION}."
  exit 1
fi

chmod +x "$tmp"
target="${install_dir}/pentesting"
if mv "$tmp" "$target" 2>/dev/null; then
  trap - EXIT INT TERM
else
  err "cannot write ${target}; re-run with sudo or set PENTESTING_INSTALL_DIR."
  exit 1
fi

echo "install: installed pentesting -> ${target}"
case ":${PATH}:" in
  *":${install_dir}:"*) : ;;
  *) echo "install: add ${install_dir} to PATH:  export PATH=\"${install_dir}:\$PATH\"" ;;
esac
echo "install: run 'pentesting' to start."
