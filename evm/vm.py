import contextlib
import functools
import io
import itertools
import logging

from toolz import (
    partial,
)

from eth_utils import (
    to_normalized_address,
)

from evm import opcodes
from evm.defaults.gas_costs import (
    memory_gas_cost,
    sstore_gas_cost,
    call_gas_cost,
    OPCODE_GAS_COSTS,
)
from evm.defaults.opcodes import (
    OPCODE_LOGIC_FUNCTIONS,
)
from evm.logic.invalid import (
    invalid_op,
)
from evm.constants import (
    NULL_BYTE,
)
from evm.exceptions import (
    ValidationError,
    VMError,
    OutOfGas,
    InsufficientStack,
    FullStack,
    StackDepthLimit,
)
from evm.validation import (
    validate_canonical_address,
    validate_is_bytes,
    validate_length,
    validate_lte,
    validate_gte,
    validate_uint256,
    validate_word,
    validate_opcode,
    validate_integer,
)

from evm.utils.numeric import (
    ceil32,
    int_to_big_endian,
)


class Memory(object):
    """
    EVM Memory
    """
    bytes = None

    def __init__(self):
        self.bytes = bytearray()

    def extend(self, start_position, size):
        if size == 0:
            return

        new_size = ceil32(start_position + size)
        if new_size <= len(self):
            return

        size_to_extend = new_size - len(self)
        self.bytes.extend(itertools.repeat(0, size_to_extend))

    def __len__(self):
        return len(self.bytes)

    def write(self, start_position, size, value):
        """
        Write `value` into memory.
        """
        validate_uint256(start_position)
        validate_uint256(size)
        validate_is_bytes(value)
        validate_length(value, length=size)
        validate_lte(start_position + size, maximum=len(self))

        self.bytes = (
            self.bytes[:start_position] +
            bytearray(value) +
            self.bytes[start_position + size:]
        )

    def read(self, start_position, size):
        """
        Read a value from memory.
        """
        return self.bytes[start_position:start_position + size]


class Stack(object):
    """
    EVM Stack
    """
    values = None
    logger = logging.getLogger('evm.vm.Stack')

    def __init__(self):
        self.values = []

    def __len__(self):
        return len(self.values)

    def push(self, value):
        """
        Push an item onto the stack.
        """
        if len(self.values) + 1 > 1024:
            raise FullStack('Stack limit reached')

        validate_is_bytes(value)
        validate_lte(len(value), maximum=32)

        self.values.append(value)

    def pop(self):
        """
        Pop an item off thes stack.
        """
        if not self.values:
            raise InsufficientStack('Popping from empty stack')

        value = self.values.pop()

        return value

    def swap(self, position):
        """
        Perform a SWAP operation on the stack.
        """
        idx = -1 * position - 1
        try:
            self.values[-1], self.values[idx] = self.values[idx], self.values[-1]
        except IndexError:
            raise InsufficientStack("Insufficient stack items for SWAP{0}".format(position))

    def dup(self, position):
        """
        Perform a DUP operation on the stack.
        """
        idx = -1 * position
        try:
            self.push(self.values[idx])
        except IndexError:
            raise InsufficientStack("Insufficient stack items for DUP{0}".format(position))


class Message(object):
    """
    A message for EVM computation.
    """
    origin = None
    account = None
    sender = None
    value = None
    data = None
    gas = None
    gas_price = None

    depth = None

    def __init__(self, gas, gas_price, origin, account, sender, value, data, depth=0):
        validate_uint256(gas)
        self.gas = gas

        validate_uint256(gas_price)
        self.gas_price = gas_price

        validate_canonical_address(origin)
        self.origin = origin

        validate_canonical_address(account)
        self.account = account

        validate_canonical_address(sender)
        self.sender = sender

        validate_uint256(value)
        self.value = value

        validate_is_bytes(data)
        self.data = data

        validate_integer(depth)
        validate_gte(depth, minimum=0)
        self.depth = depth


class CodeStream(object):
    stream = None

    logger = logging.getLogger('evm.vm.CodeStream')

    def __init__(self, code_bytes):
        validate_is_bytes(code_bytes)
        self.stream = io.BytesIO(code_bytes)

    def read(self, size):
        value = self.stream.read(size)
        return value

    def __len__(self):
        return len(self.stream.getvalue())

    def __iter__(self):
        return self

    def __next__(self):
        return self.next()

    def next(self):
        next_opcode_as_byte = self.read(1)

        if len(next_opcode_as_byte) == 1:
            next_opcode = ord(next_opcode_as_byte)
        else:
            next_opcode = opcodes.STOP

        return next_opcode

    def peek(self):
        current_pc = self.pc
        next_opcode = next(self)
        self.pc = current_pc
        return next_opcode

    @property
    def pc(self):
        return self.stream.tell()

    @pc.setter
    def pc(self, value):
        self.stream.seek(min(value, len(self)))

    @contextlib.contextmanager
    def seek(self, pc):
        anchor_pc = self.pc
        self.pc = pc
        try:
            yield self
        except:
            raise
        finally:
            self.pc = anchor_pc

    def is_valid_opcode(self, position):
        with self.seek(max(0, position - 32)):
            prefix = self.read(min(position, 32))

        for offset, opcode in enumerate(reversed(prefix)):
            if opcode < opcodes.PUSH1 or opcode > opcodes.PUSH32:
                continue

            push_size = 1 + opcode - opcodes.PUSH1
            if push_size <= offset:
                continue

            opcode_position = position - 1 - offset
            if not self.is_valid_opcode(opcode_position):
                continue

            return False
        else:
            return True


