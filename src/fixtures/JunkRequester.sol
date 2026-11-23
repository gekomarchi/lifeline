// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

import { ISystemContract } from "../interfaces/ISystemContract.sol";

/// @title Experiment fixture: requests one callback to any recipient from a plain transaction.
/// @notice The system contract accepts a callback request from any caller (specification F1), and the
///         callback proxy charges the recipient. The junk-callback scenario uses this contract against the
///         builder's own vault only: the driver checks the recipient, and only the owner can call `request`.
contract JunkRequester {
    error NotOwner();

    address public immutable owner;

    ISystemContract private immutable SYSTEM;

    constructor(address systemContract) {
        owner = msg.sender;
        SYSTEM = ISystemContract(systemContract);
    }

    /// @notice Requests one callback. The node overwrites the first argument of `payload` with the address
    ///         of this contract.
    /// @param chainId The chain ID of the recipient.
    /// @param recipient The contract that the proxy calls and charges.
    /// @param gasLimit The gas limit of the callback. The node ignores a request below 100,000.
    /// @param payload The call data of the callback: a selector and a first argument of type `address`.
    function request(uint256 chainId, address recipient, uint64 gasLimit, bytes calldata payload) external {
        if (msg.sender != owner) revert NotOwner();
        SYSTEM.requestCallbackV_1_0(
            ISystemContract.CallbackConfiguration_V_1_0({
                chainId: chainId, recipient: recipient, gasLimit: gasLimit, payload: payload
            })
        );
    }
}
