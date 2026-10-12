from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter


BASE_URL = "https://issues.apache.org/jira/rest/api/2"
DEFAULT_PAGE_SIZE = 50

REQUEST_TIMEOUT_SECONDS = 60

MAX_RETRIES = 7
MAX_BACKOFF_SECONDS = 60

RPC_TIMEOUT_KEYWORDS = [
    "timeout",
    "rpc timeout",
    "read timeout",
    "write timeout",
    "request timeout",
    "response timeout",
    "socket timeout",
    "network timeout",
    "connection timeout",
    "deadline",
    "deadline exceeded",
    "remaining timeout",
    "ReadRPCTimeout",
    "WriteRPCTimeout",
    "RangeRPCTimeout",
]

INCLUSION_PATTERNS: dict[str, list[str]] = {
    "configuracion_valor": [
        r"\b(?:increase|decrease|raise|lower|reduce|change|set|configure|default)\w*\b.{0,80}\btimeout\b",
        r"\btimeout\b.{0,80}\b(?:increase|decrease|raise|lower|reduce|change|set|configure|default)\w*\b",
        r"\b\d+(?:\.\d+)?\s*(?:ms|milliseconds?|s|sec|seconds?|m|min|minutes?)\b",
    ],
    "justificacion": [
        r"\b(?:because|since|therefore|thus|so that|in order to|reason|rationale|why)\b",
        r"\b(?:too low|too high|too short|too long|aggressive|conservative)\b",
        r"\b(?:client expects?|expected by the client|from the client perspective)\b",
    ],
    "rendimiento_tradeoff": [
        r"\b(?:latency|throughput|performance|overhead|load|workload|queue|contention|resource)\b",
        r"\b(?:trade[- ]?off|impact|degrad|regression|slow(?:er)?|fast(?:er)?)\w*\b",
    ],
    "experimento_evidencia": [
        r"\b(?:benchmark|experiment|test result|stress test|load test|measurement|measured|observed|production)\b",
        r"\b(?:p50|p90|p95|p99|percentile|average latency|mean latency)\b",
    ],
    "mecanismo_timeout": [
        r"\b(?:deadline|remaining timeout|timeout budget|propagat\w* timeout|elapsed time)\b",
        r"\b(?:readrpctimeout|writerpctimeout|rangerpctimeout|rpc timeout|request timeout|response timeout)\b",
    ],
}

EXCLUSION_PATTERNS: dict[str, list[str]] = {
    "solo_pruebas_ci": [
        r"\b(?:jenkins|circleci|github actions|buildkite|maven|gradle)\b.{0,80}\btimeout\b",
        r"\b(?:unit test|integration test|test suite|flaky test|dtest)\b.{0,80}\btimeout\b",
    ],
    "solo_logs_o_error": [
        r"\b(?:stack trace|stacktrace|exception log|log message)\b",
    ],
    "ui_o_usuario": [
        r"\b(?:gui|user interface|browser|web ui|screen|user input)\b.{0,80}\btimeout\b",
    ],
    "proceso_local_no_rpc": [
        r"\b(?:local lock|mutex|semaphore|thread join|sleep timeout|process timeout)\b",
    ],
}

TIMEOUT_VALUE_PATTERN = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ms|milliseconds?|s|sec|seconds?|m|min|minutes?)\b",
    re.IGNORECASE,
)

REAL_FIX_RESOLUTIONS = {"Fixed", "Done", "Implemented"}


class JiraScraperError(RuntimeError):
    """Error controlado producido al consultar Jira."""


def nested_name(value: Any) -> str | None:
    return value.get("name") if isinstance(value, dict) else None


