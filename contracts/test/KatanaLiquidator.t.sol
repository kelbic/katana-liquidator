// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

import {Test, console} from "forge-std/Test.sol";
import {KatanaLiquidator, MarketParams} from "../src/KatanaLiquidator.sol";

struct Market {
    uint128 totalSupplyAssets; uint128 totalSupplyShares;
    uint128 totalBorrowAssets; uint128 totalBorrowShares;
    uint128 lastUpdate; uint128 fee;
}

interface IMorphoFull {
    function owner() external view returns (address);
    function enableIrm(address irm) external;
    function enableLltv(uint256 lltv) external;
    function createMarket(MarketParams memory mp) external;
    function supply(MarketParams memory mp, uint256 assets, uint256 shares, address onBehalf, bytes memory data) external returns (uint256, uint256);
    function supplyCollateral(MarketParams memory mp, uint256 assets, address onBehalf, bytes memory data) external;
    function borrow(MarketParams memory mp, uint256 assets, uint256 shares, address onBehalf, address receiver) external returns (uint256, uint256);
    function position(bytes32 id, address user) external view returns (uint256 supplyShares, uint128 borrowShares, uint128 collateral);
}

interface IERC20Min {
    function balanceOf(address) external view returns (uint256);
    function approve(address, uint256) external returns (bool);
}

contract MockERC20 {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    function mint(address to, uint256 amt) external { balanceOf[to] += amt; }
    function approve(address sp, uint256 amt) external returns (bool) { allowance[msg.sender][sp] = amt; return true; }
    function transfer(address to, uint256 amt) external returns (bool) { balanceOf[msg.sender] -= amt; balanceOf[to] += amt; return true; }
    function transferFrom(address f, address t, uint256 amt) external returns (bool) {
        uint256 a = allowance[f][msg.sender];
        if (a != type(uint256).max) allowance[f][msg.sender] = a - amt;
        balanceOf[f] -= amt; balanceOf[t] += amt; return true;
    }
}

contract MockOracle {
    uint256 public price;
    function setPrice(uint256 p) external { price = p; }
}

contract MockIrm {
    function borrowRate(MarketParams memory, Market memory) external pure returns (uint256) { return 0; }
    function borrowRateView(MarketParams memory, Market memory) external pure returns (uint256) { return 0; }
}

/// @dev Mock aggregator standing in for the Sushi RouteProcessor in the deterministic test:
/// pulls all collateral from the caller and pays out loanToken at a fixed rate.
contract MockSwapper {
    MockERC20 public coll; MockERC20 public loan; uint256 public rate; // loan per coll, 1e18
    constructor(MockERC20 c, MockERC20 l, uint256 r) { coll = c; loan = l; rate = r; }
    function swapAll() external {
        uint256 amountIn = coll.balanceOf(msg.sender);
        coll.transferFrom(msg.sender, address(this), amountIn);
        loan.transfer(msg.sender, amountIn * rate / 1e18);
    }
}

/// @notice Thin standalone probe that reproduces EXACTLY what KatanaLiquidator.onMorphoLiquidate
/// does for the swap leg (force-approve the router, low-level call its calldata), so we can prove
/// the REAL Sushi RouteProcessor swap against REAL Katana pools in isolation. Output goes to the
/// `to` baked into the calldata (a fixed sink), which we assert on.
contract SwapProbe {
    function run(address token, address router, bytes calldata data) external {
        IERC20Min(token).approve(router, type(uint256).max);
        (bool ok, ) = router.call(data);
        require(ok, "sushi swap failed");
    }
}

