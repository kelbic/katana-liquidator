// SPDX-License-Identifier: MIT
pragma solidity 0.8.23;

import {Script, console} from "forge-std/Script.sol";
import {KatanaLiquidator} from "../src/KatanaLiquidator.sol";

/// @notice Foundry deploy script (alternative to bot/deploy.sh's forge create).
///   KATANA_RPC_URL=https://rpc.katana.network \
///   forge script script/Deploy.s.sol:Deploy --rpc-url katana --broadcast --private-key $KEY
/// The owner is the broadcasting key. Morpho Blue on Katana is hardcoded (verified on-chain).
contract Deploy is Script {
    address constant MORPHO = 0xD50F2DffFd62f94Ee4AEd9ca05C61d0753268aBc;

    function run() external returns (KatanaLiquidator liq) {
        vm.startBroadcast();
        liq = new KatanaLiquidator(MORPHO);
        vm.stopBroadcast();
        console.log("KatanaLiquidator deployed:", address(liq));
        console.log("owner:", liq.owner());
        console.log("morpho:", liq.MORPHO());
    }
}
