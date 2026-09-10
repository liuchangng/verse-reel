"""守护测试：_audio_acrossfade_filter 的滤镜图构造契约。

背景（2026-09-10 双 bug 修复）：
  旧版 xfade 视频转场 + concat 音频顺序拼接 → 视频被压缩 (n-1)*trans 秒，
  音频未被压缩 → 音轨相对字幕/画面后漂（7段×0.4s = 末段漂 2.4s）。
  修复：音频也走 acrossfade 链（独立标签），与视频 xfade 同步压缩。

契约点：
  1. n<=1 或 trans<=0 → 退化为纯 concat（旧行为），不引入 acrossfade。
  2. 有转场时：链头用 [0:a]，每段引入 [k:a]acrossfade=d=trans，
     末段目标 [aout]（供 -map）。
  3. 相邻段用 aa{k} 独立中间标签（规避本机合并滤镜图丢标签问题）。
  4. 链段数 = n-1（每段一次 acrossfade）。
"""
import re

from app.services.pipeline import PipelineEngine


def test_n_1_returns_identity():
    """单段无转场，直接 [0:a]（不能 acrossfade 自己）。"""
    f = PipelineEngine._audio_acrossfade_filter(1, 0.4)
    assert f == "[0:a]"


def test_zero_trans_falls_back_to_concat():
    """trans=0 时保持旧 concat 行为（无重叠），n=3。"""
    f = PipelineEngine._audio_acrossfade_filter(3, 0.0)
    # 期望: [0:a][1:a][2:a]concat=n=3:v=0:a=1[aout]
    assert f == "[0:a][1:a][2:a]concat=n=3:v=0:a=1[aout]"
    assert "acrossfade" not in f


def test_two_segments_chain():
    """两段 + trans=0.4 → 一次 acrossfade，末端直接到 [aout]（无 aa 中间标签）。"""
    f = PipelineEngine._audio_acrossfade_filter(2, 0.4)
    # [0:a][1:a]acrossfade=d=0.40[aout]
    assert f == "[0:a][1:a]acrossfade=d=0.40[aout]"
    assert "aa" not in f


def test_three_segments_uses_aa1_middle_label():
    """三段 → [0:a][1:a]acrossfade[d->aa1];[aa1][2:a]acrossfade[aout]。"""
    f = PipelineEngine._audio_acrossfade_filter(3, 0.4)
    parts = f.split(";")
    assert len(parts) == 2
    assert parts[0] == "[0:a][1:a]acrossfade=d=0.40[aa1]"
    assert parts[1] == "[aa1][2:a]acrossfade=d=0.40[aout]"


def test_seven_segments_all_labels_sequential():
    """7 段 → 6 个 acrossfade 段，中间标签 aa1..aa5，末端 [aout]。"""
    f = PipelineEngine._audio_acrossfade_filter(7, 0.4)
    parts = f.split(";")
    assert len(parts) == 6
    # 链头
    assert parts[0].startswith("[0:a][1:a]acrossfade=d=0.40[aa1]")
    # 中段使用独立 aa{k} 标签
    assert "[aa1][2:a]acrossfade" in parts[1]
    assert "[aa2][3:a]acrossfade" in parts[2]
    assert "[aa3][4:a]acrossfade" in parts[3]
    assert "[aa4][5:a]acrossfade" in parts[4]
    # 末端
    assert "[aa5][6:a]acrossfade=d=0.40[aout]" == parts[5]
    # acrossfade 出现次数 == n-1
    assert f.count("acrossfade") == 6


def test_each_acrossfade_uses_same_trans():
    """所有跨段都用同一 trans 值（保持与视频 xfade 同步）。"""
    f = PipelineEngine._audio_acrossfade_filter(5, 0.4)
    # 提取所有 d= 值
    ds = re.findall(r"acrossfade=d=([\d.]+)", f)
    assert len(ds) == 4
    assert all(d == "0.40" for d in ds)
