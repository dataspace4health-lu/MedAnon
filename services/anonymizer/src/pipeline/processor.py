import json
import logging
import re
from copy import deepcopy

from utils.fhirpath import not_implemented, find_nodes
from actions.substitute import _substitute_nodes

from pipeline.deidentify import actions as deident_actions, perform_deidentification
from integrations.gpas.dispatcher import pseudo_actions, perform_pseudonymization
from integrations.gpas.dispatcher import depseudo_actions, perform_depseudonymization
from integrations.gpas.client import gpas_pseudonymize_batch

import fhirpathpy

audit_log = logging.getLogger("medanon.audit")

# Register the FHIRPath log() invocation once at module level to avoid
# repeated global mutation on every rule evaluation (thread-safety fix).
fhirpathpy.engine.invocations['log'] = {
    'fn': lambda ctx, els: [{'path': x.path, 'value': x.data} for x in els]
}

# Pre-compute action sets as module-level frozensets (Step 7)
DEIDENT_ACTIONS = frozenset(deident_actions)
PSEUDO_ACTIONS = frozenset(pseudo_actions)
DEPSEUDO_ACTIONS = frozenset(depseudo_actions)
GPAS_PSEUDO_ACTIONS = frozenset({'gpas_pseudonymize'})


def _processing_errors_mode(settings):
    return str(getattr(settings, 'processing_errors', 'raise')).lower()


def _interpolate_dynamic(value, dynamic_settings):
    if isinstance(value, str):
        out = value
        for k, v in dynamic_settings.items():
            out = out.replace("{{" + str(k) + "}}", str(v))
            out = out.replace("${" + str(k) + "}", str(v))
        return out
    if isinstance(value, list):
        return [_interpolate_dynamic(x, dynamic_settings) for x in value]
    if isinstance(value, dict):
        return {k: _interpolate_dynamic(v, dynamic_settings) for k, v in value.items()}
    return value


def _resolve_rule_params(rule, settings):
    dynamic_settings = getattr(settings, 'dynamic_rule_settings', None)
    # Skip deepcopy when no dynamic settings (the common case) (Step 7)
    if not isinstance(dynamic_settings, dict) or not dynamic_settings:
        return rule.get('params', {})

    params = deepcopy(rule['params']) if 'params' in rule else {}
    params = _interpolate_dynamic(params, dynamic_settings)
    for key, value in dynamic_settings.items():
        if key in params:
            params[key] = value
    return params


def _build_match_candidates(match_expr, resource):
    if not isinstance(match_expr, str):
        return []

    candidates = [match_expr]
    if isinstance(resource, dict):
        resource_type = resource.get('resourceType')
        if resource_type:
            if match_expr.startswith('*.'):
                candidates.append(f"{resource_type}{match_expr[1:]}")
            if '{resourceType}' in match_expr:
                candidates.append(match_expr.replace('{resourceType}', resource_type))

    return list(dict.fromkeys(candidates))


# -- Rule index by ResourceType (Step 2) ------------------------------------

_rule_index_cache = {}


def _get_rule_resource_type(rule):
    """Extract the resource type prefix from a rule's match expression."""
    match = rule.get('match', '')
    if not isinstance(match, str):
        return '*'
    if match.startswith('*.') or match.startswith('{resourceType}'):
        return '*'
    dot = match.find('.')
    if dot > 0:
        return match[:dot]
    return '*'


def _build_rule_index(rules):
    """Build {resourceType: [rules]} index for fast rule lookup."""
    index = {}
    for rule in rules:
        if not isinstance(rule, dict) or 'match' not in rule or 'action' not in rule:
            continue
        rt = _get_rule_resource_type(rule)
        index.setdefault(rt, []).append(rule)
    return index


def _get_rules_for_resource(resource, settings):
    """Return only the rules applicable to this resource's type."""
    rules = getattr(settings, 'rules', [])
    rules_id = id(rules)
    if rules_id not in _rule_index_cache:
        _rule_index_cache[rules_id] = _build_rule_index(rules)
    index = _rule_index_cache[rules_id]

    resource_type = resource.get('resourceType', '') if isinstance(resource, dict) else ''
    applicable = list(index.get(resource_type, []))
    applicable.extend(index.get('*', []))
    return applicable


# -- gPAS params cache (Step 7) ---------------------------------------------

_gpas_params_cache = {}


def _extract_gpas_params(settings):
    """Extract gPAS params from the first gpas_pseudonymize rule, if any."""
    rules = getattr(settings, 'rules', [])
    rules_id = id(rules)
    if rules_id in _gpas_params_cache:
        return _gpas_params_cache[rules_id]
    result = None
    for rule in rules:
        if isinstance(rule, dict) and rule.get('action') == 'gpas_pseudonymize':
            result = _resolve_rule_params(rule, settings)
            break
    _gpas_params_cache[rules_id] = result
    return result


