#!/usr/bin/env python3
"""Read-only validator for the China social-history monthly archive."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse


ARCHIVE_PATH_RE = re.compile(
    r"^archive/(?P<year>\d{4})/(?P<month>(?P=year)-(?:0[1-9]|1[0-2]))\.md$"
)
MONTH_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")
ENTRY_HEADING_RE = re.compile(
    r"^##[ \t]+(?P<number>\d{1,2})[.、)][ \t]+(?P<title>.+?)\s*$"
)
ANY_H2_RE = re.compile(r"^##(?:[ \t]+|$)")
FIELD_RE = re.compile(
    r"^[ \t]*-[ \t]*(?:\*\*)?"
    r"(?P<label>标题|描述|历史意义|新闻链接)"
    r"(?:\*\*)?[ \t]*[：:][ \t]*(?P<value>.*)$"
)
LINK_LINE_RE = re.compile(
    r"^[ \t]*(?:-[ \t]*)?\[(?P<label>[^\]\n]+)\]"
    r"\((?P<url>https?://[^\s)]+)\)(?:[ \t]+.*)?$"
)
PLACEHOLDER_RE = re.compile(
    r"^(?:待补充|待核验|TODO|TBD|N/?A|\.{3}|…+|"
    r"<[^>]+>|用中性、可检索的短句概括事件)$",
    re.IGNORECASE,
)

REQUIRED_FIELDS = ("标题", "描述", "历史意义", "新闻链接")
COVERAGE_FIELDS = ("month", "status", "event_count", "verified_count", "notes")
ALLOWED_STATUSES = {"pending", "seed_pending", "draft", "in_review", "verified"}
PROJECT_START_MONTH = "1995-01"
INITIAL_COVERAGE_END = "2026-08"


@dataclass(frozen=True)
class Issue:
    severity: str
    code: str
    message: str
    path: str | None = None
    line: int | None = None
    month: str | None = None
    entry: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def add_month(month: str) -> str:
    year, month_number = (int(part) for part in month.split("-"))
    if month_number == 12:
        return f"{year + 1:04d}-01"
    return f"{year:04d}-{month_number + 1:02d}"


def month_sequence(start: str, end: str) -> list[str]:
    if not MONTH_RE.fullmatch(start) or not MONTH_RE.fullmatch(end):
        raise ValueError("month must use YYYY-MM")
    if start > end:
        raise ValueError("start month must not be after end month")
    months: list[str] = []
    current = start
    while current <= end:
        months.append(current)
        current = add_month(current)
    return months


def display_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def is_placeholder(value: str) -> bool:
    compact = " ".join(value.split()).strip()
    return not compact or bool(PLACEHOLDER_RE.fullmatch(compact))


def is_valid_http_url(url: str) -> bool:
    if any(character.isspace() for character in url):
        return False
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def parse_entry_fields(
    lines: list[str],
    start: int,
    end: int,
    path_label: str,
    month: str,
    entry_number: int,
    heading_title: str,
) -> tuple[dict[str, list[str]], list[Issue]]:
    fields: dict[str, list[str]] = {}
    field_lines: dict[str, int] = {}
    active_field: str | None = None
    issues: list[Issue] = []

    for index in range(start, end):
        match = FIELD_RE.match(lines[index])
        if match:
            label = match.group("label")
            value = match.group("value").rstrip()
            if label in fields:
                issues.append(
                    Issue(
                        "error",
                        "duplicate_field",
                        f"字段“{label}”在同一条记录中重复出现",
                        path_label,
                        index + 1,
                        month,
                        entry_number,
                    )
                )
            else:
                fields[label] = []
                field_lines[label] = index + 1
            active_field = label
            if value:
                fields[label].append(value)
            continue

        if active_field is not None and lines[index].strip():
            fields[active_field].append(lines[index].rstrip())

    for required in REQUIRED_FIELDS:
        if required not in fields:
            issues.append(
                Issue(
                    "error",
                    "missing_field",
                    f"缺少字段“{required}”",
                    path_label,
                    start,
                    month,
                    entry_number,
                )
            )
            continue
        value = "\n".join(fields[required]).strip()
        if is_placeholder(value):
            issues.append(
                Issue(
                    "error",
                    "empty_or_placeholder_field",
                    f"字段“{required}”为空或仍是占位文本",
                    path_label,
                    field_lines[required],
                    month,
                    entry_number,
                )
            )

    if "标题" in fields:
        field_title = " ".join(fields["标题"]).strip()
        if field_title and not is_placeholder(field_title) and field_title != heading_title:
            issues.append(
                Issue(
                    "warning",
                    "title_mismatch",
                    f"标题字段“{field_title}”与二级标题“{heading_title}”不一致",
                    path_label,
                    field_lines["标题"],
                    month,
                    entry_number,
                )
            )

    if "新闻链接" in fields:
        link_lines = [line for line in fields["新闻链接"] if line.strip()]
        valid_urls: list[str] = []
        for link_line in link_lines:
            match = LINK_LINE_RE.fullmatch(link_line)
            if not match:
                issues.append(
                    Issue(
                        "error",
                        "invalid_link_format",
                        "新闻链接的每一行都必须是带说明文字的 Markdown HTTP(S) 链接",
                        path_label,
                        field_lines["新闻链接"],
                        month,
                        entry_number,
                    )
                )
                continue
            label = match.group("label").strip()
            url = match.group("url")
            if is_placeholder(label) or not is_valid_http_url(url):
                issues.append(
                    Issue(
                        "error",
                        "invalid_news_link",
                        f"新闻链接标签或 URL 无效：{url}",
                        path_label,
                        field_lines["新闻链接"],
                        month,
                        entry_number,
                    )
                )
                continue
            if url.endswith("example.org/article") or url.endswith("example.com"):
                issues.append(
                    Issue(
                        "error",
                        "placeholder_news_link",
                        f"新闻链接仍是示例地址：{url}",
                        path_label,
                        field_lines["新闻链接"],
                        month,
                        entry_number,
                    )
                )
                continue
            valid_urls.append(url)

        if not valid_urls:
            issues.append(
                Issue(
                    "error",
                    "missing_valid_news_link",
                    "新闻链接字段至少需要一个有效的 HTTP(S) Markdown 链接",
                    path_label,
                    field_lines["新闻链接"],
                    month,
                    entry_number,
                )
            )
        elif len(valid_urls) != len(set(valid_urls)):
            issues.append(
                Issue(
                    "warning",
                    "duplicate_news_link",
                    "同一条记录中存在重复的新闻链接",
                    path_label,
                    field_lines["新闻链接"],
                    month,
                    entry_number,
                )
            )

    return fields, issues


def validate_month_file(
    path: Path,
    root: Path,
    month: str,
    min_events: int,
    max_events: int,
    strict_count: bool,
) -> tuple[dict[str, object], list[Issue]]:
    path_label = display_path(path, root)
    issues: list[Issue] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return (
            {"path": path_label, "month": month, "event_count": 0},
            [
                Issue(
                    "error",
                    "file_read_error",
                    f"无法以 UTF-8 读取文件：{exc}",
                    path_label,
                    month=month,
                )
            ],
        )

    lines = text.splitlines()
    h2_positions = [
        (index, line) for index, line in enumerate(lines) if ANY_H2_RE.match(line)
    ]
    recognized: list[tuple[int, re.Match[str]]] = []

    for index, line in h2_positions:
        match = ENTRY_HEADING_RE.match(line)
        if match is None:
            issues.append(
                Issue(
                    "error",
                    "invalid_entry_heading",
                    "二级标题必须使用“## 01. 事件标题”格式",
                    path_label,
                    index + 1,
                    month,
                )
            )
        else:
            recognized.append((index, match))

    for expected_number, (line_index, match) in enumerate(recognized, start=1):
        actual_number = int(match.group("number"))
        if actual_number != expected_number:
            issues.append(
                Issue(
                    "error",
                    "entry_number_sequence",
                    f"记录编号应为 {expected_number:02d}，实际为 {actual_number:02d}",
                    path_label,
                    line_index + 1,
                    month,
                    actual_number,
                )
            )

        next_h2 = next(
            (
                candidate_index
                for candidate_index, _ in h2_positions
                if candidate_index > line_index
            ),
            len(lines),
        )
        _, entry_issues = parse_entry_fields(
            lines,
            line_index + 1,
            next_h2,
            path_label,
            month,
            actual_number,
            match.group("title").strip(),
        )
        issues.extend(entry_issues)

    event_count = len(recognized)
    if event_count == 0:
        issues.append(
            Issue(
                "error",
                "no_entries",
                "月文件没有找到合法记录",
                path_label,
                month=month,
            )
        )
    elif event_count < min_events or event_count > max_events:
        severity = "error" if strict_count else "warning"
        issues.append(
            Issue(
                severity,
                "event_count_out_of_range",
                f"本月有 {event_count} 条记录，原则范围为 {min_events}–{max_events} 条",
                path_label,
                month=month,
            )
        )

    return (
        {"path": path_label, "month": month, "event_count": event_count},
        issues,
    )


def parse_nonnegative_integer(
    value: str,
    field: str,
    row_number: int,
    path_label: str,
    month: str | None,
    issues: list[Issue],
) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = -1
    if parsed < 0:
        issues.append(
            Issue(
                "error",
                "invalid_coverage_count",
                f"coverage.csv 第 {row_number} 行的 {field} 必须是非负整数",
                path_label,
                row_number,
                month,
            )
        )
        return None
    return parsed


def validate_coverage(
    coverage_path: Path,
    root: Path,
    month_counts: dict[str, int],
) -> tuple[dict[str, object], list[Issue]]:
    path_label = display_path(coverage_path, root)
    issues: list[Issue] = []
    if not coverage_path.is_file():
        return (
            {"path": path_label, "rows": 0},
            [
                Issue(
                    "error",
                    "coverage_missing",
                    "找不到 coverage.csv",
                    path_label,
                )
            ],
        )

    try:
        with coverage_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != COVERAGE_FIELDS:
                return (
                    {"path": path_label, "rows": 0},
                    [
                        Issue(
                            "error",
                            "coverage_header",
                            "coverage.csv 表头必须严格为 "
                            + ",".join(COVERAGE_FIELDS),
                            path_label,
                            1,
                        )
                    ],
                )
            raw_rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        return (
            {"path": path_label, "rows": 0},
            [
                Issue(
                    "error",
                    "coverage_read_error",
                    f"无法读取 coverage.csv：{exc}",
                    path_label,
                )
            ],
        )

    seen: set[str] = set()
    ordered_months: list[str] = []
    parsed_rows: dict[str, dict[str, object]] = {}

    for row_number, row in enumerate(raw_rows, start=2):
        month = (row.get("month") or "").strip()
        status = (row.get("status") or "").strip()
        notes = (row.get("notes") or "").strip()
        if not MONTH_RE.fullmatch(month):
            issues.append(
                Issue(
                    "error",
                    "invalid_coverage_month",
                    f"coverage.csv 第 {row_number} 行月份必须使用 YYYY-MM",
                    path_label,
                    row_number,
                )
            )
            continue
        if month in seen:
            issues.append(
                Issue(
                    "error",
                    "duplicate_coverage_month",
                    f"coverage.csv 中月份 {month} 重复",
                    path_label,
                    row_number,
                    month,
                )
            )
            continue
        seen.add(month)
        ordered_months.append(month)

        if status not in ALLOWED_STATUSES:
            issues.append(
                Issue(
                    "error",
                    "invalid_coverage_status",
                    f"未知状态“{status}”",
                    path_label,
                    row_number,
                    month,
                )
            )

        event_count = parse_nonnegative_integer(
            row.get("event_count") or "",
            "event_count",
            row_number,
            path_label,
            month,
            issues,
        )
        verified_count = parse_nonnegative_integer(
            row.get("verified_count") or "",
            "verified_count",
            row_number,
            path_label,
            month,
            issues,
        )
        parsed_rows[month] = {
            "status": status,
            "event_count": event_count,
            "verified_count": verified_count,
            "notes": notes,
            "row_number": row_number,
        }

        if event_count is not None and verified_count is not None:
            if verified_count > event_count:
                issues.append(
                    Issue(
                        "error",
                        "verified_count_exceeds_total",
                        "verified_count 不能大于 event_count",
                        path_label,
                        row_number,
                        month,
                    )
                )
            if status in {"pending", "seed_pending"} and (
                event_count != 0 or verified_count != 0
            ):
                issues.append(
                    Issue(
                        "error",
                        "pending_status_has_events",
                        f"{status} 状态的两个计数字段必须为 0",
                        path_label,
                        row_number,
                        month,
                    )
                )
            if status in {"draft", "in_review"} and event_count == 0:
                issues.append(
                    Issue(
                        "error",
                        "active_status_without_events",
                        f"{status} 状态至少需要 1 条记录",
                        path_label,
                        row_number,
                        month,
                    )
                )
            if status == "verified" and (
                event_count == 0 or verified_count != event_count
            ):
                issues.append(
                    Issue(
                        "error",
                        "invalid_verified_status",
                        "verified 状态要求 event_count 大于 0 且全部记录已核验",
                        path_label,
                        row_number,
                        month,
                    )
                )
        if status == "seed_pending" and not notes:
            issues.append(
                Issue(
                    "warning",
                    "seed_without_notes",
                    "seed_pending 状态应在 notes 中说明种子来源或待办",
                    path_label,
                    row_number,
                    month,
                )
            )

    if ordered_months != sorted(ordered_months):
        issues.append(
            Issue(
                "error",
                "coverage_order",
                "coverage.csv 必须按月份升序排列",
                path_label,
            )
        )

    if ordered_months:
        if ordered_months[0] != PROJECT_START_MONTH:
            issues.append(
                Issue(
                    "error",
                    "coverage_start",
                    f"coverage.csv 必须从 {PROJECT_START_MONTH} 开始",
                    path_label,
                )
            )
        last_month = max(ordered_months)
        if last_month < INITIAL_COVERAGE_END:
            issues.append(
                Issue(
                    "error",
                    "coverage_end",
                    f"coverage.csv 当前至少应覆盖到 {INITIAL_COVERAGE_END}",
                    path_label,
                )
            )
        expected = set(month_sequence(PROJECT_START_MONTH, last_month))
        missing = sorted(expected - seen)
        if missing:
            sample = ", ".join(missing[:12])
            suffix = "…" if len(missing) > 12 else ""
            issues.append(
                Issue(
                    "error",
                    "coverage_gap",
                    f"coverage.csv 缺少 {len(missing)} 个月份：{sample}{suffix}",
                    path_label,
                )
            )
    else:
        issues.append(
            Issue(
                "error",
                "coverage_empty",
                "coverage.csv 没有数据行",
                path_label,
            )
        )

    for month, archived_count in month_counts.items():
        coverage_row = parsed_rows.get(month)
        if coverage_row is None:
            issues.append(
                Issue(
                    "error",
                    "archive_month_not_in_coverage",
                    f"归档月份 {month} 不在 coverage.csv 中",
                    path_label,
                    month=month,
                )
            )
            continue
        ledger_count = coverage_row["event_count"]
        if ledger_count is not None and ledger_count != archived_count:
            issues.append(
                Issue(
                    "error",
                    "coverage_event_count_mismatch",
                    f"coverage.csv 记录 {ledger_count} 条，月文件实际 {archived_count} 条",
                    path_label,
                    int(coverage_row["row_number"]),
                    month,
                )
            )

    for month, row in parsed_rows.items():
        ledger_count = row["event_count"]
        if (
            isinstance(ledger_count, int)
            and ledger_count > 0
            and month not in month_counts
        ):
            issues.append(
                Issue(
                    "error",
                    "coverage_events_without_archive",
                    f"coverage.csv 记录 {ledger_count} 条，但找不到对应月文件",
                    path_label,
                    int(row["row_number"]),
                    month,
                )
            )

    return (
        {
            "path": path_label,
            "rows": len(raw_rows),
            "first_month": ordered_months[0] if ordered_months else None,
            "last_month": ordered_months[-1] if ordered_months else None,
        },
        issues,
    )


def validate_repository(
    root: Path,
    *,
    min_events: int = 3,
    max_events: int = 5,
    strict_count: bool = False,
    coverage_path: Path | None = None,
) -> dict[str, object]:
    root = root.resolve()
    issues: list[Issue] = []
    file_results: list[dict[str, object]] = []
    archive_root = root / "archive"

    if min_events < 1 or max_events < min_events:
        raise ValueError("event count range is invalid")

    if not archive_root.is_dir():
        issues.append(
            Issue(
                "error",
                "archive_missing",
                "找不到 archive 目录",
                "archive",
            )
        )
        markdown_files: list[Path] = []
    else:
        markdown_files = sorted(
            path for path in archive_root.rglob("*.md") if path.is_file()
        )
        if not markdown_files:
            issues.append(
                Issue(
                    "error",
                    "no_archive_files",
                    "archive 目录中没有月度 Markdown 文件",
                    "archive",
                )
            )

    month_counts: dict[str, int] = {}
    for path in markdown_files:
        path_label = display_path(path, root)
        match = ARCHIVE_PATH_RE.fullmatch(path_label)
        if match is None:
            issues.append(
                Issue(
                    "error",
                    "invalid_archive_path",
                    "月文件路径必须为 archive/YYYY/YYYY-MM.md，且目录年份与文件一致",
                    path_label,
                )
            )
            file_results.append(
                {"path": path_label, "month": None, "event_count": 0}
            )
            continue

        month = match.group("month")
        result, file_issues = validate_month_file(
            path,
            root,
            month,
            min_events,
            max_events,
            strict_count,
        )
        file_results.append(result)
        issues.extend(file_issues)
        month_counts[month] = int(result["event_count"])

    coverage_result: dict[str, object] | None = None
    if coverage_path is not None:
        coverage_result, coverage_issues = validate_coverage(
            coverage_path.resolve(),
            root,
            month_counts,
        )
        issues.extend(coverage_issues)

    issue_dicts = [issue.to_dict() for issue in issues]
    errors = sum(issue.severity == "error" for issue in issues)
    warnings = sum(issue.severity == "warning" for issue in issues)
    invalid_paths = {
        issue.path
        for issue in issues
        if issue.severity == "error"
        and issue.path is not None
        and issue.path.startswith("archive/")
    }
    valid_files = sum(
        1
        for result in file_results
        if result["path"] not in invalid_paths and result["month"] is not None
    )

    return {
        "ok": errors == 0,
        "root": str(root),
        "summary": {
            "files_scanned": len(markdown_files),
            "valid_files": valid_files,
            "invalid_files": len(markdown_files) - valid_files,
            "months_scanned": len(month_counts),
            "events_scanned": sum(month_counts.values()),
            "errors": errors,
            "warnings": warnings,
        },
        "files": file_results,
        "coverage": coverage_result,
        "issues": issue_dicts,
    }


def render_text(result: dict[str, object]) -> str:
    summary = result["summary"]
    assert isinstance(summary, dict)
    status = "PASS" if result["ok"] else "FAIL"
    lines = [
        f"Archive validation: {status}",
        f"Root: {result['root']}",
        (
            "Summary: "
            f"{summary['files_scanned']} files, "
            f"{summary['months_scanned']} months, "
            f"{summary['events_scanned']} events, "
            f"{summary['errors']} errors, "
            f"{summary['warnings']} warnings"
        ),
    ]
    coverage = result.get("coverage")
    if isinstance(coverage, dict):
        lines.append(
            "Coverage: "
            f"{coverage.get('rows', 0)} rows, "
            f"{coverage.get('first_month') or '-'} to "
            f"{coverage.get('last_month') or '-'}"
        )

    issues = result["issues"]
    assert isinstance(issues, list)
    if issues:
        lines.append("Issues:")
    for raw_issue in issues:
        assert isinstance(raw_issue, dict)
        location = str(raw_issue.get("path") or "")
        if raw_issue.get("line") is not None:
            location += f":{raw_issue['line']}"
        context = []
        if raw_issue.get("month"):
            context.append(f"month={raw_issue['month']}")
        if raw_issue.get("entry") is not None:
            context.append(f"entry={int(raw_issue['entry']):02d}")
        suffix = f" ({', '.join(context)})" if context else ""
        lines.append(
            f"  [{str(raw_issue['severity']).upper()}] "
            f"{raw_issue['code']} {location}{suffix}: {raw_issue['message']}"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only validation for archive/YYYY/YYYY-MM.md files."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="target repository root; defaults to the current directory",
    )
    parser.add_argument(
        "--coverage",
        type=Path,
        help="coverage CSV path, relative paths are resolved from --root",
    )
    parser.add_argument(
        "--skip-coverage",
        action="store_true",
        help="do not validate coverage.csv",
    )
    parser.add_argument("--min-events", type=int, default=3)
    parser.add_argument("--max-events", type=int, default=5)
    parser.add_argument(
        "--strict-count",
        action="store_true",
        help="treat monthly event-count deviations as errors instead of warnings",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="stdout format",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.skip_coverage:
        coverage_path = None
    elif args.coverage is None:
        coverage_path = root / "coverage.csv"
    else:
        coverage_path = (
            args.coverage
            if args.coverage.is_absolute()
            else root / args.coverage
        )

    try:
        result = validate_repository(
            root,
            min_events=args.min_events,
            max_events=args.max_events,
            strict_count=args.strict_count,
            coverage_path=coverage_path,
        )
    except ValueError as exc:
        parser.error(str(exc))

    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_text(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
