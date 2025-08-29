# streamlit_app.py — 단계별 로그/진행률/재시도 로그 추가 버전
# 주요 변경점
# - Logger 클래스로 실시간 로그 출력 (좌측 "실시간 로그" 패널)
# - st.status()로 단계별 상태 표시 (YTA → yt-dlp → pytube)
# - 진행률 바: 전체 파이프라인 가중치 기반
# - yt-dlp 자막 URL GET 시에도 UA/쿠키/언어 헤더 적용 + 재시도 로그
# - 각 단계별 후보 언어/포맷/재시도 상세 로그

import re
import random
import time
from time import sleep
import html
from typing import Optional, List
from urllib.parse import urlparse, parse_qs
import ssl
import json
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

# ===== 공통 =====
class TranscriptExtractionError(Exception):
    pass

ssl._create_default_https_context = ssl._create_unverified_context

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

def urlopen_with_headers(url: str, headers: dict, logger, timeout: int = 30, retries: int = 3):
    last_err = None
    for attempt in range(retries):
        try:
            opener = urllib.request.build_opener()
            opener.addheaders = list(headers.items())
            logger.add(f"[GET] 자막 파일 요청: {url.split('?')[0]}... (시도 {attempt+1}/{retries})")
            with opener.open(url, timeout=timeout) as resp:
                data = resp.read()
                logger.add(f"[GET] 성공 ({len(data)} bytes)")
                return data
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            logger.add(f"[GET] 실패: {e}")
            if any(x in msg for x in ["429", "too many requests", "temporarily", "timed out", "403", "unavailable"]):
                wait = (2 ** attempt) + random.uniform(0.5, 1.5)
                logger.add(f"[GET] 백오프 {wait:.1f}s 후 재시도")
                sleep(wait)
                continue
            break
    raise last_err

# ===== Logger =====
class Logger:
    def __init__(self, container):
        self.lines = []
        self.container = container

    def add(self, msg):
        now = time.strftime("%H:%M:%S")
        self.lines.append(f"[{now}] {msg}")
        self.container.write("\n".join(self.lines[-200:]))

# ===== URL/ID 유틸 =====
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

# ===== 메타 (선택) =====
def safe_get_youtube_info(url: str, logger: Logger = None):
    try:
        ydl_opts = {"quiet": True, "noplaylist": True, "extract_flat": False}
        if logger: logger.add("yt-dlp로 영상 메타 요청")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        class YouTubeInfo:
            def __init__(self, d):
                self.title = d.get("title", "제목 확인 불가")
                self.length = d.get("duration", 0)
        if logger: logger.add("메타 수신 완료")
        return YouTubeInfo(info)
    except Exception as e:
        if logger: logger.add(f"메타 조회 실패: {e}")
        st.caption(f"영상 정보 가져오기 실패: {str(e)[:60]}")
        return None

# ===== 1) YTA =====
def fetch_via_yta_with_retry(video_id: str, langs: List[str], logger: Logger, max_retries: int = 3) -> str:
    last_error = None
    logger.add(f"[YTA] 시작 — video_id={video_id}, 선호언어={langs}")
    for attempt in range(max_retries):
        try:
            logger.add(f"[YTA] transcripts 목록 조회 (시도 {attempt+1}/{max_retries})")
            tl = YouTubeTranscriptApi.list_transcripts(video_id)
            try:
                logger.add("[YTA] 업로더 제공 자막 우선 탐색")
                tr = tl.find_transcript(langs)
            except Exception:
                logger.add("[YTA] 업로더 자막 없음 → 자동 생성 자막 탐색")
                tr = tl.find_generated_transcript(langs)
            logger.add(f"[YTA] 매칭: language={tr.language}, is_generated={tr.is_generated}")
            entries = tr.fetch()
            logger.add(f"[YTA] fetch 성공, 라인 {len(entries)}")
            st.success(f"✅ 자막 추출 성공 (YTA): {tr.language}" + (" [자동]" if tr.is_generated else " [수동]"))
            return "\n".join([f"[{e['start']:.1f}] {e['text']}" for e in entries])
        except Exception as e:
            last_error = e
            msg = str(e).lower()
            logger.add(f"[YTA] 실패: {e}")
            if ("429" in msg or "too many requests" in msg) and attempt < max_retries - 1:
                wait = (2 ** attempt) + random.uniform(1, 3)
                logger.add(f"[YTA] 429/일시오류 → {wait:.1f}s 대기 후 재시도")
                sleep(wait)
                continue
            if isinstance(e, (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable)):
                raise
            raise TranscriptExtractionError(f"YTA 처리 실패: {str(e)}")
    raise TranscriptExtractionError(f"YTA 재시도 실패: {str(last_error)}")

