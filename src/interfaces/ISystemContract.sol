// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

import { IProxyLedger } from "./IProxyLedger.sol";

/// @dev Wildcard topic value for event subscriptions. Copied from `reactive-lib-omni` at `3ade0dc`, `src/base/AbstractReactive.sol`.
uint256 constant REACTIVE_IGNORE = 0xa65f96fc951c35ead38878e0f0b7a3c744a6f5ccc1476b313353ce31712313ad;

/// @title The system contract of Reactive Network (Lasna).
/// @notice Only the members that Lifeline uses. Functions and the callback struct are copied from
///         `reactive-lib-omni` at `3ade0dc` (`src/interfaces/ISubscriptionService.sol`,
///         `src/interfaces/ISystemContract.sol`). Events are copied from `system-smart-contracts`
///         at `72710d2` (`src/omni/SystemContract.sol`).
interface ISystemContract is IProxyLedger {
    /// @notice List of supported callback configuration versions.
    enum CallbackVersion {
        V_1_0
    }

    /// @notice Callback configuration struct for legacy-style callbacks.
    // forge-lint: disable-next-line(pascal-case-struct)
    struct CallbackConfiguration_V_1_0 {
        uint256 chainId;
        address recipient;
        uint64 gasLimit;
        bytes payload;
    }

    /// @notice An event requesting a new subscription from the network.
    event Subscribe(
        address indexed subscriber,
        uint256 indexed chainId,
        address indexed contractAddress,
        uint256 topic0,
        uint256 topic1,
        uint256 topic2,
        uint256 topic3
    );

    /// @notice An event requesting the removal of an existing subscription.
    event Unsubscribe(
        address indexed subscriber,
        uint256 indexed chainId,
        address indexed contractAddress,
        uint256 topic0,
        uint256 topic1,
        uint256 topic2,
        uint256 topic3
    );

    /// @notice Indicates a pending callback request received from a reactive contract.
    event CallbackRequest(
        uint256 indexed chainId_,
        address indexed sender_,
        address indexed recipient_,
        CallbackVersion version_,
        bytes configuration_
    );

    /// @notice Subscribes the calling contract to receive events matching the criteria specified.
    /// @param chainId_ EIP155 source chain ID for the event (as a `uint256`), or `0` for all chains.
    /// @param contract_ Contract address to monitor, or `0` for all contracts.
    /// @param topic0_ Topic 0 to monitor, or `REACTIVE_IGNORE` for all topics.
    /// @param topic1_ Topic 1 to monitor, or `REACTIVE_IGNORE` for all topics.
    /// @param topic2_ Topic 2 to monitor, or `REACTIVE_IGNORE` for all topics.
    /// @param topic3_ Topic 3 to monitor, or `REACTIVE_IGNORE` for all topics.
    function subscribe(
        uint256 chainId_,
        address contract_,
        uint256 topic0_,
        uint256 topic1_,
        uint256 topic2_,
        uint256 topic3_
    ) external;

    /// @notice Removes active subscription of the calling contract, matching the criteria specified, if one exists.
    function unsubscribe(
        uint256 chainId_,
        address contract_,
        uint256 topic0_,
        uint256 topic1_,
        uint256 topic2_,
        uint256 topic3_
    ) external;

    /// @notice Requests the posting of a legacy style callback to some destination network.
    /// @param config_ Callback configuration in V_1_0 format.
    // forge-lint: disable-next-line(mixed-case-function)
    function requestCallbackV_1_0(CallbackConfiguration_V_1_0 memory config_) external;
}
