# streamlit_app.py — Captions-first / ASR-first / ASR-only + 고친 logger

import os
import re
import random
import time
from time import sleep
import html
from typing import Optional, List, Tuple
from urllib.parse import urlparse, parse_qs
import ssl
import json
import tempfile
import urllib.request

import streamlit as st
from youtube_transcript_api import (
    YouTubeTranscriptApi,
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)
from pytube import YouTube
import yt_dlp
import ffmpeg
from faster_whisper import WhisperModel

# ---------------------------
# 공통 설정 & 유틸
# ---------------------------
ssl._create_default_https_context = ssl._create_unverified_context

class TranscriptExtractionError(Exception):
    pass

# 전역 Whisper 모델 (최초 1회 로드)
_WHISPER = {"model": None}

def get_whisper_model(model_size: str = "small", device: str = "cpu", compute_type: str = "int8"):
    if _WHISPER["model"] is None:
        _WHISPER["model"] = WhisperModel(model_size, device=device, compute_type=compute_type)
    return _WHISPER["model"]

def build_common_headers(ua: str = None) -> dict:
    return {
        "User-Agent": ua or random.choice([
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        ]),
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Cookie": "CONSENT=YES+cb",
        "Accept": "*/*",
        "Connection": "close",
    }

def urlopen_with_headers(url: str, headers: dict, timeout: int = 30, retries: int = 3, logger=None) -> bytes:
    last_err = None
    for attempt in range(retries):
        try:
            opener = urllib.request.build_opener()
            opener.addheaders = list(headers.items())
            if logger: logger(f"[GET] {url.split('?')[0]} (try {attempt+1}/{retries})")
            with opener.open(url, timeout=timeout) as resp:
                data = resp.read()
                if logger: logger(f"[GET] OK ({len(data)} bytes)")
                return data
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            if logger: logger(f"[GET] fail: {e}")
            if any(x in msg for x in ["429", "too many requests", "temporarily", "timed out", "403", "unavailable"]):
                wait = (2 ** attempt) + random.uniform(0.5, 1.5)
                if logger: logger(f"[GET] backoff {wait:.1f}s")
                time.sleep(wait)
                continue
            break
    raise last_err

# ✅ 고친 logger: 전역 st.session_state 사용
def make_logger(area):
    if "_log_lines" not in st.session_state:
        st.session_state["_log_lines"] = []
    def _log(msg: str):
        now = time.strftime("%H:%M:%S")
        lines = st.session_state["_log_lines"]
        lines.append(f"[{now}] {msg}")
        st.session_state["_log_lines"] = lines[-200:]
        area.write("\n".join(st.session_state["_log_lines"]))
    return _log

# URL/ID
YOUTUBE_URL_RE = re.compile(
    r'(?
