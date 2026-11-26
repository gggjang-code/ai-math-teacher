import streamlit as st
import base64
import os
import csv
import datetime
import hashlib
import uuid
import time
import zipfile
from zoneinfo import ZoneInfo
from pathlib import Path
from io import BytesIO
from PIL import Image
import fitz  # PyMuPDF
from google import genai
from google.genai import types
from student_store import StudentStore, StorageError

# --- 1. 기본 경로 및 파일 관리 ---
BASE_DIR = Path(__file__).parent
KEY_FILE = BASE_DIR / ".gemini_key"
PW_FILE = BASE_DIR / ".subscriber_pw"
ADMIN_PIN_FILE = BASE_DIR / ".admin_pin"
QUESTIONS_DIR = BASE_DIR / "student_questions"
QUESTIONS_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = QUESTIONS_DIR / "questions_log.csv"
CONVERSATION_LOG_FILE = QUESTIONS_DIR / "conversation_log.csv"
PROBLEM_INDEX_FILE = QUESTIONS_DIR / "problem_index_v2.csv"

# 1인당 하루 질문 제한 횟수
DAILY_LIMIT = 5

# AI 응답 속도 설정: 원본 사진/PDF 전체를 보내지 않고 문제를 읽을 정도로만 축소합니다.
MAX_IMAGE_SIDE = 1600
GEMINI_TIMEOUT_MS = 60_000
INTERACTION_TIMEOUT_SECONDS = 45
MAX_RESPONSE_TOKENS = 2500
GEMINI_MODEL = "gemini-3.5-flash-lite"

# 기본 비밀번호 초기화 (파일이나 Secrets가 없을 때)
DEFAULT_SUBSCRIBER_PW = ""
DEFAULT_ADMIN_PIN = ""

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

def configure_student_store():
    """Only use remote storage when BOTH server-side secrets are configured."""
    url = get_secret("SUPABASE_URL", "")
    key = get_secret("SUPABASE_SECRET_KEY", "") or get_secret("SUPABASE_SERVICE_ROLE_KEY", "")
    if bool(url) != bool(key):
        raise ValueError("학생 기록 영구 저장 설정이 일부만 입력되었습니다.")
    return StudentStore(url, key) if url and key else None


STUDENT_STORE_CONFIG_ERROR = ""
try:
    STUDENT_STORE = configure_student_store()
except ValueError as storage_config_error:
    STUDENT_STORE = None
    STUDENT_STORE_CONFIG_ERROR = str(storage_config_error)


def save_local_file(file_path, value_str):
    try:
        file_path.write_text(value_str.strip(), encoding="utf-8")
        return True
    except Exception:
        return False

# --- 학생 질문 및 사진 자동 저장 ---
def append_csv_record(path, headers, record):
    """Append a full question/answer record without overwriting existing data."""
    new_file = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(headers)
        writer.writerow(record)


def save_problem_file_once(problem_id, original_bytes, extension):
    """Store one attachment per problem; never save it again on hint turns."""
    if not original_bytes:
        return ""
    extension = extension.lower().lstrip(".")
    if extension not in {"pdf", "png", "jpg", "jpeg"}:
        return ""
    filename = f"{problem_id}.{extension}"
    path = QUESTIONS_DIR / filename
    if not path.exists():
        path.write_bytes(original_bytes)
    return filename


def save_conversation_turn(problem_id, role, content, status="ok"):
    if STUDENT_STORE is not None:
        STUDENT_STORE.save_turn(uuid.uuid4().hex, problem_id, role, content, status)
        return
    now = datetime.datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")
    append_csv_record(
        CONVERSATION_LOG_FILE,
        ["문제ID", "일시", "역할", "내용", "상태"],
        [problem_id, now, role, content, status],
    )


def save_question_index(problem_id, user_question, file_name):
    now = datetime.datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")
    append_csv_record(
        PROBLEM_INDEX_FILE,
        ["일시", "문제사진파일명", "학생질문내용", "문제ID"],
        [now, file_name or "파일없음", user_question, problem_id],
    )


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
if STUDENT_STORE is None:
    st.warning("⚠️ 임시 서버 저장 모드입니다. 서버 재시작 시 학생 질문과 사진이 유실될 수 있습니다. 영구 저장소 연결 전에는 실제 학생에게 배포하지 마세요.")

