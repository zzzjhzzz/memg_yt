import re
import random
from time import sleep
import html
from typing import Optional, List
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen, Request
import ssl
import hashlib

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
# 봇 차단 우회 설정
# ---------------------------------

# 더 다양하고 현실적인 User-Agent 목록
USER_AGENTS = [
    # Chrome Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    # Chrome Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    # Firefox
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:122.0) Gecko/20100101 Firefox/122.0",
    # Safari
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2.1 Safari/605.1.15",
    # Edge
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
]

def get_realistic_headers():
    """실제 브라우저와 유사한 헤더 생성"""
    ua = random.choice(USER_AGENTS)
    
    # User-Agent에 따른 브라우저 타입 결정
    if "Chrome" in ua:
        browser_hints = {
            "sec-ch-ua": '"Not A(Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"' if "Windows" in ua else '"macOS"',
        }
    else:
        browser_hints = {}
    
    base_headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Cache-Control": "max-age=0",
    }
    
    # 브라우저별 헤더 추가
    base_headers.update(browser_hints)
    
    return base_headers

def get_session_fingerprint():
    """세션별 고유 식별자 생성 (IP 변경 시뮬레이션용)"""
    if 'session_id' not in st.session_state:
        st.session_state.session_id = hashlib.md5(str(random.random()).encode()).hexdigest()[:8]
    return st.session_state.session_id

def smart_delay(attempt: int = 0, base_delay: float = 1.0):
    """지능적 대기 (인간과 유사한 패턴)"""
    # 기본 대기 + 지수 백오프 + 랜덤 지터
    delay = base_delay * (1.5 ** attempt) + random.uniform(0.5, 2.0)
    
    # 너무 길면 최대값으로 제한
    delay = min(delay, 15.0)
    
    st.caption(f"⏳ 자연스러운 간격으로 대기 중... ({delay:.1f}초)")
    sleep(delay)

# ---------------------------------
# 자막 중복 제거 및 병합 함수들 (기존과 동일)
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
# URL 정리 / 비디오ID 추출 (기존과 동일)
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

# ---------------------------------
# 향상된 자막 추출 함수들
# ---------------------------------
def fetch_via_yta_with_enhanced_retry(video_id: str, langs: List[str], max_retries: int = 3) -> str:
    """향상된 재시도 로직이 포함된 YTA 자막 추출"""
    last_error = None
    session_id = get_session_fingerprint()
    
    for attempt in range(max_retries):
        try:
            # 각 시도마다 약간의 지연
            if attempt > 0:
                smart_delay(attempt, 2.0)
            
            # 세션 상태 표시
            st.caption(f"🔄 YTA 시도 {attempt + 1}/{max_retries} (세션: {session_id})")
            
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
            
            # 특정 오류 타입에 따른 처리
            if any(phrase in error_msg for phrase in ["too many requests", "429", "rate limit"]):
                if attempt < max_retries - 1:
                    wait_time = (2 ** attempt) + random.uniform(3, 8)
                    st.warning(f"⚠️ API 요청 제한 감지. {wait_time:.1f}초 후 재시도...")
                    sleep(wait_time)
                    continue
                else:
                    raise TranscriptExtractionError(f"YouTube API 요청 제한 초과")
            elif any(phrase in error_msg for phrase in ["403", "forbidden", "blocked"]):
                # IP 차단의 경우 더 긴 대기
                if attempt < max_retries - 1:
                    wait_time = 10 + random.uniform(5, 15)
                    st.warning(f"🚫 접근 차단 감지. {wait_time:.1f}초 후 재시도...")
                    sleep(wait_time)
                    continue
                else:
                    raise TranscriptExtractionError(f"YouTube에서 접근을 차단했습니다")
            else:
                # 다른 오류는 재시도 없이 바로 발생
                if isinstance(e, (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable)):
                    raise
                else:
                    raise TranscriptExtractionError(f"YTA 처리 실패: {str(e)}")
    
    raise TranscriptExtractionError(f"YTA 재시도 실패: {str(last_error)}")