def user_display_name(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    return (
        value.get("displayName")
        or value.get("name")
        or value.get("key")
    )


def list_names(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [
        str(v["name"])
        for v in values
        if isinstance(v, dict) and v.get("name")
    ]


def extract_issue_links(issue_links: Any) -> list[dict[str, Any]]:
    if not isinstance(issue_links, list):
        return []

    normalized: list[dict[str, Any]] = []

    for link in issue_links:
        if not isinstance(link, dict):
            continue

        link_type = link.get("type") or {}

        if "outwardIssue" in link:
            related = link["outwardIssue"]
            direction = "outward"
            relation = link_type.get("outward")
        elif "inwardIssue" in link:
            related = link["inwardIssue"]
            direction = "inward"
            relation = link_type.get("inward")
        else:
            continue

        related_fields = related.get("fields") or {}

        normalized.append(
            {
                "direction": direction,
                "relation": relation,
                "key": related.get("key"),
                "summary": related_fields.get("summary"),
                "status": nested_name(related_fields.get("status")),
                "priority": nested_name(related_fields.get("priority")),
                "issue_type": nested_name(related_fields.get("issuetype")),
            }
        )

    return normalized


def extract_comments(comment_field: Any) -> list[dict[str, Any]]:
    if not isinstance(comment_field, dict):
        return []

    return [
        {
            "id": c.get("id"),
            "author": user_display_name(c.get("author")),
            "created": c.get("created"),
            "updated": c.get("updated"),
            "body": c.get("body"),
        }
        for c in comment_field.get("comments", [])
        if isinstance(c, dict)
    ]


def extract_changelog(changelog: Any) -> list[dict[str, Any]]:
    if not isinstance(changelog, dict):
        return []

    changes: list[dict[str, Any]] = []

    for history in changelog.get("histories", []):
        if not isinstance(history, dict):
            continue

        author = user_display_name(history.get("author"))
        created = history.get("created")

        for item in history.get("items", []):
            if not isinstance(item, dict):
                continue

            changes.append(
                {
                    "author": author,
                    "created": created,
                    "field": item.get("field"),
                    "from": item.get("fromString"),
                    "to": item.get("toString"),
                }
            )

    return changes


def normalize_issue(issue: dict[str, Any]) -> dict[str, Any]:
    fields = issue.get("fields") or {}
    project = fields.get("project") or {}
    comments = extract_comments(fields.get("comment"))
    issue_key = issue.get("key")

    return {
        "id": issue.get("id"),
        "key": issue_key,
        "url": f"https://issues.apache.org/jira/browse/{issue_key}",
        "project_key": project.get("key"),
        "project_name": project.get("name"),
        "summary": fields.get("summary"),
        "description": fields.get("description"),
        "issue_type": nested_name(fields.get("issuetype")),
        "status": nested_name(fields.get("status")),
        "resolution": nested_name(fields.get("resolution")),
        "priority": nested_name(fields.get("priority")),
        "reporter": user_display_name(fields.get("reporter")),
        "assignee": user_display_name(fields.get("assignee")),
        "creator": user_display_name(fields.get("creator")),
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "resolution_date": fields.get("resolutiondate"),
        "labels": fields.get("labels") or [],
        "components": list_names(fields.get("components")),
        "fix_versions": list_names(fields.get("fixVersions")),
        "affected_versions": list_names(fields.get("versions")),
        "comments_count": len(comments),
        "comments": comments,
        "issue_links": extract_issue_links(fields.get("issuelinks")),
        "changelog": extract_changelog(issue.get("changelog")),
        "matched_keywords": [],
        "attachments": [
            {
                "filename": a.get("filename"),
                "mime_type": a.get("mimeType"),
                "size": a.get("size"),
                "content_url": a.get("content"),
            }
            for a in fields.get("attachment", [])
            if isinstance(a, dict)
        ],
    }


def issue_text(issue: dict[str, Any]) -> str:
    comments = "\n".join(
        str(c.get("body") or "")
        for c in issue.get("comments", [])
    )

    return "\n".join(
        [
            str(issue.get("summary") or ""),
            str(issue.get("description") or ""),
            comments,
        ]
    )


def evaluate_selection_criteria(
    issue: dict[str, Any],
) -> dict[str, Any]:
    text = issue_text(issue)
    lower_text = text.lower()

    inclusion_hits: dict[str, list[str]] = {}
    exclusion_hits: dict[str, list[str]] = {}

    for category, patterns in INCLUSION_PATTERNS.items():
        hits: list[str] = []

        for pattern in patterns:
            if re.search(
                pattern,
                text,
                re.IGNORECASE | re.DOTALL,
            ):
                hits.append(pattern)

        if hits:
            inclusion_hits[category] = hits

    for category, patterns in EXCLUSION_PATTERNS.items():
        hits: list[str] = []

        for pattern in patterns:
            if re.search(
                pattern,
                text,
                re.IGNORECASE | re.DOTALL,
            ):
                hits.append(pattern)

        if hits:
            exclusion_hits[category] = hits

    timeout_values = sorted(
        set(TIMEOUT_VALUE_PATTERN.findall(text))
    )

    has_rpc_timeout = any(
        keyword.lower() in lower_text
        for keyword in RPC_TIMEOUT_KEYWORDS
        if keyword.lower() != "timeout"
    )

    has_value_change = "configuracion_valor" in inclusion_hits

    has_reasoning = any(
        category in inclusion_hits
        for category in (
            "justificacion",
            "rendimiento_tradeoff",
            "experimento_evidencia",
            "mecanismo_timeout",
        )
    )

    score = 0
    score += 3 if has_rpc_timeout else 0
    score += 3 if "configuracion_valor" in inclusion_hits else 0
    score += 3 if "justificacion" in inclusion_hits else 0
    score += 2 if "rendimiento_tradeoff" in inclusion_hits else 0
    score += 2 if "experimento_evidencia" in inclusion_hits else 0
    score += 1 if timeout_values else 0

    strong_exclusion = any(
        category in exclusion_hits
        for category in (
            "solo_pruebas_ci",
            "ui_o_usuario",
            "proceso_local_no_rpc",
        )
    )

    resolution = issue.get("resolution")

    has_real_fix = (
        resolution in REAL_FIX_RESOLUTIONS
        or resolution is None
    )

    included = (
        has_rpc_timeout
        and has_value_change
        and has_reasoning
        and has_real_fix
        and not strong_exclusion
    )

    if included:
        decision_reason = (
            "Incluida: contiene evidencia de timeout RPC, "
            "un cambio de configuración/valor, razonamiento de apoyo, "
            f"y tiene fix o todavía no se resolvió "
            f"(status={issue.get('status')!r}, "
            f"resolution={resolution!r})."
        )
    elif strong_exclusion:
        decision_reason = (
            "Excluida: el contexto coincide con un "
            "criterio fuerte de exclusión."
        )
    elif not has_rpc_timeout:
        decision_reason = (
            "Excluida: no hay evidencia suficiente de "
            "que sea un timeout RPC."
        )
    elif not has_value_change:
        decision_reason = (
            "Excluida: no se discute un cambio de "
            "valor/configuración del timeout."
        )
    elif not has_reasoning:
        decision_reason = (
            "Excluida: hay cambio de configuración, "
            "pero no explica por qué o con qué evidencia."
        )
    else:
        decision_reason = (
            "Excluida: se cerró sin un fix real "
            f"(status={issue.get('status')!r}, "
            f"resolution={resolution!r})."
        )

    return {
        "selection_included": included,
        "selection_score": score,
        "selection_reason": decision_reason,
        "inclusion_categories": sorted(inclusion_hits),
        "exclusion_categories": sorted(exclusion_hits),
        "timeout_values": timeout_values,
        "has_rpc_timeout_evidence": has_rpc_timeout,
        "has_value_change": has_value_change,
        "has_configuration_reasoning": has_reasoning,
        "has_real_fix": has_real_fix,
    }


def apply_selection_criteria(
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []

    for issue in issues:
        evaluation = evaluate_selection_criteria(issue)
        issue.update(evaluation)

        if evaluation["selection_included"]:
            filtered.append(issue)

    return filtered


class ApacheJiraClient:
    def __init__(
        self,
        base_url: str = BASE_URL,
        delay_seconds: float = 0.2,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.delay_seconds = max(delay_seconds, 0.0)
        self.max_retries = max(1, max_retries)

        self.session = requests.Session()

        adapter = HTTPAdapter(
            pool_connections=5,
            pool_maxsize=5,
            max_retries=0,
        )

        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self._configure_session()

    def _configure_session(self) -> None:
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "ApacheJiraResearchScraper/2.0",
                "Connection": "keep-alive",
            }
        )

    def _reset_session(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

        self.session = requests.Session()

        adapter = HTTPAdapter(
            pool_connections=5,
            pool_maxsize=5,
            max_retries=0,
        )

        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._configure_session()

    @staticmethod
    def _backoff(attempt: int) -> int:
        return min(
            MAX_BACKOFF_SECONDS,
            2 ** max(0, attempt - 1),
        )

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"

        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.get(
                    url,
                    params=params,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )

                if response.status_code == 429:
                    retry_after_raw = response.headers.get(
                        "Retry-After",
                        "10",
                    )

                    try:
                        retry_after = int(retry_after_raw)
                    except ValueError:
                        retry_after = 10

                    retry_after = max(
                        1,
                        min(retry_after, MAX_BACKOFF_SECONDS),
                    )

                    print(
                        f"\nJira limitó las peticiones (HTTP 429). "
                        f"Esperando {retry_after}s..."
                    )

                    time.sleep(retry_after)
                    continue

                if response.status_code in {500, 502, 503, 504}:
                    if attempt < self.max_retries:
                        wait = self._backoff(attempt)

                        print(
                            f"\nJira respondió HTTP "
                            f"{response.status_code}. "
                            f"Reintentando en {wait}s "
                            f"(intento {attempt}/{self.max_retries})..."
                        )

                        time.sleep(wait)
                        continue

                response.raise_for_status()

                try:
                    data = response.json()
                except ValueError as exc:
                    raise JiraScraperError(
                        "Jira no devolvió JSON válido."
                    ) from exc

                if self.delay_seconds:
                    time.sleep(self.delay_seconds)

                return data

            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise JiraScraperError(
                        "No se pudo conectar con Jira después de "
                        f"{self.max_retries} intentos: {exc}"
                    ) from exc

                wait = self._backoff(attempt)

                print(
                    f"\nConexión con Jira falló: {exc}"
                )
                print(
                    f"Reintentando en {wait}s "
                    f"(intento {attempt}/{self.max_retries})..."
                )

                self._reset_session()

                time.sleep(wait)

        raise JiraScraperError(
            "No se pudo obtener respuesta de Jira."
        )

    def search_issue_keys(
        self,
        jql: str,
        start_at: int = 0,
        limit: int | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> list[str]:
        keys: list[str] = []
        current_start = start_at
        page_size = min(max(page_size, 1), 100)

        while True:
            remaining = (
                None
                if limit is None
                else limit - len(keys)
            )

            if remaining is not None and remaining <= 0:
                break

            current_size = (
                page_size
                if remaining is None
                else min(page_size, remaining)
            )

            data = self._get(
                "search",
                params={
                    "jql": jql,
                    "startAt": current_start,
                    "maxResults": current_size,
                    "fields": "key",
                },
            )

            issues = data.get("issues", [])

            if not issues:
                break

            new_keys = [
                issue["key"]
                for issue in issues
                if issue.get("key")
            ]

            keys.extend(new_keys)
            current_start += len(issues)

            total = int(
                data.get("total", len(keys))
            )

            print(
                f"\rIssues encontradas: {len(keys)} "
                f"(posiciones {start_at + 1}-{current_start} "
                f"de {total})",
                end="",
                flush=True,
            )

            if current_start >= total:
                break

        print()
        return keys

    def search_by_keywords(
        self,
        project: str,
        keywords: list[str],
        limit_per_keyword: int | None = None,
        checkpoint_path: Path | None = None,
    ) -> dict[str, list[str]]:
        matches: dict[str, list[str]] = {}

        if checkpoint_path and checkpoint_path.exists():
            try:
                saved = json.loads(
                    checkpoint_path.read_text(
                        encoding="utf-8"
                    )
                )

                if isinstance(saved, dict):
                    matches = {
                        str(key): list(value)
                        for key, value in saved.items()
                        if isinstance(value, list)
                    }

                    print(
                        f"Checkpoint recuperado: "
                        f"{len(matches)} issues únicas."
                    )
            except (OSError, json.JSONDecodeError) as exc:
                print(
                    f"Aviso: no se pudo leer el checkpoint: {exc}"
                )

        project = project.strip().upper()

        for index, keyword in enumerate(
            keywords,
            start=1,
        ):
            print(
                f"\n[{index}/{len(keywords)}] "
                f"Buscando palabra clave: {keyword!r}"
            )

            safe_keyword = (
                keyword
                .replace("\\", "\\\\")
                .replace('"', '\\"')
            )

            jql = (
                f'project = "{project}" '
                f'AND text ~ "{safe_keyword}" '
                f"ORDER BY created DESC"
            )

            print(f"JQL: {jql}")

            for issue_key in self.search_issue_keys(
                jql=jql,
                limit=limit_per_keyword,
            ):
                keywords_for_issue = matches.setdefault(
                    issue_key,
                    [],
                )

                if keyword not in keywords_for_issue:
                    keywords_for_issue.append(keyword)

            if checkpoint_path:
                atomic_write_json(
                    matches,
                    checkpoint_path,
                )

                print(
                    f"Checkpoint guardado: "
                    f"{len(matches)} issues únicas."
                )

        return matches

    def get_issue(
        self,
        issue_key: str,
    ) -> dict[str, Any]:
        fields = ",".join(
            [
                "project",
                "summary",
                "description",
                "issuetype",
                "status",
                "resolution",
                "priority",
                "reporter",
                "assignee",
                "creator",
                "created",
                "updated",
                "resolutiondate",
                "labels",
                "components",
                "fixVersions",
                "versions",
                "comment",
                "issuelinks",
                "attachment",
            ]
        )

        return self._get(
            f"issue/{issue_key}",
            params={
                "fields": fields,
                "expand": "changelog",
            },
        )


def atomic_write_text(
    text: str,
    output_path: Path,
) -> None:
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp_path = output_path.with_name(
        output_path.name + ".tmp"
    )

    tmp_path.write_text(
        text,
        encoding="utf-8",
    )

    tmp_path.replace(output_path)


def atomic_write_json(
    data: Any,
    output_path: Path,
) -> None:
    atomic_write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ),
        output_path,
    )


CSV_COLUMNS = [
    "key",
    "url",
    "project_key",
    "project_name",
    "summary",
    "description",
    "issue_type",
    "status",
    "resolution",
    "priority",
    "reporter",
    "assignee",
    "created",
    "updated",
    "resolution_date",
    "labels",
    "components",
    "fix_versions",
    "comments_count",
    "comments_text",
    "related_issue_keys",
    "selection_score",
    "selection_included",
]


def save_csv(
    issues: list[dict[str, Any]],
    output_path: Path,
) -> None:
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp_path = output_path.with_name(
        output_path.name + ".tmp"
    )

    with tmp_path.open(
        "w",
        encoding="utf-8-sig",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=CSV_COLUMNS,
        )

        writer.writeheader()

        for issue in issues:
            comments_text = "\n\n".join(
                f"[{comment.get('author')} | "
                f"{comment.get('created')}]\n"
                f"{comment.get('body') or ''}"
                for comment in issue.get("comments", [])
            )

            related_issue_keys = "; ".join(
                str(link.get("key"))
                for link in issue.get("issue_links", [])
                if link.get("key")
            )

            writer.writerow(
                {
                    "key": issue.get("key"),
                    "url": issue.get("url"),
                    "project_key": issue.get("project_key"),
                    "project_name": issue.get("project_name"),
                    "summary": issue.get("summary"),
                    "description": issue.get("description"),
                    "issue_type": issue.get("issue_type"),
                    "status": issue.get("status"),
                    "resolution": issue.get("resolution"),
                    "priority": issue.get("priority"),
                    "reporter": issue.get("reporter"),
                    "assignee": issue.get("assignee"),
                    "created": issue.get("created"),
                    "updated": issue.get("updated"),
                    "resolution_date": issue.get(
                        "resolution_date"
                    ),
                    "labels": "; ".join(
                        issue.get("labels", [])
                    ),
                    "components": "; ".join(
                        issue.get("components", [])
                    ),
                    "fix_versions": "; ".join(
                        issue.get("fix_versions", [])
                    ),
                    "comments_count": issue.get(
                        "comments_count"
                    ),
                    "comments_text": comments_text,
                    "related_issue_keys": related_issue_keys,
                    "selection_score": issue.get(
                        "selection_score"
                    ),
                    "selection_included": issue.get(
                        "selection_included"
                    ),
                }
            )

    tmp_path.replace(output_path)


def save_results(
    issues: list[dict[str, Any]],
    json_path: Path,
    csv_path: Path,
    filter_criteria: bool,
    filtered_json_path: Path,
    filtered_csv_path: Path,
) -> None:
    atomic_write_json(
        issues,
        json_path,
    )

    save_csv(
        issues,
        csv_path,
    )

    if filter_criteria:
        filtered = apply_selection_criteria(
            issues
        )

        atomic_write_json(
            filtered,
            filtered_json_path,
        )

        save_csv(
            filtered,
            filtered_csv_path,
        )


def build_jql(
    project: str,
    text: str | None,
) -> str:
    project = project.strip().upper()

    if not project:
        raise ValueError(
            "El proyecto no puede estar vacío."
        )

    jql = f'project = "{project}"'

    if text:
        safe_text = (
            text
            .replace("\\", "\\\\")
            .replace('"', '\\"')
        )

        jql += f' AND text ~ "{safe_text}"'

    return jql + " ORDER BY key ASC"


def guess_project_folder(
    args: argparse.Namespace,
) -> str:
    if args.rpc_timeouts:
        return args.rpc_timeouts.strip().upper()

    if args.project:
        return args.project.strip().upper()

    match = re.search(
        r'project\s*=\s*"?([A-Za-z0-9_]+)"?',
        args.jql or "",
        re.IGNORECASE,
    )

    return (
        match.group(1).upper()
        if match
        else "custom_query"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Descarga issues desde Apache Jira "
            "y las guarda en JSON y CSV."
        )
    )

    query_group = parser.add_mutually_exclusive_group(
        required=True
    )

    query_group.add_argument(
        "--project",
    )

    query_group.add_argument(
        "--jql",
    )

    query_group.add_argument(
        "--rpc-timeouts",
        metavar="PROJECT",
    )

    parser.add_argument(
        "--text",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
    )

    parser.add_argument(
        "--start",
        type=int,
        default=1,
    )

    parser.add_argument(
        "--end",
        type=int,
        default=None,
    )

    parser.add_argument(
        "--limit-per-keyword",
        type=int,
        default=None,
    )

    parser.add_argument(
        "--filter-criteria",
        action="store_true",
    )

    parser.add_argument(
        "--output-dir",
        default="jira_output",
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=0.2,
    )

    parser.add_argument(
        "--no-resume",
        action="store_true",
    )

    parser.add_argument(
        "--max-retries",
        type=int,
        default=MAX_RETRIES,
    )

    return parser.parse_args()


def validate_args(
    args: argparse.Namespace,
) -> str | None:
    if args.limit is not None and args.limit <= 0:
        return "--limit debe ser mayor que cero."

    if (
        args.limit_per_keyword is not None
        and args.limit_per_keyword <= 0
    ):
        return (
            "--limit-per-keyword debe ser mayor que cero."
        )

    if args.start <= 0:
        return (
            "--start debe ser mayor que cero."
        )

    if (
        args.end is not None
        and args.end < args.start
    ):
        return (
            "--end debe ser mayor o igual que --start."
        )

    if (
        args.end is not None
        and args.limit is not None
    ):
        return (
            "No combines --limit con --start/--end."
        )

    if (
        args.rpc_timeouts
        and (
            args.start != 1
            or args.end is not None
        )
    ):
        return (
            "--start/--end no está soportado junto "
            "con --rpc-timeouts."
        )

    if args.text and not args.project:
        return (
            "--text solo puede utilizarse junto con "
            "--project."
        )

    if args.max_retries <= 0:
        return (
            "--max-retries debe ser mayor que cero."
        )

    return None


def build_query(
    args: argparse.Namespace,
) -> tuple[str | None, str]:
    if args.rpc_timeouts:
        return (
            None,
            f"{args.rpc_timeouts.upper()}_rpc_timeouts",
        )

    if args.jql:
        return args.jql, "jira_custom_query"

    jql = build_jql(
        args.project,
        args.text,
    )

    suffix = (
        f"_{args.text}"
        if args.text
        else ""
    )

    safe_suffix = "".join(
        c
        if c.isalnum() or c in "-_"
        else "_"
        for c in suffix
    )

    base_prefix = (
        f"{args.project.upper()}{safe_suffix}"
    )

    if (
        args.start != 1
        or args.end is not None
    ):
        range_suffix = (
            f"_{args.start}-"
            f"{args.end if args.end is not None else 'fin'}"
        )
    else:
        range_suffix = ""

    return (
        jql,
        f"{base_prefix}{range_suffix}",
    )


def load_existing_issues(
    json_path: Path,
) -> list[dict[str, Any]]:
    if not json_path.exists():
        return []

    try:
        data = json.loads(
            json_path.read_text(
                encoding="utf-8"
            )
        )
    except (
        OSError,
        json.JSONDecodeError,
    ) as exc:
        print(
            f"Aviso: no se pudo recuperar "
            f"{json_path}: {exc}"
        )
        return []

    if not isinstance(data, list):
        print(
            f"Aviso: {json_path} no contiene una lista. "
            "Se empieza desde cero."
        )
        return []

    valid = [
        item
        for item in data
        if isinstance(item, dict)
        and item.get("key")
    ]

    return valid


def main() -> int:
    args = parse_args()

    error = validate_args(args)

    if error:
        print(
            f"Error: {error}",
            file=sys.stderr,
        )
        return 2

    start_at = args.start - 1

    range_limit = (
        args.end - args.start + 1
        if args.end is not None
        else args.limit
    )

    jql, file_prefix = build_query(args)

    output_dir = (
        Path(args.output_dir)
        / guess_project_folder(args)
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    json_path = (
        output_dir / f"{file_prefix}.json"
    )
    csv_path = (
        output_dir / f"{file_prefix}.csv"
    )

    filtered_json_path = (
        output_dir / f"{file_prefix}_filtered.json"
    )
    filtered_csv_path = (
        output_dir / f"{file_prefix}_filtered.csv"
    )

    keyword_checkpoint_path = (
        output_dir
        / f"{file_prefix}_keyword_checkpoint.json"
    )

    client = ApacheJiraClient(
        delay_seconds=args.delay,
        max_retries=args.max_retries,
    )

    keyword_matches: dict[str, list[str]] = {}

    try:
        if args.rpc_timeouts:
            keyword_matches = (
                client.search_by_keywords(
                    project=args.rpc_timeouts,
                    keywords=RPC_TIMEOUT_KEYWORDS,
                    limit_per_keyword=args.limit_per_keyword,
                    checkpoint_path=keyword_checkpoint_path,
                )
            )

            issue_keys = list(
                keyword_matches
            )

            if args.limit is not None:
                issue_keys = issue_keys[:args.limit]

            print(
                "\nIssues únicas después de "
                f"eliminar duplicados: {len(issue_keys)}"
            )

        else:
            print(f"JQL: {jql}")

            issue_keys = client.search_issue_keys(
                jql=jql,
                start_at=start_at,
                limit=range_limit,
            )

        if not issue_keys:
            print("No se encontraron issues.")
            return 0

        if args.no_resume:
            normalized_issues: list[
                dict[str, Any]
            ] = []
        else:
            normalized_issues = (
                load_existing_issues(json_path)
            )

        downloaded_by_key = {
            issue["key"]: issue
            for issue in normalized_issues
            if issue.get("key")
        }

        if downloaded_by_key:
            print(
                f"\nCheckpoint de issues recuperado: "
                f"{len(downloaded_by_key)} ya descargadas."
            )

        for index, issue_key in enumerate(
            issue_keys,
            start=1,
        ):
            if issue_key in downloaded_by_key:
                print(
                    f"[{index}/{len(issue_keys)}] "
                    f"{issue_key} ya existe. Saltando."
                )
                continue

            print(
                f"[{index}/{len(issue_keys)}] "
                f"Descargando {issue_key}..."
            )

            normalized = normalize_issue(
                client.get_issue(issue_key)
            )

            normalized["matched_keywords"] = (
                keyword_matches.get(
                    issue_key,
                    [],
                )
            )

            downloaded_by_key[
                issue_key
            ] = normalized

            normalized_issues = list(
                downloaded_by_key.values()
            )
            save_results(
                issues=normalized_issues,
                json_path=json_path,
                csv_path=csv_path,
                filter_criteria=args.filter_criteria,
                filtered_json_path=filtered_json_path,
                filtered_csv_path=filtered_csv_path,
            )

            print(
                f"    ✓ Guardado "
                f"({len(normalized_issues)}/{len(issue_keys)})"
            )

        normalized_issues = list(
            downloaded_by_key.values()
        )

        save_results(
            issues=normalized_issues,
            json_path=json_path,
            csv_path=csv_path,
            filter_criteria=args.filter_criteria,
            filtered_json_path=filtered_json_path,
            filtered_csv_path=filtered_csv_path,
        )

        print("\nProceso terminado.")
        print(
            f"Issues descargadas: "
            f"{len(normalized_issues)}"
        )
        print(
            f"JSON: {json_path.resolve()}"
        )
        print(
            f"CSV:  {csv_path.resolve()}"
        )

        if args.filter_criteria:
            filtered_count = sum(
                1
                for issue in normalized_issues
                if issue.get(
                    "selection_included"
                )
            )

            print(
                f"Issues incluidas por criterios: "
                f"{filtered_count}"
            )
            print(
                "JSON filtrado: "
                f"{filtered_json_path.resolve()}"
            )
            print(
                "CSV filtrado: "
                f"{filtered_csv_path.resolve()}"
            )

        return 0

    except JiraScraperError as exc:
        print(
            f"\nError: {exc}",
            file=sys.stderr,
        )

        print(
            "\nIMPORTANTE: el progreso anterior "
            "ya quedó guardado en disco."
        )

        print(
            f"Puedes volver a ejecutar el mismo comando "
            f"para continuar desde lo descargado."
        )

        return 1

    except KeyboardInterrupt:
        print(
            "\nProceso cancelado por el usuario.",
            file=sys.stderr,
        )

        print(
            "El progreso ya descargado quedó guardado."
        )

        return 130


if __name__ == "__main__":
    raise SystemExit(main())
