"""蒸馏 prompt 模板测试（任务包 §4.4）。"""
import pytest

from distill import prompts

ALL_PROMPTS = [
    prompts.STEP1_SYSTEM,
    prompts.STEP1_USER,
    prompts.STEP2_SYSTEM,
    prompts.STEP2_USER,
    prompts.STEP3_USER,
    prompts.STEP4_USER,
]


def test_step1_user_has_title_and_raw_content_placeholders():
    assert "{title}" in prompts.STEP1_USER
    assert "{raw_content}" in prompts.STEP1_USER


def test_step2_user_has_structured_json_placeholder():
    assert "{structured_json}" in prompts.STEP2_USER


def test_step3_user_has_rewrite_body_placeholder():
    assert "{rewrite_body}" in prompts.STEP3_USER


def test_step4_user_has_segments_placeholder():
    assert "{segments}" in prompts.STEP4_USER


def test_step1_user_format_does_not_raise_key_error():
    rendered = prompts.STEP1_USER.format(title="AI 观察", raw_content="正文内容")
    assert "AI 观察" in rendered
    assert "正文内容" in rendered
    assert "{" not in rendered  # 占位符已全部替换


def test_step2_user_format_does_not_raise_key_error():
    rendered = prompts.STEP2_USER.format(structured_json='{"summary": "x"}')
    assert '{"summary": "x"}' in rendered


def test_step3_and_step4_user_format_does_not_raise_key_error():
    assert "稿子" in prompts.STEP3_USER.format(rewrite_body="稿子")
    assert "seg1" in prompts.STEP4_USER.format(segments="seg1")


@pytest.mark.parametrize("prompt", ALL_PROMPTS)
def test_all_prompts_are_non_empty_strings(prompt):
    assert isinstance(prompt, str)
    assert prompt.strip()


def test_system_prompts_have_no_placeholders():
    """system prompt 是静态指令，format 时不该有未替换占位符。"""
    assert "{" not in prompts.STEP1_SYSTEM
    assert "{" not in prompts.STEP2_SYSTEM


def test_step1_system_asks_for_json_with_chapters():
    assert "JSON" in prompts.STEP1_SYSTEM
    assert "chapters" in prompts.STEP1_SYSTEM
    assert "summary" in prompts.STEP1_SYSTEM


def test_step2_system_asks_for_colloquial_podcast_script():
    assert "播客" in prompts.STEP2_SYSTEM
    assert "口语" in prompts.STEP2_SYSTEM
    assert "不要 markdown" in prompts.STEP2_SYSTEM


def test_step3_user_specifies_segment_rules():
    assert "3-5 段" in prompts.STEP3_USER
    assert "normal" in prompts.STEP3_USER


def test_step4_user_specifies_audio_format():
    assert "m4a" in prompts.STEP4_USER
    assert "128kbps" in prompts.STEP4_USER
