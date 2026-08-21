# -*- coding: utf-8 -*-
"""
Simple config loader. Reads config.json if present (preferred, easiest for
non-developers), falling back to environment variables for anyone who
prefers the old export-based workflow.
"""

import json
import os

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')
_cache = None


def _load():
    global _cache
    if _cache is not None:
        return _cache
    cfg = {}
    if os.path.exists(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
        except Exception as e:
            print(f"⚠️  Could not read config.json ({e}); falling back to environment variables.")
            cfg = {}
    _cache = cfg
    return cfg


def get(key: str, env_name: str = None, default=None):
    cfg = _load()
    val = cfg.get(key)
    if val not in (None, ''):
        return val
    if env_name:
        env_val = os.environ.get(env_name)
        if env_val:
            return env_val
    return default


def reload():
    global _cache
    _cache = None
    return _load()
