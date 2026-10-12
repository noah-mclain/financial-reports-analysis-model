#!/usr/bin/env bash
# Print the apt packages the Docker image installs for Tesseract, taken from the Dockerfile's
# `apt-get install` command, so the package list lives in one place. CI installs exactly these.
# Usage: tesseract-packages.sh [DOCKERFILE]
set -euo pipefail

dockerfile="${1:-$(dirname "${BASH_SOURCE[0]}")/../../docker/Dockerfile}"
if [[ ! -f "$dockerfile" ]]; then
  echo "tesseract-packages: $dockerfile does not exist" >&2
  exit 1
fi

# Join backslash continuations, then keep the first apt-get install command.
command_line="$(awk '
  {
    if (continued) {
      line = line $0
    } else {
      line = $0
    }
    if (line ~ /\\$/) {
      sub(/\\$/, "", line)
      continued = 1
      next
    }
    if (index(line, "apt-get install")) {
      print line
      exit
    }
    line = ""
    continued = 0
  }
' "$dockerfile")"
if [[ -z "$command_line" ]]; then
  echo "tesseract-packages: no 'apt-get install' command in $dockerfile" >&2
  exit 1
fi

# Everything after `apt-get install`, up to the end of that shell command.
arguments="${command_line#*apt-get install}"
arguments="${arguments%%&&*}"
arguments="${arguments%%;*}"
arguments="${arguments%%|*}"

packages=()
for word in $arguments; do
  [[ "$word" == -* ]] && continue
  if [[ ! "$word" =~ ^[a-z0-9][a-z0-9+.-]*$ ]]; then
    echo "tesseract-packages: '$word' in $dockerfile is not a literal package name" >&2
    exit 1
  fi
  packages+=("$word")
done
if (( ${#packages[@]} == 0 )); then
  echo "tesseract-packages: the 'apt-get install' command in $dockerfile names no package" >&2
  exit 1
fi
echo "${packages[*]}"
