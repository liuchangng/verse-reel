"""TTS fail-loud 门禁 守护测试（2026-09-10 任务005 无声片事故，第二次）

事故：edge-tts 7 段 × 3 次重试全失败（瞬时网络/代理故障），每段降级为
anullsrc 静音兜底；旧逻辑照常合成静音 audio.mp3 并出"成功的无声片"，
用户侧零提示。这是该问题第二次出现——上次（9206c37）只修了"静音兜底分段
不许复用"，没修"全失败还照常出片"。

修复：``pipeline._tts_loudness_gate`` —— 全部段落 fallback → 拒绝出片
（success=False → run_stage raise → Job failed）；部分 fallback → 保留但在
task.error_message 留显式警告。
"""
import pytest

from app.services.pipeline import pipeline_engine


class _FakeTask:
    def __init__(self, task_id=5):
        self.id = task_id
        self.error_message = None


def _seg(i, fallback=False, error=None):
    d = {"index": i, "text": f"段{i}", "duration": 3.0, "path": f"narration_{i}.mp3"}
    if fallback:
        d.update({"fallback": True, "error": error})
    return d


class TestTtsLoudnessGate:
    def test_all_normal_passes(self):
        """全部正常 → 通过，不出警告。"""
        task = _FakeTask()
        assert pipeline_engine._tts_loudness_gate(task, [_seg(0), _seg(1)]) is None
        assert task.error_message is None

    def test_all_fallback_rejected(self):
        """全部降级静音 → 拒绝出片，错误信息含分段原因与恢复指引。"""
        task = _FakeTask()
        segs = [_seg(0, True, "NoAudioReceived"), _seg(1, True, "timeout")]
        result = pipeline_engine._tts_loudness_gate(task, segs)
        assert result is not None and result["success"] is False
        assert "拒绝出无声片" in result["error"]
        assert "NoAudioReceived" in result["error"]
        assert "2 段失败" in result["error"]

    def test_partial_fallback_warns_but_passes(self):
        """部分降级 → 放行合成，但 task.error_message 留显式警告。"""
        task = _FakeTask()
        segs = [_seg(0), _seg(1, True, "timeout"), _seg(2)]
        assert pipeline_engine._tts_loudness_gate(task, segs) is None
        assert task.error_message is not None
        assert "1/3 段降级" in task.error_message
        assert "timeout" in task.error_message

    def test_no_fallback_key_treated_as_normal(self):
        """无 fallback 键（旧数据/正常段）→ 视为正常。"""
        task = _FakeTask()
        segs = [{"index": 0, "text": "x", "duration": 1.0, "path": "a.mp3"}]
        assert pipeline_engine._tts_loudness_gate(task, segs) is None
