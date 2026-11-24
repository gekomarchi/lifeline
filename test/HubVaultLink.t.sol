// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

import { Vm } from "forge-std/Vm.sol";
import { console2 } from "forge-std/console2.sol";
import { TwoForkHarness, ISystemContractCallbacks } from "./harness/TwoForkHarness.sol";
import { LifelineHub, SWEEP_PER_TARGET, SWEEP_MAX, REQUEST_ID_BASE } from "../src/LifelineHub.sol";
import { LifelineVault } from "../src/LifelineVault.sol";
import { Policy, TargetKind, TargetState, InvalidPolicy } from "../src/LifelineTypes.sol";
import { ICallbackProxy, IProxyLedger } from "../src/interfaces/IProxyLedger.sol";
import { ISystemContract, REACTIVE_IGNORE } from "../src/interfaces/ISystemContract.sol";
import { CanaryCallback } from "../src/fixtures/CanaryCallback.sol";
import { CanaryReactive } from "../src/fixtures/CanaryReactive.sol";

/// @notice The hub with its two internal request functions open, for the tests of those seams.
contract HubSeams is LifelineHub {
    constructor(address systemContract) payable LifelineHub(systemContract, 0, 0, 0, 0) { }

    function requestVault(uint256 chainId, bytes memory payload, uint256 n) external returns (bool) {
        return _requestVault(chainId, payload, n);
    }

    function requestSweep(uint256 chainId, address[] memory targets, uint256 id) external returns (bool) {
        return _requestSweep(chainId, targets, id);
    }
}

