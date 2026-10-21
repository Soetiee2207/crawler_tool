"""Threads crawler: raw material for TikTok videos."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# VieNeu-TTS tải model ONNX từ HuggingFace. Cache dạng Xet / huggingface_hub 2.x đặt file .data
# ở thư mục blob dùng chung, và onnxruntime từ chối nạp ("External data path escapes model
# directory"). Dùng cache riêng + tải HTTP thường để file nằm cạnh nhau. Phải đặt trước khi
# import huggingface_hub.
os.environ.setdefault("HF_HUB_CACHE", str(PROJECT_ROOT / ".cache" / "hf"))
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