# ===== 2) pytube =====
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

def fetch_via_pytube(url_or_id: str, langs: List[str], logger: Logger) -> str:
    url = to_clean_watch_url(url_or_id)
    try:
        logger.add(f"[pytube] 시작 — {url}")
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
        logger.add(f"[pytube] 제공 코드: {list(available.keys())}")
        for code in list(candidates):
            cap = available.get(code)
            if not cap:
                for k, v in available.items():
                    if k.lower().startswith(code.lower().replace("a.", "")):
                        cap = v; code = k; break
            if not cap:
                continue
            logger.add(f"[pytube] 시도: {code} (SRT→XML)")
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
                            if text:
                                lines.append(f"[{start:.1f}] {text}")
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
    raise TranscriptExtractionError(
        f"pytube: 매칭되는 자막 없음 (사용가능: {list(available.keys()) if 'available' in locals() else 'N/A'})"
    )

# ===== 3) yt-dlp =====
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

def fetch_via_ytdlp_enhanced(url_or_id: str, langs: List[str], logger: Logger) -> str:
    url = to_clean_watch_url(url_or_id)
    common_headers = build_common_headers()
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "socket_timeout": 60,
        "retries": 3,
        "http_headers": common_headers,
    }
    logger.add(f"[yt-dlp] 시작 — {url} / 선호언어={langs}")
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        logger.add("[yt-dlp] info 추출 성공")
    except Exception as e:
        logger.add(f"[yt-dlp] info 실패: {e}")
        raise TranscriptExtractionError(f"yt-dlp 정보 추출 실패: {str(e)}")

    subs = info.get("subtitles") or {}
    autos = info.get("automatic_captions") or {}
    logger.add(f"[yt-dlp] 제공 수동자막: {list(subs.keys())} / 자동자막: {list(autos.keys())}")

    candidates = []
    for lg in langs:
        if lg in subs: candidates.append(("manual", lg, subs[lg]))
    for lg in langs:
        if lg in autos: candidates.append(("auto", lg, autos[lg]))
    if "en" not in langs:
        if "en" in subs: candidates.append(("manual", "en", subs["en"]))
        if "en" in autos: candidates.append(("auto", "en", autos["en"]))
    if not candidates:
        all_available = list(subs.keys()) + list(autos.keys())
        if all_available:
            first_lang = all_available[0]
            if first_lang in subs:
                candidates.append(("manual", first_lang, subs[first_lang]))
            elif first_lang in autos:
                candidates.append(("auto", first_lang, autos[first_lang]))
    logger.add(f"[yt-dlp] 후보 트랙: {[(k, lg, len(lst)) for k, lg, lst in candidates]}")

    format_priority = ["vtt", "webvtt", "srv3", "ttml", "json3"]
    for kind, lg, fmt_list in candidates:
        if not fmt_list: continue
        sorted_formats = []
        for fmt_name in format_priority:
            for item in fmt_list:
                if item.get("ext", "").lower() == fmt_name:
                    sorted_formats.append(item)
        for item in fmt_list:
            if item not in sorted_formats:
                sorted_formats.append(item)

        for item in sorted_formats:
            ext = item.get("ext", "").lower()
            logger.add(f"[yt-dlp] 다운로드 시도: lang={lg}, kind={kind}, ext={ext}")
            try:
                data_bytes = urlopen_with_headers(item["url"], common_headers, logger, timeout=30, retries=3)
                data = data_bytes.decode("utf-8", errors="ignore")
                if ext in ("vtt", "webvtt"):
                    lines = parse_vtt(data)
                    logger.add(f"[yt-dlp] VTT 파싱 라인: {len(lines)}")
                    if lines:
                        st.success(f"✅ 자막 추출 성공 (yt-dlp): {lg} ({kind}, {ext.upper()})")
                        return "\n".join(lines)
                elif ext in ("srv3", "json3"):
                    lines = parse_srv3_json(data)
                    logger.add(f"[yt-dlp] SRV3 파싱 라인: {len(lines)}")
                    if lines:
                        st.success(f"✅ 자막 추출 성공 (yt-dlp): {lg} ({kind}, SRV3)")
                        return "\n".join(lines)
                elif ext == "ttml":
                    lines = parse_ttml(data)
                    logger.add(f"[yt-dlp] TTML 파싱 라인: {len(lines)}")
                    if lines:
                        st.success(f"✅ 자막 추출 성공 (yt-dlp): {lg} ({kind}, TTML)")
                        return "\n".join(lines)
                else:
                    text = re.sub(r"<.*?>", " ", data)
                    text = html.unescape(text)
                    text = re.sub(r"\s+", " ", text).strip()
                    logger.add(f"[yt-dlp] 기타 포맷 길이: {len(text)}")
                    if text and len(text) > 100:
                        st.success(f"✅ 자막 추출 성공 (yt-dlp): {lg} ({kind}, {ext.upper()})")
                        return text
            except Exception as e:
                logger.add(f"[yt-dlp] 포맷 시도 실패: {e}")
                continue

    available_langs = list(set(list(subs.keys()) + list(autos.keys())))
    raise TranscriptExtractionError(f"yt-dlp: 자막 추출 실패 (사용가능: {available_langs})")

