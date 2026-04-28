from __future__ import annotations

from agent.lexical import count_cjk_chars, keyword_terms, lexical_terms


def test_lexical_terms_handles_chinese_and_english_mixed_text():
    terms = lexical_terms("测试 test 文件")

    assert "test" in terms
    assert "测试" in terms
    assert "文件" in terms


def test_keyword_terms_applies_stopwords_and_aliases_across_languages():
    terms = keyword_terms(
        "请先展示 modified files",
        stopwords={"请先"},
        aliases={
            "展示": "show",
            "modified": "modify",
            "files": "file",
        },
    )

    assert "请先" not in terms
    assert {"show", "modify", "file"}.issubset(terms)


def test_count_cjk_chars_covers_hanzi_kana_and_hangul():
    assert count_cjk_chars("汉字かなカナ한글abc") == 8
