"""Oracle-tx fingerprints for the mempool same-block backrun layer (bot/mempool.py).

The Morpho market oracle (row["oracle"], a Chainlink-pair wrapper like 0xB60F728…) reads its
price from underlying Chainlink OCR2 aggregators (BTC/USD, ETH/USD, LBTC/USD, USDC/USD) and a
RedStone push feed (weETH_FUNDAMENTAL). A liquidation window OPENS when one of those feeds
reprices — the deviation-driven BTC/ETH pushes are what flip our targets. Those pushes are
submitted by a rotating committee of transmitter EOAs and land as a tx whose `to` is an OCR
forwarder/aggregator; the aggregator then emits AnswerUpdated.

This module is the repo-side mirror of the read-only research census (…/jobs/d9c2c3f6/tmp/
infra_oracle_census.json + infra_oracle_feeds.json, verified 2026-07-17): the aggregator
addresses and the transmitter-EOA committees per feed, plus which feeds drive each of our six
Morpho markets. The mempool layer matches a PENDING tx's `to`/`from` against these to learn
(i) WHICH market is about to reprice and (ii) the tx's exact priority fee, before it lands.

Transmitter EOAs are SHARED across feeds (the same committee member signs BTC, ETH and LBTC
rounds), so a `from`-only match is ambiguous between feeds — it yields the UNION of every
market those feeds drive. That is intentional: the mempool layer degrades gracefully by arming
all hot targets in the candidate markets and letting the existing flip-check confirm the actual
price when it lands. An aggregator `to`-match is unambiguous (one feed).

Pure data + lookups; no network, no imports from the executor. Unit-tested in test_oracles.py.
"""
from __future__ import annotations

from analysis.protocols import MARKETS


def _lc(a: str) -> str:
    return a.lower()


# --- feeds: Chainlink OCR2 aggregator + its transmitter-EOA committee -----------------------
# aggregator + transmitters from infra_oracle_census.json (12h window, 2026-07-17). USDC/USD is
# a $1 peg that did not push in the window (empty committee) but still drives the quote leg of
# the USDC markets — kept so an (unlikely) USDC repricing is still recognised. weETH_FUNDAMENTAL
# is a RedStone push (no Chainlink AnswerUpdated / transmitter census); we match its aggregator
# address and degrade to arming the whole market on any tx touching it.
FEEDS: dict[str, dict] = {
    "BTC/USD": {
        "kind": "chainlink",
        "aggregator": "0x56ac2b1b78225d47993e8866795a34ad540a515c",
        "transmitters": {
            "0x9185d7aeabaa7a7edad2954536e1536bfde2f7e8",
            "0xcdfcf3e9cd2f012ce7cc324d5d2d9c9a184f6281",
            "0x25a5d0ace41195110b86196af29596a4f18d1f33",
            "0x7f333870a01566fac9b7207c0abd096c761914d6",
        },
    },
    "ETH/USD": {
        "kind": "chainlink",
        "aggregator": "0x47522e7273344f1016a1e67e496ddb4f77d852c9",
        "transmitters": {
            "0x3dc34a1b06256842259a33bf7c6c4f7cc1688872",
            "0x9185d7aeabaa7a7edad2954536e1536bfde2f7e8",
            "0xa5ab9b39bd68095ba0f8a7d348c7baa0729de1c9",
            "0xf5690ee572102ef19fa5d0452b71e395eaae3083",
            "0x55a526ffda984163afec30827bbb3f7d067c663b",
            "0xcdfcf3e9cd2f012ce7cc324d5d2d9c9a184f6281",
        },
    },
    "LBTC/USD": {
        "kind": "chainlink",
        "aggregator": "0xa3e7cf38e05f6ed4e9c96a477263c984e2e30326",
        "transmitters": {
            "0x3dc34a1b06256842259a33bf7c6c4f7cc1688872",
            "0x624e4d4f4f5f1ea78c09da905d51bec9924e8303",
            "0x25a5d0ace41195110b86196af29596a4f18d1f33",
            "0xa5ab9b39bd68095ba0f8a7d348c7baa0729de1c9",
            "0x55a526ffda984163afec30827bbb3f7d067c663b",
        },
    },
    "USDC/USD": {
        "kind": "chainlink",
        "aggregator": "0xa89e9c15935bfb49d0f11d0d2ecf6bb7800cbe97",
        "transmitters": set(),
    },
    # WBTC/BTC peg feed (BASE_FEED_1 of the vbWBTC markets) — slow, low-deviation, but a
    # reprice still shifts the WBTC markets; matched by aggregator only (no census committee).
    "WBTC/BTC": {
        "kind": "chainlink",
        "aggregator": "0x433c0516fae1a55e750d701c9f9031b2359bc647",
        "transmitters": set(),
    },
    "weETH_FUNDAMENTAL": {
        "kind": "redstone",
        "aggregator": "0xe8d9fbc10e00ecc9f0694617075fdaf657a76fb2",
        "transmitters": set(),
    },
}