class GasMeter(object):
    start_gas = None

    deductions = None
    refunds = None

    logger = logging.getLogger('evm.vm.GasMeter')

    def __init__(self, start_gas):
        validate_uint256(start_gas)

        self.deductions = []
        self.returns = []
        self.refunds = []
        self.start_gas = start_gas

    #
    # Write API
    #
    def consume_gas(self, amount):
        try:
            validate_uint256(amount)
        except ValidationError:
            raise OutOfGas("Gas amount exceeds 256 integer size: {0}".format(amount))

        if self.is_out_of_gas:
            raise OutOfGas("Failed to consume {0} gas.  Already out of gas: {1}".format(
                amount,
                self.gas_remaining,
            ))

        before_value = self.gas_remaining
        self.deductions.append(amount)

        self.logger.debug(
            'GAS CONSUMPTION: %s - %s -> %s',
            before_value,
            amount,
            self.gas_remaining,
        )

    def return_gas(self, amount):
        validate_uint256(amount)

        before_value = self.gas_remaining
        self.returns.append(amount)

        self.logger.info(
            'GAS RETURNED: %s + %s -> %s',
            before_value,
            amount,
            self.gas_remaining,
        )

    def refund_gas(self, amount):
        validate_uint256(amount)

        before_value = self.gas_refunded
        self.refunds.append(amount)

        self.logger.info(
            'GAS REFUND: %s + %s -> %s',
            before_value,
            amount,
            self.gas_refunded,
        )


    #
    # Read API
    #
    @property
    def gas_used(self):
        return sum(self.deductions) + sum(self.returns)

    @property
    def gas_refunded(self):
        return sum(self.refunds)

    @property
    def gas_remaining(self):
        return self.start_gas - self.gas_used

    @property
    def is_out_of_gas(self):
        return self.gas_remaining < 0

    def wrap_opcode_fn(self, opcode, opcode_fn, gas_cost):
        opcode_mnemonic = opcodes.MNEMONICS[opcode]

        @functools.wraps(opcode_fn)
        def inner(*args, **kwargs):
            self.consume_gas(gas_cost)
            if self.is_out_of_gas:
                raise OutOfGas("Insufficient gas for opcode 0x{0:x}".format(
                    opcode,
                ))
            return opcode_fn(*args, **kwargs)
        return inner


class ChainEnvironment(object):
    block_number = None
    gas_limit = None
    timestamp = None

    logger = logging.getLogger('evm.vm.ChainEnvironment')

    def __init__(self, block_number, gas_limit, timestamp):
        validate_uint256(block_number)
        self.block_number = block_number

        validate_uint256(gas_limit)
        self.gas_limit = gas_limit

        validate_uint256(timestamp)
        self.timestamp = timestamp


class State(object):
    """
    The local computation state during EVM execution.
    """
    memory = None
    stack = None
    gas_meter = None
    code = None

    logger = logging.getLogger('evm.vm.State')

    def __init__(self, code, start_gas):
        self.memory = Memory()
        self.stack = Stack()

        validate_is_bytes(code)
        self.code = CodeStream(code)

        self.gas_meter = GasMeter(start_gas)

    #
    # Write API
    #
    def extend_memory(self, start_position, size):
        validate_uint256(start_position)
        validate_uint256(size)

        before_size = ceil32(len(self.memory))
        after_size = ceil32(start_position + size)

        before_cost = memory_gas_cost(before_size)
        after_cost = memory_gas_cost(after_size)

        self.logger.debug(
            "MEMORY: size (%s -> %s) | cost (%s -> %s)",
            before_size,
            after_size,
            before_cost,
            after_cost,
        )

        if size:
            if before_cost < after_cost:
                gas_fee = after_cost - before_cost
                self.gas_meter.consume_gas(gas_fee)

            if self.gas_meter.is_out_of_gas:
                raise OutOfGas("Ran out of gas extending memory")

            self.memory.extend(start_position, size)


