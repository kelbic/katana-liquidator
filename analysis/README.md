# analysis/ — read-only Katana Morpho tooling

Pure-stdlib, READ-ONLY (whitelisted `eth_*` reads only — cannot send a tx). Ported from the
monad-liquidator research toolkit; verified against Katana mainnet 2026-07-13.

- `keccak.py`   — offline Keccak-256 (selectors/topic0 never hand-pasted)
- `rpc.py`      — minimal JSON-RPC client, Katana endpoints, read-only method whitelist
- `multicall.py`— Multicall3.aggregate3 encoder/decoder (N reads -> 1 round-trip)
- `models.py`   — Morpho Blue math: LIF from LLTV, HF, shares->assets (mirrors Morpho.sol)
- `protocols.py`— verified Katana addresses (Morpho, Sushi RP), token + market registry, decoders
- `morpho_api.py`— Morpho indexer discovery: CURRENT near-edge borrowers (no getLogs-from-0 scan)
- `monitor.py`  — live position scanner: discover (indexer) -> exact on-chain HF -> flag HF<1 targets

Tests (stdlib, offline): `python3 -m analysis.test_keccak|test_models|test_protocols`
