"""Privacy regressions for parsing and persisting electricity records."""
import json
from pathlib import Path

import pytest

from nju_electric_query import parse_html, save_result


def test_daily_record_schemas_disallow_student_number():
    repository_root = Path(__file__).resolve().parents[2]
    schema_paths = (
        repository_root
        / "specs/001-daily-data-pipeline/contracts/daily-record.schema.json",
        repository_root / "tests/schemas/daily-record.schema.json",
    )

    schemas = [json.loads(path.read_text(encoding="utf-8")) for path in schema_paths]

    assert schemas[0] == schemas[1]
    for schema in schemas:
        assert "学号" not in schema["properties"]
        assert schema["additionalProperties"] is False


def test_parse_html_does_not_return_student_number():
    student_number = "STUDENT-ID-TEST-ONLY"
    html = (
        '<script>this.check = {'
        '"sysName":"仙林校区",'
        '"buildName":"19幢",'
        '"roomName":"19栋第16层1613",'
        f'"stuempno":"{student_number}"'
        '};</script>'
    )

    result = parse_html(html)

    assert result["房间"] == "19栋第16层1613"
    assert "学号" not in result
    assert student_number not in json.dumps(result, ensure_ascii=False)


@pytest.mark.asyncio
async def test_save_result_discards_student_number(tmp_path):
    student_number = "STUDENT-ID-TEST-ONLY"
    result = {
        "校区": "仙林校区",
        "楼栋": "19幢",
        "房间": "19栋第16层1613",
        "学号": student_number,
        "剩余电量": "125.50度",
        "success": True,
    }

    assert await save_result(result, tmp_path, quiet=True)

    saved_files = list(tmp_path.rglob("*.json"))
    assert len(saved_files) == 1
    saved_text = saved_files[0].read_text(encoding="utf-8")
    saved_data = json.loads(saved_text)
    assert "学号" not in saved_data
    assert student_number not in saved_text
    assert saved_data["剩余电量"] == "125.50度"
