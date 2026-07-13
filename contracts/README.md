# contracts/ — KatanaLiquidator callback executor

`src/KatanaLiquidator.sol` — zero-capital Morpho Blue liquidation callback for Katana
(chainId 747474). Byte-identical flow to the production Base/Monad executors:
`liquidate()` → Morpho seizes LIF-incentivized collateral to the contract →
`onMorphoLiquidate()` swaps it to loanToken via the Sushi RouteProcessor → Morpho pulls
`repaidAssets` → surplus swept to owner. On-chain `minProfit` gate = slippage protection.

## Fork tests (full path, real Katana)
```
KATANA_RPC_URL=https://rpc.katana.network ./run_fork_test.sh [seize_vbwbtc]
```
1. `KatanaLiquidatorForkTest` — deterministic seize→swap→repay→sweep + minProfit gate against
   the REAL Morpho Blue on a Katana fork (mock market/tokens for determinism).
2. `SushiRealSwapForkTest` — REAL Sushi RouteProcessor swap of REAL vbWBTC on REAL pools, using
   live calldata fetched by the harness. Proves the swap leg the mock stubs out.

Plain `forge test` (no RPC) stays green — fork tests early-return.

## Deploy
See `script/Deploy.s.sol` and `../bot/deploy.sh`. Constructor arg = Morpho Blue
`0xD50F2DffFd62f94Ee4AEd9ca05C61d0753268aBc`.
