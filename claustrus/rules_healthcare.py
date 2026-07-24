"""
Claustrus MCP Server — healthcare compliance rules.

Eight rules derived from real 2026 CMS regulations.
Each rule is independent of the grounding score.
Returns structured rule results — no prose, only typed dicts.
"""

from __future__ import annotations

import re
from typing import Optional


RULES_VERSION = "1.0.0-CMS2026"

RULE_SOURCES = {
    "R1": "42 CFR 438.404 / CMS-0057-F (January 2026)",
    "R2": "CMS-0057-F PA Final Rule / WISeR model (January 2026)",
    "R3": "CMS CERT Program / OIG Work Plan high-dollar audit priorities",
    "R4": "42 CFR 438.404(b)(3)",
    "R5": "CMS 2026 CPT updates (288 new codes) / ICD-10-CM updates (614 new codes, Oct 2025)",
    "R6": "SSA Section 1862(a)(1) / CMS Claims Processing Manual Chapter 1 Section 80",
    "R7": "42 CFR 438.404(b) / CMS-0057-F adverse determination notice requirements",
    "R8": "CMS enrollment and eligibility requirements / Qualigenix 2026 claims denial analysis",
}

WISR_STATES = {"NJ", "OH", "OK", "TX", "AZ", "WA",
               "New Jersey", "Ohio", "Oklahoma", "Texas", "Arizona", "Washington"}


def _extract_clause_ids(text: str) -> list[str]:
    """Pull any clause-like IDs from text: SEC-x.x, LCD-Lxxxxx, NCD, CPT codes."""
    return re.findall(
        r'\b(?:SEC|LCD|NCD|CPT|ICD|CFR|CMS|HCPCS)[-\s]?[\w.]+\b',
        text, re.IGNORECASE
    )


