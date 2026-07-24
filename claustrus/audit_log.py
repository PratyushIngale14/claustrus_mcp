"""
Claustrus MCP Server — audit log.

Writes one NDJSON line per grounding call to claustrus_audit.ndjson.
Format is structured for EU AI Act Article 12 compliance logging.
The /logs/download endpoint serves the file for download.

NDJSON (Newline-Delimited JSON) is the right format here:
- One JSON object per line, trivially appendable
- Standard format for audit trails (used by Datadog, Cloudwatch, Splunk)
- Can be filtered with grep, jq, or any log tool without parsing a giant array
- Each line is independently valid JSON so partial reads never corrupt the log
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path(os.environ.get("CLAUSTRUS_LOG_PATH", "/tmp/claustrus_audit.ndjson"))


def write(result: dict) -> None:
    """Append one audit entry to the NDJSON log file."""
    entry = {
        "log_schema": "claustrus-audit-v1",
        "logged_at_utc": datetime.now(timezone.utc).isoformat(),
        "claim_id": result.get("claim_id"),
        "faithfulness_score": result.get("faithfulness_score"),
        "status": result.get("status"),
        "sentences_checked": result.get("sentences_checked"),
        "sentences_grounded": result.get("sentences_grounded"),
        "sentences_flagged": result.get("sentences_flagged"),
        "method": result.get("method"),
        "latency_ms": result.get("latency_ms"),
        "source_docs_used": result.get("source_docs_used", []),
        "eu_ai_act": result.get("eu_ai_act_fields", {}),
        "compliance_rules": result.get("compliance_rules"),
        "citations_summary": [
            {
                "sentence": c["sentence"][:80],
                "grounded": c["grounded"],
                "overlap_score": c["overlap_score"],
                "source_doc": c.get("source_doc"),
            }
            for c in result.get("citations", [])
        ],
    }
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def read_all() -> list[dict]:
    """Read all audit entries from the log file."""
    if not LOG_PATH.exists():
        return []
    entries = []
    with LOG_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return entries


def clear() -> int:
    """Clear the log file. Returns number of entries deleted."""
    count = len(read_all())
    if LOG_PATH.exists():
        LOG_PATH.unlink()
    return count
