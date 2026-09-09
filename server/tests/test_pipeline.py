"""流水线终态单元测试（mock 外部 AI 服务）"""
import sys
import os
import pytest
import sqlite3
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestPipelinePendingReview:
    """run_pipeline 自动阶段跑完后应进入 pending_review（Q2A）"""

    def test_model_has_review_fields(self):
        """Task 模型含 review_status/review_comment/reviewed_at"""
        from app.models.task import Task
        assert "review_status" in Task.__table__.columns
        assert "review_comment" in Task.__table__.columns
        assert "reviewed_at" in Task.__table__.columns

    @pytest.mark.asyncio
    async def test_run_pipeline_sets_pending_review(self, monkeypatch):
        """mock 各阶段后，run_pipeline 终态为 pending_review + progress 95"""
        from app.database import Base
        from app.services.pipeline import pipeline_engine
        from app.models.task import Task
        from app.models.poem import Poem

        # 用独立内存库避免污染生产库
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)

        # mock 阶段方法（避免调用真实 AI API）
        async def fake_script(*a, **k):
            from app.services.critic import ScoreResult
            return "床前明月光，疑是地上霜。", ScoreResult(score=8.0, passed=True, feedback="ok")
        async def fake_storyboard(*a, **k):
            return [{"time": "0-5s", "description": "画面"}]
        async def fake_chars(*a, **k):
            return {"ref": "http://char/1.jpg", "description": "李白，唐代诗人"}
        async def fake_images(*a, **k):
            return ["http://img/1.jpg"]
        async def fake_video(*a, **k):
            return "http://video/1.mp4"
        async def fake_tts_segments(*a, **k):
            return {"success": True, "audio_url": "http://audio/1.mp3", "duration_ms": 15000}
        async def fake_subtitle(*a, **k):
            return "http://video/1.mp4"

        monkeypatch.setattr(pipeline_engine, "_generate_script", fake_script)
        monkeypatch.setattr(pipeline_engine, "_generate_storyboard", fake_storyboard)
        monkeypatch.setattr("app.services.character.character_service.generate_character_reference", fake_chars)
        monkeypatch.setattr(pipeline_engine, "_generate_images", fake_images)
        monkeypatch.setattr(pipeline_engine, "_generate_video", fake_video)
        monkeypatch.setattr(pipeline_engine, "_generate_tts_segments", fake_tts_segments)
        monkeypatch.setattr(pipeline_engine, "_burn_subtitles", fake_subtitle)

        async with Session() as db:
            # 建一首诗 + 一个任务
            db.add(Poem(id=1, title="静夜思", author="李白", dynasty="唐", content="床前明月光"))
            await db.commit()
            task = await pipeline_engine.create_task(db, 1, "douyin")
            await pipeline_engine.run_pipeline(db, task.id)

        # 终态断言
        async with Session() as db:
            from sqlalchemy import select
            result = await db.execute(select(Task).where(Task.id == task.id))
            saved = result.scalar_one()
            assert saved.status == "pending_review"
            assert saved.current_stage == "subtitle"  # 保持最后执行阶段，不回退到 review
            assert saved.progress == 95
            assert saved.review_status == "pending"


class TestCleanAudioChain:
    """#20260903 杂音修复：edge-tts 段禁用 afftdn/acompressor，避免干净信号被抬噪。

    根因：afftdn nf=-30 假设噪声底 -30dB，而 edge-tts raw 噪声底仅 -45.5dB，
    模型失配反而把底噪抬到 -35.4dB（听感"沙"）。轻链仅保留 highpass+loudnorm。
    """

    def test_full_chain_keeps_denoise(self):
        """noise_reduce=True（cosyvoice 段）保留 afftdn/acompressor 全链"""
        from app.services.pipeline import PipelineEngine
        chain = PipelineEngine._af_chain(noise_reduce=True, sr=24000)
        assert "afftdn" in chain
        assert "acompressor" in chain
        assert "loudnorm" in chain

    def test_light_chain_drops_denoise(self):
        """noise_reduce=False（edge-tts 段）去掉 afftdn/acompressor，仅轻清洗"""
        from app.services.pipeline import PipelineEngine
        chain = PipelineEngine._af_chain(noise_reduce=False, sr=24000)
        assert "afftdn" not in chain
        assert "acompressor" not in chain
        assert "highpass=f=60" in chain
        assert "loudnorm" in chain
        assert "aresample=24000" in chain


