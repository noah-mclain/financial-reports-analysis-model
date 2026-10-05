#!/usr/bin/env bash
set -euo pipefail

readonly ACTIONLINT_VERSION=1.7.12
readonly ACTIONLINT_RELEASE="https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}"
readonly temp_dir="$(mktemp -d)"
trap 'rm -rf "$temp_dir"' EXIT

asset=''
checksum=''
case "$(uname -s)-$(uname -m)" in
  Linux-x86_64)
    asset="actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz"
    checksum=8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8
    ;;
  Linux-aarch64|Linux-arm64)
    asset="actionlint_${ACTIONLINT_VERSION}_linux_arm64.tar.gz"
    checksum=325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6
    ;;
  Darwin-arm64)
    asset="actionlint_${ACTIONLINT_VERSION}_darwin_arm64.tar.gz"
    checksum=aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f
    ;;
  Darwin-x86_64)
    asset="actionlint_${ACTIONLINT_VERSION}_darwin_amd64.tar.gz"
    checksum=5b44c3bc2255115c9b69e30efc0fecdf498fdb63c5d58e17084fd5f16324c644
    ;;
  *)
    echo "unsupported actionlint platform: $(uname -s)-$(uname -m)" >&2
    exit 1
    ;;
esac

if command -v actionlint >/dev/null 2>&1; then
  binary="$(command -v actionlint)"
  installed_version="$(actionlint -version | head -n 1 | awk '{print $1}')"
  if [[ "$installed_version" != "$ACTIONLINT_VERSION" ]]; then
    echo "actionlint $ACTIONLINT_VERSION required, found $installed_version" >&2
    exit 1
  fi
else
  curl --fail --silent --show-error --location "$ACTIONLINT_RELEASE/$asset" --output "$temp_dir/$asset"
  printf '%s  %s\n' "$checksum" "$temp_dir/$asset" | shasum -a 256 --check --status
  tar -xzf "$temp_dir/$asset" -C "$temp_dir" actionlint
  binary="$temp_dir/actionlint"
fi

"$binary" .github/workflows/*.yml
