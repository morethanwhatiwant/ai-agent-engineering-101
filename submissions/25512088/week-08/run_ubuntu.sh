#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
role="${1:-}"
case "$role" in
  shop) flags=(--shop-only --tunnel) ;;
  buyer) flags=(--buyer-only) ;;
  *) echo "Usage: bash run_ubuntu.sh shop|buyer" >&2; exit 2 ;;
esac
for file in shop_key.pem buyer_key.pem .market/owner.env; do
  [[ -f "$HERE/$file" ]] || { echo "Restore private ZIP first: missing $file" >&2; exit 1; }
done
[[ -f "$ROOT/.venv/bin/activate" ]] || { echo "Create .venv at repository root first." >&2; exit 1; }
source "$ROOT/.venv/bin/activate"
set -a
source "$HERE/.market/owner.env"
set +a
cd "$ROOT"
exec python scripts/market_kit.py join --dir "$HERE" --market https://market.ourolabs.ai --name 25512088 "${flags[@]}"
