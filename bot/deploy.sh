#!/usr/bin/env bash
# Deploy KatanaLiquidator.sol to Katana mainnet (chainId 747474) with the OPERATOR's key.
# This is the ONE live on-chain step the operator runs (the bot itself is deploy-ready but
# never deploys on its own). Requires: a funded key (a little ETH for gas — deploy is ~1.2M gas,
# ~$0.01 at Katana's ~0.002 gwei) and foundry.
#
# Usage:
#   KT_KEYFILE=~/.katana-bot/key ./bot/deploy.sh        # key in a 600-perm file (preferred)
#   KT_PRIVATE_KEY=0x... ./bot/deploy.sh                # or inline (avoid; shell history)
set -euo pipefail
cd "$(dirname "$0")/../contracts"

MORPHO=0xD50F2DffFd62f94Ee4AEd9ca05C61d0753268aBc      # Morpho Blue on Katana (constructor arg)
RPC=${KT_RPC:-https://rpc.katana.network}
KEYFILE=${KT_KEYFILE:-$HOME/.katana-bot/key}

if [ -n "${KT_PRIVATE_KEY:-}" ]; then
    SIGN=(--private-key "$KT_PRIVATE_KEY")
    echo "signing: inline KT_PRIVATE_KEY"
elif [ -f "$KEYFILE" ]; then
    SIGN=(--private-key "$(cat "$KEYFILE")")
    echo "signing: key file $KEYFILE"
else
    echo "ERROR: no key. Set KT_PRIVATE_KEY or put a key in $KEYFILE (chmod 600)." >&2
    exit 1
fi

echo "Deploying KatanaLiquidator(morpho=$MORPHO) to Katana via $RPC…"
# --constructor-args is variadic — keep it LAST so forge doesn't swallow later flags.
forge create src/KatanaLiquidator.sol:KatanaLiquidator \
    "${SIGN[@]}" \
    --rpc-url "$RPC" --broadcast \
    --constructor-args "$MORPHO"

echo
echo "Done. Copy 'Deployed to: 0x…' -> set KT_CONTRACT=0x… in ~/.katana-bot/env, then:"
echo "  1) fund the deployer/owner wallet with a little ETH for gas"
echo "  2) DRY_RUN=1 python3 -m bot.executor once   # verify it sees targets"
echo "  3) flip DRY_RUN=0 in ~/.katana-bot/env and start the service"
