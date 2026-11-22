// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

import { IProxyLedger } from "./interfaces/IProxyLedger.sol";
import { Policy, validatePolicy } from "./LifelineTypes.sol";

/// @dev Sweep guard: the gas that the vault keeps for one target of a sweep.
uint256 constant VAULT_ITER_RESERVE = 60_000;

/// @dev Sweep guard: the gas that the vault keeps for the end of a sweep: self-care and its events.
uint256 constant VAULT_TAIL_RESERVE = 60_000;

/// @title LifelineVault: the payer of one destination chain.
/// @notice The owner sets a policy for a callback contract. The vault clears the debt of that contract at
///         the callback proxy and refills it inside the policy: at once in `setPolicy`, and later in the
///         callbacks `reconcile` and `sweep`. The vault is the only authority for its chain: it reads the
///         ledger at execution and acts on the state that it finds. It stores no request ID, so a late or
///         duplicate callback is harmless.
///
///         Funds. Principal is the contract balance; the owner can withdraw it. The fee float is the
///         vault's own reserves at the proxy. The vault has no `pay()` and no fallback, so the proxy can
///         take only the float. Principal moves into the float after an authenticated callback only, and
///         at most `selfCareDailyCap` for each calendar day.
///
///         Authentication. A callback is accepted only from the proxy, and only if its first argument is
///         `callbackSender`: the node overwrites that argument with the identity of the requester. Any
///         other callback reverts, and the proxy charges its fee to the float.
/// @dev The vault does not inherit a library contract, because the library base contracts add `pay()`.
///
///      Reentrancy. The only contract that the vault calls is the proxy, apart from the transfer to the
///      owner in `withdraw`. Each function that changes state accepts only the owner or the proxy, and
///      `receive()` changes no state. So no other contract can enter the vault between a call and the
///      storage writes that follow it.
contract LifelineVault {
    // ------------------------------------------------------------------------------------------
    // Types
    // ------------------------------------------------------------------------------------------

    /// @notice The result of one reconcile of one target. Off-chain readers decode it by position: do not
    ///         reorder.
    enum Outcome {
        Rescued, // paid: the payment cleared a debt above zero
        ToppedUp, // paid: no debt, reserves were below `low`
        NoNeed, // no debt and reserves at or above `low`
        Paused, // the vault is paused
        NoPolicy, // the target has no policy
        Quarantined, // the target is quarantined
        NoCode, // the target has no code
        DebtAboveCap, // `debt > maxDebt`; there is never a partial payment
        DailyCap, // the payment is above the remaining `dailyCap` of the calendar day
        Floor, // the payment is above the balance, or takes the balance below `principalFloor`
        Failed // a call to the proxy reverted
    }

    /// @notice The record of one target. The state is one storage slot, so the quarantine check is one
    ///         storage read and a payment is one storage write.
    struct Record {
        bool hasPolicy;
        bool quarantined;
        uint16 spentDay; // the calendar day (`block.timestamp / 1 days`) of `spentToday`
        uint96 spentToday; // principal paid to this target on `spentDay`
        uint128 rescueTimes; // sliding window: the timestamps of the last four rescues, 32 bits each, newest lowest
        Policy policy;
    }

    // ------------------------------------------------------------------------------------------
    // Events and errors
    // ------------------------------------------------------------------------------------------

    /// @param id The `requestId` of a `reconcile`, the `tickId` of a `sweep`, 0 for the reconcile in `setPolicy`.
    event Result(uint256 indexed id, address indexed target, Outcome outcome, uint256 paid);
    event PolicySet(address indexed target);
    event Disarmed(address indexed target);
    event Quarantined(address indexed target);
    event Released(address indexed target);
    event SelfCare(uint256 added);
    event VaultLow(uint256 balance, uint256 principalFloor);
    event SweepPartial(uint256 indexed tickId, uint256 processed);

    error NotProxy();
    error NotCallbackSender(address sender);
    error NotOwner();
    error InvalidTarget(address target);
    error InsufficientValue(uint256 value, uint256 feeFloat);
    error WithdrawFailed();

    // ------------------------------------------------------------------------------------------
    // Constants and storage
    // ------------------------------------------------------------------------------------------

    /// @dev The rescue window holds `MAX_RESCUES_LIMIT` timestamps of 32 bits: valid until the year 2106.
    uint256 internal constant RESCUE_TIME_BITS = 32;

    address public immutable owner;

    /// @notice The callback proxy of this chain: the only caller of the callbacks, and the ledger.
    address public immutable proxy;

    /// @notice The value that the node writes into the first argument of each callback that the hub requests.
    address public immutable callbackSender;

    /// @notice A paused vault pays nothing.
    bool public paused;

    /// @dev The calendar day of `_selfCareSpent`. The constructor sets it, so this slot is never zero and a
    ///      later write does not pay for a new slot.
    uint64 internal _selfCareDay;
    uint184 internal _selfCareSpent;

    /// @notice The vault tops its own reserves up to this value after each authenticated callback.
    uint256 public selfReserveTarget;

    /// @notice The most principal that self-care moves to the float in one calendar day.
    uint256 public selfCareDailyCap;

    /// @notice No payment and no self-care takes the balance below this value.
    uint256 public principalFloor;

    mapping(address target => Record) internal _records;

    // ------------------------------------------------------------------------------------------
    // Construction and funds
    // ------------------------------------------------------------------------------------------

    /// @param proxy_ The callback proxy of this chain.
    /// @param callbackSender_ The value of the first argument of an authenticated callback.
    /// @param feeFloat The part of `msg.value` that goes to the vault's reserves. The rest stays as principal.
    constructor(
        address proxy_,
        address callbackSender_,
        uint256 feeFloat,
        uint256 selfReserveTarget_,
        uint256 selfCareDailyCap_,
        uint256 principalFloor_
    ) payable {
        if (msg.value < feeFloat) revert InsufficientValue(msg.value, feeFloat);

        owner = msg.sender;
        proxy = proxy_;
        callbackSender = callbackSender_;
        selfReserveTarget = selfReserveTarget_;
        selfCareDailyCap = selfCareDailyCap_;
        principalFloor = principalFloor_;
        _selfCareDay = uint64(block.timestamp / 1 days);

        if (feeFloat != 0) {
            IProxyLedger(proxy_).depositTo{ value: feeFloat }(address(this));
        }
        emit SelfCare(feeFloat);
    }

    /// @notice Accepts principal. The proxy's `pay(uint256)` call has calldata and does not reach this.
    receive() external payable { }

    modifier onlyOwner() {
        _onlyOwner();
        _;
    }

    function _onlyOwner() internal view {
        if (msg.sender != owner) revert NotOwner();
    }

    /// @notice Sends principal to the owner.
    function withdraw(uint256 amount) external onlyOwner {
        (bool ok,) = owner.call{ value: amount }("");
        if (!ok) revert WithdrawFailed();
    }

    function setSelfCare(uint256 selfReserveTarget_, uint256 selfCareDailyCap_) external onlyOwner {
        selfReserveTarget = selfReserveTarget_;
        selfCareDailyCap = selfCareDailyCap_;
    }

    function setPrincipalFloor(uint256 principalFloor_) external onlyOwner {
        principalFloor = principalFloor_;
    }

    /// @notice A paused vault gives `Paused` for each target.
    function pause() external onlyOwner {
        paused = true;
    }

    function unpause() external onlyOwner {
        paused = false;
    }

    // ------------------------------------------------------------------------------------------
    // Policies
    // ------------------------------------------------------------------------------------------

    /// @notice Stores the policy of a target and reconciles the target at once, with id 0. It needs no
    ///         callback and runs no self-care. It keeps a quarantine, the rescue history and the spend of
    ///         the day. The vault itself is not a target: only self-care moves principal into the float.
    function setPolicy(address target, Policy calldata policy) external onlyOwner {
        validatePolicy(policy);
        if (target == address(this)) revert InvalidTarget(target);

        Record storage r = _records[target];
        r.hasPolicy = true;
        r.policy = policy;
        emit PolicySet(target);

        _reconcile(0, target);
    }

    /// @notice Clears the quarantine and the rescue history of a target.
    function release(address target) external onlyOwner {
        Record storage r = _records[target];
        r.quarantined = false;
        r.rescueTimes = 0;
        emit Released(target);
    }

    /// @notice Deletes the policy, the quarantine and the counters of a target. The target then gets `NoPolicy`.
    function disarm(address target) external onlyOwner {
        delete _records[target];
        emit Disarmed(target);
    }

    // ------------------------------------------------------------------------------------------
    // Callbacks
    // ------------------------------------------------------------------------------------------

    /// @notice Callback: reconciles one target. After the authentication, no path reverts.
    /// @param sender The first callback argument: the node overwrites it.
    function reconcile(address sender, address target, uint256 requestId) external {
        _authenticate(sender);
        _reconcile(requestId, target);
        _selfCare();
    }

    /// @notice Callback: reconciles each target of the list. It stops with `SweepPartial` when the gas that
    ///         is left does not cover one more target and the end of the sweep. After the authentication, no
    ///         path reverts.
    /// @param sender The first callback argument: the node overwrites it.
    function sweep(address sender, address[] calldata targets, uint256 tickId) external {
        _authenticate(sender);

        uint256 count = targets.length;
        for (uint256 i = 0; i < count; ++i) {
            if (gasleft() < VAULT_ITER_RESERVE + VAULT_TAIL_RESERVE) {
                emit SweepPartial(tickId, i);
                break;
            }
            _reconcile(tickId, targets[i]);
        }

        _selfCare();
    }

    /// @dev The two local checks. Both reverts are intended: the proxy then emits `CallbackFailure`.
    function _authenticate(address sender) internal view {
        if (msg.sender != proxy) revert NotProxy();
        if (sender != callbackSender) revert NotCallbackSender(sender);
    }

    // ------------------------------------------------------------------------------------------
    // Reconcile
    // ------------------------------------------------------------------------------------------

    /// @dev The one reconcile path of a target, for `setPolicy`, `reconcile` and each target of `sweep`.
    ///      It emits exactly one `Result` and never reverts.
    function _reconcile(uint256 id, address target) internal {
        (Outcome outcome, uint256 paid) = _settle(target);
        emit Result(id, target, outcome, paid);
    }

    /// @dev The checks in their order: paused, policy, quarantine, code, ledger, rescue window, `maxDebt`,
    ///      then the payment. Each call to the proxy runs in a `try`: a revert gives `Failed`.
    function _settle(address target) internal returns (Outcome, uint256) {
        if (paused) return (Outcome.Paused, 0);

        Record storage r = _records[target];
        if (!r.hasPolicy) return (Outcome.NoPolicy, 0);
        // Before any ledger read: a quarantined target costs one storage read.
        if (r.quarantined) return (Outcome.Quarantined, 0);
        if (target.code.length == 0) return (Outcome.NoCode, 0);

        (bool ok, uint256 debt, uint256 reserves) = _readLedger(target);
        if (!ok) return (Outcome.Failed, 0);

        Policy storage policy = r.policy;
        if (debt == 0) {
            if (reserves >= policy.low) return (Outcome.NoNeed, 0);
        } else {
            if (_rescueWindowFull(r.rescueTimes, policy.maxRescues, policy.window)) {
                r.quarantined = true;
                emit Quarantined(target);
                return (Outcome.Quarantined, 0);
            }
            if (debt > policy.maxDebt) return (Outcome.DebtAboveCap, 0);
        }

        uint256 high = policy.high;
        return _pay(r, target, debt, reserves < high ? high - reserves : 0);
    }

    /// @dev Reads the debt and the reserves of a contract at the proxy now. It never reverts.
    function _readLedger(address target) internal view returns (bool ok, uint256 debt, uint256 reserves) {
        try IProxyLedger(proxy).debt(target) returns (uint256 debt_) {
            try IProxyLedger(proxy).reserves(target) returns (uint256 reserves_) {
                return (true, debt_, reserves_);
            } catch { }
        } catch { }
    }

    /// @dev Pays `debt + refill` to a target if the whole amount is inside the remaining `dailyCap` of the
    ///      calendar day and leaves `principalFloor` in the balance. If not, it pays nothing. The caller
    ///      makes sure that `debt + refill > 0`. It never reverts.
    function _pay(Record storage r, address target, uint256 debt, uint256 refill) internal returns (Outcome, uint256) {
        uint256 day = block.timestamp / 1 days;
        uint256 spent = r.spentDay == uint16(day) ? r.spentToday : 0;
        uint256 dailyCap = r.policy.dailyCap;
        // `spentToday` has 96 bits.
        if (dailyCap > type(uint96).max) dailyCap = type(uint96).max;
        uint256 room = spent < dailyCap ? dailyCap - spent : 0;
        if (debt > room || refill > room - debt) return (Outcome.DailyCap, 0);
        // No overflow: `debt + refill <= room`.
        uint256 amount = debt + refill;

        // `depositTo` spends the balance; the float cannot fund it. Check before the call.
        uint256 balance = address(this).balance;
        if (balance < amount || balance - amount < principalFloor) return (Outcome.Floor, 0);

        try IProxyLedger(proxy).depositTo{ value: amount }(target) { }
        catch {
            return (Outcome.Failed, 0);
        }

        r.spentDay = uint16(day);
        // No truncation: `spent + amount <= dailyCap <= type(uint96).max`.
        r.spentToday = uint96(spent + amount);
        if (debt == 0) return (Outcome.ToppedUp, amount);

        // A payment that cleared a debt above zero is a rescue. Only a rescue counts toward `maxRescues`.
        r.rescueTimes = (r.rescueTimes << uint128(RESCUE_TIME_BITS)) | uint32(block.timestamp);
        return (Outcome.Rescued, amount);
    }

    /// @dev True if `maxRescues` rescues are inside `window`: the `maxRescues`-th newest timestamp is set and
    ///      less than `window` seconds old. `validatePolicy` keeps `maxRescues` in 1..4.
    function _rescueWindowFull(uint256 rescueTimes, uint256 maxRescues, uint256 window) internal view returns (bool) {
        uint256 oldest = uint32(rescueTimes >> (RESCUE_TIME_BITS * (maxRescues - 1)));
        return oldest != 0 && block.timestamp - oldest < window;
    }

    // ------------------------------------------------------------------------------------------
    // Self-care
    // ------------------------------------------------------------------------------------------

    /// @dev The end of an authenticated callback. Emits `VaultLow` if the balance is below the floor. If
    ///      not, tops the vault's own reserves up to `selfReserveTarget`: at most `selfCareDailyCap` for
    ///      each calendar day, and never below `principalFloor`. It never reverts.
    function _selfCare() internal {
        uint256 balance = address(this).balance;
        uint256 floor = principalFloor;
        if (balance < floor) {
            emit VaultLow(balance, floor);
            return;
        }

        uint256 target = selfReserveTarget;
        if (target == 0) return;

        uint256 reserves;
        try IProxyLedger(proxy).reserves(address(this)) returns (uint256 value) {
            reserves = value;
        } catch {
            return;
        }
        if (reserves >= target) return;
        uint256 add = target - reserves;

        uint256 day = block.timestamp / 1 days;
        uint256 spent = _selfCareDay == day ? _selfCareSpent : 0;
        uint256 cap = selfCareDailyCap;
        // `_selfCareSpent` has 184 bits.
        if (cap > type(uint184).max) cap = type(uint184).max;
        uint256 room = spent < cap ? cap - spent : 0;
        if (add > room) add = room;
        if (add > balance - floor) add = balance - floor;

        if (add == 0) return;

        try IProxyLedger(proxy).depositTo{ value: add }(address(this)) {
            _selfCareDay = uint64(day);
            // No truncation: `spent + add <= cap <= type(uint184).max`.
            _selfCareSpent = uint184(spent + add);
            emit SelfCare(add);
        } catch { }
    }

    // ------------------------------------------------------------------------------------------
    // Views
    // ------------------------------------------------------------------------------------------

    function hasPolicy(address target) external view returns (bool) {
        return _records[target].hasPolicy;
    }

    /// @notice The stored policy of a target. All fields are 0 if the target has no policy.
    function policyOf(address target) external view returns (Policy memory) {
        return _records[target].policy;
    }

    function quarantined(address target) external view returns (bool) {
        return _records[target].quarantined;
    }
}
