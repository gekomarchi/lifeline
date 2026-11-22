// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

import { IReactive } from "@reactive/src/interfaces/IReactive.sol";
import { ISystemContract, REACTIVE_IGNORE } from "./interfaces/ISystemContract.sol";
import { LifelineVault } from "./LifelineVault.sol";
import { Policy, TargetKind, TargetState, validatePolicy } from "./LifelineTypes.sol";

/// @dev The gas of a vault callback for each target of the request (spec §4.4). The start value of the
///      specification was 50,000. The fork test `test_sweepFitsItsGasLimit` needs 79,986 for a sweep of one
///      target, because the vault keeps 120,000 gas before each target: 80,000 is the smallest step of 1,000
///      that passes on both proxy configurations.
uint256 constant SWEEP_PER_TARGET = 80_000;

/// @dev The most targets of one request to a vault.
uint256 constant SWEEP_MAX = 8;

/// @dev A `requestId` is `REQUEST_ID_BASE + n`, where n counts the requests of the hub from 1. The id of a
///      gauge sweep is a block number, so the two can never be equal.
uint256 constant REQUEST_ID_BASE = 1 << 128;

/// @dev The most Lasna targets that one cron tick processes (spec §4.4). The next tick continues at the cursor.
uint256 constant MAX_PER_TICK = 12;

/// @dev The gas that the loop of a tick keeps for one target (spec §4.4): the start value. The loop stops
///      when less than `ITER_RESERVE + TAIL_RESERVE` is left. Measured on the fork for one target, in gas:
///      about 5,200 if it is not active, 15,500 with nothing to pay, 39,300 for a top-up, 59,200 for the
///      first payment to a target, 76,300 if that target also has reserves of 0, and 108,300 for a rescue.
///      `TAIL_RESERVE` covers the part of a target above this value.
uint256 constant ITER_RESERVE = 60_000;

/// @dev The gas that the loop of a tick keeps for the tail (spec §4.4). The start value was 150,000. With the
///      two reserves below, the fork tests fail at 280,000 and pass at 290,000, in steps of 10,000. After a
///      first payment to an empty target (about 16,300 above `ITER_RESERVE`) the tail of two vaults still has
///      its largest need: about 3,600, then the first vault (at most about 67,100), then
///      `SWEEP_RESERVE + END_RESERVE` at the check of the second vault. A rescue takes more: after it the
///      tail can stop before the second vault, and the next tail starts there.
uint256 constant TAIL_RESERVE = 290_000;

/// @dev The gas that the tail of a tick keeps for one vault: the reads of its list, the request and its
///      cursor. Measured on the fork, in gas: about 32,100 for 3 targets, 47,000 for 8, and 67,100 for a list
///      of more than `SWEEP_MAX` targets with a first write of its cursor.
uint256 constant SWEEP_RESERVE = 70_000;

/// @dev The gas that the tail of a tick keeps for the end of the reaction. Measured on the fork, in gas:
///      about 22,400 for the first tick write and `Tick` (5,300 after that); for self-care at most 44,500,
///      and about 17,100 more if the reserves of the hub are 0; and the gas that the system contract needs
///      after `react()` to charge the fee: 8,100, or 35,400 when the float does not cover the fee. A
///      reaction that leaves less makes the whole trigger run out of gas. The fork tests of this end fail at
///      110,000 and pass at 120,000; the value keeps one more step.
uint256 constant END_RESERVE = 130_000;

/// @dev The emitter of the cron logs on Reactive Network: `SYSTEM_CONTRACT_ADDR` of `src/SystemContract.sol` in
///      `system-smart-contracts` at `72710d2`. It is not the ledger at `0x8888...8888`: the node calls
///      `triggerCron()` on this address in each block, and a cron subscription on Lasna names this emitter.
address constant CRON_EMITTER = 0x0000000000000000000000000000000000fffFfF;

/// @dev `topic0` of `Cron1000(uint256 indexed number)` and of `Cron10000(uint256 indexed number)`: the events of
///      `src/SystemContract.sol` in `system-smart-contracts` at `72710d2`. A block of Lasna is about 1 second.
uint256 constant CRON_1000_TOPIC0 = uint256(keccak256("Cron1000(uint256)"));
uint256 constant CRON_10000_TOPIC0 = uint256(keccak256("Cron10000(uint256)"));

