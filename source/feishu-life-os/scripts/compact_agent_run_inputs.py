from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(description="Compact oversized core_agent_runs.input_json records.")
    parser.add_argument("--db", default=".data/lifeos.sqlite3", help="SQLite database path.")
    parser.add_argument("--threshold-bytes", type=int, default=20_000, help="Only compact rows above this size.")
    parser.add_argument("--apply", action="store_true", help="Write changes. Default is dry-run.")
    parser.add_argument("--no-backup", action="store_true", help="Skip backup when --apply is used.")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"database not found: {db_path}")

    if args.apply and not args.no_backup:
        backup_path = db_path.with_suffix(db_path.suffix + f".bak-{datetime.now().strftime('%Y%m%d%H%M%S')}")
        shutil.copy2(db_path, backup_path)
        print(f"backup created: {backup_path}")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, capture_id, provider, model, created_at, length(input_json) AS input_len
            FROM core_agent_runs
            WHERE length(input_json) > ?
            ORDER BY input_len DESC
            """,
            (args.threshold_bytes,),
        ).fetchall()

        print(f"oversized rows: {len(rows)}")
        for row in rows:
            summary = _summary(row)
            print(f"- {row['id']} input_json={row['input_len']} bytes capture_id={row['capture_id']}")
            if args.apply:
                conn.execute(
                    "UPDATE core_agent_runs SET input_json=? WHERE id=?",
                    (json.dumps(summary, ensure_ascii=False), row["id"]),
                )
        if args.apply:
            conn.commit()
            print("compaction applied")
        else:
            print("dry-run only; pass --apply to update core_agent_runs.input_json")
    return 0


def _summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "compacted": True,
        "original_input_json_bytes": int(row["input_len"] or 0),
        "capture_id": row["capture_id"],
        "provider": row["provider"],
        "model": row["model"],
        "created_at": row["created_at"],
        "note": "Oversized recursive AgentRun input was replaced. Original capture/evidence rows are retained.",
    }


if __name__ == "__main__":
    raise SystemExit(main())