def safe_get_youtube_info_enhanced(url: str):
    """향상된 안전한 YouTube 정보 가져오기"""
    try:
        headers = get_realistic_headers()
        
        ydl_opts = {
            "quiet": True,
            "noplaylist": True,
            "extract_flat": False,
            "http_headers": headers,
            "socket_timeout": 30,
            "retries": 2,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
        class YouTubeInfo:
            def __init__(self, info_dict):
                self.title = info_dict.get('title', '제목 확인 불가')
                self.length = info_dict.get('duration', 0)
                
        return YouTubeInfo(info)
        
    except Exception:
        return None

def fetch_via_ytdlp_enhanced_stealth(url_or_id: str, langs: List[str]) -> str:
    """스텔스 모드 yt-dlp 자막 가져오기"""
    url = to_clean_watch_url(url_or_id)
    headers = get_realistic_headers()
    session_id = get_session_fingerprint()
    
    # 더 현실적인 yt-dlp 설정
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "socket_timeout": 45,
        "retries": 2,
        "http_headers": headers,
        # YouTube 우회를 위한 추가 옵션들
        "extractor_args": {
            "youtube": {
                "skip": ["dash", "hls"],
                "player_client": ["android", "web"],
            }
        },
        # 쿠키 및 캐시 설정
        "cachedir": False,
        "no_cache_dir": True,
    }
    
    st.caption(f"🔍 yt-dlp 스텔스 모드 (세션: {session_id})")
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise TranscriptExtractionError(f"yt-dlp 정보 추출 실패: {str(e)}")

    subs = info.get("subtitles") or {}
    autos = info.get("automatic_captions") or {}
    
    candidates = []
    
    # 우선순위: 수동 자막 > 자동 자막
    for lg in langs:
        if lg in subs:
            candidates.append(("manual", lg, subs[lg]))
    
    for lg in langs:
        if lg in autos:
            candidates.append(("auto", lg, autos[lg]))
    
    # 영어 폴백
    if "en" not in langs:
        if "en" in subs:
            candidates.append(("manual", "en", subs["en"]))
        if "en" in autos:
            candidates.append(("auto", "en", autos["en"]))
    
    # 아무 언어나 사용
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
        
        # 포맷 우선순위에 따라 정렬
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
                # 향상된 헤더로 요청
                req = Request(item["url"], headers=headers)
                
                # 작은 랜덤 지연 추가
                sleep(random.uniform(0.5, 1.5))
                
                with urlopen(req, timeout=30) as resp:
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
                    # 일반 텍스트 처리
                    text = re.sub(r"<.*?>", " ", data)
                    text = html.unescape(text)
                    text = re.sub(r"\s+", " ", text).strip()
                    if text and len(text) > 100:
                        st.success(f"자막 추출 성공 (yt-dlp): {lg} ({kind}, {ext.upper()})")
                        return text
                        
            except Exception as e:
                st.caption(f"⚠️ {ext.upper()} 포맷 실패: {str(e)[:50]}...")
                continue

    available_langs = list(set(list(subs.keys()) + list(autos.keys())))
    raise TranscriptExtractionError(f"yt-dlp: 자막 추출 실패 (사용가능: {available_langs})")

