"""隐私政策正文加载（2026-10-21 真机 E2E 回归）

真机上发现的问题：政策正文**第一行是原始 HTML 注释**。

    <!-- 听匣隐私政策 -->
    听匣隐私政策
    版本：v1　最后更新：2026-10-21

原因：政策文件头用 HTML 注释带元信息（version / updated），
加载器只剥了那两条 —— 但文件**第一行**还写着 `<!-- 听匣隐私政策 -->`
（当初是当标题用的），于是原样显示给了用户。

这个 bug 在单测里测不出来（没人断言正文首行），
是真机把政策从头到尾渲染一遍才看见的。
"""

from stashbox.backend.common.policies import load_privacy_policy, privacy_policy_version


def test_政策正文不含任何HTML注释():
    policy = load_privacy_policy()
    assert "<!--" not in policy["content"], "政策正文里残留了 HTML 注释"
    assert "-->" not in policy["content"], "政策正文里残留了 HTML 注释"


def test_正文第一行是标题不是注释():
    policy = load_privacy_policy()
    first = policy["content"].splitlines()[0].strip()
    assert first.startswith("# "), f"正文第一行应该是 Markdown 标题，实际是 {first!r}"
    assert "听匣隐私政策" in first


def test_元信息能正确读出():
    policy = load_privacy_policy()
    assert policy["version"] == "v1"
    assert policy["updated_at"] == "2026-10-21"
    assert policy["title"] == "听匣隐私政策"
    assert privacy_policy_version() == "v1"


def test_正文非空且包含关键章节():
    policy = load_privacy_policy()
    content = policy["content"]
    assert len(content) > 500, "政策正文太短了"
    for section in ("我们收集哪些信息", "第三方服务", "保存期限", "你的权利", "注销账号"):
        assert section in content, f"政策正文缺少章节：{section}"
