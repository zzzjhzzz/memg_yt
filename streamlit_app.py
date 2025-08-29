import re
import random
from time import sleep
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
from pytube import YouTube
import yt_dlp

# 커스텀 예외 클래스 정의
class TranscriptExtractionError(Exception):
    """자막 추출 실패 시 사용하는 커스텀 예외"""
    pass

# SSL 인증서 문제 해결
ssl._create_default_https_context = ssl._create_unverified_context

# ---------------------------------
# 자막 중복 제거 및 병합 함수들
# ---------------------------------
def clean_duplicate_subtitles(transcript_text: str) -> str:
    """자막에서 중복된 문장들을 제거"""
    lines = transcript_text.strip().split('\n')
    cleaned_lines = []
    seen_texts = set()
    
    for line in lines:
        if not line.strip():
            continue
            
        # 시간 태그와 텍스트 분리
        match = re.match(r'\[(\d+\.?\d*)\]\s*(.*)', line)
        if not match:
            continue
            
        timestamp = float(match.group(1))
        text = match.group(2).strip()
        
        if not text or text in ['[Music]', '[Applause]', '[Laughter]']:
            continue
            
        # 중복 텍스트 체크 (대소문자 구분 안함)
        text_lower = text.lower()
        
        # 완전 중복 제거
        if text_lower in seen_texts:
            continue
            
        # 부분 중복 제거 (한 문장이 다른 문장에 포함된 경우)
        is_duplicate = False
        texts_to_remove = []
        
        for seen_text in list(seen_texts):
            # 현재 텍스트가 이전 텍스트에 포함되거나 그 반대
            if text_lower in seen_text:
                # 현재 텍스트가 더 짧으면 스킵
                is_duplicate = True
                break
            elif seen_text in text_lower:
                # 이전 텍스트가 더 짧으면 제거 대상으로 마킹
                texts_to_remove.append(seen_text)
                
        if not is_duplicate:
            # 제거할 텍스트들 처리
            for old_text in texts_to_remove:
                seen_texts.discard(old_text)
            
            seen_texts.add(text_lower)
            cleaned_lines.append(f"[{timestamp:.1f}] {text}")
    
    return '\n'.join(cleaned_lines)

def merge_consecutive_subtitles(transcript_text: str, time_threshold: float = 2.0) -> str:
    """연속된 비슷한 자막들을 병합"""
    lines = transcript_text.strip().split('\n')
    merged_lines = []
    
    i = 0
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
            
        match = re.match(r'\[(\d+\.?\d*)\]\s*(.*)', lines[i])
        if not match:
            i += 1
            continue
            
        current_time = float(match.group(1))
        current_text = match.group(2).strip()
        
        # 다음 라인들과 비교해서 병합 가능한지 체크
        merged_text = current_text
        j = i + 1
        
        while j < len(lines):
            if j >= len(lines):
                break
                
            next_match = re.match(r'\[(\d+\.?\d*)\]\s*(.*)', lines[j])
            if not next_match:
                j += 1
                continue
                
            next_time = float(next_match.group(1))
            next_text = next_match.group(2).strip()
            
            # 시간이 너무 멀면 중단
            if (next_time - current_time) > time_threshold:
                break
                
            # 텍스트가 현재 텍스트의 연장인지 체크
            if (current_text.lower() in next_text.lower() or 
                next_text.lower() in current_text.lower()):
                # 더 긴 텍스트로 업데이트
                if len(next_text) > len(merged_text):
                    merged_text = next_text
                j += 1
            else:
                break
                
        merged_lines.append(f"[{current_time:.1f}] {merged_text}")
        i = max(i + 1, j)
    
    return '\n'.join(merged_lines)

