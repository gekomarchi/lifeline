// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

import { IReactive } from "@reactive/src/interfaces/IReactive.sol";
import { ISystemContract, REACTIVE_IGNORE } from "../interfaces/ISystemContract.sol";

/// @title Test fixture: a reactive contract that runs one cheap reaction for each `poke()`.
/// @notice It has no `pay()` and holds no balance, so an unfunded reaction always becomes a debt.
contract CanaryReactive {
    event Poke(uint256 indexed n);
    event Tick(uint256 indexed n);

    error NotOwner();
    error NotSystem(address caller);

    address public immutable owner;

    ISystemContract private immutable SYSTEM;
    uint256 private immutable CALLBACK_CHAIN_ID;
    address private immutable CALLBACK_RECIPIENT;
    uint64 private immutable CALLBACK_GAS_LIMIT;

    uint256 private _pokes;

    constructor(address systemContract, uint256 callbackChainId, address callbackRecipient, uint64 callbackGasLimit) {
        owner = msg.sender;
        SYSTEM = ISystemContract(systemContract);
        CALLBACK_CHAIN_ID = callbackChainId;
        CALLBACK_RECIPIENT = callbackRecipient;
        CALLBACK_GAS_LIMIT = callbackGasLimit;
        SYSTEM.subscribe(
            block.chainid, address(this), uint256(Poke.selector), REACTIVE_IGNORE, REACTIVE_IGNORE, REACTIVE_IGNORE
        );
    }

    /// @notice Emits `Poke(n)`, n from 1. The node then calls `react` with that log.
    function poke() external {
        if (msg.sender != owner) revert NotOwner();
        emit Poke(++_pokes);
    }

    /// @notice The reaction to `Poke(n)`: emits `Tick(n)` and, if a recipient is set, requests `pong(address(0), n)`.
    function react(IReactive.LogRecord calldata log) external {
        if (msg.sender != address(SYSTEM)) revert NotSystem(msg.sender);
        if (
            log.chainId != block.chainid || log.contractAddress != address(this) || log.topic0 != uint256(Poke.selector)
        ) return;

        uint256 n = log.topic1;
        emit Tick(n);

        if (CALLBACK_RECIPIENT != address(0)) {
            SYSTEM.requestCallbackV_1_0(
                ISystemContract.CallbackConfiguration_V_1_0({
                    chainId: CALLBACK_CHAIN_ID,
                    recipient: CALLBACK_RECIPIENT,
                    gasLimit: CALLBACK_GAS_LIMIT,
                    // The node overwrites the first argument with the identity of the requester.
                    payload: abi.encodeWithSignature("pong(address,uint256)", address(0), n)
                })
            );
        }
    }
}
