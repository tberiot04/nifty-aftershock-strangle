#!/usr/bin/env python3
"""Audit the provided NIFTY option archives without modifying the raw files.

The script reads ZIP members in-place, inventories every expiry archive, samples
option schemas, consolidates the repeated spot series, and rejects archives after
the defined research cutoff.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import re
import statistics
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path


ARCHIVE_RE = re.compile(r"^(?P<expiry>\d{8})\.zip$")
OPTION_RE = re.compile(
    r"^(?P<strike>\d+)(?P<right>CE|PE)_(?P<expiry>\d{8})\.csv$"
)
EXPECTED_COLUMNS = [
    "Date",
    "Timestamp",
    "Open",
    "High",
    "Low",
    "Close",
    "Volume",
    "OI",
    "Ticker",
]
TIMESTAMP_FORMAT = "%d-%m-%Y %H:%M:%S"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--output-root", default=Path("data"), type=Path)
    parser.add_argument(
        "--cutoff",
        default="20251130",
        help="Latest permitted observation/archive date in YYYYMMDD format.",
    )
    return parser.parse_args()


def first_record_and_header(
    archive: zipfile.ZipFile, member: str
) -> tuple[list[str], dict[str, str] | None]:
    with archive.open(member) as raw:
        reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
        return list(reader.fieldnames or []), next(reader, None)


def sample_members(option_members: list[str]) -> list[str]:
    """Choose representative low/middle/high contracts for each option right."""
    selected: list[str] = []
    for right in ("CE", "PE"):
        members = sorted(
            (name for name in option_members if f"{right}_" in name),
            key=lambda name: int(OPTION_RE.match(name).group("strike")),  # type: ignore[union-attr]
        )
        if not members:
            continue
        for index in sorted({0, len(members) // 2, len(members) - 1}):
            selected.append(members[index])
    return selected


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    data_root = args.data_root.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    audit_root = output_root / "audit"
    processed_root = output_root / "processed"
    audit_root.mkdir(parents=True, exist_ok=True)
    processed_root.mkdir(parents=True, exist_ok=True)

    if not data_root.is_dir():
        raise SystemExit(f"Data root does not exist: {data_root}")
    if not re.fullmatch(r"\d{8}", args.cutoff):
        raise SystemExit("--cutoff must use YYYYMMDD format")

    archives = sorted(data_root.glob("*.zip"))
    manifest: list[dict[str, object]] = []
    schema_counts: Counter[tuple[str, ...]] = Counter()
    option_schema_counts: Counter[tuple[str, ...]] = Counter()
    spot_by_timestamp: dict[datetime, dict[str, str]] = {}
    spot_source_by_timestamp: dict[datetime, str] = {}
    spot_duplicate_rows = 0
    spot_conflicting_duplicates = 0
    archive_errors: list[str] = []
    sample_errors: list[str] = []

    for archive_path in archives:
        match = ARCHIVE_RE.match(archive_path.name)
        expiry_key = match.group("expiry") if match else ""
        eligible = bool(match and expiry_key <= args.cutoff)
        row: dict[str, object] = {
            "archive": archive_path.name,
            "expiry": expiry_key,
            "eligible": eligible,
            "compressed_bytes": archive_path.stat().st_size,
            "status": "excluded_after_cutoff" if match and not eligible else "pending",
            "member_count": "",
            "option_member_count": "",
            "ce_member_count": "",
            "pe_member_count": "",
            "paired_strike_count": "",
            "ce_only_strike_count": "",
            "pe_only_strike_count": "",
            "min_strike": "",
            "max_strike": "",
            "uncompressed_bytes": "",
            "spot_rows": "",
            "spot_start": "",
            "spot_end": "",
            "spot_present": "",
            "malformed_option_members": "",
            "sample_error_count": 0,
        }
        if not match:
            row["status"] = "invalid_archive_name"
            manifest.append(row)
            continue
        if not eligible:
            manifest.append(row)
            continue

        try:
            with zipfile.ZipFile(archive_path) as archive:
                infos = [info for info in archive.infolist() if not info.is_dir()]
                names = [info.filename for info in infos]
                option_members = [name for name in names if OPTION_RE.match(name)]
                malformed = [
                    name
                    for name in names
                    if name.lower().endswith(".csv")
                    and name != "nifty_spot.csv"
                    and not OPTION_RE.match(name)
                ]
                parsed = [OPTION_RE.match(name) for name in option_members]
                strikes = [int(item.group("strike")) for item in parsed if item]
                ce_count = sum(item.group("right") == "CE" for item in parsed if item)
                pe_count = sum(item.group("right") == "PE" for item in parsed if item)
                ce_strikes = {
                    int(item.group("strike"))
                    for item in parsed
                    if item and item.group("right") == "CE"
                }
                pe_strikes = {
                    int(item.group("strike"))
                    for item in parsed
                    if item and item.group("right") == "PE"
                }
                mismatched_expiry = sum(
                    item.group("expiry") != expiry_key for item in parsed if item
                )

                row.update(
                    {
                        "status": "ok",
                        "member_count": len(infos),
                        "option_member_count": len(option_members),
                        "ce_member_count": ce_count,
                        "pe_member_count": pe_count,
                        "paired_strike_count": len(ce_strikes & pe_strikes),
                        "ce_only_strike_count": len(ce_strikes - pe_strikes),
                        "pe_only_strike_count": len(pe_strikes - ce_strikes),
                        "min_strike": min(strikes) if strikes else "",
                        "max_strike": max(strikes) if strikes else "",
                        "uncompressed_bytes": sum(info.file_size for info in infos),
                        "spot_present": "nifty_spot.csv" in names,
                        "malformed_option_members": len(malformed),
                        "mismatched_member_expiry": mismatched_expiry,
                    }
                )

                for member in sample_members(option_members):
                    try:
                        header, first = first_record_and_header(archive, member)
                        option_schema_counts[tuple(header)] += 1
                        if first is None:
                            sample_errors.append(f"{archive_path.name}:{member}: empty")
                    except Exception as exc:  # continue auditing other archives
                        sample_errors.append(
                            f"{archive_path.name}:{member}: {type(exc).__name__}: {exc}"
                        )

                if "nifty_spot.csv" not in names:
                    row["status"] = "missing_spot"
                    manifest.append(row)
                    continue

                with archive.open("nifty_spot.csv") as raw:
                    reader = csv.DictReader(
                        io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                    )
                    header = tuple(reader.fieldnames or [])
                    schema_counts[header] += 1
                    spot_rows = 0
                    first_timestamp: datetime | None = None
                    last_timestamp: datetime | None = None
                    for spot_row in reader:
                        spot_rows += 1
                        try:
                            timestamp = datetime.strptime(
                                spot_row["Timestamp"], TIMESTAMP_FORMAT
                            )
                        except Exception as exc:
                            sample_errors.append(
                                f"{archive_path.name}:nifty_spot.csv row {spot_rows}: "
                                f"invalid timestamp ({exc})"
                            )
                            continue
                        if first_timestamp is None:
                            first_timestamp = timestamp
                        last_timestamp = timestamp
                        prior = spot_by_timestamp.get(timestamp)
                        if prior is not None:
                            spot_duplicate_rows += 1
                            comparable = ("Open", "High", "Low", "Close", "Ticker")
                            if any(prior.get(key) != spot_row.get(key) for key in comparable):
                                spot_conflicting_duplicates += 1
                        else:
                            spot_by_timestamp[timestamp] = spot_row
                            spot_source_by_timestamp[timestamp] = archive_path.name
                    row.update(
                        {
                            "spot_rows": spot_rows,
                            "spot_start": first_timestamp.isoformat(sep=" ")
                            if first_timestamp
                            else "",
                            "spot_end": last_timestamp.isoformat(sep=" ")
                            if last_timestamp
                            else "",
                        }
                    )
        except Exception as exc:  # record a bad archive but finish the audit
            row["status"] = "archive_error"
            archive_errors.append(
                f"{archive_path.name}: {type(exc).__name__}: {exc}"
            )

        row["sample_error_count"] = sum(
            error.startswith(f"{archive_path.name}:") for error in sample_errors
        )
        manifest.append(row)

    manifest_fields = [
        "archive",
        "expiry",
        "eligible",
        "status",
        "compressed_bytes",
        "uncompressed_bytes",
        "member_count",
        "option_member_count",
        "ce_member_count",
        "pe_member_count",
        "paired_strike_count",
        "ce_only_strike_count",
        "pe_only_strike_count",
        "min_strike",
        "max_strike",
        "spot_present",
        "spot_rows",
        "spot_start",
        "spot_end",
        "malformed_option_members",
        "mismatched_member_expiry",
        "sample_error_count",
    ]
    for row in manifest:
        row.setdefault("mismatched_member_expiry", "")
    write_csv(audit_root / "archive_manifest.csv", manifest, manifest_fields)

    sorted_spot = sorted(spot_by_timestamp.items())
    spot_output = processed_root / "nifty_spot_1min.csv.gz"
    with gzip.open(spot_output, "wt", encoding="utf-8", newline="") as handle:
        fields = EXPECTED_COLUMNS + ["SourceArchive"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for timestamp, spot_row in sorted_spot:
            output_row = {key: spot_row.get(key, "") for key in EXPECTED_COLUMNS}
            output_row["SourceArchive"] = spot_source_by_timestamp[timestamp]
            writer.writerow(output_row)

    dates = Counter(timestamp.date().isoformat() for timestamp, _ in sorted_spot)
    observations_per_day = sorted(dates.values())
    timestamps_by_date: dict[str, list[datetime]] = {}
    for timestamp, _ in sorted_spot:
        timestamps_by_date.setdefault(timestamp.date().isoformat(), []).append(timestamp)
    daily_coverage: list[dict[str, object]] = []
    for date, timestamps in sorted(timestamps_by_date.items()):
        regular = [
            timestamp
            for timestamp in timestamps
            if (timestamp.hour, timestamp.minute) >= (9, 15)
            and (timestamp.hour, timestamp.minute) <= (15, 30)
        ]
        daily_coverage.append(
            {
                "date": date,
                "observations": len(timestamps),
                "first_timestamp": timestamps[0].isoformat(sep=" "),
                "last_timestamp": timestamps[-1].isoformat(sep=" "),
                "regular_session_observations": len(regular),
                "regular_session_complete": len(regular) >= 370,
            }
        )
    write_csv(
        audit_root / "spot_daily_coverage.csv",
        daily_coverage,
        [
            "date",
            "observations",
            "first_timestamp",
            "last_timestamp",
            "regular_session_observations",
            "regular_session_complete",
        ],
    )
    partial_session_dates = [
        row for row in daily_coverage if not row["regular_session_complete"]
    ]
    eligible_rows = [row for row in manifest if row["eligible"]]
    ok_rows = [row for row in eligible_rows if row["status"] == "ok"]
    summary = {
        "data_root": str(data_root),
        "cutoff": args.cutoff,
        "total_archives": len(manifest),
        "eligible_archives": len(eligible_rows),
        "excluded_archives": sum(not row["eligible"] for row in manifest),
        "eligible_archives_ok": len(ok_rows),
        "eligible_compressed_bytes": sum(
            int(row["compressed_bytes"]) for row in eligible_rows
        ),
        "eligible_uncompressed_bytes": sum(
            int(row["uncompressed_bytes"] or 0) for row in eligible_rows
        ),
        "option_members": sum(
            int(row["option_member_count"] or 0) for row in eligible_rows
        ),
        "ce_members": sum(int(row["ce_member_count"] or 0) for row in eligible_rows),
        "pe_members": sum(int(row["pe_member_count"] or 0) for row in eligible_rows),
        "paired_strikes": sum(
            int(row["paired_strike_count"] or 0) for row in eligible_rows
        ),
        "ce_only_strikes": sum(
            int(row["ce_only_strike_count"] or 0) for row in eligible_rows
        ),
        "pe_only_strikes": sum(
            int(row["pe_only_strike_count"] or 0) for row in eligible_rows
        ),
        "archive_errors": archive_errors,
        "sample_errors": sample_errors,
        "spot_schema_variants": [
            {"columns": list(columns), "archive_count": count}
            for columns, count in schema_counts.items()
        ],
        "option_sample_schema_variants": [
            {"columns": list(columns), "sample_count": count}
            for columns, count in option_schema_counts.items()
        ],
        "spot_rows_scanned": sum(int(row["spot_rows"] or 0) for row in eligible_rows),
        "spot_unique_rows": len(sorted_spot),
        "spot_duplicate_rows": spot_duplicate_rows,
        "spot_conflicting_duplicates": spot_conflicting_duplicates,
        "spot_start": sorted_spot[0][0].isoformat(sep=" ") if sorted_spot else None,
        "spot_end": sorted_spot[-1][0].isoformat(sep=" ") if sorted_spot else None,
        "spot_trading_dates": len(dates),
        "spot_observations_per_day": {
            "min": min(observations_per_day) if observations_per_day else None,
            "median": statistics.median(observations_per_day)
            if observations_per_day
            else None,
            "max": max(observations_per_day) if observations_per_day else None,
        },
        "partial_regular_session_dates": partial_session_dates,
        "processed_spot_file": str(spot_output),
        "expected_columns": EXPECTED_COLUMNS,
    }
    with (audit_root / "audit_summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)

    report = f"""# NIFTY data audit

