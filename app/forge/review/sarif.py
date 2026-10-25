"""SARIF 2.1.0 (Static Analysis Results Interchange Format) exporter for FORGE review findings."""

import json
from typing import Any, Dict, List, Optional

from forge import __version__
from forge.config import redact_secrets
from forge.review.models import ReviewFinding, ReviewReport
from forge.review.policy_models import PolicyEvaluationReport, FindingPolicyAction
from forge.schemas import FindingSeverity

SARIF_SCHEMA_URI = "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"
SARIF_VERSION = "2.1.0"

# Mapping FORGE FindingSeverity to SARIF result levels: error, warning, note, none
SEVERITY_TO_SARIF_LEVEL = {
    FindingSeverity.CRITICAL: "error",
    FindingSeverity.HIGH: "error",
    FindingSeverity.MEDIUM: "warning",
    FindingSeverity.LOW: "note",
    FindingSeverity.INFO: "note",
}


def review_report_to_sarif(
    report: ReviewReport,
    policy_report: Optional[PolicyEvaluationReport] = None,
    indent: int = 2,
) -> str:
    """Serialize ReviewReport to standards-compliant SARIF 2.1.0 JSON format.

    Guarantees:
    - Standards-compliant SARIF 2.1.0 schema structure.
    - Deterministic ordering of rules and results.
    - Relative artifact URIs without host machine paths.
    - Verified line and column regions (omits non-existent or invalid locations).
    - Attached evidence and confidence recorded in property bags (`properties`).
    - Accurate suppression metadata if a policy suppression was applied.
    - Secret redaction across all text fields and messages.
    """
    # 1. Map suppressions if policy report provided
    suppressed_finding_ids: Dict[str, str] = {}
    if policy_report:
        for dec in policy_report.decisions:
            if dec.action == FindingPolicyAction.SUPPRESSED and dec.suppression_applied:
                suppressed_finding_ids[dec.finding_id] = dec.suppression_applied.reason

    # 2. Collect unique rules deterministically
    rule_map: Dict[str, Dict[str, Any]] = {}
    for f in sorted(report.findings, key=lambda x: x.rule_id):
        if f.rule_id not in rule_map:
            rule_map[f.rule_id] = {
                "id": f.rule_id,
                "name": f.rule_id,
                "shortDescription": {
                    "text": f.title.split(" at ")[0].strip(),
                },
                "fullDescription": {
                    "text": f.observed_evidence,
                },
                "help": {
                    "text": f"{f.inference}\n\nRecommendation: {f.recommendation}",
                    "markdown": f"**Inference:** {f.inference}\n\n**Recommendation:** {f.recommendation}",
                },
                "properties": {
                    "category": f.category.value,
                    "defaultSeverity": f.severity.value,
                },
            }

    rules_list = list(rule_map.values())

    # 3. Build SARIF Results
    results: List[Dict[str, Any]] = []
    for f in report.findings:
        level = SEVERITY_TO_SARIF_LEVEL.get(f.severity, "warning")

        result_obj: Dict[str, Any] = {
            "ruleId": f.rule_id,
            "level": level,
            "message": {
                "text": f"{f.title}: {f.explanation}",
            },
            "properties": {
                "findingId": f.finding_id,
                "category": f.category.value,
                "severity": f.severity.value,
                "confidence": f.confidence.value,
                "lifecycleState": f.lifecycle_state.value,
                "evidenceStatus": f.evidence_status.value,
            },
        }

        # Add location if file path is a valid repository relative path
        if f.file_path and f.file_path != "<commit_message>":
            norm_uri = f.file_path.replace("\\", "/").lstrip("/")
            location_entry: Dict[str, Any] = {
                "physicalLocation": {
                    "artifactLocation": {
                        "uri": norm_uri,
                        "uriBaseId": "%SRCROOT%",
                    },
                }
            }
            if f.line_number is not None and f.line_number >= 1:
                region: Dict[str, Any] = {
                    "startLine": f.line_number,
                    "startColumn": 1,
                }
                if f.end_line_number is not None and f.end_line_number >= f.line_number:
                    region["endLine"] = f.end_line_number
                if f.code_snippet:
                    region["snippet"] = {
                        "text": f.code_snippet,
                    }
                location_entry["physicalLocation"]["region"] = region

            result_obj["locations"] = [location_entry]

        # Add dataflow path if available
        if f.dataflow_path:
            result_obj["properties"]["dataflowPath"] = f.dataflow_path

        # Add suppression if applied
        if f.finding_id in suppressed_finding_ids:
            result_obj["suppressions"] = [
                {
                    "kind": "external",
                    "justification": suppressed_finding_ids[f.finding_id],
                }
            ]

        results.append(result_obj)

    sarif_doc: Dict[str, Any] = {
        "$schema": SARIF_SCHEMA_URI,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "FORGE Review",
                        "version": __version__,
                        "informationUri": "https://github.com/thisisanimesh01/FORGE",
                        "rules": rules_list,
                    }
                },
                "artifacts": [
                    {
                        "location": {
                            "uri": f.replace("\\", "/").lstrip("/"),
                            "uriBaseId": "%SRCROOT%",
                        }
                    }
                    for f in sorted(report.files_reviewed)
                    if f and f != "<commit_message>"
                ],
                "results": results,
                "invocations": [
                    {
                        "executionSuccessful": True,
                        "endTimeUtc": report.created_at,
                    }
                ],
            }
        ],
    }

    raw_json = json.dumps(sarif_doc, indent=indent, sort_keys=True)
    return redact_secrets(raw_json)