class ExecutionEnvironment(object):
    """
    The execution environment
    """
    evm = None
    message = None
    state = None

    sub_environments = None

    output = b''
    error = None

    logs = None
    accounts_to_delete = None

    logger = logging.getLogger('evm.vm.ExecutionEnvironment')

    def __init__(self, evm, chain_environment, message, parent=None):
        self.evm = evm
        self.chain_environment = chain_environment
        self.message = message

        self.sub_environments = []
        self.accounts_to_delete = {}
        self.logs = []

        account_code = self.storage.get_code(message.account)
        self.state = State(
            code=account_code,
            start_gas=message.gas,
        )

    @property
    def storage(self):
        return self.evm.storage

    def execute(self):
        return execute_vm(self.evm, self)

    def apply_message(self, message):
        sub_environment = apply_message(self.evm, message)
        self.sub_environments.append(sub_environment)
        return sub_environment

    def create_message(self, gas, to, value, data):
        message = Message(
            gas=gas,
            gas_price=self.message.gas_price,
            origin=self.message.origin,
            account=to,
            sender=self.message.account,
            value=value,
            data=data,
            depth=self.message.depth + 1,
        )
        return message

    #
    # Opcode Functions
    #
    def get_opcode_fn(self, opcode):
        try:
            validate_opcode(opcode)
        except ValidationError:
            opcode_fn = partial(invalid_op, opcode=opcode)
        else:
            base_opcode_fn = self.evm.get_base_opcode_fn(opcode)
            opcode_gas_cost = self.evm.get_opcode_gas_cost(opcode)
            opcode_fn = self.state.gas_meter.wrap_opcode_fn(
                opcode=opcode,
                opcode_fn=base_opcode_fn,
                gas_cost=opcode_gas_cost,
            )

        return opcode_fn

    #
    # Runtime Operations
    #
    def register_account_for_deletion(self, beneficiary):
        validate_canonical_address(beneficiary)

        if self.message.account in self.accounts_to_delete:
            raise ValueError(
                "Invariant.  Should be impossible for an account to be "
                "registered for deletion multiple times"
            )
        self.accounts_to_delete[self.message.account] = beneficiary

    def add_log_entry(self, account, topics, data):
        self.logs.append((account, topics, data))

    #
    # Context Manager API
    #
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type and issubclass(exc_type, VMError):
            self.error = exc_value
            # suppress VM exceptions
            return True
        elif exc_type is None:
            for account, beneficiary in self.accounts_to_delete.items():
                self.logger.info('DELETING ACCOUNT: %s', to_normalized_address(account))
                self.storage.delete_storage(account)
                self.storage.delete_code(account)

                account_balance = self.storage.get_balance(account)
                self.storage.set_balance(account, 0)

                beneficiary_balance = self.storage.get_balance(beneficiary)
                beneficiary_updated_balance = beneficiary_balance + account_balance
                self.storage.set_balance(beneficiary, beneficiary_updated_balance)


BREAK_OPCODES = {
    opcodes.RETURN,
    opcodes.STOP,
    opcodes.SUICIDE,
}


def apply_message(evm, message):
    snapshot = evm.snapshot()

    if message.depth >= 1024:
        raise StackDepthLimit("Stack depth limit reached")
    if message.value:
        sender_balance = evm.storage.get_balance(message.sender)

        if sender_balance < message.value:
            raise InsufficientFunds(
                "Insufficient funds: {0} < {1}".format(sender_balance, message.value)
            )

        account_balance = evm.storage.get_balance(message.account)

        sender_balance -= message.value
        account_balance += message.value

        evm.storage.set_balance(message.sender, sender_balance)
        evm.storage.set_balance(message.account, account_balance)

    environment = evm.setup_environment(message)
    result_environment = execute_vm(evm, environment)

    if result_environment.error:
        evm.revert(snapshot)
    return result_environment


def execute_vm(evm, environment):
    with environment:
        logger = environment.logger
        logger.debug(
            "EXECUTING: gas: %s | from: %s | to: %s | value: %s",
            environment.message.gas,
            environment.message.sender,
            environment.message.account,
            environment.message.value,
        )

        for opcode in environment.state.code:
            opcode_mnemonic = opcodes.get_mnemonic(opcode)
            logger.debug("OPCODE: 0x%x (%s)", opcode, opcode_mnemonic)

            opcode_fn = environment.get_opcode_fn(opcode)

            try:
                opcode_fn(environment=environment)
            except VMError as err:
                environment.error = err
                break

            if opcode in BREAK_OPCODES:
                break

    return environment


class EVM(object):
    storage = None
    chain_environment = None

    def __init__(self, storage, chain_environment):
        self.storage = storage
        self.chain_environment = chain_environment

    def setup_environment(self, message):
        environment = ExecutionEnvironment(
            evm=self,
            chain_environment=self.chain_environment,
            message=message,
        )
        return environment

    #
    # Snapshot and Revert
    #
    def snapshot(self):
        return self.storage.snapshot()

    def revert(self, snapshot):
        self.storage.revert(snapshot)

    #
    # Opcode API
    #
    def get_base_opcode_fn(self, opcode):
        base_opcode_fn = OPCODE_LOGIC_FUNCTIONS[opcode]
        return base_opcode_fn

    def get_opcode_gas_cost(self, opcode):
        opcode_gas_cost = OPCODE_GAS_COSTS[opcode]
        return opcode_gas_cost

    def get_sstore_gas_fn(self):
        return sstore_gas_cost

    def get_call_gas_fn(self):
        return call_gas_cost

    execute = execute_vm
