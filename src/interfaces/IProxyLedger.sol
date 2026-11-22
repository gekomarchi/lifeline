// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

/// @title The debt and reserve ledger of the system contract (Lasna) and of each callback proxy.
/// @notice Only the members that Lifeline uses. Each signature is copied from
///         `system-smart-contracts` at `72710d2`, `src/omni/base/AbstractProxiedPayableBridge.sol`.
interface IProxyLedger {
    /// @notice Requests a given contract to be blacklisted. Emitted only when a debt goes from zero to positive.
    /// @param callback_ Contract's address.
    event BlacklistContract(address indexed callback_);

    /// @notice Requests a given contract to be removed from the blacklist. Emitted when a deposit clears a debt.
    /// @param callback_ Contract's address.
    event WhitelistContract(address indexed callback_);

    /// @notice Indicates the payment failure on the part of the contract.
    /// @param contract_ Address of the contract in question.
    /// @param amount_ Requested payment amount.
    event PaymentFailure(address indexed contract_, uint256 indexed amount_);

    /// @notice Method for covering callback fee debts. The value pays the debt first; the rest becomes reserves.
    /// @param contract_ Address of the callback contract the balance of which should be updated.
    function depositTo(address contract_) external payable;

    /// @notice Returns the contract's current reserves.
    /// @param contract_ Address of the contract.
    function reserves(address contract_) external view returns (uint256 reserves_);

    /// @notice Returns the given contract's outstanding debt.
    /// @param contract_ Address of the contract.
    function debt(address contract_) external view returns (uint256 debt_);
}