# --- which feeds drive each market's Morpho oracle price (by pair name -> feed list) ---------
# base (volatile) feed first, then the quote leg. From infra_oracle_feeds.json BASE/QUOTE_FEED.
_MARKET_FEEDS_BY_PAIR: dict[str, list[str]] = {
    "vbWBTC/vbUSDC": ["BTC/USD", "WBTC/BTC", "USDC/USD"],
    "vbETH/vbUSDC": ["ETH/USD", "USDC/USD"],
    "vbWBTC/vbUSDT": ["BTC/USD", "WBTC/BTC"],
    "vbETH/vbUSDT": ["ETH/USD"],
    "LBTC/vbUSDC": ["LBTC/USD", "USDC/USD"],
    "weETH/vbETH": ["weETH_FUNDAMENTAL"],
}

# marketId (0x…, lower) -> [feed names]. Resolved against the protocols registry so the ids stay
# single-sourced (a market whose pair is absent from the registry is simply skipped).
MARKET_FEEDS: dict[str, list[str]] = {
    _lc(MARKETS[pair]["id"]): feeds
    for pair, feeds in _MARKET_FEEDS_BY_PAIR.items() if pair in MARKETS
}
_MARKET_PAIR: dict[str, str] = {_lc(MARKETS[pair]["id"]): pair for pair in _MARKET_FEEDS_BY_PAIR
                                if pair in MARKETS}

# --- reverse indexes (built once at import) -------------------------------------------------
# aggregator addr -> feed name (unambiguous)
AGG_TO_FEED: dict[str, str] = {_lc(f["aggregator"]): name for name, f in FEEDS.items()}
# transmitter EOA -> set of feed names it signs for (ambiguous — committees are shared)
TRANSMITTER_FEEDS: dict[str, set[str]] = {}
for _name, _f in FEEDS.items():
    for _eoa in _f["transmitters"]:
        TRANSMITTER_FEEDS.setdefault(_lc(_eoa), set()).add(_name)
# feed name -> set of marketIds it can reprice
FEED_MARKETS: dict[str, set[str]] = {}
for _mid, _feeds in MARKET_FEEDS.items():
    for _fn in _feeds:
        FEED_MARKETS.setdefault(_fn, set()).add(_mid)
# every address that fingerprints an oracle tx (fast membership pre-filter)
ORACLE_ADDRS: frozenset[str] = frozenset(AGG_TO_FEED) | frozenset(TRANSMITTER_FEEDS)


def market_pair(market_id: str) -> str:
    """Human pair label for a marketId (for logging); the raw id if unknown."""
    return _MARKET_PAIR.get(_lc(market_id), market_id[:10])


def is_oracle_tx(to: str | None, frm: str | None) -> bool:
    """Cheap pre-filter: does this pending tx touch any tracked oracle aggregator (`to`) or
    come from a tracked transmitter EOA (`from`)? Membership only — no market resolution."""
    return ((to or "").lower() in ORACLE_ADDRS) or ((frm or "").lower() in ORACLE_ADDRS)


def feeds_for_tx(to: str | None, frm: str | None) -> set[str]:
    """Feed names this pending tx is (un)ambiguously about: the aggregator it targets (`to`,
    unambiguous) union the feeds its sender signs for (`from`, may be several)."""
    feeds: set[str] = set()
    t, f = (to or "").lower(), (frm or "").lower()
    if t in AGG_TO_FEED:
        feeds.add(AGG_TO_FEED[t])
    if f in TRANSMITTER_FEEDS:
        feeds |= TRANSMITTER_FEEDS[f]
    return feeds


def markets_for_tx(to: str | None, frm: str | None) -> set[str]:
    """MarketIds this pending oracle tx could reprice — the union over feeds_for_tx(). Empty if
    the tx is not an oracle push we track. This is the same-block layer's 'which market is about
    to reprice' answer; a `from`-only (transmitter) match is deliberately broad (shared
    committees), so the caller arms all hot targets in the returned markets."""
    out: set[str] = set()
    for feed in feeds_for_tx(to, frm):
        out |= FEED_MARKETS.get(feed, set())
    return out


def tx_priority_fee_wei(tx: dict, base_fee_wei: int | None = None) -> int | None:
    """Effective priority fee (tip) of a tx, in wei — the value op-reth orders by, matched to
    the wei by the same-block backrun. type-2: min(maxPriorityFeePerGas, maxFeePerGas - base)
    when a base fee is known, else maxPriorityFeePerGas; legacy: gasPrice - base. A pending
    oracle push is type-2 with maxPriorityFeePerGas set to its tip. Returns None if unparseable.

    Katana base fee is pinned ~0.001 gwei; pass the latest head's base for the exact effective
    tip, or omit it to take maxPriorityFeePerGas directly (what the committee set)."""
    def _hex(v):
        if v is None:
            return None
        return int(v, 16) if isinstance(v, str) else int(v)
    try:
        mp = _hex(tx.get("maxPriorityFeePerGas"))
        if mp is not None:
            if base_fee_wei is not None:
                mf = _hex(tx.get("maxFeePerGas"))
                if mf is not None:
                    return max(0, min(mp, mf - base_fee_wei))
            return mp
        gp = _hex(tx.get("gasPrice"))
        if gp is not None:
            return max(0, gp - base_fee_wei) if base_fee_wei is not None else gp
    except (ValueError, TypeError):
        return None
    return None
