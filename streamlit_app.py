# streamlit_app.py — Captions-first / ASR-first / ASR-only 모드 지원

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

# 간단한 로그 writer
def make_logger(area):
    def _log(msg):
        now = time.strftime("%H:%M:%S")
        history = area.session_state.get("_log_lines", [])
        history.append(f"[{now}] {msg}")
        area.session_state["_log_lines"] = history[-200:]
        area.write("\n".join(area.session_state["_log_lines"]))
    return _log

# URL/ID
YOUTUBE_URL_RE = re.compile(
    r'(?:https?://)?(?:www\.)?(?:youtube\.com/(?:watch\?v=|embed/|live/|shorts/)|youtu\.be/)([\w-]{11})(?:\S+)?'
)

def extract_video_id(url: str) -> Optional[str]:
    if not url:
        return None
    m = YOUTUBE_URL_RE.search(url)
    if m:
        return m.group(1)
    try:
        parsed = urlparse(url)
        if parsed.hostname in ["youtube.com", "www.youtube.com"]:
            vid = parse_qs(parsed.query).get("v", [None])[0]
            if vid and len(vid) == 11:
                return vid
        elif parsed.hostname in ["youtu.be", "www.youtu.be"]:
            vid = parsed.path.lstrip("/")
            if len(vid) == 11:
                return vid
    except Exception:
        pass
    return None

def to_clean_watch_url(url_or_id: str) -> str:
    vid = extract_video_id(url_or_id) if "http" in url_or_id else url_or_id
    return f"https://www.youtube.com/watch?v={vid}" if vid else url_or_id

# ---------------------------
# 메타 (선택)
# ---------------------------
def safe_get_youtube_info(url: str):
    try:
        ydl_opts = {"quiet": True, "noplaylist": True, "extract_flat": False}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        class YouTubeInfo:
            def __init__(self, d):
                self.title = d.get("title", "제목 확인 불가")
                self.length = d.get("duration", 0)
        return YouTubeInfo(info)
    except Exception as e:
        st.caption(f"영상 정보 실패: {str(e)[:60]}")
        return None

# ---------------------------
# 1) youtube_transcript_api
# ---------------------------
def fetch_via_yta_with_retry(video_id: str, langs: List[str], logger=None, max_retries: int = 3) -> str:
    last_error = None
    for attempt in range(max_retries):
        try:
            if logger: logger(f"[YTA] list_transcripts (try {attempt+1}/{max_retries})")
            tl = YouTubeTranscriptApi.list_transcripts(video_id)
            try:
                tr = tl.find_transcript(langs)
            except Exception:
                tr = tl.find_generated_transcript(langs)
            entries = tr.fetch()
            st.success(f"✅ 자막 추출 성공 (YTA): {tr.language}" + (" [자동]" if tr.is_generated else " [수동]"))
            return "\n".join([f"[{e['start']:.1f}] {e['text']}" for e in entries])
        except Exception as e:
            last_error = e
            msg = str(e).lower()
            if logger: logger(f"[YTA] fail: {e}")
            if ("429" in msg or "too many requests" in msg) and attempt < max_retries - 1:
                wait = (2 ** attempt) + random.uniform(1, 3)
                if logger: logger(f"[YTA] backoff {wait:.1f}s")
                sleep(wait)
                continue
            if isinstance(e, (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable)):
                raise
            raise TranscriptExtractionError(f"YTA 처리 실패: {str(e)}")
    raise TranscriptExtractionError(f"YTA 재시도 실패: {str(last_error)}")

# ---------------------------
# 2) yt-dlp (헤더전달)
# ---------------------------
def parse_vtt(vtt: str) -> List[str]:
    lines = []
    blocks = [b for b in vtt.strip().split("\n\n") if "-->" in b]
    for block in blocks:
        rows = block.split("\n")
        if not rows: continue
        ts = rows[0].replace(",", ".")
        m = re.match(r"(\d+):(\d+):(\d+(?:\.\d+)?)", ts)
        start = 0.0
        if m:
            h, m_, s = m.groups()
            start = int(h) * 3600 + int(m_) * 60 + float(s)
        text = " ".join(rows[1:]).strip()
        text = re.sub(r"<.*?>", " ", text)
        text = re.sub(r"\s+", " ", text)
        if text: lines.append(f"[{start:.1f}] {text}")
    return lines

