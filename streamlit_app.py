import re
import time
import html
from typing import Optional, List
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen

import streamlit as st
from youtube_transcript_api import (
    YouTubeTranscriptApi,
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)
from pytube import YouTube
import yt_dlp


# ---------------------------------
# URL 정리 / 비디오ID 추출
# ---------------------------------
YOUTUBE_URL_RE = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:watch\?v=|embed/|live/|shorts/))([\w-]{11})"
)

def extract_video_id(url: str) -> Optional[str]:
    if not url:
        return None
    m = YOUTUBE_URL_RE.search(url)
    if m:
        return m.group(1)
    try:
        q = urlparse(url)
        vid = parse_qs(q.query).get("v", [None])[0]
        if vid and len(vid) == 11:
            return vid
    except Exception:
        pass
    return None

def to_clean_watch_url(url_or_id: str) -> str:
    """짧은 주소/파라미터를 표준 watch URL로 정리."""
    vid = extract_video_id(url_or_id) if "http" in url_or_id else url_or_id
    return f"https://www.youtube.com/watch?v={vid}" if vid else url_or_id


# ---------------------------------
# 예외 유틸: 일관된 NoTranscriptFound 발생
# ---------------------------------
def raise_no_transcript(langs: List[str]) -> None:
    """
    youtube_transcript_api.NoTranscriptFound 는
    (requested_language_codes, transcript_data) 두 인자를 요구함.
    """
    raise NoTranscriptFound(langs, [])


# ---------------------------------
# 1) youtube_transcript_api (공식/자동생성)
# ---------------------------------
def fetch_via_yta(video_id: str, langs: List[str]) -> str:
    """업로더 자막 → 자동생성 자막 순으로 시도."""
    tl = YouTubeTranscriptApi.list_transcripts(video_id)
    try:
        tr = tl.find_transcript(langs)           # 업로더 자막
    except Exception:
        tr = tl.find_generated_transcript(langs) # 자동생성 자막
    entries = tr.fetch()
    if not entries:
        raise_no_transcript(langs)
    st.success(f"자막 확보(yta): lang={tr.language}, auto={tr.is_generated}")
    return "\n".join([f"[{e['start']:.1f}] {e['text']}" for e in entries])


# ---------------------------------
# 2) pytube captions 폴백 (SRT/XML)
# ---------------------------------
def clean_xml_text(xml_text: str) -> List[tuple]:
    """
    <text start="12.34" dur="3.21">문장</text> ... 형태의 XML에서
    (start, text) 리스트로 변환.
    """
    items = []
    xml_text = xml_text.replace("\n", "")
    for m in re.finditer(r'<text[^>]*start="([\d\.]+)"[^>]*>(.*?)</text>', xml_text):
        start = float(m.group(1))
        raw = re.sub(r"<.*?>", " ", m.group(2))
        text = html.unescape(raw)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            items.append((start, text))
    return items

def fetch_via_pytube(url_or_id: str, langs: List[str]) -> str:
    """pytube 자막 트랙(SRT/XML)에서 추출."""
    url = to_clean_watch_url(url_or_id)
    yt = YouTube(url)
    tracks = yt.captions  # CaptionQuery

    # 선호 언어 코드 + 자동생성 코드(a.xx) 후보 구성
    candidates = []
    for lg in langs:
        candidates.append(lg)          # 업로더 자막
    for lg in langs:
        candidates.append(f"a.{lg}")   # 자동생성 자막
    if "en" not in candidates:
        candidates += ["en", "a.en"]

    available_codes = {c.code: c for c in tracks}

    for code in candidates:
        cap = available_codes.get(code)
        if not cap:
            # ko-KR 같은 지역코드 매칭 보조
            for k, v in available_codes.items():
                if k.lower().startswith(code.lower()):
                    cap = v
                    break
        if not cap:
            continue

        # SRT → 파싱, 실패하면 XML 파싱
        try:
            try:
                srt = cap.generate_srt_captions()
                lines = []
                for block in srt.strip().split("\n\n"):
                    parts = block.split("\n")
                    if len(parts) >= 3:
                        # "00:00:12,340 --> 00:00:15,120"
                        ts = parts[1].split("-->")[0].strip()
                        h, m, s_ms = ts.split(":")
                        s, ms = s_ms.split(",")
                        start = int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0
                        text = " ".join(parts[2:]).strip()
                        if text:
                            lines.append(f"[{start:.1f}] {text}")
                if lines:
                    st.success(f"자막 확보(pytube-srt): {code}")
                    return "\n".join(lines)
            except Exception:
                pass

            xml = cap.xml_captions
            items = clean_xml_text(xml)
            if items:
                st.success(f"자막 확보(pytube-xml): {code}")
                return "\n".join([f"[{stt:.1f}] {txt}" for stt, txt in items])
        except Exception:
            continue

    # 여기에 도달하면 pytube로는 찾지 못함
    raise_no_transcript(langs)


