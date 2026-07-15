# katana-liquidator

Battle-ready **Morpho Blue liquidation bot for Katana** (chainId 747474). Zero-capital atomic
liquidations (Morpho callback seizes collateral → swaps it on Sushi → repays → sweeps surplus),
a live-signing executor with a full capital-protection stack, and a fork-tested end-to-end path.

**The verdict, economics, risks, and architecture are in [`STATE.md`](STATE.md) — read it first.**
Short version: **+EV is real** (~$24–28k/mo of liquidation bonus flows on Katana, net-positive at
every realistic size — a $148k exit still nets +2.48% after slippage), gas is ~$0.005, no flash
loan, and it is a cheap early option on a growing chain. Do not oversize into weETH/vbETH.

## What's here
- `analysis/` — READ-ONLY, stdlib-only Morpho tooling (RPC, multicall, HF math, position monitor).
- `contracts/` — `KatanaLiquidator.sol` callback + fork tests + deploy script.
- `bot/` — Sushi routing client + live-signing executor + deploy/run/systemd.

## Verify it yourself (no key needed)

```bash
# 1. offline tests (pure math / decoders / executor logic — no network)
python3 -m analysis.test_keccak && python3 -m analysis.test_models \
  && python3 -m analysis.test_protocols && python3 -m analysis.test_monitor \
  && PYTHONPATH=. python3 -m bot.test_executor

# 2. full-path fork test on a Katana fork (real Morpho liquidate + real Sushi swap)
cd contracts && KATANA_RPC_URL=https://rpc.katana.network ./run_fork_test.sh 1.0

# 3. see what the bot WOULD do, live, against Katana — never sends (DRY_RUN default)
#    Discovery is via the Morpho indexer (current borrowers), so no checkpoint is needed.
DRY_RUN=1 KT_CONTRACT=0x25b5DeA89c8d337d0B040aBd10f8D69c2DfbCa45 python3 -m bot.executor once
# -> "block … | positions N | targets(HF<1) K | guard=OK … contract=set" in <10s
```

## Go live — operator steps (the parts that need your key)

The bot is deploy-ready but **never deploys or funds itself**. You do these three things:

**1. Install fire-path deps & deploy the contract** (~1.2M gas ≈ $0.01 on Katana):
```bash
pip install -r requirements.txt        # eth-abi + eth-account: REQUIRED for the live fire path
(umask 077 && mkdir -p ~/.katana-bot)  # umask BEFORE mkdir so the dir itself is 700
cat > ~/.katana-bot/key                # paste the key + Enter + Ctrl-D — keeps it out of
                                       # bash history (unlike printf '0x…')
KT_KEYFILE=~/.katana-bot/key ./bot/deploy.sh
# -> copy "Deployed to: 0x…"
```

**2. Configure & fund:**
```bash
cp .env.example ~/.katana-bot/env && chmod 600 ~/.katana-bot/env
# edit ~/.katana-bot/env: set KT_CONTRACT=0x… (from step 1). Leave DRY_RUN=1 for now.
# send a few dollars of ETH to the owner wallet (gas only — no trading capital needed).
```

**3. Dry-run, then flip live:**
```bash
./bot/run.sh once                      # confirm it scans + sees targets, still DRY_RUN
# when satisfied, set DRY_RUN=0 in ~/.katana-bot/env, then install the service:
sudo cp bot/katana-executor.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now katana-executor
journalctl -u katana-executor -f      # watch it
```

Kill-switch: on a daily-gas or consecutive-revert breach the loop stops and alerts. Recover with
`python3 -m bot.executor reset` then `systemctl start katana-executor`.

## Safety model (why this is safe to run with capital)
- **DRY_RUN=1 by default** — nothing sends until you flip it.
- **Zero standing capital**: the seized collateral funds the repayment inside one atomic tx; the
  hot wallet holds only gas; profit is swept to the owner every time. Not a honeypot.
- **Two profit gates**: the executor won't fire below `KT_MIN_PROFIT_USD`, AND the contract
  reverts on-chain if realised surplus < `minProfit` — a stale/optimistic quote can't lose money.
- **Chunking**: large or thin exits are auto-split so slippage never eats the bonus.
- **Kill-switch + dedup**: bounded daily gas, stop on repeated reverts, no re-firing a target.
- **No secrets in the repo**: `.gitignore` excludes keys/env; alerts and logs never print the key.

## Push-ready
Local git history only (clean logical commits). No remote is set — add yours and push when ready:
```bash
git remote add origin git@github.com:<you>/katana-liquidator.git && git push -u origin main
```
