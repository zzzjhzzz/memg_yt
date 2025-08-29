import re
import time
import html
from typing import Optional, List
from urllib.parse import urlparse, parse_qs

import streamlit as st
from youtube_transcript_api import (
    YouTubeTranscriptApi,
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)
from pytube import YouTube

# -----------------------------
# 유튜브 URL → 비디오ID 추출
# -----------------------------
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

# -----------------------------
# 1차: youtube_transcript_api
# -----------------------------
def fetch_via_yta(video_id: str, langs: List[str]) -> str:
    # list_transcripts -> 공식/자동생성 우선
    tl = YouTubeTranscriptApi.list_transcripts(video_id)
    try:
        tr = tl.find_transcript(langs)  # 업로더 자막
    except Exception:
        tr = tl.find_generated_transcript(langs)  # 자동생성 자막
    entries = tr.fetch()
    st.success(f"자막 확보(yta): lang={tr.language}, auto={tr.is_generated}")
    return "\n".join([f"[{e['start']:.1f}] {e['text']}" for e in entries])

# -----------------------------
# 2차: pytube 자막 트랙 폴백
# -----------------------------
def clean_xml_text(xml_text: str) -> List[tuple]:
    """
    <text start="12.34" dur="3.21">문장</text> 형식의 xml을
    [(start, text), ...] 리스트로 변환
    """
    items = []
    # 줄바꿈/엔티티 정리
    xml_text = xml_text.replace("\n", "")
    # 간단한 정규식 파싱 (외부 의존성 없이)
    for m in re.finditer(r'<text[^>]*start="([\d\.]+)"[^>]*>(.*?)</text>', xml_text):
        start = float(m.group(1))
        # XML 안의 <br> 등 태그 제거 & 엔티티 디코딩
        raw = re.sub(r"<.*?>", " ", m.group(2))
        text = html.unescape(raw)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            items.append((start, text))
    return items

def fetch_via_pytube(url_or_id: str, langs: List[str]) -> str:
    """
    pytube로 자막 트랙을 찾아서 xml/srt를 파싱.
    자동생성 트랙은 보통 'a.xx' 코드.
    """
    yt = YouTube(url_or_id if url_or_id.startswith("http") else f"https://www.youtube.com/watch?v={url_or_id}")
    tracks = yt.captions  # CaptionQuery

    # 선호 언어 코드와 자동생성 코드 후보 구성
    candidates = []
    for lg in langs:
        candidates.append(lg)        # 공식
    for lg in langs:
        candidates.append(f"a.{lg}") # 자동생성

    # 추가 폴백: en, a.en
    if "en" not in candidates: candidates += ["en", "a.en"]

    # 사용가능한 코드 맵
    available_codes = {c.code: c for c in tracks}

    # 순서대로 시도
    for code in candidates:
        cap = available_codes.get(code)
        if not cap:
            # 일부 환경에서는 코드가 ko-KR처럼 지역코드 포함일 수 있어 시작 일치로 보조 매칭
            for k, v in available_codes.items():
                if k.lower().startswith(code.lower()):
                    cap = v; break
        if not cap:
            continue

        # srt가 되면 srt, 안 되면 xml 파싱
        try:
            try:
                srt = cap.generate_srt_captions()
                # SRT -> 간단 변환
                lines = []
                for block in srt.strip().split("\n\n"):
                    parts = block.split("\n")
                    if len(parts) >= 3:
                        # 00:00:12,340 --> 00:00:15,120
                        # 내용...
                        # 시작 시간만 초로 대략 변환
                        ts = parts[1].split("-->")[0].strip()
                        h, m, s_ms = ts.split(":")
                        s, ms = s_ms.split(",")
                        start = int(h)*3600 + int(m)*60 + int(s) + int(ms)/1000.0
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

    raise NoTranscriptFound("pytube: no caption track matched")

# -----------------------------
# 최종 래퍼 (재시도 포함)
# -----------------------------
def fetch_transcript_resilient(url: str, video_id: str, langs: List[str]) -> str:
    # 1) yta 우선 → 실패 시 pytube 폴백
    # 빈 응답/일시 오류 대비 재시도
    attempts = 2
    last_err = None

    for i in range(attempts):
        try:
            return fetch_via_yta(video_id, langs)
        except (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable) as e:
            last_err = e
            break
        except Exception as e:
            last_err = e
            time.sleep(0.7)

    # pytube 폴백 (URL 사용)
    attempts = 2
    for i in range(attempts):
        try:
            return fetch_via_pytube(url, langs)
        except Exception as e:
            last_err = e
            time.sleep(0.7)

    # 여기까지 오면 실패
    if isinstance(last_err, NoTranscriptFound):
        raise NoTranscriptFound("자막을 찾을 수 없습니다.")
    raise last_err or Exception("자막 처리 실패")

# -----------------------------
# Streamlit UI
# -----------------------------
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

    vid = extract_video_id(url)
    if not vid:
        st.error("유효한 YouTube 링크가 아닙니다.")
        st.stop()

    # (선택) 영상 메타정보
    if show_meta:
        try:
            yt = YouTube(url)
            title = yt.title or "제목 확인 불가"
            length_min = int((yt.length or 0) / 60)
            st.info(f"**제목**: {title}  |  **길이**: 약 {length_min}분")
        except Exception:
            st.caption("제목/길이 조회 실패 — 계속 진행합니다.")

    # 자막 가져오기 (재시도/폴백 포함)
    try:
        transcript_text = fetch_transcript_resilient(url, vid, lang_pref)
    except NoTranscriptFound:
        st.error("이 영상은 자막이 없거나 비활성화되어 있습니다. (무료판은 자막만 처리 가능합니다)")
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
