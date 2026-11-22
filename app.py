import streamlit as st
import base64
import os
import csv
import datetime
from pathlib import Path
from io import BytesIO
from PIL import Image
import fitz  # PyMuPDF
from google import genai
from google.genai import types

# --- 1. 기본 경로 및 파일 관리 ---
BASE_DIR = Path(__file__).parent
KEY_FILE = BASE_DIR / ".gemini_key"
PW_FILE = BASE_DIR / ".subscriber_pw"
ADMIN_PIN_FILE = BASE_DIR / ".admin_pin"
QUESTIONS_DIR = BASE_DIR / "student_questions"
QUESTIONS_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = QUESTIONS_DIR / "questions_log.csv"

# 1인당 하루 질문 제한 횟수
DAILY_LIMIT = 5

# AI 응답 속도 설정: 원본 사진/PDF 전체를 보내지 않고 문제를 읽을 정도로만 축소합니다.
MAX_IMAGE_SIDE = 1600
GEMINI_TIMEOUT_MS = 30_000
MAX_RESPONSE_TOKENS = 700

# 기본 비밀번호 초기화 (파일이나 Secrets가 없을 때)
DEFAULT_SUBSCRIBER_PW = "수학1234"
DEFAULT_ADMIN_PIN = "7777"

def get_secret(key_name, default_val=""):
    """관리자가 화면에서 변경한 파일 우선 확인 후 Streamlit Secrets 확인"""
    if key_name == "ADMIN_PIN" and ADMIN_PIN_FILE.exists():
        val = ADMIN_PIN_FILE.read_text(encoding="utf-8").strip()
        if val:
            return val
    if key_name == "SUBSCRIBER_PW" and PW_FILE.exists():
        val = PW_FILE.read_text(encoding="utf-8").strip()
        if val:
            return val
    if key_name == "GEMINI_API_KEY" and KEY_FILE.exists():
        val = KEY_FILE.read_text(encoding="utf-8").strip()
        if val:
            return val

    try:
        if key_name in st.secrets:
            return str(st.secrets[key_name]).strip()
    except Exception:
        pass

    return os.environ.get(key_name, default_val)

def save_local_file(file_path, value_str):
    try:
        file_path.write_text(value_str.strip(), encoding="utf-8")
        return True
    except Exception:
        return False

# --- 학생 질문 및 사진 자동 저장 ---
def save_student_submission(user_question, image_obj=None, pdf_bytes=None, pdf_name=""):
    now = datetime.datetime.now()
    timestamp_str = now.strftime("%Y%m%d_%H%M%S")
    saved_file_name = ""

    if pdf_bytes is not None:
        saved_file_name = f"{timestamp_str}_{pdf_name}"
        pdf_path = QUESTIONS_DIR / saved_file_name
        pdf_path.write_bytes(pdf_bytes)
    elif image_obj is not None:
        saved_file_name = f"{timestamp_str}_problem.jpg"
        img_path = QUESTIONS_DIR / saved_file_name
        # PNG/RGBA 이미지는 JPEG 저장 전 RGB 변환 필요
        save_obj = image_obj.convert("RGB") if image_obj.mode in ("RGBA", "P", "LA", "CMYK") else image_obj
        save_obj.save(img_path, format="JPEG")

    file_exists = LOG_FILE.exists()
    try:
        with open(LOG_FILE, "a", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["일시", "문제사진파일명", "학생질문내용"])
            writer.writerow([now.strftime("%Y-%m-%d %H:%M:%S"), saved_file_name if saved_file_name else "파일없음", user_question])
    except Exception as e:
        st.error(f"질문 기록 저장 중 오류: {e}")

def image_to_fast_jpeg(image_obj):
    """Gemini에 보낼 이미지를 눈으로 읽기에 충분한 크기의 JPEG로 축소한다."""
    image = image_obj.convert("RGB") if image_obj.mode != "RGB" else image_obj.copy()
    image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.Resampling.LANCZOS)
    buffered = BytesIO()
    image.save(buffered, format="JPEG", quality=82, optimize=True)
    return buffered.getvalue()