def _process_single_resource(resource, settings):
    result = resource
    processing_mode = _processing_errors_mode(settings)
    applicable_rules = _get_rules_for_resource(resource, settings)

    # Two-pass processing for gPAS batching (Step 1)
    # Pass 1: evaluate all rules, collect matched elements.
    # For gPAS actions, accumulate values for batch call.
    # For non-gPAS actions, apply immediately.
    gpas_work = []  # list of (rule, el, params) for gPAS actions
    processed_paths = set()  # (path, category) for double-processing prevention (Step 5)

    for rule in applicable_rules:
        action = rule['action']
        params = _resolve_rule_params(rule, settings)

        matched_elements = []
        for candidate in _build_match_candidates(rule['match'], resource):
            try:
                matched = fhirpathpy.evaluate(resource, candidate + '.log()', [])
                matched_elements.extend(matched)
            except Exception:
                audit_log.warning(
                    "fhirpath_eval_failed expression=%s resource_type=%s",
                    # Strip newlines to prevent log injection
                    candidate.replace('\n', ' ').replace('\r', ' '),
                    resource.get('resourceType', 'unknown') if isinstance(resource, dict) else 'unknown',
                )
                continue

        # Determine which category this action belongs to (Step 5)
        if action in DEIDENT_ACTIONS:
            category = 'deidentify'
        elif action in PSEUDO_ACTIONS:
            category = 'pseudonymize'
        elif action in DEPSEUDO_ACTIONS:
            category = 'depseudonymize'
        else:
            category = 'unknown'

        # Filter out elements already processed by a *previous* rule in the
        # same category (prevents double-hashing from overlapping rules like
        # Practitioner.identifier.value + *.identifier.value).
        # Elements within the same rule are NOT filtered — the action itself
        # handles array elements (e.g. redact removes the key for each match).
        elements_to_process = []
        for el in matched_elements:
            el_path = el.get('path', '?')
            path_key = (el_path, category)
            if path_key in processed_paths:
                audit_log.info(
                    "rule_skipped_duplicate action=%s path=%s category=%s",
                    action, el_path, category,
                )
                continue
            elements_to_process.append(el)

        # Mark all paths from this rule as processed *before* applying,
        # so subsequent rules won't re-process them.
        for el in elements_to_process:
            processed_paths.add((el.get('path', '?'), category))

        for el in elements_to_process:
            el_path = el.get('path', '?')

            audit_log.info(
                "rule_applied action=%s match=%s path=%s resource_type=%s",
                action,
                rule['match'],
                el_path,
                resource.get('resourceType', 'unknown') if isinstance(resource, dict) else 'unknown',
            )

            if action in GPAS_PSEUDO_ACTIONS:
                gpas_work.append((rule, el, params))
                continue

            try:
                if action in DEIDENT_ACTIONS:
                    result = perform_deidentification(action, resource, el, params)
                elif action in PSEUDO_ACTIONS:
                    result = perform_pseudonymization(action, resource, el, params)
                elif action in DEPSEUDO_ACTIONS:
                    result = perform_depseudonymization(action, resource, el, params)
                else:
                    not_implemented(f'Method {action} is not implemented')
            except Exception:
                if processing_mode == 'skip':
                    audit_log.exception(
                        "rule_failed_skip action=%s match=%s path=%s",
                        action, rule.get('match'), el_path,
                    )
                    # Redact the field to prevent PHI leakage (Step 4)
                    try:
                        perform_deidentification('redact', resource, el, {})
                    except Exception:
                        audit_log.error(
                            "fallback_redact_failed path=%s — PHI may be exposed; re-raising",
                            el_path,
                        )
                        raise
                    continue
                raise

    # Pass 2: batch gPAS pseudonymization
    batch_mapping = {}  # populated below if gpas_work; used for rewrite_text_ids
    if gpas_work:
        gpas_params = gpas_work[0][2]
        values_to_pseudonymize = []
        for _rule, el, _params in gpas_work:
            val = el['value']
            original_value = str(val) if not isinstance(val, dict) else json.dumps(val)
            values_to_pseudonymize.append(original_value)

        try:
            batch_mapping = gpas_pseudonymize_batch(values_to_pseudonymize, gpas_params)
        except Exception:
            if processing_mode == 'skip':
                audit_log.exception("gpas_batch_failed count=%d", len(values_to_pseudonymize))
                # Redact all gPAS fields to prevent PHI leakage (Step 4)
                for _rule, el, _params in gpas_work:
                    try:
                        perform_deidentification('redact', resource, el, {})
                    except Exception:
                        audit_log.exception("fallback_redact_failed path=%s", el.get('path', '?'))
                batch_mapping = {}
            else:
                raise

        for _rule, el, _params in gpas_work:
            val = el['value']
            original_value = str(val) if not isinstance(val, dict) else json.dumps(val)
            pseudonym = batch_mapping.get(original_value)
            if pseudonym is None:
                if processing_mode == 'skip':
                    audit_log.warning("gpas_no_pseudonym path=%s", el.get('path', '?'))
                    try:
                        perform_deidentification('redact', resource, el, {})
                    except Exception:
                        audit_log.exception("fallback_redact_failed path=%s", el.get('path', '?'))
                    continue
                raise ValueError(f'gPAS did not return a pseudonym for value (path={el["path"]})')

            path = el['path'].split('.')[1:]
            if len(path) == 0:
                resource.clear()
                continue
            ret = find_nodes(resource, path[:-1], [])
            _substitute_nodes(ret, path[-1], el['value'], pseudonym)

    # ---- Post-processing: pseudonymize FHIR references across resources ----
    if getattr(settings, 'rewrite_references', False):
        gpas_params = _extract_gpas_params(settings)
        if gpas_params:
            _deep_rewrite_references_gpas(result, gpas_params)

    # ---- Post-processing: replace bare IDs embedded in free-text fields ----
    if getattr(settings, 'rewrite_text_ids', False) and batch_mapping:
        id_text_map = {
            k: v for k, v in batch_mapping.items()
            if k and v and k != v and not k.startswith('{')
        }
        if id_text_map:
            audit_log.info("rewriting_text_ids count=%d", len(id_text_map))
            _rewrite_text_ids(result, id_text_map)

    return result


