// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

/// @notice Morpho Blue market parameters (mirrors IMorpho.MarketParams).
struct MarketParams {
    address loanToken;
    address collateralToken;
    address oracle;
    address irm;
    uint256 lltv;
}

interface IMorpho {
    function liquidate(
        MarketParams memory marketParams,
        address borrower,
        uint256 seizedAssets,
        uint256 repaidShares,
        bytes memory data
    ) external returns (uint256 seized, uint256 repaid);
}

interface IERC20 {
    function balanceOf(address account) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
}

/// @title KatanaLiquidator — zero-capital Morpho Blue liquidations on Katana (chainId 747474).
/// @notice Direct port of the production Base/Monad executor (Katana is standard EVM; the
/// Morpho.sol liquidate callback flow is byte-identical). Flow:
///   1. liquidate() calls Morpho.liquidate with seizedAssets=0 and our repaidShares; Morpho
///      seizes the LIF-incentivized collateral to THIS contract.
///   2. Morpho invokes onMorphoLiquidate(repaidAssets, data); we swap the seized collateral to
///      loanToken via the Sushi RouteProcessor (generic calldata built off-chain by the bot).
///   3. Morpho pulls exactly `repaidAssets` of loanToken right after the callback returns.
/// No standing capital: the seized collateral funds repayment via the swap; the surplus (LIF
/// bonus minus swap slippage) is the profit, swept to the owner. The hot wallet holds only gas;
/// the contract holds no standing funds (not a honeypot).
///
/// vb-token note: on Katana the only ATOMIC collateral exit is Sushi (vb tokens are 1:1
/// redeemable to L1 but the bridge round-trip is not atomic). swapTarget is therefore the
/// Sushi RouteProcessor; swapCallData comes from api.sushi.com/swap/v7/747474.
///
/// Safety: swap-success + can-repay checks, minProfit gate (slippage protection), nonReentrant,
/// onlyOwner entry / onlyMorpho callback, return-data-checked ERC20 ops, market params passed as
/// arguments (never hardcoded), force-approve (USDT-style safe) with allowance reset.
contract KatanaLiquidator {
    address public immutable MORPHO;
    address public owner;
    uint256 private _locked = 1; // 1 = unlocked, 2 = locked (nonzero-init saves gas)

    /// @dev Swap context handed to the callback via Morpho's `data`.
    struct SwapData {
        address swapTarget;       // Sushi RouteProcessor (built off-chain by the bot)
        bytes swapCallData;
        address loanToken;
        address collateralToken;
    }

    error NotOwner();
    error NotMorpho();
    error Reentrant();
    error SwapFailed();
    error CannotRepay();
    error ProfitTooLow(uint256 got, uint256 min);
    error ERC20OpFailed();
    error ZeroAddress();

    event Liquidated(address indexed borrower, address indexed loanToken, uint256 profit,
                     uint256 seizedAssets, uint256 repaidAssets);
    event OwnerChanged(address indexed from, address indexed to);

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier nonReentrant() {
        if (_locked == 2) revert Reentrant();
        _locked = 2;
        _;
        _locked = 1;
    }

    constructor(address morpho) {
        MORPHO = morpho;
        owner = msg.sender;
    }

    function setOwner(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert ZeroAddress();
        emit OwnerChanged(owner, newOwner);
        owner = newOwner;
    }

    /// @notice Liquidate `borrower` on market `mp`, swapping the seized collateral to loanToken
    /// via `swapTarget`/`swapCallData`. Exactly ONE of `seizedAssets`/`repaidShares` must be
    /// nonzero (Morpho enforces this — same argument order as Morpho.liquidate):
    ///   * repaidShares mode — full/partial closes where debt is the binding side; seized is
    ///     derived by Morpho at execution price.
    ///   * seizedAssets mode — collateral-capped closes (deep underwater): we pin the seize and
    ///     Morpho derives repaid at execution price, so an adverse tick between scan and
    ///     inclusion can never underflow `position.collateral -= seizedAssets` (Panic 0x11).
    /// Reverts unless realized profit (swept to owner) >= `minProfit`. onlyOwner so swap
    /// calldata is always our own. Any collateral left over after the swap (input haircut,
    /// partial route) is swept to the owner too — nothing accrues in the contract.
    function liquidate(
        MarketParams calldata mp,
        address borrower,
        uint256 seizedAssets,
        uint256 repaidShares,
        address swapTarget,
        bytes calldata swapCallData,
        uint256 minProfit
    ) external onlyOwner nonReentrant returns (uint256 profit) {
        bytes memory data = abi.encode(
            SwapData({
                swapTarget: swapTarget,
                swapCallData: swapCallData,
                loanToken: mp.loanToken,
                collateralToken: mp.collateralToken
            })
        );

        uint256 balBefore = IERC20(mp.loanToken).balanceOf(address(this));
        (uint256 seized, uint256 repaid) =
            IMorpho(MORPHO).liquidate(mp, borrower, seizedAssets, repaidShares, data);
        uint256 balAfter = IERC20(mp.loanToken).balanceOf(address(this));

        profit = balAfter - balBefore;
        if (profit < minProfit) revert ProfitTooLow(profit, minProfit);
        _safeTransfer(mp.loanToken, owner, balAfter); // sweep everything (incl. any prior dust)
        // sweep collateral dust too (swap-input haircut leaves ~0.3% of the seize here every
        // liquidation — unswept it silently accrues as unhedged inventory)
        uint256 collLeft = IERC20(mp.collateralToken).balanceOf(address(this));
        if (collLeft != 0) _safeTransfer(mp.collateralToken, owner, collLeft);
        emit Liquidated(borrower, mp.loanToken, profit, seized, repaid);
    }

    /// @notice Morpho callback: collateral already received; swap it to loanToken and ensure the
    /// contract can cover `repaidAssets` (Morpho pulls it right after this returns).
    function onMorphoLiquidate(uint256 repaidAssets, bytes calldata data) external {
        if (msg.sender != MORPHO) revert NotMorpho();
        SwapData memory s = abi.decode(data, (SwapData));

        uint256 collBal = IERC20(s.collateralToken).balanceOf(address(this));
        _forceApprove(s.collateralToken, s.swapTarget, collBal);
        (bool ok, ) = s.swapTarget.call(s.swapCallData);
        if (!ok) revert SwapFailed();
        _forceApprove(s.collateralToken, s.swapTarget, 0); // drop dangling allowance

        if (IERC20(s.loanToken).balanceOf(address(this)) < repaidAssets) revert CannotRepay();
        _forceApprove(s.loanToken, MORPHO, repaidAssets); // Morpho pulls exactly this next
    }

    /// @notice Recover stuck tokens (dust collateral from a partial swap, airdrops) to owner.
    function sweep(address token) external onlyOwner {
        _safeTransfer(token, owner, IERC20(token).balanceOf(address(this)));
    }

    // --- return-data-checked ERC20 helpers (handle non-standard tokens) ---

    function _forceApprove(address token, address spender, uint256 amount) internal {
        _call(token, abi.encodeWithSelector(IERC20.approve.selector, spender, 0));
        if (amount != 0) {
            _call(token, abi.encodeWithSelector(IERC20.approve.selector, spender, amount));
        }
    }

    function _safeTransfer(address token, address to, uint256 amount) internal {
        _call(token, abi.encodeWithSelector(IERC20.transfer.selector, to, amount));
    }

    function _call(address token, bytes memory payload) private {
        (bool ok, bytes memory ret) = token.call(payload);
        if (!ok || (ret.length != 0 && !abi.decode(ret, (bool)))) revert ERC20OpFailed();
    }
}
