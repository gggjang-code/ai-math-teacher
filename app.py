import streamlit as st
import base64
import os
import csv
import datetime
from pathlib import Path
from io import BytesIO
from PIL import Image
from google import genai

# --- 1. 기본 경로 및 파일 관리 ---
BASE_DIR = Path(__file__).parent
KEY_FILE = BASE_DIR / ".gemini_key"
PW_FILE = BASE_DIR / ".subscriber_pw"
ADMIN_PIN_FILE = BASE_DIR / ".admin_pin"
QUESTIONS_DIR = BASE_DIR / "student_questions"
QUESTIONS_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = QUESTIONS_DIR / "questions_log.csv"

# 기본 비밀번호 초기화 (파일이 없을 때)
DEFAULT_SUBSCRIBER_PW = "수학1234"
DEFAULT_ADMIN_PIN = "7777"  # 관리자용 초기 PIN 번호

def get_secret(key_name, default_val=""):
    """Streamlit Cloud의 secrets 또는 환경변수, 로컬 파일 순으로 읽기"""
    # 1. Streamlit Cloud Secrets
    try:
        if key_name in st.secrets:
            return str(st.secrets[key_name]).strip()
    except Exception:
        pass
    # 2. 로컬 파일
    if key_name == "GEMINI_API_KEY" and KEY_FILE.exists():
        return KEY_FILE.read_text(encoding="utf-8").strip()
    if key_name == "SUBSCRIBER_PW" and PW_FILE.exists():
        return PW_FILE.read_text(encoding="utf-8").strip()
    if key_name == "ADMIN_PIN" and ADMIN_PIN_FILE.exists():
        return ADMIN_PIN_FILE.read_text(encoding="utf-8").strip()
    # 3. 환경변수
    return os.environ.get(key_name, default_val)

def save_local_file(file_path, value_str):
    try:
        file_path.write_text(value_str.strip(), encoding="utf-8")
        return True
    except Exception:
        return False

# --- 학생 질문 및 사진 자동 저장 ---
def save_student_submission(user_question, image_obj=None):
    now = datetime.datetime.now()
    timestamp_str = now.strftime("%Y%m%d_%H%M%S")
    saved_img_name = ""

    if image_obj is not None:
        saved_img_name = f"{timestamp_str}_problem.jpg"
        img_path = QUESTIONS_DIR / saved_img_name
        image_obj.save(img_path, format="JPEG")

    file_exists = LOG_FILE.exists()
    try:
        with open(LOG_FILE, "a", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["일시", "문제사진파일명", "학생질문내용"])
            writer.writerow([now.strftime("%Y-%m-%d %H:%M:%S"), saved_img_name if saved_img_name else "사진없음", user_question])
    except Exception as e:
        st.error(f"질문 기록 저장 중 오류: {e}")

# --- 2. 페이지 기본 설정 ---
st.set_page_config(
    page_title="AI 수학 튜터",
    page_icon="📐",
    layout="centered"
)

# 현재 설정값 로드
api_key = get_secret("GEMINI_API_KEY", "")
current_subscriber_pw = get_secret("SUBSCRIBER_PW", DEFAULT_SUBSCRIBER_PW)
current_admin_pin = get_secret("ADMIN_PIN", DEFAULT_ADMIN_PIN)

# 세션 상태 초기화
if "is_authenticated" not in st.session_state:
    st.session_state.is_authenticated = False
if "is_admin" not in st.session_state:
    st.session_state.is_admin = False
if "messages" not in st.session_state:
    st.session_state.messages = []

