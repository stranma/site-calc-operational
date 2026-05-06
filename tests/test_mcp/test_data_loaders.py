"""Tests for data_loaders: profile resolution, save_csv, fetch_url_to_file."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import respx
from httpx import Response

from site_calc_operational.mcp.data_loaders import (
    fetch_url_to_file,
    resolve_profile,
    save_csv,
)

# ---------------------------------------------------------------------------
# resolve_profile
# ---------------------------------------------------------------------------


def test_resolve_profile_broadcasts_scalar() -> None:
    """Failure mode: scalar is returned as-is, so the server sees a number
    where it expects an array of length=intervals."""
    assert resolve_profile(50.0, 4) == [50.0, 50.0, 50.0, 50.0]
    assert resolve_profile(7, 3) == [7.0, 7.0, 7.0]


def test_resolve_profile_scalar_without_length_raises() -> None:
    """Failure mode: a scalar without a known length is silently expanded to
    [], producing a length-mismatch the LLM can't easily debug."""
    with pytest.raises(ValueError, match="without a timespan"):
        resolve_profile(50.0, None)


def test_resolve_profile_rejects_bool() -> None:
    """Failure mode: True is treated as 1.0 because bool is a subclass of int,
    so misuse like {"price": True} silently broadcasts to ones."""
    with pytest.raises(ValueError, match="Boolean"):
        resolve_profile(True, 3)  # type: ignore[arg-type]


def test_resolve_profile_validates_array_length() -> None:
    """Failure mode: array of wrong length is forwarded to the server, which
    returns a 422 mentioning intervals not the offending property."""
    with pytest.raises(ValueError, match="does not match expected length"):
        resolve_profile([1.0, 2.0], 3)


def test_resolve_profile_passes_array_through_when_length_ok() -> None:
    """Failure mode: array values are coerced to ints, dropping fractional precision."""
    result = resolve_profile([1.5, 2.5, 3.5], 3)
    assert result == [1.5, 2.5, 3.5]


def test_resolve_profile_loads_json_file(tmp_path: Path) -> None:
    """Failure mode: JSON-array file references are forwarded raw to the server,
    which can't read the user's local filesystem."""
    p = tmp_path / "vals.json"
    p.write_text("[1.0, 2.0, 3.0]")
    assert resolve_profile({"file": str(p)}, 3) == [1.0, 2.0, 3.0]


def test_resolve_profile_loads_csv_named_column(tmp_csv: str) -> None:
    """Failure mode: column='price_eur_mwh' is ignored, the first column (hour)
    is loaded instead, and prices come out as 0..23 rather than 50..61."""
    result = resolve_profile({"file": tmp_csv, "column": "price_eur_mwh"}, 24)
    assert len(result) == 24
    assert result[0] == 50.0
    assert result[12] == 50.0  # i=12 -> 50 + (12 % 12) = 50


def test_resolve_profile_loads_csv_first_numeric_column_when_no_column(tmp_csv_no_header: str) -> None:
    """Failure mode: header-less CSVs raise instead of falling back to the
    first numeric column."""
    result = resolve_profile({"file": tmp_csv_no_header}, 24)
    assert len(result) == 24
    assert result[0] == 50.0


def test_resolve_profile_named_column_on_no_header_raises(tmp_csv_no_header: str) -> None:
    """Failure mode: named column on a CSV without a header is silently
    matched against the first row's values, producing wrong arrays."""
    with pytest.raises(ValueError, match="no header"):
        resolve_profile({"file": tmp_csv_no_header, "column": "price"}, 24)


def test_resolve_profile_missing_file_raises() -> None:
    """Failure mode: a missing file is forwarded to the server as the literal
    path string, producing a 422 instead of FileNotFoundError locally."""
    with pytest.raises(FileNotFoundError, match="not found"):
        resolve_profile({"file": "/no/such/path.csv"}, 4)


def test_resolve_profile_unknown_extension_raises(tmp_path: Path) -> None:
    """Failure mode: an unsupported file extension is treated as JSON, fails
    json.loads and produces a misleading error."""
    p = tmp_path / "bad.parquet"
    p.write_bytes(b"\x00\x00")
    with pytest.raises(ValueError, match="Unsupported file format"):
        resolve_profile({"file": str(p)}, 4)


def test_resolve_profile_json_non_list_raises(tmp_path: Path) -> None:
    """Failure mode: JSON object {"prices": [...]} is silently flattened to its
    keys, producing arrays of strings."""
    p = tmp_path / "obj.json"
    p.write_text('{"prices": [1, 2, 3]}')
    with pytest.raises(ValueError, match="flat array"):
        resolve_profile({"file": str(p)}, 3)


# ---------------------------------------------------------------------------
# save_csv
# ---------------------------------------------------------------------------


def test_save_csv_writes_header_and_values(tmp_path: Path) -> None:
    """Failure mode: header row is missing or column ordering is non-deterministic,
    so the LLM cannot pass column='price' to a subsequent load."""
    out = save_csv(
        file_path="prices.csv",
        columns={"hour": [0.0, 1.0, 2.0], "price": [50.0, 60.0, 70.0]},
        data_dir=str(tmp_path),
        overwrite=False,
    )
    rows = Path(out).read_text().splitlines()
    assert rows[0] == "hour,price"
    assert rows[1] == "0,50"
    assert rows[3] == "2,70"


