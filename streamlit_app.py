import re
import time
import html
from typing import Optional, List
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen
import ssl

import streamlit as st
from youtube_transcript_api import (
    YouTubeTranscriptApi,
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)

# 커스텀 예외 클래스 정의
class TranscriptExtractionError(Exception):
    """자막 추출 실패 시 사용하는 커스텀 예외"""
    pass
from pytube import YouTube
import yt_dlp

# SSL 인증서 문제 해결
ssl._create_default_https_context = ssl._create_unverified_context

# ---------------------------------
# URL 정리 / 비디오ID 추출
# ---------------------------------
YOUTUBE_URL_RE = re.compile(
    r'(?:https?://)?(?:www\.)?(?:youtube\.com/(?:watch\?v=|embed/|live/|shorts/)|youtu\.be/)([\w-]{11})(?:\S+)?'
)

def extract_video_id(url: str) -> Optional[str]:
    if not url:
        return None
    
    # 정규표현식으로 먼저 시도
    m = YOUTUBE_URL_RE.search(url)
    if m:
        return m.group(1)
    
    # URL 파싱으로 재시도
    try:
        parsed = urlparse(url)
        if parsed.hostname in ['youtube.com', 'www.youtube.com']:
            vid = parse_qs(parsed.query).get("v", [None])[0]
            if vid and len(vid) == 11:
                return vid
        elif parsed.hostname in ['youtu.be', 'www.youtu.be']:
            vid = parsed.path.lstrip('/')
            if len(vid) == 11:
                return vid
    except Exception:
        pass
    
    return None

def to_clean_watch_url(url_or_id: str) -> str:
    """짧은 주소/파라미터를 표준 watch URL로 정리."""
    vid = extract_video_id(url_or_id) if "http" in url_or_id else url_or_id
    return f"https://www.youtube.com/watch?v={vid}" if vid else url_or_id

def safe_get_youtube_info(url: str):
    """안전한 YouTube 정보 가져오기"""
    try:
        # pytube 설정 개선
        yt = YouTube(url, use_oauth=False, allow_oauth_cache=False)
        # 연결 테스트
        _ = yt.title
        return yt
    except Exception as e:
        st.warning(f"YouTube 정보 가져오기 실패: {str(e)}")
        return None

# ---------------------------------
# 1) youtube_transcript_api (공식/자동생성)
# ---------------------------------
def fetch_via_yta(video_id: str, langs: List[str]) -> str:
    """업로더 자막 → 자동생성 자막 순으로 시도."""
    try:
        tl = YouTubeTranscriptApi.list_transcripts(video_id)
        
        # 업로더 자막 먼저 시도
        try:
            tr = tl.find_transcript(langs)
        except Exception:
            # 자동생성 자막으로 폴백
            tr = tl.find_generated_transcript(langs)
        
        entries = tr.fetch()
        st.success(f"자막 확보(yta): lang={tr.language}, auto={tr.is_generated}")
        return "\n".join([f"[{e['start']:.1f}] {e['text']}" for e in entries])
    except (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable):
        # 원본 예외를 그대로 재발생
        raise
    except Exception as e:
        raise TranscriptExtractionError(f"YTA 방식 실패: {str(e)}")

# ---------------------------------
# 2) pytube captions 폴백 (SRT/XML)
# ---------------------------------
def clean_xml_text(xml_text: str) -> List[tuple]:
    """XML에서 (start, text) 리스트로 변환."""
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
    """pytube 자막 트랙에서 추출."""
    url = to_clean_watch_url(url_or_id)
    
    try:
        yt = safe_get_youtube_info(url)
        if not yt:
            raise TranscriptExtractionError("pytube: YouTube 객체 생성 실패")
        
        tracks = yt.captions
        if not tracks:
            raise TranscriptExtractionError("pytube: 자막 트랙이 없음")

        # 선호 언어 코드 + 자동생성 코드 후보 구성
        candidates = []
        for lg in langs:
            candidates.append(lg)
            candidates.append(f"a.{lg}")
        
        # 영어 폴백
        if "en" not in [c.replace("a.", "") for c in candidates]:
            candidates.extend(["en", "a.en"])

        available_codes = {c.code: c for c in tracks}

        for code in candidates:
            cap = available_codes.get(code)
            
            # 지역코드 매칭 시도 (예: ko-KR)
            if not cap:
                for k, v in available_codes.items():
                    if k.lower().startswith(code.lower().replace("a.", "")):
                        cap = v
                        break
            
            if not cap:
                continue

            try:
                # SRT 형식으로 시도
                srt = cap.generate_srt_captions()
                lines = []
                
                for block in srt.strip().split("\n\n"):
                    if not block.strip():
                        continue
                        
                    parts = block.split("\n")
                    if len(parts) >= 3:
                        # 타임스탬프 파싱
                        ts = parts[1].split("-->")[0].strip()
                        try:
                            h, m, s_ms = ts.split(":")
                            s, ms = s_ms.split(",")
                            start = int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0
                            text = " ".join(parts[2:]).strip()
                            if text:
                                lines.append(f"[{start:.1f}] {text}")
                        except ValueError:
                            continue
                
                if lines:
                    st.success(f"자막 확보(pytube-srt): {code}")
                    return "\n".join(lines)
                    
            except Exception:
                # XML 형식으로 폴백
                try:
                    xml = cap.xml_captions
                    items = clean_xml_text(xml)
                    if items:
                        st.success(f"자막 확보(pytube-xml): {code}")
                        return "\n".join([f"[{stt:.1f}] {txt}" for stt, txt in items])
                except Exception:
                    continue

    except Exception as e:
        raise TranscriptExtractionError(f"pytube 방식 실패: {str(e)}")
    
    raise TranscriptExtractionError("pytube: 매칭되는 자막 트랙 없음")