def fetch_via_pytube_enhanced(url_or_id: str, langs: List[str]) -> str:
    """향상된 pytube 자막 추출"""
    url = to_clean_watch_url(url_or_id)
    session_id = get_session_fingerprint()
    
    st.caption(f"🔍 pytube 향상 모드 (세션: {session_id})")
    
    try:
        # User-Agent 설정으로 pytube 초기화
        import urllib.request
        opener = urllib.request.build_opener()
        headers = get_realistic_headers()
        opener.addheaders = [(k, v) for k, v in headers.items()]
        urllib.request.install_opener(opener)
        
        # 첫 번째 시도
        try:
            yt = YouTube(url, use_oauth=False, allow_oauth_cache=False)
            _ = yt.title  # 메타데이터 로드 테스트
        except Exception:
            # 재시도 with 다른 헤더
            smart_delay(0, 1.0)
            headers = get_realistic_headers()
            opener = urllib.request.build_opener()
            opener.addheaders = [(k, v) for k, v in headers.items()]
            urllib.request.install_opener(opener)
            
            yt = YouTube(url, use_oauth=False, allow_oauth_cache=False)
            _ = yt.title
        
        tracks = yt.captions
        if not tracks:
            raise TranscriptExtractionError("pytube: 자막 트랙이 없음")

        # 언어 우선순위 설정
        candidates = []
        for lg in langs:
            candidates.append(lg)
            candidates.append(f"a.{lg}")  # 자동생성 자막
        
        if "en" not in [c.replace("a.", "") for c in candidates]:
            candidates.extend(["en", "a.en"])

        available_codes = {c.code: c for c in tracks}

        for code in candidates:
            cap = available_codes.get(code)
            
            # 부분 매칭 시도
            if not cap:
                for k, v in available_codes.items():
                    if k.lower().startswith(code.lower().replace("a.", "")):
                        cap = v
                        code = k
                        break
            
            if not cap:
                continue

            try:
                # SRT 방식 먼저 시도
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
                # XML 방식으로 폴백
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

# 기존 파싱 함수들 (parse_vtt, parse_srv3_json, parse_ttml, clean_xml_text)은 동일

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

