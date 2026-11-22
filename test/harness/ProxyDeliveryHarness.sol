// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

import { Test, Vm } from "forge-std/Test.sol";
import { ICallbackProxy, IProxyLedger } from "../../src/interfaces/IProxyLedger.sol";

/// @title Delivery harness for the callback proxy of Base Sepolia.
/// @notice The harness forks Base Sepolia at a pinned block, so the callback proxy is the real deployed
///         code. It emulates only the node: it delivers a callback through the real `deliverCallback`, from
///         an authorized delivery account of the proxy.
///
///         Rules of the emulation:
///         - One delivery is one transaction in its own block. Its gas limit is the gas limit of the
///           callback, as in the delivery transactions on the chain. The intrinsic gas of that transaction
///           is taken from the gas limit.
///         - The harness overwrites the first argument of the payload, as the node does. The caller gives
///           the value: the identity of the requester.
///         - The transaction origin is the delivery account, so the proxy pays the fee to that account.
///
///         A test must use `_mine` and `_skipTime` to move the block number and the time.
///
///         This contract has each member of the harness but the name `BLOCK_TIME`, so that `TwoForkHarness`
///         can inherit it together with `LasnaForkHarness`. The functions for accounts, blocks, time and
///         delivery are virtual for the same reason. A test of one fork inherits `ProxyDeliveryHarness`.
abstract contract ProxyDelivery is Test {
    // ------------------------------------------------------------------------------------------
    // Pinned values
    // ------------------------------------------------------------------------------------------

    uint256 internal constant BASE_SEPOLIA_CHAIN_ID = 84532;

    /// @dev The block of the fork rehearsal.
    uint256 internal constant BASE_SEPOLIA_FORK_BLOCK = 61_846_945;

    address internal constant PROXY_ADDRESS = 0x476F44b11F5C942c58f4c8E3d0300E5710743585;

    /// @dev The sender of the delivery transactions on the chain, for example
    ///      `0x2cf3d197e6b780ef454f3f3a3fdefd9124056a736fc1b7fbc952183c2a65372d` in block 61,845,799. The
    ///      harness reads `_operators` of this account from the chain.
    address internal constant DELIVERY_ACCOUNT = 0xF7E866D726658F6C3F14B8EE537fe268a4D91DDb;

    /// @dev The values of the deployed proxy. The harness reads each value from the chain and compares it
    ///      with this one.
    uint256 internal constant PROXY_EXTRA_GAS = 80_000;
    uint256 internal constant PROXY_MAX_CHARGE_GAS = 50_000;
    uint256 internal constant PROXY_GAS_PRICE_COEFF_PER_1000 = 1100;

    /// @dev The gas price of the delivery transactions: the base fee of the pinned block, 0.005 gwei.
    uint256 internal constant DELIVERY_GAS_PRICE = 5_000_000;

    /// @dev The gas limit of a delivery that a test does not measure. Delivery transactions on the chain
    ///      have this value.
    uint64 internal constant DELIVERY_GAS = 1_000_000;

    /// @dev Seconds for each block.
    uint256 internal constant DESTINATION_BLOCK_TIME = 2;

    // ------------------------------------------------------------------------------------------
    // Types and state
    // ------------------------------------------------------------------------------------------

    /// @notice The result of one delivery transaction.
    struct Delivery {
        bool delivered; // the delivery transaction did not revert
        bytes revertData; // the revert data of the transaction, if it reverted
        bool callbackFailed; // the proxy emitted `CallbackFailure`: the call to the recipient reverted
        uint256 fee; // the fee that the proxy charged: the payment to the transaction origin
        uint256 gasUsed; // the gas of the transaction, with the intrinsic gas
        Vm.Log[] logs; // the logs of the transaction
    }

    ICallbackProxy internal proxy = ICallbackProxy(PROXY_ADDRESS);

    // ------------------------------------------------------------------------------------------
    // Setup
    // ------------------------------------------------------------------------------------------

    /// @dev Forks Base Sepolia at the pinned block. Call it first in `setUp`.
    function _forkBaseSepolia() internal {
        // The endpoint `base-sepolia` of `foundry.toml` is the environment variable `BASE_SEPOLIA_RPC_URL`.
        vm.createSelectFork("base-sepolia", BASE_SEPOLIA_FORK_BLOCK);
        assertEq(block.chainid, BASE_SEPOLIA_CHAIN_ID, "the RPC endpoint is not Base Sepolia");
        assertGt(PROXY_ADDRESS.code.length, 0, "no callback proxy at the pinned block");

        assertTrue(proxy._operators(DELIVERY_ACCOUNT), "the delivery account is not authorized at the pinned block");
        assertEq(proxy._extraGas(), PROXY_EXTRA_GAS, "the extra gas differs from the pinned value");
        assertEq(proxy._maxChargeGas(), PROXY_MAX_CHARGE_GAS, "the charge gas differs from the pinned value");
        assertEq(
            proxy._gasPriceCoeffPer1000(),
            PROXY_GAS_PRICE_COEFF_PER_1000,
            "the fee coefficient differs from the pinned value"
        );
        assertEq(block.basefee, DELIVERY_GAS_PRICE, "the base fee differs from the pinned value");

        vm.txGasPrice(DELIVERY_GAS_PRICE);
        vm.recordLogs();
    }

    /// @dev Returns an address that has no balance, no nonce, no code and no ledger entry on the fork.
    ///      Default test accounts can hold real testnet funds on a fork.
    function _freshAccount(string memory label) internal virtual returns (address account) {
        account = makeAddr(string.concat("lifeline.proxy-harness.", label));
        _assertFresh(account);
    }

    /// @dev Fails if the address was used on the forked chain.
    function _assertFresh(address account) internal view virtual {
        assertEq(account.balance, 0, "the address has a balance on the fork");
        assertEq(account.code.length, 0, "the address has code on the fork");
        assertEq(vm.getNonce(account), 0, "the address has a nonce on the fork");
        assertEq(proxy.reserves(account), 0, "the address has reserves on the fork");
        assertEq(proxy.debt(account), 0, "the address has a debt on the fork");
    }

    /// @dev The address of the next contract that `deployer` creates. Fails if that address is not fresh.
    function _nextFreshContract(address deployer) internal view virtual returns (address created) {
        created = vm.computeCreateAddress(deployer, vm.getNonce(deployer));
        _assertFresh(created);
    }

    // ------------------------------------------------------------------------------------------
    // Blocks and time
    // ------------------------------------------------------------------------------------------

    /// @dev Moves `blocks` blocks forward.
    function _mine(uint256 blocks) internal virtual {
        _skipTime(blocks * DESTINATION_BLOCK_TIME);
    }

    /// @dev Moves `secs` seconds forward, with the matching number of blocks.
    function _skipTime(uint256 secs) internal virtual {
        vm.roll(vm.getBlockNumber() + (secs + DESTINATION_BLOCK_TIME - 1) / DESTINATION_BLOCK_TIME);
        vm.warp(vm.getBlockTimestamp() + secs);
    }

    // ------------------------------------------------------------------------------------------
    // Delivery
    // ------------------------------------------------------------------------------------------

    /// @notice Delivers a callback through the real `deliverCallback`, from the delivery account.
    /// @dev The logs that the EVM recorded before the delivery are dropped: take them with `_takeLogs` first.
    /// @param recipient The callback recipient.
    /// @param payload The payload that the requester gave. It has a selector and a first argument.
    /// @param gasLimit The gas limit of the callback: the gas limit of the delivery transaction.
    /// @param firstArgument The value that the node writes into the first argument: the requester.
    function _deliver(address recipient, bytes memory payload, uint64 gasLimit, address firstArgument)
        internal
        virtual
        returns (Delivery memory result)
    {
        _mine(1);
        bytes memory callData = abi.encodeCall(
            ICallbackProxy.deliverCallback,
            (
                ICallbackProxy.CallbackVersion.V_1_0,
                abi.encode(
                    ICallbackProxy.CallbackConfiguration_V_1_0({
                        chainId: BASE_SEPOLIA_CHAIN_ID,
                        recipient: recipient,
                        gasLimit: gasLimit,
                        payload: _withFirstArgument(payload, firstArgument)
                    })
                )
            )
        );
        uint256 intrinsic = _deliveryIntrinsicGas(callData);
        require(gasLimit > intrinsic, "ProxyDeliveryHarness: the gas limit is below the intrinsic gas");
        uint256 feeBefore = DELIVERY_ACCOUNT.balance;
        vm.getRecordedLogs();

        vm.prank(DELIVERY_ACCOUNT, DELIVERY_ACCOUNT);
        uint256 gasBefore = gasleft();
        (bool delivered, bytes memory returnData) = PROXY_ADDRESS.call{ gas: gasLimit - intrinsic }(callData);
        uint256 gasAfter = gasleft();

        result.delivered = delivered;
        if (!delivered) result.revertData = returnData;
        result.fee = DELIVERY_ACCOUNT.balance - feeBefore;
        result.gasUsed = intrinsic + gasBefore - gasAfter;
        result.logs = vm.getRecordedLogs();
        result.callbackFailed = _countLogs(result.logs, PROXY_ADDRESS, ICallbackProxy.CallbackFailure.selector) != 0;
    }

    /// @dev A copy of the payload with the first argument overwritten: the 32 bytes after the selector.
    function _withFirstArgument(bytes memory payload, address firstArgument)
        internal
        pure
        returns (bytes memory injected)
    {
        require(payload.length >= 36, "ProxyDeliveryHarness: the payload has no first argument");
        injected = bytes.concat(payload);
        assembly ("memory-safe") {
            mstore(add(injected, 36), firstArgument)
        }
    }

    /// @dev The intrinsic gas of a transaction with this calldata.
    function _deliveryIntrinsicGas(bytes memory callData) private pure returns (uint256 gas) {
        gas = 21_000;
        for (uint256 i = 0; i < callData.length; ++i) {
            gas += callData[i] == 0 ? 4 : 16;
        }
    }

    // ------------------------------------------------------------------------------------------
    // Logs
    // ------------------------------------------------------------------------------------------

    /// @notice The logs that the EVM recorded after the last delivery or the last call of this function.
    function _takeLogs() internal view returns (Vm.Log[] memory) {
        return vm.getRecordedLogs();
    }

    /// @notice The number of logs of one emitter and one `topic0`.
    function _countLogs(Vm.Log[] memory logs, address emitter, bytes32 topic0) internal pure returns (uint256 count) {
        for (uint256 i = 0; i < logs.length; ++i) {
            if (logs[i].emitter == emitter && logs[i].topics.length != 0 && logs[i].topics[0] == topic0) ++count;
        }
    }

    /// @notice The `n`-th log (from 0) of one emitter and one `topic0`. Fails if there is none.
    function _findLog(Vm.Log[] memory logs, address emitter, bytes32 topic0, uint256 n)
        internal
        pure
        returns (Vm.Log memory)
    {
        for (uint256 i = 0; i < logs.length; ++i) {
            if (logs[i].emitter == emitter && logs[i].topics.length != 0 && logs[i].topics[0] == topic0) {
                if (n == 0) return logs[i];
                --n;
            }
        }
        revert("ProxyDeliveryHarness: the log was not found");
    }

    /// @notice The one log of one emitter and one `topic0`. Fails if there is none or more than one.
    function _onlyLog(Vm.Log[] memory logs, address emitter, bytes32 topic0) internal pure returns (Vm.Log memory) {
        assertEq(_countLogs(logs, emitter, topic0), 1, "ProxyDeliveryHarness: not exactly one log");
        return _findLog(logs, emitter, topic0, 0);
    }

    /// @notice True if the logs hold `BlacklistContract(account)` of the proxy: a debt started.
    function _wentIntoDebt(Vm.Log[] memory logs, address account) internal pure returns (bool) {
        return _hasLedgerLog(logs, IProxyLedger.BlacklistContract.selector, account);
    }

    /// @notice True if the logs hold `WhitelistContract(account)` of the proxy: a deposit cleared a debt.
    function _debtCleared(Vm.Log[] memory logs, address account) internal pure returns (bool) {
        return _hasLedgerLog(logs, IProxyLedger.WhitelistContract.selector, account);
    }

    function _hasLedgerLog(Vm.Log[] memory logs, bytes32 topic0, address account) private pure returns (bool) {
        for (uint256 i = 0; i < logs.length; ++i) {
            if (
                logs[i].emitter == PROXY_ADDRESS && logs[i].topics.length == 2 && logs[i].topics[0] == topic0
                    && logs[i].topics[1] == bytes32(uint256(uint160(account)))
            ) return true;
        }
        return false;
    }
}

/// @title Delivery harness for the tests of one fork: `ProxyDelivery` with the name `BLOCK_TIME`.
abstract contract ProxyDeliveryHarness is ProxyDelivery {
    /// @dev Seconds for each block.
    uint256 internal constant BLOCK_TIME = DESTINATION_BLOCK_TIME;
}
