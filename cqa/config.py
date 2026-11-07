"""Paths, .env loading and runtime settings."""
import json
import os
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
CHECKLIST_DIR = os.path.join(ROOT, "checklists")
STORAGE_DIR = os.path.join(ROOT, "storage")
RESULTS_DIR = os.path.join(ROOT, "results")
WEB_DIR = os.path.join(ROOT, "web")
SETTINGS_PATH = os.path.join(STORAGE_DIR, "settings.json")

# USD per 1M tokens (input, output). Used for the cost-per-evaluation estimate.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}

DEFAULT_SETTINGS = {
    # goes into results.json
    "team_name": "Название команды",
    # anthropic | gemini | ollama | hybrid | mock
    "provider": "anthropic",
    "gemini_model": "gemini-2.5-pro",
    # which external provider the hybrid mode escalates to: anthropic | gemini
    "hybrid_external": "anthropic",
    "anthropic_model": "claude-opus-5-5",
    "anthropic_effort": "medium",
    # server-side refusal fallback (Claude API); turned off automatically if rejected
    "anthropic_fallbacks": True,
    "ollama_url": "http://localhost:11434",
    "ollama_model": "gemma4:31b",
    # hybrid routing: these go straight to Claude, everything else to the local model first
    "hybrid_claude_languages": ["KZ"],
    "hybrid_claude_channels": ["voice call"],
    # local result is escalated to Claude if any criterion confidence is below this
    "hybrid_min_confidence": 0.6,
    # which checklist id is used by default for each channel
    "default_checklists": {"agent chat": "chat", "voice call": "call"},
}

_lock = threading.Lock()


def load_env():
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if val and key not in os.environ:
                os.environ[key] = val


def get_settings():
    with _lock:
        s = dict(DEFAULT_SETTINGS)
        if os.path.exists(SETTINGS_PATH):
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                s.update(json.load(f))
        return s


def update_settings(patch):
    with _lock:
        cur = {}
        if os.path.exists(SETTINGS_PATH):
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                cur = json.load(f)
        for k, v in patch.items():
            if k in DEFAULT_SETTINGS:
                cur[k] = v
        os.makedirs(STORAGE_DIR, exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
    return get_settings()


def cost_usd(model, input_tokens, output_tokens):
    p = PRICES.get(model)
    if not p:
        return 0.0
    return round(input_tokens / 1e6 * p[0] + output_tokens / 1e6 * p[1], 5)
