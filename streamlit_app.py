import re
from urllib.parse import urlparse, parse_qs

import streamlit as st
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled, VideoUnavailable
from pytube import YouTube

YOUTUBE_URL_RE = re.compile(r"(?:youtu.be/|youtube.com/(?:watch\?v=|embed/|live/|shorts/))([\w-]{11})")

def extract_video_id(url: str) -> str | None:
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

st.set_page_config(page_title="YouTube 자막 추출기 (무료)", layout="wide")
st.title("🎬 YouTube 자막 추출기 — 0원 버전")
st.caption("자막만 처리합니다. ASR/요약 모델 호출 없음 → 완전 무료.")

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

    # 메타정보(선택): pytube로 제목/길이 표시 (네트워크 실패해도 무시)
    if show_meta:
        try:
            yt = YouTube(url)
            title = yt.title or "제목 확인 불가"
            length_min = int((yt.length or 0) / 60)
            st.info(f"**제목**: {title}  |  **길이**: 약 {length_min}분")
        except Exception:
            st.caption("제목/길이 조회 실패 — 계속 진행합니다.")

    # 자막 시도
    transcript_text = None
    error_msg = None
    try:
        tl = YouTubeTranscriptApi.list_transcripts(vid)
        try:
            tr = tl.find_transcript(lang_pref)
        except Exception:
            tr = tl.find_generated_transcript(lang_pref)
        entries = tr.fetch()
        transcript_text = "\n".join([f"[{e['start']:.1f}] {e['text']}" for e in entries])
        st.success(f"자막 확보: lang={tr.language}, auto={tr.is_generated}")
    except (NoTranscriptFound, TranscriptsDisabled):
        error_msg = "이 영상은 자막이 없거나 비활성화되어 있습니다. (무료판은 자막만 처리 가능)"
    except VideoUnavailable:
        error_msg = "영상에 접근할 수 없습니다. (비공개, 지역제한 등)"
    except Exception as e:
        error_msg = f"자막 처리 중 오류: {e}"

    if error_msg:
        st.error(error_msg)
        st.stop()

    # 출력
    st.subheader("📄 Raw 자막")
    st.download_button("자막 저장 (TXT)", data=transcript_text.encode("utf-8"), file_name="transcript.txt", mime="text/plain")
    st.text_area("", value=transcript_text, height=560)

st.markdown("---")
st.caption(
    "본 도구는 개인 학습/연구 목적의 자막 보기 용도입니다. 일부 영상은 라이선스/연령/지역 제한으로 처리되지 않을 수 있습니다."
)