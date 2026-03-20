import argparse
import datetime
import hashlib
import json
from collections import Counter
from pathlib import Path

from pipeline.io_formats import detect_format, read_input_file


def _collect_keyed_strings(obj, keys, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, str):
                out.append(v)
            _collect_keyed_strings(v, keys, out)
    elif isinstance(obj, list):
        for item in obj:
            _collect_keyed_strings(item, keys, out)


def _collect_request_urls(obj, out):
    if isinstance(obj, dict):
        if isinstance(obj.get('request'), dict) and isinstance(obj['request'].get('url'), str):
            out.append(obj['request']['url'])
        for v in obj.values():
            _collect_request_urls(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _collect_request_urls(item, out)


def _collect_resource_types(payload):
    counter = Counter()

    def walk(node):
        if isinstance(node, dict):
            rt = node.get('resourceType')
            if isinstance(rt, str):
                counter[rt] += 1
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return counter


def _extract_resources(payload):
    if isinstance(payload, dict) and payload.get('resourceType') == 'Bundle':
        resources = []
        for entry in payload.get('entry', []):
            if isinstance(entry, dict) and isinstance(entry.get('resource'), dict):
                resources.append(entry['resource'])
        return resources
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def _metric_ids_changed(original, processed):
    orig_resources = _extract_resources(original)
    proc_resources = _extract_resources(processed)
    total = min(len(orig_resources), len(proc_resources))

    changed = 0
    for i in range(total):
        o = orig_resources[i]
        p = proc_resources[i]
        if isinstance(o, dict) and isinstance(p, dict):
            if o.get('id') != p.get('id'):
                changed += 1
    return changed, total


def _metric_reference_url_changes(original, processed):
    orig_refs, proc_refs = [], []
    orig_urls, proc_urls = [], []
    orig_req_urls, proc_req_urls = [], []
    _collect_keyed_strings(original, {'reference'}, orig_refs)
    _collect_keyed_strings(processed, {'reference'}, proc_refs)
    _collect_keyed_strings(original, {'url'}, orig_urls)
    _collect_keyed_strings(processed, {'url'}, proc_urls)
    _collect_request_urls(original, orig_req_urls)
    _collect_request_urls(processed, proc_req_urls)

    ref_changes = 0
    for i in range(min(len(orig_refs), len(proc_refs))):
        if orig_refs[i] != proc_refs[i]:
            ref_changes += 1

    url_key_changes = 0
    for i in range(min(len(orig_urls), len(proc_urls))):
        if orig_urls[i] != proc_urls[i]:
            url_key_changes += 1

    request_url_changes = 0
    for i in range(min(len(orig_req_urls), len(proc_req_urls))):
        if orig_req_urls[i] != proc_req_urls[i]:
            request_url_changes += 1

    return ref_changes, request_url_changes, url_key_changes


def _metric_token_counts(processed):
    text = json.dumps(processed, ensure_ascii=False)
    token_counts = Counter()

    i = 0
    while i < len(text):
        start = text.find('[[', i)
        if start < 0:
            break
        end = text.find(']]', start + 2)
        if end < 0:
            break
        token = text[start + 2:end]
        prefix = token.split('_', 1)[0] if '_' in token else token
        token_counts[prefix] += 1
        i = end + 2

    return token_counts


def _config_info(config_path: str | None) -> dict:
    """Return config filename and SHA-256 hash for audit purposes."""
    if not config_path:
        return {'config_file': None, 'config_sha256': None}
    data = Path(config_path).read_bytes()
    return {
        'config_file': Path(config_path).name,
        'config_sha256': hashlib.sha256(data).hexdigest(),
    }


def analyze_pair(input_file, output_file, in_format='auto', out_format='auto',
                 strip_line_prefix='//', operator=None, config_path=None):
    chosen_in = detect_format(input_file, in_format)
    chosen_out = detect_format(output_file, out_format)

    original = read_input_file(input_file, chosen_in, strip_line_prefix)
    processed = read_input_file(output_file, chosen_out, strip_line_prefix)

    ids_changed, comparable_ids = _metric_ids_changed(original, processed)
    ref_changes, request_url_changes, url_key_changes = _metric_reference_url_changes(original, processed)

    result = {
        'input_file': input_file,
        'output_file': output_file,
        'input_format': chosen_in,
        'output_format': chosen_out,
        'operator': operator or None,
        'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z'),
        **_config_info(config_path),
        'resource_types_before': dict(_collect_resource_types(original)),
        'resource_types_after': dict(_collect_resource_types(processed)),
        'ids_changed': ids_changed,
        'comparable_resource_ids': comparable_ids,
        'reference_changes': ref_changes,
        'request_url_changes': request_url_changes,
        'url_key_changes': url_key_changes,
        'token_counts': dict(_metric_token_counts(processed)),
    }
    return result


def _markdown_report(results):
    lines = ['# MedAnon Analytics Report', '']
    for idx, r in enumerate(results, start=1):
        lines.append(f'## Dataset {idx}')
        lines.append(f"- input: {r['input_file']}")
        lines.append(f"- output: {r['output_file']}")
        lines.append(f"- format: {r['input_format']} -> {r['output_format']}")
        if r.get('operator'):
            lines.append(f"- operator: {r['operator']}")
        if r.get('timestamp'):
            lines.append(f"- timestamp: {r['timestamp']}")
        if r.get('config_file'):
            lines.append(f"- config: {r['config_file']}")
        if r.get('config_sha256'):
            lines.append(f"- config sha256: {r['config_sha256']}")
        lines.append(f"- ids changed: {r['ids_changed']} / {r['comparable_resource_ids']}")
        lines.append(f"- reference changes: {r['reference_changes']}")
        lines.append(f"- request.url changes: {r['request_url_changes']}")
        lines.append(f"- any url-key changes: {r['url_key_changes']}")
        lines.append('- resource types before:')
        for k, v in sorted(r['resource_types_before'].items()):
            lines.append(f'  - {k}: {v}')
        lines.append('- resource types after:')
        for k, v in sorted(r['resource_types_after'].items()):
            lines.append(f'  - {k}: {v}')
        lines.append('- token counts:')
        if r['token_counts']:
            for k, v in sorted(r['token_counts'].items()):
                lines.append(f'  - {k}: {v}')
        else:
            lines.append('  - none')
        lines.append('')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='Analyze before/after FHIR transformation outputs.')
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--input-format', default='auto', choices=['auto', 'json', 'ndjson', 'xml'])
    parser.add_argument('--output-format', default='auto', choices=['auto', 'json', 'ndjson', 'xml'])
    parser.add_argument('--strip-line-prefix', default='//')
    parser.add_argument('--operator', default=None,
                        help='Name of the person running the analysis (included in report)')
    parser.add_argument('--config', default=None,
                        help='Path to the config file used during de-identification (SHA-256 hash included in report)')
    parser.add_argument('--report-json')
    parser.add_argument('--report-md')
    args = parser.parse_args()

    result = analyze_pair(
        input_file=args.input,
        output_file=args.output,
        in_format=args.input_format,
        out_format=args.output_format,
        strip_line_prefix=args.strip_line_prefix,
        operator=args.operator,
        config_path=args.config,
    )

    if args.report_json:
        Path(args.report_json).parent.mkdir(parents=True, exist_ok=True)
        with open(args.report_json, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2)
            f.write('\n')

    if args.report_md:
        Path(args.report_md).parent.mkdir(parents=True, exist_ok=True)
        with open(args.report_md, 'w', encoding='utf-8') as f:
            f.write(_markdown_report([result]))
            f.write('\n')

    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
