"""GitHub Actions workflow 文件验证（CP1.3.1）。"""
from pathlib import Path
import yaml

REPO_ROOT = Path(__file__).parents[3]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"


def test_workflows_dir_exists():
    """.github/workflows/ 目录存在"""
    assert WORKFLOWS_DIR.is_dir(), f"目录不存在: {WORKFLOWS_DIR}"


def test_at_least_2_workflow_files():
    """≥ 2 workflow 文件（test.yml + lint.yml）"""
    if not WORKFLOWS_DIR.is_dir():
        # 上一个测试会 fail，这里跳过
        import pytest
        pytest.skip(".github/workflows/ 不存在")
    ymls = list(WORKFLOWS_DIR.glob("*.yml"))
    assert len(ymls) >= 2, f"只有 {len(ymls)} workflow 文件: {[y.name for y in ymls]}"


def test_workflow_yaml_legal():
    """每个 workflow YAML 合法 + 必含 on/jobs 字段"""
    if not WORKFLOWS_DIR.is_dir():
        import pytest
        pytest.skip(".github/workflows/ 不存在")
    for yml in WORKFLOWS_DIR.glob("*.yml"):
        data = yaml.safe_load(yml.read_text())
        # YAML 1.1 compatibility: 'on' key parses as Python True
        assert "on" in data or True in data, f"{yml.name} 缺 on 字段"
        assert "jobs" in data, f"{yml.name} 缺 jobs 字段"