# --- 3. 사이드바: 관리자 전용 메뉴 ---
with st.sidebar:
    st.header("⚙️ 채널 관리 메뉴")
    
    with st.expander("👑 관리자 설정 (선생님 전용)", expanded=st.session_state.is_admin):
        if not st.session_state.is_admin:
            admin_input = st.text_input("관리자 PIN 번호 입력", type="password", help="초기 기본 PIN: 7777")
            if st.button("관리자 로그인", use_container_width=True):
                if admin_input == current_admin_pin:
                    st.session_state.is_admin = True
                    st.success("관리자로 인증되었습니다!")
                    st.rerun()
                else:
                    st.error("PIN 번호가 일치하지 않습니다.")
        else:
            st.success("🟢 관리자 로그인 상태")
            
            # [1] 구독자 비밀번호 1초 변경
            st.subheader("🔑 이번 주 구독자 비밀번호")
            new_sub_pw = st.text_input("현재 구독자 비밀번호 변경", value=current_subscriber_pw)
            if st.button("💾 구독자 비밀번호 즉시 변경", use_container_width=True):
                if new_sub_pw.strip():
                    save_local_file(PW_FILE, new_sub_pw)
                    st.success(f"비밀번호가 [{new_sub_pw}]로 변경되었습니다!")
                    st.rerun()
            
            st.markdown("---")
            # [2] 관리자 PIN 변경
            with st.expander("🔒 관리자 PIN 번호 변경"):
                new_pin = st.text_input("새 관리자 PIN 번호", type="password")
                if st.button("PIN 번호 변경", use_container_width=True):
                    if new_pin.strip():
                        save_local_file(ADMIN_PIN_FILE, new_pin)
                        st.success("관리자 PIN이 변경되었습니다!")
                        st.rerun()

            st.markdown("---")
            # [3] Gemini API 키 설정
            with st.expander("🔑 Gemini API Key 설정"):
                new_key = st.text_input("API Key 입력", value=api_key, type="password")
                if st.button("API Key 저장", use_container_width=True):
                    if new_key.strip():
                        save_local_file(KEY_FILE, new_key)
                        st.success("API Key 저장 완료!")
                        st.rerun()

            st.markdown("---")
            # [4] 수집된 질문 통계 및 로그
            st.subheader("📊 콘텐츠 소재 보관함")
            saved_images = list(QUESTIONS_DIR.glob("*.jpg"))
            st.write(f"📸 수집된 문제 사진: **{len(saved_images)}개**")
            
            if LOG_FILE.exists():
                import pandas as pd
                try:
                    df = pd.read_csv(LOG_FILE, encoding="utf-8-sig")
                    st.dataframe(df.tail(5), use_container_width=True)
                except Exception:
                    pass
            
            if st.button("🚪 관리자 로그아웃", use_container_width=True):
                st.session_state.is_admin = False
                st.rerun()

    st.markdown("---")
    st.markdown("""
    **💡 이용 팁:**
    - 문제 사진을 찍어 올릴 수 있습니다.
    - 정답을 바로 주지 않고 개념과 힌트로 생각하게 돕습니다.
    - "다음 힌트 줘", "2단계 알려줘"로 질문해 보세요!
    """)
    if st.button("대화 내용 초기화", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

# --- 4. 메인 화면 ---
st.title("📐 나만의 AI 수학 튜터")
st.caption("유튜브 [AI 수학 선생님] 구독자 전용 1:1 맞춤형 힌트 서비스")

# 🔒 [구독자 인증 화면]: 비밀번호를 모르면 튜터창이 열리지 않음
if not st.session_state.is_authenticated:
    st.markdown("---")
    st.info("🔒 **본 서비스는 유튜브 채널 구독자 전용 서비스입니다.**")
    st.markdown("유튜브 최신 영상 더보기란 또는 커뮤니티 탭에 공지된 **이번 주 구독자 비밀번호**를 입력해 주세요.")
    
    col_pw, col_btn = st.columns([3, 1])
    with col_pw:
        user_pw_input = st.text_input("구독자 비밀번호", type="password", placeholder="비밀번호를 입력하세요", label_visibility="collapsed")
    with col_btn:
        enter_btn = st.button("입장하기 🚀", use_container_width=True)
        
    if enter_btn:
        if user_pw_input.strip() == current_subscriber_pw:
            st.session_state.is_authenticated = True
            st.success("🎉 인증 성공! 환영합니다.")
            st.rerun()
        else:
            st.error("비밀번호가 일치하지 않습니다. 최신 유튜브 영상이나 커뮤니티를 확인해 주세요!")
    
    st.stop()  # 인증되지 않으면 아래 질문창이 아예 실행되지 않음

# --- 5. 인증 완료된 학생용 튜터 화면 ---
st.success("🔓 구독자 인증 완료! 질문이나 문제 사진을 올려보세요.")

# 시스템 프롬프트
SYSTEM_PROMPT = """
당신은 대한민국 최고의 친절하고 유능한 'AI 수학 선생님(수학 튜터)'입니다.
학생이 문제를 올렸을 때 절대로 처음부터 최종 답을 바로 전부 알려주지 마세요.

[답변 원칙]
1. 친절하고 격려하는 말투(~해요, ~해볼까요?)를 사용합니다.
2. 학생이 문제를 올리면 먼저 '어떤 단원의 어떤 개념'을 묻는 문제인지 짚어줍니다.
3. 문제 해결을 위한 첫 걸음인 [1단계 힌트]와 핵심 질문 하나를 던져 학생이 스스로 생각하게 유도합니다.
4. 학생이 "잘 모르겠어", "다음 힌트 줘", "풀어줘"라고 요청하면 그 다음 단계 힌트나 풀이를 단계적으로 제시합니다.
5. 모든 수학 공식이나 기호는 반드시 마크다운 수식 기호인 $...$(인라인) 또는 $$...$$(블록)를 사용하여 학생이 읽기 쉽게 작성하세요.
"""

# 기존 대화 내역 출력
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if "image" in msg and msg["image"] is not None:
            st.image(msg["image"], caption="업로드한 문제", use_container_width=True)

# 학생 입력창
uploaded_file = st.file_uploader("문제 사진 업로드 (선택 사항)", type=["png", "jpg", "jpeg"])
user_question = st.chat_input("질문이나 풀이 상황을 입력하세요 (예: 1단계 힌트 줘, 어떻게 시작해?)")

if user_question:
    if not api_key:
        st.error("⚠️ 선생님의 API Key 설정이 필요합니다. 관리자 메뉴에서 API 키를 확인해 주세요.")
        st.stop()

    client = genai.Client(api_key=api_key)

    current_image = None
    input_payload = [{"type": "text", "text": f"{SYSTEM_PROMPT}\n\n[사용자 질문]: {user_question}"}]

    if uploaded_file is not None:
        image = Image.open(uploaded_file)
        current_image = image
        
        buffered = BytesIO()
        image.save(buffered, format="JPEG")
        img_b64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
        
        input_payload.append({
            "type": "image",
            "data": img_b64,
            "mime_type": "image/jpeg"
        })

    # 학생이 올린 질문 및 사진 자동 저장
    save_student_submission(user_question, current_image)

    # 화면 표시
    with st.chat_message("user"):
        st.markdown(user_question)
        if current_image:
            st.image(current_image, caption="업로드한 문제", use_container_width=True)

    st.session_state.messages.append({
        "role": "user",
        "content": user_question,
        "image": current_image
    })

    # AI 답변 생성
    with st.chat_message("assistant"):
        with st.spinner("선생님이 문제를 분석하고 있어요... 💭"):
            try:
                response = client.interactions.create(
                    model="gemini-4-hydrogen",
                    input=input_payload
                )
                ai_reply = response.output_text
                st.markdown(ai_reply)
                
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": ai_reply
                })
            except Exception as e:
                st.error(f"오류가 발생했습니다: {e}")
