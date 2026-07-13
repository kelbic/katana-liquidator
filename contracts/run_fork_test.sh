#!/usr/bin/env bash
# Full-path fork test on a Katana fork. Two suites:
#   1. KatanaLiquidatorForkTest  — deterministic seize->swap->repay->sweep against REAL Morpho
#      (mock market + mock swapper). Always runs.
#   2. SushiRealSwapForkTest      — REAL Sushi RouteProcessor swap of REAL vbWBTC on REAL pools.
#      This script fetches a fresh quote (pool state drifts) with recipient = the fixed sink
#      0x…bEEF that the test asserts on, then exports the calldata for the probe.
#
# Usage:  KATANA_RPC_URL=https://rpc.katana.network ./run_fork_test.sh [seize_vbwbtc]
#   seize_vbwbtc: collateral size to swap, default 1.0 (~$62k). Try 0.5 / 2.4 to see slippage.
set -euo pipefail
cd "$(dirname "$0")"

: "${KATANA_RPC_URL:?set KATANA_RPC_URL (e.g. https://rpc.katana.network)}"
SEIZE_BTC="${1:-1.0}"
SINK="0x000000000000000000000000000000000000bEEF"
VBWBTC="0x0913DA6Da4b42f538B445599b46Bb4622342Cf52"
VBUSDC="0x203A662b0BD271A6ed5a60EdFbd04bFce608FD36"

echo "== 1/2 deterministic path vs real Morpho =="
forge test --match-contract KatanaLiquidatorForkTest -vv

echo
echo "== 2/2 real Sushi swap of ${SEIZE_BTC} vbWBTC -> vbUSDC (to sink ${SINK}) =="
# Fetch a fresh quote with recipient = SINK; emit shell exports for calldata/amounts.
eval "$(python3 - "$SEIZE_BTC" "$SINK" "$VBWBTC" "$VBUSDC" <<'PY'
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath("run_fork_test.sh")), ".."))
sys.path.insert(0, "..")
from bot.sushi import quote
seize_btc, sink, wbtc, usdc = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
seize_wei = int(float(seize_btc) * 10**8)
q = quote(wbtc, usdc, seize_wei, sender=sink, recipient=sink, max_slippage=0.03)
min_out = int(q["amount_out"] * 0.97)   # allow 3% vs the quoted assumedAmountOut
print(f'export SUSHI_CALLDATA="{q["swap_calldata"]}"')
print(f'export SEIZE_WEI="{seize_wei}"')
print(f'export SUSHI_MIN_OUT="{min_out}"')
print(f'echo "  quoted out={q["amount_out"]/1e6:,.2f} vbUSDC  priceImpact={q["price_impact"]*100:.3f}%  floor={min_out/1e6:,.2f}" 1>&2')
PY
)"
forge test --match-contract SushiRealSwapForkTest -vv
echo
echo "== fork test complete =="