if "is_authenticated" not in st.session_state:
    st.session_state.is_authenticated = False
if "is_admin" not in st.session_state:
    st.session_state.is_admin = False
if "messages" not in st.session_state:
    st.session_state.messages = []
if "question_count" not in st.session_state:
    st.session_state.question_count = 0
if "problem_image_b64" not in st.session_state:
    st.session_state.problem_image_b64 = None
if "problem_id" not in st.session_state:
    st.session_state.problem_id = None
if "problem_registered" not in st.session_state:
    st.session_state.problem_registered = False
if "uploaded_digest" not in st.session_state:
    st.session_state.uploaded_digest = None
if "uploader_epoch" not in st.session_state:
    st.session_state.uploader_epoch = 0
if "cached_preview" not in st.session_state:
    st.session_state.cached_preview = None

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
                if not current_admin_pin:
                    st.warning("관리자 PIN이 설정되지 않았습니다. Streamlit Secrets에서 ADMIN_PIN을 설정하세요.")
                admin_input = st.text_input("관리자 PIN 번호 입력", type="password")
                if st.button("관리자 로그인", use_container_width=True):
                    if current_admin_pin and admin_input == current_admin_pin:
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
                if STUDENT_STORE is not None:
                    st.success("영구 저장소 연결: 비공개 클라우드 저장")
                    try:
                        remote_problems = STUDENT_STORE.recent_problems(200)
                        st.write(f"☁️ 영구 저장된 문제: **{len(remote_problems)}건** (최근 200건)")
                        if remote_problems:
                            remote_labels = [f"{p['created_at']} · {p['problem_id']} · {p['initial_question'][:45]}"
                                             for p in remote_problems]
                            selected_remote_index = st.selectbox("문제별 전체 대화", range(len(remote_labels)),
                                                                 format_func=lambda i: remote_labels[i])
                            selected_remote = remote_problems[selected_remote_index]
                            saved_turns = STUDENT_STORE.turns_for_problem(selected_remote["problem_id"])
                            for turn in saved_turns:
                                st.markdown(f"**{turn['role']} · {turn['created_at']}**")
                                st.write(turn['content'])
                            if selected_remote.get("file_path"):
                                attachment = STUDENT_STORE.download_attachment(selected_remote["file_path"])
                                ext = selected_remote["file_path"].rsplit(".", 1)[-1].lower()
                                if ext == "pdf":
                                    st.download_button("📄 문제 PDF 다운로드", attachment,
                                                       file_name=selected_remote["problem_id"] + ".pdf",
                                                       mime="application/pdf")
                                else:
                                    st.image(attachment, caption="저장된 문제 사진", use_container_width=True)
                    except (StorageError, ValueError) as e:
                        st.error(f"영구 저장소 조회 실패: {e}")
                else:
                    st.warning("영구 저장소가 연결되지 않았습니다. 아래 목록은 현재 서버의 임시 파일만 보여줍니다.")
                saved_images = sorted(
                    [
                        path
                        for path in QUESTIONS_DIR.iterdir()
                        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
                    ],
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
                saved_pdfs = sorted(QUESTIONS_DIR.glob("*.pdf"), reverse=True)
                st.write(f"📸 수집된 문제 사진: **{len(saved_images)}개**")
                st.write(f"📄 수집된 PDF: **{len(saved_pdfs)}개**")

                today_kst = datetime.datetime.now(ZoneInfo("Asia/Seoul"))
                today_date_text = today_kst.strftime("%Y-%m-%d")
                today_file_prefix = today_kst.strftime("%Y%m%d")
                today_media = [
                    path
                    for path in [*saved_images, *saved_pdfs]
                    if path.name.startswith(today_file_prefix)
                ]
                today_log_count = 0
                
                if LOG_FILE.exists():
                    import pandas as pd
                    try:
                        df = pd.read_csv(LOG_FILE, encoding="utf-8-sig")
                        if "일시" in df.columns:
                            today_mask = df["일시"].astype(str).str.startswith(today_date_text)
                            today_log_count = int(today_mask.sum())
                        st.dataframe(df.tail(5), use_container_width=True)
                        st.download_button(
                            "📥 전체 질문 내역 CSV 다운로드",
                            data=LOG_FILE.read_bytes(),
                            file_name="AI_수학쌤_학생질문_전체내역.csv",
                            mime="text/csv",
                            use_container_width=True,
                        )
                    except Exception:
                        pass

                with st.expander("💾 현재 서버 기록 전체 백업", expanded=False):
                    st.caption("Streamlit 서버에 현재 남아 있는 자료만 백업합니다. 영구 저장은 별도 연결이 필요합니다.")
                    if st.button("기록 ZIP 준비", key="prepare_student_backup", use_container_width=True):
                        memory_file = BytesIO()
                        with zipfile.ZipFile(memory_file, "w", zipfile.ZIP_DEFLATED) as archive:
                            for stored_file in QUESTIONS_DIR.rglob("*"):
                                if stored_file.is_file():
                                    archive.write(stored_file, arcname=stored_file.relative_to(QUESTIONS_DIR).as_posix())
                        st.session_state.student_backup_data = memory_file.getvalue()
                    if st.session_state.get("student_backup_data"):
                        st.download_button("📥 서버 기록 ZIP 다운로드",
                                           data=st.session_state.student_backup_data,
                                           file_name="AI_Math_Teacher_student_questions_backup.zip",
                                           mime="application/zip", use_container_width=True)

                if CONVERSATION_LOG_FILE.exists():
                    with st.expander("💬 학생·AI 대화 전체 기록", expanded=False):
                        try:
                            import pandas as pd
                            conversation_df = pd.read_csv(CONVERSATION_LOG_FILE, encoding="utf-8-sig")
                            st.dataframe(conversation_df.tail(300), use_container_width=True)
                            st.download_button("📥 전체 대화 CSV 다운로드",
                                               data=CONVERSATION_LOG_FILE.read_bytes(),
                                               file_name="AI_수학쌤_전체대화.csv",
                                               mime="text/csv", use_container_width=True)
                        except Exception as e:
                            st.warning(f"대화 기록을 읽을 수 없습니다: {e}")

                if saved_images:
                    with st.expander("🖼️ 학생 문제 사진 갤러리", expanded=False):
                        selected_name = st.selectbox(
                            "크게 볼 사진 선택",
                            options=[path.name for path in saved_images],
                            key="admin_gallery_selected_image",
                        )
                        selected_image = QUESTIONS_DIR / selected_name
                        st.image(selected_image, caption=selected_name, use_container_width=True)
                        st.download_button(
                            "📥 선택한 문제 사진 다운로드",
                            data=selected_image.read_bytes(),
                            file_name=selected_name,
                            mime="image/jpeg",
                            use_container_width=True,
                        )
                else:
                    st.caption("아직 수집된 문제 사진이 없습니다.")

                with st.expander("🧹 오늘 테스트 자료만 초기화", expanded=False):
                    st.write(
                        f"{today_date_text}(한국 시간) 대상: "
                        f"문제 파일 **{len(today_media)}개**, 질문 로그 **{today_log_count}건**"
                    )
                    st.caption("이전 날짜의 자료는 삭제되지 않습니다.")
                    reset_text = st.text_input(
                        "'오늘초기화'를 입력하세요",
                        key="admin_today_reset_confirmation",
                    )
                    if st.button(
                        "오늘 테스트 자료만 초기화",
                        disabled=reset_text.strip() != "오늘초기화",
                        use_container_width=True,
                    ):
                        removed_files = 0
                        for stored_file in today_media:
                            if stored_file.exists() and stored_file.is_file():
                                stored_file.unlink()
                                removed_files += 1

                        removed_rows = 0
                        if LOG_FILE.exists():
                            log_df = pd.read_csv(LOG_FILE, encoding="utf-8-sig")
                            if "일시" in log_df.columns:
                                remove_mask = log_df["일시"].astype(str).str.startswith(today_date_text)
                                removed_rows = int(remove_mask.sum())
                                remaining_df = log_df.loc[~remove_mask]
                                if remaining_df.empty:
                                    LOG_FILE.unlink()
                                else:
                                    remaining_df.to_csv(LOG_FILE, index=False, encoding="utf-8-sig")

                        st.success(
                            f"오늘 테스트 자료만 초기화했습니다. "
                            f"(파일 {removed_files}개, 로그 {removed_rows}건)"
                        )
                        st.rerun()
                
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
    if st.button("새 문제 시작", use_container_width=True):
        st.session_state.messages = []
        st.session_state.problem_image_b64 = None
        st.session_state.problem_id = None
        st.session_state.problem_registered = False
        st.session_state.uploaded_digest = None
        st.session_state.cached_preview = None
        st.session_state.uploader_epoch += 1
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
        
    if not current_subscriber_pw:
        st.error("구독자 인증 비밀번호가 설정되지 않았습니다. 관리자에게 문의하세요.")
    if enter_btn:
        if current_subscriber_pw and user_pw_input.strip() == current_subscriber_pw:
            st.session_state.is_authenticated = True
            st.success("🎉 인증 성공! 환영합니다.")
            st.rerun()
        else:
            st.error("비밀번호가 일치하지 않습니다. 최신 유튜브 영상이나 커뮤니티를 확인해 주세요!")
    
    st.stop()

# --- 6. 인증 완료된 학생용 튜터 화면 ---
remaining_count = max(0, DAILY_LIMIT - st.session_state.question_count)

if not st.session_state.is_admin:
    st.info(
        f"⏳ **오늘 남은 새 문제:** `{remaining_count}회` / `{DAILY_LIMIT}회`  "
        "\n현재 문제의 후속 대화는 횟수가 차감되지 않아요."
    )
else:
    st.success("👑 관리자 모드: 질문 횟수 무제한")

# 기존 대화 내역 출력
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if "image" in msg and msg["image"] is not None:
            st.image(msg["image"], caption="업로드한 문제", use_container_width=True)

# 질문 횟수 소진 여부 확인
if not st.session_state.is_admin and remaining_count <= 0 and not st.session_state.messages:
    st.warning("⚠️ **오늘 사용 가능한 새 문제 5개를 모두 사용했습니다!**\n\n내일 다시 만나요. 오늘 배운 내용을 복습해 보세요! 화이팅! 📚")
    st.stop()

uploaded_file = st.file_uploader("문제 사진 또는 PDF 업로드 (선택 사항)",
                                 type=["png", "jpg", "jpeg", "pdf"],
                                 key=f"problem_upload_{st.session_state.uploader_epoch}")
user_question = st.chat_input("질문이나 풀이 상황을 입력하세요 (예: 1단계 힌트 줘, 어떻게 시작해?)")

# 시스템 프롬프트
SYSTEM_PROMPT = """
당신은 대한민국 최고의 친절하고 유능한 유튜브 'AI 수학쌤'입니다.
학생이 처음 문제를 올렸을 때는 첫 힌트부터 시작하되, 전체 풀이를 명시적으로 요청하면 정답과 검산까지 끝까지 설명하세요.

[답변 원칙]
1. 친절하고 격려하는 말투(~해요, ~해볼까요?)를 사용합니다.
2. 학생이 문제를 올리면 먼저 '어떤 단원의 어떤 개념'을 묻는 문제인지 짚어줍니다.
3. 문제 해결을 위한 첫 걸음인 [1단계 힌트]와 핵심 질문 하나를 던져 학생이 스스로 생각하게 유도합니다.
4. 학생이 "잘 모르겠어", "다음 힌트 줘", "풀어줘"라고 요청하면 그 다음 단계 힌트나 풀이를 단계적으로 제시합니다.
5. 모든 수학 공식이나 기호는 반드시 마크다운 수식 기호인 $...$(인라인) 또는 $$...$$(블록)를 사용하여 학생이 읽기 쉽게 작성하세요.
   - 수식 기호를 백틱(`) 안에 넣지 마세요.
   - 여는 $ 기호와 닫는 $ 기호를 반드시 한 쌍으로 사용하세요.
   - 한국어 설명과 수식을 붙여 쓰지 말고 가독성 있게 작성하세요.
   - 올바른 예: "상수항이 $14$이므로, $x$가 없는 상수항인 $4b^2$은 무엇과 같아야 할까요?"
   - 잘못된 예: "x가없는상수항인`4b^2$"
6. 학생의 대답이 맞는지 짧게 피드백한 뒤, 이미 제시한 힌트를 반복하지 말고 바로 다음 단계로 이어갑니다.
7. 학생이 명시적으로 최종 풀이를 요청하기 전에는 한 번에 전체 정답을 공개하지 않습니다.
8. "다음 힌트", "왜?", "여기까지 했어"같은 짧은 후속 질문도 지금까지의 대화와 연결해 이해합니다.
"""

if user_question:
    if not api_key:
        st.error("⚠️ 선생님의 API Key 설정이 필요합니다. 관리자 메뉴에서 API 키를 확인해 주세요.")
        st.stop()

    upload_bytes = uploaded_file.getvalue() if uploaded_file is not None else None
    upload_hash = hashlib.sha256(upload_bytes).hexdigest() if upload_bytes is not None else None

    # A different attachment starts a new problem, not another hint on the previous one.
    attachment_changed = (
        upload_hash is not None
        and upload_hash != st.session_state.uploaded_digest
        and bool(st.session_state.messages)
    )
    if attachment_changed:
        st.session_state.messages = []
        st.session_state.problem_image_b64 = None
        st.session_state.problem_id = None
        st.session_state.problem_registered = False
        st.session_state.cached_preview = None

    is_new_problem = len(st.session_state.messages) == 0
    if is_new_problem and not st.session_state.is_admin and remaining_count <= 0:
        st.warning("오늘 새 문제 5회를 모두 사용했습니다.")
        st.stop()
    first_attempt = st.session_state.problem_id is None

    client = genai.Client(api_key=api_key,
                          http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS))
    preview = None
    if upload_bytes is not None and (
        upload_hash != st.session_state.uploaded_digest
        or st.session_state.problem_image_b64 is None
    ):
        try:
            if uploaded_file.type == "application/pdf":
                with fitz.open(stream=upload_bytes, filetype="pdf") as doc:
                    if doc.page_count == 0:
                        st.error("내용이 없는 PDF입니다.")
                        st.stop()
                    preview = doc[0].get_pixmap(dpi=105, alpha=False).tobytes("png")
                fast_jpeg = image_to_fast_jpeg(Image.open(BytesIO(preview)))
            else:
                preview = Image.open(BytesIO(upload_bytes)).copy()
                fast_jpeg = image_to_fast_jpeg(preview)
        except Exception:
            st.error("문제 파일을 읽을 수 없습니다. 정상적인 사진 또는 PDF를 올려 주세요.")
            st.stop()
        st.session_state.problem_image_b64 = base64.b64encode(fast_jpeg).decode("utf-8")
        st.session_state.cached_preview = preview
        st.session_state.uploaded_digest = upload_hash

    # Allocate an ID only after a valid image/PDF has been decoded.
    if first_attempt:
        st.session_state.problem_id = "Q-" + uuid.uuid4().hex[:12]
    problem_id = st.session_state.problem_id

    history_text = "\n".join(
        f"[{'학생' if msg['role'] == 'user' else 'AI 수학쌤'}]: {msg['content']}"
        for msg in st.session_state.messages[-24:]
    )
    if is_new_problem:
        context_instruction = (
            "이번은 새로운 문제의 첫 질문입니다. 이전 대화가 있다고 가정하지 마세요. "
            "단원과 조건을 확인하고 첫 번째 힌트를 제공하세요."
        )
    else:
        context_instruction = (
            "같은 문제에 관한 후속 질문입니다. 이미 제시한 힌트를 반복하지 마세요. "
            "학생이 전체 풀이를 요청하면 계산과 최종 정답까지 완료하세요."
        )
    input_payload = [{"type": "text", "text": (
        f"{SYSTEM_PROMPT}\n\n{context_instruction}\n\n"
        f"[이 문제의 대화]\n{history_text or '(없음)'}\n\n"
        f"[현재 학생 질문]: {user_question}"
    )}]
    if st.session_state.problem_image_b64:
        input_payload.append({
            "type": "image",
            "data": st.session_state.problem_image_b64,
            "mime_type": "image/jpeg",
        })

    # Index and attachment are stored exactly once per problem.
    if STUDENT_STORE_CONFIG_ERROR:
        st.error(STUDENT_STORE_CONFIG_ERROR)
        st.stop()
    if not st.session_state.problem_registered:
        try:
            file_name = ""
            if STUDENT_STORE is not None:
                ext = ("pdf" if uploaded_file and uploaded_file.type == "application/pdf"
                       else uploaded_file.name.rsplit(".", 1)[-1].lower() if uploaded_file else None)
                mime = (uploaded_file.type if uploaded_file is not None else None)
                file_name = STUDENT_STORE.save_problem(
                    problem_id, user_question, upload_bytes, ext, mime) or ""
            else:
                if upload_bytes is not None:
                    ext = ("pdf" if uploaded_file.type == "application/pdf"
                           else uploaded_file.name.rsplit(".", 1)[-1])
                    file_name = save_problem_file_once(problem_id, upload_bytes, ext)
                save_question_index(problem_id, user_question, file_name)
            st.session_state.problem_registered = True
        except (StorageError, ValueError, OSError) as e:
            st.error(f"문제 저장 실패: {e}. 개인정보 보호를 위해 질문 전송을 중단했습니다.")
            st.stop()

    with st.chat_message("user"):
        st.markdown(user_question)
        if is_new_problem and st.session_state.cached_preview is not None:
            st.image(st.session_state.cached_preview, caption="업로드한 문제", use_container_width=True)

    try:
        save_conversation_turn(problem_id, "학생", user_question)
    except (StorageError, OSError) as e:
        st.error(f"학생 질문 저장 실패: {e}. AI 전송을 중단했습니다.")
        st.stop()
    st.session_state.messages.append({
        "role": "user",
        "content": user_question,
        "image": st.session_state.cached_preview if is_new_problem else None,
    })

    with st.chat_message("assistant"):
        placeholder = st.empty()
        placeholder.markdown("🔎 문제를 읽고 있어요...")
        full_reply = ""
        try:
            last_ui_update = 0.0
            stream = client.interactions.create(
                model=GEMINI_MODEL,
                input=input_payload,
                stream=True,
                store=False,
                automatic_continuation=False,
                timeout=INTERACTION_TIMEOUT_SECONDS,
                generation_config={
                    "max_output_tokens": MAX_RESPONSE_TOKENS,
                    "temperature": 0.2,
                    "thinking_level": "minimal",
                    "thinking_summaries": "none",
                },
            )
            for event in stream:
                if event.event_type == "step.delta" and event.delta.type == "text":
                    full_reply += event.delta.text
                    now = time.monotonic()
                    if now - last_ui_update >= 0.18:
                        placeholder.markdown(full_reply + "▌")
                        last_ui_update = now

            if not full_reply:
                raise RuntimeError("AI에서 답변 텍스트를 받지 못했습니다.")

            placeholder.markdown(full_reply)
            ai_record_saved = True
            try:
                save_conversation_turn(problem_id, "AI", full_reply)
            except (StorageError, OSError) as e:
                ai_record_saved = False
                st.error(f"AI 답변이 표시되었지만 영구 저장에 실패했습니다: {e}. 대화를 종료하지 말고 관리자에게 알려 주세요.")

            st.session_state.messages.append({
                "role": "assistant",
                "content": full_reply
            })
            if is_new_problem:
                st.session_state.question_count += 1
            if ai_record_saved:
                st.rerun()

        except Exception as e:
            if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
                st.session_state.messages.pop()
            try:
                save_conversation_turn(problem_id, "시스템", "AI 응답 실패", status="error")
            except (StorageError, OSError):
                st.warning("실패 기록 저장도 완료되지 않았습니다.")
            error_text = str(e)
            if "timed out" in error_text.lower() or "timeout" in error_text.lower():
                st.error("응답이 지연되고 있어 45초 후 중단했어요. 잠시 후 다시 시도해 주세요.")
            else:
                st.error(f"오류가 발생했습니다: {e}")
