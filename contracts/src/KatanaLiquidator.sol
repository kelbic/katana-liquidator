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

/// @notice Минимальный ERC-4626: выход из yv-вольтов идёт через redeem, а не через своп.
/// @notice Выход через ERC-4626, одним параметром: девятый адрес в liquidate() упирался в
/// «stack too deep», а включать via_ir ради двух полей — менять кодоген всего контракта.
struct VaultExit {
    address vault;       // 0 = коллатерал свопится как есть (поведение v1 бит-в-бит)
    address asset;       // ожидаемый asset() вольта; сверяется он-чейн в колбэке
}

interface IERC4626 {
    function asset() external view returns (address);
    function redeem(uint256 shares, address receiver, address owner) external returns (uint256);
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
        // v2: ERC-4626-коллатерал (yvvbUSDC/yvvbUSDT). У долей вольта НЕТ пула на Sushi —
        // маршрут не существует в принципе, и такие рынки были для нас мертвы ($42.7k приза
        // на 30.07). Выход: redeem доли -> базовый токен -> обычный своп. vault=0 =
        // прежнее поведение бит-в-бит.
        address vault;
        address vaultAsset;       // ожидаемый asset() вольта; сверяется он-чейн
    }

    error NotOwner();
    error NotMorpho();
    error Reentrant();
    error SwapFailed();
    error VaultAssetMismatch();
    error RedeemFailed();
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
        uint256 minProfit,
        VaultExit calldata ve
    ) external onlyOwner nonReentrant returns (uint256 profit) {
        bytes memory data = abi.encode(
            SwapData({
                swapTarget: swapTarget,
                swapCallData: swapCallData,
                loanToken: mp.loanToken,
                collateralToken: mp.collateralToken,
                vault: ve.vault,
                vaultAsset: ve.asset
            })
        );

        uint256 balBefore = IERC20(mp.loanToken).balanceOf(address(this));
        (uint256 seized, uint256 repaid) =
            IMorpho(MORPHO).liquidate(mp, borrower, seizedAssets, repaidShares, data);
        uint256 balAfter = IERC20(mp.loanToken).balanceOf(address(this));

        profit = balAfter - balBefore;
        if (profit < minProfit) revert ProfitTooLow(profit, minProfit);
        _safeTransfer(mp.loanToken, owner, balAfter); // sweep everything (incl. any prior dust)
        _sweepLeftovers(mp.collateralToken, ve.asset, mp.loanToken);
        emit Liquidated(borrower, mp.loanToken, profit, seized, repaid);
    }

    /// @notice Morpho callback: collateral already received; swap it to loanToken and ensure the
    /// contract can cover `repaidAssets` (Morpho pulls it right after this returns).
    function onMorphoLiquidate(uint256 repaidAssets, bytes calldata data) external {
        if (msg.sender != MORPHO) revert NotMorpho();
        SwapData memory s = abi.decode(data, (SwapData));

        address tokenIn = s.collateralToken;
        uint256 amountIn = IERC20(s.collateralToken).balanceOf(address(this));

        // v2: ERC-4626-коллатерал выходит через redeem, а не через своп — у долей вольта нет
        // пула. asset() сверяется он-чейн: подставить чужой vaultAsset в calldata нельзя.
        if (s.vault != address(0)) {
            if (IERC4626(s.vault).asset() != s.vaultAsset) revert VaultAssetMismatch();
            amountIn = IERC4626(s.vault).redeem(amountIn, address(this), address(this));
            if (amountIn == 0) revert RedeemFailed();
            tokenIn = s.vaultAsset;
        }

        // База вольта МОЖЕТ совпасть с займом (yvvbUSDC -> vbUSDC при займе vbUSDC): тогда
        // свопа нет вовсе — ни маршрута, ни проскальзывания, ни газа на роутер.
        if (tokenIn != s.loanToken) {
            _forceApprove(tokenIn, s.swapTarget, amountIn);
            (bool ok, ) = s.swapTarget.call(s.swapCallData);
            if (!ok) revert SwapFailed();
            _forceApprove(tokenIn, s.swapTarget, 0); // drop dangling allowance
        }

        if (IERC20(s.loanToken).balanceOf(address(this)) < repaidAssets) revert CannotRepay();
        _forceApprove(s.loanToken, MORPHO, repaidAssets); // Morpho pulls exactly this next
    }


    /// @dev Подмести остатки. Вынесено из liquidate() не ради красоты: с девятым параметром
    /// кадр функции упирался в «stack too deep», а via_ir поменял бы кодоген всего контракта.
    ///  * дребезг коллатерала — haircut входа свопа оставляет ~0.3% seize каждую ликвидацию;
    ///  * базовый токен вольта — остаток redeem, если он не совпал с займом.
    /// Неподметённое молча копится как нехеджированный остаток.
    function _sweepLeftovers(address coll, address vaultAsset, address loan) private {
        uint256 left = IERC20(coll).balanceOf(address(this));
        if (left != 0) _safeTransfer(coll, owner, left);
        if (vaultAsset != address(0) && vaultAsset != loan && vaultAsset != coll) {
            left = IERC20(vaultAsset).balanceOf(address(this));
            if (left != 0) _safeTransfer(vaultAsset, owner, left);
        }
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
