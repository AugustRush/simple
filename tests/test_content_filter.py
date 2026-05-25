"""Tests for the content filter defense layers."""

import json
import pytest

from agent.security.content_filter import (
    ContentFilter,
    _extract_ngrams,
    _feature_set,
    filter_tool_results,
    summarize_tool_result,
)


class TestNgramExtraction:
    def test_empty_text(self):
        assert _extract_ngrams("", 2) == []
        assert _extract_ngrams("a", 3) == []

    def test_single_ngrams(self):
        result = _extract_ngrams("hello world", 2)
        assert "he" in result
        assert "ld" in result
        assert len(result) == len("hello world") - 1

    def test_multiple_n(self):
        features = _feature_set("abc", max_n=2)
        assert "a" in features
        assert "b" in features
        assert "c" in features
        assert "ab" in features
        assert "bc" in features


class TestContentFilter:
    def test_new_filter_scores_safe(self):
        cf = ContentFilter()
        score = cf.score("This is a normal text about Python programming")
        assert 0.0 <= score <= 0.5, f"Normal text should not score high, got {score}"

    def test_empty_text_scores_zero(self):
        cf = ContentFilter()
        assert cf.score("") == 0.0

    def test_is_risky_respects_threshold(self):
        cf = ContentFilter()
        assert cf.is_risky("hello world", threshold=0.9) is False

    def test_learn_and_score(self):
        cf = ContentFilter(alpha=0.1)
        # Learn a risky pattern
        cf.learn("blocked content pattern X", is_risky=True)
        cf.learn("blocked content pattern X", is_risky=True)
        cf.learn("blocked content pattern X", is_risky=True)
        # Learn safe patterns
        cf.learn("normal python code", is_risky=False)
        cf.learn("normal python code", is_risky=False)
        cf.learn("normal python code", is_risky=False)

        # Exact risky text should score high
        risky_score = cf.score("blocked content pattern X")
        assert risky_score > 0.5, f"Risky text should score high, got {risky_score}"

        # Normal text should score low
        safe_score = cf.score("normal python code")
        assert safe_score < risky_score, (
            f"Safe text should score lower than risky: "
            f"safe={safe_score:.3f} risky={risky_score:.3f}"
        )

    def test_learn_batch(self):
        cf = ContentFilter()
        cf.learn_batch(["pattern A text", "pattern B text"], is_risky=True)
        cf.learn_batch(["safe code here", "normal output"], is_risky=False)
        assert cf.score("pattern A text") > 0.5
        assert cf.score("safe code here") < 0.5

    def test_persistence_roundtrip(self, tmp_path):
        cf = ContentFilter(alpha=0.2)
        cf.learn("risky stuff here", is_risky=True)
        cf.learn("safe content here", is_risky=False)

        path = tmp_path / "model.json"
        cf.save(path)

        loaded = ContentFilter.load(path)
        assert loaded.alpha == 0.2
        assert loaded.score("risky stuff here") == cf.score("risky stuff here")
        assert loaded.score("safe content here") == cf.score("safe content here")

    def test_load_missing_file(self, tmp_path):
        cf = ContentFilter.load(tmp_path / "nonexistent.json")
        assert cf.alpha == 0.1
        assert cf.score("anything") == 0.5

    def test_stats(self):
        cf = ContentFilter()
        cf.learn("a", is_risky=True)
        cf.learn("b", is_risky=False)
        s = cf.stats
        assert s["safe_examples"] > 0
        assert s["risky_examples"] > 0
        assert s["vocab_size"] > 0


class TestSummarizeToolResult:
    def test_read_file_summary(self):
        content = "\n".join(["line1"] * 100)
        result = summarize_tool_result("read_file", content)
        parsed = json.loads(result)
        assert parsed["ok"] is True
        assert parsed["summarized"] is True
        assert parsed["lines"] == 100
        assert "line1" in parsed["preview"]

    def test_shell_summary(self):
        output = "\n".join(["compilation output"] * 50)
        result = summarize_tool_result("shell", output)
        parsed = json.loads(result)
        assert parsed["ok"] is True
        assert parsed["summarized"] is True
        assert parsed["output_lines"] == 50

    def test_short_output_preserved(self):
        short = "just a short message"
        result = summarize_tool_result("shell", short)
        parsed = json.loads(result)
        assert parsed["ok"] is True
        assert parsed["summarized"] is True
        assert "just a short message" in parsed["preview"]

    def test_list_files_kept_if_short(self):
        short_list = "\n".join(f"file_{i}.txt" for i in range(5))
        result = summarize_tool_result("list_files", short_list)
        assert result == short_list  # Short lists kept as-is

    def test_generic_long_truncation(self):
        long_text = "x" * 3000
        result = summarize_tool_result("unknown_tool", long_text)
        parsed = json.loads(result)
        assert parsed["summarized"] is True
        assert parsed["size_bytes"] == 3000
        assert len(parsed["preview"]) < 500

    def test_error_result_preserved(self):
        error = json.dumps({"ok": False, "error": "permission denied"})
        result = summarize_tool_result("shell", error)
        parsed = json.loads(result)
        assert parsed["ok"] is False
        assert parsed["error"] == "permission denied"
        assert parsed["summarized"] is True

    def test_redacted_summary_omits_raw_preview(self):
        result = summarize_tool_result(
            "shell",
            "sensitive blocked text",
            include_preview=False,
        )
        parsed = json.loads(result)
        assert parsed["summarized"] is True
        assert parsed["preview_omitted"] is True
        assert "sensitive blocked text" not in result


class TestFilterToolResults:
    def test_safe_results_passed_through(self):
        cf = ContentFilter()
        cf.learn("safe output", is_risky=False)
        cf.learn("safe output", is_risky=False)

        tool_calls = [{"name": "read_file", "id": "1", "input": {}}]
        results = ["safe output"]
        filtered, risky = filter_tool_results(cf, tool_calls, results, threshold=0.9)
        assert risky == []
        assert filtered == results

    def test_risky_results_summarized(self):
        cf = ContentFilter()
        # Train on repeated blocked patterns
        training_text = "blocked pattern repeated many times blocked blocked extra padding"
        for _ in range(20):
            cf.learn(training_text, is_risky=True)

        tool_calls = [{"name": "shell", "id": "1", "input": {}}]
        results = [training_text]  # same text that was trained as risky
        filtered, risky = filter_tool_results(cf, tool_calls, results, threshold=0.5)
        assert len(risky) == 1
        assert risky[0] == 0
        assert filtered[0] != results[0]
        parsed = json.loads(filtered[0])
        assert parsed["summarized"] is True

    def test_mixed_batch(self):
        cf = ContentFilter()
        for _ in range(5):
            cf.learn("bad content here", is_risky=True)
        cf.learn("good output", is_risky=False)
        cf.learn("good output", is_risky=False)

        tool_calls = [
            {"name": "read_file", "id": "1", "input": {}},
            {"name": "shell", "id": "2", "input": {}},
        ]
        results = ["good output", "bad content here"]
        filtered, risky = filter_tool_results(cf, tool_calls, results, threshold=0.5)
        assert risky == [1]
        assert filtered[0] == "good output"  # unchanged
        assert filtered[1] != "bad content here"  # summarized
