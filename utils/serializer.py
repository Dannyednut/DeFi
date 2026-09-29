from web3.datastructures import AttributeDict
from web3 import Web3
from hexbytes.main import HexBytes
from eth_account._utils.legacy_transactions import serializable_unsigned_transaction_from_dict, encode_transaction


def _normalise_tx(tx) -> dict:
    out = {}
    for k, v in tx.items():
        if isinstance(v, (bytes, bytearray, HexBytes)):
            out[k] = v.hex()
        else:
            out[k] = v
    return out


def _int_bytes(value: int) -> bytes:
    value = int(value)
    return b"" if value == 0 else value.to_bytes((value.bit_length() + 7) // 8, "big")


def _hex_bytes(value) -> bytes:
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray, HexBytes)):
        return bytes(value)
    text = str(value)
    return bytes.fromhex(text[2:] if text.startswith("0x") else text)


def serialize(tx):
    """Re-encode a signed RPC transaction for bundle submission.

    Supports legacy and EIP-1559/type-2 envelopes and preserves v/r/s.
    """
    n = _normalise_tx(tx)
    if not all(k in n for k in ('v', 'r', 's')):
        raise ValueError('transaction signature fields v/r/s are required')
    tx_type = n.get('type', 0)
    if isinstance(tx_type, str):
        tx_type = int(tx_type, 16) if tx_type.startswith('0x') else int(tx_type)

    if int(tx_type) == 2 or 'maxFeePerGas' in n:
        import rlp
        to = _hex_bytes(n.get('to'))
        access = []
        for item in n.get('accessList', []) or []:
            address, storage_keys = item
            access.append([_hex_bytes(address), [_hex_bytes(k) for k in storage_keys]])
        y_parity = int(n['v'])
        if y_parity in (27, 28):
            y_parity -= 27
        payload = [
            _int_bytes(int(n['chainId'])),
            _int_bytes(int(n['nonce'])),
            _int_bytes(int(n['maxPriorityFeePerGas'])),
            _int_bytes(int(n['maxFeePerGas'])),
            _int_bytes(int(n.get('gas', n.get('gasLimit', 0)))),
            to,
            _int_bytes(int(n.get('value', 0))),
            _hex_bytes(n.get('input', n.get('data', '0x'))),
            access,
            _int_bytes(y_parity),
            _int_bytes(int(n['r'])),
            _int_bytes(int(n['s'])),
        ]
        return Web3.to_hex(b'\x02' + rlp.encode(payload))

    filtered = {
        'nonce': int(n['nonce']),
        'gas': int(n.get('gas', n.get('gasLimit', 0))),
        'gasPrice': int(n.get('gasPrice', 0)),
        'to': n.get('to'),
        'value': int(n.get('value', 0)),
        'data': n.get('input', n.get('data', '0x')),
        'chainId': int(n.get('chainId', 0)),
    }
    unsigned = serializable_unsigned_transaction_from_dict(filtered)
    signed = encode_transaction(unsigned, (int(n['v']), int(n['r']), int(n['s'])))
    return Web3.to_hex(signed)
