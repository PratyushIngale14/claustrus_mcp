# Claustrus MCP Server

**Universal grounding and citation verification for AI agent outputs.**

A hosted MCP server that checks every sentence of an AI-generated rationale
against provided source documents, cites what is grounded, flags what is not,
and logs the full audit trail in EU AI Act Article 12 compatible format.

---

## Endpoints

| Endpoint | Method | What it does |
|---|---|---|
| `/ground` | POST | Core grounding — any domain, any source documents |
| `/ground/healthcare` | POST | Grounding + 8 CMS 2026 compliance rules |
| `/logs/download?format=ndjson` | GET | Download full audit log as NDJSON |
| `/logs/download?format=json` | GET | Download full audit log as JSON array |
| `/logs/summary` | GET | Summary stats of all logged calls |
| `/health` | GET | Health check |
| `/.well-known/mcp.json` | GET | MCP manifest for client discovery |

---

## Run locally

```bash
git clone https://github.com/PratyushIngale14/claustrus-mcp.git
cd claustrus-mcp
pip install -r requirements.txt
cp .env.example .env   # fill in values
uvicorn server:app --reload
```

Server starts at http://localhost:8000

Interactive API docs at http://localhost:8000/docs

---

## Connect from Claude Code

Add to your Claude Code MCP config:

```json
{
  "mcpServers": {
    "claustrus": {
      "url": "https://your-railway-url.railway.app",
      "headers": {
        "x-api-key": "your-key"
      }
    }
  }
}
```

---

## Example — core grounding call

```bash
curl -X POST https://your-railway-url.railway.app/ground \
  -H "Content-Type: application/json" \
  -H "x-api-key: your-key" \
  -d '{
    "rationale": "The MRI is covered under advanced imaging services. The headache diagnosis supports medical necessity.",
    "sources": {
      "SEC-5.1": "Advanced imaging including MRI requires prior authorization before the date of service."
    },
    "claim_id": "CLM-1001"
  }'
```

Response includes:
- `faithfulness_score` — 0.0 to 1.0
- `status` — PASSED / WARNING / BLOCKED
- `citations` — per-sentence grounding with source excerpt and reason
- `eu_ai_act_fields` — Article 12 compliant logging fields
- `human_summary` — clean readable summary
- `latency_ms` — end-to-end processing time

---

## Example — healthcare compliance call

```bash
curl -X POST https://your-railway-url.railway.app/ground/healthcare \
  -H "Content-Type: application/json" \
  -H "x-api-key: your-key" \
  -d '{
    "rationale": "The MRI is approved. It is covered under advanced imaging services.",
    "sources": {"SEC-5.1": "Advanced imaging including MRI requires prior authorization."},
    "claim_id": "CLM-1001",
    "decision": "approve",
    "billed_amount": 2400,
    "prior_authorization": null,
    "policy_clause_ids": ["SEC-5.1"],
    "diagnosis_code": "R51.9",
    "procedure_code": "70553",
    "state": "TX"
  }'
```

Returns grounding result plus eight compliance rule results (R1-R8),
each with the exact CMS regulatory source that the rule is derived from.

---

## Download audit logs

```bash
# NDJSON — one line per call, best for log tools (grep, jq, Splunk)
curl https://your-url/logs/download?format=ndjson \
  -H "x-api-key: your-key" -o audit.ndjson

# JSON array — best for manual review or spreadsheet import
curl https://your-url/logs/download?format=json \
  -H "x-api-key: your-key" -o audit.json
```

Each log entry includes the EU AI Act Article 12 required fields:
input data hash, system ID, faithfulness score, status, and whether
human oversight is required.

---

## EU AI Act Article 12 compliance

Every call produces an `eu_ai_act_fields` block:

```json
{
  "article": "Article 12 — Record-keeping",
  "input_data_hash": "sha256 of the rationale text",
  "system_id": "claustrus-core-v1.0.0",
  "faithfulness_score": 0.67,
  "status": "BLOCKED",
  "human_oversight_required": true,
  "timestamp_utc": "2026-07-24T05:00:00+00:00"
}
```

This is logged to a persistent NDJSON file and downloadable on demand.
The combination of input hash, system ID, score, status, and timestamp
provides the evidence trail Article 12 requires for high-risk AI systems.

---

## Deploy to Railway (recommended, $5/month hobby tier)

1. Push this repo to GitHub
2. Go to railway.app, create new project from GitHub repo
3. Set environment variables: CLAUSTRUS_API_KEY, ANTHROPIC_API_KEY
4. Deploy — Railway detects the Dockerfile automatically
5. Your MCP server is live at the Railway-provided URL

---

## Token efficiency

The deterministic token-overlap grounding pass costs zero API tokens.
Claude is only called when:
- `anthropic_api_key` is provided in the request, AND
- At least one sentence is flagged as unsupported

In that case, Claude is called once per flagged sentence using
claude-haiku (fastest, cheapest model) with an 80-token max response.

Typical call costs:
- No API key: 0 tokens, under 1ms latency
- With API key, all sentences grounded: 0 tokens
- With API key, one flagged sentence: ~150 tokens, 300-500ms

---

## License

MIT. Use it, build on it, contribute back.

## Author

Pratyush Ingale
github.com/PratyushIngale14
