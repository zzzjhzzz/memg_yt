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
        
        if not text:
            continue
            
        # 중복 텍스트 체크 (대소문자 구분 안함)
        text_lower = text.lower()
        
        # 완전 중복 제거
        if text_lower in seen_texts:
            continue
            
        # 부분 중복 제거 (한 문장이 다른 문장에 포함된 경우)
        is_duplicate = False
        for seen_text in list(seen_texts):
            # 현재 텍스트가 이전 텍스트에 포함되거나
            if text_lower in seen_text or seen_text in text_lower:
                # 더 긴 텍스트를 유지
                if len(text_lower) > len(seen_text):
                    # 기존 것 제거하고 새것 추가
                    seen_texts.discard(seen_text)
                    # 기존 라인도 제거
                    cleaned_lines = [l for l in cleaned_lines if not l.lower().endswith(seen_text)]
                else:
                    # 현재 것이 더 짧으면 스킵
                    is_duplicate = True
                    break
                    
        if not is_duplicate:
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
            next_match = re.match(r'\[(\d+\.?\d*)\]\s*(.*)', lines[j])
            if not next_match:
                j += 1
                continue
                
            next_time = float(next_match.group(1))
            next_text = next_match.group(2).strip()
            
            # 시간이 너무 멀거나, 텍스트가 중복되지 않으면 중단
            if (next_time - current_time) > time_threshold:
                break
                
            # 텍스트가 현재 텍스트의 연장인지 체크
            if (current_text in next_text or 
                next_text in current_text or 
                next_text.startswith(current_text) or
                current_text.startswith(next_text)):
                # 더 긴 텍스트로 업데이트
                if len(next_text) > len(merged_text):
                    merged_text = next_text
                j += 1
            else:
                break
                
        merged_lines.append(f"[{current_time:.1f}] {merged_text}")
        i = j
    
    return '\n'.join(merged_lines)

# 자막 추출 함수들에 이 함수들을 적용
def fetch_via_yta_with_retry_cleaned(video_id: str, langs: List[str], max_retries: int = 3) -> str:
    """중복 제거가 포함된 YTA 자막 추출"""
    # ... 기존 코드 ...
    
    entries = tr.fetch()
    st.success(f"✅ 자막 추출 성공 (YTA): {tr.language}" + (" [자동생성]" if tr.is_generated else " [수동]"))
    
    # 기본 변환
    raw_transcript = "\n".join([f"[{e['start']:.1f}] {e['text']}" for e in entries])
    
    # 중복 제거 및 병합
    cleaned_transcript = clean_duplicate_subtitles(raw_transcript)
    final_transcript = merge_consecutive_subtitles(cleaned_transcript)
    
    return final_transcript
