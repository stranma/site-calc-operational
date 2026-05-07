"""CSV/JSON data loading + URL-fetch utilities for the operational MCP server.

These helpers exist because the LLM cannot itself touch the local filesystem,
but the MCP server runs on the user's machine and *can*. Tools call into here
to (a) write generated profile arrays to disk so they can be referenced by
``add_device`` via ``{"file": "..."}``, (b) download remote CSV/JSON data,
and (c) resolve a property value (scalar / list / file ref) to a flat array
when ``build_request`` materializes a payload.
"""

from __future__ import annotations

import csv
import ipaddress
import json
import os
import posixpath
import socket
from typing import Any
from urllib.parse import urlparse

import httpx

# Hosts/IPs that must not be reachable through ``fetch_url`` even if the user's
# machine can reach them. The MCP tool sits on the trust boundary between an
# LLM (which may be running prompt-injected content) and the user's local
# network, so a public-only allow-list is the right default. Operators who
# legitimately need to fetch from internal hosts can use ``save_data_file``
# with content they paste themselves.
_BLOCKED_HOST_LITERALS = frozenset(
    {
        "localhost",
        "ip6-localhost",
        "ip6-loopback",
        "broadcasthost",
    }
)

# ---------------------------------------------------------------------------
# Profile resolution
# ---------------------------------------------------------------------------


def resolve_profile(
    value: float | int | list[float] | dict[str, Any],
    expected_length: int | None,
) -> list[float]:
    """Resolve a profile-shaped property value to a flat list of floats.

    Accepted forms:
    - ``float`` or ``int``: broadcast to ``[value] * expected_length``
    - ``list``: returned (after float-casting); validated against ``expected_length``
    - ``{"file": "<path>"}``: loaded from CSV (first numeric column) or JSON (flat array)
    - ``{"file": "<path>", "column": "<name>"}``: specific CSV column

    :param value: Scalar, array, or file-reference dict.
    :param expected_length: Required array length, or ``None`` to skip validation.
    :returns: Flat list of floats with ``len() == expected_length`` if specified.
    :raises ValueError: Unsupported value type or length mismatch.
    :raises FileNotFoundError: File reference points to a missing path.
    """
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        raise ValueError("Boolean values are not valid profile data; pass a number, list, or {file: ...}.")
    if isinstance(value, (int, float)):
        if expected_length is None:
            raise ValueError(
                "Cannot broadcast a scalar profile value without a timespan. "
                "Set the timespan first or supply an explicit array."
            )
        return [float(value)] * expected_length
    if isinstance(value, list):
        result = [float(v) for v in value]
        if expected_length is not None and len(result) != expected_length:
            raise ValueError(
                f"Profile array length {len(result)} does not match expected length "
                f"{expected_length} (derived from timespan / resolution)."
            )
        return result
    if isinstance(value, dict):
        return _load_array(value, expected_length)
    raise ValueError(
        f"Unsupported profile value type {type(value).__name__}; expected float, list[float], or {{'file': '<path>'}}."
    )


def _load_array(spec: dict[str, Any], expected_length: int | None) -> list[float]:
    """Load a numeric array from a file reference dict."""
    path = spec.get("file")
    if not path:
        raise ValueError("File reference must include a 'file' key with the path.")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Data file not found: {path}. Provide an absolute path to a CSV or JSON file on the local filesystem."
        )

    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        result = _load_json_array(path)
    elif ext in (".csv", ".tsv", ".txt"):
        result = _load_csv_column(path, spec.get("column"))
    else:
        raise ValueError(f"Unsupported file format '{ext}'. Supported: .csv, .tsv, .txt, .json")

    if expected_length is not None and len(result) != expected_length:
        raise ValueError(f"File '{path}' has {len(result)} values, but expected {expected_length} (from timespan).")
    return result


