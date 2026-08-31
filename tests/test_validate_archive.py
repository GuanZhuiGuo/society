from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "validate_archive.py"
SPEC = importlib.util.spec_from_file_location("validate_archive", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
validate_archive = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validate_archive
SPEC.loader.exec_module(validate_archive)


def event_block(number: int, title: str, url: str | None = None) -> str:
    link = url or f"https://news.test.invalid/article-{number}"
    return f"""## {number:02d}. {title}

- 标题：{title}
- 描述：这是用于校验器自测的完整事件描述，包含明确的时间、主体与结果。
- 历史意义：这是基于样例内容的测试说明，不代表真实事件判断。
- 新闻链接：
  - [测试媒体｜测试报道｜2026-08-01]({link})
"""


class ArchiveValidatorTests(unittest.TestCase):
    def make_root(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        (root / "archive" / "2026").mkdir(parents=True)
        return temporary, root

    def write_month(self, root: Path, body: str, name: str = "2026-08.md") -> None:
        (root / "archive" / "2026" / name).write_text(
            "# 中国社会百态｜2026 年 08 月\n\n" + body,
            encoding="utf-8",
        )

    def codes(self, result: dict[str, object]) -> set[str]:
        issues = result["issues"]
        assert isinstance(issues, list)
        return {str(issue["code"]) for issue in issues}

    def test_valid_month_passes(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        body = "\n".join(
            event_block(index, f"样本事件{index}") for index in range(1, 4)
        )
        self.write_month(root, body)

        result = validate_archive.validate_repository(root, coverage_path=None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["events_scanned"], 3)
        self.assertEqual(result["issues"], [])

    def test_missing_field_and_bad_link_fail(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        bad_event = """## 01. 不完整事件

- 标题：不完整事件
- 描述：缺少历史意义字段，且链接协议不合规。
- 新闻链接：
  - [测试来源](javascript:alert(1))
"""
        body = bad_event + event_block(2, "样本事件2") + event_block(3, "样本事件3")
        self.write_month(root, body)

        result = validate_archive.validate_repository(root, coverage_path=None)

        self.assertFalse(result["ok"])
        self.assertIn("missing_field", self.codes(result))
        self.assertIn("invalid_link_format", self.codes(result))
        self.assertIn("missing_valid_news_link", self.codes(result))

    def test_count_is_warning_by_default_and_error_in_strict_mode(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        self.write_month(root, event_block(1, "单条草稿"))

        normal = validate_archive.validate_repository(root, coverage_path=None)
        strict = validate_archive.validate_repository(
            root,
            strict_count=True,
            coverage_path=None,
        )

        self.assertTrue(normal["ok"])
        self.assertEqual(normal["summary"]["warnings"], 1)
        self.assertFalse(strict["ok"])
        self.assertEqual(strict["summary"]["errors"], 1)

    def test_invalid_archive_path_fails(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        self.write_month(root, event_block(1, "错误路径"), name="August.md")

        result = validate_archive.validate_repository(root, coverage_path=None)

        self.assertFalse(result["ok"])
        self.assertIn("invalid_archive_path", self.codes(result))

    def test_requested_coverage_range_has_380_months(self) -> None:
        months = validate_archive.month_sequence("1995-01", "2026-08")
        self.assertEqual(months[0], "1995-01")
        self.assertEqual(months[-1], "2026-08")
        self.assertEqual(len(months), 380)

    def test_checked_in_coverage_is_complete_and_consistent(self) -> None:
        result = validate_archive.validate_repository(
            REPOSITORY_ROOT,
            coverage_path=REPOSITORY_ROOT / "coverage.csv",
        )
        coverage = result["coverage"]
        issues = result["issues"]
        errors = [issue for issue in issues if issue["severity"] == "error"]

        self.assertEqual(errors, [])
        assert isinstance(coverage, dict)
        self.assertEqual(coverage["rows"], 380)
        self.assertEqual(coverage["first_month"], "1995-01")
        self.assertEqual(coverage["last_month"], "2026-08")

    def test_cli_emits_text_and_json_summaries(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        body = "\n".join(
            event_block(index, f"命令行样本{index}") for index in range(1, 4)
        )
        self.write_month(root, body)

        base_command = [
            sys.executable,
            str(SCRIPT_PATH),
            "--root",
            str(root),
            "--skip-coverage",
        ]
        text_run = subprocess.run(
            base_command + ["--format", "text"],
            check=False,
            capture_output=True,
            text=True,
        )
        json_run = subprocess.run(
            base_command + ["--format", "json"],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(text_run.returncode, 0, text_run.stderr)
        self.assertIn("Archive validation: PASS", text_run.stdout)
        self.assertEqual(json_run.returncode, 0, json_run.stderr)
        self.assertTrue(json.loads(json_run.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
