"""配置管理模块"""
import os
from pathlib import Path
from pydantic_settings import BaseSettings
from pydantic import Field


# 获取项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.parent
# 数据库/输出目录：与导入脚本 import_xml_fast.py 保持一致，统一指向 server/data
DATA_DIR = PROJECT_ROOT / "server" / "data"


# ====== 档位档案（video-comm 设计文档 §三「时长档位化」，决策 1 定稿）======
# 单一事实源：文案字数 / 镜头数 / 成片时长窗口 + 平台→档位映射。
# 本轮默认产出 = S 快档 × douyin/kuaishou/xiaohongshu（用户 2026-09-03 拍板）；
# L 深档 + bilibili/youtube 为后续扩展——档案先行落地（文案改造轮 W1），
# 供 critic 提示词档位化（W2-W4）与成片时长校验（W5）统一引用，杜绝散落魔数。
TIER_PROFILES: dict[str, dict] = {
    "S": {
        "label": "快档",
        "chars_min": 80, "chars_max": 130,   # 解说文案字数预算（script 与 Σnarration 双口径）
        "shots_min": 6, "shots_max": 9,       # 分镜镜头预算
        "dur_min": 25, "dur_max": 40,         # 成片目标时长窗（秒）
        "dur_hard_max": 50,                   # 硬上限（超窗仅告警不自动截断，见 W5）
        "shot_sec_min": 3, "shot_sec_max": 7, # 单镜时长参考
    },
    "L": {
        "label": "深档",
        "chars_min": 300, "chars_max": 450,
        "shots_min": 14, "shots_max": 22,
        "dur_min": 90, "dur_max": 150,
        "dur_hard_max": None,                 # L 无硬上限（扩展期再定）
        "shot_sec_min": 5, "shot_sec_max": 10,
    },
}

# 平台 → 档位映射（PLATFORM_CONFIG 无档位维度；未知平台兜底 S，
# 与 PLATFORM_CONFIG.get(platform, douyin) 的兜底口径一致）
PLATFORM_TIER: dict[str, str] = {
    "douyin": "S", "kuaishou": "S", "xiaohongshu": "S",
    "bilibili": "L", "youtube": "L",
}


def tier_of(platform: str | None) -> str:
    """平台 → 档位；未知/空平台回退 S（与 PLATFORM_CONFIG 兜底 douyin 一致）。"""
    return PLATFORM_TIER.get((platform or "").lower(), "S")


def tier_profile(tier: str | None) -> dict:
    """档位档案；未知/空档位回退 S。"""
    return TIER_PROFILES.get((tier or "").upper(), TIER_PROFILES["S"])


