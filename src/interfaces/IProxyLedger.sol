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

/// @title The callback proxy of a destination chain.
/// @notice Only the members that Lifeline and its delivery harness use. Each signature is copied from
///         `system-smart-contracts` at `72710d2`: `src/omni/CallbackProxy.sol` and
///         `src/omni/base/AbstractProxiedPayableBridge.sol`.
interface ICallbackProxy is IProxyLedger {
    /// @notice Supported callback versions. Copied from `reactive-lib-omni` at `3ade0dc`, `src/interfaces/ISystemContract.sol`.
    enum CallbackVersion {
        V_1_0
    }

    /// @notice Callback configuration struct for legacy-style callbacks. Copied from the same file.
    // forge-lint: disable-next-line(pascal-case-struct)
    struct CallbackConfiguration_V_1_0 {
        uint256 chainId;
        address recipient;
        uint64 gasLimit;
        bytes payload;
    }

    /// @notice Emitted when the call to the callback recipient reverted. The fee is still charged.
    /// @param target_ Target contract for the callback.
    /// @param payload_ Payload sent to the contract.
    event CallbackFailure(address indexed target_, bytes payload_);

    /// @notice Indicates that the message sender is not authorized to perform the operation.
    error NotAuthorized();

    /// @notice Indicates that the contract is currently in debt. The delivery reverts before the callback runs.
    /// @param contract_ Contract's address.
    /// @param debt_ Current debt.
    error InDebt(address contract_, uint256 debt_);

    /// @notice Indicates that a callback target address is not a contract.
    /// @param target_ Callback target address.
    error NotAContract(address target_);

    /// @notice Indicates that the allocated gas limit is too low to perform the operation.
    /// @param remaining_ Gas remaining at the point of checking.
    /// @param required_ Minimum gas required.
    error InsufficientGas(uint256 remaining_, uint256 required_);

    /// @notice Mapping listing authorized bridge operators: the delivery accounts of the node.
    function _operators(address operator_) external view returns (bool);

    /// @notice Default gas limit for transaction payments.
    function _maxChargeGas() external view returns (uint256);

    /// @notice Extra gas to be paid for when executing a transaction.
    function _extraGas() external view returns (uint256);

    /// @notice Gas price coefficient (in promille) when executing a transactions.
    function _gasPriceCoeffPer1000() external view returns (uint256);

    /// @notice Performs callback delivery on this network.
    /// @param version_ Version of the callback configuration used.
    /// @param config_ ABI-encoded callback configration in a format corresponding to the version specified.
    function deliverCallback(CallbackVersion version_, bytes memory config_) external;
}