_MAX_NESTING_DEPTH = 50


def _rewrite_references(obj, ref_map, _depth=0):
    """Deep-walk a JSON structure and rewrite FHIR reference strings and request URLs."""
    if _depth > _MAX_NESTING_DEPTH:
        raise ValueError(
            f"FHIR resource nesting exceeds maximum depth of {_MAX_NESTING_DEPTH}"
        )
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str) and value in ref_map and key in ('reference', 'url'):
                audit_log.info(
                    "reference_rewritten field=%s old=%s new=%s",
                    key, value, ref_map[value],
                )
                obj[key] = ref_map[value]
            else:
                _rewrite_references(value, ref_map, _depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _rewrite_references(item, ref_map, _depth + 1)


def _rewrite_text_ids(obj, id_map, compiled=None, _depth=0):
    """Walk all string values and replace any bare original ID with its pseudonym.

    id_map: {original_id: new_id}  (raw IDs, no resource-type prefix)
    Skips structural fields ('id', 'reference', 'url', 'resourceType') which
    are handled by other post-processors.
    """
    if _depth > _MAX_NESTING_DEPTH:
        raise ValueError(
            f"FHIR resource nesting exceeds maximum depth of {_MAX_NESTING_DEPTH}"
        )
    if not id_map:
        return
    if compiled is None:
        parts = [re.escape(k) for k in id_map if k]
        if not parts:
            return
        compiled = re.compile(r'\b(' + '|'.join(parts) + r')\b')

    if isinstance(obj, dict):
        for key in list(obj.keys()):
            if key in ('id', 'reference', 'url', 'resourceType'):
                continue
            value = obj[key]
            if isinstance(value, str):
                new_val = compiled.sub(lambda m: id_map[m.group(1)], value)
                if new_val != value:
                    audit_log.info("text_id_rewritten field=%s", key)
                    obj[key] = new_val
            else:
                _rewrite_text_ids(value, id_map, compiled, _depth + 1)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str):
                new_val = compiled.sub(lambda m: id_map[m.group(1)], item)
                if new_val != item:
                    obj[i] = new_val
            else:
                _rewrite_text_ids(item, id_map, compiled, _depth + 1)


# ---------------------------------------------------------------------------
# Cross-resource reference pseudonymization (for NDJSON / bulk exports)
# ---------------------------------------------------------------------------

def _collect_reference_ids(obj, ids):
    """Collect all reference IDs from a FHIR resource for batch pseudonymization."""
    if isinstance(obj, dict):
        ref = obj.get('reference')
        if isinstance(ref, str) and ref and '?' not in ref and not ref.startswith('#'):
            if ref.startswith('urn:uuid:'):
                resource_id = ref[len('urn:uuid:'):]
                if resource_id:
                    ids.add(resource_id)
            elif '/' in ref and not ref.startswith('http'):
                parts = ref.split('/')
                if len(parts) == 2 and parts[0] and parts[1]:
                    ids.add(parts[1])
        for value in obj.values():
            _collect_reference_ids(value, ids)
    elif isinstance(obj, list):
        for item in obj:
            _collect_reference_ids(item, ids)


