from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Dict

_LAST_CALL_TIME = 0.0

ALLOWED = {"TIMEOUT_CHANGE", "TIMEOUT_STRATEGY", "EXCLUDE", "MANUAL_REVIEW"}
OUTPUT_FIELDS = [
    "issue_ID", "classification", "within_research_scope", "contains_timeout_change",
    "contains_timeout_strategy", "reason", "evidence", "needs_manual_review",
    "uncertainties", "processing_status", "error_message", "model_name", "prompt_version",
    "priority_score",
]

SCORE_COLUMN_CANDIDATES = ("selection_score", "priority_score", "score")
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "classification": {"type": "string", "enum": sorted(ALLOWED)},
        "within_research_scope": {"type": "boolean", "nullable": True},
        "contains_timeout_change": {"type": "boolean", "nullable": True},
        "contains_timeout_strategy": {"type": "boolean", "nullable": True},
        "reason": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "needs_manual_review": {"type": "boolean"},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["classification", "within_research_scope", "contains_timeout_change",
                 "contains_timeout_strategy", "reason", "evidence", "needs_manual_review",
                 "uncertainties"],
}

PROMPT_VERSION = "eligibility-v1"
SYSTEM_INSTRUCTION = """You are Agent 1, a strict eligibility classifier for research on heuristics for setting RPC timeouts in Java Apache server components. Return JSON only, conforming exactly to the supplied schema. Do not use external knowledge.

Scope: server-to-server/internal RPCs, including a server daemon acting as RPC client (replication, recovery, control plane). Exclude external app/browser/UI/REST/CLI clients; tests/builds/CI; non-RPC operations; and timeout errors with no relevant timeout decision or reusable strategy.

Decision order: (1) determine scope; if genuinely indeterminate, MANUAL_REVIEW; (2) outside scope => EXCLUDE; (3) relevant RPC timeout configuration change (numeric or structural, configurable/default/fallback/infinite-to-finite) => TIMEOUT_CHANGE; (4) otherwise reusable reasoning for selecting/designing an RPC timeout => TIMEOUT_STRATEGY; otherwise EXCLUDE. A change wins over strategy, but mark both true when both occur.

Use MANUAL_REVIEW only for genuine ambiguity/incompleteness, not small omissions. Evidence must be short, exact excerpts copied from the supplied issue. Never infer callers, timeout values, implementation, or rationale. Positive classifications require direct evidence. A proposal is not implementation unless stated as such."""


def allow_large_csv_fields() -> None:
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


def issue_id_column(fieldnames: Iterable[str] | None) -> str:
    names = list(fieldnames or [])
    for name in ("issue_ID", "issue_id", "key"):
        if name in names:
            return name
    raise ValueError("Input CSV must contain issue_ID, issue_id, or key")


def collect_issue_text(row: dict[str, str], issue_id: str) -> str:
    labels = [
        ("project", "Project"), ("project_key", "Project key"), ("project_name", "Project name"),
        ("title", "Title"), ("summary", "Summary"), ("description", "Description"),
        ("discussion", "Discussion"), ("comments", "Comments"), ("comments_text", "Comments"),
        ("importance", "Importance score"),
    ]
    parts = [f"Issue ID: {issue_id}"]
    for key, label in labels:
        value = (row.get(key) or "").strip()
        if value:
            parts.append(f"{label}:\n{value}")
    return "\n\n".join(parts)


def score_column(fieldnames: Iterable[str] | None) -> str | None:
    names = list(fieldnames or [])
    for name in SCORE_COLUMN_CANDIDATES:
        if name in names:
            return name
    return None