# ---------------------------------
# 3) yt-dlp 폴백 (subtitles/automatic_captions)
# ---------------------------------
def parse_vtt(vtt: str) -> List[str]:
    """WebVTT를 [start] text 줄 형식으로 변환."""
    lines = []
    blocks = [b for b in vtt.strip().split("\n\n") if "-->" in b]
    for block in blocks:
        rows = block.split("\n")
        ts = rows[0]  # "00:00:01.000 --> 00:00:03.000"
        # 시작 시간만 초로 환산
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
    """yt-dlp 메타에서 자막 파일 URL을 얻어 직접 다운로드/파싱."""
    url = to_clean_watch_url(url_or_id)
    ydl_opts = {"quiet": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    subs = info.get("subtitles") or {}
    autos = info.get("automatic_captions") or {}

    candidates = []
    for lg in langs:
        if lg in subs:
            candidates.append(("subs", lg, subs[lg]))
    for lg in langs:
        if lg in autos:
            candidates.append(("auto", lg, autos[lg]))
    # 폴백 en
    if not any(c[1] == "en" for c in candidates):
        if "en" in subs:
            candidates.append(("subs", "en", subs["en"]))
        if "en" in autos:
            candidates.append(("auto", "en", autos["en"]))

    # 각 후보는 여러 포맷(예: vtt/ttml/srv3/…) 리스트를 가짐 → vtt 우선
    for kind, lg, lst in candidates:
        vtt_item = None
        first_item = lst[0] if lst else None
        for it in lst:
            ext = it.get("ext", "")
            if ext.lower() in ("vtt", "webvtt"):
                vtt_item = it
                break
        target = vtt_item or first_item
        if not target:
            continue

        try:
            with urlopen(target["url"]) as resp:
                data = resp.read().decode("utf-8", errors="ignore")
            if target.get("ext", "").lower() in ("vtt", "webvtt"):
                lines = parse_vtt(data)
                if lines:
                    st.success(f"자막 확보(yt-dlp-{kind}-vtt): {lg}")
                    return "\n".join(lines)
            else:
                # srv/json/xml 등: 우선 태그 제거로 빠르게 텍스트만 추출
                text = re.sub(r"<.*?>", " ", data)
                text = html.unescape(text)
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    st.success(f"자막 확보(yt-dlp-{kind}-{target.get('ext','raw')}): {lg}")
                    return text
        except Exception:
            continue

    # 여기까지 오면 yt-dlp 경로로도 실패
    raise_no_transcript(langs)


# ---------------------------------
# 최종 래퍼 (YTA → pytube → yt-dlp)
# ---------------------------------
def fetch_transcript_resilient(url: str, video_id: str, langs: List[str]) -> str:
    # 1) youtube_transcript_api
    try:
        return fetch_via_yta(video_id, langs)
    except (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable):
        pass
    except Exception:
        time.sleep(0.5)

    # 2) pytube
    try:
        return fetch_via_pytube(url, langs)
    except NoTranscriptFound:
        pass
    except Exception:
        time.sleep(0.5)

    # 3) yt-dlp
    return fetch_via_ytdlp(url, langs)


# ---------------------------------
# Streamlit UI
# ---------------------------------
st.set_page_config(page_title="YouTube 자막 추출기 (무료)", layout="wide")
st.title("🎬 YouTube 자막 추출기 — 0원 버전")
st.caption("자막만 처리합니다. ASR/요약 모델 호출 없음 → API 키 불필요, 완전 무료.")

with st.sidebar:
    st.header("설정")
    lang_pref = st.multiselect(
        "언어 우선순위 (위에서부터 시도)",
        ["ko", "en", "ja", "zh-Hans", "zh-Hant", "es", "fr", "de"],
        default=["ko", "en"],
    )
    show_meta = st.toggle("영상 제목/길이 표시", value=True)

url = st.text_input("YouTube 링크", placeholder="https://www.youtube.com/watch?v=...")
run = st.button("자막 추출 (무료)")

if run:
    if not url:
        st.warning("URL을 입력하세요.")
        st.stop()

    clean_url = to_clean_watch_url(url)  # ?t=8s 등 파라미터 정리
    vid = extract_video_id(clean_url)
    if not vid:
        st.error("유효한 YouTube 링크가 아닙니다.")
        st.stop()

    # (선택) 메타 정보
    if show_meta:
        try:
            yt = YouTube(clean_url)
            title = yt.title or "제목 확인 불가"
            length_min = int((yt.length or 0) / 60)
            st.info(f"**제목**: {title}  |  **길이**: 약 {length_min}분")
        except Exception:
            st.caption("제목/길이 조회 실패 — 계속 진행합니다.")

    # 자막 가져오기
    try:
        transcript_text = fetch_transcript_resilient(clean_url, vid, lang_pref)
    except NoTranscriptFound:
        st.error("이 영상은 자막이 없거나 비활성화되어 있습니다. (무료판은 자막만 처리 가능)")
        st.stop()
    except TranscriptsDisabled:
        st.error("이 영상은 자막 기능이 비활성화되어 있습니다.")
        st.stop()
    except VideoUnavailable:
        st.error("영상에 접근할 수 없습니다. (비공개, 지역/연령 제한 등)")
        st.stop()
    except Exception as e:
        st.error(f"자막 처리 중 오류: {e}")
        st.stop()

    # 출력
    st.subheader("📄 Raw 자막")
    st.download_button(
        "자막 저장 (TXT)",
        data=transcript_text.encode("utf-8"),
        file_name="transcript.txt",
        mime="text/plain",
    )
    st.text_area("", value=transcript_text, height=560)

st.markdown("---")
st.caption(
    "본 도구는 개인 학습/연구 목적의 자막 보기 용도입니다. 일부 영상은 라이선스/연령/지역 제한으로 처리되지 않을 수 있습니다."
)
