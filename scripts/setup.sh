#!/usr/bin/env bash
# 兼容入口：统一走 lquant.sh
cd "$(dirname "$0")/.."
exec bash lquant.sh "${1:-install}"