class TestSubtitleGoldenAndHold:
    """video-comm 决策 5：金句字幕加权停留（_split_timeline）+ 金句识别 + 文案金句提取

    金句字幕是可截图的"最小传播单元"：命中金句的 cue 按 golden_weight 放大"字数当量"
    参与同镜时长池分配 → 停留显著长于普通过渡句。本组为纯静态方法单测，离线无 ffmpeg。
    """

    def test_extract_golden_from_script(self):
        """文案脚本中的「引号句子」被抽成金句候选集"""
        from app.services.pipeline import PipelineEngine
        script = ('开篇铺垫……诗人写道「但愿人长久，千里共婵娟」，让异地的人破防；'
                  '又引“明月几时有”作对照。')
        out = PipelineEngine._extract_golden_from_script(script)
        assert "但愿人长久，千里共婵娟" in out
        assert "明月几时有" in out
        assert PipelineEngine._extract_golden_from_script("") == []
        assert PipelineEngine._extract_golden_from_script("全篇无引号也无金句") == []

    def test_is_golden_cue_pool_and_quote(self):
        """命中判定：金句池子串 或 cue 自带引号；短子串(<4字)防噪音"""
        from app.services.pipeline import PipelineEngine
        assert PipelineEngine._is_golden_cue(
            "但愿人长久，千里共婵娟。", ["但愿人长久，千里共婵娟"])
        assert not PipelineEngine._is_golden_cue(
            "这句写的是苏轼的旷达。", ["但愿人长久，千里共婵娟"])
        assert PipelineEngine._is_golden_cue("诗人说「但愿人长久」，异地的人最懂。", None)
        assert not PipelineEngine._is_golden_cue("这句写的是苏轼的旷达。", None)
        # 池子串 <4 字（如"明月"）过于宽泛，不命中
        assert not PipelineEngine._is_golden_cue("今晚的明月格外亮", ["明月"])

    def test_golden_cue_gets_longer_hold(self):
        """同镜多 cue：命中金句的 cue 停留变长、起点前移（从普通句匀时），不越镜长

        构造：max_chars=12（cpl=12,max_lines=1），文本拆为
        cue1「先说铺垫话。」(6字) + cue2「他此刻的处境。让人沉默。」(12字，含金句)。
        golden_weight=2.0 → cue2 当量 24 vs cue1 6 → 起点显著前移、停留变长。
        """
        from app.services.pipeline import pipeline_engine
        tl = [(0.0, 10.0, "先说铺垫话。他此刻的处境。让人沉默。")]
        plain = pipeline_engine._split_timeline(tl, chars_per_line=12, max_lines=1)
        w = pipeline_engine._split_timeline(
            tl, chars_per_line=12, max_lines=1,
            golden_lines=["他此刻的处境"], golden_weight=2.0)
        assert len(plain) == 2 and len(w) == 2

        def locate(cues, needle):
            for (_s, _e, t) in cues:
                if needle in t.replace("\\N", ""):
                    return _s, _e
            return None

        ps, pe = locate(plain, "他此刻的处境")
        ws, we = locate(w, "他此刻的处境")
        assert ps is not None and ws is not None
        assert we - ws > pe - ps, f"金句停留应变长: plain={pe-ps:.2f}s weighted={we-ws:.2f}s"
        assert ws < ps - 0.3, f"金句 cue 起点应前移(从普通句匀时): plain={ps:.2f} weighted={ws:.2f}"
        assert w[-1][1] <= 10.0 + 1e-6  # 不越镜长

    def test_no_golden_pool_keeps_legacy_behavior(self):
        """无金句池 / 权重<=1 / 无命中：输出与旧版完全一致（回归保护）"""
        from app.services.pipeline import pipeline_engine
        tl = [(0.0, 8.0, "秋风起，落叶黄。诗人登高远望。")]
        a = pipeline_engine._split_timeline(tl, chars_per_line=20, max_lines=2)
        b = pipeline_engine._split_timeline(tl, chars_per_line=20, max_lines=2,
                                            golden_lines=None, golden_weight=1.4)
        c = pipeline_engine._split_timeline(tl, chars_per_line=20, max_lines=2,
                                            golden_lines=["不存在的金句"], golden_weight=1.0)
        assert a == b == c


class TestGoldenPoemUpgrade:
    """video-comm 决策 5 升级（文案改造轮 W3）：金句池并入「原诗整句」。

    S 三段式文案落地后，白话段旁白常不带引号原样念出原诗整句——旧启发
    （仅 script 引号内句子）会漏掉；poem_content 传入时原诗行直接入池，
    使无引号的"原诗整句 cue"同样获得加权停留。
    """

    def test_poem_lines_merged_into_pool(self):
        from app.services.pipeline import PipelineEngine
        script = "这句诗，写的是不是此刻的你。"
        poem = "床前明月光，疑是地上霜。\n举头望明月，低头思故乡。"
        out = PipelineEngine._extract_golden_from_script(script, poem_content=poem)
        for line in ("床前明月光", "疑是地上霜", "举头望明月", "低头思故乡"):
            assert line in out, f"原诗整句未入池: {line}"
        assert out == sorted(set(out), key=out.index)  # 无重复

    def test_quote_lines_and_poem_lines_dedup(self):
        from app.services.pipeline import PipelineEngine
        # 同一句既在 script 引号内、又是原诗行 → 只入池一次
        script = "李白写道：「举头望明月」"
        poem = "举头望明月，低头思故乡。"
        out = PipelineEngine._extract_golden_from_script(script, poem_content=poem)
        assert out.count("举头望明月") == 1
        assert "低头思故乡" in out

    def test_short_noise_excluded(self):
        from app.services.pipeline import PipelineEngine
        poem = "风。\n月。\n惊涛拍岸，卷起千堆雪。"
        out = PipelineEngine._extract_golden_from_script("", poem_content=poem)
        assert "风" not in out and "月" not in out   # <4 字噪音
        assert "惊涛拍岸" in out and "卷起千堆雪" in out  # 长行按标点细切

    def test_poem_none_keeps_legacy_quote_only(self):
        from app.services.pipeline import PipelineEngine
        script = "他说「天生我材必有用」，千金散尽还复来。"
        out = PipelineEngine._extract_golden_from_script(script)  # 不传 poem
        assert out == ["天生我材必有用"]
        assert PipelineEngine._extract_golden_from_script("", None) == []

    def test_unquoted_poem_cue_now_hits_golden(self):
        """白话段 cue 不带引号念出原诗整句 → 原诗池使其命中（升级核心收益）"""
        from app.services.pipeline import PipelineEngine
        cue = "举头望明月，低头思故乡。"
        # 旧行为：无引号 → 不命中
        assert not PipelineEngine._is_golden_cue(cue, None)
        # 新行为：原诗整句在池中 → 命中（len>=4）
        pool = PipelineEngine._extract_golden_from_script("", poem_content="举头望明月，低头思故乡。")
        assert PipelineEngine._is_golden_cue(cue, pool)