/// @dev Forks Katana mainnet to run against the REAL Morpho Blue deployment
/// (0xD50F2Dff…, verified on-chain), then spins up a fresh mock market so the liquidation is
/// deterministic. Proves the executor's seize->swap->repay->sweep path and the minProfit gate
/// against the real Morpho.liquidate logic.
///   Run: KATANA_RPC_URL=https://rpc.katana.network forge test -vv
/// Skips automatically (green) when KATANA_RPC_URL is unset.
contract KatanaLiquidatorForkTest is Test {
    address constant MORPHO = 0xD50F2DffFd62f94Ee4AEd9ca05C61d0753268aBc; // Katana Morpho Blue
    uint256 constant LLTV = 0.86e18;
    IMorphoFull morpho = IMorphoFull(MORPHO);
    MockERC20 loan; MockERC20 coll; MockOracle oracle; MockIrm irm; MockSwapper swapper;
    KatanaLiquidator liq;
    MarketParams mp; bytes32 id;
    address borrower = address(0xB0B);
    bool forked;

    function setUp() public {
        try vm.envString("KATANA_RPC_URL") returns (string memory url) {
            if (bytes(url).length == 0) return;
            vm.createSelectFork(url);
            forked = true;
        } catch {
            return; // no RPC — tests below early-return so the suite stays green offline
        }
        loan = new MockERC20(); coll = new MockERC20(); oracle = new MockOracle(); irm = new MockIrm();

        address mOwner = morpho.owner();
        vm.startPrank(mOwner);
        try morpho.enableIrm(address(irm)) {} catch {}
        try morpho.enableLltv(LLTV) {} catch {}
        vm.stopPrank();

        mp = MarketParams({loanToken: address(loan), collateralToken: address(coll), oracle: address(oracle), irm: address(irm), lltv: LLTV});
        id = keccak256(abi.encode(mp));

        oracle.setPrice(2e36);                 // 1 coll = 2 loan
        morpho.createMarket(mp);

        loan.mint(address(this), 1_000_000e18);
        loan.approve(MORPHO, type(uint256).max);
        morpho.supply(mp, 500_000e18, 0, address(this), "");

        coll.mint(borrower, 100e18);
        vm.startPrank(borrower);
        coll.approve(MORPHO, type(uint256).max);
        morpho.supplyCollateral(mp, 100e18, borrower, "");
        morpho.borrow(mp, 170e18, 0, borrower, borrower);   // max 172 at price 2, lltv 0.86
        vm.stopPrank();

        liq = new KatanaLiquidator(MORPHO);     // owner = this
        swapper = new MockSwapper(coll, loan, 1.8e18);  // swap at post-drop price
        loan.mint(address(swapper), 1_000_000e18);
    }

    /// Full happy path against real Morpho: price drop -> underwater -> liquidate -> mock swap ->
    /// repay -> profit swept to owner, debt cleared.
    function testLiquidate() public {
        if (!forked) return;
        oracle.setPrice(1.8e36);               // 100 coll = 180 loan, max 154.8 < 170 -> liquidatable
        (, uint128 borrowShares,) = morpho.position(id, borrower);
        assertGt(borrowShares, 0, "no debt");

        uint256 ownerBefore = loan.balanceOf(address(this));
        bytes memory swapData = abi.encodeWithSelector(MockSwapper.swapAll.selector);
        uint256 profit = liq.liquidate(mp, borrower, uint256(borrowShares), address(swapper), swapData, 0);

        (, uint128 sharesAfter,) = morpho.position(id, borrower);
        console.log("profit (loan wei):", profit);
        assertGt(profit, 0, "no profit");
        assertEq(sharesAfter, 0, "debt not cleared");
        assertEq(loan.balanceOf(address(this)) - ownerBefore, profit, "profit not swept");
    }

    /// The minProfit gate must revert the whole tx when the realised surplus is below the floor
    /// (this is the on-chain slippage protection — the second safety layer under the off-chain net).
    function testRevertsIfMinProfitTooHigh() public {
        if (!forked) return;
        oracle.setPrice(1.8e36);
        (, uint128 borrowShares,) = morpho.position(id, borrower);
        bytes memory swapData = abi.encodeWithSelector(MockSwapper.swapAll.selector);
        vm.expectRevert();                     // ProfitTooLow
        liq.liquidate(mp, borrower, uint256(borrowShares), address(swapper), swapData, 1_000_000e18);
    }

    function testOnlyOwnerAndOnlyMorpho() public {
        if (!forked) return;
        bytes memory swapData = "";
        vm.prank(address(0xBAD));
        vm.expectRevert(KatanaLiquidator.NotOwner.selector);
        liq.liquidate(mp, borrower, 1, address(swapper), swapData, 0);

        vm.prank(address(0xBAD));
        vm.expectRevert(KatanaLiquidator.NotMorpho.selector);
        liq.onMorphoLiquidate(1, "");
    }
}

/// @dev Proves the REAL Sushi RouteProcessor swap (the leg the mock test stubs out) against REAL
/// Katana pools. Deals real vbWBTC to a probe, runs the exact approve+call the callback uses with
/// live Sushi calldata, and asserts the fixed sink received the expected vbUSDC.
/// Driven by run_fork_test.sh, which fetches a fresh quote (pool state drifts) and exports:
///   SUSHI_CALLDATA  — tx.data from api.sushi.com/swap/v7/747474 (to = SUSHI_SINK)
///   SEIZE_WEI       — vbWBTC amountIn (8 dec) baked into the calldata
///   SUSHI_MIN_OUT   — assumedAmountOut * (1 - slippage) in vbUSDC wei (6 dec)
/// Skips (green) when SUSHI_CALLDATA is unset.
contract SushiRealSwapForkTest is Test {
    address constant VBWBTC = 0x0913DA6Da4b42f538B445599b46Bb4622342Cf52;
    address constant VBUSDC = 0x203A662b0BD271A6ed5a60EdFbd04bFce608FD36;
    address constant ROUTE_PROCESSOR = 0xAC4c6e212A361c968F1725b4d055b47E63F80b75;
    address constant SUSHI_SINK = 0x000000000000000000000000000000000000bEEF; // matches run_fork_test.sh --to

    function test_realSushiSwap() public {
        bytes memory data;
        try vm.envBytes("SUSHI_CALLDATA") returns (bytes memory d) { data = d; } catch { return; }
        if (data.length == 0) return;
        uint256 seizeWei = vm.envUint("SEIZE_WEI");
        uint256 minOut = vm.envUint("SUSHI_MIN_OUT");

        vm.createSelectFork(vm.envString("KATANA_RPC_URL"));
        SwapProbe probe = new SwapProbe();
        deal(VBWBTC, address(probe), seizeWei);            // real vbWBTC into the probe
        assertEq(IERC20Min(VBWBTC).balanceOf(address(probe)), seizeWei, "deal failed");

        uint256 sinkBefore = IERC20Min(VBUSDC).balanceOf(SUSHI_SINK);
        probe.run(VBWBTC, ROUTE_PROCESSOR, data);          // real RouteProcessor swap
        uint256 got = IERC20Min(VBUSDC).balanceOf(SUSHI_SINK) - sinkBefore;

        console.log("real Sushi swap out (vbUSDC 6dec):", got);
        console.log("min acceptable:", minOut);
        assertGe(got, minOut, "sushi swap out below floor");
    }
}