def test_save_csv_appends_csv_extension(tmp_path: Path) -> None:
    """Failure mode: file written without extension, so OS associations / later
    file-resolution loaders treat it as plain text."""
    out = save_csv(
        file_path="prices",
        columns={"x": [1.0]},
        data_dir=str(tmp_path),
        overwrite=False,
    )
    assert out.endswith("prices.csv")


def test_save_csv_resolves_relative_against_data_dir(tmp_path: Path) -> None:
    """Failure mode: relative paths are resolved against cwd instead of data_dir,
    so saved files end up in the wrong place."""
    out = save_csv(
        file_path="sub/prices.csv",
        columns={"x": [1.0, 2.0]},
        data_dir=str(tmp_path),
        overwrite=False,
    )
    assert os.path.normcase(out).startswith(os.path.normcase(str(tmp_path)))
    assert os.path.exists(out)


def test_save_csv_rejects_overwrite_when_disallowed(tmp_path: Path) -> None:
    """Failure mode: existing files silently overwritten, so an LLM mistake
    destroys data the user intended to preserve."""
    save_csv("a.csv", {"x": [1.0]}, data_dir=str(tmp_path), overwrite=False)
    with pytest.raises(FileExistsError):
        save_csv("a.csv", {"x": [2.0]}, data_dir=str(tmp_path), overwrite=False)


def test_save_csv_overwrites_when_allowed(tmp_path: Path) -> None:
    """Failure mode: overwrite=True doesn't actually replace contents, so
    subsequent reads see stale data."""
    out = save_csv("a.csv", {"x": [1.0]}, data_dir=str(tmp_path), overwrite=False)
    save_csv("a.csv", {"x": [99.0]}, data_dir=str(tmp_path), overwrite=True)
    assert Path(out).read_text().splitlines()[1] == "99"


def test_save_csv_rejects_mismatched_lengths(tmp_path: Path) -> None:
    """Failure mode: mismatched column lengths produce a ragged CSV that
    silently corrupts later loads."""
    with pytest.raises(ValueError, match="same length"):
        save_csv("a.csv", {"x": [1.0, 2.0], "y": [3.0]}, data_dir=str(tmp_path), overwrite=False)


def test_save_csv_rejects_empty_columns(tmp_path: Path) -> None:
    """Failure mode: no-op call writes an empty file that masks the LLM's
    actual mistake (forgetting to populate columns)."""
    with pytest.raises(ValueError, match="at least one column"):
        save_csv("a.csv", {}, data_dir=str(tmp_path), overwrite=False)


# ---------------------------------------------------------------------------
# fetch_url_to_file
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_url_writes_csv_and_returns_metadata(tmp_path: Path) -> None:
    """Failure mode: metadata reports wrong row count or omits column names,
    so set_timespan(intervals=...) gets the wrong value."""
    body = "hour,price\n" + "\n".join(f"{i},{50 + i}" for i in range(24)) + "\n"
    respx.get("http://example.test/prices.csv").mock(return_value=Response(200, text=body))

    meta = fetch_url_to_file(
        url="http://example.test/prices.csv",
        data_dir=str(tmp_path),
        file_path=None,
        overwrite=False,
    )
    assert meta["rows"] == 24
    assert meta["columns"] == ["hour", "price"]
    assert "price" in meta["numeric_columns"]
    assert os.path.exists(meta["file_path"])


@respx.mock
def test_fetch_url_rejects_unsupported_scheme(tmp_path: Path) -> None:
    """Failure mode: file:// URLs slip through, exposing arbitrary local files
    via the MCP tool."""
    with pytest.raises(ValueError, match="scheme"):
        fetch_url_to_file(
            url="file:///etc/passwd",
            data_dir=str(tmp_path),
            file_path=None,
            overwrite=False,
        )


@respx.mock
def test_fetch_url_refuses_overwrite_by_default(tmp_path: Path) -> None:
    """Failure mode: a second fetch silently replaces the user's previous
    download, losing data they wanted to keep."""
    body = "x\n1\n"
    respx.get("http://example.test/q.csv").mock(return_value=Response(200, text=body))

    fetch_url_to_file("http://example.test/q.csv", data_dir=str(tmp_path), file_path=None, overwrite=False)
    with pytest.raises(FileExistsError):
        fetch_url_to_file("http://example.test/q.csv", data_dir=str(tmp_path), file_path=None, overwrite=False)


@respx.mock
def test_fetch_url_metadata_error_does_not_break_download(tmp_path: Path) -> None:
    """Failure mode: a malformed CSV body raises during parsing and the file is
    left half-written. The download itself should always succeed; metadata
    failure is a soft warning the LLM can inspect."""
    # Purposefully tiny body that confuses the sniffer.
    body = "x"
    respx.get("http://example.test/tiny.csv").mock(return_value=Response(200, text=body))
    meta = fetch_url_to_file("http://example.test/tiny.csv", data_dir=str(tmp_path), file_path=None, overwrite=False)
    assert os.path.exists(meta["file_path"])
    # CSV with a single non-numeric cell -> rows=0, columns=[], numeric_columns=[]
    # (sniffer treats the single value as a header row.)
    # Either the parser succeeds and reports zero rows, or it sets metadata_error.
    assert "rows" in meta or "metadata_error" in meta
