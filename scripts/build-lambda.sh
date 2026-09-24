#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
build_dir="${repo_root}/build/lambda"

rm -rf "${build_dir}"
mkdir -p "${build_dir}"
cp -R "${repo_root}/backend/app" "${build_dir}/app"
find "${build_dir}" -type d -name __pycache__ -prune -exec rm -rf {} +
find "${build_dir}" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
python3 -m pip install --disable-pip-version-check --no-compile \
  -r "${repo_root}/backend/lambda-requirements.txt" \
  -t "${build_dir}"

# Lambda imports the libraries directly; console entry points are unnecessary
# and otherwise embed the build runner's absolute Python path in the archive.
rm -rf "${build_dir}/bin"
find "${build_dir}" -type f -path '*/.dist-info/RECORD' -exec sed -i '/^bin\//d' {} +
