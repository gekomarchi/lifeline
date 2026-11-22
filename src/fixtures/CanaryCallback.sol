// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

/// @title Test fixture: a callback contract that does one cheap action for each callback.
/// @notice It has no `pay()`, holds no balance and has no access check, so an unfunded callback always
///         becomes a debt at the callback proxy.
contract CanaryCallback {
    event Pong(uint256 indexed n);

    /// @notice The callback: emits `Pong(n)`.
    /// @param sender The first callback argument: the node overwrites it. The canary does not read it.
    function pong(address sender, uint256 n) external {
        sender;
        emit Pong(n);
    }
}
