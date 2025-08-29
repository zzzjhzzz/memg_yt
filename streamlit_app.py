import re
import random
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

# ---- 커스텀 예외 ----
class TranscriptExtractionError(Exception):
    pass

# ---- SSL 완화 (일부 호스팅 환경용) ----
ssl._create_default_https_context = ssl._create_unverified_context

# ---- 공통 헤더/쿠키 (자막 파일 GET에도 동일 적용) ----
def build_common_headers(ua: str = None) -> dict:
    return {
        "User-Agent": ua
        or random.choice(
            [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            ]
        ),
        # 언어 힌트
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        # 간단 CONSENT 회피용 쿠키
        "Cookie": "CONSENT=YES+cb",
        "Accept": "*/*",
        "Connection": "close",
    }

def urlopen_with_headers(url: str, headers: dict, timeout: int = 30, retries: int = 3):
    """
    자막/JSON/VTT 요청 시 헤더를 동일하게 전달하고 429/403 등 일시오류에 재시도.
    """
    last_err = None
    for attempt in range(retries):
        try:
            opener = urllib.request.build_opener()
            opener.addheaders = list(headers.items())
            with opener.open(url, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            if any(x in msg for x in ["429", "too many requests", "temporarily", "timed out", "403", "unavailable"]):
                sleep((2 ** attempt) + random.uniform(0.5, 1.5))
                continue
            break
    raise last_err

# ---- URL/ID 유틸 ----
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

# ---- 메타 정보 (요청 최소화) ----
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
        st.caption(f"영상 정보 가져오기 실패: {str(e)[:60]}")
        return None

# ---- YTA (1차 시도, 내부 재시도) ----
def fetch_via_yta_with_retry(video_id: str, langs: List[str], max_retries: int = 3) -> str:
    last_error = None
    for attempt in range(max_retries):
        try:
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
            if ("429" in msg or "too many requests" in msg) and attempt < max_retries - 1:
                sleep((2 ** attempt) + random.uniform(1, 3))
                continue
            if isinstance(e, (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable)):
                raise
            raise TranscriptExtractionError(f"YTA 처리 실패: {str(e)}")
    raise TranscriptExtractionError(f"YTA 재시도 실패: {str(last_error)}")

# ---- pytube (3차 폴백) ----
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
                # ko-KR 같은 지역코드 매칭
                for k, v in available.items():
                    if k.lower().startswith(code.lower().replace("a.", "")):
                        cap = v
                        code = k
                        break
            if not cap:
                continue
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

# ---- yt-dlp (2차 폴백, 헤더전달·재시도 보강) ----
def parse_vtt(vtt: str) -> List[str]:
    lines = []
    blocks = [b for b in vtt.strip().split("\n\n") if "-->" in b]
    for block in blocks:
        rows = block.split("\n")
        if not rows:
            continue
        ts = rows[0].replace(",", ".")
        m = re.match(r"(\d+):(\d+):(\d+(?:\.\d+)?)", ts)
        start = 0.0
        if m:
            h, m_, s = m.groups()
            start = int(h) * 3600 + int(m_) * 60 + float(s)
        text = " ".join(rows[1:]).strip()
        text = re.sub(r"<.*?>", " ", text)
        text = re.sub(r"\s+", " ", text)
        if text:
            lines.append(f"[{start:.1f}] {text}")
    return lines

def parse_srv3_json(json_data: str) -> List[str]:
    try:
        data = json.loads(json_data)
        lines = []
        for event in data.get("events", []):
            start_time = event.get("tStartMs", 0) / 1000.0
            segs = event.get("segs", [])
            text = "".join([seg.get("utf8", "") for seg in segs]).strip()
            if text:
                lines.append(f"[{start_time:.1f}] {text}")
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
            if text:
                lines.append(f"[{start_time:.1f}] {text}")
        return lines
    except Exception:
        return []

def fetch_via_ytdlp_enhanced(url_or_id: str, langs: List[str]) -> str:
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
        "http_headers": common_headers,  # 중요: yt-dlp 측 요청에도 동일 헤더
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise TranscriptExtractionError(f"yt-dlp 정보 추출 실패: {str(e)}")

    subs = info.get("subtitles") or {}
    autos = info.get("automatic_captions") or {}

    candidates = []
    for lg in langs:
        if lg in subs:
            candidates.append(("manual", lg, subs[lg]))
    for lg in langs:
        if lg in autos:
            candidates.append(("auto", lg, autos[lg]))
    if "en" not in langs:
        if "en" in subs:
            candidates.append(("manual", "en", subs["en"]))
        if "en" in autos:
            candidates.append(("auto", "en", autos["en"]))
    if not candidates:
        all_available = list(subs.keys()) + list(autos.keys())
        if all_available:
            first_lang = all_available[0]
            if first_lang in subs:
                candidates.append(("manual", first_lang, subs[first_lang]))
            elif first_lang in autos:
                candidates.append(("auto", first_lang, autos[first_lang]))

    format_priority = ["vtt", "webvtt", "srv3", "ttml", "json3"]

    for kind, lg, fmt_list in candidates:
        if not fmt_list:
            continue
        sorted_formats = []
        for fmt_name in format_priority:
            for item in fmt_list:
                if item.get("ext", "").lower() == fmt_name:
                    sorted_formats.append(item)
        for item in fmt_list:
            if item not in sorted_formats:
                sorted_formats.append(item)

        for item in sorted_formats:
            try:
                data_bytes = urlopen_with_headers(item["url"], common_headers, timeout=30, retries=3)
                data = data_bytes.decode("utf-8", errors="ignore")
                ext = item.get("ext", "").lower()

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
            except Exception:
                continue

    available_langs = list(set(list(subs.keys()) + list(autos.keys())))
    raise TranscriptExtractionError(f"yt-dlp: 자막 추출 실패 (사용가능: {available_langs})")

# ---- 최종 래퍼 + 캐시 ----
def fetch_transcript_resilient(url: str, video_id: str, langs: List[str]) -> str:
    # 세션 캐시
    if "transcript_cache" not in st.session_state:
        st.session_state.transcript_cache = {}

    cache_key = (video_id, tuple(langs))
    if cache_key in st.session_state.transcript_cache:
        st.caption("캐시 히트: 이전에 추출한 자막을 사용합니다.")
        return st.session_state.transcript_cache[cache_key]

    errors = []
    # 1) YTA
    try:
        text = fetch_via_yta_with_retry(video_id, langs)
        st.session_state.transcript_cache[cache_key] = text
        return text
    except (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable) as e:
        errors.append(f"YTA: {str(e)}")
        sleep(0.6)
    except TranscriptExtractionError as e:
        errors.append(f"YTA: {str(e)}")
        sleep(0.6)
    except Exception as e:
        errors.append(f"YTA: {str(e)}")
        sleep(0.6)

    # 2) yt-dlp
    try:
        text = fetch_via_ytdlp_enhanced(url, langs)
        st.session_state.transcript_cache[cache_key] = text
        return text
    except TranscriptExtractionError as e:
        errors.append(f"yt-dlp: {str(e)}")
        sleep(0.6)
    except Exception as e:
        errors.append(f"yt-dlp: {str(e)}")
        sleep(0.6)

    # 3) pytube
    try:
        text = fetch_via_pytube(url, langs)
        st.session_state.transcript_cache[cache_key] = text
        return text
    except TranscriptExtractionError as e:
        errors.append(f"pytube: {str(e)}")
    except Exception as e:
        errors.append(f"pytube: {str(e)}")

    with st.expander("🔍 상세 오류 정보", expanded=False):
        for i, err in enumerate(errors, 1):
            st.text(f"{i}. {err}")

    if any("429" in err or "too many requests" in err.lower() for err in errors):
        raise TranscriptExtractionError("YouTube 요청 제한 (429/일시 차단) - 잠시 후 다시 시도하거나 다른 영상으로 테스트해 보세요.")
    if any("자막" in err and ("없음" in err or "찾을 수 없음" in err) for err in errors):
        raise TranscriptExtractionError("이 영상에는 자막이 없습니다.")
    raise TranscriptExtractionError("자막 추출 실패 - 위의 상세 오류 정보를 확인하세요.")

# ---- Streamlit UI ----
st.set_page_config(page_title="YouTube 자막 추출기", layout="wide")
st.title("🎬 YouTube 자막 추출기")
st.caption("YouTube 영상의 자막을 추출합니다. (공식 키 없이 웹 엔드포인트 기반)")

with st.sidebar:
    st.header("설정")
    lang_pref = st.multiselect(
        "언어 우선순위 (위에서부터 시도)",
        ["ko", "en", "ja", "zh-Hans", "zh-Hant", "es", "fr", "de"],
        default=["ko", "en"],
        help="선호한 언어를 순서대로 시도합니다.",
    )
    show_meta = st.toggle("영상 제목/길이 표시 (요청 추가 발생)", value=False)

url = st.text_input(
    "YouTube 링크",
    placeholder="https://www.youtube.com/watch?v=... 또는 https://youtu.be/...",
    help="YouTube 영상의 URL을 입력하세요.",
)

if st.button("자막 추출", type="primary"):
    if not url.strip():
        st.warning("URL을 입력하세요.")
        st.stop()

    clean_url = to_clean_watch_url(url.strip())
    vid = extract_video_id(clean_url)
    if not vid:
        st.error("유효한 YouTube 링크가 아닙니다. URL을 다시 확인해주세요.")
        st.stop()

    st.info(f"비디오 ID: {vid}")

    if show_meta:
        with st.spinner("영상 정보 가져오는 중..."):
            info = safe_get_youtube_info(clean_url)
            if info:
                length_min = int((info.length or 0) / 60) if info.length else 0
                st.info(f"**제목**: {info.title}  |  **길이**: 약 {length_min}분")
            else:
                st.caption("영상 정보 조회 실패 - 자막 추출을 계속합니다.")

    with st.spinner("자막 추출 중..."):
        try:
            transcript_text = fetch_transcript_resilient(clean_url, vid, lang_pref)
        except TranscriptExtractionError as e:
            st.error(f"❌ {str(e)}")
            st.stop()
        except (NoTranscriptFound, TranscriptsDisabled) as e:
            st.error(f"❌ 자막을 찾을 수 없습니다: {str(e)}")
            st.stop()
        except VideoUnavailable:
            st.error("❌ 영상에 접근할 수 없습니다 (비공개, 지역/연령 제한 등)")
            st.stop()
        except Exception as e:
            st.error(f"❌ 예상치 못한 오류: {str(e)}")
            st.stop()

    st.success("자막 추출 완료!")

    col1, col2 = st.columns([1, 4])
    with col1:
        st.download_button(
            "📄 자막 다운로드 (TXT)",
            data=transcript_text.encode("utf-8"),
            file_name=f"transcript_{vid}.txt",
            mime="text/plain",
        )
    with col2:
        st.caption(f"총 {len(transcript_text.split()):,}개 단어")

    st.subheader("📄 추출된 자막")
    st.text_area("", value=transcript_text, height=500, help="자막 내용을 확인하고 복사할 수 있습니다.")

st.markdown("---")
st.caption(
    "💡 팁: 공유 호스팅/공유 IP 환경에서는 요청 제한(429)이 발생할 수 있어요. 동일 영상 반복 요청은 캐시되며, "
    "일부 영상은 저작권·연령·지역 제한으로 자막 접근이 차단될 수 있습니다."
)
