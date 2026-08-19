"""代码分析专家(论文 §III-B2 图4)单元测试: 递归遍历 / 去重 / 外部依赖。"""

import json

from rcagent.config import load_config
from rcagent.env.local import CODE_REPO_DIR
from rcagent.experts.code_agent import CodeExpertAgent
from rcagent.llm.client import LLMClient

CFG = load_config()


def make_agent(script):
    llm = LLMClient(CFG.llm, mock_script=script)
    return CodeExpertAgent(llm, CODE_REPO_DIR, CFG)


class TestFileResolution:
    def test_simple_name_resolution(self):
        agent = make_agent(lambda m, p: "{}")
        p = agent._resolve_file("JobConnectorSinkConnectionFailService")
        assert p is not None and p.name.endswith(".java")

    def test_fqcn_resolution(self):
        agent = make_agent(lambda m, p: "{}")
        p = agent._resolve_file("com.alibaba.flink.advisor.mapper.FlinkLifecycleMapper")
        assert p is not None and "FlinkLifecycleMapper" in p.name

    def test_external_dependency_returns_none(self):
        agent = make_agent(lambda m, p: "{}")
        assert agent._resolve_file("org.apache.commons.lang3.StringUtils") is None
        assert agent._resolve_file("NoSuchClass") is None


class TestRecursiveAnalysis:
    def test_follows_suggestions_and_dedupes(self):
        """图4 流程: 入口类 → 建议类入队 → 去重 → 无新建议时停止。"""
        analyzed = []

        def script(messages, params):
            user = messages[-1]["content"]
            # 总结阶段 prompt 以固定前缀开头
            if "Below are analyses" in user:
                return json.dumps({"summary": "merged explanation"})
            # 从 prompt 的代码块里取类名做建议(模拟 LLM 理解代码依赖)
            if "JobConnectorSinkConnectionFailService" in user:
                analyzed.append("JobConnectorSinkConnectionFailService")
                return json.dumps({"summary": "checks SINK_CONN_ERROR events",
                                   "suggested_classes": ["FlinkLifecycleMapper",
                                                         "RuleDecisionBase",
                                                         "FlinkLifecycleMapper"]})
            if "FlinkLifecycleMapper" in user:
                analyzed.append("FlinkLifecycleMapper")
                return json.dumps({"summary": "data access layer",
                                   "suggested_classes": ["FlinkLifecycle"]})
            if "RuleDecisionBase" in user:
                analyzed.append("RuleDecisionBase")
                return json.dumps({"summary": "abstract base", "suggested_classes": []})
            if "FlinkLifecycle" in user:
                analyzed.append("FlinkLifecycle")
                return json.dumps({"summary": "entity", "suggested_classes": []})
            return json.dumps({"summary": "fallback"})

        agent = make_agent(script)
        out = agent.run("JobConnectorSinkConnectionFailService")
        assert "merged explanation" in out
        # 4 个本地类各分析一次(FlinkLifecycleMapper 重复建议被去重)
        assert analyzed == ["JobConnectorSinkConnectionFailService",
                            "FlinkLifecycleMapper", "RuleDecisionBase", "FlinkLifecycle"]

    def test_external_suggestion_skipped(self):
        """外部依赖(仓库外类)不进任务队列。"""
        queue_hits = []

        def script(messages, params):
            user = messages[-1]["content"]
            if "JobConnectorSinkConnectionFailService" in user:
                return json.dumps({"summary": "s",
                                   "suggested_classes": [
                                       "org.apache.commons.lang3.StringUtils",
                                       "ExternalUtil",  # 仓库内,应入队
                                   ]})
            if "ExternalUtil" in user:
                queue_hits.append("ExternalUtil")
                return json.dumps({"summary": "util", "suggested_classes": []})
            return json.dumps({"summary": "merged"})

        agent = make_agent(script)
        out = agent.run("JobConnectorSinkConnectionFailService")
        assert "ExternalUtil" in queue_hits  # 仓库内类被分析
        assert "StringUtils" not in queue_hits  # 外部类被跳过

    def test_max_files_limit(self):
        """循环上限: 永不停止的建议不会无限递归。"""
        calls = {"n": 0}

        def script(messages, params):
            calls["n"] += 1
            return json.dumps({"summary": "s", "suggested_classes": ["RuleDecisionBase"]})

        agent = make_agent(script)
        out = agent.run("JobConnectorSinkConnectionFailService")
        assert calls["n"] <= 25  # max_files=20 + 总结 1 次 + 余量

class TestGenericRepo:
    """通用性(PRD §2.11): 仓库即插件,支持多语言与频繁变动。"""

    def test_python_repo_supported(self, tmp_path):
        (tmp_path / "diagnoser.py").write_text("def diagnose():\n    pass\n", encoding="utf-8")
        (tmp_path / "models.py").write_text("class Job:\n    pass\n", encoding="utf-8")
        agent = make_agent(lambda m, p: "{}")
        agent.repo_dir = tmp_path
        assert agent._resolve_file("diagnoser") is not None
        assert agent._resolve_file("models") is not None
        assert agent._resolve_file("external_module") is None

    def test_module_path_resolution(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "mod.py").write_text("z = 3\n", encoding="utf-8")
        agent = make_agent(lambda m, p: "{}")
        agent.repo_dir = tmp_path
        assert agent._resolve_file("pkg.mod") is not None
        assert agent._resolve_file("mod") is not None

    def test_index_refreshes_on_new_files(self, tmp_path):
        """代码频繁变动: 新增文件立即可见(索引每次调用重建)。"""
        (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
        agent = make_agent(lambda m, p: "{}")
        agent.repo_dir = tmp_path
        assert agent._resolve_file("b") is None
        (tmp_path / "b.py").write_text("y = 2\n", encoding="utf-8")
        assert agent._resolve_file("b") is not None

    def test_code_content_read_fresh(self, tmp_path):
        """内容实时读取: 文件修改后分析到的是新内容(不缓存)。"""
        f = tmp_path / "svc.py"
        f.write_text("VERSION = 1\n", encoding="utf-8")
        seen = []

        def script(messages, params):
            seen.append(messages[-1]["content"])
            return json.dumps({"summary": "s", "suggested_classes": []})

        llm = LLMClient(CFG.llm, mock_script=script)
        agent = CodeExpertAgent(llm, tmp_path, CFG)
        agent.run("svc")
        f.write_text("VERSION = 2\n", encoding="utf-8")
        agent.run("svc")
        # 分析阶段的 prompt 含代码块;第二次应读到新内容
        analyze_prompts = [c for c in seen if "```" in c]
        assert "VERSION = 1" in analyze_prompts[0]
        assert "VERSION = 2" in analyze_prompts[-1]


class TestUnparsable:
    def test_unparsable_analysis_skipped(self):
        def script(messages, params):
            return "not json"

        agent = make_agent(script)
        out = agent.run("JobConnectorSinkConnectionFailService")
        assert "no code files analyzed" in out