def parse_srv3_json(json_data: str) -> List[str]:
    try:
        data = json.loads(json_data)
        lines = []
        for event in data.get("events", []):
            start_time = event.get("tStartMs", 0) / 1000.0
            segs = event.get("segs", [])
            text = "".join([seg.get("utf8", "") for seg in segs]).strip()
            if text: lines.append(f"[{start_time:.1f}] {text}")
        return lines
    except Exception:
        return []

def parse_ttml(ttml_data: str) -> List[str]:
    try:
        lines = []
        pattern = r'<p[^>]*begin="([^"]*)"[^>]*>(.*?)</p>'
        for match in re.finditer(pattern, ttml_data, re.DOTALL):
            time_str = match.group(1)
            text_content = match.group(2)
            try:
                parts = time_str.replace(",", ".").split(":")
                if len(parts) == 3:
                    h, m, s = parts
                    start_time = int(h) * 3600 + int(m) * 60 + float(s)
                else:
                    start_time = 0.0
            except:
                start_time = 0.0
            text = re.sub(r"<.*?>", " ", text_content)
            text = html.unescape(text)
            text = re.sub(r"\s+", " ", text).strip()
            if text: lines.append(f"[{start_time:.1f}] {text}")
        return lines
    except Exception:
        return []

def fetch_via_ytdlp_enhanced(url_or_id: str, langs: List[str], logger=None) -> str:
    url = to_clean_watch_url(url_or_id)
    common_headers = build_common_headers()
    ydl_opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "writesubtitles": False, "writeautomaticsub": False,
        "socket_timeout": 60, "retries": 3, "http_headers": common_headers,
    }
    try:
        if logger: logger("[yt-dlp] extract_info")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise TranscriptExtractionError(f"yt-dlp 정보 추출 실패: {str(e)}")

    subs = info.get("subtitles") or {}
    autos = info.get("automatic_captions") or {}
    if logger: logger(f"[yt-dlp] subs={list(subs.keys())}, autos={list(autos.keys())}")

    # 후보 구성
    candidates: List[Tuple[str, str, list]] = []
    for lg in langs:
        if lg in subs: candidates.append(("manual", lg, subs[lg]))
    for lg in langs:
        if lg in autos: candidates.append(("auto", lg, autos[lg]))
    if "en" not in langs:
        if "en" in subs: candidates.append(("manual", "en", subs["en"]))
        if "en" in autos: candidates.append(("auto", "en", autos["en"]))
    if not candidates:
        avail = list(subs.keys()) + list(autos.keys())
        if avail:
            first = avail[0]
            if first in subs: candidates.append(("manual", first, subs[first]))
            elif first in autos: candidates.append(("auto", first, autos[first]))
    if logger: logger(f"[yt-dlp] candidates={[(k, lg, len(lst)) for k, lg, lst in candidates]}")

    priority = ["vtt", "webvtt", "srv3", "ttml", "json3"]
    for kind, lg, fmt_list in candidates:
        if not fmt_list: continue
        sorted_formats = []
        for p in priority:
            for item in fmt_list:
                if item.get("ext", "").lower() == p:
                    sorted_formats.append(item)
        for item in fmt_list:
            if item not in sorted_formats:
                sorted_formats.append(item)

        for item in sorted_formats:
            ext = item.get("ext", "").lower()
            if logger: logger(f"[yt-dlp] try {lg}/{kind}/{ext}")
            try:
                data = urlopen_with_headers(item["url"], common_headers, timeout=30, retries=3, logger=logger).decode("utf-8", errors="ignore")
                if ext in ("vtt", "webvtt"):
                    lines = parse_vtt(data)
                    if lines:
                        st.success(f"✅ 자막 추출 성공 (yt-dlp): {lg} ({kind}, {ext.upper()})")
                        return "\n".join(lines)
                elif ext in ("srv3", "json3"):
                    lines = parse_srv3_json(data)
                    if lines:
                        st.success(f"✅ 자막 추출 성공 (yt-dlp): {lg} ({kind}, SRV3)")
                        return "\n".join(lines)
                elif ext == "ttml":
                    lines = parse_ttml(data)
                    if lines:
                        st.success(f"✅ 자막 추출 성공 (yt-dlp): {lg} ({kind}, TTML)")
                        return "\n".join(lines)
                else:
                    text = re.sub(r"<.*?>", " ", data)
                    text = html.unescape(text)
                    text = re.sub(r"\s+", " ", text).strip()
                    if text and len(text) > 100:
                        st.success(f"✅ 자막 추출 성공 (yt-dlp): {lg} ({kind}, {ext.upper()})")
                        return text
            except Exception as e:
                if logger: logger(f"[yt-dlp] format fail: {e}")
                continue

    available_langs = list(set(list(subs.keys()) + list(autos.keys())))
    raise TranscriptExtractionError(f"yt-dlp: 자막 추출 실패 (사용가능: {available_langs})")