class Settings(BaseSettings):
    """应用配置"""
    
    # ====== 文本模型配置 ======
    text_api_key: str = Field(default="sk-AOzSrTPz1GNuZR3XxJcEmhloPkvUAsMCZUWcUExotFdjOrAN")
    text_base_url: str = Field(default="https://api.agnes-ai.cn/v1")
    text_model: str = Field(default="agnes-2.5-flash")
    text_concurrency: int = Field(default=5)
    
    # ====== 图片模型配置 ======
    image_api_key: str = Field(default="sk-AOzSrTPz1GNuZR3XxJcEmhloPkvUAsMCZUWcUExotFdjOrAN")
    image_base_url: str = Field(default="https://api.agnes-ai.cn/v1")
    image_model: str = Field(default="agnes-image-2.1-flash")
    image_concurrency: int = Field(default=5)
    
    # ====== 视频模型配置 ======
    video_api_key: str = Field(default="sk-AOzSrTPz1GNuZR3XxJcEmhloPkvUAsMCZUWcUExotFdjOrAN")
    video_base_url: str = Field(default="https://api.agnes-ai.cn/v1")
    video_model: str = Field(default="agnes-video-v2.0")
    # agnes 视频硬限流 1 次/分钟，必须串行（并发 1）
    video_concurrency: int = Field(default=1)
    # 视频提交最小间隔（秒）：跨任务全局节流，>=60s 才不会撞 429
    video_min_interval: float = Field(default=62.0)

    # ====== TTS 配置 ======
    # 注意：TTS 已合并进主后端（进程内，见 app/services/tts_core.py），
    # 不再依赖独立 tts-server。tts_server_url 仅作历史兼容占位，未再使用。
    tts_server_url: str = Field(default="http://localhost:8003")
    tts_voice: str = Field(default="zh-CN-YunxiNeural")
    tts_speed: float = Field(default=1.0)
    tts_engine: str = Field(default="auto")  # auto | cosyvoice | edge-tts
    # TTS 并发（独立服务，可高于 1）
    tts_concurrency: int = Field(default=3)
    # ====== CosyVoice2 参考音频（zero-shot 克隆用）======
    # CosyVoice2-0.5B 是基础模型，无预设说话人，必须提供一段参考音频 + 其文字
    # 来克隆音色（inference_zero_shot）。缺省则不走 CosyVoice2，自动降级 edge-tts。
    # 参考音频建议：中文女声/男声朗读片段，wav/mp3 均可，6~10s，采样率 >=16k。
    cosyvoice_prompt_wav: str = Field(default="")
    cosyvoice_prompt_text: str = Field(default="")
    # 可选指令（如 "用温柔的女声朗读"）；为空则用 zero_shot 模式
    cosyvoice_instruct_text: str = Field(default="")

    # ====== 字幕配置 ======
    # 字幕预设：kai=古风楷体(推荐) | yahei=安全黑体 | default=旧版微软雅黑
    subtitle_style: str = Field(default="kai")
    # 字幕字号占视频高度比例：竖屏1920→0.045≈86px（抖音70-95/小红书48-72/B站横屏56px下限），
    # 贴近行业「标准字幕偏小、手机小屏需放大」结论；上限120下限56，超长句由自适应进一步缩。
    subtitle_fontsize_ratio: float = Field(default=0.045)
    # 字幕底部边距占视频高度比例：抖音/小红书底部 18~20% 被 UI(昵称/点赞栏/手势条)遮挡，
    # 研究结论建议字幕置于距底 25~30% 才稳妥；取 0.25（1920→≈480px，留 5%≈96px 净空于 UI 顶缘之上），
    # 旧值0.05=96px 正好落进 UI 区→被遮挡，0.20 仅贴 UI 顶缘临界。B站横屏1080→270px亦安全。
    subtitle_margin_v_ratio: float = Field(default=0.25)
    # 字幕左右安全边距占视频宽度比例（默认6%→1080宽约65px，避免长句贴边）
    subtitle_margin_h_ratio: float = Field(default=0.06)
    # 字幕块最大高度占视频高度比例：诗词旁白单句长(40+字)→6%边距下折4行，
    # 原0.10(192px)逼字号缩到下限28px(太小)；提到0.14(269px)让长句保持~55-60px可读。
    # 配合 margin_v=0.20，4行块顶边约70%处，仍在下半区不侵内容。
    subtitle_max_block_height_ratio: float = Field(default=0.14)
    # ASS WrapStyle：0=智能均衡换行(按字宽自动折行，长句安全) | 1=行尾换行 |
    # 2=禁止换行(长句溢出画面被裁切!) | 3=智能(下行更宽)。
    # 中文无空格，libass 仅按空格断词，故默认必须 ≠2；配合 SRT 内显式 \N 硬换行双保险，杜绝截断。
    subtitle_wrap_style: int = Field(default=0)
    # 自适应字号下限(px)：原28px过小(手机小屏眯眼)；提到40px保证超长句仍可读
    subtitle_min_fontsize: int = Field(default=40)
    # 字幕换行：每行最多中文字数（按画幅比例）。行业规范：中文≤13-15字/行、竖屏一行8-12字；
    # 超过则按字宽硬换行(\N)，避免长句/长文案被裁切。9:16=抖音/快手(窄屏,11字/行) |
    # 3:4=小红书(13字/行) | 16:9=B站/YouTube(16字/行)。
    subtitle_chars_per_line: dict = Field(default_factory=lambda: {"9:16": 11, "3:4": 13, "16:9": 16})
    # 字幕最多行数：单条时序字幕渲染行数上限（默认2行，硬上限3）。行业规范≤2行。
    # 超长文案先按"标点+硬切"拆成多条时序字幕(每条≤ chars_per_line*max_lines 字)，
    # 再每条内部按 chars_per_line 硬换行；双保险确保不溢出画面、不被截断。
    subtitle_max_lines: int = Field(default=2)
    # 金句字幕加权停留（video-comm 决策 5）：命中金句的 cue 按此倍率放大"字数当量"
    # 再参与同镜时长池分配（实际停留 ≈ 字数占比×该倍率）。识别首批=引号包裹子句
    # （「」/“”/『』），快档三段式文案落地后升级"原诗整句匹配"。默认 1.4（×1.3–1.6 中值）。
    golden_subtitle_weight: float = Field(default=1.4)
    # 末镜静音定格（video-comm 决策 5）：成片结尾补此秒数画面定格+无旁白（BGM 续铺），
    # SRT 末条字幕延续至定格窗，服务完播率与截图传播。0 = 关闭（保持旧行为）。
    end_hold_duration: float = Field(default=2.0)

    # ====== 语速标定（video-comm 决策 3）======
    # 中文旁白 TTS 实测字/秒估计值（字数预算与成片时长估算用；默认 3.5 为行业经验值，
    # 由 scripts/calibrate_speech_rate.py 离线扫 tts_segments.json 标定后 --apply 写回）。
    speech_rate_cps: float = Field(default=3.5)

    # ====== Voice Preset 配置 ======
    # Voice preset 目录文件（JSON，含 id/name/gender/style/ref_wav/prompt_text）。
    # 为空时默认 voice_presets.json（相对于 data/ 目录）。
    voice_preset_file: str = Field(default="voice_presets.json")
    # 默认使用的 voice preset id（为空则不使用 preset，走全局 cosyvoice_prompt_wav）。
    default_voice_preset: str = Field(default="")
    ffmpeg_path: str = Field(default="D:/Software/ffmpeg/bin/ffmpeg.exe")
    # 字幕烧录（ffmpeg）并发：CPU 密集，限制 2
    subtitle_concurrency: int = Field(default=2)

    # ====== 镜间转场配置 ======
    # 逐镜以图定音：是否对相邻片段做交叉淡入淡出（xfade），消除硬切生硬感
    transition_enabled: bool = Field(default=True)
    transition_duration: float = Field(default=0.4)   # 单段过渡时长(秒)
    transition_type: str = Field(default="fade")       # fade=交叉淡入淡出

    # ====== 文字水印配置 ======
    # 视频与分镜图统一叠加的文字水印（防搬运/品牌标识）
    watermark_enabled: bool = Field(default=True)
    watermark_text: str = Field(default="昊康动漫")
    # 水印字体文件（已启用）：drawtext 在本机走 Fontconfig 且默认配置缺失，
    # 字体名方式会失败，故水印必须用 fontfile 绝对路径（楷体 simkai.ttf，与古风统一；
    # 路径中的 ':' 在滤镜图中需转义为 '\:'）。
    watermark_fontfile: str = Field(default="C:/Windows/Fonts/simkai.ttf")
    watermark_fontsize_ratio: float = Field(default=0.06)  # 字号 = 视频高度 × 该比例（原0.045偏小）
    watermark_colour: str = Field(default="white")
    watermark_alpha: float = Field(default=0.8)             # 透明度（原0.65仍偏淡，提至0.8确保可见）
    watermark_margin_ratio: float = Field(default=0.03)    # 距边 = 视频高度 × 该比例
    watermark_position: str = Field(default="right_bottom")  # right_bottom | left_bottom

    # ====== 多平台输出配置 ======
    # 默认全平台输出（抖音/小红书/快手/B站），设置页"发布平台"的默认值；
    # 设为 ["douyin"] 可退回单平台模式。渲染阶段按此列表为每个平台生成对应尺寸视频。
    output_platforms: list[str] = Field(default=["douyin", "xiaohongshu", "kuaishou", "bilibili"])

    # ====== 通用配置 ======
    critic_concurrency: int = Field(default=5)
    script_score_threshold: float = Field(default=7.0)
    image_score_threshold: float = Field(default=7.0)
    max_retries: int = Field(default=3)

    # ====== 队列配置 ======
    # 消费者轮询间隔（秒）：扫描待办 Job 的频率
    queue_poll_interval: float = Field(default=2.0)
    
    # 数据库配置 - 使用绝对路径
    database_url: str = Field(default="")
    
    # 文件路径
    xml_file_path: str = Field(default="G:/cnkgraph/CNKGraph.Writings.xml")
    output_dir: str = Field(default="")
    
    # 服务器配置
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000)
    debug: bool = Field(default=True)
    # 对外可访问的基础 URL（用于把本地产物文件拼成前端可直连的绝对 URL）
    server_public_url: str = Field(default="http://localhost:8000")
    
    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # 确保数据目录存在
        DATA_DIR.mkdir(exist_ok=True)
        # 设置默认路径
        if not self.database_url:
            self.database_url = f"sqlite+aiosqlite:///{DATA_DIR / 'poems.db'}"
        if not self.output_dir:
            self.output_dir = str(DATA_DIR / "output")


# 全局配置实例
settings = Settings()
