import pytest
from worker_wrapper.result_parser import ResultParseError, ResultParser


class TestResultParser:
    def test_extracts_json_from_result_tags(self):
        """Should extract JSON from <result>...</result>."""
        stdout = """
        Some text output
        <result>
        {"status": "success", "commit_sha": "abc123"}
        </result>
        More text
        """
        result = ResultParser.parse(stdout)
        assert result["status"] == "success"
        assert result["commit_sha"] == "abc123"

    def test_handles_missing_result_tags(self):
        """Should return None when no result tags found."""
        stdout = "Agent finished without result tags"
        result = ResultParser.parse(stdout)
        assert result is None

    def test_handles_malformed_json(self):
        """Should raise ParseError for invalid JSON inside tags."""
        stdout = "<result>not valid json</result>"
        with pytest.raises(ResultParseError):
            ResultParser.parse(stdout)

    def test_handles_multiline_json(self):
        """Should handle pretty-printed JSON."""
        stdout = """<result>
        {
            "status": "success",
            "summary": "Done"
        }
        </result>"""
        result = ResultParser.parse(stdout)
        assert result["status"] == "success"

    def test_extracts_first_result_block(self):
        """If multiple result blocks, extract first one."""
        stdout = '<result>{"a": 1}</result> text <result>{"b": 2}</result>'
        result = ResultParser.parse(stdout)
        assert result["a"] == 1
