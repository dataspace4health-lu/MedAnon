"""Lightweight in-memory OMOP CDM (v5.4 core subset).

``OmopData`` holds each table as a list of row dicts. ``CORE_TABLES`` is the CDM
spec the DQD-style checks read: primary key, required (NOT NULL) fields, foreign
keys, and date fields. Kept deliberately small (core clinical tables) per the
"core tables first" scope; extend the spec to widen coverage.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TableSpec:
    name: str
    pk: str
    required: tuple[str, ...]  # NOT NULL columns
    fks: tuple[tuple[str, str], ...] = ()  # (column, referenced_table)
    dates: tuple[str, ...] = ()  # date/datetime columns (lexical YYYY-MM-DD...)


# OMOP CDM v5.4 core clinical tables. PERSON is the only table required to be
# present; the rest are checked only when the provider supplies them.
CORE_TABLES: dict[str, TableSpec] = {
    "person": TableSpec(
        name="person",
        pk="person_id",
        required=("person_id", "gender_concept_id", "year_of_birth"),
    ),
    "observation_period": TableSpec(
        name="observation_period",
        pk="observation_period_id",
        required=(
            "observation_period_id",
            "person_id",
            "observation_period_start_date",
            "observation_period_end_date",
        ),
        fks=(("person_id", "person"),),
        dates=("observation_period_start_date", "observation_period_end_date"),
    ),
    "visit_occurrence": TableSpec(
        name="visit_occurrence",
        pk="visit_occurrence_id",
        required=(
            "visit_occurrence_id",
            "person_id",
            "visit_concept_id",
            "visit_start_date",
        ),
        fks=(("person_id", "person"),),
        dates=("visit_start_date", "visit_end_date"),
    ),
    "condition_occurrence": TableSpec(
        name="condition_occurrence",
        pk="condition_occurrence_id",
        required=(
            "condition_occurrence_id",
            "person_id",
            "condition_concept_id",
            "condition_start_date",
        ),
        fks=(("person_id", "person"),),
        dates=("condition_start_date", "condition_end_date"),
    ),
    "drug_exposure": TableSpec(
        name="drug_exposure",
        pk="drug_exposure_id",
        required=(
            "drug_exposure_id",
            "person_id",
            "drug_concept_id",
            "drug_exposure_start_date",
        ),
        fks=(("person_id", "person"),),
        dates=("drug_exposure_start_date", "drug_exposure_end_date"),
    ),
    "measurement": TableSpec(
        name="measurement",
        pk="measurement_id",
        required=(
            "measurement_id",
            "person_id",
            "measurement_concept_id",
            "measurement_date",
        ),
        fks=(("person_id", "person"),),
        dates=("measurement_date",),
    ),
    "observation": TableSpec(
        name="observation",
        pk="observation_id",
        required=(
            "observation_id",
            "person_id",
            "observation_concept_id",
            "observation_date",
        ),
        fks=(("person_id", "person"),),
        dates=("observation_date",),
    ),
    "procedure_occurrence": TableSpec(
        name="procedure_occurrence",
        pk="procedure_occurrence_id",
        required=(
            "procedure_occurrence_id",
            "person_id",
            "procedure_concept_id",
            "procedure_date",
        ),
        fks=(("person_id", "person"),),
        dates=("procedure_date",),
    ),
    "device_exposure": TableSpec(
        name="device_exposure",
        pk="device_exposure_id",
        required=(
            "device_exposure_id",
            "person_id",
            "device_concept_id",
            "device_exposure_start_date",
        ),
        fks=(("person_id", "person"),),
        dates=("device_exposure_start_date", "device_exposure_end_date"),
    ),
    "specimen": TableSpec(
        name="specimen",
        pk="specimen_id",
        required=("specimen_id", "person_id", "specimen_concept_id", "specimen_date"),
        fks=(("person_id", "person"),),
        dates=("specimen_date",),
    ),
    "visit_detail": TableSpec(
        name="visit_detail",
        pk="visit_detail_id",
        required=(
            "visit_detail_id",
            "person_id",
            "visit_detail_concept_id",
            "visit_detail_start_date",
        ),
        fks=(("person_id", "person"),),
        dates=("visit_detail_start_date", "visit_detail_end_date"),
    ),
    "death": TableSpec(
        name="death",
        pk="person_id",  # OMOP death is one row per person
        required=("person_id", "death_date"),
        fks=(("person_id", "person"),),
        dates=("death_date",),
    ),
    "note": TableSpec(
        name="note",
        pk="note_id",
        required=("note_id", "person_id", "note_date"),
        fks=(("person_id", "person"),),
        dates=("note_date",),
    ),
}

# The single table that must be present for an OMOP dataset to be assessable.
REQUIRED_TABLES: tuple[str, ...] = ("person",)


@dataclass
class OmopData:
    """An in-memory OMOP dataset: ``tables[name]`` is a list of row dicts."""

    tables: dict[str, list[dict]] = field(default_factory=dict)
    # Tables the provider submitted that are not in CORE_TABLES (not assessed).
    # Surfaced in the passport so coverage is transparent, never silently dropped.
    ignored_tables: list[str] = field(default_factory=list)

    def rows(self, table: str) -> list[dict]:
        return self.tables.get(table, [])

    def add(self, table: str, row: dict) -> None:
        self.tables.setdefault(table, []).append(row)

    def pk_set(self, table: str) -> set:
        spec = CORE_TABLES.get(table)
        if spec is None:
            return set()
        return {
            r.get(spec.pk)
            for r in self.rows(table)
            if r.get(spec.pk) is not None
        }

    def total_rows(self) -> int:
        return sum(len(v) for v in self.tables.values())

    def present_tables(self) -> list[str]:
        return [t for t in CORE_TABLES if self.tables.get(t)]
