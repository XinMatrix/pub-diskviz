# -*- coding: utf-8 -*-
"""
config.py — 用户自带 API Key（BYOK）的配置读写。

DiskViz 不在客户端里内置任何 API Key：它调用哪家模型、花谁的钱，由使用者
自己决定。配置存在 %APPDATA%\\DiskViz\\config.json，明文保存 —— 和你把 key
写进 .env 是同一个安全等级，请不要把这份文件分享给别人。

任何 OpenAI 兼容接口都能用：OpenAI / DeepSeek / Moonshot / 通义 / 本地 Ollama
/ 各种中转网关，只要它支持 /v1/chat/completions。
"""

from __future__ import annotations

import json
import os

APP_NAME = "DiskViz"

# 常见服务商的预设，仅用于界面上一键填入，不做任何网络请求
PRESETS = {
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model_analyze": "gpt-4o-mini",
        "model_ask": "gpt-4o-mini",
        "key_url": "https://platform.openai.com/api-keys",
    },
    "deepseek": {
        "label": "DeepSeek（国内直连，便宜）",
        "base_url": "https://api.deepseek.com/v1",
        "model_analyze": "deepseek-chat",
        "model_ask": "deepseek-chat",
        "key_url": "https://platform.deepseek.com/api_keys",
    },
    "moonshot": {
        "label": "Moonshot / Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model_analyze": "moonshot-v1-8k",
        "model_ask": "moonshot-v1-8k",
        "key_url": "https://platform.moonshot.cn/console/api-keys",
    },
    "ollama": {
        "label": "本地 Ollama（完全离线，零成本）",
        "base_url": "http://localhost:11434/v1",
        "model_analyze": "qwen2.5:14b",
        "model_ask": "qwen2.5:14b",
        "key_url": "https://ollama.com/download",
    },
    "custom": {
        "label": "自定义（任意 OpenAI 兼容网关）",
        "base_url": "",
        "model_analyze": "",
        "model_ask": "",
        "key_url": "",
    },
}

DEFAULTS = {
    "base_url": "",
    "api_key": "",
    "model_analyze": "",
    "model_ask": "",
    "min_mb": 100,
    "preset": "openai",
}


def config_dir() -> str:
    base = os.getenv("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


def load() -> dict:
    """读配置；文件不存在或损坏时返回默认值。"""
    data = dict(DEFAULTS)
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            for k in DEFAULTS:
                if k in saved:
                    data[k] = saved[k]
    except (OSError, ValueError):
        pass

    # 环境变量优先级最高，方便临时切换 / CI 里跑
    data["base_url"] = os.environ.get("DISKVIZ_API_URL") or data["base_url"]
    data["api_key"] = os.environ.get("DISKVIZ_API_KEY") or data["api_key"]
    data["model_analyze"] = os.environ.get("DISKVIZ_MODEL") or data["model_analyze"]
    data["model_ask"] = os.environ.get("DISKVIZ_ASK_MODEL") or data["model_ask"]
    return data


def save(patch: dict) -> dict:
    """合并保存，返回保存后的完整配置。"""
    data = load()
    for k in DEFAULTS:
        if k in patch and patch[k] is not None:
            data[k] = patch[k]
    if "min_mb" in data:
        try:
            # 下限 10MB：再小的话全盘会有上百万条候选，数据库和模型费用都会失控
            data["min_mb"] = max(10, int(data["min_mb"]))
        except (TypeError, ValueError):
            data["min_mb"] = 100
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def is_configured() -> bool:
    c = load()
    return bool(c["base_url"].strip() and c["model_analyze"].strip())


def masked(cfg: dict | None = None) -> dict:
    """给前端看的版本：key 只留头尾，避免整串出现在页面/日志里。"""
    c = dict(cfg or load())
    key = c.get("api_key") or ""
    c["api_key"] = (key[:6] + "…" + key[-4:]) if len(key) > 12 else ("已设置" if key else "")
    c["has_key"] = bool(key)
    c["configured"] = bool(c.get("base_url") and c.get("model_analyze"))
    c["config_path"] = config_path()
    c["presets"] = {k: {kk: vv for kk, vv in v.items()} for k, v in PRESETS.items()}
    return c
