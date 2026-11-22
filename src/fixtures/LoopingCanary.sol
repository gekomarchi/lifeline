// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

import { IReactive } from "@reactive/src/interfaces/IReactive.sol";
import { ISystemContract, REACTIVE_IGNORE } from "../interfaces/ISystemContract.sol";

/// @title Test fixture: a broken reactive contract. Each reaction reverts, costs a fee and does no work.
/// @notice It has no `pay()` and holds no balance.
contract LoopingCanary {
    event Poke(uint256 indexed n);

    error NotOwner();
    error AlwaysReverts();

    address public immutable owner;

    uint256 private _pokes;

    constructor(address systemContract) {
        owner = msg.sender;
        ISystemContract(systemContract)
            .subscribe(
                block.chainid, address(this), uint256(Poke.selector), REACTIVE_IGNORE, REACTIVE_IGNORE, REACTIVE_IGNORE
            );
    }

    /// @notice Emits `Poke(n)`, n from 1. The node then calls `react` with that log.
    function poke() external {
        if (msg.sender != owner) revert NotOwner();
        emit Poke(++_pokes);
    }

    /// @notice The reaction always reverts.
    function react(IReactive.LogRecord calldata) external pure {
        revert AlwaysReverts();
    }
}