/// @notice Fork tests of the link from the hub to a vault, on a Lasna fork and a Base Sepolia fork. The hub,
///         the vault, the canaries, the system contract and the callback proxy are real; only the node is
///         emulated by the harness.
/// @dev The tests that use `vm.mockCallRevert` replace one answer of the system contract, because the real
///      system contract never reverts a callback request.
contract HubVaultLinkTest is TwoForkHarness {
    // Lasna: the hub.
    uint256 internal constant HUB_VALUE = 5 ether;
    uint256 internal constant HUB_FEE_FLOAT = 0.5 ether;
    /// @dev Above the fees of the longest test: the float of the hub never runs out.
    uint256 internal constant HUB_SELF_CARE_DAILY_CAP = 4 ether;

    // Destination: the vault.
    uint256 internal constant PRINCIPAL = 0.01 ether;
    uint256 internal constant FEE_FLOAT = 1.5e13;
    uint256 internal constant SELF_CARE_DAILY_CAP = 3e13;
    uint256 internal constant PRINCIPAL_FLOOR = 0.001 ether;

    uint256 internal constant HIGH = 5e12;
    uint256 internal constant MAX_DEBT = 1e13;
    uint256 internal constant DAILY_CAP = 1e14;
    uint32 internal constant WINDOW = 1 hours;

    /// @dev `sweepBase` is `90,000 + _extraGas + _maxChargeGas` of the proxy.
    uint256 internal constant SWEEP_BASE_GAS = 90_000;
    uint256 internal constant BASE_SEPOLIA_SWEEP_BASE = 220_000;
    uint256 internal constant ETHEREUM_SEPOLIA_SWEEP_BASE = 290_000;

    /// @dev A chain ID that has no vault.
    uint256 internal constant CHAIN_WITHOUT_VAULT = 1234;

    address internal owner;
    address internal stranger; // a requester that is not the hub, and an account that is not the owner
    LifelineHub internal hub;
    LifelineVault internal vault;

    function setUp() public {
        _forkBoth();
        owner = _freshAccountOnBoth("owner");
        stranger = _freshAccountOnBoth("stranger");

        vm.deal(owner, 100 ether);
        vm.deal(stranger, 100 ether);
        _nextFreshContract(owner);
        vm.prank(owner);
        hub = new LifelineHub{ value: HUB_VALUE }(
            SYSTEM_ADDRESS, HUB_FEE_FLOAT, HUB_FEE_FLOAT, HUB_SELF_CARE_DAILY_CAP, 0
        );

        _toDestination();
        vm.deal(owner, 1 ether);
        vm.deal(stranger, 1 ether);
        // With the same nonce on both chains the vault would get the address of the hub.
        vm.setNonce(owner, 1);
        _nextFreshContract(owner);
        vm.prank(owner);
        vault = new LifelineVault{ value: PRINCIPAL + FEE_FLOAT }(
            PROXY_ADDRESS, address(hub), FEE_FLOAT, FEE_FLOAT, SELF_CARE_DAILY_CAP, PRINCIPAL_FLOOR
        );
        assertTrue(address(vault) != address(hub), "the hub and the vault have different addresses");
        assertEq(SWEEP_BASE_GAS + proxy._extraGas() + proxy._maxChargeGas(), BASE_SEPOLIA_SWEEP_BASE);

        _setVault(BASE_SEPOLIA_SWEEP_BASE);
    }

    // ------------------------------------------------------------------------------------------
    // Helpers: actions
    // ------------------------------------------------------------------------------------------

    struct ResultLog {
        uint256 id;
        address target;
        LifelineVault.Outcome outcome;
        uint256 paid;
    }

    /// @notice The values of one `VaultUp` of the hub.
    struct UpLog {
        uint256 requestId;
        uint256 sent;
        uint256 remaining;
    }

    function _setVault(uint256 sweepBase) internal {
        _toLasna();
        vm.prank(owner);
        hub.setVault(DESTINATION_CHAIN_ID, PROXY_ADDRESS, address(vault), sweepBase);
    }

    /// @dev `low` is 0, so `setPolicy` does not fund a target that has no debt.
    function _policy() internal pure returns (Policy memory) {
        return Policy({ low: 0, high: HIGH, maxDebt: MAX_DEBT, dailyCap: DAILY_CAP, maxRescues: 2, window: WINDOW });
    }

    function _policy(uint32 maxRescues) internal pure returns (Policy memory policy) {
        policy = _policy();
        policy.maxRescues = maxRescues;
    }

    /// @dev An unfunded callback canary on the destination: no balance, no reserves, no debt.
    function _deployCanary() internal returns (address canary) {
        _toDestination();
        _nextFreshContract(owner);
        vm.prank(owner);
        canary = address(new CanaryCallback());
    }

    /// @dev A reactive canary on Lasna that requests `pong` on a callback canary. It has reserves, so its
    ///      own reactions leave no debt.
    function _deployReactive(address recipient, uint256 chainId, uint64 gasLimit)
        internal
        returns (CanaryReactive reactive)
    {
        _toLasna();
        _nextFreshContract(owner);
        vm.prank(owner);
        reactive = new CanaryReactive(SYSTEM_ADDRESS, chainId, recipient, gasLimit);
        vm.prank(stranger);
        system.depositTo{ value: 1 ether }(address(reactive));
    }

    function _poke(CanaryReactive reactive) internal {
        _toLasna();
        vm.prank(owner);
        reactive.poke();
    }

    /// @dev The owner transaction `vault.setPolicy` on the destination and its logs.
    function _setPolicy(address target, Policy memory policy) internal returns (Vm.Log[] memory) {
        uint256 mark = _destinationMark();
        _toDestination();
        vm.prank(owner);
        vault.setPolicy(target, policy);
        return _destinationLogs(mark);
    }

    /// @dev The owner transaction `hub.enroll` on Lasna for a destination target.
    function _enroll(address target, Policy memory policy) internal {
        _toLasna();
        vm.prank(owner);
        hub.enroll(DESTINATION_CHAIN_ID, target, policy);
    }

    function _disarm(address target) internal {
        _toLasna();
        vm.prank(owner);
        hub.disarm(DESTINATION_CHAIN_ID, target);
    }

    /// @dev A canary in the recommended order: the vault has its policy, then the hub has it enrolled.
    function _armedCanary(Policy memory policy) internal returns (address canary) {
        canary = _deployCanary();
        _onlyResult(_setPolicy(canary, policy), 0, canary, LifelineVault.Outcome.NoNeed);
        _enroll(canary, policy);
    }

    function _armedCanaries(uint256 count) internal returns (address[] memory canaries) {
        canaries = new address[](count);
        for (uint256 i = 0; i < count; ++i) {
            canaries[i] = _armedCanary(_policy());
        }
    }

    /// @dev Delivers `pong` to a canary with reserves of 0: the callback runs and leaves a debt, and the proxy
    ///      emits `BlacklistContract(canary)`. The requester is not the hub: a canary has no access check.
    function _pongIntoDebt(address canary) internal returns (uint256 debt) {
        _toDestination();
        assertEq(proxy.reserves(canary), 0, "the canary has no reserves");
        assertEq(proxy.debt(canary), 0, "the canary has no debt");
        Delivery memory d =
            _deliver(canary, abi.encodeCall(CanaryCallback.pong, (address(0), 1)), DELIVERY_GAS, stranger);
        assertTrue(d.delivered, "the delivery did not revert");
        assertFalse(d.callbackFailed, "no CallbackFailure");
        assertTrue(_wentIntoDebt(d.logs, canary), "BlacklistContract(canary)");
        debt = proxy.debt(canary);
        assertEq(debt, d.fee, "the debt is the fee");
    }

    /// @dev Another account pays exactly the debt of a contract: no debt, and reserves of 0.
    function _clearDebt(address account) internal {
        _toDestination();
        uint256 debt = proxy.debt(account);
        vm.prank(stranger);
        proxy.depositTo{ value: debt }(account);
        assertEq(proxy.debt(account), 0);
        assertEq(proxy.reserves(account), 0);
    }

    /// @dev One round of a debt that the vault does not pay: the canary goes into debt, the relay runs, and
    ///      another account clears the debt.
    function _debtRound(address canary) internal returns (uint256 triggers, uint256 deliveries) {
        _pongIntoDebt(canary);
        (triggers, deliveries) = _relayAll();
        _clearDebt(canary);
    }

    /// @dev Junk callbacks empty the float of the vault until the vault is in debt: `BlacklistContract(vault)`.
    ///      The first argument is not the hub, so the vault rejects each of them. The hub hears the debt:
    ///      `VaultDown`.
    function _takeVaultDown() internal {
        _toDestination();
        bytes memory payload = abi.encodeCall(LifelineVault.reconcile, (address(0), address(0xdead), 0));
        Delivery memory d;
        for (uint256 i = 0; proxy.debt(address(vault)) == 0; ++i) {
            assertLt(i, 200, "the flood ends");
            d = _deliver(address(vault), payload, DELIVERY_GAS, stranger);
            assertTrue(d.delivered && d.callbackFailed, "the vault rejects a junk callback");
        }
        assertTrue(_wentIntoDebt(d.logs, address(vault)), "BlacklistContract(vault)");

        uint256 mark = _lasnaMark();
        (uint256 triggers, uint256 deliveries) = _relayAll();
        assertEq(triggers, 1, "the hub hears the debt of the vault");
        assertEq(deliveries, 0);
        assertEq(_onlyHubLog(mark, LifelineHub.VaultDown.selector).topics[1], DESTINATION_CHAIN_ID);
        _assertHubHeld();
        assertFalse(_vaultIsUp(), "the vault state is down");
    }

    /// @dev The one restoring transaction on the destination: `proxy.depositTo{value: debt + float}(vault)`.
    function _restoreVault() internal {
        _toDestination();
        uint256 debt = proxy.debt(address(vault));
        assertGt(debt, 0, "the vault is in debt");
        uint256 mark = _destinationMark();
        vm.prank(stranger);
        proxy.depositTo{ value: debt + FEE_FLOAT }(address(vault));
        assertTrue(_debtCleared(_destinationLogs(mark), address(vault)), "WhitelistContract(vault)");
        assertEq(proxy.debt(address(vault)), 0);
        assertEq(proxy.reserves(address(vault)), FEE_FLOAT);
    }

    /// @dev The owner transaction `hub.vaultUp` on Lasna and the values of its `VaultUp`.
    function _vaultUp() internal returns (UpLog memory up) {
        uint256 mark = _lasnaMark();
        vm.prank(owner);
        hub.vaultUp(DESTINATION_CHAIN_ID);
        ChainLog memory log = _onlyHubLog(mark, LifelineHub.VaultUp.selector);
        assertEq(log.topics[1], DESTINATION_CHAIN_ID, "VaultUp chain");
        up.requestId = log.topics[2];
        (up.sent, up.remaining) = abi.decode(log.data, (uint256, uint256));
        assertEq(_hubRequestCount(mark), up.sent == 0 ? 0 : 1, "one request for each sweep");
        assertTrue(_vaultIsUp(), "vaultUp sets the state up");
    }

    /// @dev Asserts a `VaultUp` that sent nothing: `VaultUp(chainId, 0, 0, 0)`.
    function _assertNothingSent(UpLog memory up) internal pure {
        assertEq(up.requestId, 0);
        assertEq(up.sent, 0);
        assertEq(up.remaining, 0);
    }

    // ------------------------------------------------------------------------------------------
    // Helpers: the Lasna side
    // ------------------------------------------------------------------------------------------

    function _lasnaMark() internal returns (uint256) {
        _toLasna();
        return _logMark();
    }

    function _hubLogs(uint256 mark, bytes32 topic0) internal returns (uint256) {
        _toLasna();
        return _countLogs(mark, address(hub), topic0);
    }

    function _hubLog(uint256 mark, bytes32 topic0, uint256 n) internal returns (ChainLog memory) {
        _toLasna();
        return _findLog(mark, address(hub), topic0, n);
    }

    function _onlyHubLog(uint256 mark, bytes32 topic0) internal returns (ChainLog memory) {
        _toLasna();
        return _onlyLog(mark, address(hub), topic0);
    }

    /// @dev The newest trigger of the hub succeeded, and the system contract reported no reverted reaction.
    function _assertHubHeld() internal {
        _toLasna();
        TriggerResult memory result = _lastTrigger(address(hub));
        assertTrue(result.success, "the trigger succeeded");
        assertFalse(result.reactReverted, "no reverted reaction");
        assertEq(system.debt(address(hub)), 0, "the hub has no debt");
    }

    /// @dev Asserts one `Relayed` of the hub after a mark for this target, and returns its `requestId`.
    function _assertRelayed(uint256 mark, address target) internal returns (uint256 requestId) {
        ChainLog memory relayed = _onlyHubLog(mark, LifelineHub.Relayed.selector);
        assertEq(relayed.topics[2], DESTINATION_CHAIN_ID, "Relayed chain");
        assertEq(relayed.topics[3], uint256(uint160(target)), "Relayed target");
        _assertHubHeld();
        return relayed.topics[1];
    }

    /// @dev Asserts one `Skipped` of the hub after a mark, with these values.
    function _assertSkipped(uint256 mark, address target, LifelineHub.SkipReason reason) internal {
        ChainLog memory skipped = _onlyHubLog(mark, LifelineHub.Skipped.selector);
        assertEq(skipped.topics[1], DESTINATION_CHAIN_ID, "Skipped chain");
        assertEq(skipped.topics[2], uint256(uint160(target)), "Skipped target");
        assertEq(abi.decode(skipped.data, (uint256)), uint256(reason), "Skipped reason");
        _assertHubHeld();
    }

    /// @dev Asserts one log of the hub after a mark with the topics `(chainId, target)`.
    function _assertTargetLog(uint256 mark, bytes32 topic0, address target) internal {
        ChainLog memory log = _onlyHubLog(mark, topic0);
        assertEq(log.topics[1], DESTINATION_CHAIN_ID, "chain");
        assertEq(log.topics[2], uint256(uint160(target)), "target");
    }

    /// @dev The number of callback requests of the hub after a mark.
    function _hubRequestCount(uint256 mark) internal returns (uint256 count) {
        _toLasna();
        bytes32 topic0 = ISystemContractCallbacks.CallbackRequest.selector;
        uint256 all = _countLogs(mark, SYSTEM_ADDRESS, topic0);
        for (uint256 i = 0; i < all; ++i) {
            if (_findLog(mark, SYSTEM_ADDRESS, topic0, i).topics[2] == uint256(uint160(address(hub)))) ++count;
        }
    }

    /// @dev The first callback request of the hub after a mark.
    function _hubRequest(uint256 mark) internal returns (ICallbackProxy.CallbackConfiguration_V_1_0 memory request) {
        _toLasna();
        bytes32 topic0 = ISystemContractCallbacks.CallbackRequest.selector;
        uint256 all = _countLogs(mark, SYSTEM_ADDRESS, topic0);
        for (uint256 i = 0; i < all; ++i) {
            ChainLog memory log = _findLog(mark, SYSTEM_ADDRESS, topic0, i);
            if (log.topics[2] == uint256(uint160(address(hub)))) return _requestConfig(log);
        }
        fail("the hub made no request");
    }

    /// @dev The six fields of a `Subscribe` or `Unsubscribe` log of the system contract for a debt tuple of
    ///      the hub at the destination proxy.
    function _assertDebtTuple(ChainLog memory log, address topic1) internal view {
        assertEq(log.topics[1], uint256(uint160(address(hub))), "subscriber");
        assertEq(log.topics[2], DESTINATION_CHAIN_ID, "chain");
        assertEq(log.topics[3], uint256(uint160(PROXY_ADDRESS)), "emitter");
        assertEq(
            log.data,
            abi.encode(
                uint256(IProxyLedger.BlacklistContract.selector),
                uint256(uint160(topic1)),
                REACTIVE_IGNORE,
                REACTIVE_IGNORE
            ),
            "topics"
        );
    }

    function _assertState(address target, TargetState expected) internal {
        _toLasna();
        (TargetKind kind, TargetState state,) = hub.targetInfo(DESTINATION_CHAIN_ID, target);
        assertEq(uint256(kind), uint256(TargetKind.Destination));
        assertEq(uint256(state), uint256(expected));
    }

    function _vaultIsUp() internal returns (bool up) {
        _toLasna();
        (,,, up,) = hub.vaultInfo(DESTINATION_CHAIN_ID);
    }

    function _pendingCount() internal returns (uint256 pending) {
        _toLasna();
        (,,,, pending) = hub.vaultInfo(DESTINATION_CHAIN_ID);
    }

    /// @dev A forged debt log of the destination proxy, sent to the hub through the real `trigger`.
    function _forgedDebtLog(address emitter, address target) internal returns (TriggerResult memory) {
        _toLasna();
        return _trigger(address(hub), _debtLogRecord(DESTINATION_CHAIN_ID, emitter, target, 1));
    }

    // ------------------------------------------------------------------------------------------
    // Helpers: the destination side
    // ------------------------------------------------------------------------------------------

    /// @dev The principal of the vault. The balance of an address is the balance on the active fork.
    function _vaultBalance() internal returns (uint256) {
        _toDestination();
        return address(vault).balance;
    }

    function _vaultLogs(Vm.Log[] memory logs, bytes32 topic0) internal view returns (uint256) {
        return _countLogs(logs, address(vault), topic0);
    }

    /// @dev The `n`-th `Result` (from 0) of the vault.
    function _result(Vm.Log[] memory logs, uint256 n) internal view returns (ResultLog memory result) {
        Vm.Log memory log = _findLog(logs, address(vault), LifelineVault.Result.selector, n);
        result.id = uint256(log.topics[1]);
        result.target = address(uint160(uint256(log.topics[2])));
        (result.outcome, result.paid) = abi.decode(log.data, (LifelineVault.Outcome, uint256));
    }

    /// @dev Checks that the vault emitted exactly one `Result` with these values, and returns its payment.
    function _onlyResult(Vm.Log[] memory logs, uint256 id, address target, LifelineVault.Outcome outcome)
        internal
        view
        returns (uint256 paid)
    {
        assertEq(_vaultLogs(logs, LifelineVault.Result.selector), 1, "exactly one Result");
        ResultLog memory result = _result(logs, 0);
        assertEq(result.id, id, "Result id");
        assertEq(result.target, target, "Result target");
        assertEq(uint256(result.outcome), uint256(outcome), "Result outcome");
        if (outcome == LifelineVault.Outcome.Rescued) assertGt(result.paid, 0, "a payment is above 0");
        else assertEq(result.paid, 0, "a skip pays 0");
        return result.paid;
    }

    /// @dev The sum of `SelfCare` of the vault: the principal that moved into its float.
    function _selfCareSum(Vm.Log[] memory logs) internal view returns (uint256 sum) {
        uint256 count = _vaultLogs(logs, LifelineVault.SelfCare.selector);
        for (uint256 i = 0; i < count; ++i) {
            sum += abi.decode(_findLog(logs, address(vault), LifelineVault.SelfCare.selector, i).data, (uint256));
        }
    }

    /// @dev Checks a delivery that the relay made for a request of the hub: the recipient is the vault, the
    ///      first argument is the address of the hub, the gas limit is the computed one, and the callback
    ///      ran. Returns its logs.
    function _assertVaultDelivery(uint256 index, uint256 gasLimit) internal view returns (Vm.Log[] memory) {
        DeliveryRecord memory d = _deliveryAt(index);
        assertEq(d.recipient, address(vault), "the recipient is the vault");
        assertEq(d.requester, address(hub), "the first argument is the address of the hub");
        assertEq(d.gasLimit, gasLimit, "the gas limit of the delivery is the computed limit");
        assertTrue(d.delivered, "the delivery did not revert");
        assertFalse(d.callbackFailed, "no CallbackFailure");
        assertLe(d.gasUsed, gasLimit, "the transaction is inside its gas limit");
        return _deliveryLogs(index);
    }

    /// @dev The arguments of a `sweep` payload.
    function _sweepArguments(bytes memory payload) internal pure returns (address[] memory targets, uint256 id) {
        assertEq(bytes4(payload), LifelineVault.sweep.selector, "the payload is a sweep");
        bytes memory arguments = new bytes(payload.length - 4);
        for (uint256 i = 0; i < arguments.length; ++i) {
            arguments[i] = payload[i + 4];
        }
        address sender;
        (sender, targets, id) = abi.decode(arguments, (address, address[], uint256));
        assertEq(sender, address(0), "the first argument of the request is the placeholder");
    }

    function _contains(address[] memory list, address item) internal pure returns (bool) {
        for (uint256 i = 0; i < list.length; ++i) {
            if (list[i] == item) return true;
        }
        return false;
    }

    // ------------------------------------------------------------------------------------------
    // Harness
    // ------------------------------------------------------------------------------------------

    /// @dev The relay delivers a request with the requested gas limit, and writes the address of the
    ///      requesting contract into the first argument.
    function test_harness_deliversARequestWithItsGasLimitAndItsRequester() public {
        address canary = _deployCanary();
        CanaryReactive reactive = _deployReactive(canary, DESTINATION_CHAIN_ID, 345_678);
        uint256 before = _deliveryCount();

        _poke(reactive);
        (uint256 triggers, uint256 deliveries) = _relayAll();
        assertEq(triggers, 1, "only the reactive canary reacts: the hub does not listen to this canary");
        assertEq(deliveries, 1);

        DeliveryRecord memory d = _deliveryAt(before);
        assertEq(d.recipient, canary);
        assertEq(d.requester, address(reactive));
        assertEq(d.gasLimit, 345_678);
        assertTrue(d.delivered);
        Vm.Log[] memory logs = _deliveryLogs(before);
        assertEq(uint256(_onlyLog(logs, canary, CanaryCallback.Pong.selector).topics[1]), 1, "Pong(1)");
        assertTrue(_wentIntoDebt(logs, canary));
    }

    /// @dev The node ignores a request below 100,000 gas, and the harness has one destination chain.
    function test_harness_ignoresARequestThatTheNodeDoesNotDeliver() public {
        address canary = _deployCanary();
        CanaryReactive low = _deployReactive(canary, DESTINATION_CHAIN_ID, uint64(MIN_CALLBACK_GAS - 1));
        CanaryReactive otherChain = _deployReactive(canary, CHAIN_WITHOUT_VAULT, DELIVERY_GAS);

        _poke(low);
        _poke(otherChain);
        (uint256 triggers, uint256 deliveries) = _relayAll();
        assertEq(triggers, 2);
        assertEq(deliveries, 0);
    }

    // ------------------------------------------------------------------------------------------
    // The vault registry
    // ------------------------------------------------------------------------------------------

    function test_constants_haveTheFixedValues() public pure {
        assertEq(SWEEP_PER_TARGET, 80_000);
        assertEq(SWEEP_MAX, 8);
        assertEq(REQUEST_ID_BASE, 1 << 128);
    }

    /// @dev The ABI has each addition of this unit. `test_abi_hasNoPay` of the earlier unit checks the rest.
    function test_abi_hasTheAdditions() public view {
        string memory json = vm.readFile("out/LifelineHub.sol/LifelineHub.json");

        string[6] memory expected = [
            "setVault(uint256,address,address,uint256)",
            "rearm(uint256,address)",
            "vaultUp(uint256)",
            "vaultInfo(uint256)",
            "vaultChainCount()",
            "vaultChainAt(uint256)"
        ];
        for (uint256 i = 0; i < expected.length; ++i) {
            assertTrue(
                vm.keyExistsJson(json, string.concat(".methodIdentifiers['", expected[i], "']")),
                string.concat("missing in the ABI: ", expected[i])
            );
        }

        assertEq(LifelineHub.VaultSet.selector, keccak256("VaultSet(uint256,address,address,uint256)"));
        assertEq(LifelineHub.Relayed.selector, keccak256("Relayed(uint256,uint256,address)"));
        assertEq(LifelineHub.RelayLimited.selector, keccak256("RelayLimited(uint256,address)"));
        assertEq(LifelineHub.Rearmed.selector, keccak256("Rearmed(uint256,address)"));
        assertEq(LifelineHub.VaultDown.selector, keccak256("VaultDown(uint256)"));
        assertEq(LifelineHub.Pending.selector, keccak256("Pending(uint256,address)"));
        assertEq(LifelineHub.VaultUp.selector, keccak256("VaultUp(uint256,uint256,uint256,uint256)"));
    }

    /// @dev Later units append skip reasons and do not reorder them.
    function test_skipReason_keepsTheOrderOfTheEarlierUnit() public pure {
        assertEq(uint256(LifelineHub.SkipReason.Paused), 0);
        assertEq(uint256(LifelineHub.SkipReason.Unhandled), 13);
        assertEq(uint256(LifelineHub.SkipReason.AlreadyPending), 14);
        assertEq(uint256(LifelineHub.SkipReason.RequestFailed), 15);
    }

    function test_ownerFunctions_rejectAnotherCaller() public {
        _toLasna();
        vm.startPrank(stranger);
        vm.expectRevert(LifelineHub.NotOwner.selector);
        hub.setVault(DESTINATION_CHAIN_ID, PROXY_ADDRESS, address(vault), BASE_SEPOLIA_SWEEP_BASE);
        vm.expectRevert(LifelineHub.NotOwner.selector);
        hub.rearm(DESTINATION_CHAIN_ID, address(1));
        vm.expectRevert(LifelineHub.NotOwner.selector);
        hub.vaultUp(DESTINATION_CHAIN_ID);
        vm.stopPrank();
    }

    /// @dev `setVault` in `setUp` registered the vault of the destination chain.
    function test_setVault_registersTheVaultAndSubscribesTheExactTuple() public {
        _toLasna();
        assertEq(hub.vaultChainCount(), 1);
        assertEq(hub.vaultChainAt(0), DESTINATION_CHAIN_ID);
        (address proxy_, address vault_, uint256 sweepBase, bool up, uint256 pending) =
            hub.vaultInfo(DESTINATION_CHAIN_ID);
        assertEq(proxy_, PROXY_ADDRESS);
        assertEq(vault_, address(vault));
        assertEq(sweepBase, BASE_SEPOLIA_SWEEP_BASE);
        assertTrue(up);
        assertEq(pending, 0);
        _assertDebtTuple(_onlyLog(0, SYSTEM_ADDRESS, ISystemContract.Subscribe.selector), address(vault));
        assertEq(_activeSubscriptions(address(hub)), 1, "the debt tuple of the vault");

        // A second chain: the event and the one subscription, emitter = that proxy, topic1 = that vault.
        address otherProxy = address(0xB0B);
        address otherVault = address(0xCAFE);
        uint256 mark = _logMark();
        vm.prank(owner);
        hub.setVault(1301, otherProxy, otherVault, 250_000);

        ChainLog memory set = _onlyHubLog(mark, LifelineHub.VaultSet.selector);
        assertEq(set.topics[1], 1301);
        assertEq(set.data, abi.encode(otherProxy, otherVault, uint256(250_000)));
        assertEq(_activeSubscriptions(address(hub)), 2);
        assertEq(hub.vaultChainCount(), 2);
        assertEq(hub.vaultChainAt(1), 1301);
        (proxy_, vault_, sweepBase, up, pending) = hub.vaultInfo(1301);
        assertEq(proxy_, otherProxy);
        assertEq(vault_, otherVault);
        assertEq(sweepBase, 250_000);
        assertTrue(up);
        assertEq(_hubRequestCount(mark), 0, "no callback");

        // A chain with no vault has an empty record.
        (proxy_, vault_, sweepBase, up, pending) = hub.vaultInfo(CHAIN_WITHOUT_VAULT);
        assertEq(proxy_, address(0));
        assertEq(vault_, address(0));
        assertEq(sweepBase, 0);
        assertFalse(up);
    }

    function test_setVault_rejectsInvalidInput() public {
        _toLasna();
        address v = address(vault);
        uint256 tooLarge = uint256(type(uint64).max) - SWEEP_PER_TARGET * SWEEP_MAX + 1;

        _expectInvalidVault(LASNA_CHAIN_ID, PROXY_ADDRESS, v, BASE_SEPOLIA_SWEEP_BASE);
        _expectInvalidVault(0, PROXY_ADDRESS, v, BASE_SEPOLIA_SWEEP_BASE);
        _expectInvalidVault(1301, address(0), v, BASE_SEPOLIA_SWEEP_BASE);
        _expectInvalidVault(1301, PROXY_ADDRESS, address(0), BASE_SEPOLIA_SWEEP_BASE);
        _expectInvalidVault(1301, PROXY_ADDRESS, v, tooLarge);

        assertEq(hub.vaultChainCount(), 1);
        vm.prank(owner);
        hub.setVault(1301, PROXY_ADDRESS, v, tooLarge - 1);
        assertEq(hub.vaultChainCount(), 2);
    }

    function _expectInvalidVault(uint256 chainId, address proxy_, address vault_, uint256 sweepBase) internal {
        vm.expectRevert(abi.encodeWithSelector(LifelineHub.InvalidVault.selector, chainId, proxy_, vault_, sweepBase));
        vm.prank(owner);
        hub.setVault(chainId, proxy_, vault_, sweepBase);
    }

    /// @dev A second call with the same proxy and vault updates `sweepBase`, restores the debt tuple of the
    ///      vault after `unsubscribeAll`, and keeps a down state.
    function test_setVault_theSamePairUpdatesTheBaseAndRestoresTheTuple() public {
        uint256 mark = _lasnaMark();
        _setVault(ETHEREUM_SEPOLIA_SWEEP_BASE);
        (,, uint256 sweepBase,,) = hub.vaultInfo(DESTINATION_CHAIN_ID);
        assertEq(sweepBase, ETHEREUM_SEPOLIA_SWEEP_BASE);
        assertEq(hub.vaultChainCount(), 1, "the chain is listed once");
        assertEq(_countLogs(mark, SYSTEM_ADDRESS, ISystemContract.Subscribe.selector), 0, "the tuple is registered");
        assertEq(_hubLogs(mark, LifelineHub.VaultSet.selector), 1);

        // The debt log of the vault takes the vault down.
        assertTrue(_forgedDebtLog(PROXY_ADDRESS, address(vault)).success);
        assertFalse(_vaultIsUp());

        vm.prank(owner);
        hub.unsubscribeAll();
        assertEq(_activeSubscriptions(address(hub)), 0);

        mark = _logMark();
        _setVault(BASE_SEPOLIA_SWEEP_BASE);
        _assertDebtTuple(_onlyLog(mark, SYSTEM_ADDRESS, ISystemContract.Subscribe.selector), address(vault));
        assertEq(_activeSubscriptions(address(hub)), 1);
        assertFalse(_vaultIsUp(), "only vaultUp ends a down state");
    }

    /// @dev A different proxy or vault is accepted only while no target is enrolled on that chain.
    function test_setVault_aDifferentPairNeedsAChainWithNoTarget() public {
        address canary = _armedCanary(_policy());
        address newVault = address(0xCAFE);

        _toLasna();
        vm.expectRevert(abi.encodeWithSelector(LifelineHub.VaultInUse.selector, DESTINATION_CHAIN_ID));
        vm.prank(owner);
        hub.setVault(DESTINATION_CHAIN_ID, PROXY_ADDRESS, newVault, BASE_SEPOLIA_SWEEP_BASE);
        vm.expectRevert(abi.encodeWithSelector(LifelineHub.VaultInUse.selector, DESTINATION_CHAIN_ID));
        vm.prank(owner);
        hub.setVault(DESTINATION_CHAIN_ID, address(0xB0B), address(vault), BASE_SEPOLIA_SWEEP_BASE);

        // The old vault is down when the owner replaces it.
        assertTrue(_forgedDebtLog(PROXY_ADDRESS, address(vault)).success);
        assertFalse(_vaultIsUp());
        _disarm(canary);

        uint256 mark = _logMark();
        vm.prank(owner);
        hub.setVault(DESTINATION_CHAIN_ID, PROXY_ADDRESS, newVault, BASE_SEPOLIA_SWEEP_BASE);
        _assertDebtTuple(_onlyLog(mark, SYSTEM_ADDRESS, ISystemContract.Unsubscribe.selector), address(vault));
        _assertDebtTuple(_onlyLog(mark, SYSTEM_ADDRESS, ISystemContract.Subscribe.selector), newVault);
        (, address vault_,, bool up,) = hub.vaultInfo(DESTINATION_CHAIN_ID);
        assertEq(vault_, newVault);
        assertTrue(up, "a new pair starts in state up");
        assertEq(hub.vaultChainCount(), 1);
        assertEq(_activeSubscriptions(address(hub)), 1);
    }

    // ------------------------------------------------------------------------------------------
    // Enrollment of a destination target
    // ------------------------------------------------------------------------------------------

    /// @dev Completion criterion: `enroll` for a chain with no registered vault reverts `UnknownChain`.
    function test_enroll_aChainWithNoVaultRevertsUnknownChain() public {
        address canary = _deployCanary();
        _toLasna();
        vm.startPrank(owner);
        vm.expectRevert(abi.encodeWithSelector(LifelineHub.UnknownChain.selector, CHAIN_WITHOUT_VAULT));
        hub.enroll(CHAIN_WITHOUT_VAULT, canary, _policy());
        vm.expectRevert(abi.encodeWithSelector(LifelineHub.UnknownChain.selector, CHAIN_WITHOUT_VAULT));
        hub.disarm(CHAIN_WITHOUT_VAULT, canary);
        // Lasna has no vault: the two vault functions do not accept its chain ID.
        uint256[2] memory chains = [CHAIN_WITHOUT_VAULT, LASNA_CHAIN_ID];
        for (uint256 i = 0; i < chains.length; ++i) {
            vm.expectRevert(abi.encodeWithSelector(LifelineHub.UnknownChain.selector, chains[i]));
            hub.rearm(chains[i], canary);
            vm.expectRevert(abi.encodeWithSelector(LifelineHub.UnknownChain.selector, chains[i]));
            hub.vaultUp(chains[i]);
        }
        vm.stopPrank();

        assertEq(_activeSubscriptions(address(hub)), 1, "only the debt tuple of the vault");
        assertEq(hub.targetCount(CHAIN_WITHOUT_VAULT), 0);
    }

    /// @dev The hub stores `maxRescues` and `window` only, subscribes the exact tuple at the proxy, and sends
    ///      no callback and no payment.
    function test_enroll_storesTheTargetAndSubscribesTheExactTuple() public {
        address canary = _deployCanary();
        Policy memory policy = _policy(3);
        uint256 mark = _lasnaMark();
        uint256 balanceBefore = address(hub).balance;
        _enroll(canary, policy);

        _assertTargetLog(mark, LifelineHub.Enrolled.selector, canary);
        assertEq(hub.targetCount(DESTINATION_CHAIN_ID), 1);
        assertEq(hub.targetAt(DESTINATION_CHAIN_ID, 0), canary);
        assertEq(hub.targetCount(LASNA_CHAIN_ID), 0, "the Lasna fleet has its own list");
        (TargetKind kind, TargetState state, Policy memory stored) = hub.targetInfo(DESTINATION_CHAIN_ID, canary);
        assertEq(uint256(kind), uint256(TargetKind.Destination));
        assertEq(uint256(state), uint256(TargetState.Active));
        assertEq(
            abi.encode(stored),
            abi.encode(Policy({ low: 0, high: 0, maxDebt: 0, dailyCap: 0, maxRescues: 3, window: WINDOW })),
            "the vault owns the marks and the caps"
        );

        _assertDebtTuple(_onlyLog(mark, SYSTEM_ADDRESS, ISystemContract.Subscribe.selector), canary);
        assertEq(_activeSubscriptions(address(hub)), 2);

        assertEq(_hubRequestCount(mark), 0, "enroll sends no callback");
        assertEq(address(hub).balance, balanceBefore, "enroll pays nothing");
        (uint256 triggers, uint256 deliveries) = _relayAll();
        assertEq(triggers + deliveries, 0);
    }

    function test_enroll_rejectsInvalidInput() public {
        address canary = _deployCanary();
        _toLasna();

        Policy memory invalid = _policy();
        invalid.window = 0;
        vm.expectRevert(InvalidPolicy.selector);
        vm.prank(owner);
        hub.enroll(DESTINATION_CHAIN_ID, canary, invalid);

        vm.expectRevert(abi.encodeWithSelector(LifelineHub.InvalidTarget.selector, address(hub)));
        vm.prank(owner);
        hub.enroll(DESTINATION_CHAIN_ID, address(hub), _policy());

        // The debt log of the vault has its own handler.
        vm.expectRevert(abi.encodeWithSelector(LifelineHub.InvalidTarget.selector, address(vault)));
        vm.prank(owner);
        hub.enroll(DESTINATION_CHAIN_ID, address(vault), _policy());

        _enroll(canary, _policy());
        vm.expectRevert(abi.encodeWithSelector(LifelineHub.AlreadyEnrolled.selector, DESTINATION_CHAIN_ID, canary));
        vm.prank(owner);
        hub.enroll(DESTINATION_CHAIN_ID, canary, _policy());

        vm.expectRevert(abi.encodeWithSelector(LifelineHub.NotEnrolled.selector, DESTINATION_CHAIN_ID, stranger));
        vm.prank(owner);
        hub.disarm(DESTINATION_CHAIN_ID, stranger);

        assertEq(hub.targetCount(DESTINATION_CHAIN_ID), 1);
    }

    /// @dev `disarm` unsubscribes the tuple with its six fields. The next debt of the target does not wake
    ///      the hub. `enroll` is permitted again.
    function test_disarm_stopsTheTarget() public {
        address canary = _armedCanary(_policy());
        address other = _armedCanary(_policy());

        uint256 mark = _lasnaMark();
        _disarm(canary);
        _assertTargetLog(mark, LifelineHub.Disarmed.selector, canary);
        _assertDebtTuple(_onlyLog(mark, SYSTEM_ADDRESS, ISystemContract.Unsubscribe.selector), canary);
        (, TargetState state,) = hub.targetInfo(DESTINATION_CHAIN_ID, canary);
        assertEq(uint256(state), uint256(TargetState.Disarmed));
        assertEq(hub.targetCount(DESTINATION_CHAIN_ID), 1);
        assertEq(hub.targetAt(DESTINATION_CHAIN_ID, 0), other);

        _pongIntoDebt(canary);
        (uint256 triggers, uint256 deliveries) = _relayAll();
        assertEq(triggers + deliveries, 0, "the hub does not hear a disarmed target");

        // A forged debt log for it is a skip.
        mark = _lasnaMark();
        assertTrue(_forgedDebtLog(PROXY_ADDRESS, canary).success);
        _assertSkipped(mark, canary, LifelineHub.SkipReason.NotEnrolled);

        _enroll(canary, _policy());
        _assertState(canary, TargetState.Active);
        assertEq(hub.targetCount(DESTINATION_CHAIN_ID), 2);
    }

    // ------------------------------------------------------------------------------------------
    // Destination loop
    // ------------------------------------------------------------------------------------------

    /// @dev Completion criterion: a callback canary goes into debt at the proxy; the hub emits `Relayed`; the
    ///      vault emits `Result` with the same `requestId` and a payment; `debt` is 0; the next callback to
    ///      the canary runs.
    function test_destinationLoop_theHubRelaysAndTheVaultRescues() public {
        address canary = _armedCanary(_policy());
        CanaryReactive reactive = _deployReactive(canary, DESTINATION_CHAIN_ID, DELIVERY_GAS);
        uint256 vaultBalanceBefore = _vaultBalance();
        uint256 mark = _lasnaMark();
        uint256 first = _deliveryCount();

        _poke(reactive);
        (uint256 triggers, uint256 deliveries) = _relayAll();
        assertEq(triggers, 2, "the reactive canary and the hub react");
        assertEq(deliveries, 2, "pong and reconcile");

        // Delivery 1: `Pong(1)`, then the debt at the proxy.
        uint256 debt = _assertPongThenDebt(first, canary);

        // The reaction of the hub: `Relayed` and one request for `reconcile(address(0), canary, requestId)`.
        uint256 requestId = _assertRelayed(mark, canary);
        assertEq(requestId, REQUEST_ID_BASE + 1, "the first request");
        assertEq(_hubRequestCount(mark), 1);
        ICallbackProxy.CallbackConfiguration_V_1_0 memory request = _hubRequest(mark);
        assertEq(request.chainId, DESTINATION_CHAIN_ID);
        assertEq(request.recipient, address(vault));
        assertEq(request.gasLimit, BASE_SEPOLIA_SWEEP_BASE + SWEEP_PER_TARGET, "sweepBase + SWEEP_PER_TARGET x 1");
        assertEq(request.payload, abi.encodeCall(LifelineVault.reconcile, (address(0), canary, requestId)));
        assertEq(_hubLogs(mark, LifelineHub.Quarantined.selector), 0);
        assertEq(_hubLogs(mark, LifelineHub.Rescued.selector), 0, "the hub does not pay a destination target");

        // Delivery 2: the vault pays. `Result` has the `requestId` of `Relayed`.
        Vm.Log[] memory rescueLogs = _assertVaultDelivery(first + 1, BASE_SEPOLIA_SWEEP_BASE + SWEEP_PER_TARGET);
        assertEq(_onlyResult(rescueLogs, requestId, canary, LifelineVault.Outcome.Rescued), debt + HIGH);
        assertTrue(_debtCleared(rescueLogs, canary), "WhitelistContract(canary)");
        _toDestination();
        assertEq(proxy.debt(canary), 0, "debt is 0");
        assertEq(proxy.reserves(canary), HIGH);
        assertEq(_vaultBalance(), vaultBalanceBefore - debt - HIGH, "the fee came from the float");

        // The next callback to the canary runs.
        _poke(reactive);
        (triggers, deliveries) = _relayAll();
        assertEq(triggers, 1, "the hub is not woken");
        assertEq(deliveries, 1);
        assertTrue(_deliveryAt(first + 2).delivered);
        assertFalse(_deliveryAt(first + 2).callbackFailed);
        Vm.Log memory pong2 = _onlyLog(_deliveryLogs(first + 2), canary, CanaryCallback.Pong.selector);
        assertEq(uint256(pong2.topics[1]), 2, "Pong(2)");
        _toDestination();
        assertEq(proxy.debt(canary), 0);
    }

    /// @dev Checks the delivery of the first `pong` to an unfunded canary: `Pong(1)`, then
    ///      `BlacklistContract(canary)` at the proxy. Returns the debt.
    function _assertPongThenDebt(uint256 index, address canary) internal view returns (uint256 debt) {
        DeliveryRecord memory pong = _deliveryAt(index);
        assertEq(pong.recipient, canary);
        assertTrue(pong.delivered);
        assertFalse(pong.callbackFailed);
        Vm.Log[] memory logs = _deliveryLogs(index);
        assertEq(uint256(_onlyLog(logs, canary, CanaryCallback.Pong.selector).topics[1]), 1, "Pong(1)");
        assertTrue(_wentIntoDebt(logs, canary), "BlacklistContract(canary)");
        // The callback runs first; the charge and the debt come after it.
        assertEq(logs[0].topics[0], CanaryCallback.Pong.selector, "Pong is the first log");
        assertEq(logs[logs.length - 1].topics[0], IProxyLedger.BlacklistContract.selector, "the debt log is the last");
        debt = pong.fee;
        assertGt(debt, 0);
    }

    /// @dev Control: the same start with the hub paused. The hub emits a skip and no request; the canary
    ///      stays in debt and the next delivery to it reverts `InDebt`.
    function test_destinationLoop_controlWithTheHubPaused() public {
        address canary = _armedCanary(_policy());
        CanaryReactive reactive = _deployReactive(canary, DESTINATION_CHAIN_ID, DELIVERY_GAS);
        vm.prank(owner);
        hub.pause();
        uint256 mark = _logMark();
        uint256 first = _deliveryCount();

        _poke(reactive);
        (uint256 triggers, uint256 deliveries) = _relayAll();
        assertEq(triggers, 2, "the reactive canary and the paused hub react");
        assertEq(deliveries, 1, "pong only");

        uint256 debt = _deliveryAt(first).fee;
        assertTrue(_wentIntoDebt(_deliveryLogs(first), canary));
        _assertSkipped(mark, canary, LifelineHub.SkipReason.Paused);
        assertEq(_hubLogs(mark, LifelineHub.Relayed.selector), 0);
        assertEq(_hubRequestCount(mark), 0, "no CallbackRequest");

        _poke(reactive);
        (triggers, deliveries) = _relayAll();
        assertEq(triggers, 1);
        assertEq(deliveries, 1);
        DeliveryRecord memory second = _deliveryAt(first + 1);
        assertFalse(second.delivered, "the second delivery reverts");
        assertEq(second.revertData, abi.encodeWithSelector(ICallbackProxy.InDebt.selector, canary, debt));
        assertEq(_deliveryLogs(first + 1).length, 0, "no Pong");
        _toDestination();
        assertEq(proxy.debt(canary), debt);
    }

    // ------------------------------------------------------------------------------------------
    // Relay limit
    // ------------------------------------------------------------------------------------------

    /// @dev Three relays answered with a skip by the vault leave the target subscribed and not
    ///      quarantined on either chain. The hub counts requests, and a request is not a rescue.
    function test_relayCountNeverQuarantines() public {
        // Each debt is above `maxDebt` of the vault: the vault answers `DebtAboveCap`.
        Policy memory vaultPolicy = _policy();
        vaultPolicy.maxDebt = 1;
        address canary = _deployCanary();
        _onlyResult(_setPolicy(canary, vaultPolicy), 0, canary, LifelineVault.Outcome.NoNeed);
        // `maxRescues` 1: three relays are more than the rescues that the policy permits.
        _enroll(canary, _policy(1));

        uint256 start = _lasnaMark();
        uint256 vaultBalanceBefore = _vaultBalance();
        uint256 selfCare;
        for (uint256 round = 0; round < 3; ++round) {
            uint256 mark = _lasnaMark();
            uint256 first = _deliveryCount();
            (uint256 triggers, uint256 deliveries) = _debtRound(canary);
            assertEq(triggers, 1, "the hub reacts");
            assertEq(deliveries, 1, "reconcile");

            uint256 requestId = _assertRelayed(mark, canary);
            assertEq(requestId, REQUEST_ID_BASE + round + 1);
            // `_pongIntoDebt` made the delivery at `first`; the relay made the next one.
            Vm.Log[] memory logs = _assertVaultDelivery(first + 1, BASE_SEPOLIA_SWEEP_BASE + SWEEP_PER_TARGET);
            _onlyResult(logs, requestId, canary, LifelineVault.Outcome.DebtAboveCap);
            selfCare += _selfCareSum(logs);
        }

        assertEq(_hubLogs(start, LifelineHub.Relayed.selector), 3, "three Relayed");
        assertEq(_hubLogs(start, LifelineHub.RelayLimited.selector), 0);
        assertEq(_hubLogs(start, LifelineHub.Quarantined.selector), 0, "never Quarantined for a destination target");
        assertEq(_countLogs(start, SYSTEM_ADDRESS, ISystemContract.Unsubscribe.selector), 0, "the tuple is subscribed");
        assertEq(_activeSubscriptions(address(hub)), 2, "the debt tuples of the vault and of the target");
        _assertState(canary, TargetState.Active);

        _toDestination();
        assertFalse(vault.quarantined(canary), "not quarantined on the vault");
        assertEq(
            _vaultBalance(), vaultBalanceBefore - selfCare, "the vault paid nothing: only self-care moved principal"
        );
        assertEq(proxy.reserves(canary), 0);
    }

    /// @dev Drives a target to `maxRelays` relays inside `relayWindow`. The vault has no policy for it, so
    ///      each answer is `NoPolicy`, and another account clears each debt.
    /// @return windowStart The time on Lasna of the first relay.
    function _relaysToTheLimit(address canary, uint256 maxRelays) internal returns (uint256 windowStart) {
        for (uint256 i = 0; i < maxRelays; ++i) {
            uint256 mark = _lasnaMark();
            uint256 first = _deliveryCount();
            (uint256 triggers, uint256 deliveries) = _debtRound(canary);
            assertEq(triggers, 1);
            assertEq(deliveries, 1);
            uint256 requestId = _assertRelayed(mark, canary);
            _onlyResult(_deliveryLogs(first + 1), requestId, canary, LifelineVault.Outcome.NoPolicy);
            // The trigger of the hub is the newest block on Lasna.
            if (i == 0) windowStart = vm.getBlockTimestamp();
        }
    }

    /// @dev At `maxRelays` the hub emits `RelayLimited`, unsubscribes one tuple, and other targets
    ///      still work. `rearm` restores the first.
    function test_relayLimit_parksOnlyThatTuple() public {
        // `maxRescues` 1: `maxRelays = 2 x 1 + 2 = 4`.
        address first = _deployCanary();
        _enroll(first, _policy(1));
        address second = _armedCanary(_policy());
        uint256 start = _lasnaMark();
        _relaysToTheLimit(first, 4);
        assertEq(_hubLogs(start, LifelineHub.Relayed.selector), 4);
        assertEq(_activeSubscriptions(address(hub)), 3, "the vault and two targets");

        // The next debt event of the first target: `RelayLimited`, one `Unsubscribe`, `Parked`, no request.
        uint256 mark = _logMark();
        (uint256 triggers, uint256 deliveries) = _debtRound(first);
        assertEq(triggers, 1, "the hub reacts");
        assertEq(deliveries, 0, "no request");
        _assertTargetLog(mark, LifelineHub.RelayLimited.selector, first);
        _assertHubHeld();
        _assertDebtTuple(_onlyLog(mark, SYSTEM_ADDRESS, ISystemContract.Unsubscribe.selector), first);
        _assertState(first, TargetState.Parked);
        assertEq(_hubLogs(mark, LifelineHub.Relayed.selector), 0);
        assertEq(_hubLogs(mark, LifelineHub.Quarantined.selector), 0);
        assertEq(_hubRequestCount(mark), 0);
        assertEq(_activeSubscriptions(address(hub)), 2);

        // A parked target does not wake the hub, and a forged debt log for it is a skip.
        (triggers, deliveries) = _debtRound(first);
        assertEq(triggers + deliveries, 0);
        mark = _lasnaMark();
        assertTrue(_forgedDebtLog(PROXY_ADDRESS, first).success);
        _assertSkipped(mark, first, LifelineHub.SkipReason.NotActive);

        // The second target on the same chain is still relayed, and the vault rescues it.
        mark = _lasnaMark();
        uint256 firstDelivery = _deliveryCount();
        uint256 debt = _pongIntoDebt(second);
        (triggers, deliveries) = _relayAll();
        assertEq(triggers, 1);
        assertEq(deliveries, 1);
        uint256 requestId = _assertRelayed(mark, second);
        assertEq(
            _onlyResult(_deliveryLogs(firstDelivery + 1), requestId, second, LifelineVault.Outcome.Rescued), debt + HIGH
        );
        _assertState(second, TargetState.Active);

        // `rearm` restores the first target: active, subscribed again, no callback.
        mark = _lasnaMark();
        vm.prank(owner);
        hub.rearm(DESTINATION_CHAIN_ID, first);
        _assertTargetLog(mark, LifelineHub.Rearmed.selector, first);
        _assertDebtTuple(_onlyLog(mark, SYSTEM_ADDRESS, ISystemContract.Subscribe.selector), first);
        _assertState(first, TargetState.Active);
        assertEq(_hubRequestCount(mark), 0, "rearm sends no callback");
        (triggers, deliveries) = _relayAll();
        assertEq(triggers + deliveries, 0);

        // The relay count starts again: the target has `maxRelays` relays before the next limit.
        mark = _lasnaMark();
        _relaysToTheLimit(first, 4);
        assertEq(_hubLogs(mark, LifelineHub.Relayed.selector), 4);
        assertEq(_hubLogs(mark, LifelineHub.RelayLimited.selector), 0);
        _assertState(first, TargetState.Active);
    }

    function test_rearm_revertsForATargetThatIsNotParked() public {
        address canary = _armedCanary(_policy());
        _toLasna();

        vm.expectRevert(abi.encodeWithSelector(LifelineHub.NotParked.selector, DESTINATION_CHAIN_ID, canary));
        vm.prank(owner);
        hub.rearm(DESTINATION_CHAIN_ID, canary);

        vm.expectRevert(abi.encodeWithSelector(LifelineHub.NotParked.selector, DESTINATION_CHAIN_ID, stranger));
        vm.prank(owner);
        hub.rearm(DESTINATION_CHAIN_ID, stranger);

        _disarm(canary);
        vm.expectRevert(abi.encodeWithSelector(LifelineHub.NotParked.selector, DESTINATION_CHAIN_ID, canary));
        vm.prank(owner);
        hub.rearm(DESTINATION_CHAIN_ID, canary);
    }

    /// @dev `unsubscribeAll` parks each active target. `setVault` and `rearm` restore the destination side.
    function test_rearm_restoresATargetAfterUnsubscribeAll() public {
        address canary = _armedCanary(_policy());
        _toLasna();
        vm.prank(owner);
        hub.unsubscribeAll();
        assertEq(_activeSubscriptions(address(hub)), 0);
        _assertState(canary, TargetState.Parked);

        _setVault(BASE_SEPOLIA_SWEEP_BASE);
        vm.prank(owner);
        hub.rearm(DESTINATION_CHAIN_ID, canary);
        assertEq(_activeSubscriptions(address(hub)), 2);

        uint256 mark = _logMark();
        _pongIntoDebt(canary);
        _relayAll();
        _assertRelayed(mark, canary);
        _toDestination();
        assertEq(proxy.debt(canary), 0);
    }

    /// @dev The relay window: a relay at `windowStart + relayWindow` starts a new window with count 1.
    function test_relayWindow_aRelayAtTheEndOfTheWindowStartsANewWindow() public {
        address canary = _deployCanary();
        _enroll(canary, _policy(1));
        uint256 windowStart = _relaysToTheLimit(canary, 4);

        _pongIntoDebt(canary);
        // The trigger of the hub is one block later: at `windowStart + WINDOW` exactly.
        _toLasna();
        _skipTime(windowStart + WINDOW - BLOCK_TIME - vm.getBlockTimestamp());
        uint256 mark = _logMark();
        (uint256 triggers, uint256 deliveries) = _relayAll();
        assertEq(vm.getBlockTimestamp(), windowStart + WINDOW);
        assertEq(triggers, 1);
        assertEq(deliveries, 1);
        _assertRelayed(mark, canary);
        assertEq(_hubLogs(mark, LifelineHub.RelayLimited.selector), 0);
        _clearDebt(canary);

        // The new window has `maxRelays` relays: three more are inside the limit, the next one is not.
        mark = _lasnaMark();
        _relaysToTheLimit(canary, 3);
        assertEq(_hubLogs(mark, LifelineHub.RelayLimited.selector), 0);
        _debtRound(canary);
        _assertTargetLog(mark, LifelineHub.RelayLimited.selector, canary);
    }

    /// @dev The relay window: one second before `windowStart + relayWindow` the limit holds.
    function test_relayWindow_aRelayInsideTheWindowIsLimited() public {
        address canary = _deployCanary();
        _enroll(canary, _policy(1));
        uint256 windowStart = _relaysToTheLimit(canary, 4);

        _pongIntoDebt(canary);
        _toLasna();
        _skipTime(windowStart + WINDOW - 1 - BLOCK_TIME - vm.getBlockTimestamp());
        uint256 mark = _logMark();
        (uint256 triggers, uint256 deliveries) = _relayAll();
        assertEq(vm.getBlockTimestamp(), windowStart + WINDOW - 1);
        assertEq(triggers, 1);
        assertEq(deliveries, 0);
        _assertTargetLog(mark, LifelineHub.RelayLimited.selector, canary);
        _assertState(canary, TargetState.Parked);
    }

    /// @dev The wrong order costs one `NoPolicy` result; `setPolicy` then clears the debt locally.
    function test_enrollBeforePolicy_selfHeals() public {
        address canary = _deployCanary();
        uint256 mark = _lasnaMark();
        _enroll(canary, _policy());
        assertEq(_hubRequestCount(mark), 0, "hub.enroll sends no callback");
        (uint256 triggers, uint256 deliveries) = _relayAll();
        assertEq(triggers + deliveries, 0, "nothing is burned at enrollment");

        // The debt event arrives before the policy exists.
        mark = _lasnaMark();
        uint256 first = _deliveryCount();
        uint256 vaultBalanceBefore = _vaultBalance();
        uint256 debt = _pongIntoDebt(canary);
        (triggers, deliveries) = _relayAll();
        assertEq(triggers, 1);
        assertEq(deliveries, 1);
        uint256 requestId = _assertRelayed(mark, canary);
        Vm.Log[] memory logs = _assertVaultDelivery(first + 1, BASE_SEPOLIA_SWEEP_BASE + SWEEP_PER_TARGET);
        assertEq(_onlyResult(logs, requestId, canary, LifelineVault.Outcome.NoPolicy), 0);

        // The cost: one callback fee from the float, and no principal.
        _toDestination();
        assertEq(proxy.debt(canary), debt, "the canary stays in debt");
        assertEq(proxy.reserves(address(vault)), FEE_FLOAT - _deliveryAt(first + 1).fee, "one callback fee");
        assertEq(_vaultBalance(), vaultBalanceBefore);

        // `setPolicy` repairs the target locally: no callback and no reaction.
        uint256 deliveriesBefore = _deliveryCount();
        mark = _lasnaMark();
        logs = _setPolicy(canary, _policy());
        assertEq(_onlyResult(logs, 0, canary, LifelineVault.Outcome.Rescued), debt + HIGH);
        assertTrue(_debtCleared(logs, canary), "WhitelistContract(canary)");
        assertEq(proxy.debt(canary), 0);
        assertEq(proxy.reserves(canary), HIGH);
        (triggers, deliveries) = _relayAll();
        assertEq(triggers + deliveries, 0);
        assertEq(_deliveryCount(), deliveriesBefore, "no callback");
        assertEq(_hubRequestCount(mark), 0);
        _assertState(canary, TargetState.Active);
    }

    // ------------------------------------------------------------------------------------------
    // Guards of the two handlers
    // ------------------------------------------------------------------------------------------

    /// @dev A debt log of the proxy for a contract that the hub does not have is a skip with no request.
    function test_guard_debtLogOfAContractThatIsNotEnrolled() public {
        uint256 mark = _lasnaMark();
        uint256 balanceBefore = address(hub).balance;
        assertTrue(_forgedDebtLog(PROXY_ADDRESS, stranger).success);
        _assertSkipped(mark, stranger, LifelineHub.SkipReason.NotEnrolled);
        assertEq(_hubRequestCount(mark), 0);
        assertEq(address(hub).balance, balanceBefore);
    }

    /// @dev A log of another emitter on the destination chain does not reach the two handlers.
    function test_guard_forgedEmitter() public {
        address canary = _armedCanary(_policy());
        uint256 mark = _lasnaMark();
        assertTrue(_forgedDebtLog(stranger, canary).success);
        _assertSkipped(mark, canary, LifelineHub.SkipReason.WrongEmitter);

        mark = _logMark();
        assertTrue(_forgedDebtLog(stranger, address(vault)).success);
        _assertSkipped(mark, address(vault), LifelineHub.SkipReason.WrongEmitter);
        assertTrue(_vaultIsUp(), "the vault state did not change");
        assertEq(_hubRequestCount(mark), 0);
    }

    /// @dev A callback request that the system contract rejects is a skip: no revert, no `requestId` used and
    ///      no relay counted. The target still has its `maxRelays` relays.
    function test_guard_aFailedRequestIsASkip() public {
        // The vault has no policy for the canary. `maxRescues` 1: `maxRelays` is 4.
        address canary = _deployCanary();
        _enroll(canary, _policy(1));
        vm.mockCallRevert(SYSTEM_ADDRESS, abi.encodeWithSelector(ISystemContract.requestCallbackV_1_0.selector), "");
        for (uint256 i = 0; i < 2; ++i) {
            uint256 mark = _logMark();
            assertTrue(_forgedDebtLog(PROXY_ADDRESS, canary).success);
            _assertSkipped(mark, canary, LifelineHub.SkipReason.RequestFailed);
            assertEq(_hubLogs(mark, LifelineHub.Relayed.selector), 0);
        }
        vm.clearMockedCalls();

        uint256 start = _logMark();
        _assertFourRelaysThenTheLimit(canary);
        assertEq(_hubLog(start, LifelineHub.Relayed.selector, 0).topics[1], REQUEST_ID_BASE + 1);
    }

    /// @dev A target with `maxRelays` 4 that has no relay counted: four debt events are relayed, and the
    ///      fifth is at the limit.
    function _assertFourRelaysThenTheLimit(address canary) internal {
        uint256 mark = _lasnaMark();
        _relaysToTheLimit(canary, 4);
        assertEq(_hubLogs(mark, LifelineHub.Relayed.selector), 4);
        assertEq(_hubLogs(mark, LifelineHub.RelayLimited.selector), 0, "no relay was counted before");
        _assertState(canary, TargetState.Active);
        _debtRound(canary);
        _assertTargetLog(mark, LifelineHub.RelayLimited.selector, canary);
    }

    /// @dev `vaultUp` reverts if the system contract rejects the request, so the pending set stays whole.
    function test_vaultUp_revertsIfTheRequestFails() public {
        address canary = _armedCanary(_policy());
        _takeVaultDown();
        _pongIntoDebt(canary);
        _relayAll();
        assertEq(_pendingCount(), 1);

        vm.mockCallRevert(SYSTEM_ADDRESS, abi.encodeWithSelector(ISystemContract.requestCallbackV_1_0.selector), "");
        vm.expectRevert(abi.encodeWithSelector(LifelineHub.RequestFailed.selector, DESTINATION_CHAIN_ID));
        vm.prank(owner);
        hub.vaultUp(DESTINATION_CHAIN_ID);
        vm.clearMockedCalls();

        assertEq(_pendingCount(), 1);
        assertFalse(_vaultIsUp());
    }

    /// @dev Fuzz: a forged debt log of the destination proxy never reverts the reaction and never moves
    ///      principal, for a target in each state and for the vault itself.
    function test_forgedDestinationDebtLog_neverRevertsAndNeverPays(uint256 seed, address other) public {
        address active = _armedCanary(_policy());
        address disarmed = _armedCanary(_policy());
        _disarm(disarmed);
        if (seed % 2 == 1) assertTrue(_forgedDebtLog(PROXY_ADDRESS, address(vault)).success);

        address[5] memory targets = [active, disarmed, address(vault), address(hub), other];
        uint256 balanceBefore = address(hub).balance;
        for (uint256 i = 0; i < 2; ++i) {
            address target = targets[(seed / 2 + i) % targets.length];
            uint256 mark = _lasnaMark();
            TriggerResult memory result = _forgedDebtLog(PROXY_ADDRESS, target);
            assertTrue(result.success, "the trigger succeeded");
            assertFalse(result.reactReverted, "no reverted reaction");
            assertEq(_countLogsOf(mark, address(hub)), 1 + _hubLogs(mark, LifelineHub.SelfCare.selector));
        }
        assertEq(_hubLogs(0, LifelineHub.Quarantined.selector), 0);
        assertEq(_hubLogs(0, LifelineHub.Rescued.selector), 0);
        assertEq(address(hub).balance + _hubSelfCareSum(), balanceBefore, "only self-care moved principal");
    }

    /// @dev The sum of `SelfCare` of the hub after the deployment.
    function _hubSelfCareSum() internal returns (uint256 sum) {
        // The first `SelfCare` is the one of the constructor: the float.
        uint256 count = _hubLogs(0, LifelineHub.SelfCare.selector);
        for (uint256 i = 1; i < count; ++i) {
            sum += abi.decode(_hubLog(0, LifelineHub.SelfCare.selector, i).data, (uint256));
        }
    }

    // ------------------------------------------------------------------------------------------
    // The request functions
    // ------------------------------------------------------------------------------------------

    /// @dev `_requestVault` sends nothing if the vault is not registered or not up, or if `n` is 0 or above
    ///      `SWEEP_MAX`. If not, the gas limit is `sweepBase + SWEEP_PER_TARGET x n`.
    function test_requestVault_checksTheVaultAndTheCount() public {
        _toLasna();
        vm.prank(owner);
        HubSeams seams = new HubSeams(SYSTEM_ADDRESS);
        bytes memory payload = abi.encodeCall(LifelineVault.reconcile, (address(0), address(1), 7));
        bytes32 topic0 = ISystemContractCallbacks.CallbackRequest.selector;

        uint256 mark = _logMark();
        assertFalse(seams.requestVault(DESTINATION_CHAIN_ID, payload, 1), "no vault on that chain");
        vm.prank(owner);
        seams.setVault(DESTINATION_CHAIN_ID, PROXY_ADDRESS, address(vault), BASE_SEPOLIA_SWEEP_BASE);
        assertFalse(seams.requestVault(DESTINATION_CHAIN_ID, payload, 0), "n is 0");
        assertFalse(seams.requestVault(DESTINATION_CHAIN_ID, payload, SWEEP_MAX + 1), "n is above SWEEP_MAX");
        assertFalse(seams.requestVault(CHAIN_WITHOUT_VAULT, payload, 1), "no vault on that chain");
        assertFalse(seams.requestSweep(DESTINATION_CHAIN_ID, new address[](0), 1), "an empty sweep");
        assertFalse(seams.requestSweep(DESTINATION_CHAIN_ID, new address[](SWEEP_MAX + 1), 1), "a sweep too long");
        assertEq(_countLogs(mark, SYSTEM_ADDRESS, topic0), 0, "no request");

        for (uint256 n = 1; n <= SWEEP_MAX; ++n) {
            mark = _logMark();
            address[] memory targets = new address[](n);
            for (uint256 i = 0; i < n; ++i) {
                targets[i] = address(uint160(100 + i));
            }
            assertTrue(seams.requestSweep(DESTINATION_CHAIN_ID, targets, 42));

            ChainLog memory log = _onlyLog(mark, SYSTEM_ADDRESS, topic0);
            assertEq(log.topics[1], DESTINATION_CHAIN_ID);
            assertEq(log.topics[2], uint256(uint160(address(seams))), "the requester is the hub");
            assertEq(log.topics[3], uint256(uint160(address(vault))));
            ICallbackProxy.CallbackConfiguration_V_1_0 memory config = _requestConfig(log);
            assertEq(config.gasLimit, BASE_SEPOLIA_SWEEP_BASE + SWEEP_PER_TARGET * n);
            assertEq(config.payload, abi.encodeCall(LifelineVault.sweep, (address(0), targets, 42)));
        }

        // No request goes to a vault that is down.
        _trigger(address(seams), _debtLogRecord(DESTINATION_CHAIN_ID, PROXY_ADDRESS, address(vault), 1));
        mark = _logMark();
        assertFalse(seams.requestVault(DESTINATION_CHAIN_ID, payload, 1), "the vault is down");
        assertEq(_countLogs(mark, SYSTEM_ADDRESS, topic0), 0);
    }

    // ------------------------------------------------------------------------------------------
    // Sweep gas limit
    // ------------------------------------------------------------------------------------------

    /// @dev One catch-up sweep of `n` enrolled targets in debt: the targets go into debt while the vault is
    ///      down, then `depositTo` and `vaultUp`. The sweep is delivered with exactly the computed gas
    ///      limit and must rescue each target: no `CallbackFailure` and no `SweepPartial`.
    /// @return gasUsed The gas of the delivery transaction.
    function _sweepFits(uint256 n, uint256 sweepBase) internal returns (uint256 gasUsed) {
        _toDestination();
        assertEq(SWEEP_BASE_GAS + proxy._extraGas() + proxy._maxChargeGas(), sweepBase, "the proxy configuration");
        address[] memory targets = _armedCanaries(n);

        _takeVaultDown();
        for (uint256 i = 0; i < n; ++i) {
            _pongIntoDebt(targets[i]);
        }
        (uint256 triggers, uint256 deliveries) = _relayAll();
        assertEq(triggers, n, "one reaction for each debt event");
        assertEq(deliveries, 0, "no request to a vault that is down");
        assertEq(_pendingCount(), n);

        _restoreVault();
        uint256 first = _deliveryCount();
        UpLog memory up = _vaultUp();
        assertEq(up.sent, n);
        assertEq(up.remaining, 0);
        (triggers, deliveries) = _relayAll();
        assertEq(triggers, 0);
        assertEq(deliveries, 1, "one sweep");

        Vm.Log[] memory logs = _assertVaultDelivery(first, sweepBase + SWEEP_PER_TARGET * n);
        assertEq(_vaultLogs(logs, LifelineVault.SweepPartial.selector), 0, "no SweepPartial");
        _assertAllRescued(logs, up.requestId, targets);

        gasUsed = _deliveryAt(first).gasUsed;
        console2.log(
            string.concat(
                "SWEEP_GAS n=", vm.toString(n), " base=", vm.toString(sweepBase), " used=", vm.toString(gasUsed)
            )
        );
    }

    /// @dev Checks the logs of one sweep: one `Result(requestId, target, Rescued, paid)` with a payment for
    ///      each target of the list, in any order, and each debt is 0.
    function _assertAllRescued(Vm.Log[] memory logs, uint256 requestId, address[] memory targets) internal {
        assertEq(_vaultLogs(logs, LifelineVault.Result.selector), targets.length, "one Result for each target");
        _toDestination();
        for (uint256 i = 0; i < targets.length; ++i) {
            ResultLog memory result = _result(logs, i);
            assertEq(result.id, requestId, "Result id");
            assertEq(uint256(result.outcome), uint256(LifelineVault.Outcome.Rescued), "Result outcome");
            assertGt(result.paid, 0, "a payment");
            assertTrue(_contains(targets, result.target), "Result target");
            for (uint256 j = 0; j < i; ++j) {
                assertTrue(_result(logs, j).target != result.target, "one Result for each target");
            }
            assertEq(proxy.debt(targets[i]), 0, "debt is 0");
            assertEq(proxy.reserves(targets[i]), HIGH);
        }
    }

    /// @dev The proxy configuration of Ethereum Sepolia on the Base Sepolia fork: `_extraGas` and
    ///      `_maxChargeGas` of the live Ethereum Sepolia proxy, and the matching `sweepBase` in the hub.
    function _useEthereumSepoliaConfiguration() internal {
        _setProxyGas(ETHEREUM_SEPOLIA_PROXY_EXTRA_GAS, ETHEREUM_SEPOLIA_PROXY_MAX_CHARGE_GAS);
        _setVault(ETHEREUM_SEPOLIA_SWEEP_BASE);
    }

    /// @dev Fuzz: for n = 1..8 with all targets topped up, the sweep completes inside the computed
    ///      limit on each proxy configuration: `sweepBase` 220,000 with the Base Sepolia proxy values, and
    ///      290,000 with the Ethereum Sepolia proxy values.
    function test_sweepFitsItsGasLimit(uint256 n) public {
        n = bound(n, 1, SWEEP_MAX);
        _sweepFits(n, BASE_SEPOLIA_SWEEP_BASE);
        _useEthereumSepoliaConfiguration();
        _sweepFits(n, ETHEREUM_SEPOLIA_SWEEP_BASE);
    }

    /// @dev Each n of 1..8 on each configuration, one time. This test prints the 16 `SWEEP_GAS` lines.
    function test_sweepFitsItsGasLimit_eachCountOnEachConfiguration() public {
        for (uint256 n = 1; n <= SWEEP_MAX; ++n) {
            _sweepFits(n, BASE_SEPOLIA_SWEEP_BASE);
        }
        _useEthereumSepoliaConfiguration();
        for (uint256 n = 1; n <= SWEEP_MAX; ++n) {
            _sweepFits(n, ETHEREUM_SEPOLIA_SWEEP_BASE);
        }
    }

    // ------------------------------------------------------------------------------------------
    // Vault down, pending and catch-up
    // ------------------------------------------------------------------------------------------

    /// @dev Float emptied, `VaultDown`, no further requests, `depositTo`, `vaultUp`, pending
    ///      targets rescued.
    function test_vaultDownUp_catchUp() public {
        address[] memory targets = _armedCanaries(2);
        uint256 vaultBalanceBefore = _vaultBalance();

        // Junk deliveries empty the float until `BlacklistContract(vault)`. The hub emits `VaultDown`.
        uint256 start = _lasnaMark();
        _takeVaultDown();
        assertEq(_vaultBalance(), vaultBalanceBefore, "the flood took no principal");

        // Two targets go into debt: two `Pending`, the same target is stored once, no request.
        uint256 debts = _twoTargetsGoPending(targets[0], targets[1]);
        assertEq(_hubRequestCount(start), 0, "no CallbackRequest");
        assertEq(_hubLogs(start, LifelineHub.Relayed.selector), 0, "no relay counted");
        _assertState(targets[0], TargetState.Active);
        _assertState(targets[1], TargetState.Active);

        // One transaction restores the vault, then `vaultUp` sends one sweep.
        _restoreVault();
        uint256 first = _deliveryCount();
        uint256 mark = _lasnaMark();
        UpLog memory up = _vaultUp();
        assertEq(up.requestId, REQUEST_ID_BASE + 1, "the first request of the hub");
        assertEq(up.sent, 2);
        assertEq(up.remaining, 0);
        assertEq(_pendingCount(), 0);
        _assertSweepRequest(mark, up.requestId, targets);

        (uint256 triggers, uint256 deliveries) = _relayAll();
        assertEq(triggers, 0);
        assertEq(deliveries, 1, "one sweep");
        Vm.Log[] memory logs = _assertVaultDelivery(first, BASE_SEPOLIA_SWEEP_BASE + SWEEP_PER_TARGET * 2);
        _assertAllRescued(logs, up.requestId, targets);
        assertEq(_result(logs, 0).paid + _result(logs, 1).paid, debts + 2 * HIGH);
        assertEq(_vaultBalance(), vaultBalanceBefore - debts - 2 * HIGH);

        // The vault is up again: the next debt event is relayed.
        address c = _armedCanary(_policy());
        mark = _lasnaMark();
        _pongIntoDebt(c);
        (triggers, deliveries) = _relayAll();
        assertEq(triggers, 1);
        assertEq(deliveries, 1);
        assertEq(_assertRelayed(mark, c), REQUEST_ID_BASE + 2);
        _toDestination();
        assertEq(proxy.debt(c), 0);
    }

    /// @dev While the vault is down, two targets go into debt: two `Pending` and no delivery. Then the debt
    ///      of the first one is cleared from outside and it goes into debt again: the set does not change.
    /// @return debts The sum of the two debts at the end.
    function _twoTargetsGoPending(address a, address b) internal returns (uint256 debts) {
        uint256 mark = _lasnaMark();
        _pongIntoDebt(a);
        debts = _pongIntoDebt(b);
        (uint256 triggers, uint256 deliveries) = _relayAll();
        assertEq(triggers, 2);
        assertEq(deliveries, 0);
        assertEq(_hubLogs(mark, LifelineHub.Pending.selector), 2, "two Pending");
        assertEq(_hubLog(mark, LifelineHub.Pending.selector, 0).topics[1], DESTINATION_CHAIN_ID);
        assertEq(_hubLog(mark, LifelineHub.Pending.selector, 0).topics[2], uint256(uint160(a)));
        assertEq(_hubLog(mark, LifelineHub.Pending.selector, 1).topics[2], uint256(uint160(b)));
        _assertHubHeld();
        assertEq(_pendingCount(), 2);

        _clearDebt(a);
        debts += _pongIntoDebt(a);
        uint256 again = _lasnaMark();
        (triggers, deliveries) = _relayAll();
        assertEq(triggers, 1);
        assertEq(deliveries, 0);
        _assertSkipped(again, a, LifelineHub.SkipReason.AlreadyPending);
        assertEq(_pendingCount(), 2, "the same target is stored once");
        assertEq(_hubLogs(mark, LifelineHub.Pending.selector), 2);
    }

    /// @dev Checks the one request of the hub after a mark: a sweep of exactly these targets, in any order,
    ///      with the `requestId` as its id and the computed gas limit.
    function _assertSweepRequest(uint256 mark, uint256 requestId, address[] memory targets) internal {
        ICallbackProxy.CallbackConfiguration_V_1_0 memory request = _hubRequest(mark);
        assertEq(request.chainId, DESTINATION_CHAIN_ID);
        assertEq(request.recipient, address(vault));
        (address[] memory swept, uint256 id) = _sweepArguments(request.payload);
        assertEq(id, requestId, "the id of the sweep is the requestId");
        assertEq(swept.length, targets.length);
        assertEq(request.gasLimit, BASE_SEPOLIA_SWEEP_BASE + SWEEP_PER_TARGET * swept.length);
        for (uint256 i = 0; i < targets.length; ++i) {
            assertTrue(_contains(swept, targets[i]), "a pending target is in the sweep");
        }
    }

    /// @dev A debt event that the hub stores as pending counts no relay, and a catch-up sweep counts no
    ///      relay: after them the target still has its `maxRelays` relays inside the window.
    function test_pending_andTheCatchUpSweepCountNoRelay() public {
        // The vault has no policy for the canary. `maxRescues` 1: `maxRelays` is 4.
        address canary = _deployCanary();
        _enroll(canary, _policy(1));
        uint256 start = _lasnaMark();

        // Two debt events while the vault is down: `Pending`, then `AlreadyPending`.
        _takeVaultDown();
        _debtRound(canary);
        _debtRound(canary);
        assertEq(_hubLogs(start, LifelineHub.Pending.selector), 1);
        assertEq(_pendingCount(), 1);

        // The catch-up sweep. The vault answers `NoPolicy`.
        _restoreVault();
        uint256 first = _deliveryCount();
        UpLog memory up = _vaultUp();
        assertEq(up.sent, 1);
        _relayAll();
        Vm.Log[] memory logs = _assertVaultDelivery(first, BASE_SEPOLIA_SWEEP_BASE + SWEEP_PER_TARGET);
        _onlyResult(logs, up.requestId, canary, LifelineVault.Outcome.NoPolicy);
        assertEq(_hubLogs(start, LifelineHub.Relayed.selector), 0);

        // The target has all four relays; the fifth debt event is at the limit.
        _assertFourRelaysThenTheLimit(canary);
    }

    /// @dev With an empty pending set `vaultUp` sets the state up, sends no request and emits
    ///      `VaultUp(chainId, 0, 0, 0)`.
    function test_vaultUp_anEmptySetSendsNoRequest() public {
        _assertNothingSent(_vaultUp());

        assertTrue(_forgedDebtLog(PROXY_ADDRESS, address(vault)).success);
        assertFalse(_vaultIsUp());
        _assertNothingSent(_vaultUp());
        (uint256 triggers, uint256 deliveries) = _relayAll();
        assertEq(triggers + deliveries, 0);
    }

    /// @dev Completion criterion: with 9 pending targets, two `vaultUp` calls give 9 `Result` events and 0
    ///      remaining.
    function test_vaultUp_ninePendingTargetsNeedTwoCalls() public {
        address[] memory targets = _armedCanaries(SWEEP_MAX + 1);
        _takeVaultDown();
        for (uint256 i = 0; i < targets.length; ++i) {
            _pongIntoDebt(targets[i]);
        }
        (uint256 triggers, uint256 deliveries) = _relayAll();
        assertEq(triggers, 9);
        assertEq(deliveries, 0);
        assertEq(_pendingCount(), 9);
        assertEq(hub.targetCount(DESTINATION_CHAIN_ID), 9, "the set is not larger than the enrolled targets");

        _restoreVault();
        uint256 first = _deliveryCount();
        UpLog memory up1 = _vaultUp();
        assertEq(up1.sent, 8);
        assertEq(up1.remaining, 1);
        assertEq(_pendingCount(), 1);
        UpLog memory up2 = _vaultUp();
        assertEq(up2.sent, 1);
        assertEq(up2.remaining, 0);
        assertEq(_pendingCount(), 0);
        assertEq(up2.requestId, up1.requestId + 1);

        (triggers, deliveries) = _relayAll();
        assertEq(triggers, 0);
        assertEq(deliveries, 2, "two sweeps");

        Vm.Log[] memory logs = _assertVaultDelivery(first, BASE_SEPOLIA_SWEEP_BASE + SWEEP_PER_TARGET * 8);
        Vm.Log[] memory logs2 = _assertVaultDelivery(first + 1, BASE_SEPOLIA_SWEEP_BASE + SWEEP_PER_TARGET);
        assertEq(_vaultLogs(logs, LifelineVault.Result.selector), 8);
        assertEq(_vaultLogs(logs2, LifelineVault.Result.selector), 1);
        assertEq(_vaultLogs(logs, LifelineVault.SweepPartial.selector), 0);
        for (uint256 i = 0; i < 8; ++i) {
            ResultLog memory result = _result(logs, i);
            assertEq(result.id, up1.requestId);
            assertEq(uint256(result.outcome), uint256(LifelineVault.Outcome.Rescued));
            assertTrue(result.target != _result(logs2, 0).target);
        }
        assertEq(_result(logs2, 0).id, up2.requestId);
        assertEq(uint256(_result(logs2, 0).outcome), uint256(LifelineVault.Outcome.Rescued));

        _toDestination();
        for (uint256 i = 0; i < targets.length; ++i) {
            assertEq(proxy.debt(targets[i]), 0, "each of the 9 targets is rescued");
            assertEq(proxy.reserves(targets[i]), HIGH);
        }

        // A third call has nothing to send.
        _assertNothingSent(_vaultUp());
    }

    /// @dev Completion criterion: a target disarmed while pending is in no sweep.
    function test_disarm_aTargetDisarmedWhilePendingIsInNoSweep() public {
        address[] memory targets = _armedCanaries(3);
        _takeVaultDown();
        uint256 debt = _pongIntoDebt(targets[0]);
        _pongIntoDebt(targets[1]);
        _pongIntoDebt(targets[2]);
        _relayAll();
        assertEq(_pendingCount(), 3);

        // The first entry of the set leaves it: the last entry takes its place.
        _disarm(targets[0]);
        assertEq(_pendingCount(), 2);
        assertEq(hub.targetCount(DESTINATION_CHAIN_ID), 2);
        address[] memory left = new address[](2);
        left[0] = targets[1];
        left[1] = targets[2];

        _restoreVault();
        uint256 first = _deliveryCount();
        uint256 mark = _lasnaMark();
        UpLog memory up = _vaultUp();
        assertEq(up.sent, 2);
        assertEq(up.remaining, 0);
        // The payload has exactly the two other targets.
        _assertSweepRequest(mark, up.requestId, left);

        _relayAll();
        Vm.Log[] memory logs = _assertVaultDelivery(first, BASE_SEPOLIA_SWEEP_BASE + SWEEP_PER_TARGET * 2);
        // Exactly two `Result`, and each is for one of the two other targets.
        _assertAllRescued(logs, up.requestId, left);
        assertEq(proxy.debt(targets[0]), debt, "the disarmed target stays in debt");

        // The target can be enrolled again; it is not pending.
        _enroll(targets[0], _policy());
        assertEq(_pendingCount(), 0);
        _assertNothingSent(_vaultUp());
    }

    /// @dev A second debt log of the vault while it is down emits `VaultDown` again and changes nothing.
    function test_vaultDown_isReportedForEachDebtLogOfTheVault() public {
        uint256 mark = _lasnaMark();
        assertTrue(_forgedDebtLog(PROXY_ADDRESS, address(vault)).success);
        assertTrue(_forgedDebtLog(PROXY_ADDRESS, address(vault)).success);
        assertEq(_hubLogs(mark, LifelineHub.VaultDown.selector), 2);
        assertEq(_hubLogs(mark, LifelineHub.Skipped.selector), 0);
        assertFalse(_vaultIsUp());
        _assertHubHeld();
    }
}