def run_rules(
    decision: str,
    rationale: str,
    billed_amount: float,
    prior_authorization: Optional[str],
    policy_clause_ids: list[str],
    policy_text: str,
    diagnosis_code: Optional[str] = None,
    procedure_code: Optional[str] = None,
    state: Optional[str] = None,
) -> dict:
    """
    Run all eight CMS-derived compliance rules.

    Args:
        decision: "approve" | "deny" | "partial" | "pend"
        rationale: the AI's natural-language justification
        billed_amount: total billed in dollars
        prior_authorization: auth number string or None
        policy_clause_ids: list of real clause IDs in the claim record
        policy_text: full text of all policy clauses concatenated
        diagnosis_code: ICD-10 code e.g. "R51.9"
        procedure_code: CPT code e.g. "70553"
        state: US state abbreviation or name (for WISeR model check)

    Returns structured dict of rule results.
    """
    results = []
    critical = 0
    warnings = 0
    info = 0

    rationale_lower = rationale.lower()
    policy_lower = policy_text.lower()
    cited_ids = _extract_clause_ids(rationale)

    # ---- R1: Denial must cite a real policy clause ----
    r1 = {"rule_id": "R1", "name": "DENIAL-CITATION-REQUIRED",
          "severity": "critical", "status": "PASS",
          "reason": None, "regulatory_source": RULE_SOURCES["R1"]}
    if decision.lower() == "deny":
        real_cited = [c for c in cited_ids
                      if any(c.upper() in p.upper() for p in policy_clause_ids)]
        if not real_cited:
            r1["status"] = "CRITICAL"
            r1["reason"] = (
                "Denial issued without citing a specific policy clause present in the record. "
                "Under 42 CFR 438.404 and CMS-0057-F, adverse determinations must reference "
                "the specific criteria or process used."
            )
            critical += 1
    results.append(r1)

    # ---- R2: Prior authorization required but missing ----
    r2 = {"rule_id": "R2", "name": "PRIOR-AUTHORIZATION-MISSING",
          "severity": "critical", "status": "PASS",
          "reason": None, "regulatory_source": RULE_SOURCES["R2"]}
    pa_terms = ["prior authorization", "pre-authorization", "preauth",
                "requires authorization", "pa required"]
    requires_pa = any(t in policy_lower for t in pa_terms)
    if requires_pa and not prior_authorization and decision.lower() == "approve":
        wisr_note = ""
        if state and any(s in state for s in WISR_STATES):
            wisr_note = (
                f" {state} is a WISeR model state — providers must submit PA "
                "or face pre-payment medical review."
            )
        r2["status"] = "CRITICAL"
        r2["reason"] = (
            "Policy requires prior authorization. No authorization number is on file. "
            "Under CMS-0057-F (January 2026), standard PA decisions must be made within "
            f"7 calendar days, expedited within 72 hours.{wisr_note}"
        )
        critical += 1
    results.append(r2)

    # ---- R3: High-dollar auto-approval ----
    r3 = {"rule_id": "R3", "name": "HIGH-DOLLAR-THRESHOLD",
          "severity": "warning", "status": "PASS",
          "reason": None, "regulatory_source": RULE_SOURCES["R3"]}
    if billed_amount >= 25000 and decision.lower() == "approve":
        r3["status"] = "WARNING"
        r3["reason"] = (
            f"Billed amount ${billed_amount:,.2f} meets or exceeds the $25,000 high-dollar "
            "review threshold. CMS CERT audit guidance and OIG Work Plan recommend pending "
            "high-dollar claims for manual review rather than auto-approving."
        )
        warnings += 1
    results.append(r3)

    # ---- R4: Fabricated policy reference ----
    r4 = {"rule_id": "R4", "name": "FABRICATED-POLICY-REFERENCE",
          "severity": "critical", "status": "PASS",
          "reason": None, "regulatory_source": RULE_SOURCES["R4"],
          "fabricated_refs": []}
    fabricated = [c for c in cited_ids
                  if not any(c.upper() in p.upper() for p in policy_clause_ids)
                  and c.upper() not in policy_text.upper()]
    if fabricated:
        r4["status"] = "CRITICAL"
        r4["fabricated_refs"] = fabricated
        r4["reason"] = (
            f"Rationale cites {fabricated} which do not appear in the provided policy "
            "documents. These are hallucinated policy references. Under 42 CFR 438.404(b)(3), "
            "denials must cite real criteria — a fabricated reference creates legal exposure."
        )
        critical += 1
    results.append(r4)

    # ---- R5: Coding accuracy (2026 CPT/ICD-10 format check) ----
    r5 = {"rule_id": "R5", "name": "CODING-ACCURACY-CHECK",
          "severity": "warning", "status": "PASS",
          "reason": None, "regulatory_source": RULE_SOURCES["R5"]}
    if procedure_code and not re.match(r'^\d{5}[A-Z0-9]?$', procedure_code.strip()):
        r5["status"] = "WARNING"
        r5["reason"] = (
            f"Procedure code '{procedure_code}' format does not match standard CPT structure. "
            "288 new CPT codes became effective January 1 2026. Coding errors cause 32% of "
            "claim denials per 2026 CMS analysis."
        )
        warnings += 1
    if diagnosis_code and not re.match(r'^[A-Z]\d{2}(\.\d+)?$', diagnosis_code.strip()):
        r5["status"] = "WARNING"
        r5["reason"] = (r5.get("reason") or "") + (
            f" Diagnosis code '{diagnosis_code}' format does not match ICD-10-CM structure. "
            "614 new ICD-10-CM codes became effective October 1 2025."
        )
        warnings += 1
    results.append(r5)

    # ---- R6: Medical necessity not documented ----
    r6 = {"rule_id": "R6", "name": "MEDICAL-NECESSITY-DOCUMENTATION",
          "severity": "critical", "status": "PASS",
          "reason": None, "regulatory_source": RULE_SOURCES["R6"]}
    necessity_terms = ["medically necessary", "medical necessity", "reasonable and necessary",
                       "reasonable and customary", "clinically indicated"]
    asserts_necessity = any(t in rationale_lower for t in necessity_terms)
    necessity_in_policy = any(t in policy_lower for t in necessity_terms)
    if asserts_necessity and not necessity_in_policy:
        r6["status"] = "CRITICAL"
        r6["reason"] = (
            "Medical necessity asserted in rationale but no necessity criteria appear in the "
            "provided policy documents. Under SSA Section 1862(a)(1) and CMS Claims Processing "
            "Manual Chapter 1 Section 80, necessity determinations require documented criteria."
        )
        critical += 1
    results.append(r6)

    # ---- R7: Appeal rights not referenced in denial ----
    r7 = {"rule_id": "R7", "name": "APPEAL-RIGHTS-NOTICE",
          "severity": "info", "status": "PASS",
          "reason": None, "regulatory_source": RULE_SOURCES["R7"]}
    if decision.lower() == "deny":
        appeal_terms = ["appeal", "grievance", "expedited review", "right to appeal"]
        if not any(t in rationale_lower for t in appeal_terms):
            r7["status"] = "INFO"
            r7["reason"] = (
                "Denial does not reference member appeal rights. Under 42 CFR 438.404(b), "
                "adverse determination notices must include appeal procedures, rights to "
                "evidence copies, and expedited review options."
            )
            info += 1
    results.append(r7)

    # ---- R8: Eligibility verification missing ----
    r8 = {"rule_id": "R8", "name": "ELIGIBILITY-VERIFICATION",
          "severity": "warning", "status": "PASS",
          "reason": None, "regulatory_source": RULE_SOURCES["R8"]}
    eligibility_terms = ["eligib", "enrollment", "member status", "coverage period",
                         "active member", "plan member"]
    has_eligibility = any(t in rationale_lower for t in eligibility_terms)
    if not has_eligibility:
        r8["status"] = "WARNING"
        r8["reason"] = (
            "Eligibility verification not referenced in rationale or claim record. "
            "Patient eligibility errors account for 56% of claim denials per 2026 "
            "CMS claims analysis."
        )
        warnings += 1
    results.append(r8)

    # ---- Overall status ----
    if critical > 0:
        overall = "BLOCKED"
    elif warnings > 0:
        overall = "NEEDS_REVIEW"
    else:
        overall = "PASSED"

    return {
        "rules_version": RULES_VERSION,
        "overall_status": overall,
        "critical_count": critical,
        "warning_count": warnings,
        "info_count": info,
        "rules": results,
    }