# ---------------------------
# 3) pytube
# ---------------------------
def clean_xml_text(xml_text: str) -> List[tuple]:
    items = []
    xml_text = xml_text.replace("\n", "")
    pattern = r'<text[^>]*start="([\d\.]+)"[^>]*>(.*?)</text>'
    for m in re.finditer(pattern, xml_text, re.DOTALL):
        try:
            start = float(m.group(1))
            raw = re.sub(r"<.*?>", " ", m.group(2))
            text = html.unescape(raw)
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                items.append((start, text))
        except ValueError:
            continue
    return items

def fetch_via_pytube(url_or_id: str, langs: List[str]) -> str:
    url = to_clean_watch_url(url_or_id)
    try:
        yt = YouTube(url, use_oauth=False, allow_oauth_cache=False)
        _ = yt.title
        tracks = yt.captions
        if not tracks:
            raise TranscriptExtractionError("pytube: 자막 트랙이 없음")

        candidates = []
        for lg in langs:
            candidates += [lg, f"a.{lg}"]
        if "en" not in [c.replace("a.", "") for c in candidates]:
            candidates += ["en", "a.en"]

        available = {c.code: c for c in tracks}
        for code in list(candidates):
            cap = available.get(code)
            if not cap:
                for k, v in available.items():
                    if k.lower().startswith(code.lower().replace("a.", "")):
                        cap = v; code = k; break
            if not cap: continue
            try:
                srt = cap.generate_srt_captions()
                lines = []
                for block in srt.strip().split("\n\n"):
                    parts = block.split("\n")
                    if len(parts) >= 3:
                        ts = parts[1].split("-->")[0].strip()
                        try:
                            h, m, s_ms = ts.split(":")
                            s, ms = s_ms.split(",")
                            start = int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0
                            text = " ".join(parts[2:]).strip()
                            if text: lines.append(f"[{start:.1f}] {text}")
                        except Exception:
                            continue
                if lines:
                    st.success(f"✅ 자막 추출 성공 (pytube): {code}")
                    return "\n".join(lines)
            except Exception:
                try:
                    xml = cap.xml_captions
                    items = clean_xml_text(xml)
                    if items:
                        st.success(f"✅ 자막 추출 성공 (pytube): {code}")
                        return "\n".join([f"[{stt:.1f}] {txt}" for stt, txt in items])
                except Exception:
                    continue
    except Exception as e:
        raise TranscriptExtractionError(f"pytube 처리 실패: {str(e)}")
    raise TranscriptExtractionError(f"pytube: 매칭되는 자막 없음 (사용가능: {list(available.keys()) if 'available' in locals() else 'N/A'})")

