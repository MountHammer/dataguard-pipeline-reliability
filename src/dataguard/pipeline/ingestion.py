"""Read the four source CSV files into pandas DataFrames.

This is the first stage of the pipeline. Its only job is to read files
safely and check their *structure*. It does not judge individual
values - that is the validation stage's job (data dictionary §9).

What it checks (batch-level - any failure stops the whole run):
    - each of the four expected files exists and is readable
    - the file has a header row
    - the header has every expected column and no unexpected ones
    - the CSV can be parsed (row boundaries can be established)

What it keeps exactly as found, for the validation stage:
    - every value as a plain string; an empty field stays "" (never NaN)
    - malformed values such as "abc" in a numeric column
    - rows with the wrong number of fields: kept, with the reason in
      the ``ingestion_error`` column, so validation can quarantine them
      as MALFORMED_ROW (data dictionary §9)

Columns added for traceability (data dictionary §11):
    source_file        e.g. "orders.csv"
    source_row_number  physical line number (header = 1, first record = 2)
    ingestion_error    "" for a normal row, otherwise a MALFORMED_ROW message

Only the four file names in the schema registry are opened, so the
``_expected/`` answer-key folder is never read.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd

from dataguard.schemas import SCHEMAS, EntitySchema

TRACE_COLUMNS = ("source_file", "source_row_number", "ingestion_error")


class SourceDataError(ValueError):
    """A batch-level problem with a source file (missing, unreadable or bad header).

    Raising this stops the pipeline run before anything is published.
    """


def load_source_data(input_dir: Path | str) -> dict[str, pd.DataFrame]:
    """Read all four source files from ``input_dir``.

    Files are read in dependency order (customers, products, orders,
    order_items). The first batch-level problem raises SourceDataError.

    Returns:
        {"customers": DataFrame, "products": ..., "orders": ..., "order_items": ...}
        Each DataFrame has the schema columns in schema order, followed by
        source_file, source_row_number and ingestion_error.
    """
    input_dir = Path(input_dir)
    return {name: read_source_file(input_dir / schema.file_name, schema)
            for name, schema in SCHEMAS.items()}


def read_source_file(path: Path, schema: EntitySchema) -> pd.DataFrame:
    """Read and structurally check one source CSV file."""
    if not path.is_file():
        raise SourceDataError(f"{schema.file_name}: required source file not found at {path}")

    try:
        # newline="" is how the csv module expects files to be opened.
        # utf-8-sig also accepts files saved with a byte-order mark (e.g. by Excel).
        with open(path, newline="", encoding="utf-8-sig") as handle:
            # strict=True makes broken quoting an error instead of a silent guess.
            reader = csv.reader(handle, strict=True)
            header = next(reader, None)
            check_header(header, schema)

            rows = []
            for fields in reader:
                # reader.line_num = physical line where this record ends.
                rows.append(_build_row(fields, header, schema, reader.line_num))
    except UnicodeDecodeError as error:
        raise SourceDataError(f"{schema.file_name}: file is not valid UTF-8 ({error})") from None
    except csv.Error as error:
        raise SourceDataError(f"{schema.file_name}: cannot be parsed as CSV ({error})") from None

    # dtype=object keeps plain Python strings; pandas does no type guessing.
    columns = list(schema.column_names) + list(TRACE_COLUMNS)
    return pd.DataFrame(rows, columns=columns, dtype=object)


def check_header(header: list[str] | None, schema: EntitySchema) -> None:
    """Raise SourceDataError unless the header has exactly the expected columns.

    Column order does not matter (data dictionary §9).
    """
    if not header:
        raise SourceDataError(f"{schema.file_name}: file is empty or has no header row")

    duplicates = sorted({name for name in header if header.count(name) > 1})
    if duplicates:
        raise SourceDataError(f"{schema.file_name}: duplicate columns {duplicates}")

    missing = [name for name in schema.column_names if name not in header]
    if missing:
        raise SourceDataError(f"{schema.file_name}: missing required columns {missing}")

    unexpected = [name for name in header if name not in schema.column_names]
    if unexpected:
        raise SourceDataError(f"{schema.file_name}: unexpected columns {unexpected}")


def _build_row(fields: list[str], header: list[str], schema: EntitySchema, line_number: int) -> list:
    """Turn one CSV record into a row in schema column order, plus trace columns.

    If the record has the wrong number of fields we cannot tell which
    value belongs to which column, so the data columns are left empty
    and the problem is written to ingestion_error instead of guessing.
    """
    if len(fields) != len(header):
        error = f"MALFORMED_ROW: expected {len(header)} fields, found {len(fields)}"
        return [""] * len(schema.column_names) + [schema.file_name, line_number, error]

    by_name = dict(zip(header, fields))                  # incoming column order
    values = [by_name[name] for name in schema.column_names]  # schema column order
    return values + [schema.file_name, line_number, ""]
