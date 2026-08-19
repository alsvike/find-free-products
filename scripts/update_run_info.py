#!/usr/bin/env python3
import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


COMPLETED_STATUSES = {
    "ok",
    "invalid_domain",
    "robots_disallowed",
    "external_redirect",
    "homepage_not_found",
}


def read_semicolon_csv(path: Path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle, delimiter=";"))


def count_input_domains(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(8192)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
            dialect.delimiter = ";"
        reader = csv.DictReader(handle, dialect=dialect)
        if not reader.fieldnames:
            return 0
        lower = {name.lower(): name for name in reader.fieldnames}
        column = next(
            (lower[name] for name in ("web_domain", "domain", "website", "url") if name in lower),
            reader.fieldnames[0],
        )
        return len({str(row.get(column, "")).strip().lower() for row in reader if row.get(column)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--findings", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    findings = read_semicolon_csv(args.findings)
    status_rows = read_semicolon_csv(args.status)
    latest = {}
    for row in status_rows:
        domain = str(row.get("domain", "") or "").strip().lower()
        if domain:
            latest[domain] = row

    status_counts = Counter(str(row.get("status", "") or "") for row in latest.values())
    completed = sum(
        1 for row in latest.values() if str(row.get("status", "") or "") in COMPLETED_STATUSES
    )
    pages = sum(int(row.get("pages_checked", 0) or 0) for row in latest.values())
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_shops": count_input_domains(args.input),
        "recorded_shops": len(latest),
        "completed_shops": completed,
        "pages_checked": pages,
        "findings": len(findings),
        "status_counts": dict(sorted(status_counts.items())),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)


if __name__ == "__main__":
    main()