def row_score(row: dict[str, str], column: str | None) -> float | None:
    if not column:
        return None
    raw = (row.get(column) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def build_prompt(row: dict[str, str], issue_id: str) -> str:
    return "Classify this issue using the policy. Importance is retrieval-only, not a decision rule.\n\n" + collect_issue_text(row, issue_id)


def validate_result(result: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = RESPONSE_SCHEMA["required"]
    for key in required:
        if key not in result:
            errors.append(f"missing required field: {key}")
    if errors:
        return errors
    classification = result["classification"]
    if classification not in ALLOWED:
        errors.append("invalid classification")
    for key in ("within_research_scope", "contains_timeout_change", "contains_timeout_strategy"):
        if result[key] is not None and type(result[key]) is not bool:
            errors.append(f"{key} must be boolean or null")
    if type(result["needs_manual_review"]) is not bool:
        errors.append("needs_manual_review must be boolean")
    for key in ("reason",):
        if not isinstance(result[key], str):
            errors.append(f"{key} must be a string")
    for key in ("evidence", "uncertainties"):
        if not isinstance(result[key], list) or not all(isinstance(x, str) for x in result[key]):
            errors.append(f"{key} must be a list of strings")
    if classification == "MANUAL_REVIEW" and result["needs_manual_review"] is not True:
        errors.append("MANUAL_REVIEW requires needs_manual_review=true")
    if classification == "TIMEOUT_CHANGE" and result["contains_timeout_change"] is not True:
        errors.append("TIMEOUT_CHANGE requires contains_timeout_change=true")
    if classification == "TIMEOUT_STRATEGY" and result["contains_timeout_strategy"] is not True:
        errors.append("TIMEOUT_STRATEGY requires contains_timeout_strategy=true")
    if classification in {"TIMEOUT_CHANGE", "TIMEOUT_STRATEGY"} and not result["evidence"]:
        errors.append("positive classification requires evidence")
    if classification == "EXCLUDE" and result["contains_timeout_change"] is True and "outside" not in result["reason"].lower():
        errors.append("EXCLUDE cannot claim a timeout change without explaining it is outside scope")
    return errors


def is_transient(error: Exception) -> bool:
    text = str(error).lower()
    return any(token in text for token in ("429", "500", "502", "503", "504", "resource exhausted", "timeout", "temporar", "connection"))

def call_gemini_with_retry(
    client: Any,
    model: str,
    config: Any,
    prompt: str,
    max_attempts: int = 5,
    min_interval: float = 4.2,  
) -> Dict[str, Any]:
    global _LAST_CALL_TIME
    last_error: Exception | None = None

    for attempt in range(max_attempts):
        elapsed = time.time() - _LAST_CALL_TIME
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)

        try:
            _LAST_CALL_TIME = time.time()

            response = client.models.generate_content(
                model=model, contents=prompt, config=config
            )

            parsed = getattr(response, "parsed", None)
            if parsed is None:
                parsed = json.loads(response.text)

            if not isinstance(parsed, dict):
                raise ValueError("Gemini response JSON is not an object")

            errors = validate_result(parsed)
            if errors:
                raise ValueError("invalid Gemini response: " + "; ".join(errors))

            return parsed

        except Exception as exc:
            last_error = exc
            if not is_transient(exc) or attempt == max_attempts - 1:
                break

            backoff = (2**attempt) * 2 + random.uniform(0.5, 1.5)
            time.sleep(backoff)

    raise RuntimeError(
        f"Gemini request failed after {max_attempts} attempt(s): {last_error}"
    )

def completed_ids(path: Path) -> set[str]:
    if not path.exists() or path.stat().st_size == 0:
        return set()
    with path.open("r", newline="", encoding="utf-8") as handle:
        return {row.get("issue_ID", "") for row in csv.DictReader(handle) if row.get("issue_ID")}


def output_row(issue_id: str, model: str, prompt_version: str, result: dict[str, Any] | None = None,
                error: Exception | None = None, status: str | None = None, score: float | None = None) -> dict[str, Any]:
    result = result or {}
    return {"issue_ID": issue_id, "classification": result.get("classification", ""),
            "within_research_scope": result.get("within_research_scope", ""), "contains_timeout_change": result.get("contains_timeout_change", ""),
            "contains_timeout_strategy": result.get("contains_timeout_strategy", ""), "reason": result.get("reason", ""),
            "evidence": json.dumps(result.get("evidence", []), ensure_ascii=False), "needs_manual_review": result.get("needs_manual_review", ""),
            "uncertainties": json.dumps(result.get("uncertainties", []), ensure_ascii=False),
            "processing_status": status or ("error" if error else "success"), "error_message": str(error or ""),
            "model_name": model, "prompt_version": prompt_version,
            "priority_score": "" if score is None else score}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path); parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", default="gemini-3.1-flash-lite-preview")
    parser.add_argument("--max-rows", type=int); parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--api-key-env", default="GEMINI_API_KEY"); parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--prompt-version", default=PROMPT_VERSION)
    parser.add_argument("--min-score", type=float, default=None,
                         help="Reject rows whose priority/selection score is below this threshold "
                              "without calling the model (saved with processing_status=skipped_low_priority). "
                              "Rows with a missing/unparseable score are kept, not rejected.")
    parser.add_argument("--score-column", default=None,
                         help="Name of the CSV column holding the priority/relevance score. "
                              "Auto-detected from selection_score/priority_score/score if not given.")
    args = parser.parse_args()
    api_key = os.getenv(args.api_key_env)
    if not api_key:
        parser.error(f"environment variable {args.api_key_env} is not set")
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        parser.error("Install the SDK: pip install -U google-genai")
    if args.overwrite and args.output.exists():
        args.output.unlink()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    allow_large_csv_fields()
    existing = completed_ids(args.output)
    client = genai.Client(api_key=api_key)
    config = types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION,
        response_mime_type="application/json", response_json_schema=RESPONSE_SCHEMA, temperature=0)
    with args.input.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source); id_col = issue_id_column(reader.fieldnames)
        score_col = args.score_column or score_column(reader.fieldnames)
        all_rows = list(reader)
    if score_col:
        all_rows = sorted(
            all_rows,
            key=lambda r: (row_score(r, score_col) is None, -(row_score(r, score_col) or 0.0)),
        )
        print(f"Sorting by '{score_col}' (descending priority).")
    else:
        print("No priority/score column found; processing rows in file order.")
    rows = all_rows[args.start_index:]
    mode = "a" if args.output.exists() and args.output.stat().st_size else "w"
    with args.output.open(mode, newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=OUTPUT_FIELDS)
        if mode == "w": writer.writeheader(); target.flush()
        processed = 0
        for row in rows:
            if args.max_rows is not None and processed >= args.max_rows: break
            issue_id = (row.get(id_col) or "").strip()
            if not issue_id or issue_id in existing: continue
            score = row_score(row, score_col)
            if args.min_score is not None and score is not None and score < args.min_score:
                print(f'Rejecting row ID: {issue_id} (score={score} < min-score={args.min_score})')
                writer.writerow(output_row(issue_id, args.model, args.prompt_version,
                                            status="skipped_low_priority", score=score))
                target.flush(); existing.add(issue_id)
                continue
            print(f'Reading row ID: {issue_id}' + (f' (score={score})' if score is not None else ''))
            try:
                result = call_gemini_with_retry(client, args.model, config, build_prompt(row, issue_id))
                writer.writerow(output_row(issue_id, args.model, args.prompt_version, result=result, score=score))
            except Exception as exc:
                print(f"{issue_id}: {exc}", file=sys.stderr)
                writer.writerow(output_row(issue_id, args.model, args.prompt_version, error=exc, score=score))
            target.flush(); existing.add(issue_id); processed += 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())