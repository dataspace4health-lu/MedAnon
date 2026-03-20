"""gPAS dispatcher — routes pipeline pseudonymization actions to the gPAS client.

Merges the old clients/pseudonymize.py and clients/depseudonymize.py into a
single adapter module.  The pipeline imports only from here; it never calls
the gPAS HTTP client directly.
"""

from utils.fhirpath import not_implemented
from actions.encrypt import encrypt_by_path
from actions.decrypt import decrypt_by_path
from integrations.gpas.client import (
    gpas_pseudonymize_by_path,
    gpas_depseudonymize_by_path,
)

pseudo_actions = {
    "gpas_pseudonymize": gpas_pseudonymize_by_path,
    "encrypt": encrypt_by_path,
}

depseudo_actions = {
    "gpas_depseudonymize": gpas_depseudonymize_by_path,
    "decrypt": decrypt_by_path,
}


def perform_pseudonymization(action, resource, el, params):
    if action in pseudo_actions:
        pseudo_actions[action](resource, el, params)
    else:
        not_implemented(f'Method {action} is not implemented')
    return resource


def perform_depseudonymization(action, resource, el, params):
    if action in depseudo_actions:
        depseudo_actions[action](resource, el, params)
    else:
        not_implemented(f'Method {action} is not implemented')
    return resource