Generated from the raw Dropbox archives without modifying or extracting them.

## Coverage

- Total ZIP archives found: {summary['total_archives']}
- Eligible archives through November 2025: {summary['eligible_archives']}
- Eligible archives read successfully: {summary['eligible_archives_ok']}
- Excluded post-cutoff archives: {summary['excluded_archives']}
- Unique consolidated spot observations: {summary['spot_unique_rows']:,}
- Spot coverage: {summary['spot_start']} to {summary['spot_end']}
- Unique spot trading dates: {summary['spot_trading_dates']}

## Option inventory

- Option CSV members: {summary['option_members']:,}
- Call members: {summary['ce_members']:,}
- Put members: {summary['pe_members']:,}
- Paired call/put strikes: {summary['paired_strikes']:,}
- Call-only strikes: {summary['ce_only_strikes']:,}
- Put-only strikes: {summary['pe_only_strikes']:,}
- Eligible compressed size: {summary['eligible_compressed_bytes'] / 1024**2:,.1f} MB
- Eligible expanded size (not extracted): {summary['eligible_uncompressed_bytes'] / 1024**3:,.2f} GB

## Validation

- Archive errors: {len(archive_errors)}
- Sample/schema errors: {len(sample_errors)}
- Duplicate spot rows removed: {spot_duplicate_rows:,}
- Conflicting duplicate spot rows: {spot_conflicting_duplicates:,}
- Partial or special-session dates: {len(partial_session_dates)}
- Expected schema: {', '.join(EXPECTED_COLUMNS)}

## Generated files

- `data/audit/archive_manifest.csv`
- `data/audit/audit_summary.json`
- `data/audit/spot_daily_coverage.csv`
- `data/processed/nifty_spot_1min.csv.gz`

The raw ZIP archives remain the source of truth. The processed spot file is a
deduplicated working cache and includes the source archive for traceability.
"""
    (audit_root / "data_audit.md").write_text(report, encoding="utf-8")

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
