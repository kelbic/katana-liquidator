# bot/ — live-signing executor + Sushi routing

- `sushi.py`    — Sushi v7 swap API client (quote + ready-to-execute RouteProcessor calldata).
                  The only atomic exit for vb collateral. Drift-safe input haircut.
- `executor.py` — the loop: monitor.scan() → evaluate() (chunk-size the exit vs a LIVE Sushi
                  quote so slippage never eats the LIF bonus) → fire() (sign + broadcast an
                  atomic KatanaLiquidator.liquidate()). Ported from the production Base executor.
- `deploy.sh`   — deploy KatanaLiquidator to Katana (operator's key). The one live step.
- `run.sh`      — local launcher (sources ~/.katana-bot/env). Default = one DRY_RUN pass.
- `katana-executor.service` — systemd unit for the live loop.

Safety layers (all on by default): DRY_RUN=1, off-chain net gate, on-chain minProfit gate,
swap-input haircut, daily-gas + consecutive-revert kill-switch, target dedup, chunk sizing.

Signing: default `cast` (foundry, no python deps). `KT_RAW_TX=1` uses in-process eth_account
(pip install -r requirements.txt). Calldata is byte-identical on both paths (checked vs cast).

Tests: `PYTHONPATH=. python3 -m bot.test_executor` (offline, stubbed quote).