# ===== 최종 래퍼 + 캐시 + 진행률 =====
def fetch_transcript_resilient(url: str, video_id: str, langs: List[str], logger: Logger, progress):
    # 캐시
    if "transcript_cache" not in st.session_state:
        st.session_state.transcript_cache = {}
    cache_key = (video_id, tuple(langs))
    if cache_key in st.session_state.transcript_cache:
        logger.add("캐시 히트: 이전 추출 결과 사용")
        return st.session_state.transcript_cache[cache_key]

    # 진행률 가중치: YTA 40% → yt-dlp 40% → pytube 20%
    def set_progress(p): progress.progress(min(max(int(p), 0), 100))

    errors = []
    with st.status("YTA 시도 중...", state="running") as s1:
        set_progress(10)
        try:
            text = fetch_via_yta_with_retry(video_id, langs, logger)
            st.session_state.transcript_cache[cache_key] = text
            set_progress(100)
            s1.update(label="YTA 성공", state="complete")
            return text
        except (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable) as e:
            errors.append(f"YTA: {str(e)}"); s1.update(label="YTA: 자막 없음/비활성/접근불가", state="error")
        except TranscriptExtractionError as e:
            errors.append(f"YTA: {str(e)}"); s1.update(label="YTA: 처리 실패", state="error")
        except Exception as e:
            errors.append(f"YTA: {str(e)}"); s1.update(label="YTA: 예외", state="error")
        set_progress(40)

    with st.status("yt-dlp 시도 중...", state="running") as s2:
        try:
            text = fetch_via_ytdlp_enhanced(url, langs, logger)
            st.session_state.transcript_cache[cache_key] = text
            set_progress(100)
            s2.update(label="yt-dlp 성공", state="complete")
            return text
        except TranscriptExtractionError as e:
            errors.append(f"yt-dlp: {str(e)}"); s2.update(label="yt-dlp: 처리 실패", state="error")
        except Exception as e:
            errors.append(f"yt-dlp: {str(e)}"); s2.update(label="yt-dlp: 예외", state="error")
        set_progress(80)

    with st.status("pytube 시도 중...", state="running") as s3:
        try:
            text = fetch_via_pytube(url, langs, logger)
            st.session_state.transcript_cache[cache_key] = text
            set_progress(100)
            s3.update(label="pytube 성공", state="complete")
            return text
        except TranscriptExtractionError as e:
            errors.append(f"pytube: {str(e)}"); s3.update(label="pytube: 처리 실패", state="error")
        except Exception as e:
            errors.append(f"pytube: {str(e)}"); s3.update(label="pytube: 예외", state="error")
        set_progress(100)

    with st.expander("🔍 상세 오류 정보", expanded=False):
        for i, err in enumerate(errors, 1):
            st.text(f"{i}. {err}")

    if any("429" in err or "too many requests" in err.lower() for err in errors):
        raise TranscriptExtractionError("YouTube 요청 제한 (429/일시 차단)")
    if any("자막" in err and ("없음" in err or "찾을 수 없음" in err) for err in errors):
        raise TranscriptExtractionError("이 영상에는 자막이 없습니다.")
    raise TranscriptExtractionError("자막 추출 실패 - 상세 오류를 확인하세요.")

