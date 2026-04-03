import json
from pathlib import Path
import defusedxml.ElementTree as ET
import xml.etree.ElementTree as _ET_WRITE  # stdlib ET used only for write operations


# Common FHIR repeating element names that should be represented as lists even
# when a single occurrence appears in XML.
_REPEATING_KEYS = {
    'entry', 'name', 'given', 'identifier', 'extension', 'coding',
    'component', 'category', 'address', 'telecom', 'note', 'line', 'contained',
    'dosage', 'performer', 'participant', 'reasonCode', 'reasonReference',
}


def detect_format(path_str, requested='auto'):
    """Detect input/output format from explicit request or file extension."""
    if requested and requested != 'auto':
        return requested

    suffix = Path(path_str).suffix.lower()
    if suffix in ('.ndjson', '.jsonl'):
        return 'ndjson'
    if suffix == '.xml':
        return 'xml'
    return 'json'


def _coerce_primitive(value):
    if value is None:
        return None
    text = str(value)
    if text.lower() == 'true':
        return True
    if text.lower() == 'false':
        return False
    if text.lower() == 'null':
        return None
    try:
        if '.' in text:
            return float(text)
        return int(text)
    except ValueError:
        return text


def _strip_ns(tag):
    return tag.split('}', 1)[1] if '}' in tag else tag


def _xml_to_obj(elem):
    children = list(elem)
    attrs = dict(elem.attrib)

    # FHIR primitive style: <given value="Dan" />
    if not children and 'value' in attrs:
        return _coerce_primitive(attrs['value'])

    ret = {}

    # Preserve primitive+extension representation when both are present.
    if 'value' in attrs:
        ret['value'] = _coerce_primitive(attrs['value'])

    for child in children:
        key = _strip_ns(child.tag)
        val = _xml_to_obj(child)
        if key in ret:
            if not isinstance(ret[key], list):
                ret[key] = [ret[key]]
            ret[key].append(val)
        else:
            ret[key] = val

    for key in list(ret.keys()):
        if key in _REPEATING_KEYS and not isinstance(ret[key], list):
            ret[key] = [ret[key]]

    text = (elem.text or '').strip()
    if not children and text:
        return text

    return ret


def read_fhir_xml(file_path):
    tree = ET.parse(file_path)
    root = tree.getroot()
    return _xml_root_to_payload(root)


def _xml_root_to_payload(root):
    resource_type = _strip_ns(root.tag)
    payload = _xml_to_obj(root)
    if not isinstance(payload, dict):
        payload = {'value': payload}
    payload['resourceType'] = resource_type
    return payload


def read_fhir_xml_string(xml_text):
    root = ET.fromstring(xml_text)
    return _xml_root_to_payload(root)


def _append_xml(parent, key, value):
    if isinstance(value, list):
        for item in value:
            _append_xml(parent, key, item)
        return

    child = _ET_WRITE.SubElement(parent, key)

    if isinstance(value, dict):
        # If this is an embedded resource in a Bundle entry.resource, wrap the
        # inner resource with its resourceType tag for valid FHIR structure.
        if key == 'resource' and 'resourceType' in value:
            nested = _ET_WRITE.SubElement(child, value['resourceType'])
            for k, v in value.items():
                if k == 'resourceType':
                    continue
                _append_xml(nested, k, v)
            return

        for k, v in value.items():
            if k == 'resourceType':
                continue
            _append_xml(child, k, v)
        return

    if value is None:
        child.set('value', 'null')
    elif isinstance(value, bool):
        child.set('value', 'true' if value else 'false')
    else:
        child.set('value', str(value))


def write_fhir_xml(payload, file_path):
    xml_text = write_fhir_xml_string(payload)
    with open(file_path, 'w', encoding='utf-8') as fout:
        fout.write(xml_text)