def fetch_transcript_resilient_enhanced(url: str, video_id: str, langs: List[str]) -> str:
    """향상된 3단계 폴백으로 자막 가져오기"""
    errors = []
    method_results = []
    session_id = get_session_fingerprint()
    
    st.info(f"🎯 자막 추출 시작 (세션: {session_id}, 언어: {', '.join(langs)})")
    
    # 세션 기반 방법 순서 랜덤화
    methods = ["yta", "ytdlp", "pytube"]
    if int(session_id[-1], 16) % 2 == 0:  # 세션 ID 기반으로 순서 변경
        methods = ["ytdlp", "yta", "pytube"]
    
    for i, method in enumerate(methods):
        if i > 0:
            smart_delay(i-1, 3.0)  # 방법 간 지연
        
        st.write(f"🔄 **방법 {i+1}/3**: {method.upper()} 시도 중...")
        
        try:
            if method == "yta":
                result = fetch_via_yta_with_enhanced_retry(video_id, langs, max_retries)
            elif method == "ytdlp":
                result = fetch_via_ytdlp_enhanced_stealth(url, langs)
            elif method == "pytube":
                result = fetch_via_pytube_enhanced(url, langs)
            
            if result and len(result.strip()) > 0:
                st.write(f"✅ **{method.upper()} 성공**: {len(result)} 문자 추출")
                return result
            else:
                st.write(f"⚠️ {method.upper()} 빈 결과")
                method_results.append((method.upper(), "빈 결과"))
                
        except TranscriptExtractionError as e:
            st.write(f"❌ {method.upper()} 실패: {str(e)}")
            method_results.append((method.upper(), f"실패: {str(e)}"))
            errors.append(f"{method.upper()}: {str(e)}")
        except (NoTranscriptFound, TranscriptsDisabled) as e:
            st.write(f"❌ {method.upper()} 자막 없음: {str(e)}")
            method_results.append((method.upper(), f"자막 없음: {str(e)}"))
            errors.append(f"{method.upper()}: {str(e)}")
        except VideoUnavailable as e:
            st.write(f"❌ {method.upper()} 영상 접근 불가: {str(e)}")
            method_results.append((method.upper(), f"영상 접근 불가: {str(e)}"))
            errors.append(f"{method.upper()}: 영상 접근 불가 - {str(e)}")
            # 영상 접근 불가면 다른 방법도 실패할 가능성이 높음
            break
        except Exception as e:
            st.write(f"❌ {method.upper()} 예상치 못한 오류: {str(e)}")
            method_results.append((method.upper(), f"예상치 못한 오류: {str(e)}"))
            errors.append(f"{method.upper()}: 예상치 못한 오류 - {str(e)}")

    # 실패 분석 및 권장사항
    st.error("🚫 **모든 방법 실패**")
    
    with st.expander("📊 상세 실패 분석", expanded=True):
        for i, (method, error) in enumerate(method_results, 1):
            st.text(f"{i}. {method}: {error}")
    
    # 오류 패턴 분석 및 해결책 제안
    all_errors_text = " ".join(errors).lower()
    
    st.subheader("🔧 권장 해결책")
    
    if any(phrase in all_errors_text for phrase in ["429", "too many requests", "rate limit"]):
        st.warning("**원인**: YouTube API 요청 제한")
        st.markdown("""
        **해결책**:
        - 5-10분 후 다시 시도
        - VPN 사용하여 IP 변경
        - 다른 시간대에 시도
        - 여러 영상을 연속으로 처리하지 말고 개별적으로 처리
        """)
        raise TranscriptExtractionError("YouTube API 요청 제한 - 잠시 후 다시 시도하세요")
        
    elif any(phrase in all_errors_text for phrase in ["403", "forbidden", "blocked", "400", "bad request"]):
        st.warning("**원인**: IP/봇 차단")
        st.markdown("""
        **해결책**:
        - VPN으로 다른 국가 IP 사용
        - 모바일 네트워크 사용
        - 시크릿/프라이빗 브라우저에서 영상 접근 테스트
        - 다른 시간대에 재시도
        """)
        raise TranscriptExtractionError("YouTube에서 접근을 차단했습니다 - VPN 사용을 권장합니다")
        
    elif any(phrase in all_errors_text for phrase in ["subtitles are disabled", "no transcript found", "자막 없음"]):
        st.info("**원인**: 자막 비활성화")
        st.markdown("""
        **확인사항**:
        - 해당 영상에 실제로 자막이 있는지 YouTube에서 직접 확인
        - 자동생성 자막도 활성화되어 있는지 확인
        - 다른 언어의 자막이 있는지 확인
        """)
        raise TranscriptExtractionError("이 영상에는 자막이 없거나 자막 기능이 비활성화되어 있습니다")
        
    elif any(phrase in all_errors_text for phrase in ["영상 접근 불가", "video unavailable", "private"]):
        st.info("**원인**: 영상 접근 제한")
        st.markdown("""
        **확인사항**:
        - 영상이 비공개 설정인지 확인
        - 연령 제한이 있는지 확인
        - 지역 제한이 있는지 확인
        - 영상이 삭제되었는지 확인
        """)
        raise TranscriptExtractionError("영상에 접근할 수 없습니다 (비공개, 연령제한, 지역제한 등)")
        
    else:
        st.warning("**원인**: 알 수 없는 오류")
        st.markdown("""
        **일반적 해결책**:
        - 네트워크 연결 확인
        - 잠시 후 다시 시도
        - 다른 브라우저나 환경에서 시도
        - YouTube에서 해당 영상 직접 접근 가능한지 확인
        """)
        raise TranscriptExtractionError("알 수 없는 이유로 자막 추출에 실패했습니다")

# ---------------------------------
# Streamlit UI (향상된 버전)
# ---------------------------------
st.set_page_config(page_title="YouTube 자막 추출기 (Anti-Bot)", layout="wide")
st.title("🎬 YouTube 자막 추출기")
st.caption("YouTube 영상의 자막을 추출합니다. 봇 차단 우회 기능 포함.")

# 세션 상태 초기화
if 'extraction_count' not in st.session_state:
    st.session_state.extraction_count = 0
if 'last_extraction_time' not in st.session_state:
    st.session_state.last_extraction_time = 0