# ---------------------------------
# 3) yt-dlp 폴백
# ---------------------------------
def parse_vtt(vtt: str) -> List[str]:
    """WebVTT를 [start] text 형식으로 변환."""
    lines = []
    blocks = [b for b in vtt.strip().split("\n\n") if "-->" in b]
    
    for block in blocks:
        rows = block.split("\n")
        if not rows:
            continue
            
        ts = rows[0]
        m = re.match(r"(\d+):(\d+):(\d+(?:\.\d+)?)", ts.replace(",", "."))
        
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

def fetch_via_ytdlp(url_or_id: str, langs: List[str]) -> str:
    """yt-dlp로 자막 가져오기."""
    url = to_clean_watch_url(url_or_id)
    
    ydl_opts = {
        "quiet": True,
        "noplaylist": True,
        "writesubtitles": False,
        "writeautomaticsub": False,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise TranscriptExtractionError(f"yt-dlp 정보 추출 실패: {str(e)}")

    subs = info.get("subtitles") or {}
    autos = info.get("automatic_captions") or {}

    # 후보 구성
    candidates = []
    for lg in langs:
        if lg in subs:
            candidates.append(("subs", lg, subs[lg]))
    for lg in langs:
        if lg in autos:
            candidates.append(("auto", lg, autos[lg]))
    
    # 영어 폴백
    if not any(c[1] == "en" for c in candidates):
        if "en" in subs:
            candidates.append(("subs", "en", subs["en"]))
        if "en" in autos:
            candidates.append(("auto", "en", autos["en"]))

    for kind, lg, fmt_list in candidates:
        if not fmt_list:
            continue
            
        # VTT 형식 우선 선택
        vtt_item = None
        for item in fmt_list:
            if item.get("ext", "").lower() in ("vtt", "webvtt"):
                vtt_item = item
                break
        
        target = vtt_item or fmt_list[0]
        
        try:
            with urlopen(target["url"]) as resp:
                data = resp.read().decode("utf-8", errors="ignore")
            
            if target.get("ext", "").lower() in ("vtt", "webvtt"):
                lines = parse_vtt(data)
                if lines:
                    st.success(f"자막 확보(yt-dlp-{kind}-vtt): {lg}")
                    return "\n".join(lines)
            else:
                # 다른 형식: 태그 제거 후 텍스트만
                text = re.sub(r"<.*?>", " ", data)
                text = html.unescape(text)
                text = re.sub(r"\s+", " ", text).strip()
                if text and len(text) > 50:  # 최소 길이 체크
                    st.success(f"자막 확보(yt-dlp-{kind}-{target.get('ext','raw')}): {lg}")
                    return text
        except Exception:
            continue

    raise TranscriptExtractionError("yt-dlp: 매칭되는 자막 소스 없음")

# ---------------------------------
# 최종 래퍼
# ---------------------------------
def fetch_transcript_resilient(url: str, video_id: str, langs: List[str]) -> str:
    """3단계 폴백으로 자막 가져오기"""
    errors = []
    
    # 1) youtube_transcript_api
    try:
        return fetch_via_yta(video_id, langs)
    except (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable):
        # 원본 예외는 그대로 재발생 (마지막에)
        errors.append("YTA: 자막을 찾을 수 없음")
        time.sleep(0.5)
    except TranscriptExtractionError as e:
        errors.append(f"YTA: {str(e)}")
        time.sleep(0.5)
    except Exception as e:
        errors.append(f"YTA: 예상치 못한 오류 - {str(e)}")
        time.sleep(0.5)

    # 2) pytube
    try:
        return fetch_via_pytube(url, langs)
    except TranscriptExtractionError as e:
        errors.append(f"pytube: {str(e)}")
        time.sleep(0.5)
    except Exception as e:
        errors.append(f"pytube: 예상치 못한 오류 - {str(e)}")
        time.sleep(0.5)

    # 3) yt-dlp
    try:
        return fetch_via_ytdlp(url, langs)
    except TranscriptExtractionError as e:
        errors.append(f"yt-dlp: {str(e)}")
    except Exception as e:
        errors.append(f"yt-dlp: 예상치 못한 오류 - {str(e)}")

    # 모든 방법 실패 시 상세한 오류 정보 제공
    error_msg = " | ".join(errors)
    raise TranscriptExtractionError(f"모든 방법 실패: {error_msg}")

# ---------------------------------
# Streamlit UI
# ---------------------------------
st.set_page_config(page_title="YouTube 자막 추출기", layout="wide")
st.title("🎬 YouTube 자막 추출기")
st.caption("YouTube 영상의 자막을 추출합니다. API 키 불필요.")

with st.sidebar:
    st.header("설정")
    lang_pref = st.multiselect(
        "언어 우선순위 (위에서부터 시도)",
        ["ko", "en", "ja", "zh-Hans", "zh-Hant", "es", "fr", "de"],
        default=["ko", "en"],
        help="선호하는 언어를 순서대로 선택하세요"
    )
    show_meta = st.toggle("영상 제목/길이 표시", value=True)

url = st.text_input(
    "YouTube 링크", 
    placeholder="https://www.youtube.com/watch?v=... 또는 https://youtu.be/...",
    help="YouTube 영상의 URL을 입력하세요"
)

run = st.button("자막 추출", type="primary")

if run:
    if not url.strip():
        st.warning("URL을 입력하세요.")
        st.stop()

    # URL 정리 및 비디오 ID 추출
    clean_url = to_clean_watch_url(url.strip())
    vid = extract_video_id(clean_url)
    
    if not vid:
        st.error("유효한 YouTube 링크가 아닙니다. URL을 다시 확인해주세요.")
        st.stop()

    st.info(f"비디오 ID: {vid}")

    # 메타 정보 표시
    if show_meta:
        with st.spinner("영상 정보 가져오는 중..."):
            try:
                yt = safe_get_youtube_info(clean_url)
                if yt:
                    title = yt.title or "제목 확인 불가"
                    length_min = int((yt.length or 0) / 60)
                    st.info(f"**제목**: {title}  |  **길이**: 약 {length_min}분")
                else:
                    st.caption("영상 정보 조회 실패 - 자막 추출을 계속 진행합니다.")
            except Exception:
                st.caption("영상 정보 조회 실패 - 자막 추출을 계속 진행합니다.")

    # 자막 추출
    with st.spinner("자막 추출 중... (여러 방법을 순차적으로 시도합니다)"):
        try:
            transcript_text = fetch_transcript_resilient(clean_url, vid, lang_pref)
        except (NoTranscriptFound, TranscriptsDisabled) as e:
            st.error(f"자막을 찾을 수 없습니다: {str(e)}")
            st.info("이 영상은 자막이 없거나 비활성화되어 있을 수 있습니다.")
            st.stop()
        except VideoUnavailable:
            st.error("영상에 접근할 수 없습니다. (비공개, 지역제한, 연령제한 등)")
            st.stop()
        except TranscriptExtractionError as e:
            st.error(f"자막 추출 실패: {str(e)}")
            st.info("문제가 지속되면 다른 영상으로 시도해보세요.")
            st.stop()
        except Exception as e:
            st.error(f"예상치 못한 오류가 발생했습니다: {str(e)}")
            st.info("문제가 지속되면 다른 영상으로 시도해보세요.")
            st.stop()

    # 결과 출력
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
        word_count = len(transcript_text.split())
        st.caption(f"총 {word_count:,}개 단어")

    st.subheader("📄 추출된 자막")
    st.text_area(
        "", 
        value=transcript_text, 
        height=500,
        help="자막 내용을 확인하고 복사할 수 있습니다"
    )

# 하단 정보
st.markdown("---")
st.caption(
    "💡 **사용 팁**: 이 도구는 개인 학습/연구 목적으로 사용하세요. "
    "일부 영상은 저작권, 연령제한, 지역제한 등으로 처리되지 않을 수 있습니다."
)
