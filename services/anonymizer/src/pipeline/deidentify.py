from utils.fhirpath import not_implemented
from actions.redact import redact_by_path
from actions.cryptohash import cryptohash_by_path
from actions.perturb import perturb_by_path
from actions.substitute import substitute_by_path
from actions.generalize import generalize_by_path
from actions.scrub_text import scrub_text_by_path
from integrations.nlp.detector import nlp_detect_by_path

actions = {
    "redact": redact_by_path,
    "perturb": perturb_by_path,
    "cryptohash": cryptohash_by_path,
    "substitute": substitute_by_path,
    "generalize": generalize_by_path,
    "scrub_text": scrub_text_by_path,
    "nlp_detect": nlp_detect_by_path,
}


def perform_deidentification(action, resource, el, params):
    if action in list(actions.keys()):
        actions[action](resource, el, params)
    else:
        not_implemented(f'Method {action} is not implemented')
    return resource
