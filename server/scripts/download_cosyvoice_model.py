"""下载 CosyVoice2-0.5B 权重到 server/pretrained_models/CosyVoice2-0.5B。

合并后的 TTS 代码（app/services/tts_core.py）会在该路径下自动发现模型，
engine=auto 时优先使用 CosyVoice2，未下载则降级 edge-tts。

用法（在后端 venv 中执行）：
  server\.venv\Scripts\python.exe server\scripts\download_cosyvoice_model.py

说明：
  - 默认走国内 hf-mirror.com 镜像（HF_ENDPOINT），如需 ModelScope 原仓可改 REPO。
  - hf-mirror 镜像的是 HuggingFace 原仓，仓库名为 FunAudioLLM/CosyVoice2-0.5B
    （ModelScope 同名仓库为 iic/CosyVoice2-0.5B）。
"""
import os
import sys
from pathlib import Path

# 默认走国内 hf-mirror 镜像
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

REPO = "FunAudioLLM/CosyVoice2-0.5B"
TARGET = Path(__file__).resolve().parents[1] / "pretrained_models" / "CosyVoice2-0.5B"


def main() -> None:
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        sys.exit("请先安装 huggingface_hub：server\\.venv\\Scripts\\python.exe -m pip install huggingface_hub")

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    print(f"下载 {REPO} -> {TARGET}")
    print(f"HF_ENDPOINT={os.environ['HF_ENDPOINT']}")
    path = snapshot_download(REPO, local_dir=str(TARGET))
    print(f"完成：{path}")


if __name__ == "__main__":
    main()
