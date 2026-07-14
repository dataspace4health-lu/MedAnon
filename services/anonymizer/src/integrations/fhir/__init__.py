"""integrations.fhir  FHIR R4 server client.

Public API (re-exported from sub-modules):
    reader.py    paginated fetch, $everything, search
    writer.py    upload, batch Bundles, reference rewriting
    bulk.py      $bulk-export: kick-off, poll, download
    client.py    unified facade (re-exports reader + writer + bulk)
"""