with st.sidebar:
    st.header("⚙️ 설정")
    
    # 세션 정보 표시
    session_id = get_session_fingerprint()
    st.info(f"세션 ID: {session_id}")
    st.caption(f"추출 횟수: {st.session_state.extraction_count}")
    
    lang_pref = st.multiselect(
        "언어 우선순위 (위에서부터 시도)",
        ["ko", "en", "ja", "zh-Hans", "zh-Hant", "es", "fr", "de"],
        default=["ko", "en"],
        help="선호하는 언어를 순서대로 선택하세요"
    )
    
    show_meta = st.toggle("영상 제목/길이 표시", value=True)
    
    st.subheader("🧹 자막 정리 옵션")
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
    
    st.subheader("📤 출력 옵션")
    show_original = st.toggle(
        "원본 자막도 함께 표시", 
        value=False,
        help="정리된 자막과 원본 자막을 모두 표시합니다"
    )
    
    # 차단 우회 옵션
    st.subheader("🛡️ 차단 우회 설정")
    base_delay = st.slider(
        "기본 대기 시간 (초)", 
        min_value=0.5, 
        max_value=5.0, 
        value=2.0,
        help="요청 간 기본 대기 시간"
    )
    
    max_retries = st.slider(
        "최대 재시도 횟수", 
        min_value=1, 
        max_value=5, 
        value=3,
        help="각 방법별 최대 재시도 횟수"
    )

# 메인 입력
url = st.text_input(
    "🔗 YouTube 링크", 
    placeholder="https://www.youtube.com/watch?v=... 또는 https://youtu.be/...",
    help="YouTube 영상의 URL을 입력하세요"
)

# 추출 횟수 제한 경고
if st.session_state.extraction_count >= 10:
    st.warning("⚠️ 많은 추출을 수행했습니다. IP 차단 위험이 있으니 잠시 휴식 후 사용하세요.")

run = st.button("🚀 자막 추출", type="primary")

if run:
    if not url.strip():
        st.warning("URL을 입력하세요.")
        st.stop()

    # 요청 제한 체크
    import time
    current_time = time.time()
    if current_time - st.session_state.last_extraction_time < 10:
        remaining = 10 - (current_time - st.session_state.last_extraction_time)
        st.warning(f"⏰ 요청 제한: {remaining:.1f}초 후 다시 시도하세요.")
        st.stop()

    clean_url = to_clean_watch_url(url.strip())
    vid = extract_video_id(clean_url)
    
    if not vid:
        st.error("❌ 유효한 YouTube 링크가 아닙니다. URL을 다시 확인해주세요.")
        st.stop()

    st.info(f"🎯 비디오 ID: `{vid}`")

    # 추출 횟수 업데이트
    st.session_state.extraction_count += 1
    st.session_state.last_extraction_time = current_time

    # 메타 정보 표시
    if show_meta:
        with st.spinner("📋 영상 정보 가져오는 중..."):
            try:
                info = safe_get_youtube_info_enhanced(clean_url)
                if info:
                    title = info.title
                    length_min = int((info.length or 0) / 60) if info.length else 0
                    st.success(f"**📹 제목**: {title}")
                    st.info(f"**⏱️ 길이**: 약 {length_min}분")
                else:
                    st.caption("영상 정보 조회 실패 - 자막 추출을 계속 진행합니다.")
            except Exception:
                st.caption("영상 정보 조회 실패 - 자막 추출을 계속 진행합니다.")

    # 자막 추출
    with st.spinner("🔍 자막 추출 중..."):
        try:
            raw_transcript = fetch_transcript_resilient_enhanced(clean_url, vid, lang_pref)
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
        with st.spinner("🧹 자막 정리 중..."):
            cleaned_transcript = apply_subtitle_cleaning(raw_transcript, clean_duplicates, merge_consecutive)
    else:
        cleaned_transcript = raw_transcript

    # 결과 출력
    st.success("🎉 자막 추출 완료!")
    
    # 통계 정보
    col1, col2, col3 = st.columns([1, 1, 1])
    with col1:
        raw_word_count = len(raw_transcript.split())
        raw_lines = len([l for l in raw_transcript.split('\n') if l.strip()])
        st.metric("원본", f"{raw_word_count:,}개 단어", f"{raw_lines}줄")
    
    with col2:
        if cleaned_transcript != raw_transcript:
            cleaned_word_count = len(cleaned_transcript.split())
            cleaned_lines = len([l for l in cleaned_transcript.split('\n') if l.strip()])
            word_reduction = raw_word_count - cleaned_word_count
            line_reduction = raw_lines - cleaned_lines
            st.metric("정리됨", f"{cleaned_word_count:,}개 단어", f"-{word_reduction} 단어, -{line_reduction} 줄")
        else:
            st.metric("정리됨", "비활성화", "설정에서 활성화 가능")
    
    with col3:
        efficiency = (len(cleaned_transcript) / len(raw_transcript) * 100) if raw_transcript else 0
        st.metric("압축률", f"{efficiency:.1f}%", "")

    # 다운로드 버튼들
    st.subheader("💾 다운로드")
    download_col1, download_col2 = st.columns([1, 1])
    
    with download_col1:
        st.download_button(
            "📄 정리된 자막 다운로드 (TXT)",
            data=cleaned_transcript.encode("utf-8"),
            file_name=f"transcript_cleaned_{vid}.txt",
            mime="text/plain",
        )
    
    with download_col2:
        if show_original:
            st.download_button(
                "📄 원본 자막 다운로드 (TXT)",
                data=raw_transcript.encode("utf-8"),
                file_name=f"transcript_original_{vid}.txt",
                mime="text/plain",
            )

    # 자막 내용 표시
    st.subheader("📜 자막 내용")
    
    if show_original and cleaned_transcript != raw_transcript:
        # 원본과 정리된 것을 탭으로 분리
        tab1, tab2 = st.tabs(["🧹 정리된 자막", "📋 원본 자막"])
        
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
        display_transcript = cleaned_transcript if (clean_duplicates or merge_consecutive) else raw_transcript
        st.text_area(
            "", 
            value=display_transcript, 
            height=500,
            help="자막 내용을 확인하고 복사할 수 있습니다"
        )

