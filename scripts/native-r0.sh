#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail
# R0 now belongs in its isolated Ubuntu 24.04 container. See deploy/native/README.md.
exec bash "$(dirname "$0")/../deploy/native/build.sh" "$@"
