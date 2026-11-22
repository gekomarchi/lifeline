// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

import { IReactive } from "@reactive/src/interfaces/IReactive.sol";
import { ISystemContract, REACTIVE_IGNORE } from "./interfaces/ISystemContract.sol";
import { Policy, TargetKind, TargetState, validatePolicy } from "./LifelineTypes.sol";

/// @title LifelineHub: the guardian of enrolled reactive contracts on Reactive Network (Lasna).
/// @notice When the system contract reports a debt for an enrolled target, the hub clears the debt and
///         refills the target inside a policy. It counts rescues and quarantines a target that breaks.
///
///         Funds. Principal is the contract balance; the owner can withdraw it. The fee float is the
///         hub's own reserves at the system contract. The hub has no `pay()` and no fallback, so the
///         ledger can take only the float.
///
///         The contract is not upgradeable. The storage layout, the `react()` dispatch and the external
///         signatures of `enroll` and `disarm` are the ones of the full design: the handlers for
///         destination targets, vault debt and cron are skips here and get their bodies in later units.
/// @dev The hub does not inherit a library contract, because the library base contracts add `pay()`.
///      It uses the log record type of `reactive-lib-omni` and matches the `react` selector.
///
///      Reentrancy. The only contract that the hub calls is the system contract, apart from the transfer to
///      the owner in `withdraw`. Each function that changes state accepts only the owner or the system
///      contract, and `receive()` changes no state. So no other contract can enter the hub between a call
///      and the storage writes that follow it.
contract LifelineHub {
    // ------------------------------------------------------------------------------------------
    // Types
    // ------------------------------------------------------------------------------------------

    /// @notice Why a log or a payment was skipped. Later units append values and do not reorder.
    enum SkipReason {
        Paused, // the hub is paused
        UnknownChain, // the chain of the log is not Lasna and has no registered vault
        WrongEmitter, // the emitter is not the ledger of that chain
        UnknownTopic, // `topic0` is not an event that the hub handles
        BadTopicCount, // the log does not have the topic count of the event
        SelfTarget, // the debt log is for the hub itself
        NotEnrolled, // the target was never enrolled, or it is disarmed
        NotActive, // the target is quarantined or parked
        LedgerReadFailed, // the ledger did not answer `debt` or `reserves`
        DebtAboveMax, // `debt > maxDebt`; there is never a partial payment
        DailyCap, // the payment is above the remaining `dailyCap` of the calendar day
        Floor, // the payment is above the balance, or takes the balance below `principalFloor`
        DepositFailed, // `depositTo` reverted
        Unhandled // the event kind has no handler in this unit
    }

    /// @notice The kinds of subscription tuple in the registry.
    enum TupleKind {
        TargetDebt, // `BlacklistContract(target)` of the ledger of the target's chain
        VaultDebt, // `BlacklistContract(vault)` of a destination proxy
        Cron // the cron tick
    }

    /// @notice One subscription: the exact six fields of `subscribe` and `unsubscribe`.
    struct Tuple {
        TupleKind kind;
        uint32 index; // position in `_tupleKeys[kind]` plus 1; 0 = not registered
        address emitter;
        uint256 chainId;
        uint256 topic0;
        uint256 topic1;
        uint256 topic2;
        uint256 topic3;
    }

    /// @notice The record of one target, keyed by `(chainId, target)`.
    struct Target {
        TargetKind kind;
        TargetState state;
        uint32 listIndex; // position in `_targetList[chainId]` plus 1; 0 = not in the list
        uint64 spentDay; // the calendar day (`block.timestamp / 1 days`) of `spentToday`
        uint256 spentToday; // principal paid to this target on `spentDay`
        uint256 rescueTimes; // sliding window: the timestamps of the last four rescues, 64 bits each, newest lowest
        Policy policy;
    }

    /// @notice The record of the vault of one destination chain. Empty in this unit.
    struct Vault {
        address proxy; // the callback proxy of that chain: the emitter of its debt logs
        address vault; // the LifelineVault of that chain
        uint64 sweepBase; // the base gas of a sweep callback on that proxy
        bool up; // false while the vault is in debt at its proxy
    }

    // ------------------------------------------------------------------------------------------
    // Events and errors
    // ------------------------------------------------------------------------------------------

    event Enrolled(uint256 indexed chainId, address indexed target);
    event Disarmed(uint256 indexed chainId, address indexed target);
    event Rescued(address indexed target, uint256 debt, uint256 paid);
    event ToppedUp(address indexed target, uint256 paid);
    event Gap(address indexed target, uint256 deadFromBlock, uint256 aliveAtBlock);
    event Skipped(uint256 indexed chainId, address indexed target, SkipReason reason);
    event Quarantined(address indexed target);
    event Released(address indexed target);
    event HubLow(uint256 balance, uint256 principalFloor);
    event SelfCare(uint256 added);

    error UnknownChain(uint256 chainId);
    error NotOwner();
    error NotSystem(address caller);
    error InvalidTarget(address target);
    error AlreadyEnrolled(uint256 chainId, address target);
    error NotEnrolled(uint256 chainId, address target);
    error NotQuarantined(address target);
    error InsufficientValue(uint256 value, uint256 feeFloat);
    error WithdrawFailed();

    // ------------------------------------------------------------------------------------------
    // Constants and storage
    // ------------------------------------------------------------------------------------------

    /// @dev `topic0` of each debt subscription: the selector of `IProxyLedger.BlacklistContract`.
    ///      A constant in code: it can never be a wildcard.
    uint256 internal constant BLACKLIST_TOPIC0 = uint256(keccak256("BlacklistContract(address)"));

    /// @dev `BlacklistContract(address indexed)` has two topics. The node sets `opCode` to the topic count.
    uint256 internal constant DEBT_LOG_TOPIC_COUNT = 2;

    /// @dev The rescue window holds four timestamps of 64 bits in one slot.
    uint256 internal constant RESCUE_TIME_BITS = 64;

    ISystemContract internal immutable SYSTEM;

    address public immutable owner;

    bool public paused;

    /// @notice The hub tops its own reserves up to this value after each authenticated `react()`.
    uint256 public selfReserveTarget;

    /// @notice The most principal that self-care moves to the float in one calendar day.
    uint256 public selfCareDailyCap;

    /// @notice No payment and no self-care takes the balance below this value.
    uint256 public principalFloor;

    uint64 internal _selfCareDay;
    uint192 internal _selfCareSpent;

    mapping(uint256 chainId => mapping(address target => Target)) internal _targets;

    /// @dev The enrolled targets of each chain ID. The list of `block.chainid` is the Lasna fleet.
    mapping(uint256 chainId => address[]) internal _targetList;

    /// @dev The vault of each destination chain ID. The dispatch routes a destination log by its proxy.
    mapping(uint256 chainId => Vault) internal _vaults;

    /// @dev The subscription-tuple registry. Each subscribe and each unsubscribe goes through it.
    mapping(bytes32 key => Tuple) internal _tuples;
    mapping(TupleKind kind => bytes32[]) internal _tupleKeys;

    // ------------------------------------------------------------------------------------------
    // Construction and funds
    // ------------------------------------------------------------------------------------------

    /// @param systemContract The system contract of Reactive Network.
    /// @param feeFloat The part of `msg.value` that goes to the hub's reserves. The rest stays as principal.
    constructor(
        address systemContract,
        uint256 feeFloat,
        uint256 selfReserveTarget_,
        uint256 selfCareDailyCap_,
        uint256 principalFloor_
    ) payable {
        if (msg.value < feeFloat) revert InsufficientValue(msg.value, feeFloat);

        owner = msg.sender;
        SYSTEM = ISystemContract(systemContract);
        selfReserveTarget = selfReserveTarget_;
        selfCareDailyCap = selfCareDailyCap_;
        principalFloor = principalFloor_;

        if (feeFloat != 0) {
            SYSTEM.depositTo{ value: feeFloat }(address(this));
        }
        emit SelfCare(feeFloat);
    }

    /// @notice Accepts principal. The ledger's `pay(uint256)` call has calldata and does not reach this.
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

    /// @notice A paused hub skips each log.
    function pause() external onlyOwner {
        paused = true;
    }

    function unpause() external onlyOwner {
        paused = false;
    }

    // ------------------------------------------------------------------------------------------
    // Enrollment
    // ------------------------------------------------------------------------------------------

    /// @notice Enrolls a target. A Lasna target is subscribed with the exact `topic1` and reconciled at once,
    ///         because a subscription does not replay an existing debt.
    function enroll(uint256 chainId, address target, Policy calldata policy) external onlyOwner {
        if (chainId == block.chainid) {
            _addTarget(chainId, target, TargetKind.Lasna, policy);
            _subscribe(TupleKind.TargetDebt, chainId, address(SYSTEM), BLACKLIST_TOPIC0, uint256(uint160(target)));
            emit Enrolled(chainId, target);
            _reconcileLasna(target);
        } else {
            revert UnknownChain(chainId);
        }
    }

    /// @notice The intentional stop of a target. `enroll` is permitted again after it.
    function disarm(uint256 chainId, address target) external onlyOwner {
        if (chainId == block.chainid) {
            _removeTarget(chainId, target);
            _unsubscribe(TupleKind.TargetDebt, chainId, address(SYSTEM), BLACKLIST_TOPIC0, uint256(uint160(target)));
            emit Disarmed(chainId, target);
        } else {
            revert UnknownChain(chainId);
        }
    }

    /// @notice Restores a quarantined Lasna target: clears its rescue history, subscribes again and reconciles.
    function release(address target) external onlyOwner {
        Target storage t = _targets[block.chainid][target];
        if (t.state != TargetState.Quarantined) revert NotQuarantined(target);

        t.state = TargetState.Active;
        t.rescueTimes = 0;
        _subscribe(TupleKind.TargetDebt, block.chainid, address(SYSTEM), BLACKLIST_TOPIC0, uint256(uint160(target)));
        emit Released(target);
        _reconcileLasna(target);
    }

    /// @notice Unsubscribes each tuple in the registry with its stored fields and parks each active target.
    ///         There is no bulk restore: a target is restored with `disarm` and `enroll`.
    function unsubscribeAll() external onlyOwner {
        bytes32[] storage keys = _tupleKeys[TupleKind.TargetDebt];
        uint256 count = keys.length;
        for (uint256 i = 0; i < count; ++i) {
            Tuple storage tuple = _tuples[keys[i]];
            Target storage t = _targets[tuple.chainId][address(uint160(tuple.topic1))];
            if (t.state == TargetState.Active) t.state = TargetState.Parked;
        }
        _unsubscribeKind(TupleKind.TargetDebt);
        _unsubscribeKind(TupleKind.VaultDebt);
        _unsubscribeKind(TupleKind.Cron);
    }

    /// @dev Validates and stores the record of a target and adds it to the list of its chain.
    function _addTarget(uint256 chainId, address target, TargetKind kind, Policy calldata policy) internal {
        validatePolicy(policy);
        if (target == address(this)) revert InvalidTarget(target);

        Target storage t = _targets[chainId][target];
        if (t.kind != TargetKind.None && t.state != TargetState.Disarmed) revert AlreadyEnrolled(chainId, target);

        address[] storage list = _targetList[chainId];
        list.push(target);

        t.kind = kind;
        t.state = TargetState.Active;
        t.listIndex = uint32(list.length);
        t.rescueTimes = 0;
        t.policy = policy;
        // `spentDay` and `spentToday` stay: `dailyCap` is a cap of the calendar day across `disarm` and `enroll`.
    }

    /// @dev Removes a target from the list of its chain and keeps its record in state `Disarmed`.
    function _removeTarget(uint256 chainId, address target) internal {
        Target storage t = _targets[chainId][target];
        if (t.kind == TargetKind.None || t.state == TargetState.Disarmed) revert NotEnrolled(chainId, target);

        address[] storage list = _targetList[chainId];
        uint256 index = t.listIndex;
        uint256 last = list.length;
        if (index != last) {
            address moved = list[last - 1];
            list[index - 1] = moved;
            _targets[chainId][moved].listIndex = uint32(index);
        }
        list.pop();

        t.listIndex = 0;
        t.state = TargetState.Disarmed;
    }

    // ------------------------------------------------------------------------------------------
    // Subscription-tuple registry
    // ------------------------------------------------------------------------------------------

    function _tupleKey(uint256 chainId, address emitter, uint256 topic0, uint256 topic1)
        internal
        pure
        returns (bytes32)
    {
        return keccak256(abi.encode(chainId, emitter, topic0, topic1, REACTIVE_IGNORE, REACTIVE_IGNORE));
    }

    /// @dev Stores the six-field tuple and subscribes with it. A tuple that is registered stays as it is.
    function _subscribe(TupleKind kind, uint256 chainId, address emitter, uint256 topic0, uint256 topic1) internal {
        bytes32 key = _tupleKey(chainId, emitter, topic0, topic1);
        Tuple storage tuple = _tuples[key];
        if (tuple.index != 0) return;

        bytes32[] storage keys = _tupleKeys[kind];
        keys.push(key);

        tuple.kind = kind;
        tuple.index = uint32(keys.length);
        tuple.emitter = emitter;
        tuple.chainId = chainId;
        tuple.topic0 = topic0;
        tuple.topic1 = topic1;
        tuple.topic2 = REACTIVE_IGNORE;
        tuple.topic3 = REACTIVE_IGNORE;

        SYSTEM.subscribe(chainId, emitter, topic0, topic1, REACTIVE_IGNORE, REACTIVE_IGNORE);
    }

    /// @dev Unsubscribes a registered tuple with its stored fields. A tuple that is not registered is ignored.
    function _unsubscribe(TupleKind kind, uint256 chainId, address emitter, uint256 topic0, uint256 topic1) internal {
        bytes32 key = _tupleKey(chainId, emitter, topic0, topic1);
        Tuple storage tuple = _tuples[key];
        uint256 index = tuple.index;
        if (index == 0 || tuple.kind != kind) return;

        bytes32[] storage keys = _tupleKeys[kind];
        uint256 last = keys.length;
        if (index != last) {
            bytes32 moved = keys[last - 1];
            keys[index - 1] = moved;
            _tuples[moved].index = uint32(index);
        }
        keys.pop();

        _dropTuple(key, tuple);
    }

    /// @dev Unsubscribes each tuple of one kind.
    function _unsubscribeKind(TupleKind kind) internal {
        bytes32[] storage keys = _tupleKeys[kind];
        for (uint256 i = keys.length; i != 0; --i) {
            bytes32 key = keys[i - 1];
            keys.pop();
            _dropTuple(key, _tuples[key]);
        }
    }

    /// @dev Deletes the stored tuple and calls `unsubscribe` with the six stored fields.
    function _dropTuple(bytes32 key, Tuple storage tuple) internal {
        uint256 chainId = tuple.chainId;
        address emitter = tuple.emitter;
        uint256 topic0 = tuple.topic0;
        uint256 topic1 = tuple.topic1;
        uint256 topic2 = tuple.topic2;
        uint256 topic3 = tuple.topic3;
        delete _tuples[key];

        SYSTEM.unsubscribe(chainId, emitter, topic0, topic1, topic2, topic3);
    }

    /// @dev A cron log is a log that matches the chain, the emitter and `topic0` of a registered cron tuple.
    function _isCronLog(IReactive.LogRecord calldata log) internal view returns (bool) {
        Tuple storage tuple = _tuples[_tupleKey(log.chainId, log.contractAddress, log.topic0, REACTIVE_IGNORE)];
        return tuple.index != 0 && tuple.kind == TupleKind.Cron;
    }

    // ------------------------------------------------------------------------------------------
    // Reaction
    // ------------------------------------------------------------------------------------------

    /// @notice Entry point of the node. Only the system contract can call it. After that check, no path
    ///         reverts: each path checks, emits and returns.
    function react(IReactive.LogRecord calldata log) external {
        if (msg.sender != address(SYSTEM)) revert NotSystem(msg.sender);

        uint256 balance = address(this).balance;
        uint256 floor = principalFloor;
        if (balance < floor) {
            // Park: stop the cron cost. The hub pays nothing in this state.
            emit HubLow(balance, floor);
            _unsubscribeKind(TupleKind.Cron);
            return;
        }

        if (paused) {
            emit Skipped(log.chainId, address(uint160(log.topic1)), SkipReason.Paused);
        } else {
            _dispatch(log);
        }

        _selfCare();
    }

    /// @dev Routes a log by chain, emitter and `topic0` to the handler of its event kind.
    function _dispatch(IReactive.LogRecord calldata log) internal {
        uint256 chainId = log.chainId;
        address emitter = log.contractAddress;
        bool isDebtLog = log.topic0 == BLACKLIST_TOPIC0;
        bool isLasna = chainId == block.chainid;
        SkipReason reason;

        if (isLasna) {
            if (emitter == address(SYSTEM) && isDebtLog) {
                if (log.opCode == DEBT_LOG_TOPIC_COUNT) {
                    _onLasnaTargetDebt(log);
                    return;
                }
                reason = SkipReason.BadTopicCount;
            } else if (_isCronLog(log)) {
                _onCron(log);
                return;
            } else {
                reason = emitter == address(SYSTEM) ? SkipReason.UnknownTopic : SkipReason.WrongEmitter;
            }
        } else {
            Vault storage v = _vaults[chainId];
            address proxy = v.proxy;
            if (proxy == address(0)) {
                reason = SkipReason.UnknownChain;
            } else if (emitter != proxy) {
                reason = SkipReason.WrongEmitter;
            } else if (!isDebtLog) {
                reason = SkipReason.UnknownTopic;
            } else if (log.opCode != DEBT_LOG_TOPIC_COUNT) {
                reason = SkipReason.BadTopicCount;
            } else {
                if (address(uint160(log.topic1)) == v.vault) {
                    _onVaultDebt(log);
                } else {
                    _onDestinationTargetDebt(log);
                }
                return;
            }
        }

        emit Skipped(chainId, address(uint160(log.topic1)), reason);
    }

    /// @dev Handler: `BlacklistContract(target)` of the system contract on Lasna.
    function _onLasnaTargetDebt(IReactive.LogRecord calldata log) internal {
        address target = address(uint160(log.topic1));
        SkipReason reason;

        if (target == address(this)) {
            reason = SkipReason.SelfTarget;
        } else {
            Target storage t = _targets[block.chainid][target];
            TargetState state = t.state;
            if (t.kind == TargetKind.None || state == TargetState.Disarmed) {
                reason = SkipReason.NotEnrolled;
            } else if (state != TargetState.Active) {
                reason = SkipReason.NotActive;
            } else {
                // The origin block of the debt log is the start of the gap.
                _reconcileLasnaAt(target, log.blockNumber);
                return;
            }
        }

        emit Skipped(block.chainid, target, reason);
    }

    /// @dev Handler: `BlacklistContract(target)` of a registered destination proxy. A skip in this unit.
    function _onDestinationTargetDebt(IReactive.LogRecord calldata log) internal {
        emit Skipped(log.chainId, address(uint160(log.topic1)), SkipReason.Unhandled);
    }

    /// @dev Handler: `BlacklistContract(vault)` of a registered destination proxy. A skip in this unit.
    function _onVaultDebt(IReactive.LogRecord calldata log) internal {
        emit Skipped(log.chainId, address(uint160(log.topic1)), SkipReason.Unhandled);
    }

    /// @dev Handler: the cron tick. A skip in this unit.
    function _onCron(IReactive.LogRecord calldata log) internal {
        emit Skipped(log.chainId, address(uint160(log.topic1)), SkipReason.Unhandled);
    }

    // ------------------------------------------------------------------------------------------
    // Lasna reconcile
    // ------------------------------------------------------------------------------------------

    /// @dev The one reconcile path of a Lasna target, for a call that no debt log started: `enroll`,
    ///      `release` and the gauge. It never reverts.
    function _reconcileLasna(address target) internal {
        _reconcileLasnaAt(target, 0);
    }

    /// @dev Reads `debt` and `reserves` now, then pays `debt + (high - reserves)` inside the policy, or
    ///      skips. There is never a partial payment. It never reverts.
    /// @param deadFromBlock The origin block of the debt log, or 0 if no debt log started this call.
    function _reconcileLasnaAt(address target, uint256 deadFromBlock) internal {
        Target storage t = _targets[block.chainid][target];
        if (t.kind != TargetKind.Lasna || t.state != TargetState.Active) return;

        (bool ok, uint256 debt, uint256 reserves) = _readLedger(target);
        if (!ok) {
            emit Skipped(block.chainid, target, SkipReason.LedgerReadFailed);
            return;
        }

        Policy storage policy = t.policy;

        // The healthy target: no event and no payment.
        if (debt == 0 && reserves >= policy.low) return;

        if (debt != 0) {
            if (_rescueWindowFull(t.rescueTimes, policy.maxRescues, policy.window)) {
                t.state = TargetState.Quarantined;
                emit Quarantined(target);
                // A quarantined event still costs a reaction, so the hub stops listening to this target.
                _unsubscribe(
                    TupleKind.TargetDebt, block.chainid, address(SYSTEM), BLACKLIST_TOPIC0, uint256(uint160(target))
                );
                return;
            }
            if (debt > policy.maxDebt) {
                emit Skipped(block.chainid, target, SkipReason.DebtAboveMax);
                return;
            }
        }

        uint256 high = policy.high;
        uint256 paid = _payLasna(t, target, debt, reserves < high ? high - reserves : 0);
        if (paid == 0) return;

        if (debt != 0) {
            // A payment that cleared a debt above zero is a rescue. Only a rescue counts toward `maxRescues`.
            t.rescueTimes = (t.rescueTimes << RESCUE_TIME_BITS) | uint64(block.timestamp);
            emit Rescued(target, debt, paid);
            emit Gap(target, deadFromBlock, block.number);
        } else {
            emit ToppedUp(target, paid);
        }
    }

    /// @dev Reads the debt and the reserves of a contract at the system contract. It never reverts.
    function _readLedger(address target) internal view returns (bool ok, uint256 debt, uint256 reserves) {
        try SYSTEM.debt(target) returns (uint256 value) {
            debt = value;
        } catch {
            return (false, 0, 0);
        }
        try SYSTEM.reserves(target) returns (uint256 value) {
            reserves = value;
        } catch {
            return (false, 0, 0);
        }
        ok = true;
    }

    /// @dev Pays `debt + refill` to a Lasna target if the whole amount is inside the remaining `dailyCap` of
    ///      the calendar day and leaves `principalFloor` in the balance. If not, it emits the skip and pays
    ///      nothing. The caller makes sure that `debt + refill > 0`. It never reverts.
    /// @return amount The amount paid, or 0 after a skip.
    function _payLasna(Target storage t, address target, uint256 debt, uint256 refill)
        internal
        returns (uint256 amount)
    {
        uint256 day = block.timestamp / 1 days;
        uint256 spent = t.spentDay == day ? t.spentToday : 0;
        uint256 dailyCap = t.policy.dailyCap;
        uint256 room = spent < dailyCap ? dailyCap - spent : 0;
        if (debt > room || refill > room - debt) {
            emit Skipped(block.chainid, target, SkipReason.DailyCap);
            return 0;
        }
        // No overflow: `debt + refill <= room`.
        amount = debt + refill;

        // `depositTo` spends the balance; reserves cannot fund it. Check before the call.
        uint256 balance = address(this).balance;
        if (balance < amount || balance - amount < principalFloor) {
            emit Skipped(block.chainid, target, SkipReason.Floor);
            return 0;
        }

        try SYSTEM.depositTo{ value: amount }(target) { }
        catch {
            emit Skipped(block.chainid, target, SkipReason.DepositFailed);
            return 0;
        }

        t.spentDay = uint64(day);
        t.spentToday = spent + amount;
    }

    /// @dev True if `maxRescues` rescues are inside `window`: the `maxRescues`-th newest timestamp is set and
    ///      less than `window` seconds old. `validatePolicy` keeps `maxRescues` in 1..4.
    function _rescueWindowFull(uint256 rescueTimes, uint256 maxRescues, uint256 window) internal view returns (bool) {
        uint256 oldest = uint64(rescueTimes >> (RESCUE_TIME_BITS * (maxRescues - 1)));
        return oldest != 0 && block.timestamp - oldest < window;
    }

    // ------------------------------------------------------------------------------------------
    // Self-care
    // ------------------------------------------------------------------------------------------

    /// @dev Tops the hub's own reserves up to `selfReserveTarget`: at most `selfCareDailyCap` for each
    ///      calendar day, and never below `principalFloor`. It never reverts.
    function _selfCare() internal {
        uint256 target = selfReserveTarget;
        if (target == 0) return;

        uint256 reserves;
        try SYSTEM.reserves(address(this)) returns (uint256 value) {
            reserves = value;
        } catch {
            return;
        }
        if (reserves >= target) return;
        uint256 add = target - reserves;

        uint256 day = block.timestamp / 1 days;
        uint256 spent = _selfCareDay == day ? _selfCareSpent : 0;
        uint256 cap = selfCareDailyCap;
        // `_selfCareSpent` has 192 bits.
        if (cap > type(uint192).max) cap = type(uint192).max;
        uint256 room = spent < cap ? cap - spent : 0;
        if (add > room) add = room;

        uint256 balance = address(this).balance;
        uint256 floor = principalFloor;
        uint256 free = balance > floor ? balance - floor : 0;
        if (add > free) add = free;

        if (add == 0) return;

        try SYSTEM.depositTo{ value: add }(address(this)) {
            _selfCareDay = uint64(day);
            // No truncation: `spent + add <= cap <= type(uint192).max`.
            _selfCareSpent = uint192(spent + add);
            emit SelfCare(add);
        } catch { }
    }

    // ------------------------------------------------------------------------------------------
    // Views
    // ------------------------------------------------------------------------------------------

    function targetCount(uint256 chainId) external view returns (uint256) {
        return _targetList[chainId].length;
    }

    function targetAt(uint256 chainId, uint256 index) external view returns (address) {
        return _targetList[chainId][index];
    }

    function targetInfo(uint256 chainId, address target)
        external
        view
        returns (TargetKind kind, TargetState state, Policy memory policy)
    {
        Target storage t = _targets[chainId][target];
        return (t.kind, t.state, t.policy);
    }
}
