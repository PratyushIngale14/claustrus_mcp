"""
Claustrus MCP Server — FastAPI application.

Endpoints:
  POST /ground              — core grounding tool (no domain rules)
  POST /ground/healthcare   — grounding + CMS 2026 healthcare compliance rules
  GET  /logs/download       — download full audit log as NDJSON
  GET  /logs/summary        — summary stats of all logged calls
  GET  /health              — health check

MCP manifest:
  GET  /.well-known/mcp.json — MCP server manifest for client discovery

Response format is always structured JSON — never prose.
A human-readable summary is included as a separate field for display.
The full structured result is what gets logged and downloaded.
"""

from __future__ import annotations

import io
import json
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

from claustrus.grounding import ground
from claustrus.rules_healthcare import run_rules
from claustrus import audit_log

app = FastAPI(
    title="Claustrus MCP Server",
    description="Universal grounding and citation verification for AI agent outputs.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

API_KEY = os.environ.get("CLAUSTRUS_API_KEY", "")


def _check_key(x_api_key: Optional[str]) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


def _human_summary(result: dict) -> str:
    """Generate a clean, compact human-readable summary from the structured result."""
    score = result["faithfulness_score"]
    status = result["status"]
    sc = result["sentences_checked"]
    sg = result["sentences_grounded"]
    sf = result["sentences_flagged"]

    lines = [
        f"GROUNDING RESULT — {status}",
        f"Faithfulness: {score:.2f} ({sg}/{sc} sentences grounded, {sf} flagged)",
        "",
    ]

    flagged = [c for c in result.get("citations", []) if not c["grounded"]]
    if flagged:
        lines.append("Flagged sentences:")
        for c in flagged:
            lines.append(f"  UNSUPPORTED ({c['overlap_score']:.2f}): \"{c['sentence'][:80]}\"")
            lines.append(f"    Reason: {c['reason']}")
        lines.append("")

    grounded = [c for c in result.get("citations", []) if c["grounded"]]
    if grounded:
        lines.append("Grounded sentences:")
        for c in grounded:
            lines.append(f"  GROUNDED ({c['overlap_score']:.2f}): \"{c['sentence'][:80]}\"")
            if c.get("source_excerpt"):
                lines.append(f"    Cited from [{c['source_doc']}]: \"{c['source_excerpt'][:100]}\"")
        lines.append("")

    cr = result.get("compliance_rules")
    if cr:
        lines.append(f"COMPLIANCE RULES — {cr['overall_status']}")
        lines.append(f"Critical: {cr['critical_count']} | Warnings: {cr['warning_count']} | Info: {cr['info_count']}")
        for r in cr["rules"]:
            if r["status"] != "PASS":
                lines.append(f"  {r['status']} [{r['rule_id']}] {r['name']}: {r['reason']}")
                lines.append(f"    Source: {r['regulatory_source']}")
        lines.append("")

    lines.append(f"Method: {result['method']} | Latency: {result['latency_ms']}ms")
    lines.append(f"EU AI Act Article 12: input_hash={result['eu_ai_act_fields']['input_data_hash']}")
    return "\n".join(lines)


# ---- Request models ----

class GroundRequest(BaseModel):
    rationale: str = Field(description="The AI-generated text to verify.")
    sources: dict[str, str] = Field(
        description="Dict of {doc_id: full_text}. The citable ground truth."
    )
    claim_id: Optional[str] = Field(default=None, description="Optional identifier for audit logging.")
    anthropic_api_key: Optional[str] = Field(
        default=None,
        description="Optional Anthropic key for targeted semantic pass on flagged sentences only."
    )


class HealthcareGroundRequest(GroundRequest):
    decision: str = Field(description="approve | deny | partial | pend")
    billed_amount: float = Field(description="Total billed amount in USD.")
    prior_authorization: Optional[str] = Field(default=None, description="Auth number or null.")
    policy_clause_ids: list[str] = Field(
        default_factory=list,
        description="List of real clause IDs in the claim record e.g. ['SEC-5.1', 'LCD-L33822']"
    )
    diagnosis_code: Optional[str] = Field(default=None, description="ICD-10-CM code e.g. R51.9")
    procedure_code: Optional[str] = Field(default=None, description="CPT code e.g. 70553")
    state: Optional[str] = Field(default=None, description="US state for WISeR model check e.g. TX")


# ---- Core endpoints ----

@app.post("/ground")
async def ground_endpoint(
    req: GroundRequest,
    x_api_key: Optional[str] = Header(default=None),
):
    _check_key(x_api_key)
    result = ground(
        rationale=req.rationale,
        sources=req.sources,
        api_key=req.anthropic_api_key,
        claim_id=req.claim_id,
    )
    result["human_summary"] = _human_summary(result)
    audit_log.write(result)
    return result


@app.post("/ground/healthcare")
async def ground_healthcare_endpoint(
    req: HealthcareGroundRequest,
    x_api_key: Optional[str] = Header(default=None),
):
    _check_key(x_api_key)

    result = ground(
        rationale=req.rationale,
        sources=req.sources,
        api_key=req.anthropic_api_key,
        claim_id=req.claim_id,
    )

    policy_text = " ".join(req.sources.values())
    compliance = run_rules(
        decision=req.decision,
        rationale=req.rationale,
        billed_amount=req.billed_amount,
        prior_authorization=req.prior_authorization,
        policy_clause_ids=req.policy_clause_ids,
        policy_text=policy_text,
        diagnosis_code=req.diagnosis_code,
        procedure_code=req.procedure_code,
        state=req.state,
    )
    result["compliance_rules"] = compliance

    if compliance["critical_count"] > 0 and result["status"] == "PASSED":
        result["status"] = "BLOCKED"

    result["human_summary"] = _human_summary(result)
    audit_log.write(result)
    return result


# ---- Log endpoints ----

@app.get("/logs/download")
async def download_logs(
    format: str = "ndjson",
    x_api_key: Optional[str] = Header(default=None),
):
    """Download the full audit log.
    ?format=ndjson  — one JSON object per line (default, best for log tools)
    ?format=json    — a JSON array (best for spreadsheets / manual review)
    """
    _check_key(x_api_key)
    entries = audit_log.read_all()

    if format == "json":
        content = json.dumps(entries, indent=2)
        media_type = "application/json"
        filename = f"claustrus_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    else:
        content = "\n".join(json.dumps(e) for e in entries)
        media_type = "application/x-ndjson"
        filename = f"claustrus_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.ndjson"

    return StreamingResponse(
        io.BytesIO(content.encode()),
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/logs/summary")
async def log_summary(x_api_key: Optional[str] = Header(default=None)):
    """Summary stats of all logged grounding calls."""
    _check_key(x_api_key)
    entries = audit_log.read_all()
    if not entries:
        return {"total_calls": 0}
    scores = [e["faithfulness_score"] for e in entries if e.get("faithfulness_score") is not None]
    statuses = [e["status"] for e in entries if e.get("status")]
    return {
        "total_calls": len(entries),
        "avg_faithfulness_score": round(sum(scores) / len(scores), 3) if scores else None,
        "status_breakdown": {
            "PASSED": statuses.count("PASSED"),
            "WARNING": statuses.count("WARNING"),
            "BLOCKED": statuses.count("BLOCKED"),
            "NEEDS_REVIEW": statuses.count("NEEDS_REVIEW"),
        },
        "first_call_utc": entries[0].get("logged_at_utc"),
        "last_call_utc": entries[-1].get("logged_at_utc"),
    }


# ---- Health + MCP manifest ----

@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0", "timestamp_utc": datetime.now(timezone.utc).isoformat()}


@app.get("/.well-known/mcp.json")
async def mcp_manifest():
    """MCP server manifest for client auto-discovery."""
    return {
        "schema_version": "1.0",
        "name": "claustrus",
        "display_name": "Claustrus — Universal Grounding Layer",
        "description": "Verifies AI outputs against source documents sentence by sentence. Cites what is grounded. Flags what is not. EU AI Act Article 12 audit logging included.",
        "version": "1.0.0",
        "tools": [
            {
                "name": "ground",
                "description": "Ground an AI-generated rationale against provided source documents. Returns per-sentence citations, a faithfulness score, and EU AI Act Article 12 fields.",
                "endpoint": "/ground",
                "method": "POST",
            },
            {
                "name": "ground_healthcare",
                "description": "Ground an AI claims decision rationale and run eight CMS 2026 compliance rules. Returns grounding citations plus R1-R8 compliance results.",
                "endpoint": "/ground/healthcare",
                "method": "POST",
            },
        ],
    }