# 하단 정보 및 팁
st.markdown("---")
st.markdown("### 💡 사용 팁")

tip_col1, tip_col2 = st.columns([1, 1])

with tip_col1:
    st.markdown("""
    **차단 우회 팁**:
    - 연속 추출 시 10분 이상 간격 두기
    - VPN 사용으로 IP 변경
    - 시간대별 제한이 다르니 다른 시간에 시도
    - 너무 많은 영상을 한번에 처리하지 말기
    """)

with tip_col2:
    st.markdown("""
    **일반 사용 팁**:
    - 개인 학습/연구 목적으로만 사용
    - 저작권 보호된 콘텐츠 주의
    - 긴 영상일수록 추출 시간 오래 걸림
    - 자막 정리 옵션으로 가독성 향상
    """)

# 트러블슈팅 가이드
with st.expander("🔧 트러블슈팅 가이드"):
    st.markdown("""
    **문제별 해결책**:
    
    1. **429 오류 (Too Many Requests)**
       - 10-30분 대기 후 재시도
       - VPN으로 IP 변경
       - 다른 네트워크 환경 사용
    
    2. **403 오류 (Forbidden)**
       - VPN 사용 필수
       - 다른 국가 서버 선택
       - 모바일 네트워크 시도
    
    3. **자막 없음 오류**
       - YouTube에서 직접 자막 확인
       - 다른 언어 자막 시도
       - 자동생성 자막 활성화 확인
    
    4. **영상 접근 불가**
       - 영상 공개 상태 확인
       - 연령/지역 제한 확인
       - 직접 YouTube에서 시청 가능한지 확인
    
    5. **일반적인 차단 현상**
       - 하루에 5-10개 영상 이하로 제한
       - 각 추출 간 최소 2-3분 간격
       - 프록시나 VPN 순환 사용
    """)

st.caption("⚠️ 이 도구는 교육 및 연구 목적으로만 사용하세요. YouTube 서비스 약관을 준수해주세요.")