/// @title LifelineHub: the guardian of enrolled reactive contracts on Reactive Network (Lasna).
/// @notice When the system contract reports a debt for an enrolled target, the hub clears the debt and
///         refills the target inside a policy. It counts rescues and quarantines a target that breaks.
///
///         Destination chains. When the callback proxy of a destination chain reports a debt for an
///         enrolled target, the hub requests one `reconcile` callback on the LifelineVault of that chain,
///         inside a relay limit. The vault pays and decides; the hub only forwards. When a vault itself goes
///         into debt, the hub reports it, sends no request to that vault, keeps the debt events that arrive
///         as pending, and sends catch-up sweeps after the owner's `vaultUp`.
///
///         Funds. Principal is the contract balance; the owner can withdraw it. The fee float is the
///         hub's own reserves at the system contract. The hub has no `pay()` and no fallback, so the
///         ledger can take only the float.
///
///         Gauge. On each tick of one cron subscription the hub refills the Lasna targets that are below
///         their low mark, in a batch with a gas guard and a cursor, and requests one `sweep` callback on
///         each vault that is up and has an enrolled target. A target normally does not go into debt; the
///         reactions to debt logs stay the backstop.
///
///         The contract is not upgradeable. The storage layout, the `react()` dispatch and the external
///         signatures of `enroll` and `disarm` are the ones of the full design.
/// @dev The hub does not inherit a library contract, because the library base contracts add `pay()`.
///      It uses the log record type of `reactive-lib-omni` and matches the `react` selector.
///
///      It imports LifelineVault only to encode the two callback payloads; it holds no vault code.
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
        Unhandled, // the event kind has no handler in this unit
        AlreadyPending, // the vault is down and the target is in the pending set already
        RequestFailed, // the system contract did not accept the callback request
        DuplicateTick // the origin block of the cron log is not above the last tick, or it has more than 192 bits
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

    /// @notice The record of the vault of one destination chain. `setVault` fills it.
    struct Vault {
        address proxy; // the callback proxy of that chain: the emitter of its debt logs
        address vault; // the LifelineVault of that chain
        uint64 sweepBase; // the base gas of a sweep callback on that proxy
        bool up; // false while the vault is in debt at its proxy
    }

    /// @notice The state of the gauge: one storage slot, which a tick reads one time and writes one time.
    struct Gauge {
        uint192 lastTick; // the `tickId` of the last tick that ran: the origin block of its cron log
        uint32 fleetCursor; // the position in the Lasna list of the first target of the next tick
        uint32 tailCursor; // the position in `_vaultChains` of the first vault of the next tail
    }

    /// @notice The relay limit and the relay counter of one destination target: one storage slot.
    struct Relay {
        uint64 windowStart; // the time of the first relay of the current window
        uint32 count; // relays in the current window; 0 = no window
        uint32 maxRelays; // `2 * maxRescues + 2` of the policy
        uint32 relayWindow; // `window` of the policy, seconds
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
    event VaultSet(uint256 indexed chainId, address proxy, address vault, uint256 sweepBase);
    event Relayed(uint256 indexed requestId, uint256 indexed chainId, address indexed target);
    event RelayLimited(uint256 indexed chainId, address indexed target);
    event Rearmed(uint256 indexed chainId, address indexed target);
    event VaultDown(uint256 indexed chainId);
    event Pending(uint256 indexed chainId, address indexed target);
    event VaultUp(uint256 indexed chainId, uint256 indexed requestId, uint256 sent, uint256 remaining);
    event Tick(uint256 indexed tickId, uint256 processed, uint256 cursor, uint256 sweeps);

    error UnknownChain(uint256 chainId);
    error NotOwner();
    error NotSystem(address caller);
    error InvalidTarget(address target);
    error AlreadyEnrolled(uint256 chainId, address target);
    error NotEnrolled(uint256 chainId, address target);
    error NotQuarantined(address target);
    error InsufficientValue(uint256 value, uint256 feeFloat);
    error WithdrawFailed();
    error InvalidVault(uint256 chainId, address proxy, address vault, uint256 sweepBase);
    error VaultInUse(uint256 chainId);
    error NotParked(uint256 chainId, address target);
    error RequestFailed(uint256 chainId);
    error InvalidCronTopic(uint256 topic0);

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

    /// @dev The largest `sweepBase`: the gas limit of the largest request fits the 64 bits of the request.
    uint256 internal constant MAX_SWEEP_BASE = type(uint64).max - SWEEP_PER_TARGET * SWEEP_MAX;

    /// @dev The destination chain IDs that have a vault record, in the order of their first `setVault`.
    uint256[] internal _vaultChains;

    mapping(uint256 chainId => mapping(address target => Relay)) internal _relays;

    /// @dev The pending set of each destination chain: the active targets whose debt log came while the
    ///      vault was down. Each entry is an enrolled target of that chain, one time.
    mapping(uint256 chainId => address[]) internal _pending;

    /// @dev The position of a target in `_pending[chainId]` plus 1; 0 = not pending.
    mapping(uint256 chainId => mapping(address target => uint256)) internal _pendingIndex;

    /// @dev The number of requests that the hub sent to vaults.
    uint256 internal _requestCount;

    /// @dev The cron `topic0` that the owner selected. 0 = no selection: the default, Cron10000.
    uint256 internal _cronTopic;

    /// @dev The last tick and the two cursors of the gauge.
    Gauge internal _gauge;

    /// @dev The position in the target list of a destination chain of the first target of its next sweep.
    mapping(uint256 chainId => uint256) internal _sweepCursor;

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
    ///
    ///         A destination target needs a registered vault on its chain. The hub subscribes with the exact
    ///         `topic1` at the proxy of that chain and sends no callback: the vault reconciles locally. Of
    ///         the policy the hub keeps `maxRescues` and `window` only; the vault owns the marks and the
    ///         caps. The relay limit is `2 * maxRescues + 2` requests inside `window`.
    function enroll(uint256 chainId, address target, Policy calldata policy) external onlyOwner {
        if (chainId == block.chainid) {
            _addTarget(chainId, target, TargetKind.Lasna, policy);
            _subscribe(TupleKind.TargetDebt, chainId, address(SYSTEM), BLACKLIST_TOPIC0, uint256(uint160(target)));
            emit Enrolled(chainId, target);
            _reconcileLasna(target);
        } else {
            Vault storage v = _vaults[chainId];
            address proxy = v.proxy;
            if (proxy == address(0)) revert UnknownChain(chainId);
            // The debt log of the vault has its own handler and its own tuple.
            if (target == v.vault) revert InvalidTarget(target);

            _addTarget(chainId, target, TargetKind.Destination, policy);
            Policy storage stored = _targets[chainId][target].policy;
            stored.low = 0;
            stored.high = 0;
            stored.maxDebt = 0;
            stored.dailyCap = 0;
            // No overflow: `validatePolicy` keeps `maxRescues` in 1..4.
            _relays[chainId][target] =
                Relay({ windowStart: 0, count: 0, maxRelays: 2 * policy.maxRescues + 2, relayWindow: policy.window });

            _subscribe(TupleKind.TargetDebt, chainId, proxy, BLACKLIST_TOPIC0, uint256(uint160(target)));
            emit Enrolled(chainId, target);
        }
    }

    /// @notice The intentional stop of a target. `enroll` is permitted again after it. A destination target
    ///         leaves the pending set of its chain.
    function disarm(uint256 chainId, address target) external onlyOwner {
        if (chainId == block.chainid) {
            _removeTarget(chainId, target);
            _unsubscribe(TupleKind.TargetDebt, chainId, address(SYSTEM), BLACKLIST_TOPIC0, uint256(uint160(target)));
            emit Disarmed(chainId, target);
        } else {
            address proxy = _vaults[chainId].proxy;
            if (proxy == address(0)) revert UnknownChain(chainId);

            _removeTarget(chainId, target);
            _removePending(chainId, target);
            // A parked target has no tuple: the registry ignores it.
            _unsubscribe(TupleKind.TargetDebt, chainId, proxy, BLACKLIST_TOPIC0, uint256(uint160(target)));
            emit Disarmed(chainId, target);
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
    ///         There is no bulk restore: a Lasna target is restored with `disarm` and `enroll`, a destination
    ///         target with `rearm`, and the debt tuple of a vault with `setVault`.
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

        _dropTuple(key);
    }

    /// @dev Unsubscribes each tuple of one kind.
    function _unsubscribeKind(TupleKind kind) internal {
        bytes32[] storage keys = _tupleKeys[kind];
        for (uint256 i = keys.length; i != 0; --i) {
            bytes32 key = keys[i - 1];
            keys.pop();
            _dropTuple(key);
        }
    }

    /// @dev Deletes the stored tuple and calls `unsubscribe` with the six stored fields.
    function _dropTuple(bytes32 key) internal {
        Tuple memory tuple = _tuples[key];
        delete _tuples[key];
        SYSTEM.unsubscribe(tuple.chainId, tuple.emitter, tuple.topic0, tuple.topic1, tuple.topic2, tuple.topic3);
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
        SkipReason reason;

        if (chainId == block.chainid) {
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

    /// @dev Handler: `BlacklistContract(target)` of a registered destination proxy. The hub does not pay and
    ///      has no quarantine state for this target: it requests one `reconcile` on the vault of that chain,
    ///      inside the relay limit. While the vault is down it stores the target as pending and sends nothing.
    function _onDestinationTargetDebt(IReactive.LogRecord calldata log) internal {
        uint256 chainId = log.chainId;
        address target = address(uint160(log.topic1));
        Vault storage v = _vaults[chainId];
        Target storage t = _targets[chainId][target];
        TargetState state = t.state;
        SkipReason reason;

        if (target == v.vault || t.kind != TargetKind.Destination || state == TargetState.Disarmed) {
            reason = SkipReason.NotEnrolled;
        } else if (state != TargetState.Active) {
            reason = SkipReason.NotActive;
        } else if (!v.up) {
            // Not a relay: the limit counts requests, and no request goes to a vault that is down.
            if (_addPending(chainId, target)) {
                emit Pending(chainId, target);
                return;
            }
            reason = SkipReason.AlreadyPending;
        } else {
            Relay memory relay = _relays[chainId][target];
            // A relay at or after the end of the window starts a new window.
            if (relay.count == 0 || block.timestamp >= uint256(relay.windowStart) + relay.relayWindow) {
                relay.windowStart = uint64(block.timestamp);
                relay.count = 0;
            }
            if (relay.count >= relay.maxRelays) {
                // The limit protects the fee float: the hub stops listening to this one target.
                t.state = TargetState.Parked;
                emit RelayLimited(chainId, target);
                _unsubscribe(TupleKind.TargetDebt, chainId, v.proxy, BLACKLIST_TOPIC0, uint256(uint160(target)));
                return;
            }

            uint256 requestId = REQUEST_ID_BASE + _requestCount + 1;
            // The node overwrites the first argument with the address of the hub.
            if (_requestVault(chainId, abi.encodeCall(LifelineVault.reconcile, (address(0), target, requestId)), 1)) {
                ++relay.count;
                _relays[chainId][target] = relay;
                ++_requestCount;
                emit Relayed(requestId, chainId, target);
                return;
            }
            reason = SkipReason.RequestFailed;
        }

        emit Skipped(chainId, target, reason);
    }

    /// @dev Handler: `BlacklistContract(vault)` of a registered destination proxy. A vault in debt can run no
    ///      callback, so the hub sends no request to it until the owner's `vaultUp`.
    function _onVaultDebt(IReactive.LogRecord calldata log) internal {
        uint256 chainId = log.chainId;
        _vaults[chainId].up = false;
        emit VaultDown(chainId);
    }

    /// @dev Handler: the cron tick, the gauge. The dispatch gives it only a log of the registered cron tuple:
    ///      Lasna, the cron emitter and the selected `topic0`. The origin block of the log is the `tickId`. A
    ///      tick runs only if its `tickId` is above the last one, so a log that the node sends two times runs
    ///      one time.
    ///
    ///      Lasna batch: at most `MAX_PER_TICK` targets from the cursor, each through the reconcile path of
    ///      the airbag. A target that is not active costs one storage read and gives no event. The loop stops
    ///      while the gas still covers one target and the tail, so the cursor moves only past targets that
    ///      were processed. The tail sends the sweeps. The tick writes come last. It never reverts.
    function _onCron(IReactive.LogRecord calldata log) internal {
        uint256 tickId = log.blockNumber;
        Gauge memory gauge = _gauge;
        // A block number of Lasna has far fewer than 192 bits. A larger value is never stored.
        if (tickId <= gauge.lastTick || tickId > type(uint192).max) {
            emit Skipped(log.chainId, address(uint160(log.topic1)), SkipReason.DuplicateTick);
            return;
        }

        address[] storage fleet = _targetList[block.chainid];
        uint256 size = fleet.length;
        uint256 cursor = gauge.fleetCursor;
        // `disarm` can make the list shorter than the cursor.
        if (cursor >= size) cursor = 0;

        uint256 processed;
        while (processed < size && processed < MAX_PER_TICK) {
            // Stop early, never run out. The next tick continues at the cursor.
            if (gasleft() < ITER_RESERVE + TAIL_RESERVE) break;
            _reconcileLasna(fleet[cursor]);
            if (++cursor == size) cursor = 0;
            ++processed;
        }

        (uint256 sweeps, uint256 tailCursor) = _sweepVaults(tickId, gauge.tailCursor);

        // No truncation: `tickId` has at most 192 bits, and a cursor is below the length of its list.
        _gauge = Gauge({ lastTick: uint192(tickId), fleetCursor: uint32(cursor), tailCursor: uint32(tailCursor) });
        emit Tick(tickId, processed, cursor, sweeps);
    }

    /// @dev The tail of a tick: one sweep request for each vault that is up and has an enrolled target, with
    ///      the next targets from the cursor of that vault, at most `SWEEP_MAX`. The list of a chain holds each
    ///      enrolled target until `disarm`, whatever its state: the vault decides. A vault with an empty list
    ///      or in state down gets no request. A cursor moves only when the request was sent.
    ///
    ///      The tail stops when the gas does not cover one more vault and the end of the reaction. The next
    ///      tail then starts at the vault that got no request. It never reverts.
    /// @param index The position in `_vaultChains` of the first vault of this tail. A vault chain is never
    ///        removed, so the position is below the length.
    /// @return sweeps The number of requests that were sent.
    /// @return tailCursor The position in `_vaultChains` of the first vault of the next tail.
    function _sweepVaults(uint256 tickId, uint256 index) internal returns (uint256 sweeps, uint256 tailCursor) {
        uint256 chains = _vaultChains.length;
        for (uint256 seen = 0; seen < chains; ++seen) {
            // Stop early, never run out: the vault at `index` is the first one of the next tail.
            if (gasleft() < SWEEP_RESERVE + END_RESERVE) break;

            uint256 chainId = _vaultChains[index];
            if (++index == chains) index = 0;

            address[] storage list = _targetList[chainId];
            uint256 total = list.length;
            if (total == 0 || !_vaults[chainId].up) continue;

            uint256 first = _sweepCursor[chainId];
            // `disarm` can make the list shorter than the cursor.
            if (first >= total) first = 0;

            uint256 n = total < SWEEP_MAX ? total : SWEEP_MAX;
            address[] memory targets = new address[](n);
            uint256 next = first;
            for (uint256 i = 0; i < n; ++i) {
                targets[i] = list[next];
                if (++next == total) next = 0;
            }

            if (_requestSweep(chainId, targets, tickId)) {
                ++sweeps;
                // A list of at most `SWEEP_MAX` targets goes out whole: its cursor does not move.
                if (next != first) _sweepCursor[chainId] = next;
            }
        }
        // After a tail that goes over each vault, `index` is at its start value again.
        tailCursor = index;
    }

    // ------------------------------------------------------------------------------------------
    // Cron
    // ------------------------------------------------------------------------------------------

    /// @notice Selects the cron period of the gauge: `topic0` of Cron1000 or of Cron10000. If a cron tuple is
    ///         subscribed, the new tuple replaces it. If not, the selection takes effect at `resumeCron`.
    function setCronTopic(uint256 topic0) external onlyOwner {
        if (topic0 != CRON_1000_TOPIC0 && topic0 != CRON_10000_TOPIC0) revert InvalidCronTopic(topic0);
        if (topic0 == cronTopic()) return;

        _cronTopic = topic0;
        if (cronSubscribed()) {
            // The cron kind holds one tuple at most.
            _unsubscribeKind(TupleKind.Cron);
            _subscribeCron(topic0);
        }
    }

    /// @notice Subscribes the cron tuple of the selected topic if no cron tuple is subscribed: the start of
    ///         the gauge at deployment, and its restart after `HubLow` or `unsubscribeAll`.
    function resumeCron() external onlyOwner {
        if (!cronSubscribed()) _subscribeCron(cronTopic());
    }

    /// @notice The selected cron `topic0`. Before the first `setCronTopic` it is the one of Cron10000.
    function cronTopic() public view returns (uint256) {
        uint256 topic0 = _cronTopic;
        return topic0 == 0 ? CRON_10000_TOPIC0 : topic0;
    }

    /// @notice The `tickId` of the last cron tick that ran: the origin block of its cron log. 0 = no tick ran.
    function lastTick() external view returns (uint256) {
        return _gauge.lastTick;
    }

    /// @notice True while the cron tuple is subscribed. `HubLow` and `unsubscribeAll` end it.
    function cronSubscribed() public view returns (bool) {
        return _tupleKeys[TupleKind.Cron].length != 0;
    }

    /// @dev The cron tuple: Lasna, the cron emitter, the cron `topic0`, each block number.
    function _subscribeCron(uint256 topic0) internal {
        _subscribe(TupleKind.Cron, block.chainid, CRON_EMITTER, topic0, REACTIVE_IGNORE);
    }

    // ------------------------------------------------------------------------------------------
    // Vaults
    // ------------------------------------------------------------------------------------------

    /// @notice Registers the proxy, the vault and the base gas of a destination chain, and subscribes to the
    ///         debt log of the vault at that proxy. A new pair starts in state up.
    ///
    ///         A second call with the same proxy and vault updates `sweepBase`, restores the debt tuple of
    ///         the vault if it is absent, and keeps the up or down state: only `vaultUp` ends a down state.
    ///         A different proxy or vault is accepted only while no target is enrolled on that chain.
    /// @param sweepBase `90,000 + _extraGas + _maxChargeGas` of that proxy.
    function setVault(uint256 chainId, address proxy, address vault, uint256 sweepBase) external onlyOwner {
        // Chain ID 0 is the wildcard of `subscribe`. Lasna has no vault.
        if (
            chainId == block.chainid || chainId == 0 || proxy == address(0) || vault == address(0)
                || sweepBase > MAX_SWEEP_BASE
        ) revert InvalidVault(chainId, proxy, vault, sweepBase);

        Vault storage v = _vaults[chainId];
        address oldProxy = v.proxy;
        address oldVault = v.vault;
        if (oldProxy != proxy || oldVault != vault) {
            if (oldProxy == address(0)) {
                _vaultChains.push(chainId);
            } else {
                // No target is enrolled, so the pending set of this chain is empty.
                if (_targetList[chainId].length != 0) revert VaultInUse(chainId);
                _unsubscribe(TupleKind.VaultDebt, chainId, oldProxy, BLACKLIST_TOPIC0, uint256(uint160(oldVault)));
            }
            v.proxy = proxy;
            v.vault = vault;
            v.up = true;
        }
        // No truncation: `sweepBase <= MAX_SWEEP_BASE`.
        v.sweepBase = uint64(sweepBase);

        _subscribe(TupleKind.VaultDebt, chainId, proxy, BLACKLIST_TOPIC0, uint256(uint160(vault)));
        emit VaultSet(chainId, proxy, vault, sweepBase);
    }

    /// @notice Restores a parked destination target: clears its relay count and subscribes again. It sends
    ///         no callback.
    function rearm(uint256 chainId, address target) external onlyOwner {
        address proxy = _vaults[chainId].proxy;
        if (proxy == address(0)) revert UnknownChain(chainId);

        Target storage t = _targets[chainId][target];
        if (t.kind != TargetKind.Destination || t.state != TargetState.Parked) revert NotParked(chainId, target);

        t.state = TargetState.Active;
        _relays[chainId][target].count = 0;
        _subscribe(TupleKind.TargetDebt, chainId, proxy, BLACKLIST_TOPIC0, uint256(uint160(target)));
        emit Rearmed(chainId, target);
    }

    /// @notice Sets the vault state up and sends one catch-up sweep with at most `SWEEP_MAX` pending targets.
    ///         Call it again until `remaining` is 0. With no pending target it sends no request and emits
    ///         `VaultUp(chainId, 0, 0, 0)`. A catch-up sweep counts no relay.
    /// @dev The hub cannot read the destination ledger: call this after the debt of the vault is cleared. A
    ///      request to a vault in debt is not delivered, and its targets are not pending any more.
    function vaultUp(uint256 chainId) external onlyOwner {
        Vault storage v = _vaults[chainId];
        if (v.proxy == address(0)) revert UnknownChain(chainId);
        v.up = true;

        address[] storage pending = _pending[chainId];
        uint256 total = pending.length;
        uint256 sent = total < SWEEP_MAX ? total : SWEEP_MAX;
        uint256 requestId;
        if (sent != 0) {
            // The targets come from the end of the set.
            address[] memory targets = new address[](sent);
            for (uint256 i = 0; i < sent; ++i) {
                address target = pending[total - 1 - i];
                pending.pop();
                delete _pendingIndex[chainId][target];
                targets[i] = target;
            }
            requestId = REQUEST_ID_BASE + ++_requestCount;
            if (!_requestSweep(chainId, targets, requestId)) revert RequestFailed(chainId);
        }
        emit VaultUp(chainId, requestId, sent, total - sent);
    }

    /// @dev The one request function of the hub: the only place that calls `requestCallbackV_1_0`. It sends
    ///      nothing if the vault is not registered or not up, or if `n` is 0 or above `SWEEP_MAX`. The proxy
    ///      charges the gas used, not the limit. It never reverts.
    /// @param payload The callback of the vault. The node overwrites its first argument.
    /// @param n The number of targets of the callback.
    /// @return sent True if the system contract accepted the request.
    function _requestVault(uint256 chainId, bytes memory payload, uint256 n) internal returns (bool sent) {
        Vault storage v = _vaults[chainId];
        address vault = v.vault;
        if (vault == address(0) || !v.up || n == 0 || n > SWEEP_MAX) return false;

        // No truncation: `sweepBase <= MAX_SWEEP_BASE` and `n <= SWEEP_MAX`.
        uint64 gasLimit = uint64(uint256(v.sweepBase) + SWEEP_PER_TARGET * n);
        try SYSTEM.requestCallbackV_1_0(
            ISystemContract.CallbackConfiguration_V_1_0({
                chainId: chainId, recipient: vault, gasLimit: gasLimit, payload: payload
            })
        ) {
            return true;
        } catch { }
    }

    /// @dev Requests `sweep(address(0), targets, id)` on the vault of a chain. It never reverts.
    function _requestSweep(uint256 chainId, address[] memory targets, uint256 id) internal returns (bool sent) {
        return _requestVault(chainId, abi.encodeCall(LifelineVault.sweep, (address(0), targets, id)), targets.length);
    }

    /// @dev Adds a target to the pending set of its chain.
    /// @return added False if the target is in the set already.
    function _addPending(uint256 chainId, address target) internal returns (bool added) {
        if (_pendingIndex[chainId][target] != 0) return false;

        address[] storage pending = _pending[chainId];
        pending.push(target);
        _pendingIndex[chainId][target] = pending.length;
        return true;
    }

    /// @dev Removes a target from the pending set of its chain. A target that is not in the set is ignored.
    function _removePending(uint256 chainId, address target) internal {
        uint256 index = _pendingIndex[chainId][target];
        if (index == 0) return;

        address[] storage pending = _pending[chainId];
        uint256 last = pending.length;
        if (index != last) {
            address moved = pending[last - 1];
            pending[index - 1] = moved;
            _pendingIndex[chainId][moved] = index;
        }
        pending.pop();
        delete _pendingIndex[chainId][target];
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
        try SYSTEM.debt(target) returns (uint256 debt_) {
            try SYSTEM.reserves(target) returns (uint256 reserves_) {
                return (true, debt_, reserves_);
            } catch { }
        } catch { }
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

    /// @notice The vault record of a destination chain and the size of its pending set. All values are 0 if
    ///         the chain has no vault.
    function vaultInfo(uint256 chainId)
        external
        view
        returns (address proxy, address vault, uint256 sweepBase, bool up, uint256 pending)
    {
        Vault storage v = _vaults[chainId];
        return (v.proxy, v.vault, v.sweepBase, v.up, _pending[chainId].length);
    }

    function vaultChainCount() external view returns (uint256) {
        return _vaultChains.length;
    }

    function vaultChainAt(uint256 index) external view returns (uint256 chainId) {
        return _vaultChains[index];
    }
}
