#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
build_dir="${repo_root}/build/lambda"

rm -rf "${build_dir}"
mkdir -p "${build_dir}"
cp -R "${repo_root}/backend/app" "${build_dir}/app"
python3 -m pip install --disable-pip-version-check --no-compile \
  -r "${repo_root}/backend/lambda-requirements.txt" \
  -t "${build_dir}"