# ===== Streamlit UI =====
st.set_page_config(page_title="YouTube 자막 추출기 (로그 버전)", layout="wide")
st.title("🎬 YouTube 자막 추출기")
st.caption("단계별 로그와 진행률을 표시합니다. (웹 엔드포인트 기반)")

with st.sidebar:
    st.header("설정")
    lang_pref = st.multiselect(
        "언어 우선순위",
        ["ko", "en", "ja", "zh-Hans", "zh-Hant", "es", "fr", "de"],
        default=["ko", "en"],
    )
    show_meta = st.toggle("영상 제목/길이 표시 (추가 요청 발생)", value=False)
    st.markdown("---")
    st.caption("429/간헐 실패가 잦다면 호출 빈도를 낮추고, 같은 영상은 캐시가 사용됩니다.")

left, right = st.columns([1.1, 2.9])

with left:
    url = st.text_input("YouTube 링크", placeholder="https://www.youtube.com/watch?v=... 또는 https://youtu.be/...")
    run = st.button("자막 추출", type="primary")
    st.markdown("### 🛰️ 실시간 로그")
    log_area = st.empty()

with right:
    progress = st.progress(0)

if run:
    if not url.strip():
        st.warning("URL을 입력하세요."); st.stop()
    clean_url = to_clean_watch_url(url.strip())
    vid = extract_video_id(clean_url)
    if not vid:
        st.error("유효한 YouTube 링크가 아닙니다."); st.stop()

    logger = Logger(log_area)
    logger.add(f"입력 URL 정규화: {clean_url} (video_id={vid})")

    if show_meta:
        with st.spinner("영상 정보 가져오는 중..."):
            info = safe_get_youtube_info(clean_url, logger)
            if info:
                length_min = int((info.length or 0) / 60) if info.length else 0
                st.info(f"**제목**: {info.title}  |  **길이**: 약 {length_min}분")
            else:
                st.caption("영상 정보 조회 실패 - 자막 추출 계속")

    with st.spinner("자막 추출 중..."):
        try:
            text = fetch_transcript_resilient(clean_url, vid, lang_pref, logger, progress)
        except TranscriptExtractionError as e:
            st.error(f"❌ {str(e)}"); st.stop()
        except (NoTranscriptFound, TranscriptsDisabled) as e:
            st.error(f"❌ 자막을 찾을 수 없습니다: {str(e)}"); st.stop()
        except VideoUnavailable:
            st.error("❌ 영상에 접근할 수 없습니다 (비공개/지역/연령 제한 등)"); st.stop()
        except Exception as e:
            st.error(f"❌ 예상치 못한 오류: {str(e)}"); st.stop()

    st.success("자막 추출 완료!")
    c1, c2 = st.columns([1, 4])
    with c1:
        st.download_button(
            "📄 자막 다운로드 (TXT)",
            data=text.encode("utf-8"),
            file_name=f"transcript_{vid}.txt",
            mime="text/plain",
        )
    with c2:
        st.caption(f"총 {len(text.split()):,}개 단어")

    st.subheader("📄 추출된 자막")
    st.text_area("", value=text, height=500)

st.markdown("---")
st.caption("💡 공유 IP/무료 호스팅 환경에선 429가 더 자주 발생할 수 있습니다. 동일 영상 재요청은 캐시를 사용합니다.")
