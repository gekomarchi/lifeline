// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

import { Vm } from "forge-std/Vm.sol";
import { ProxyDeliveryHarness } from "./harness/ProxyDeliveryHarness.sol";
import { LifelineVault, VAULT_ITER_RESERVE, VAULT_TAIL_RESERVE } from "../src/LifelineVault.sol";
import { Policy, InvalidPolicy, MAX_RESCUES_LIMIT } from "../src/LifelineTypes.sol";
import { ICallbackProxy, IProxyLedger } from "../src/interfaces/IProxyLedger.sol";
import { CanaryCallback } from "../src/fixtures/CanaryCallback.sol";

/// @notice Fork tests of the destination payer on Base Sepolia. The vault, the canaries and the callback
///         proxy are real; only the node's delivery is emulated by the harness.
/// @dev The tests that use `vm.mockCallRevert` replace one answer of the proxy, because the real ledger never
///      reverts a read or a deposit.
contract LifelineVaultTest is ProxyDeliveryHarness {
    uint256 internal constant PRINCIPAL = 0.01 ether;
    uint256 internal constant FEE_FLOAT = 1.5e13;
    uint256 internal constant SELF_CARE_DAILY_CAP = 3e13;
    uint256 internal constant PRINCIPAL_FLOOR = 0.001 ether;

    uint256 internal constant LOW = 1e12;
    uint256 internal constant HIGH = 5e12;
    uint256 internal constant MAX_DEBT = 1e13;
    uint256 internal constant DAILY_CAP = 1e14;
    uint32 internal constant WINDOW = 1 hours;

    /// @dev Below the fee of one callback (more than 4e11 wei at the pinned gas price), so each callback
    ///      after a rescue is a new debt.
    uint256 internal constant HIGH_BELOW_ONE_FEE = 1e11;

    address internal owner;
    address internal hub; // the value of `callbackSender`: the requester of the authenticated callbacks
    address internal stranger; // a requester that is not the hub, and an account that is not the owner
    LifelineVault internal vault;

    function setUp() public {
        _forkBaseSepolia();
        owner = _freshAccount("owner");
        hub = _freshAccount("hub");
        stranger = _freshAccount("stranger");
        vm.deal(owner, 1 ether);
        vm.deal(stranger, 1 ether);
        vault = _deployVault(PRINCIPAL + FEE_FLOAT, FEE_FLOAT, FEE_FLOAT, SELF_CARE_DAILY_CAP, PRINCIPAL_FLOOR);
        _keepClearOfMidnight();
    }

    // ------------------------------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------------------------------

    struct ResultLog {
        uint256 id;
        address target;
        LifelineVault.Outcome outcome;
        uint256 paid;
    }

    function _deployVault(uint256 value, uint256 feeFloat, uint256 reserveTarget, uint256 selfCareCap, uint256 floor)
        internal
        returns (LifelineVault deployed)
    {
        _nextFreshContract(owner);
        vm.prank(owner);
        deployed = new LifelineVault{ value: value }(PROXY_ADDRESS, hub, feeFloat, reserveTarget, selfCareCap, floor);
    }

    /// @dev An unfunded canary: no balance, no reserves, no debt.
    function _deployCanary() internal returns (address canary) {
        _nextFreshContract(owner);
        vm.prank(owner);
        canary = address(new CanaryCallback());
    }

    /// @dev `low` is 0, so `setPolicy` does not fund a target that has no debt.
    function _policy() internal pure returns (Policy memory) {
        return Policy({ low: 0, high: HIGH, maxDebt: MAX_DEBT, dailyCap: DAILY_CAP, maxRescues: 2, window: WINDOW });
    }

    /// @dev `high` is below one callback fee: each callback after a rescue is a new debt.
    function _debtLoopPolicy(uint32 maxRescues, uint32 window) internal pure returns (Policy memory policy) {
        policy = _policy();
        policy.high = HIGH_BELOW_ONE_FEE;
        policy.maxRescues = maxRescues;
        policy.window = window;
    }

    /// @dev The owner transaction `setPolicy` and its logs.
    function _setPolicy(address target, Policy memory policy) internal returns (Vm.Log[] memory logs) {
        _takeLogs();
        vm.prank(owner);
        vault.setPolicy(target, policy);
        logs = _takeLogs();
    }

    /// @dev Delivers `pong` to a canary. The requester is not the hub: a canary has no access check.
    function _pong(address canary, uint256 n) internal returns (Delivery memory) {
        return _deliver(canary, abi.encodeCall(CanaryCallback.pong, (address(0), n)), DELIVERY_GAS, stranger);
    }

    /// @dev Delivers `pong` to a canary whose reserves do not cover the fee: the callback runs and leaves a debt.
    function _pongIntoDebt(address canary) internal returns (uint256 debt) {
        Delivery memory d = _pong(canary, 1);
        _assertPongRan(d, canary, 1);
        assertTrue(_wentIntoDebt(d.logs, canary), "BlacklistContract(canary)");
        debt = proxy.debt(canary);
        assertGt(debt, 0, "the canary is in debt");
        assertEq(proxy.reserves(canary), 0);
    }

    /// @dev Delivers `pong` until the canary is in debt.
    function _drainIntoDebt(address canary) internal returns (uint256 debt) {
        for (uint256 i = 0; i < 64 && proxy.debt(canary) == 0; ++i) {
            _assertPongRan(_pong(canary, 1), canary, 1);
        }
        debt = proxy.debt(canary);
        assertGt(debt, 0, "the canary is in debt");
    }

    /// @dev A canary with a policy and a debt. `policy.low` is 0, so `setPolicy` did not fund it.
    function _armedCanaryInDebt(Policy memory policy) internal returns (address canary, uint256 debt) {
        canary = _deployCanary();
        _onlyResult(_setPolicy(canary, policy), 0, canary, LifelineVault.Outcome.NoNeed);
        debt = _pongIntoDebt(canary);
    }

    /// @dev `count` canaries with the default policy and a debt, in the order of their deployment.
    function _armedCanariesInDebt(uint256 count) internal returns (address[] memory canaries, uint256[] memory debts) {
        canaries = new address[](count);
        debts = new uint256[](count);
        for (uint256 i = 0; i < count; ++i) {
            (canaries[i], debts[i]) = _armedCanaryInDebt(_policy());
        }
    }

    /// @dev Delivers `reconcile` with the first argument equal to `callbackSender`.
    function _reconcile(address target, uint256 requestId) internal returns (Delivery memory) {
        return _deliver(address(vault), _reconcilePayload(target, requestId), DELIVERY_GAS, hub);
    }

    /// @dev Delivers `sweep` with the first argument equal to `callbackSender`.
    function _sweep(address[] memory targets, uint256 tickId, uint64 gasLimit) internal returns (Delivery memory) {
        return _deliver(address(vault), _sweepPayload(targets, tickId), gasLimit, hub);
    }

    /// @dev The payload as the requester gives it: the first argument is 0 and the node overwrites it.
    function _reconcilePayload(address target, uint256 requestId) internal pure returns (bytes memory) {
        return abi.encodeCall(LifelineVault.reconcile, (address(0), target, requestId));
    }

    function _sweepPayload(address[] memory targets, uint256 tickId) internal pure returns (bytes memory) {
        return abi.encodeCall(LifelineVault.sweep, (address(0), targets, tickId));
    }

    function _list(address a) internal pure returns (address[] memory targets) {
        targets = new address[](1);
        targets[0] = a;
    }

    function _list(address a, address b) internal pure returns (address[] memory targets) {
        targets = new address[](2);
        targets[0] = a;
        targets[1] = b;
    }

    function _assertPongRan(Delivery memory d, address canary, uint256 n) internal pure {
        _assertRan(d);
        Vm.Log memory pong = _onlyLog(d.logs, canary, CanaryCallback.Pong.selector);
        assertEq(uint256(pong.topics[1]), n, "Pong(n)");
    }

    function _vaultLogs(Vm.Log[] memory logs, bytes32 topic0) internal view returns (uint256) {
        return _countLogs(logs, address(vault), topic0);
    }

    /// @dev Checks that the vault emitted exactly one log with this `topic0`, and that its indexed target is
    ///      `target`.
    function _assertOnlyVaultLog(Vm.Log[] memory logs, bytes32 topic0, address target) internal view {
        Vm.Log memory log = _onlyLog(logs, address(vault), topic0);
        assertEq(address(uint160(uint256(log.topics[1]))), target);
    }

    /// @dev The `n`-th `Result` (from 0) of the vault.
    function _result(Vm.Log[] memory logs, uint256 n) internal view returns (ResultLog memory result) {
        Vm.Log memory log = _findLog(logs, address(vault), LifelineVault.Result.selector, n);
        result.id = uint256(log.topics[1]);
        result.target = address(uint160(uint256(log.topics[2])));
        (result.outcome, result.paid) = abi.decode(log.data, (LifelineVault.Outcome, uint256));
    }

    /// @dev Checks the `n`-th `Result` of the vault and returns its payment.
    function _assertResult(Vm.Log[] memory logs, uint256 n, uint256 id, address target, LifelineVault.Outcome outcome)
        internal
        view
        returns (uint256 paid)
    {
        ResultLog memory result = _result(logs, n);
        assertEq(result.id, id, "Result id");
        assertEq(result.target, target, "Result target");
        assertEq(uint256(result.outcome), uint256(outcome), "Result outcome");
        bool pays = outcome == LifelineVault.Outcome.Rescued || outcome == LifelineVault.Outcome.ToppedUp;
        if (pays) assertGt(result.paid, 0, "a payment is above 0");
        else assertEq(result.paid, 0, "a skip pays 0");
        return result.paid;
    }

    /// @dev Checks that the vault emitted exactly one `Result` and returns its payment.
    function _onlyResult(Vm.Log[] memory logs, uint256 id, address target, LifelineVault.Outcome outcome)
        internal
        view
        returns (uint256 paid)
    {
        assertEq(_vaultLogs(logs, LifelineVault.Result.selector), 1, "exactly one Result");
        return _assertResult(logs, 0, id, target, outcome);
    }

    /// @dev The sum of `SelfCare` of the vault.
    function _selfCareSum(Vm.Log[] memory logs) internal view returns (uint256 sum) {
        uint256 count = _vaultLogs(logs, LifelineVault.SelfCare.selector);
        for (uint256 i = 0; i < count; ++i) {
            sum += abi.decode(_findLog(logs, address(vault), LifelineVault.SelfCare.selector, i).data, (uint256));
        }
    }

    /// @dev The callback ran: the delivery did not revert and the proxy emitted no `CallbackFailure`.
    function _assertRan(Delivery memory d) internal pure {
        assertTrue(d.delivered, "the delivery did not revert");
        assertFalse(d.callbackFailed, "no CallbackFailure");
    }

    /// @dev A skip of an authenticated `reconcile`: the callback ran, the one `Result` has paid 0, and the
    ///      balance changed only by self-care.
    function _assertSkip(
        Delivery memory d,
        uint256 balanceBefore,
        uint256 id,
        address target,
        LifelineVault.Outcome outcome
    ) internal view {
        _assertRan(d);
        assertEq(_onlyResult(d.logs, id, target, outcome), 0);
        assertEq(address(vault).balance, balanceBefore - _selfCareSum(d.logs), "only self-care moved principal");
    }

    /// @dev An authenticated callback that pays nothing to a target. Returns its self-care and its fee.
    function _idleCallback() internal returns (uint256 selfCare, uint256 fee) {
        Delivery memory d = _reconcile(address(0xdead), 1);
        _assertRan(d);
        _onlyResult(d.logs, 1, address(0xdead), LifelineVault.Outcome.NoPolicy);
        return (_selfCareSum(d.logs), d.fee);
    }

    /// @dev Puts the vault's reserves back to `selfReserveTarget` from outside: the manual `depositTo(vault)`.
    ///      The next authenticated callback then has no self-care.
    function _refillFloat() internal {
        uint256 reserves = proxy.reserves(address(vault));
        uint256 target = vault.selfReserveTarget();
        if (reserves < target) {
            vm.prank(stranger);
            proxy.depositTo{ value: target - reserves }(address(vault));
        }
    }

    /// @dev The seconds from now to the start of the next calendar day (UTC).
    function _secondsToNextDay() internal view returns (uint256) {
        uint256 nowTime = vm.getBlockTimestamp();
        return (nowTime / 1 days + 1) * 1 days - nowTime;
    }

    /// @dev Moves to the start of the next day if the day ends in less than three hours.
    function _keepClearOfMidnight() internal {
        uint256 left = _secondsToNextDay();
        if (left < 3 hours) _skipTime(left);
    }

    // ------------------------------------------------------------------------------------------
    // Harness and fixture
    // ------------------------------------------------------------------------------------------

    function test_harness_deliversOnlyFromAnAuthorizedAccount() public {
        address canary = _deployCanary();
        bytes memory config = abi.encode(
            ICallbackProxy.CallbackConfiguration_V_1_0({
                chainId: BASE_SEPOLIA_CHAIN_ID,
                recipient: canary,
                gasLimit: DELIVERY_GAS,
                payload: abi.encodeCall(CanaryCallback.pong, (stranger, 1))
            })
        );

        assertFalse(proxy._operators(stranger));
        vm.expectRevert(ICallbackProxy.NotAuthorized.selector);
        vm.prank(stranger, stranger);
        proxy.deliverCallback(ICallbackProxy.CallbackVersion.V_1_0, config);
    }

    function test_harness_overwritesTheFirstArgument() public view {
        bytes memory payload = _reconcilePayload(address(0xBEEF), 7);
        assertEq(_withFirstArgument(payload, hub), abi.encodeCall(LifelineVault.reconcile, (hub, address(0xBEEF), 7)));
        assertEq(payload, _reconcilePayload(address(0xBEEF), 7), "the payload of the caller did not change");
    }

    /// @dev The proxy does not call an address without code, and it keeps `_extraGas + _maxChargeGas` of
    ///      the gas for the charge.
    function test_harness_theProxyRejectsADeliveryThatCannotRun() public {
        address noCode = _freshAccount("no-code");
        Delivery memory d = _pong(noCode, 1);
        assertFalse(d.delivered);
        assertEq(d.revertData, abi.encodeWithSelector(ICallbackProxy.NotAContract.selector, noCode));
        assertEq(d.fee, 0);

        address canary = _deployCanary();
        d = _deliver(canary, abi.encodeCall(CanaryCallback.pong, (address(0), 1)), 150_000, hub);
        assertFalse(d.delivered);
        assertEq(bytes4(d.revertData), ICallbackProxy.InsufficientGas.selector);
        assertEq(d.fee, 0);
        assertEq(proxy.debt(canary), 0);
    }

    /// @dev An unfunded canary goes into debt on its first callback. The callback itself runs.
    function test_canary_theFirstCallbackBecomesADebt() public {
        address canary = _deployCanary();
        assertEq(canary.balance, 0);

        Delivery memory d = _pong(canary, 42);
        _assertPongRan(d, canary, 42);
        assertGt(d.fee, 0);
        assertEq(proxy.debt(canary), d.fee, "the debt is the fee");
        assertTrue(_wentIntoDebt(d.logs, canary), "BlacklistContract(canary)");
        Vm.Log memory failure = _onlyLog(d.logs, PROXY_ADDRESS, IProxyLedger.PaymentFailure.selector);
        assertEq(address(uint160(uint256(failure.topics[1]))), canary, "the canary has no pay()");

        // The canary cannot hold a balance.
        vm.prank(stranger);
        (bool ok,) = canary.call{ value: 1 }("");
        assertFalse(ok);
    }

    // ------------------------------------------------------------------------------------------
    // Construction and funds
    // ------------------------------------------------------------------------------------------

    function test_constructor_depositsTheFloatAndKeepsThePrincipal() public {
        _takeLogs();
        LifelineVault fresh = _deployVault(3e15, 2e14, 3e14, 7e14, 1e14);
        Vm.Log[] memory logs = _takeLogs();

        assertEq(fresh.owner(), owner);
        assertEq(fresh.proxy(), PROXY_ADDRESS);
        assertEq(fresh.callbackSender(), hub);
        assertEq(address(fresh).balance, 3e15 - 2e14, "principal");
        assertEq(proxy.reserves(address(fresh)), 2e14, "fee float");
        assertEq(proxy.debt(address(fresh)), 0);
        assertEq(fresh.selfReserveTarget(), 3e14);
        assertEq(fresh.selfCareDailyCap(), 7e14);
        assertEq(fresh.principalFloor(), 1e14);
        assertFalse(fresh.paused());

        Vm.Log memory selfCare = _onlyLog(logs, address(fresh), LifelineVault.SelfCare.selector);
        assertEq(abi.decode(selfCare.data, (uint256)), 2e14);
    }

    function test_constructor_revertsIfTheValueIsBelowTheFloat() public {
        vm.expectRevert(abi.encodeWithSelector(LifelineVault.InsufficientValue.selector, 1e14, 2e14));
        vm.prank(owner);
        new LifelineVault{ value: 1e14 }(PROXY_ADDRESS, hub, 2e14, 0, 0, 0);
    }

    function test_receive_acceptsPrincipal_withdraw_sendsItToTheOwner() public {
        vm.prank(stranger);
        (bool ok,) = address(vault).call{ value: 1e15 }("");
        assertTrue(ok);
        assertEq(address(vault).balance, PRINCIPAL + 1e15);
        assertEq(proxy.reserves(address(vault)), FEE_FLOAT, "principal does not go to the float");

        uint256 ownerBefore = owner.balance;
        vm.prank(owner);
        vault.withdraw(2e15);
        assertEq(owner.balance, ownerBefore + 2e15);
        assertEq(address(vault).balance, PRINCIPAL - 1e15);
        assertEq(proxy.reserves(address(vault)), FEE_FLOAT, "a withdrawal does not touch the float");

        vm.expectRevert(LifelineVault.WithdrawFailed.selector);
        vm.prank(owner);
        vault.withdraw(PRINCIPAL);
    }

    function test_ownerFunctions_rejectAnotherCaller() public {
        address canary = _deployCanary();
        bytes memory notOwner = abi.encodeWithSelector(LifelineVault.NotOwner.selector);

        address[3] memory callers = [stranger, hub, PROXY_ADDRESS];
        for (uint256 i = 0; i < callers.length; ++i) {
            vm.startPrank(callers[i]);
            vm.expectRevert(notOwner);
            vault.setPolicy(canary, _policy());
            vm.expectRevert(notOwner);
            vault.release(canary);
            vm.expectRevert(notOwner);
            vault.disarm(canary);
            vm.expectRevert(notOwner);
            vault.pause();
            vm.expectRevert(notOwner);
            vault.unpause();
            vm.expectRevert(notOwner);
            vault.withdraw(1);
            vm.expectRevert(notOwner);
            vault.setSelfCare(1, 1);
            vm.expectRevert(notOwner);
            vault.setPrincipalFloor(1);
            vm.stopPrank();
        }
    }

    function test_ownerSetters_storeTheValues() public {
        vm.startPrank(owner);
        vault.setSelfCare(11e12, 22e12);
        vault.setPrincipalFloor(33e12);
        vault.pause();
        vm.stopPrank();
        assertEq(vault.selfReserveTarget(), 11e12);
        assertEq(vault.selfCareDailyCap(), 22e12);
        assertEq(vault.principalFloor(), 33e12);
        assertTrue(vault.paused());

        vm.prank(owner);
        vault.unpause();
        assertFalse(vault.paused());
    }

    // ------------------------------------------------------------------------------------------
    // Interface
    // ------------------------------------------------------------------------------------------

    /// @dev The ABI has exactly the functions of the fixed interface, no `pay` and no fallback.
    function test_abi_hasNoPay() public view {
        string memory json = vm.readFile("out/LifelineVault.sol/LifelineVault.json");

        string[20] memory expected = [
            "setPolicy(address,(uint256,uint256,uint256,uint256,uint32,uint32))",
            "release(address)",
            "disarm(address)",
            "pause()",
            "unpause()",
            "withdraw(uint256)",
            "setSelfCare(uint256,uint256)",
            "setPrincipalFloor(uint256)",
            "reconcile(address,address,uint256)",
            "sweep(address,address[],uint256)",
            "owner()",
            "proxy()",
            "callbackSender()",
            "paused()",
            "selfReserveTarget()",
            "selfCareDailyCap()",
            "principalFloor()",
            "hasPolicy(address)",
            "policyOf(address)",
            "quarantined(address)"
        ];
        for (uint256 i = 0; i < expected.length; ++i) {
            assertTrue(
                vm.keyExistsJson(json, string.concat(".methodIdentifiers['", expected[i], "']")),
                string.concat("missing in the ABI: ", expected[i])
            );
        }
        assertEq(vm.parseJsonKeys(json, ".methodIdentifiers").length, expected.length, "a function was added");

        assertFalse(vm.keyExistsJson(json, ".abi[?(@.type == 'fallback')]"), "the ABI has a fallback");
        assertFalse(vm.keyExistsJson(json, ".abi[?(@.name == 'pay')]"), "the ABI has pay");
        assertTrue(vm.keyExistsJson(json, ".abi[?(@.type == 'receive')]"), "the ABI was read");
    }

    /// @dev Off-chain units decode `Outcome` by position.
    function test_outcome_hasTheFixedOrder() public pure {
        assertEq(uint256(LifelineVault.Outcome.Rescued), 0);
        assertEq(uint256(LifelineVault.Outcome.ToppedUp), 1);
        assertEq(uint256(LifelineVault.Outcome.NoNeed), 2);
        assertEq(uint256(LifelineVault.Outcome.Paused), 3);
        assertEq(uint256(LifelineVault.Outcome.NoPolicy), 4);
        assertEq(uint256(LifelineVault.Outcome.Quarantined), 5);
        assertEq(uint256(LifelineVault.Outcome.NoCode), 6);
        assertEq(uint256(LifelineVault.Outcome.DebtAboveCap), 7);
        assertEq(uint256(LifelineVault.Outcome.DailyCap), 8);
        assertEq(uint256(LifelineVault.Outcome.Floor), 9);
        assertEq(uint256(LifelineVault.Outcome.Failed), 10);
        assertEq(uint256(type(LifelineVault.Outcome).max), 10);

        assertEq(LifelineVault.Result.selector, keccak256("Result(uint256,address,uint8,uint256)"));
        assertEq(VAULT_ITER_RESERVE, 60_000);
        assertEq(VAULT_TAIL_RESERVE, 60_000);
    }

    /// @dev The call that the proxy makes when reserves do not cover a fee. The vault does not answer it.
    function test_pay_isNotCallable() public {
        uint256 balanceBefore = address(vault).balance;

        vm.prank(PROXY_ADDRESS);
        (bool ok,) = address(vault).call{ gas: 50_000 }(abi.encodeWithSignature("pay(uint256)", 1 ether));
        assertFalse(ok, "pay(uint256) reverts");

        vm.prank(stranger);
        (ok,) = address(vault).call{ value: 1 }(abi.encodeWithSignature("pay(uint256)", 1 ether));
        assertFalse(ok, "there is no payable fallback");

        assertEq(address(vault).balance, balanceBefore);
    }

    // ------------------------------------------------------------------------------------------
    // Authentication
    // ------------------------------------------------------------------------------------------

    /// @dev A direct call that is not from the proxy reverts, also with the correct first argument.
    function test_directCall_revertsNotProxy() public {
        (address canary,) = _armedCanaryInDebt(_policy());
        uint256 balanceBefore = address(vault).balance;

        address[4] memory callers = [stranger, owner, hub, DELIVERY_ACCOUNT];
        for (uint256 i = 0; i < callers.length; ++i) {
            vm.startPrank(callers[i]);
            vm.expectRevert(LifelineVault.NotProxy.selector);
            vault.reconcile(hub, canary, 1);
            vm.expectRevert(LifelineVault.NotProxy.selector);
            vault.sweep(hub, _list(canary), 1);
            vm.stopPrank();
        }

        assertEq(address(vault).balance, balanceBefore);
        assertGt(proxy.debt(canary), 0, "no payment");
    }

    /// @dev The second local check: the proxy calls, but the first argument is not `callbackSender`.
    function test_callback_revertsNotCallbackSender() public {
        address canary = _deployCanary();
        address[3] memory senders = [stranger, owner, address(0)];
        for (uint256 i = 0; i < senders.length; ++i) {
            bytes memory notSender = abi.encodeWithSelector(LifelineVault.NotCallbackSender.selector, senders[i]);
            vm.startPrank(PROXY_ADDRESS);
            vm.expectRevert(notSender);
            vault.reconcile(senders[i], canary, 1);
            vm.expectRevert(notSender);
            vault.sweep(senders[i], _list(canary), 1);
            vm.stopPrank();
        }
    }

    // ------------------------------------------------------------------------------------------
    // setPolicy
    // ------------------------------------------------------------------------------------------

    /// @dev Completion criterion: `setPolicy` clears the debt of a callback canary.
    function test_setPolicy_healsACanaryInDebt() public {
        address canary = _deployCanary();
        uint256 debt = _pongIntoDebt(canary);

        // The proxy does not deliver to a contract in debt.
        Delivery memory dead = _pong(canary, 2);
        assertFalse(dead.delivered);
        assertEq(dead.revertData, abi.encodeWithSelector(ICallbackProxy.InDebt.selector, canary, debt));

        uint256 balanceBefore = address(vault).balance;
        Policy memory policy = _policy();
        policy.low = LOW;
        Vm.Log[] memory logs = _setPolicy(canary, policy);

        _assertOnlyVaultLog(logs, LifelineVault.PolicySet.selector, canary);
        uint256 paid = _onlyResult(logs, 0, canary, LifelineVault.Outcome.Rescued);
        assertEq(paid, debt + HIGH, "debt + (high - reserves)");
        assertTrue(_debtCleared(logs, canary), "WhitelistContract(canary)");
        assertEq(proxy.debt(canary), 0);
        assertEq(proxy.reserves(canary), HIGH);
        assertEq(address(vault).balance, balanceBefore - paid);

        assertTrue(vault.hasPolicy(canary));
        assertEq(abi.encode(vault.policyOf(canary)), abi.encode(policy));
        assertFalse(vault.quarantined(canary));

        // No callback and no self-care.
        assertEq(_vaultLogs(logs, LifelineVault.SelfCare.selector), 0);
        assertEq(proxy.reserves(address(vault)), FEE_FLOAT);

        Delivery memory alive = _pong(canary, 3);
        _assertPongRan(alive, canary, 3);
        assertEq(proxy.reserves(canary), HIGH - alive.fee, "the fee came from the reserves");
        assertEq(proxy.debt(canary), 0);
    }

    function test_setPolicy_topsUpATargetBelowLow() public {
        address canary = _deployCanary();
        _idleCallback(); // the float is below its target now
        uint256 floatBefore = proxy.reserves(address(vault));
        uint256 balanceBefore = address(vault).balance;
        Policy memory policy = _policy();
        policy.low = LOW;

        Vm.Log[] memory logs = _setPolicy(canary, policy);
        assertEq(_onlyResult(logs, 0, canary, LifelineVault.Outcome.ToppedUp), HIGH);
        assertFalse(_debtCleared(logs, canary));
        assertEq(proxy.reserves(canary), HIGH);
        assertEq(address(vault).balance, balanceBefore - HIGH);
        assertEq(_vaultLogs(logs, LifelineVault.SelfCare.selector), 0, "setPolicy runs no self-care");
        assertEq(proxy.reserves(address(vault)), floatBefore);

        // The same policy again: the target has no need.
        _onlyResult(_setPolicy(canary, policy), 0, canary, LifelineVault.Outcome.NoNeed);
        assertEq(address(vault).balance, balanceBefore - HIGH);
    }

    function test_setPolicy_rejectsAnInvalidPolicy() public {
        address canary = _deployCanary();
        Policy memory policy = _policy();
        policy.maxRescues = MAX_RESCUES_LIMIT + 1;

        vm.expectRevert(InvalidPolicy.selector);
        vm.prank(owner);
        vault.setPolicy(canary, policy);

        policy = _policy();
        policy.low = HIGH + 1;
        vm.expectRevert(InvalidPolicy.selector);
        vm.prank(owner);
        vault.setPolicy(canary, policy);

        assertFalse(vault.hasPolicy(canary));
        assertEq(abi.encode(vault.policyOf(canary)), abi.encode(Policy(0, 0, 0, 0, 0, 0)));
    }

    /// @dev The vault is not a target: a policy for it would move principal into the float outside self-care.
    function test_setPolicy_rejectsTheVaultItself() public {
        vm.expectRevert(abi.encodeWithSelector(LifelineVault.InvalidTarget.selector, address(vault)));
        vm.prank(owner);
        vault.setPolicy(address(vault), _policy());
        assertFalse(vault.hasPolicy(address(vault)));
    }

    /// @dev `setPolicy` has the checks of each reconcile: a paused vault stores the policy and pays nothing.
    function test_setPolicy_onAPausedVaultStoresAndPaysNothing() public {
        address canary = _deployCanary();
        uint256 debt = _pongIntoDebt(canary);
        uint256 balanceBefore = address(vault).balance;

        vm.prank(owner);
        vault.pause();
        _onlyResult(_setPolicy(canary, _policy()), 0, canary, LifelineVault.Outcome.Paused);
        assertTrue(vault.hasPolicy(canary));
        assertEq(proxy.debt(canary), debt);
        assertEq(address(vault).balance, balanceBefore);
    }

    // ------------------------------------------------------------------------------------------
    // Authenticated reconcile
    // ------------------------------------------------------------------------------------------

    /// @dev Completion criterion: a delivery whose first argument equals `callbackSender` emits `Result`
    ///      with a payment, `debt` is 0, and the next callback to the canary runs.
    function test_reconcile_rescuesACanaryInDebt() public {
        (address canary, uint256 debt) = _armedCanaryInDebt(_policy());

        // An earlier authenticated callback: its fee leaves the float below the target.
        Delivery memory earlier = _reconcile(_freshAccount("unknown-target"), 1);
        _assertRan(earlier);
        assertEq(proxy.reserves(address(vault)), FEE_FLOAT - earlier.fee);

        uint256 balanceBefore = address(vault).balance;
        uint256 floatBefore = proxy.reserves(address(vault));

        Delivery memory d = _reconcile(canary, 77);
        _assertRan(d);
        uint256 paid = _onlyResult(d.logs, 77, canary, LifelineVault.Outcome.Rescued);
        assertEq(paid, debt + HIGH, "debt + (high - reserves)");
        assertTrue(_debtCleared(d.logs, canary), "WhitelistContract(canary)");
        assertEq(proxy.debt(canary), 0);
        assertEq(proxy.reserves(canary), HIGH);

        // Principal moved only to the canary and to the float. The fee came from the float.
        uint256 selfCare = _selfCareSum(d.logs);
        assertEq(selfCare, earlier.fee, "self-care put the earlier fee back");
        assertEq(address(vault).balance, balanceBefore - paid - selfCare);
        assertGt(d.fee, 0);
        assertEq(proxy.reserves(address(vault)), floatBefore + selfCare - d.fee);
        assertEq(proxy.debt(address(vault)), 0);

        _assertPongRan(_pong(canary, 2), canary, 2);
    }

    /// @dev The vault stores no request ID: a duplicate acts on the ledger state that it finds.
    function test_reconcile_aDuplicateCallbackIsHarmless() public {
        (address canary,) = _armedCanaryInDebt(_policy());

        _onlyResult(_reconcile(canary, 5).logs, 5, canary, LifelineVault.Outcome.Rescued);
        uint256 balanceBefore = address(vault).balance;

        Delivery memory duplicate = _reconcile(canary, 5);
        _assertSkip(duplicate, balanceBefore, 5, canary, LifelineVault.Outcome.NoNeed);
        assertEq(proxy.reserves(canary), HIGH);
    }

    /// @dev A late callback: the debt that started it is gone, and a later debt is there. The vault pays
    ///      the debt that it finds.
    function test_reconcile_aLateCallbackActsOnTheStateItFinds() public {
        Policy memory policy = _debtLoopPolicy(MAX_RESCUES_LIMIT, WINDOW);
        (address canary, uint256 firstDebt) = _armedCanaryInDebt(policy);

        // Another account clears the first debt. The canary then goes into debt again.
        vm.prank(stranger);
        proxy.depositTo{ value: firstDebt }(canary);
        uint256 secondDebt = _pongIntoDebt(canary);

        Delivery memory late = _reconcile(canary, 1);
        assertEq(_onlyResult(late.logs, 1, canary, LifelineVault.Outcome.Rescued), secondDebt + HIGH_BELOW_ONE_FEE);
        assertEq(proxy.debt(canary), 0);
    }

    // ------------------------------------------------------------------------------------------
    // Skip outcomes
    // ------------------------------------------------------------------------------------------

    function test_skip_paused() public {
        (address canary, uint256 debt) = _armedCanaryInDebt(_policy());
        vm.prank(owner);
        vault.pause();
        uint256 balanceBefore = address(vault).balance;

        _assertSkip(_reconcile(canary, 11), balanceBefore, 11, canary, LifelineVault.Outcome.Paused);
        assertEq(proxy.debt(canary), debt);

        // A paused sweep gives `Paused` for each target, also for a target without a policy.
        address other = _deployCanary();
        Delivery memory d = _sweep(_list(canary, other), 12, DELIVERY_GAS);
        _assertRan(d);
        assertEq(_vaultLogs(d.logs, LifelineVault.Result.selector), 2);
        _assertResult(d.logs, 0, 12, canary, LifelineVault.Outcome.Paused);
        _assertResult(d.logs, 1, 12, other, LifelineVault.Outcome.Paused);
        assertEq(proxy.debt(canary), debt);

        vm.prank(owner);
        vault.unpause();
        _onlyResult(_reconcile(canary, 13).logs, 13, canary, LifelineVault.Outcome.Rescued);
    }

    function test_skip_noPolicy() public {
        address canary = _deployCanary();
        uint256 debt = _pongIntoDebt(canary);
        uint256 balanceBefore = address(vault).balance;

        _assertSkip(_reconcile(canary, 21), balanceBefore, 21, canary, LifelineVault.Outcome.NoPolicy);
        assertEq(proxy.debt(canary), debt);
    }

    function test_skip_quarantined() public {
        (address canary,) = _armedCanaryInDebt(_debtLoopPolicy(1, WINDOW));
        _onlyResult(_reconcile(canary, 1).logs, 1, canary, LifelineVault.Outcome.Rescued);
        uint256 debt = _pongIntoDebt(canary);
        uint256 balanceBefore = address(vault).balance;

        // The second debt inside the window: the vault quarantines the target and does not pay.
        Delivery memory d = _reconcile(canary, 31);
        _assertSkip(d, balanceBefore, 31, canary, LifelineVault.Outcome.Quarantined);
        _assertOnlyVaultLog(d.logs, LifelineVault.Quarantined.selector, canary);
        assertTrue(vault.quarantined(canary));

        // From then on: the same `Result`, and no second `Quarantined` event.
        balanceBefore = address(vault).balance;
        d = _reconcile(canary, 32);
        _assertSkip(d, balanceBefore, 32, canary, LifelineVault.Outcome.Quarantined);
        assertEq(_vaultLogs(d.logs, LifelineVault.Quarantined.selector), 0);
        assertEq(proxy.debt(canary), debt);
    }

    function test_skip_noCode() public {
        address noCode = _freshAccount("target-without-code");
        _onlyResult(_setPolicy(noCode, _policy()), 0, noCode, LifelineVault.Outcome.NoCode);
        assertTrue(vault.hasPolicy(noCode));

        // `low` above 0: a contract with these reserves would get a top-up.
        Policy memory policy = _policy();
        policy.low = LOW;
        _onlyResult(_setPolicy(noCode, policy), 0, noCode, LifelineVault.Outcome.NoCode);

        uint256 balanceBefore = address(vault).balance;
        _assertSkip(_reconcile(noCode, 41), balanceBefore, 41, noCode, LifelineVault.Outcome.NoCode);
        assertEq(proxy.reserves(noCode), 0);
    }

    function test_skip_noNeed() public {
        address canary = _deployCanary();
        Policy memory policy = _policy();
        policy.low = LOW;
        _onlyResult(_setPolicy(canary, policy), 0, canary, LifelineVault.Outcome.ToppedUp);
        uint256 balanceBefore = address(vault).balance;

        _assertSkip(_reconcile(canary, 51), balanceBefore, 51, canary, LifelineVault.Outcome.NoNeed);
        assertEq(proxy.reserves(canary), HIGH);

        // Reserves equal to `low` are no need.
        policy.low = HIGH;
        _onlyResult(_setPolicy(canary, policy), 0, canary, LifelineVault.Outcome.NoNeed);
    }

    function test_skip_debtAboveCap() public {
        (address canary, uint256 debt) = _armedCanaryInDebt(_policy());
        Policy memory policy = _policy();
        policy.maxDebt = debt - 1;
        _onlyResult(_setPolicy(canary, policy), 0, canary, LifelineVault.Outcome.DebtAboveCap);
        uint256 balanceBefore = address(vault).balance;

        _assertSkip(_reconcile(canary, 61), balanceBefore, 61, canary, LifelineVault.Outcome.DebtAboveCap);
        assertEq(proxy.debt(canary), debt, "never a partial payment");
        assertEq(proxy.reserves(canary), 0);

        // A debt equal to `maxDebt` is paid.
        policy.maxDebt = debt;
        assertEq(_onlyResult(_setPolicy(canary, policy), 0, canary, LifelineVault.Outcome.Rescued), debt + HIGH);
    }

    function test_skip_dailyCap() public {
        (address canary, uint256 debt) = _armedCanaryInDebt(_policy());
        Policy memory policy = _policy();
        policy.dailyCap = debt + HIGH - 1;
        _onlyResult(_setPolicy(canary, policy), 0, canary, LifelineVault.Outcome.DailyCap);
        uint256 balanceBefore = address(vault).balance;

        _assertSkip(_reconcile(canary, 71), balanceBefore, 71, canary, LifelineVault.Outcome.DailyCap);
        assertEq(proxy.debt(canary), debt, "never a partial payment");
        assertEq(proxy.reserves(canary), 0);

        // An amount equal to the cap is paid.
        policy.dailyCap = debt + HIGH;
        assertEq(_onlyResult(_setPolicy(canary, policy), 0, canary, LifelineVault.Outcome.Rescued), debt + HIGH);
    }

    function test_skip_floor() public {
        (address canary, uint256 debt) = _armedCanaryInDebt(_policy());
        uint256 amount = debt + HIGH;
        _refillFloat();
        uint256 balanceBefore = address(vault).balance;

        // The payment leaves one wei less than the floor.
        vm.prank(owner);
        vault.setPrincipalFloor(balanceBefore - amount + 1);
        Delivery memory d = _reconcile(canary, 81);
        _assertSkip(d, balanceBefore, 81, canary, LifelineVault.Outcome.Floor);
        assertEq(_vaultLogs(d.logs, LifelineVault.VaultLow.selector), 0, "the balance is not below the floor");
        assertEq(proxy.debt(canary), debt);
        _refillFloat();

        // The payment leaves exactly the floor.
        vm.prank(owner);
        vault.setPrincipalFloor(balanceBefore - amount);
        assertEq(_onlyResult(_reconcile(canary, 82).logs, 82, canary, LifelineVault.Outcome.Rescued), amount);
        assertEq(address(vault).balance, balanceBefore - amount);

        // A balance equal to the floor is not low: no `VaultLow`. No principal is above the floor: no self-care.
        d = _reconcile(canary, 83);
        _assertSkip(d, balanceBefore - amount, 83, canary, LifelineVault.Outcome.NoNeed);
        assertEq(_vaultLogs(d.logs, LifelineVault.VaultLow.selector), 0);
        assertEq(_selfCareSum(d.logs), 0);
    }

    /// @dev A payment above the whole balance is a `Floor` skip, with a floor of 0 also.
    function test_skip_floor_paymentAboveTheBalance() public {
        (address canary, uint256 debt) = _armedCanaryInDebt(_policy());
        _refillFloat();
        vm.startPrank(owner);
        vault.setPrincipalFloor(0);
        vault.withdraw(address(vault).balance - (debt + HIGH - 1));
        vm.stopPrank();

        _assertSkip(_reconcile(canary, 83), debt + HIGH - 1, 83, canary, LifelineVault.Outcome.Floor);
        assertEq(proxy.debt(canary), debt);
    }

    /// @dev The largest policy values do not overflow: the cap of the day has no effect, and the payment
    ///      is `debt + (high - reserves)`.
    function test_reconcile_theLargestPolicyValuesDoNotOverflow() public {
        Policy memory policy = Policy({
            low: 0,
            high: HIGH,
            maxDebt: type(uint256).max,
            dailyCap: type(uint256).max,
            maxRescues: MAX_RESCUES_LIMIT,
            window: type(uint32).max
        });
        (address canary, uint256 debt) = _armedCanaryInDebt(policy);
        assertEq(_onlyResult(_reconcile(canary, 1).logs, 1, canary, LifelineVault.Outcome.Rescued), debt + HIGH);

        // A refill above each possible payment: a skip, no overflow.
        policy.high = type(uint256).max;
        policy.low = type(uint256).max;
        uint256 balanceBefore = address(vault).balance;
        _onlyResult(_setPolicy(canary, policy), 0, canary, LifelineVault.Outcome.DailyCap);
        _assertSkip(_reconcile(canary, 2), balanceBefore, 2, canary, LifelineVault.Outcome.DailyCap);
    }

    /// @dev A balance below the floor: `VaultLow`, no payment and no self-care.
    function test_vaultLow_isEmittedBelowTheFloor() public {
        (address canary, uint256 debt) = _armedCanaryInDebt(_policy());
        uint256 balanceBefore = address(vault).balance;
        vm.prank(owner);
        vault.setPrincipalFloor(balanceBefore + 1);

        Delivery memory d = _reconcile(canary, 91);
        _assertRan(d);
        _onlyResult(d.logs, 91, canary, LifelineVault.Outcome.Floor);
        Vm.Log memory low = _onlyLog(d.logs, address(vault), LifelineVault.VaultLow.selector);
        (uint256 balance, uint256 floor) = abi.decode(low.data, (uint256, uint256));
        assertEq(balance, balanceBefore);
        assertEq(floor, balanceBefore + 1);
        assertEq(_vaultLogs(d.logs, LifelineVault.SelfCare.selector), 0);
        assertEq(address(vault).balance, balanceBefore);
        assertEq(proxy.debt(canary), debt);

        // The next callback finds the float below the target and the balance below the floor: no self-care.
        d = _sweep(_list(canary), 92, DELIVERY_GAS);
        _assertRan(d);
        _onlyLog(d.logs, address(vault), LifelineVault.VaultLow.selector);
        assertEq(_vaultLogs(d.logs, LifelineVault.SelfCare.selector), 0);
        assertEq(address(vault).balance, balanceBefore);
    }

    /// @dev `dailyCap` is a cap of the calendar day: the next day has the whole cap again.
    function test_dailyCap_isACapOfTheCalendarDay() public {
        Policy memory policy = _debtLoopPolicy(MAX_RESCUES_LIMIT, 1);
        (address canary, uint256 debt) = _armedCanaryInDebt(policy);

        // The cap holds exactly the first rescue.
        policy.dailyCap = debt + HIGH_BELOW_ONE_FEE;
        _onlyResult(_setPolicy(canary, policy), 0, canary, LifelineVault.Outcome.Rescued);

        uint256 secondDebt = _pongIntoDebt(canary);
        assertLe(secondDebt + HIGH_BELOW_ONE_FEE, policy.dailyCap, "the second rescue alone is inside the cap");
        _onlyResult(_reconcile(canary, 1).logs, 1, canary, LifelineVault.Outcome.DailyCap);

        // The reconcile runs one block before the end of the day: still the same day.
        _skipTime(_secondsToNextDay() - 4);
        _onlyResult(_reconcile(canary, 2).logs, 2, canary, LifelineVault.Outcome.DailyCap);

        _skipTime(_secondsToNextDay());
        _onlyResult(_reconcile(canary, 3).logs, 3, canary, LifelineVault.Outcome.Rescued);
        assertEq(proxy.debt(canary), 0);
    }

    // ------------------------------------------------------------------------------------------
    // Quarantine
    // ------------------------------------------------------------------------------------------

    /// @dev Rescues a debt-loop canary `rounds` times with a sweep. Each round is a new debt.
    function _rescueRounds(address canary, uint256 rounds) internal {
        for (uint256 i = 0; i < rounds; ++i) {
            if (proxy.debt(canary) == 0) _pongIntoDebt(canary);
            Delivery memory d = _sweep(_list(canary), 100 + i, DELIVERY_GAS);
            _assertRan(d);
            _onlyResult(d.logs, 100 + i, canary, LifelineVault.Outcome.Rescued);
        }
    }

    /// @dev Spec §4.9: a target quarantined on the vault gets `Result(Quarantined, 0)` in every sweep and no
    ///      payment.
    function test_vaultQuarantine_sweepSkips() public {
        (address broken,) = _armedCanaryInDebt(_debtLoopPolicy(2, WINDOW));
        // The window of the other target is one second: its rescues never add up.
        (address healthy,) = _armedCanaryInDebt(_debtLoopPolicy(1, 1));

        // `maxRescues` rescues inside `window`.
        _rescueRounds(broken, 2);
        assertFalse(vault.quarantined(broken));

        // The next debt: quarantine, no payment. The other target of the same sweep is paid.
        uint256 debt = _pongIntoDebt(broken);
        uint256 balanceBefore = address(vault).balance;
        Delivery memory d = _sweep(_list(broken, healthy), 200, DELIVERY_GAS);
        _assertRan(d);
        assertEq(_vaultLogs(d.logs, LifelineVault.Result.selector), 2);
        _assertResult(d.logs, 0, 200, broken, LifelineVault.Outcome.Quarantined);
        uint256 paid = _assertResult(d.logs, 1, 200, healthy, LifelineVault.Outcome.Rescued);
        _assertOnlyVaultLog(d.logs, LifelineVault.Quarantined.selector, broken);
        assertTrue(vault.quarantined(broken));
        assertFalse(vault.quarantined(healthy));
        assertEq(proxy.debt(broken), debt);
        assertEq(address(vault).balance, balanceBefore - paid - _selfCareSum(d.logs));

        // Each later sweep, also after the window: the same `Result`, no payment, the other target is paid.
        for (uint256 i = 1; i <= 3; ++i) {
            if (i == 3) _skipTime(2 * WINDOW);
            _pongIntoDebt(healthy);
            balanceBefore = address(vault).balance;

            d = _sweep(_list(broken, healthy), 200 + i, DELIVERY_GAS);
            _assertRan(d);
            assertEq(_vaultLogs(d.logs, LifelineVault.Result.selector), 2);
            _assertResult(d.logs, 0, 200 + i, broken, LifelineVault.Outcome.Quarantined);
            paid = _assertResult(d.logs, 1, 200 + i, healthy, LifelineVault.Outcome.Rescued);
            assertEq(_vaultLogs(d.logs, LifelineVault.Quarantined.selector), 0, "the event is emitted once");
            assertEq(proxy.debt(broken), debt, "no payment to the quarantined target");
            assertEq(proxy.debt(healthy), 0);
            assertEq(address(vault).balance, balanceBefore - paid - _selfCareSum(d.logs));
        }

        // `setPolicy` does not clear a quarantine.
        _onlyResult(_setPolicy(broken, _policy()), 0, broken, LifelineVault.Outcome.Quarantined);
        assertTrue(vault.quarantined(broken));
        assertEq(proxy.debt(broken), debt);

        // `release` permits a rescue again.
        _takeLogs();
        vm.prank(owner);
        vault.release(broken);
        _assertOnlyVaultLog(_takeLogs(), LifelineVault.Released.selector, broken);
        assertFalse(vault.quarantined(broken));
        assertEq(proxy.debt(broken), debt, "release pays nothing");

        d = _sweep(_list(broken), 300, DELIVERY_GAS);
        _assertRan(d);
        assertEq(_onlyResult(d.logs, 300, broken, LifelineVault.Outcome.Rescued), debt + HIGH);
        assertEq(proxy.debt(broken), 0);
    }

    /// @dev A debt above zero with `maxRescues` rescues inside `window` is the quarantine, also when the
    ///      debt is above `maxDebt`.
    function test_quarantine_comesBeforeTheDebtCap() public {
        Policy memory policy = _debtLoopPolicy(1, WINDOW);
        (address canary,) = _armedCanaryInDebt(policy);
        _rescueRounds(canary, 1);
        uint256 debt = _pongIntoDebt(canary);

        policy.maxDebt = debt - 1;
        Vm.Log[] memory logs = _setPolicy(canary, policy);
        _onlyResult(logs, 0, canary, LifelineVault.Outcome.Quarantined);
        _onlyLog(logs, address(vault), LifelineVault.Quarantined.selector);
        assertTrue(vault.quarantined(canary));
    }

    /// @dev `release` clears the rescue history: the target has `maxRescues` rescues again.
    function test_release_clearsTheRescueHistory() public {
        (address canary,) = _armedCanaryInDebt(_debtLoopPolicy(2, WINDOW));
        _rescueRounds(canary, 2);
        _pongIntoDebt(canary);
        _onlyResult(_reconcile(canary, 1).logs, 1, canary, LifelineVault.Outcome.Quarantined);

        vm.prank(owner);
        vault.release(canary);
        _rescueRounds(canary, 2);
        _pongIntoDebt(canary);
        _onlyResult(_reconcile(canary, 2).logs, 2, canary, LifelineVault.Outcome.Quarantined);
    }

    /// @dev The quarantine check comes before any ledger read: the vault does not ask the proxy about a
    ///      quarantined target. A read that reverts would give `Failed`.
    function test_quarantine_isCheckedBeforeAnyLedgerRead() public {
        (address canary,) = _armedCanaryInDebt(_debtLoopPolicy(1, WINDOW));
        _rescueRounds(canary, 1);
        _pongIntoDebt(canary);
        _onlyResult(_reconcile(canary, 1).logs, 1, canary, LifelineVault.Outcome.Quarantined);

        vm.mockCallRevert(PROXY_ADDRESS, abi.encodeCall(IProxyLedger.debt, (canary)), "mocked");
        vm.mockCallRevert(PROXY_ADDRESS, abi.encodeCall(IProxyLedger.reserves, (canary)), "mocked");
        Delivery memory d = _sweep(_list(canary), 2, DELIVERY_GAS);
        vm.clearMockedCalls();

        _assertRan(d);
        _onlyResult(d.logs, 2, canary, LifelineVault.Outcome.Quarantined);
    }

    /// @dev The window slides: a rescue that is `window` seconds old does not count.
    function test_quarantine_theWindowSlides() public {
        (address canary,) = _armedCanaryInDebt(_debtLoopPolicy(2, WINDOW));

        _rescueRounds(canary, 1); // at T
        _skipTime(WINDOW / 2);
        _rescueRounds(canary, 1); // at T + 30 min
        _skipTime(WINDOW / 2 + 60);
        _rescueRounds(canary, 1); // at T + 61 min: only the second rescue is inside the window

        // Two rescues are inside the window now: the second and the third.
        _pongIntoDebt(canary);
        _onlyResult(_reconcile(canary, 1).logs, 1, canary, LifelineVault.Outcome.Quarantined);
    }

    /// @dev The edge of the window: a rescue exactly `window` seconds old is outside.
    function test_quarantine_theEdgeOfTheWindow() public {
        Policy memory policy = _debtLoopPolicy(1, WINDOW);
        (address canary,) = _armedCanaryInDebt(policy);
        _rescueRounds(canary, 1);
        uint256 rescuedAt = vm.getBlockTimestamp();
        _pongIntoDebt(canary);

        // The reconcile runs one block after this point: `window - 2` seconds after the rescue.
        _skipTime(rescuedAt + WINDOW - 2 * BLOCK_TIME - vm.getBlockTimestamp());
        uint256 snapshot = vm.snapshotState();
        _onlyResult(_reconcile(canary, 1).logs, 1, canary, LifelineVault.Outcome.Quarantined);
        assertEq(vm.getBlockTimestamp(), rescuedAt + WINDOW - BLOCK_TIME);

        // One block later: exactly `window` seconds after the rescue.
        assertTrue(vm.revertToState(snapshot));
        _skipTime(BLOCK_TIME);
        _onlyResult(_reconcile(canary, 2).logs, 2, canary, LifelineVault.Outcome.Rescued);
        assertEq(vm.getBlockTimestamp(), rescuedAt + WINDOW);
    }

    /// @dev Each permitted value of `maxRescues`: the debt after `maxRescues` rescues is the quarantine.
    function test_quarantine_eachRescueLimit() public {
        for (uint32 maxRescues = 1; maxRescues <= MAX_RESCUES_LIMIT; ++maxRescues) {
            (address canary,) = _armedCanaryInDebt(_debtLoopPolicy(maxRescues, WINDOW));
            _rescueRounds(canary, maxRescues);
            assertFalse(vault.quarantined(canary));
            _pongIntoDebt(canary);
            _onlyResult(_reconcile(canary, maxRescues).logs, maxRescues, canary, LifelineVault.Outcome.Quarantined);
        }
    }

    // ------------------------------------------------------------------------------------------
    // Top-up
    // ------------------------------------------------------------------------------------------

    /// @dev A top-up of a target with zero debt counts toward `dailyCap` only, not toward `maxRescues`.
    function test_topUp_isNotARescue() public {
        address canary = _deployCanary();
        // One callback fee takes the reserves below `low`. One rescue is permitted for each window.
        Policy memory policy = Policy({
            low: HIGH - 1, high: HIGH, maxDebt: MAX_DEBT, dailyCap: DAILY_CAP, maxRescues: 1, window: 2 days
        });
        assertEq(_onlyResult(_setPolicy(canary, policy), 0, canary, LifelineVault.Outcome.ToppedUp), HIGH);

        // A sweep tops up a target with zero debt and reserves below `low`.
        uint256 firstFee = _pong(canary, 1).fee;
        assertEq(proxy.debt(canary), 0);
        assertEq(proxy.reserves(canary), HIGH - firstFee);
        uint256 balanceBefore = address(vault).balance;
        Delivery memory d = _sweep(_list(canary), 1, DELIVERY_GAS);
        _assertRan(d);
        assertEq(_onlyResult(d.logs, 1, canary, LifelineVault.Outcome.ToppedUp), firstFee, "high - reserves");
        assertFalse(_debtCleared(d.logs, canary));
        assertEq(proxy.reserves(canary), HIGH);
        assertEq(address(vault).balance, balanceBefore - firstFee - _selfCareSum(d.logs));

        // The daily spend changed: it is `HIGH + firstFee`. A cap of one wei less than the spend plus the
        // next top-up is a skip; a cap of exactly that sum is a payment.
        uint256 secondFee = _pong(canary, 2).fee;
        policy.dailyCap = HIGH + firstFee + secondFee - 1;
        _onlyResult(_setPolicy(canary, policy), 0, canary, LifelineVault.Outcome.DailyCap);
        _onlyResult(_sweep(_list(canary), 2, DELIVERY_GAS).logs, 2, canary, LifelineVault.Outcome.DailyCap);
        policy.dailyCap = HIGH + firstFee + secondFee;
        assertEq(_onlyResult(_setPolicy(canary, policy), 0, canary, LifelineVault.Outcome.ToppedUp), secondFee);

        // The rescue count did not change: after three top-ups, the one rescue of the window is still
        // there. The next day has a new daily cap.
        _skipTime(_secondsToNextDay());
        uint256 debt = _drainIntoDebt(canary);
        assertEq(_onlyResult(_reconcile(canary, 3).logs, 3, canary, LifelineVault.Outcome.Rescued), debt + HIGH);

        // That rescue counts: the next debt in the window is the quarantine.
        _drainIntoDebt(canary);
        _onlyResult(_reconcile(canary, 4).logs, 4, canary, LifelineVault.Outcome.Quarantined);
    }

    // ------------------------------------------------------------------------------------------
    // Sweep
    // ------------------------------------------------------------------------------------------

    /// @dev One sweep with each kind of target: each gets its own `Result`, in the order of the list.
    function test_sweep_givesOneResultForEachTarget() public {
        (address inDebt, uint256 debt) = _armedCanaryInDebt(_policy());
        address belowLow = _deployCanary();
        Policy memory policy = _policy();
        policy.low = HIGH - 1;
        _setPolicy(belowLow, policy);
        uint256 fee = _pong(belowLow, 1).fee;
        address healthy = _deployCanary();
        _setPolicy(healthy, policy);
        address noPolicy = _deployCanary();
        address noCode = _freshAccount("sweep-no-code");
        _setPolicy(noCode, _policy());

        address[] memory targets = new address[](6);
        targets[0] = inDebt;
        targets[1] = belowLow;
        targets[2] = healthy;
        targets[3] = noPolicy;
        targets[4] = noCode;
        targets[5] = inDebt; // a duplicate in the list: the debt is gone when its turn comes

        uint256 balanceBefore = address(vault).balance;
        Delivery memory d = _sweep(targets, 9, DELIVERY_GAS);
        _assertRan(d);
        assertEq(_vaultLogs(d.logs, LifelineVault.Result.selector), 6);
        assertEq(_vaultLogs(d.logs, LifelineVault.SweepPartial.selector), 0);
        assertEq(_assertResult(d.logs, 0, 9, inDebt, LifelineVault.Outcome.Rescued), debt + HIGH);
        assertEq(_assertResult(d.logs, 1, 9, belowLow, LifelineVault.Outcome.ToppedUp), fee);
        _assertResult(d.logs, 2, 9, healthy, LifelineVault.Outcome.NoNeed);
        _assertResult(d.logs, 3, 9, noPolicy, LifelineVault.Outcome.NoPolicy);
        _assertResult(d.logs, 4, 9, noCode, LifelineVault.Outcome.NoCode);
        _assertResult(d.logs, 5, 9, inDebt, LifelineVault.Outcome.NoNeed);
        assertEq(address(vault).balance, balanceBefore - debt - HIGH - fee - _selfCareSum(d.logs));

        // An empty sweep runs and emits no `Result`.
        d = _sweep(new address[](0), 10, DELIVERY_GAS);
        _assertRan(d);
        assertEq(_vaultLogs(d.logs, LifelineVault.Result.selector), 0);
    }

    /// @dev Each target runs in its own `try`: a target whose proxy call reverts gets `Failed`, and the
    ///      others get their correct `Result`. The real ledger never reverts, so the test replaces one
    ///      answer of the proxy for each failing target.
    function test_sweep_aTargetThatFailsDoesNotStopTheOthers() public {
        (address[] memory targets, uint256[] memory debts) = _armedCanariesInDebt(5);

        vm.mockCallRevert(PROXY_ADDRESS, abi.encodeCall(IProxyLedger.debt, (targets[1])), "mocked");
        vm.mockCallRevert(PROXY_ADDRESS, abi.encodeCall(IProxyLedger.reserves, (targets[2])), "mocked");
        vm.mockCallRevert(PROXY_ADDRESS, abi.encodeCall(IProxyLedger.depositTo, (targets[3])), "mocked");
        uint256 balanceBefore = address(vault).balance;
        Delivery memory d = _sweep(targets, 5, DELIVERY_GAS);
        vm.clearMockedCalls();

        _assertRan(d);
        assertEq(_vaultLogs(d.logs, LifelineVault.Result.selector), 5);
        assertEq(_assertResult(d.logs, 0, 5, targets[0], LifelineVault.Outcome.Rescued), debts[0] + HIGH);
        _assertResult(d.logs, 1, 5, targets[1], LifelineVault.Outcome.Failed);
        _assertResult(d.logs, 2, 5, targets[2], LifelineVault.Outcome.Failed);
        _assertResult(d.logs, 3, 5, targets[3], LifelineVault.Outcome.Failed);
        assertEq(_assertResult(d.logs, 4, 5, targets[4], LifelineVault.Outcome.Rescued), debts[4] + HIGH);
        assertEq(address(vault).balance, balanceBefore - debts[0] - debts[4] - 2 * HIGH - _selfCareSum(d.logs));
        for (uint256 i = 1; i <= 3; ++i) {
            assertEq(proxy.debt(targets[i]), debts[i], "a failed target got no payment");
        }

        // A failure counts toward nothing: with the daily cap of exactly one rescue, each target is paid.
        for (uint256 i = 1; i <= 3; ++i) {
            Policy memory policy = _policy();
            policy.dailyCap = debts[i] + HIGH;
            policy.maxRescues = 1;
            _onlyResult(_setPolicy(targets[i], policy), 0, targets[i], LifelineVault.Outcome.Rescued);
        }
    }

    /// @dev `reconcile` has the same `try`.
    function test_reconcile_aProxyCallThatRevertsGivesFailed() public {
        (address canary, uint256 debt) = _armedCanaryInDebt(_policy());
        uint256 balanceBefore = address(vault).balance;

        vm.mockCallRevert(PROXY_ADDRESS, abi.encodeCall(IProxyLedger.depositTo, (canary)), "mocked");
        Delivery memory d = _reconcile(canary, 1);
        vm.clearMockedCalls();

        _assertSkip(d, balanceBefore, 1, canary, LifelineVault.Outcome.Failed);
        assertEq(proxy.debt(canary), debt);
    }

    // ------------------------------------------------------------------------------------------
    // Sweep guard
    // ------------------------------------------------------------------------------------------

    /// @dev Checks a sweep of canaries in debt that ran with some gas limit. The targets before
    ///      `SweepPartial` are rescued in the order of the list; the others are not touched.
    function _assertSweepRan(Delivery memory d, address[] memory targets, uint256 tickId)
        internal
        view
        returns (uint256 processed)
    {
        _assertRan(d);
        processed = _vaultLogs(d.logs, LifelineVault.Result.selector);
        if (processed == targets.length) {
            assertEq(_vaultLogs(d.logs, LifelineVault.SweepPartial.selector), 0, "a whole sweep is not partial");
        } else {
            Vm.Log memory partialLog = _onlyLog(d.logs, address(vault), LifelineVault.SweepPartial.selector);
            assertEq(uint256(partialLog.topics[1]), tickId, "SweepPartial tickId");
            assertEq(abi.decode(partialLog.data, (uint256)), processed, "SweepPartial processed");
        }
        for (uint256 i = 0; i < targets.length; ++i) {
            if (i < processed) {
                _assertResult(d.logs, i, tickId, targets[i], LifelineVault.Outcome.Rescued);
                assertEq(proxy.debt(targets[i]), 0);
            } else {
                assertGt(proxy.debt(targets[i]), 0, "a target after the stop is not touched");
            }
        }
    }

    /// @dev A sweep with a gas limit too low for all targets emits `SweepPartial` with the number
    ///      processed, does not run out of gas, and emits no `CallbackFailure`.
    function test_sweepGuard_stopsEarlyWithSweepPartial() public {
        (address[] memory targets,) = _armedCanariesInDebt(8);

        uint256 balanceBefore = address(vault).balance;
        Delivery memory d = _sweep(targets, 4, 500_000);
        uint256 processed = _assertSweepRan(d, targets, 4);
        assertGt(processed, 0, "some targets ran");
        assertLt(processed, targets.length, "not all targets ran");
        assertLt(d.gasUsed, 500_000, "the transaction did not use its whole gas limit");

        uint256 paid;
        for (uint256 i = 0; i < processed; ++i) {
            paid += _result(d.logs, i).paid;
        }
        assertEq(address(vault).balance, balanceBefore - paid - _selfCareSum(d.logs));

        // The next sweep, with enough gas, does the rest: the first targets have no need now.
        d = _sweep(targets, 5, 3_000_000);
        _assertRan(d);
        assertEq(_vaultLogs(d.logs, LifelineVault.SweepPartial.selector), 0);
        for (uint256 i = 0; i < targets.length; ++i) {
            _assertResult(
                d.logs, i, 5, targets[i], i < processed ? LifelineVault.Outcome.NoNeed : LifelineVault.Outcome.Rescued
            );
        }
    }

    /// @dev The smallest gas limit of the scan. Below about 230,000 gas a sweep cannot run: the proxy keeps
    ///      130,000 gas and uses gas itself, and the vault needs the gas of the end of the sweep.
    uint256 internal constant SWEEP_GAS_FLOOR = 250_000;

    /// @dev Each gas limit from the floor to the gas of the whole sweep: the sweep never runs out of gas.
    ///      This is the most expensive case that the real ledger has. Each target is a rescue that writes
    ///      new reserves, and the float is 0, so the self-care deposit at the end writes new reserves also.
    function test_sweepGuard_neverRunsOutOfGas() public {
        (address[] memory targets,) = _armedCanariesInDebt(6);
        _emptyTheFloat(targets[0]);

        uint256 snapshot = vm.snapshotState();
        Delivery memory whole = _sweep(targets, 6, 3_000_000);
        assertEq(_assertSweepRan(whole, targets, 6), targets.length);
        assertEq(_selfCareSum(whole.logs), FEE_FLOAT, "the end of the sweep fills the empty float");

        uint256 last;
        uint256 steps;
        for (uint256 gasLimit = SWEEP_GAS_FLOOR; gasLimit < whole.gasUsed + 150_000; gasLimit += 1777) {
            assertTrue(vm.revertToState(snapshot));
            Delivery memory d = _sweep(targets, 6, uint64(gasLimit));
            uint256 processed = _assertSweepRan(d, targets, 6);
            assertEq(_selfCareSum(d.logs), FEE_FLOAT, "the end of the sweep ran");
            assertGe(processed, last, "more gas does not process fewer targets");
            if (processed > last) ++steps;
            last = processed;
        }
        assertEq(last, targets.length, "the largest gas limit ran the whole sweep");
        assertEq(steps, targets.length, "each number of processed targets was seen");
    }

    /// @dev Junk callbacks take the float and leave a debt; another account pays exactly the debt. The
    ///      vault then has no debt and reserves of 0.
    function _emptyTheFloat(address target) internal {
        _junkIntoDebt(target);
        uint256 debt = proxy.debt(address(vault));
        vm.prank(stranger);
        proxy.depositTo{ value: debt }(address(vault));
        assertEq(proxy.debt(address(vault)), 0);
        assertEq(proxy.reserves(address(vault)), 0);
    }

    /// @dev The limit of the guard: a gas limit that does not cover the end of a sweep with no target. The
    ///      callback runs out of gas in the self-care deposit, the proxy emits `CallbackFailure`, and the
    ///      vault pays nothing. A gas limit below that is rejected by the proxy before the callback.
    function test_sweepGuard_aGasLimitBelowTheEndOfTheSweepFails() public {
        (address canary, uint256 debt) = _armedCanaryInDebt(_policy());
        _idleCallback(); // the float is below its target now: the end of the next callback has a deposit
        uint256 balanceBefore = address(vault).balance;

        Delivery memory d = _sweep(_list(canary), 1, 195_000);
        assertTrue(d.delivered);
        assertTrue(d.callbackFailed, "CallbackFailure");
        assertEq(_vaultLogs(d.logs, LifelineVault.Result.selector), 0);
        assertEq(address(vault).balance, balanceBefore);
        assertEq(proxy.debt(canary), debt);

        d = _sweep(_list(canary), 2, 160_000);
        assertFalse(d.delivered);
        assertEq(bytes4(d.revertData), ICallbackProxy.InsufficientGas.selector);
        assertEq(d.fee, 0);
    }

    // ------------------------------------------------------------------------------------------
    // Junk callbacks
    // ------------------------------------------------------------------------------------------

    /// @dev Three junk payloads: the real selectors with a first argument that the node set to another
    ///      requester, and an unknown selector. The sweep has a long list: a larger payload costs more.
    function _junkPayload(uint256 i, address target) internal pure returns (bytes memory) {
        if (i % 3 == 0) return _reconcilePayload(target, i);
        if (i % 3 == 1) {
            address[] memory targets = new address[](40);
            for (uint256 j = 0; j < targets.length; ++j) {
                targets[j] = target;
            }
            return _sweepPayload(targets, i);
        }
        return abi.encodeWithSignature("rescue(address,address,uint256)", address(0), target, i);
    }

    /// @dev Delivers one junk callback and checks it: `CallbackFailure`, no log of the vault, the vault
    ///      balance changes by 0, and the fee comes from the float.
    function _junk(uint256 i, address target) internal returns (Delivery memory d) {
        bytes memory payload = _junkPayload(i, target);
        uint256 balanceBefore = address(vault).balance;
        uint256 floatBefore = proxy.reserves(address(vault));

        d = _deliver(address(vault), payload, DELIVERY_GAS, stranger);

        assertTrue(d.delivered, "the delivery did not revert");
        Vm.Log memory failure = _onlyLog(d.logs, PROXY_ADDRESS, ICallbackProxy.CallbackFailure.selector);
        assertEq(address(uint160(uint256(failure.topics[1]))), address(vault), "CallbackFailure(vault, payload)");
        assertEq(abi.decode(failure.data, (bytes)), _withFirstArgument(payload, stranger));
        for (uint256 j = 0; j < d.logs.length; ++j) {
            assertTrue(d.logs[j].emitter != address(vault), "the vault emitted no log");
        }
        assertEq(address(vault).balance, balanceBefore, "the vault balance changes by 0");
        assertGt(d.fee, 0);

        if (d.fee <= floatBefore) {
            assertEq(proxy.reserves(address(vault)), floatBefore - d.fee, "the fee comes from the float");
            assertEq(proxy.debt(address(vault)), 0);
        } else {
            // The float does not cover the fee: the proxy takes the float and records the rest as a debt.
            assertEq(proxy.reserves(address(vault)), 0);
            assertEq(proxy.debt(address(vault)), d.fee - floatBefore);
            assertTrue(_wentIntoDebt(d.logs, address(vault)), "BlacklistContract(vault)");
            _onlyLog(d.logs, PROXY_ADDRESS, IProxyLedger.PaymentFailure.selector);
        }
    }

    /// @dev Delivers junk callbacks until the vault is in debt: the float does not cover the last fee.
    function _junkIntoDebt(address target) internal returns (uint256 count) {
        while (proxy.debt(address(vault)) == 0) {
            _junk(count++, target);
            assertLt(count, 200, "the flood ends");
        }
    }

    /// @dev Completion criterion: a delivery with a different first argument emits `CallbackFailure`,
    ///      changes the vault balance by 0 and takes only the float.
    function test_junk_takesOnlyTheFloat() public {
        (address canary, uint256 debt) = _armedCanaryInDebt(_policy());
        uint256 balanceBefore = address(vault).balance;

        uint256 fees;
        for (uint256 i = 0; i < 3; ++i) {
            fees += _junk(i, canary).fee;
        }

        assertEq(address(vault).balance, balanceBefore);
        assertEq(proxy.reserves(address(vault)), FEE_FLOAT - fees);
        assertEq(proxy.debt(canary), debt, "junk does not rescue");
    }

    /// @dev Spec §4.9: after any number of junk callbacks, the balance loss is at most `selfCareDailyCap`
    ///      per day.
    function test_junkFlood_lossBound() public {
        (address canary, uint256 canaryDebt) = _armedCanaryInDebt(_policy());
        uint256 balanceBefore = address(vault).balance;

        // Part 1. Junk until the vault is in debt. The loss of principal is 0.
        uint256 count = _junkIntoDebt(canary);
        uint256 vaultDebt = proxy.debt(address(vault));
        assertGt(count, 5, "the float paid for some junk");
        assertEq(proxy.reserves(address(vault)), 0);
        assertEq(address(vault).balance, balanceBefore, "the flood took no principal");
        assertEq(proxy.debt(canary), canaryDebt);

        // The vault is down: the proxy does not deliver to it, junk or not.
        Delivery memory down = _reconcile(canary, 1);
        assertFalse(down.delivered);
        assertEq(down.revertData, abi.encodeWithSelector(ICallbackProxy.InDebt.selector, address(vault), vaultDebt));
        assertEq(down.fee, 0);
        assertEq(address(vault).balance, balanceBefore);

        // One deposit by any account restores it.
        _takeLogs();
        vm.prank(stranger);
        proxy.depositTo{ value: vaultDebt + FEE_FLOAT }(address(vault));
        assertTrue(_debtCleared(_takeLogs(), address(vault)), "WhitelistContract(vault)");
        assertEq(proxy.debt(address(vault)), 0);
        assertEq(proxy.reserves(address(vault)), FEE_FLOAT);
        Delivery memory up = _reconcile(canary, 2);
        _assertRan(up);
        uint256 paid = _onlyResult(up.logs, 2, canary, LifelineVault.Outcome.Rescued);
        assertEq(address(vault).balance, balanceBefore - paid);

        // Part 2. A mix of junk and authenticated callbacks over three calendar days. The canary has no
        // need, so principal moves only into the float.
        for (uint256 day = 0; day < 3; ++day) {
            _skipTime(_secondsToNextDay());
            uint256 dayStart = address(vault).balance;
            uint256 moved = _floodDay(canary);
            assertEq(dayStart - address(vault).balance, moved, "the loss of the day is the self-care of the day");
            assertEq(moved, SELF_CARE_DAILY_CAP, "the flood used the whole cap of the day");
        }

        assertEq(address(vault).balance, balanceBefore - paid - 3 * SELF_CARE_DAILY_CAP, "three days, three caps");
    }

    /// @dev Twelve rounds of seven junk callbacks and one authenticated callback for a target with no need.
    ///      When the junk puts the vault into debt, another account pays the debt.
    /// @return moved The principal that moved into the float.
    function _floodDay(address canary) internal returns (uint256 moved) {
        for (uint256 round = 0; round < 12; ++round) {
            for (uint256 i = 0; i < 7 && proxy.debt(address(vault)) == 0; ++i) {
                _junk(round + i, canary);
            }

            uint256 debt = proxy.debt(address(vault));
            if (debt != 0) {
                // Another account pays the debt and about two callback fees. This is not principal.
                assertFalse(_reconcile(canary, round).delivered, "InDebt");
                vm.prank(stranger);
                proxy.depositTo{ value: debt + 2e12 }(address(vault));
            }

            uint256 balanceBefore = address(vault).balance;
            Delivery memory d = _reconcile(canary, round);
            _assertRan(d);
            _onlyResult(d.logs, round, canary, LifelineVault.Outcome.NoNeed);
            uint256 selfCare = _selfCareSum(d.logs);
            assertEq(address(vault).balance, balanceBefore - selfCare, "principal moves only into the float");
            moved += selfCare;
            assertLe(moved, SELF_CARE_DAILY_CAP, "self-care is inside the cap of the day");
        }
    }

    // ------------------------------------------------------------------------------------------
    // Self-care
    // ------------------------------------------------------------------------------------------

    function test_selfCare_returnsTheFloatToTheTarget() public {
        (uint256 selfCare, uint256 firstFee) = _idleCallback();
        assertEq(selfCare, 0, "the float was at the target");
        assertEq(proxy.reserves(address(vault)), FEE_FLOAT - firstFee);

        // The next callback puts the fee of the first one back. Its own fee is charged after it.
        uint256 balanceBefore = address(vault).balance;
        uint256 secondFee;
        (selfCare, secondFee) = _idleCallback();
        assertEq(selfCare, firstFee);
        assertEq(address(vault).balance, balanceBefore - firstFee);
        assertEq(proxy.reserves(address(vault)), FEE_FLOAT - secondFee);
    }

    function test_selfCare_staysInsideTheDailyCap() public {
        vm.prank(owner);
        vault.setSelfCare(FEE_FLOAT, 1e12);

        // The fee of each callback is below the float and above half of the cap.
        uint256 moved;
        for (uint256 i = 0; i < 5; ++i) {
            (uint256 selfCare,) = _idleCallback();
            moved += selfCare;
        }
        assertEq(moved, 1e12, "exactly the cap of the day");
        assertEq(address(vault).balance, PRINCIPAL - 1e12);

        // The next calendar day has the whole cap again.
        _skipTime(_secondsToNextDay());
        (uint256 nextDay,) = _idleCallback();
        assertEq(nextDay, 1e12);
        (nextDay,) = _idleCallback();
        assertEq(nextDay, 0);
    }

    function test_selfCare_neverTakesThePrincipalBelowTheFloor() public {
        _idleCallback();
        uint256 balance = address(vault).balance;
        vm.prank(owner);
        vault.setPrincipalFloor(balance - 1000);

        (uint256 selfCare,) = _idleCallback();
        assertEq(selfCare, 1000, "only the principal above the floor");
        assertEq(address(vault).balance, balance - 1000);

        (selfCare,) = _idleCallback();
        assertEq(selfCare, 0, "the balance is at the floor");
    }

    function test_selfCare_isOffWithAZeroTarget() public {
        vm.prank(owner);
        vault.setSelfCare(0, SELF_CARE_DAILY_CAP);

        _idleCallback();
        (uint256 selfCare,) = _idleCallback();
        assertEq(selfCare, 0);
        assertEq(address(vault).balance, PRINCIPAL);
    }

    /// @dev A proxy call of self-care that reverts is a skip: the callback runs and moves no principal.
    function test_selfCare_aProxyCallThatRevertsIsASkip() public {
        (, uint256 firstFee) = _idleCallback();
        uint256 balanceBefore = address(vault).balance;

        vm.mockCallRevert(PROXY_ADDRESS, abi.encodeCall(IProxyLedger.reserves, (address(vault))), "mocked");
        (uint256 selfCare,) = _idleCallback();
        vm.clearMockedCalls();
        assertEq(selfCare, 0, "the read of the float failed");

        vm.mockCallRevert(PROXY_ADDRESS, abi.encodeCall(IProxyLedger.depositTo, (address(vault))), "mocked");
        uint256 secondFee;
        (selfCare, secondFee) = _idleCallback();
        vm.clearMockedCalls();
        assertEq(selfCare, 0, "the deposit failed");
        assertEq(address(vault).balance, balanceBefore);

        // A failed deposit does not count toward the cap of the day.
        vm.prank(owner);
        vault.setSelfCare(FEE_FLOAT, 3 * firstFee + secondFee);
        (selfCare,) = _idleCallback();
        assertGt(selfCare, 3 * firstFee, "the three fees are put back");
    }

    // ------------------------------------------------------------------------------------------
    // Disarm
    // ------------------------------------------------------------------------------------------

    /// @dev `disarm` deletes the policy, the quarantine and the counters. The target then gets `NoPolicy`.
    function test_disarm_deletesThePolicyAndTheCounters() public {
        Policy memory policy = _debtLoopPolicy(1, WINDOW);
        (address canary, uint256 debt) = _armedCanaryInDebt(policy);

        // One rescue uses the rescue of the window and the whole daily cap. The next debt is the quarantine.
        policy.dailyCap = debt + HIGH_BELOW_ONE_FEE;
        _onlyResult(_setPolicy(canary, policy), 0, canary, LifelineVault.Outcome.Rescued);
        debt = _pongIntoDebt(canary);
        _onlyResult(_reconcile(canary, 1).logs, 1, canary, LifelineVault.Outcome.Quarantined);

        _takeLogs();
        vm.prank(owner);
        vault.disarm(canary);
        _assertOnlyVaultLog(_takeLogs(), LifelineVault.Disarmed.selector, canary);
        assertFalse(vault.hasPolicy(canary));
        assertFalse(vault.quarantined(canary));
        assertEq(abi.encode(vault.policyOf(canary)), abi.encode(Policy(0, 0, 0, 0, 0, 0)));

        uint256 balanceBefore = address(vault).balance;
        _assertSkip(_reconcile(canary, 2), balanceBefore, 2, canary, LifelineVault.Outcome.NoPolicy);
        assertEq(proxy.debt(canary), debt);

        // A new policy starts with no quarantine, no rescue history and no spend of the day.
        assertEq(
            _onlyResult(_setPolicy(canary, policy), 0, canary, LifelineVault.Outcome.Rescued), debt + HIGH_BELOW_ONE_FEE
        );
    }
}