def _load_json_array(path: str) -> list[float]:
    """Load a flat numeric JSON array."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"JSON file '{path}' must contain a flat array of numbers, got {type(data).__name__}.")
    try:
        return [float(v) for v in data]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"JSON file '{path}' contains non-numeric values: {exc}") from exc


def _load_csv_column(path: str, column: str | None) -> list[float]:
    """Load a numeric column from a CSV file. If ``column`` is None, use the first numeric column."""
    with open(path, encoding="utf-8", newline="") as f:
        sample = f.read(8192)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample) if sample.strip() else csv.excel
            has_header = csv.Sniffer().has_header(sample) if sample.strip() else False
        except csv.Error:
            dialect = csv.excel
            has_header = False
        reader = csv.reader(f, dialect)  # type: ignore[arg-type]

        rows = list(reader)

    if not rows:
        return []

    if has_header:
        header = rows[0]
        body = rows[1:]
        if column is not None:
            if column not in header:
                raise ValueError(f"CSV column '{column}' not found in {path}; available: {header}")
            idx = header.index(column)
        else:
            idx = _first_numeric_column_index(body)
    else:
        if column is not None:
            raise ValueError(f"CSV file '{path}' has no header, cannot resolve named column '{column}'.")
        body = rows
        idx = _first_numeric_column_index(body)

    return [float(row[idx]) for row in body if row and len(row) > idx and row[idx] != ""]


def _first_numeric_column_index(rows: list[list[str]]) -> int:
    """Return the index of the first column whose first cell is numeric."""
    if not rows:
        return 0
    sample = rows[0]
    for i, cell in enumerate(sample):
        try:
            float(cell)
            return i
        except ValueError:
            continue
    raise ValueError("No numeric column found in CSV.")


# ---------------------------------------------------------------------------
# CSV writing
# ---------------------------------------------------------------------------


def save_csv(
    file_path: str,
    columns: dict[str, list[float]],
    data_dir: str | None,
    overwrite: bool,
) -> str:
    """Write named numeric columns to a CSV file with a header row.

    :param file_path: Filename or path. ``.csv`` is appended if missing. Relative
        paths resolve against ``data_dir`` (or cwd if ``data_dir`` is None).
    :param columns: Mapping of column name to numeric values. All columns must be
        the same length.
    :param data_dir: Base directory for relative ``file_path`` (or None for cwd).
    :param overwrite: Allow overwriting an existing file.
    :returns: Absolute path of the saved file.
    :raises ValueError: ``columns`` is empty, or columns have mismatched lengths.
    :raises FileExistsError: Target exists and ``overwrite`` is False.
    """
    if not columns:
        raise ValueError("save_csv requires at least one column.")
    lengths = {name: len(values) for name, values in columns.items()}
    if len(set(lengths.values())) != 1:
        raise ValueError(f"All columns must have the same length; got {lengths}.")

    abs_path = _resolve_outpath(file_path, data_dir, default_ext=".csv")
    if os.path.exists(abs_path) and not overwrite:
        raise FileExistsError(f"File already exists: {abs_path}. Pass overwrite=True to replace it.")

    os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
    names = list(columns.keys())
    nrows = next(iter(lengths.values()))
    with open(abs_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(names)
        for i in range(nrows):
            writer.writerow([_format_number(columns[name][i]) for name in names])
    return abs_path


def _format_number(value: float) -> str:
    """Format a number for CSV output without trailing zeros.

    :param value: Number to format.
    :returns: String representation that round-trips via ``float()``.
    """
    if isinstance(value, int) or value == int(value):
        return str(int(value))
    return repr(float(value))


def _resolve_outpath(file_path: str, data_dir: str | None, default_ext: str) -> str:
    """Resolve a user-supplied path to an absolute file path inside the data dir.

    The MCP server runs locally with the user's filesystem privileges. To keep
    a prompt-injected LLM from writing arbitrary files (``/etc/cron.d/...``,
    ``~/.ssh/authorized_keys``, ...), we constrain every resolved path to the
    configured ``data_dir`` (or cwd if unset). Both absolute paths and ``..``
    traversal are rejected when they would escape the root.

    :param file_path: User-supplied filename or relative path.
    :param data_dir: Sandbox root; falls back to cwd when ``None``.
    :param default_ext: Appended to ``file_path`` when it has no extension.
    :returns: Absolute, normalized path strictly inside ``data_dir``/cwd.
    :raises ValueError: ``file_path`` resolves outside the allowed root.
    """
    if not os.path.splitext(file_path)[1]:
        file_path = file_path + default_ext
    base = os.path.abspath(data_dir or os.getcwd())
    if os.path.isabs(file_path):
        candidate = os.path.abspath(file_path)
    else:
        candidate = os.path.abspath(os.path.join(base, file_path))
    candidate = os.path.normpath(candidate)
    base_norm = os.path.normpath(base)
    # Add the separator so '/data' and '/data2' do not look like a prefix match.
    if not (candidate == base_norm or candidate.startswith(base_norm + os.sep)):
        raise ValueError(
            f"Refusing to write outside the data directory ({base_norm}). "
            f"Resolved path was {candidate!r}; supply a relative path inside the data dir."
        )
    return candidate


# ---------------------------------------------------------------------------
# URL fetching
# ---------------------------------------------------------------------------


def fetch_url_to_file(
    url: str,
    data_dir: str | None,
    file_path: str | None,
    overwrite: bool,
) -> dict[str, Any]:
    """Download ``url`` to a local file and return metadata about it.

    For CSV files, the response body is parsed and the metadata includes row
    count, header columns (if present), and the indices of numeric columns so
    the caller can wire them into ``add_device`` immediately.

    :param url: HTTP or HTTPS URL.
    :param data_dir: Base directory for relative ``file_path`` (or None for cwd).
    :param file_path: Destination filename. Default: derived from URL path.
    :param overwrite: Allow overwriting an existing file.
    :returns: Dict with ``file_path``, ``url``, ``bytes``, ``rows``, ``columns``,
        ``numeric_columns``, ``message``.
    :raises ValueError: Invalid URL scheme.
    :raises FileExistsError: Destination exists and ``overwrite`` is False.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme '{parsed.scheme}'; only http/https are allowed.")
    _assert_public_host(parsed.hostname)

    if file_path is None:
        derived = posixpath.basename(parsed.path) or "downloaded.csv"
        file_path = derived
    abs_path = _resolve_outpath(file_path, data_dir, default_ext=".csv")
    if os.path.exists(abs_path) and not overwrite:
        raise FileExistsError(f"File already exists: {abs_path}. Pass overwrite=True to replace it.")

    os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
    # Re-validate every redirect target so an attacker cannot 302 us into a
    # private network. We follow redirects manually instead of letting httpx
    # do it transparently.
    data = _fetch_url_safely(url)
    with open(abs_path, "wb") as f:
        f.write(data)

    metadata: dict[str, Any] = {
        "file_path": abs_path,
        "url": url,
        "bytes": len(data),
    }
    if abs_path.lower().endswith((".csv", ".tsv", ".txt")):
        try:
            metadata.update(_csv_metadata(abs_path))
        except (csv.Error, OSError) as exc:
            metadata["metadata_error"] = str(exc)
    metadata["message"] = f"Saved {len(data)} bytes to {abs_path}"
    return metadata


