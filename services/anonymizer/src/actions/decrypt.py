from utils.fhirpath import error, find_nodes
from utils.crypto import rsa_decrypt
import json

supported_enc_schemes = {
    'RSA': rsa_decrypt
}
expected_params = { 'RSA': [ 'private_key' ] }
encoding = 'utf-8'

def _decrypt(ciphertext, enc_params):
    plaintext = supported_enc_schemes[enc_params['algorithm']](ciphertext, enc_params)
    decoded = plaintext.decode(encoding)
    try:
        return json.loads(decoded)
    except (json.JSONDecodeError, ValueError):
        return decoded

def _decrypt_nodes(node, key, value, enc_params):
    if isinstance(node, list):
        for idx in range(len(node)):
            _decrypt_nodes(node[idx], key, value, enc_params)
    elif isinstance(node, dict) and (key in list(node.keys())):
        if isinstance(node[key], list):
            for idx, data in enumerate(node[key]):
                if data == value:
                    if isinstance(node[key][idx], dict):
                        node_str = json.dumps(node[key][idx])
                    else:
                        node_str = node[key][idx]
                    node[key][idx] = _decrypt(bytes.fromhex(node_str), enc_params)
        else:
            if isinstance(node[key], dict):
                node_str = json.dumps(node[key])
            else:
                node_str = node[key]
            node[key] = _decrypt(bytes.fromhex(node_str), enc_params)

def decrypt_by_path(resource, el, params):
    algorithm = params.get('algorithm', 'RSA')
    if algorithm not in supported_enc_schemes:
        error(f'Unsupported decryption algorithm: {algorithm!r}. Supported: {list(supported_enc_schemes)}')
    params = {**params, 'algorithm': algorithm}
    if not all(param in params for param in expected_params[algorithm]):
        error(f'Missing params (expected {expected_params[algorithm]})')
    ret = resource
    path = el['path']
    path = path.split('.')[1:] # Remove root
    if len(path) == 0:
        ret.clear()
        return
    ret = find_nodes(ret, path[:-1], [])
    _decrypt_nodes(ret, path[-1], el['value'], params)