# --- 2. 페이지 기본 설정 ---
st.set_page_config(
    page_title="AI 수학쌤 - 1:1 맞춤형 튜터",
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
if "question_count" not in st.session_state:
    st.session_state.question_count = 0

# --- 3. 관리자 모드 감지 (URL에 ?admin=true 또는 세션 로그인 상태) ---
is_admin_param = False
try:
    admin_query = st.query_params.get("admin", "")
    if isinstance(admin_query, list):
        admin_query = admin_query[0] if admin_query else ""
    is_admin_param = str(admin_query).lower() in ["true", "1", "yes"]
except Exception:
    is_admin_param = False

show_admin_menu = is_admin_param or st.session_state.is_admin

# --- 4. 사이드바 구성 ---
with st.sidebar:
    if show_admin_menu:
        st.header("⚙️ 채널 관리 메뉴")
        with st.expander("👑 관리자 설정 (선생님 전용)", expanded=True):
            if not st.session_state.is_admin:
                admin_input = st.text_input("관리자 PIN 번호 입력", type="password")
                if st.button("관리자 로그인", use_container_width=True):
                    if admin_input == current_admin_pin:
                        st.session_state.is_admin = True
                        st.success("관리자로 인증되었습니다!")
                        st.rerun()
                    else:
                        st.error("PIN 번호가 일치하지 않습니다.")
            else:
                st.success("🟢 관리자 로그인 상태 (질문 무제한)")
                
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
                    if st.button("PIN 번호 변경 저장", use_container_width=True):
                        if new_pin.strip():
                            save_local_file(ADMIN_PIN_FILE, new_pin)
                            st.success("관리자 PIN이 성공적으로 변경되었습니다!")
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
    else:
        st.header("📐 AI 수학쌤")

    st.markdown("""
    **💡 이용 팁:**
    - 문제 사진을 찍어 올릴 수 있습니다.
    - 정답을 바로 주지 않고 개념과 힌트로 생각하게 돕습니다.
    - 1인당 하루 최대 5회까지 질문할 수 있습니다.
    """)
    if st.button("대화 내용 초기화", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

# --- 5. 메인 화면 ---
st.title("📐 AI 수학쌤 (AI Math Teacher)")
st.caption("유튜브 [@AI수학쌤] 구독자 전용 1:1 맞춤형 힌트 과외 서비스")

# 🔒 [구독자 인증 화면]
if not st.session_state.is_authenticated:
    st.markdown("---")
    st.info("🔒 **본 서비스는 유튜브 [AI 수학쌤] 채널 구독자 전용 서비스입니다.**")
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
    
    st.stop()

# --- 6. 인증 완료된 학생용 튜터 화면 ---
remaining_count = max(0, DAILY_LIMIT - st.session_state.question_count)

if not st.session_state.is_admin:
    st.info(f"⏳ **오늘 남은 질문 횟수:** `{remaining_count}회` / `{DAILY_LIMIT}회`")
else:
    st.success("👑 관리자 모드: 질문 횟수 무제한")

# 기존 대화 내역 출력
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if "image" in msg and msg["image"] is not None:
            st.image(msg["image"], caption="업로드한 문제", use_container_width=True)

# 질문 횟수 소진 여부 확인
if not st.session_state.is_admin and remaining_count <= 0:
    st.warning("⚠️ **오늘 사용 가능한 질문 횟수(5회)를 모두 사용했습니다!**\n\n내일 다시 만나요. 오늘 배운 내용을 복습해 보세요! 화이팅! 📚")
    st.stop()

uploaded_file = st.file_uploader("문제 사진 또는 PDF 업로드 (선택 사항)", type=["png", "jpg", "jpeg", "pdf"])
user_question = st.chat_input("질문이나 풀이 상황을 입력하세요 (예: 1단계 힌트 줘, 어떻게 시작해?)")

# 시스템 프롬프트
SYSTEM_PROMPT = """
당신은 대한민국 최고의 친절하고 유능한 유튜브 'AI 수학쌤'입니다.
학생이 문제를 올렸을 때 절대로 처음부터 최종 답을 바로 전부 알려주지 마세요.

[답변 원칙]
1. 친절하고 격려하는 말투(~해요, ~해볼까요?)를 사용합니다.
2. 학생이 문제를 올리면 먼저 '어떤 단원의 어떤 개념'을 묻는 문제인지 짚어줍니다.
3. 문제 해결을 위한 첫 걸음인 [1단계 힌트]와 핵심 질문 하나를 던져 학생이 스스로 생각하게 유도합니다.
4. 학생이 "잘 모르겠어", "다음 힌트 줘", "풀어줘"라고 요청하면 그 다음 단계 힌트나 풀이를 단계적으로 제시합니다.
5. 모든 수학 공식이나 기호는 반드시 마크다운 수식 기호인 $...$(인라인) 또는 $$...$$(블록)를 사용하여 학생이 읽기 쉽게 작성하세요.
"""

if user_question:
    if not api_key:
        st.error("⚠️ 선생님의 API Key 설정이 필요합니다. 관리자 메뉴에서 API 키를 확인해 주세요.")
        st.stop()

    # 네트워크/서버 지연으로 무제한 기다리지 않도록 30초 상한을 둡니다.
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
    )

    current_image = None
    current_pdf_bytes = None
    current_pdf_name = ""

    # --- 파일 처리 ---
    parts = [types.Part.from_text(text=f"{SYSTEM_PROMPT}\n\n[사용자 질문]: {user_question}")]

    if uploaded_file is not None:
        file_type = uploaded_file.type

        if file_type == "application/pdf":
            current_pdf_bytes = uploaded_file.read()
            current_pdf_name = uploaded_file.name
            # 학생 화면에서도 첫 페이지만 미리보기하므로 AI에게도 첫 페이지를 축소 이미지로 보냅니다.
            with fitz.open(stream=current_pdf_bytes, filetype="pdf") as pdf_doc:
                page = pdf_doc[0]
                pix = page.get_pixmap(dpi=140, alpha=False)
                pdf_image = Image.open(BytesIO(pix.tobytes("png")))
                parts.append(types.Part.from_bytes(
                    data=image_to_fast_jpeg(pdf_image),
                    mime_type="image/jpeg",
                ))
        else:
            image = Image.open(uploaded_file)
            if image.mode in ("RGBA", "P", "LA", "CMYK"):
                image = image.convert("RGB")
            current_image = image
            parts.append(types.Part.from_bytes(
                data=image_to_fast_jpeg(image),
                mime_type="image/jpeg",
            ))

    save_student_submission(user_question, current_image, current_pdf_bytes, current_pdf_name)

    with st.chat_message("user"):
        st.markdown(user_question)
        if current_image:
            st.image(current_image, caption="업로드한 문제", use_container_width=True)
        elif current_pdf_bytes:
            try:
                pdf_doc = fitz.open(stream=current_pdf_bytes, filetype="pdf")
                page = pdf_doc[0]
                pix = page.get_pixmap(dpi=120)
                img_data = pix.tobytes("png")
                st.image(img_data, caption=f"📄 PDF: {current_pdf_name} ({pdf_doc.page_count}페이지)", use_container_width=True)
            except Exception:
                st.info(f"📄 PDF 업로드됨: {current_pdf_name}")

    st.session_state.messages.append({
        "role": "user",
        "content": user_question,
        "image": current_image
    })

    with st.chat_message("assistant"):
        placeholder = st.empty()
        full_reply = ""
        try:
            stream = client.models.generate_content_stream(
                model="gemini-3.8-flash",
                contents=[types.Content(role="user", parts=parts)],
                config=types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                    max_output_tokens=MAX_RESPONSE_TOKENS,
                    temperature=0.3,
                )
            )
            for chunk in stream:
                if chunk.text:
                    full_reply += chunk.text
                    placeholder.markdown(full_reply + "▌")
            placeholder.markdown(full_reply)

            st.session_state.messages.append({
                "role": "assistant",
                "content": full_reply
            })
            st.session_state.question_count += 1
            st.rerun()

        except Exception as e:
            st.error(f"오류가 발생했습니다: {e}")