def _csv_metadata(path: str) -> dict[str, Any]:
    """Inspect a CSV file and return rows / columns / numeric_columns."""
    with open(path, encoding="utf-8", newline="") as f:
        sample = f.read(8192)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample) if sample.strip() else csv.excel
            has_header = csv.Sniffer().has_header(sample) if sample.strip() else False
        except csv.Error:
            dialect = csv.excel
            has_header = False
        rows = list(csv.reader(f, dialect))  # type: ignore[arg-type]

    if not rows:
        return {"rows": 0, "columns": [], "numeric_columns": []}

    header: list[str] = rows[0] if has_header else []
    body = rows[1:] if has_header else rows
    numeric_idx = [i for i in range(len(rows[0])) if _is_numeric_column(body, i)]
    numeric_cols = [header[i] for i in numeric_idx] if header else numeric_idx
    return {
        "rows": len(body),
        "columns": header,
        "numeric_columns": numeric_cols,
    }


def _is_numeric_column(rows: list[list[str]], idx: int) -> bool:
    """Check whether the first non-empty value in column ``idx`` is numeric."""
    for row in rows:
        if len(row) <= idx:
            continue
        cell = row[idx]
        if cell == "":
            continue
        try:
            float(cell)
            return True
        except ValueError:
            return False
    return False


# ---------------------------------------------------------------------------
# Network safety
# ---------------------------------------------------------------------------


def _assert_public_host(host: str | None) -> None:
    """Reject hostnames/IPs that resolve to the loopback or private networks.

    Defense-in-depth against a prompt-injected LLM using ``fetch_url`` to
    pivot into the user's intranet, AWS instance metadata, or other locally-
    reachable services. DNS is resolved here so an attacker cannot use a
    public DNS name that A-records to ``127.0.0.1`` or ``169.254.169.254``.

    :param host: Hostname or IP literal extracted from the URL.
    :raises ValueError: ``host`` is empty, in the literal blocklist, or
        resolves to a non-public IP.
    """
    if host is None or not host:
        raise ValueError("URL must include a host.")
    lowered = host.lower()
    if lowered in _BLOCKED_HOST_LITERALS:
        raise ValueError(f"Refusing to fetch from disallowed host '{host}'.")

    # Try to resolve every A/AAAA record. Any non-public IP rejects the URL.
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except socket.gaierror as exc:
        raise ValueError(f"DNS resolution failed for '{host}': {exc}") from exc
    for addr in addresses:
        ip = ipaddress.ip_address(addr)
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError(
                f"Refusing to fetch from non-public address {addr} (host={host}). "
                "fetch_url is restricted to public Internet hosts; for private/intranet data "
                "use save_data_file with content you supply directly."
            )


def _fetch_url_safely(url: str, max_redirects: int = 5) -> bytes:
    """GET ``url``, validating every redirect target against :func:`_assert_public_host`.

    Manual redirect handling prevents an attacker from issuing a 302 that
    points the client at a private IP after the initial host check passed.

    :param url: HTTPS or HTTP URL whose host has already been validated.
    :param max_redirects: Hard cap on the number of redirects to follow.
    :returns: Response body bytes from the final 200 response.
    :raises ValueError: Redirect loop, redirect to a private host, or non-2xx terminal status.
    """
    seen: set[str] = set()
    current = url
    with httpx.Client(timeout=30.0, follow_redirects=False) as client:
        for _ in range(max_redirects + 1):
            if current in seen:
                raise ValueError(f"Redirect loop detected at {current}.")
            seen.add(current)
            response = client.get(current)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ValueError(f"Redirect from {current} missing Location header.")
                target = httpx.URL(current).join(location)
                _assert_public_host(target.host)
                current = str(target)
                continue
            response.raise_for_status()
            return response.content
    raise ValueError(f"Exceeded redirect limit ({max_redirects}) starting from {url}.")
