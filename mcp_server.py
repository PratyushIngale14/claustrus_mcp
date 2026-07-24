"""
Claustrus MCP Server — stdio transport.

Exposes the same grounding engine as server.py (the FastAPI REST API),
but over the real Model Context Protocol stdio transport, so Claude Code
and Claude Desktop can connect to it natively via mcpServers config.

This file does not touch server.py, the Dockerfile, or the Railway
deployment. It's a second, independent entry point into the same
claustrus package.

Run directly for local testing only (it will block waiting on stdio):
    python3 mcp_server.py
"""

from __future__ import annotations

import asyncio
import json

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from claustrus.grounding import ground
from claustrus.rules_healthcare import run_rules
from claustrus import audit_log
from server import _human_summary

app = Server("claustrus")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="ground",
            description=(
                "Ground an AI-generated rationale against provided source documents. "
                "Returns per-sentence citations, faithfulness score, "
                "PASSED/WARNING/BLOCKED status, and EU AI Act Article 12 fields."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "rationale": {
                        "type": "string",
                        "description": "The AI-generated text to verify.",
                    },
                    "sources": {
                        "type": "object",
                        "description": "Dict of {doc_id: full_text} — the citable ground truth.",
                        "additionalProperties": {"type": "string"},
                    },
                    "claim_id": {
                        "type": "string",
                        "description": "Optional identifier for audit logging.",
                    },
                    "anthropic_api_key": {
                        "type": "string",
                        "description": (
                            "Optional Anthropic key that enables a targeted semantic pass "
                            "on flagged sentences only."
                        ),
                    },
                },
                "required": ["rationale", "sources"],
            },
        ),
        Tool(
            name="ground_healthcare",
            description=(
                "Ground an AI claims decision rationale and run eight CMS 2026 compliance "
                "rules (CMS-0057-F, 42 CFR 438.404, SSA 1862(a)(1), WISeR model). Returns "
                "grounding citations plus R1-R8 compliance results."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "rationale": {
                        "type": "string",
                        "description": "The AI-generated text to verify.",
                    },
                    "sources": {
                        "type": "object",
                        "description": "Dict of {doc_id: full_text} — the citable ground truth.",
                        "additionalProperties": {"type": "string"},
                    },
                    "decision": {
                        "type": "string",
                        "description": "approve | deny | partial | pend",
                    },
                    "billed_amount": {
                        "type": "number",
                        "description": "Total billed amount in USD.",
                    },
                    "prior_authorization": {
                        "type": "string",
                        "description": "Auth number, or omit/null if none on file.",
                    },
                    "policy_clause_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of real clause IDs in the claim record, e.g. ['SEC-5.1', 'LCD-L33822'].",
                    },
                    "diagnosis_code": {
                        "type": "string",
                        "description": "ICD-10-CM code, e.g. R51.9.",
                    },
                    "procedure_code": {
                        "type": "string",
                        "description": "CPT code, e.g. 70553.",
                    },
                    "state": {
                        "type": "string",
                        "description": "US state for WISeR model check, e.g. TX.",
                    },
                    "claim_id": {
                        "type": "string",
                        "description": "Optional identifier for audit logging.",
                    },
                    "anthropic_api_key": {
                        "type": "string",
                        "description": (
                            "Optional Anthropic key that enables a targeted semantic pass "
                            "on flagged sentences only."
                        ),
                    },
                },
                "required": ["rationale", "sources", "decision", "billed_amount"],
            },
        ),
        Tool(
            name="download_logs",
            description=(
                "Return all audit log entries as formatted JSON. Each entry includes "
                "EU AI Act Article 12 fields."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


def _run_ground(arguments: dict) -> dict:
    return ground(
        rationale=arguments["rationale"],
        sources=arguments["sources"],
        api_key=arguments.get("anthropic_api_key"),
        claim_id=arguments.get("claim_id"),
    )


def _run_ground_healthcare(arguments: dict) -> dict:
    result = _run_ground(arguments)

    policy_text = " ".join(arguments["sources"].values())
    compliance = run_rules(
        decision=arguments["decision"],
        rationale=arguments["rationale"],
        billed_amount=arguments["billed_amount"],
        prior_authorization=arguments.get("prior_authorization"),
        policy_clause_ids=arguments.get("policy_clause_ids", []),
        policy_text=policy_text,
        diagnosis_code=arguments.get("diagnosis_code"),
        procedure_code=arguments.get("procedure_code"),
        state=arguments.get("state"),
    )
    result["compliance_rules"] = compliance

    if compliance["critical_count"] > 0 and result["status"] == "PASSED":
        result["status"] = "BLOCKED"

    return result


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "ground":
        result = _run_ground(arguments)
        result["human_summary"] = _human_summary(result)
        audit_log.write(result)
        return [TextContent(type="text", text=result["human_summary"])]

    if name == "ground_healthcare":
        result = _run_ground_healthcare(arguments)
        result["human_summary"] = _human_summary(result)
        audit_log.write(result)
        return [TextContent(type="text", text=result["human_summary"])]

    if name == "download_logs":
        entries = audit_log.read_all()
        return [TextContent(type="text", text=json.dumps(entries, indent=2))]

    raise ValueError(f"Unknown tool: {name}")


async def main() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