# ---------------------------
# 4) Whisper ASR (로컬)
# ---------------------------
def download_audio_only(url: str) -> str:
    """오디오만 임시 경로로 저장 후 16kHz mono wav 변환하여 반환"""
    with tempfile.TemporaryDirectory() as td:
        m4a_path = os.path.join(td, "audio.m4a")
        ydl_opts = {
            "quiet": True,
            "noplaylist": True,
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": m4a_path,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        wav_path = os.path.join(td, "audio.wav")
        (
            ffmpeg
            .input(m4a_path)
            .output(wav_path, ac=1, ar="16000")
            .overwrite_output()
            .run(quiet=True)
        )
        # 임시 디렉토리 생명주기 회피: 파일을 메모리로 읽어 새 임시파일에 저장
        with open(wav_path, "rb") as f:
            data = f.read()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    tmp.write(data); tmp.flush(); tmp.close()
    return tmp.name

def transcribe_whisper(audio_path: str, lang_hint: str = "ko") -> str:
    model = get_whisper_model(model_size="small", device="cpu", compute_type="int8")
    segments, info = model.transcribe(audio_path, language=lang_hint, vad_filter=True)
    lines = []
    for seg in segments:
        start = seg.start or 0.0
        text = (seg.text or "").strip()
        if text:
            lines.append(f"[{start:.1f}] {text}")
    return "\n".join(lines)

def fetch_via_whisper_asr(url: str, langs: List[str], logger=None) -> str:
    hint = (langs[0] if langs else "ko") or "ko"
    if logger: logger("🎤 Whisper: audio download")
    audio = download_audio_only(url)
    try:
        if logger: logger("🎤 Whisper: transcribe")
        text = transcribe_whisper(audio, hint)
        if not text:
            raise TranscriptExtractionError("Whisper: 전사 결과가 비어있습니다")
        st.success("✅ ASR(Whisper) 전사 성공")
        return text
    finally:
        try: os.remove(audio)
        except: pass

# ---------------------------
# 파이프라인 (모드 지원)
# ---------------------------
def captions_pipeline(url, vid, langs, logger, progress=None) -> str:
    # YTA → yt-dlp → pytube
    if progress: progress.progress(5)
    try:
        with st.status("YTA 시도 중...", state="running") as s1:
            text = fetch_via_yta_with_retry(vid, langs, logger)
            if progress: progress.progress(100)
            s1.update(label="YTA 성공", state="complete")
            return text
    except (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable) as e:
        if logger: logger(f"[YTA] no captions: {e}")
    except TranscriptExtractionError as e:
        if logger: logger(f"[YTA] error: {e}")
    except Exception as e:
        if logger: logger(f"[YTA] exception: {e}")
    if progress: progress.progress(40)

    try:
        with st.status("yt-dlp 시도 중...", state="running") as s2:
            text = fetch_via_ytdlp_enhanced(url, langs, logger)
            if progress: progress.progress(100)
            s2.update(label="yt-dlp 성공", state="complete")
            return text
    except Exception as e:
        if logger: logger(f"[yt-dlp] error: {e}")
    if progress: progress.progress(70)

    try:
        with st.status("pytube 시도 중...", state="running") as s3:
            text = fetch_via_pytube(url, langs)
            if progress: progress.progress(100)
            s3.update(label="pytube 성공", state="complete")
            return text
    except Exception as e:
        if logger: logger(f"[pytube] error: {e}")

    raise TranscriptExtractionError("캡션 파이프라인 실패")

def asr_pipeline(url, langs, logger, progress=None) -> str:
    if progress: progress.progress(5)
    with st.status("ASR(Whisper) 전사 중...", state="running") as s:
        text = fetch_via_whisper_asr(url, langs, logger)
        if progress: progress.progress(100)
        s.update(label="ASR 전사 성공", state="complete")
        return text

def run_pipeline(mode: str, url: str, vid: str, langs: List[str], logger, progress):
    """
    mode: 'Captions-first' | 'ASR-first' | 'ASR-only'
    """
    if "transcript_cache" not in st.session_state:
        st.session_state.transcript_cache = {}
    cache_key = (vid, mode, tuple(langs))
    if cache_key in st.session_state.transcript_cache:
        st.caption("캐시 히트: 이전 결과 사용")
        return st.session_state.transcript_cache[cache_key]

    if mode == "Captions-first":
        try:
            text = captions_pipeline(url, vid, langs, logger, progress)
            st.session_state.transcript_cache[cache_key] = text
            return text
        except Exception as e1:
            logger(f"[pipeline] captions fail → ASR 폴백: {e1}")
            text = asr_pipeline(url, langs, logger, progress)
            st.session_state.transcript_cache[cache_key] = text
            return text

    elif mode == "ASR-first":
        try:
            text = asr_pipeline(url, langs, logger, progress)
            st.session_state.transcript_cache[cache_key] = text
            return text
        except Exception as e1:
            logger(f"[pipeline] ASR fail → captions 폴백: {e1}")
            text = captions_pipeline(url, vid, langs, logger, progress)
            st.session_state.transcript_cache[cache_key] = text
            return text

    elif mode == "ASR-only":
        text = asr_pipeline(url, langs, logger, progress)
        st.session_state.transcript_cache[cache_key] = text
        return text

    raise ValueError("알 수 없는 모드")

# ---------------------------
# UI
# ---------------------------
st.set_page_config(page_title="YouTube 요약기 — 캡션/ASR 모드", layout="wide")
st.title("🎬 YouTube 자막/ASR 추출기")
st.caption("캡션(스크래핑) 또는 로컬 ASR(Whisper)을 선택적으로 사용합니다. (ASR은 API 키 불필요)")

with st.sidebar:
    st.header("설정")
    mode = st.radio(
        "추출 모드",
        ["Captions-first", "ASR-first", "ASR-only"],
        index=0,
        help="• Captions-first: 자막 우선, 실패 시 ASR 폴백\n• ASR-first: ASR 우선, 실패 시 자막 폴백\n• ASR-only: 자막 호출 없이 ASR만"
    )
    lang_pref = st.multiselect(
        "언어 우선순위",
        ["ko", "en", "ja", "zh-Hans", "zh-Hant", "es", "fr", "de"],
        default=["ko", "en"],
    )
    show_meta = st.toggle("영상 제목/길이 표시 (추가 요청 발생)", value=False)
    st.markdown("---")
    st.caption("ASR은 처음 실행 시 모델 다운로드로 시간이 걸릴 수 있어요.")

left, right = st.columns([1.1, 2.9])
with left:
    url = st.text_input("YouTube 링크", placeholder="https://www.youtube.com/watch?v=... 또는 https://youtu.be/...")
    run = st.button("추출하기", type="primary")
    st.markdown("### 🛰️ 실시간 로그")
    log_area = st.empty()
    logger = make_logger(log_area)

with right:
    progress = st.progress(0)

if run:
    if not url.strip():
        st.warning("URL을 입력하세요."); st.stop()
    clean_url = to_clean_watch_url(url.strip())
    vid = extract_video_id(clean_url)
    if not vid:
        st.error("유효한 YouTube 링크가 아닙니다."); st.stop()

    logger(f"입력 정규화: {clean_url} (video_id={vid})")

    if show_meta:
        with st.spinner("영상 정보..."):
            info = safe_get_youtube_info(clean_url)
            if info:
                length_min = int((info.length or 0) / 60) if info.length else 0
                st.info(f"**제목**: {info.title}  |  **길이**: 약 {length_min}분")
            else:
                st.caption("영상 정보 조회 실패 - 계속 진행합니다.")

    try:
        text = run_pipeline(mode, clean_url, vid, lang_pref, logger, progress)
    except TranscriptExtractionError as e:
        st.error(f"❌ {str(e)}"); st.stop()
    except (NoTranscriptFound, TranscriptsDisabled) as e:
        st.error(f"❌ 자막을 찾을 수 없습니다: {str(e)}"); st.stop()
    except VideoUnavailable:
        st.error("❌ 영상 접근 불가 (비공개/지역/연령 제한 등)"); st.stop()
    except Exception as e:
        st.error(f"❌ 예외: {str(e)}"); st.stop()

    st.success("추출 완료!")
    c1, c2 = st.columns([1, 4])
    with c1:
        st.download_button(
            "📄 다운로드 (TXT)",
            data=text.encode("utf-8"),
            file_name=f"transcript_{vid}.txt",
            mime="text/plain",
        )
    with c2:
        st.caption(f"총 {len(text.split()):,}개 단어")

    st.subheader("📄 결과")
    st.text_area("", value=text, height=500)

st.markdown("---")
st.caption("💡 ASR-only 모드는 자막 엔드포인트를 호출하지 않아 429 리스크가 가장 낮습니다. ffmpeg가 시스템에 설치되어 있어야 합니다.")