def write_fhir_xml_string(payload):
    if isinstance(payload, list):
        payload = {
            'resourceType': 'Bundle',
            'type': 'collection',
            'entry': [{'resource': item} for item in payload],
        }

    if not isinstance(payload, dict) or 'resourceType' not in payload:
        raise ValueError('XML output requires a FHIR resource object or list of resources')

    root = _ET_WRITE.Element(payload['resourceType'])
    root.set('xmlns', 'http://hl7.org/fhir')
    for k, v in payload.items():
        if k == 'resourceType':
            continue
        _append_xml(root, k, v)
    return _ET_WRITE.tostring(root, encoding='utf-8', xml_declaration=True).decode('utf-8')


def read_input_file(input_path, in_format, strip_line_prefix='//'):
    if in_format == 'json':
        with open(input_path, 'r', encoding='utf-8') as fin:
            return json.load(fin)

    if in_format == 'xml':
        return read_fhir_xml(input_path)

    if in_format == 'ndjson':
        records = []
        with open(input_path, 'r', encoding='utf-8-sig') as fin:
            for line_number, raw_line in enumerate(fin, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                if strip_line_prefix and line.startswith(strip_line_prefix):
                    line = line[len(strip_line_prefix):]
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f'Invalid NDJSON at line {line_number}: {exc}') from exc
        return records

    raise ValueError(f'Unsupported input format: {in_format}')


def write_output_file(payload, output_path, out_format, pretty=False):
    if out_format == 'json':
        with open(output_path, 'w', encoding='utf-8') as fout:
            if pretty:
                json.dump(payload, fout, indent=2)
            else:
                json.dump(payload, fout, separators=(',', ':'))
            fout.write('\n')
        return

    if out_format == 'xml':
        write_fhir_xml(payload, output_path)
        return

    if out_format == 'ndjson':
        with open(output_path, 'w', encoding='utf-8') as fout:
            if isinstance(payload, list):
                for item in payload:
                    fout.write(json.dumps(item, separators=(',', ':')))
                    fout.write('\n')
            else:
                fout.write(json.dumps(payload, separators=(',', ':')))
                fout.write('\n')
        return

    raise ValueError(f'Unsupported output format: {out_format}')


def _detect_by_content_type(content_type):
    if not content_type:
        return 'json'
    ct = content_type.lower()
    if 'ndjson' in ct:
        return 'ndjson'
    if 'xml' in ct:
        return 'xml'
    return 'json'


def parse_payload_bytes(body, in_format='auto', content_type=None, strip_line_prefix='//'):
    fmt = in_format if in_format != 'auto' else _detect_by_content_type(content_type)
    text = body.decode('utf-8-sig') if isinstance(body, (bytes, bytearray)) else str(body)

    if fmt == 'json':
        return json.loads(text)
    if fmt == 'xml':
        return read_fhir_xml_string(text)
    if fmt == 'ndjson':
        records = []
        for line_number, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            if strip_line_prefix and line.startswith(strip_line_prefix):
                line = line[len(strip_line_prefix):]
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f'Invalid NDJSON at line {line_number}: {exc}') from exc
        return records

    raise ValueError(f'Unsupported input format: {fmt}')


def serialize_payload(payload, out_format='json', pretty=False):
    if out_format == 'json':
        if pretty:
            return json.dumps(payload, indent=2) + '\n', 'application/fhir+json'
        return json.dumps(payload, separators=(',', ':')) + '\n', 'application/fhir+json'
    if out_format == 'ndjson':
        if isinstance(payload, list):
            # Build NDJSON line-by-line to avoid holding the full serialized string
            # in memory alongside the payload list.  For very large lists the caller
            # should stream instead, but this prevents the 2× peak from .join().
            parts = []
            for item in payload:
                parts.append(json.dumps(item, separators=(',', ':')) + '\n')
            text = ''.join(parts)
        else:
            text = json.dumps(payload, separators=(',', ':')) + '\n'
        return text, 'application/x-ndjson'
    if out_format == 'xml':
        return write_fhir_xml_string(payload), 'application/fhir+xml'
    raise ValueError(f'Unsupported output format: {out_format}')