def _pseudonymize_reference_string(ref, ref_mapping):
    """Pseudonymize the ID portion of a FHIR reference string using pre-computed mapping."""
    if not ref or not isinstance(ref, str):
        return ref
    if '?' in ref or ref.startswith('#'):
        return ref

    if ref.startswith('urn:uuid:'):
        resource_id = ref[len('urn:uuid:'):]
        if resource_id and resource_id in ref_mapping:
            return f"urn:uuid:{ref_mapping[resource_id]}"
        return ref

    if '/' in ref and not ref.startswith('http'):
        parts = ref.split('/')
        if len(parts) == 2 and parts[0] and parts[1]:
            resource_type, resource_id = parts
            if resource_id in ref_mapping:
                return f"{resource_type}/{ref_mapping[resource_id]}"
        return ref

    return ref


def _deep_rewrite_references_gpas(obj, gpas_params):
    """Deep-walk a FHIR resource and pseudonymize all direct reference IDs via gPAS.

    Uses batch pseudonymization: collects all reference IDs first, batch-pseudonymizes,
    then rewrites in a second pass.
    """
    # Pass 1: collect all reference IDs
    ref_ids = set()
    _collect_reference_ids(obj, ref_ids)

    if not ref_ids:
        return

    # Pass 2: batch pseudonymize all collected IDs
    ref_mapping = gpas_pseudonymize_batch(list(ref_ids), gpas_params)

    # Pass 3: rewrite references using the mapping
    _apply_reference_pseudonyms(obj, ref_mapping)


def _apply_reference_pseudonyms(obj, ref_mapping):
    """Apply pre-computed pseudonym mapping to all references in a resource."""
    if isinstance(obj, dict):
        ref = obj.get('reference')
        if isinstance(ref, str):
            new_ref = _pseudonymize_reference_string(ref, ref_mapping)
            if new_ref != ref:
                audit_log.info(
                    "reference_pseudonymized old=%s new=%s", ref, new_ref,
                )
                obj['reference'] = new_ref
            if 'display' in obj:
                audit_log.info("reference_display_redacted field=display")
                del obj['display']

        for value in obj.values():
            _apply_reference_pseudonyms(value, ref_mapping)
    elif isinstance(obj, list):
        for item in obj:
            _apply_reference_pseudonyms(item, ref_mapping)


def _process_bundle(resource, settings):
    entries = resource.get('entry', [])

    # Snapshot original resource IDs before any processing
    pre_ids = []
    for entry in entries:
        r = entry.get('resource', {}) if isinstance(entry, dict) else {}
        if isinstance(r, dict) and 'resourceType' in r and 'id' in r:
            pre_ids.append((r['resourceType'], r['id']))
        else:
            pre_ids.append((None, None))

    # Process each entry (applies gPAS pseudonymization, redaction, etc.)
    for entry in entries:
        if isinstance(entry, dict) and 'resource' in entry:
            entry['resource'] = process_data(entry['resource'], settings)

    # Build reference mapping from IDs that changed during processing
    ref_map = {}  # e.g. "Patient/DDME" -> "Patient/mii_14300827064"
    for i, entry in enumerate(entries):
        old_type, old_id = pre_ids[i]
        if old_type is None:
            continue
        r = entry.get('resource', {}) if isinstance(entry, dict) else {}
        new_id = r.get('id') if isinstance(r, dict) else None
        if new_id is not None and old_id != new_id:
            ref_map[f"{old_type}/{old_id}"] = f"{old_type}/{new_id}"

    # Rewrite all FHIR references and request URLs throughout the bundle
    if ref_map:
        audit_log.info("rewriting_references count=%d", len(ref_map))
        _rewrite_references(resource, ref_map)

    # Replace bare original IDs embedded in any free-text string field
    if getattr(settings, 'rewrite_text_ids', False) and ref_map:
        id_text_map = {}
        for old_ref, new_ref in ref_map.items():
            old_id = old_ref.split('/', 1)[-1]
            new_id = new_ref.split('/', 1)[-1]
            if old_id and new_id and old_id != new_id:
                id_text_map[old_id] = new_id
        if id_text_map:
            audit_log.info("rewriting_text_ids count=%d", len(id_text_map))
            _rewrite_text_ids(resource, id_text_map)

    return resource


def process_data(resource, settings):
    if isinstance(resource, list):
        return [process_data(res, settings) for res in resource]
    if isinstance(resource, dict) and resource.get('resourceType') == 'Bundle':
        return _process_bundle(resource, settings)
    return _process_single_resource(resource, settings)