def apply_subtitle_cleaning(raw_transcript: str, clean_duplicates: bool, merge_consecutive: bool) -> str:
    """사용자 설정에 따라 자막 정리 적용"""
    result = raw_transcript
    
    if clean_duplicates:
        result = clean_duplicate_subtitles(result)
    
    if merge_consecutive:
        result = merge_consecutive_subtitles(result)
    
    return result

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
    """yt-dlp로 안전한 YouTube 정보 가져오기"""
    try:
        ydl_opts = {
            "quiet": True,
            "noplaylist": True,
            "extract_flat": False,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
        class YouTubeInfo:
            def __init__(self, info_dict):
                self.title = info_dict.get('title', '제목 확인 불가')
                self.length = info_dict.get('duration', 0)
                
        return YouTubeInfo(info)
        
    except Exception as e:
        return None

# ---------------------------------
# 자막 추출 함수들
# ---------------------------------
def fetch_via_yta_with_retry(video_id: str, langs: List[str], max_retries: int = 3) -> str:
    """재시도 로직이 포함된 YTA 자막 추출"""
    last_error = None
    
    for attempt in range(max_retries):
        try:
            tl = YouTubeTranscriptApi.list_transcripts(video_id)
            
            try:
                tr = tl.find_transcript(langs)
            except Exception:
                tr = tl.find_generated_transcript(langs)
            
            entries = tr.fetch()
            st.success(f"자막 추출 성공 (YTA): {tr.language}" + (" [자동생성]" if tr.is_generated else " [수동]"))
            return "\n".join([f"[{e['start']:.1f}] {e['text']}" for e in entries])
            
        except Exception as e:
            last_error = e
            error_msg = str(e).lower()
            
            if "too many requests" in error_msg or "429" in error_msg:
                if attempt < max_retries - 1:
                    wait_time = (2 ** attempt) + random.uniform(1, 3)
                    sleep(wait_time)
                    continue
                else:
                    raise TranscriptExtractionError(f"YouTube API 요청 제한 초과 (429)")
            else:
                if isinstance(e, (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable)):
                    raise
                else:
                    raise TranscriptExtractionError(f"YTA 처리 실패: {str(e)}")
    
    raise TranscriptExtractionError(f"YTA 재시도 실패: {str(last_error)}")

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
    """pytube 자막 트랙에서 추출"""
    url = to_clean_watch_url(url_or_id)
    
    try:
        yt = YouTube(url, use_oauth=False, allow_oauth_cache=False)
        
        try:
            _ = yt.title
        except Exception:
            import urllib.request
            opener = urllib.request.build_opener()
            opener.addheaders = [('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')]
            urllib.request.install_opener(opener)
            
            yt = YouTube(url, use_oauth=False, allow_oauth_cache=False)
            _ = yt.title
        
        tracks = yt.captions
        if not tracks:
            raise TranscriptExtractionError("pytube: 자막 트랙이 없음")

        candidates = []
        for lg in langs:
            candidates.append(lg)
            candidates.append(f"a.{lg}")
        
        if "en" not in [c.replace("a.", "") for c in candidates]:
            candidates.extend(["en", "a.en"])

        available_codes = {c.code: c for c in tracks}

        for code in candidates:
            cap = available_codes.get(code)
            
            if not cap:
                for k, v in available_codes.items():
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
                    if not block.strip():
                        continue
                        
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
                        except (ValueError, IndexError):
                            continue
                
                if lines:
                    st.success(f"자막 추출 성공 (pytube): {code}")
                    return "\n".join(lines)
                    
            except Exception:
                try:
                    xml = cap.xml_captions
                    items = clean_xml_text(xml)
                    if items:
                        st.success(f"자막 추출 성공 (pytube): {code}")
                        return "\n".join([f"[{stt:.1f}] {txt}" for stt, txt in items])
                except Exception:
                    continue

    except Exception as e:
        raise TranscriptExtractionError(f"pytube 처리 실패: {str(e)}")
    
    raise TranscriptExtractionError(f"pytube: 매칭되는 자막 없음")

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

def parse_srv3_json(json_data: str) -> List[str]:
    """YouTube SRV3 JSON 자막 파싱"""
    try:
        import json
        data = json.loads(json_data)
        lines = []
        
        events = data.get("events", [])
        for event in events:
            start_time = event.get("tStartMs", 0) / 1000.0
            segs = event.get("segs", [])
            text = "".join([seg.get("utf8", "") for seg in segs]).strip()
            if text:
                lines.append(f"[{start_time:.1f}] {text}")
        
        return lines
    except Exception:
        return []

def parse_ttml(ttml_data: str) -> List[str]:
    """TTML XML 자막 파싱"""
    try:
        lines = []
        pattern = r'<p[^>]*begin="([^"]*)"[^>]*>(.*?)</p>'
        
        for match in re.finditer(pattern, ttml_data, re.DOTALL):
            time_str = match.group(1)
            text_content = match.group(2)
            
            try:
                parts = time_str.replace(',', '.').split(':')
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
    """향상된 yt-dlp 자막 가져오기"""
    url = to_clean_watch_url(url_or_id)
    
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "socket_timeout": 60,
        "retries": 3,
        "http_headers": {
            "User-Agent": random.choice([
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ])
        }
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
                with urlopen(item["url"]) as resp:
                    data = resp.read().decode("utf-8", errors="ignore")
                
                ext = item.get("ext", "").lower()
                
                if ext in ("vtt", "webvtt"):
                    lines = parse_vtt(data)
                    if lines:
                        st.success(f"자막 추출 성공 (yt-dlp): {lg} ({kind}, {ext.upper()})")
                        return "\n".join(lines)
                
                elif ext == "srv3":
                    lines = parse_srv3_json(data)
                    if lines:
                        st.success(f"자막 추출 성공 (yt-dlp): {lg} ({kind}, SRV3)")
                        return "\n".join(lines)
                
                elif ext == "ttml":
                    lines = parse_ttml(data)
                    if lines:
                        st.success(f"자막 추출 성공 (yt-dlp): {lg} ({kind}, TTML)")
                        return "\n".join(lines)
                        
                else:
                    text = re.sub(r"<.*?>", " ", data)
                    text = html.unescape(text)
                    text = re.sub(r"\s+", " ", text).strip()
                    if text and len(text) > 100:
                        st.success(f"자막 추출 성공 (yt-dlp): {lg} ({kind}, {ext.upper()})")
                        return text
                        
            except Exception:
                continue

    available_langs = list(set(list(subs.keys()) + list(autos.keys())))
    raise TranscriptExtractionError(f"yt-dlp: 자막 추출 실패 (사용가능: {available_langs})")

def fetch_transcript_resilient(url: str, video_id: str, langs: List[str]) -> str:
    """3단계 폴백으로 자막 가져오기"""
    errors = []
    
    # 1) youtube_transcript_api
    try:
        return fetch_via_yta_with_retry(video_id, langs)
    except (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable) as e:
        errors.append(f"YTA: {str(e)}")
        sleep(1)
    except TranscriptExtractionError as e:
        errors.append(f"YTA: {str(e)}")
        sleep(1)
    except Exception as e:
        errors.append(f"YTA: {str(e)}")
        sleep(1)

    # 2) yt-dlp
    try:
        return fetch_via_ytdlp_enhanced(url, langs)
    except TranscriptExtractionError as e:
        errors.append(f"yt-dlp: {str(e)}")
        sleep(1)
    except Exception as e:
        errors.append(f"yt-dlp: {str(e)}")
        sleep(1)

    # 3) pytube
    try:
        return fetch_via_pytube(url, langs)
    except TranscriptExtractionError as e:
        errors.append(f"pytube: {str(e)}")
    except Exception as e:
        errors.append(f"pytube: {str(e)}")

    # 모든 방법 실패 시
    with st.expander("상세 오류 정보", expanded=False):
        for i, error in enumerate(errors, 1):
            st.text(f"{i}. {error}")
    
    if any("429" in err or "Too Many Requests" in err for err in errors):
        raise TranscriptExtractionError("YouTube API 요청 제한 (429) - 잠시 후 다시 시도하거나 다른 영상을 사용해주세요")
    elif any("자막" in err and ("없음" in err or "찾을 수 없음" in err) for err in errors):
        raise TranscriptExtractionError("이 영상에는 자막이 없습니다")
    else:
        raise TranscriptExtractionError("자막 추출 실패 - 위의 상세 정보를 확인하세요")

# ---------------------------------
# Streamlit UI
# ---------------------------------
st.set_page_config(page_title="YouTube 자막 추출기", layout="wide")
st.title("YouTube 자막 추출기")
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
    
    st.subheader("자막 정리 옵션")
    clean_duplicates = st.toggle(
        "중복 자막 제거", 
        value=True,
        help="같은 내용이 반복되는 자막을 제거합니다"
    )
    merge_consecutive = st.toggle(
        "연속 자막 병합", 
        value=True,
        help="비슷한 시간대의 유사한 자막을 병합합니다"
    )
    
    st.subheader("출력 옵션")
    show_original = st.toggle(
        "원본 자막도 함께 표시", 
        value=False,
        help="정리된 자막과 원본 자막을 모두 표시합니다"
    )

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
                info = safe_get_youtube_info(clean_url)
                if info:
                    title = info.title
                    length_min = int((info.length or 0) / 60) if info.length else 0
                    st.info(f"**제목**: {title}  |  **길이**: 약 {length_min}분")
                else:
                    st.caption("영상 정보 조회 실패 - 자막 추출을 계속 진행합니다.")
            except Exception as e:
                st.caption(f"영상 정보 조회 실패 - 자막 추출을 계속 진행합니다.")

    # 자막 추출
    with st.spinner("자막 추출 중..."):
        try:
            raw_transcript = fetch_transcript_resilient(clean_url, vid, lang_pref)
        except TranscriptExtractionError as e:
            st.error(f"자막 추출 실패: {str(e)}")
            st.stop()
        except (NoTranscriptFound, TranscriptsDisabled) as e:
            st.error(f"자막을 찾을 수 없습니다: {str(e)}")
            st.stop()
        except VideoUnavailable:
            st.error("영상에 접근할 수 없습니다 (비공개, 지역제한, 연령제한 등)")
            st.stop()
        except Exception as e:
            st.error(f"예상치 못한 오류: {str(e)}")
            st.stop()

    # 자막 정리 적용
    if clean_duplicates or merge_consecutive:
        with st.spinner("자막 정리 중..."):
            cleaned_transcript = apply_subtitle_cleaning(raw_transcript, clean_duplicates, merge_consecutive)
    else:
        cleaned_transcript = raw_transcript

    # 결과 출력
    st.success("자막 추출 완료!")
    
    # 통계 정보
    col1, col2 = st.columns([1, 1])
    with col1:
        raw_word_count = len(raw_transcript.split())
        st.caption(f"원본: {raw_word_count:,}개 단어")
    
    with col2:
        if cleaned_transcript != raw_transcript:
            cleaned_word_count = len(cleaned_transcript.split())
            reduction = raw_word_count - cleaned_word_count
            st.caption(f"정리됨: {cleaned_word_count:,}개 단어 (-{reduction})")
        else:
            st.caption("정리 옵션이 비활성화됨")

    # 다운로드 버튼들
    download_col1, download_col2 = st.columns([1, 1])
    
    with download_col1:
        st.download_button(
            "정리된 자막 다운로드 (TXT)",
            data=cleaned_transcript.encode("utf-8"),
            file_name=f"transcript_cleaned_{vid}.txt",
            mime="text/plain",
        )
    
    with download_col2:
        if show_original:
            st.download_button(
                "원본 자막 다운로드 (TXT)",
                data=raw_transcript.encode("utf-8"),
                file_name=f"transcript_original_{vid}.txt",
                mime="text/plain",
            )

    # 자막 내용 표시
    if show_original and cleaned_transcript != raw_transcript:
        # 원본과 정리된 것을 탭으로 분리
        tab1, tab2 = st.tabs(["정리된 자막", "원본 자막"])
        
        with tab1:
            st.text_area(
                "", 
                value=cleaned_transcript, 
                height=500,
                help="중복 제거 및 병합이 적용된 자막입니다",
                key="cleaned_transcript"
            )
        
        with tab2:
            st.text_area(
                "", 
                value=raw_transcript, 
                height=500,
                help="원본 자막 그대로입니다",
                key="original_transcript"
            )
    else:
        # 하나만 표시
        st.subheader("추출된 자막")
        display_transcript = cleaned_transcript if (clean_duplicates or merge_consecutive) else raw_transcript
        st.text_area(
            "", 
            value=display_transcript, 
            height=500,
            help="자막 내용을 확인하고 복사할 수 있습니다"
        )

# 하단 정보
st.markdown("---")
st.caption(
    "사용 팁: 이 도구는 개인 학습/연구 목적으로 사용하세요. "
    "일부 영상은 저작권, 연령제한, 지역제한 등으로 처리되지 않을 수 있습니다."
)
