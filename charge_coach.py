import re
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import altair as alt
import pandas as pd
import plotly.express as px
import streamlit as st

import google.generativeai as genai
from google.generativeai.types import GenerationConfig, HarmCategory, HarmBlockThreshold

import gspread
from gspread.exceptions import WorksheetNotFound
from google.oauth2.service_account import Credentials


# =========================================================
# 1. 系統設定
# =========================================================
st.set_page_config(
    page_title="溫充電教練",
    layout="wide",
    page_icon="☕",
)

APP_TITLE = "☕ 溫充電教練"
DEFAULT_MODEL_NAME = "gemini-2.5-flash-lite"
MODEL_OPTIONS = ["gemini-2.5-flash-lite", "gemini-2.5-flash"]

TAIPEI_TZ = timezone(timedelta(hours=8))

RECENT_TURNS_FOR_CHAT = 10
MEMORY_UPDATE_EVERY_MESSAGES = 8
MAX_MEMORY_OUTPUT_TOKENS = 700
MAX_CHAT_OUTPUT_TOKENS = 500
MAX_STRENGTHS_OUTPUT_TOKENS = 500

KEY_COOLDOWN_SECONDS = 60
RETRY_WAIT_SECONDS = 60

SPREADSHEET_NAME = "2025創傷知情研習數據"
WORKSHEET_NAME = "Charge Coach"
SHEET_CELL_CHAR_LIMIT = 45000
SHEET_HEADERS = [
    "登入時間",
    "登出時間",
    "學員編號",
    "使用分鐘數",
    "累積使用次數",
    "完整對話紀錄",
]

VIA_KEYS = ["智慧與知識", "勇氣", "人道", "正義", "節制", "超越"]

SAFETY_SETTINGS = {
    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
}


# =========================================================
# 2. 基礎工具
# =========================================================
def utc_now():
    return datetime.now(timezone.utc)


def now_tw():
    return utc_now().astimezone(TAIPEI_TZ)


def format_tw(dt):
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S")


def safe_secret_get(section, default=None):
    try:
        return st.secrets.get(section, default)
    except Exception:
        return default


def parse_api_keys(raw_value):
    if not raw_value:
        return []

    if isinstance(raw_value, list):
        return [str(k).strip() for k in raw_value if str(k).strip()]

    return [k.strip() for k in re.split(r"[\n,]+", str(raw_value)) if k.strip()]


def sanitize_filename(text):
    text = str(text).strip() or "teacher"
    return re.sub(r'[\\/:*?"<>|]+', "_", text)


def clamp_score(value, low=1, high=10):
    try:
        num = float(value)
    except Exception:
        return None
    return int(max(low, min(high, round(num))))


def truncate_cell_text(text, limit=SHEET_CELL_CHAR_LIMIT):
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n...[內容過長，已截斷；完整紀錄請以使用者下載 JSON 為準]"


# =========================================================
# 3. Session State
# =========================================================
def default_state():
    return {
        "app_phase": "login",
        "history": [],
        "energy_log": [],
        "strengths_data": {},
        "user_nickname": "",
        "start_time": utc_now(),
        "logout_time": None,
        "session_id": str(uuid.uuid4()),
        "api_keys_list": [],
        "current_key_index": 0,
        "key_cooldowns": {},
        "valid_model_name": DEFAULT_MODEL_NAME,
        "system_prompt": "",
        "system_prompt_fallback": False,
        "memory_summary": "",
        "last_memory_update_len": 0,
        "privacy_consent": False,
        "sheets_row_number": None,
        "usage_count": None,
        "save_status": "",
    }


def init_session_state():
    for key, value in default_state().items():
        if key not in st.session_state:
            st.session_state[key] = value


def reset_app(clear_keys=True):
    managed_keys = list(default_state().keys())
    widget_keys = [
        "student_api_key_1",
        "student_api_key_2",
        "privacy_consent_checkbox",
        "new_login",
    ]

    for key in managed_keys:
        if key in st.session_state:
            del st.session_state[key]

    if clear_keys:
        for key in widget_keys:
            if key in st.session_state:
                del st.session_state[key]

    init_session_state()


init_session_state()


# =========================================================
# 4. Google Sheets 後台收集
# =========================================================
def has_google_sheets_config():
    try:
        return "gcp_service_account" in st.secrets
    except Exception:
        return False


@st.cache_resource(show_spinner=False)
def get_chargecoach_worksheet():
    sheets_cfg = safe_secret_get("google_sheets", {}) or {}
    spreadsheet_name = sheets_cfg.get("spreadsheet_name", SPREADSHEET_NAME)
    worksheet_name = sheets_cfg.get("worksheet_name", WORKSHEET_NAME)

    service_account_info = dict(st.secrets["gcp_service_account"])
    if "private_key" in service_account_info:
        service_account_info["private_key"] = service_account_info["private_key"].replace("\\n", "\n")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    client = gspread.authorize(credentials)
    spreadsheet = client.open(spreadsheet_name)

    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=worksheet_name,
            rows="2000",
            cols=str(len(SHEET_HEADERS)),
        )

    header = worksheet.row_values(1)
    if header != SHEET_HEADERS:
        worksheet.update("A1:F1", [SHEET_HEADERS])

    return worksheet


def build_export_data():
    return {
        "app": "Warm Charge Coach",
        "session_id": st.session_state.session_id,
        "nickname": st.session_state.user_nickname,
        "login_time": format_tw(st.session_state.start_time),
        "logout_time": format_tw(st.session_state.logout_time),
        "exported_at": format_tw(utc_now()),
        "energy_log": st.session_state.energy_log,
        "strengths_data": st.session_state.strengths_data,
        "memory_summary": st.session_state.memory_summary,
        "history": st.session_state.history,
    }


def format_full_conversation_record():
    lines = []
    lines.append(f"session_id: {st.session_state.session_id}")
    lines.append(f"登入時間: {format_tw(st.session_state.start_time)}")
    lines.append(f"登出時間: {format_tw(st.session_state.logout_time)}")
    lines.append(f"學員編號: {st.session_state.user_nickname}")
    lines.append("")

    if st.session_state.energy_log:
        energy_text = " -> ".join(
            [f"{item.get('階段', '')}:{item.get('分數', '')}分" for item in st.session_state.energy_log]
        )
        lines.append(f"【能量走勢】{energy_text}")

    if st.session_state.strengths_data:
        lines.append("【VIA 六大美德】")
        lines.append(json.dumps(st.session_state.strengths_data, ensure_ascii=False))

    if st.session_state.memory_summary:
        lines.append("【壓縮摘要】")
        lines.append(st.session_state.memory_summary)

    lines.append("")
    lines.append("【完整對話紀錄】")
    for msg in st.session_state.history:
        role = "老師" if msg.get("role") == "user" else "教練"
        content = str(msg.get("content", "")).strip()
        if content:
            lines.append(f"[{role}] {content}")

    return "\n".join(lines)


def find_existing_session_row(worksheet):
    if st.session_state.sheets_row_number:
        return st.session_state.sheets_row_number

    try:
        login_values = worksheet.col_values(1)
        user_values = worksheet.col_values(3)
        login_time = format_tw(st.session_state.start_time)
        user_id = str(st.session_state.user_nickname)

        for idx in range(2, len(login_values) + 1):
            login_match = idx <= len(login_values) and login_values[idx - 1] == login_time
            user_match = idx <= len(user_values) and str(user_values[idx - 1]) == user_id
            if login_match and user_match:
                return idx
    except Exception:
        return None

    return None


def get_usage_count_for_user(worksheet, user_id):
    if st.session_state.usage_count:
        return st.session_state.usage_count

    try:
        existing_ids = worksheet.col_values(3)
        completed_count = sum(1 for value in existing_ids[1:] if str(value) == str(user_id))
        st.session_state.usage_count = completed_count + 1
    except Exception:
        st.session_state.usage_count = 1

    return st.session_state.usage_count


def save_session_to_google_sheets(final=False):
    if not has_google_sheets_config():
        st.session_state.save_status = "Google Sheets 尚未設定，後台收集已略過。"
        return False

    if not st.session_state.user_nickname:
        return False

    try:
        worksheet = get_chargecoach_worksheet()

        if final:
            st.session_state.logout_time = utc_now()

        usage_minutes = round((utc_now() - st.session_state.start_time).total_seconds() / 60, 2)
        usage_count = get_usage_count_for_user(worksheet, st.session_state.user_nickname)

        data_row = [
            format_tw(st.session_state.start_time),
            format_tw(st.session_state.logout_time) if st.session_state.logout_time else "",
            st.session_state.user_nickname,
            usage_minutes,
            usage_count,
            truncate_cell_text(format_full_conversation_record()),
        ]

        row_number = find_existing_session_row(worksheet)

        if row_number and row_number > 1:
            worksheet.update(f"A{row_number}:F{row_number}", [data_row])
            st.session_state.sheets_row_number = row_number
        else:
            worksheet.append_row(data_row, value_input_option="RAW")
            st.session_state.sheets_row_number = len(worksheet.col_values(1))

        st.session_state.save_status = "Google Sheets 已儲存。"
        return True

    except Exception as e:
        st.session_state.save_status = f"Google Sheets 儲存失敗：{e}"
        return False


# =========================================================
# 5. Gemini 呼叫與 API Key 輪替
# =========================================================
def get_current_api_key():
    if not st.session_state.api_keys_list:
        raise RuntimeError("尚未輸入 Gemini API Key。請在左側欄貼上自己的 API Key。")

    if st.session_state.current_key_index >= len(st.session_state.api_keys_list):
        st.session_state.current_key_index = 0

    return st.session_state.api_keys_list[st.session_state.current_key_index]


def mark_key_cooldown(index, seconds=KEY_COOLDOWN_SECONDS):
    try:
        key = st.session_state.api_keys_list[index]
        st.session_state.key_cooldowns[key] = time.time() + seconds
    except Exception:
        pass


def next_available_key_index(exclude=None):
    exclude = exclude or set()
    keys = st.session_state.api_keys_list
    now = time.time()

    for step in range(len(keys)):
        idx = (st.session_state.current_key_index + step) % len(keys)
        if idx in exclude:
            continue
        key = keys[idx]
        if st.session_state.key_cooldowns.get(key, 0) <= now:
            return idx

    return None


def seconds_until_next_key():
    keys = st.session_state.api_keys_list
    if not keys:
        return RETRY_WAIT_SECONDS

    now = time.time()
    waits = [
        max(0, st.session_state.key_cooldowns.get(key, 0) - now)
        for key in keys
    ]
    waits = [w for w in waits if w > 0]

    if not waits:
        return 5

    return int(min(max(min(waits), 5), RETRY_WAIT_SECONDS))


def is_quota_error(err):
    text = str(err).lower()
    return any(x in text for x in ["429", "quota", "rate limit", "resource_exhausted"])


def is_api_key_error(err):
    text = str(err).lower()
    return any(x in text for x in [
        "api key not valid",
        "invalid api key",
        "api_key_invalid",
        "permission_denied",
        "403",
    ])


def build_model(temperature, max_output_tokens, system_instruction=None):
    generation_config = GenerationConfig(
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )

    if system_instruction:
        try:
            st.session_state.system_prompt_fallback = False
            return genai.GenerativeModel(
                model_name=st.session_state.valid_model_name,
                system_instruction=system_instruction,
                generation_config=generation_config,
                safety_settings=SAFETY_SETTINGS,
            )
        except TypeError:
            st.session_state.system_prompt_fallback = True

    return genai.GenerativeModel(
        model_name=st.session_state.valid_model_name,
        generation_config=generation_config,
        safety_settings=SAFETY_SETTINGS,
    )


def extract_response_text(resp):
    try:
        text = resp.text
        if text and text.strip():
            return text.strip()
    except Exception:
        pass

    try:
        candidates = getattr(resp, "candidates", []) or []
        parts = candidates[0].content.parts
        text = "\n".join(getattr(p, "text", "") for p in parts if getattr(p, "text", ""))
        if text.strip():
            return text.strip()
    except Exception:
        pass

    feedback = getattr(resp, "prompt_feedback", "")
    raise RuntimeError(f"模型沒有回傳可用文字。{feedback}")


def call_gemini_with_failover(task_fn, purpose="AI生成"):
    if not st.session_state.api_keys_list:
        raise RuntimeError("尚未輸入 Gemini API Key。")

    waited_once = False
    last_error = None

    while True:
        attempted = set()

        for _ in range(len(st.session_state.api_keys_list)):
            idx = next_available_key_index(exclude=attempted)
            if idx is None:
                break

            attempted.add(idx)
            active_key = st.session_state.api_keys_list[idx]

            try:
                genai.configure(api_key=active_key)
                result = task_fn(active_key)
                st.session_state.current_key_index = idx
                return result

            except Exception as e:
                last_error = e

                if is_quota_error(e):
                    mark_key_cooldown(idx, KEY_COOLDOWN_SECONDS)
                    st.toast(f"{purpose}：第 {idx + 1} 組 Key 暫時滿載，切換中。", icon="🔄")
                    st.session_state.current_key_index = (idx + 1) % len(st.session_state.api_keys_list)
                    continue

                if is_api_key_error(e):
                    mark_key_cooldown(idx, 3600)
                    st.toast(f"{purpose}：第 {idx + 1} 組 Key 可能無效，嘗試下一組。", icon="⚠️")
                    st.session_state.current_key_index = (idx + 1) % len(st.session_state.api_keys_list)
                    continue

                raise e

        if last_error and is_api_key_error(last_error) and not is_quota_error(last_error):
            raise last_error

        if not waited_once:
            waited_once = True
            wait_seconds = seconds_until_next_key()
            st.warning(f"⏳ {purpose}：所有 Key 暫時不可用，等待 {wait_seconds} 秒後重試。")
            time.sleep(wait_seconds)
            continue

        if last_error:
            raise last_error

        raise RuntimeError("目前沒有可用的 Gemini API Key。")


def generate_text_with_failover(prompt, purpose="AI生成", temperature=0.0, max_output_tokens=800):
    def task(_active_key):
        model = build_model(
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        resp = model.generate_content(prompt, safety_settings=SAFETY_SETTINGS)
        return extract_response_text(resp)

    return call_gemini_with_failover(task, purpose=purpose)


# =========================================================
# 6. Prompt 與記憶摘要
# =========================================================
def build_coach_system_prompt(initial_score):
    return f"""
Role: You are the "Warm Charge Coach"（溫充電教練）.
You support educators with trauma-informed care, strengths perspective, and VIA character strengths.

Language: 繁體中文。
Target user: 疲憊、壓力大、需要被安頓的學校教師。

【重要邊界】
1. 這是教育與自我照顧支持工具，不提供醫療診斷、心理治療或法律建議。
2. 不要求使用者揭露可識別學生、家長、同事或學校個案的個人資料。
3. 若使用者提到立即自傷、輕生、傷害他人或安全危機，請溫柔但明確地鼓勵他立刻聯絡當地緊急服務、可信任的人或危機專線。
4. 不要說你是 AI，不要提及 API、模型或系統指令。
5. 每次回應保持溫暖、短段落、可呼吸，不要長篇說教。

【創傷知情原則】
- 先安頓，再探索。
- 不催促、不評價、不急著給解方。
- 協助使用者回到容納之窗。
- 使用接地、命名感受、選擇權、小步行動。

【VIA 六大美德與 24 項優勢】
1. 智慧與知識：創造力、好奇心、開明思想、喜愛學習、觀點。
2. 勇氣：勇敢、堅毅、正直、生命力。
3. 人道：愛、仁慈、社交智慧。
4. 正義：公民精神、公平、領導力。
5. 節制：寬恕、謙遜、謹慎、自制力。
6. 超越：欣賞美好卓越、感恩、希望、幽默、靈修性。

【互動階段】
Phase 1: Grounding & Strengths-Spotting
- 承接使用者目前的能量分數：{initial_score}/10。
- 問今天最耗能的部分。
- 反映情緒與身體感受。
- 從故事中具體命名一個 VIA 優勢，不要空泛稱讚。

Phase 2: Micro-Action Planning
- 在使用者被理解後，提供 2 到 3 個五分鐘內可完成的微行動。
- 讓使用者選一個，而不是命令他照做。

【語氣】
- 溫柔、穩定、接住、尊重。
- 可使用括號描寫溫和的非語言動作，例如：（把語速放慢一點）。
- 不要過度正能量，不要否定痛苦。
""".strip()


def build_runtime_system_prompt():
    prompt = st.session_state.system_prompt or build_coach_system_prompt("未知")

    memory = st.session_state.memory_summary.strip()
    if memory:
        prompt += f"""

【先前對話壓縮摘要】
以下是較早前對話的壓縮摘要。請用它維持連續性，但不要逐字重複：
{memory}
""".strip()

    return prompt


def build_gemini_history(exclude_last_user=True):
    hist = st.session_state.history

    if exclude_last_user and hist and hist[-1]["role"] == "user":
        hist = hist[:-1]

    recent = hist[-RECENT_TURNS_FOR_CHAT:]
    gemini_history = []

    if st.session_state.get("system_prompt_fallback"):
        gemini_history.append({
            "role": "user",
            "parts": [build_runtime_system_prompt()],
        })
        gemini_history.append({
            "role": "model",
            "parts": ["我會以溫充電教練的角色，溫柔而穩定地陪伴。"],
        })

    for msg in recent:
        role = "model" if msg.get("role") == "assistant" else "user"
        content = str(msg.get("content", "")).strip()
        if content:
            gemini_history.append({"role": role, "parts": [content]})

    return gemini_history


def send_message_safely(user_text):
    def task(_active_key):
        model = build_model(
            temperature=0.5,
            max_output_tokens=MAX_CHAT_OUTPUT_TOKENS,
            system_instruction=build_runtime_system_prompt(),
        )
        chat_session = model.start_chat(history=build_gemini_history(exclude_last_user=True))
        resp = chat_session.send_message(user_text)
        return extract_response_text(resp)

    return call_gemini_with_failover(task, purpose="教練回應")


def format_history_for_prompt(messages):
    lines = []
    for msg in messages:
        role = "老師" if msg.get("role") == "user" else "教練"
        content = str(msg.get("content", "")).strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def maybe_update_memory(force=False):
    hist = st.session_state.history

    if len(hist) < RECENT_TURNS_FOR_CHAT + MEMORY_UPDATE_EVERY_MESSAGES and not force:
        return

    summary_until = max(0, len(hist) - RECENT_TURNS_FOR_CHAT)
    last_len = int(st.session_state.get("last_memory_update_len", 0))

    if summary_until <= last_len and not force:
        return

    delta_hist = hist[last_len:summary_until] if not force else hist[:-RECENT_TURNS_FOR_CHAT]

    if not delta_hist:
        return

    prompt = f"""
請把以下「老師與溫充電教練」的對話壓縮成後續可用的溫柔記憶摘要。

請保留：
1. 老師目前主要壓力來源；
2. 情緒與身體狀態；
3. 已經展現的 VIA 優勢；
4. 曾提過可行或不可行的自我照顧方式；
5. 後續陪伴時需要避免踩到的點。

請不要評分。
請不要寫成督導報告。
請用 350 字以內繁體中文摘要。

【既有摘要】
{st.session_state.memory_summary}

【新增對話】
{format_history_for_prompt(delta_hist)}
""".strip()

    try:
        summary = generate_text_with_failover(
            prompt,
            purpose="壓縮對話記憶",
            temperature=0.0,
            max_output_tokens=MAX_MEMORY_OUTPUT_TOKENS,
        )
        st.session_state.memory_summary = summary.strip()
        st.session_state.last_memory_update_len = summary_until
    except Exception:
        pass


# =========================================================
# 7. VIA 優勢分析
# =========================================================
def extract_json_object(text):
    text = str(text).strip()
    text = text.replace("```json", "").replace("```", "").strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        raise ValueError("找不到 JSON 物件。")

    return json.loads(match.group(0))


def parse_strengths_result(text):
    data = extract_json_object(text)
    parsed = {}

    for key in VIA_KEYS:
        raw_val = data.get(key)

        if isinstance(raw_val, str):
            num_match = re.search(r"\d+(\.\d+)?", raw_val)
            raw_val = num_match.group(0) if num_match else None

        score = clamp_score(raw_val, 1, 10)
        if score is None:
            raise ValueError(f"{key} 分數無法解析。")

        parsed[key] = score

    return parsed


def build_strengths_analysis_text():
    recent = st.session_state.history[-40:]
    parts = []

    if st.session_state.memory_summary:
        parts.append(f"【摘要】\n{st.session_state.memory_summary}")

    parts.append(f"【近期對話】\n{format_history_for_prompt(recent)}")
    return "\n\n".join(parts)


def analyze_strengths():
    prompt = f"""
你是一位正向心理學與教師支持工作專家。請根據以下老師與教練的對話，
評估老師在面對教學挑戰、壓力與情緒調適過程中展現出的 VIA 六大美德強度。

請只輸出純 JSON，不要 Markdown，不要說明文字。

六個鍵必須完全使用以下名稱：
{", ".join(VIA_KEYS)}

每個值請使用 1 到 10 的整數。
10 表示非常明顯展現，1 表示幾乎沒有觀察到。

格式範例：
{{"智慧與知識": 8, "勇氣": 7, "人道": 9, "正義": 6, "節制": 7, "超越": 8}}

【待分析資料】
{build_strengths_analysis_text()}
""".strip()

    try:
        result_text = generate_text_with_failover(
            prompt,
            purpose="VIA 優勢分析",
            temperature=0.0,
            max_output_tokens=MAX_STRENGTHS_OUTPUT_TOKENS,
        )
        return parse_strengths_result(result_text)
    except Exception as e:
        st.warning(f"VIA 優勢分析暫時失敗：{e}")
        return {}


# =========================================================
# 8. 危機偵測
# =========================================================
CRISIS_KEYWORDS = [
    "想死", "自殺", "輕生", "不想活", "活不下去", "結束生命",
    "傷害自己", "自殘", "割腕", "跳樓", "吃藥死", "殺了自己",
    "傷害別人", "殺人", "想殺", "報復",
]

CRISIS_MESSAGE = """
（我先把語速放慢，也把這件事看得很重要。）

我聽見你現在可能已經不只是累，而是有安全上的風險了。這一刻請不要一個人撐著。

如果你有立即傷害自己或他人的可能，請現在就聯絡當地緊急服務，或請身邊可信任的人陪你一起處理。若你在台灣，可以撥打 119 或 110；也可以聯絡 1925 安心專線、1995 生命線、1980 張老師專線。

你不需要把所有事情一次說清楚。現在最重要的是：先讓一個真人知道你正在危險或快撐不住。
""".strip()


def detect_crisis(text):
    text = str(text)
    return any(keyword in text for keyword in CRISIS_KEYWORDS)


# =========================================================
# 9. UI 小元件
# =========================================================
def render_window_reference():
    st.markdown("""
**💡 容納之窗參考指標**

- **8~10 分（紅區）**：過度激發，例如焦慮、煩躁、恐慌、想發脾氣。
- **4~7 分（綠區）**：容納之窗，例如平靜、安全、能自我調節。
- **0~3 分（藍區）**：過低激發，例如疲憊、無力、麻木、大腦當機。
""")


def add_energy_score(suffix, score):
    today_str = now_tw().strftime("%m/%d")
    st.session_state.energy_log.append({
        "階段": f"{today_str} {suffix}",
        "分數": int(score),
        "排序": len(st.session_state.energy_log) + 1,
        "時間": now_tw().isoformat(),
    })


def get_state_message(score):
    if score >= 8:
        return "看到您剛剛標記的狀態落在比較焦慮、煩躁的紅區。辛苦您了，現在的神經系統可能很緊繃。"
    if score <= 3:
        return "看到您剛剛標記的狀態落在比較疲憊、無力的藍區。辛苦您了，今天可能已經耗掉很多心力。"
    return "看到您剛剛標記的狀態落在相對平穩的綠區，這是一個可以慢慢整理自己的起點。"


# =========================================================
# 10. 側邊欄
# =========================================================
st.sidebar.title("⚙️ 系統設定")

st.sidebar.subheader("🔑 Gemini API Key")
st.sidebar.caption("請使用者貼上自己的 Gemini API Key。Key 只會暫存在本次 session，不會寫進下載檔或 Google Sheets。")

student_key_1 = st.sidebar.text_input(
    "Gemini API Key 1",
    type="password",
    key="student_api_key_1",
)

student_key_2 = st.sidebar.text_input(
    "Gemini API Key 2（選填）",
    type="password",
    key="student_api_key_2",
)

st.session_state.api_keys_list = parse_api_keys([student_key_1, student_key_2])
has_api_key = len(st.session_state.api_keys_list) > 0

if has_api_key:
    if st.session_state.current_key_index >= len(st.session_state.api_keys_list):
        st.session_state.current_key_index = 0
    st.sidebar.success(f"已輸入 {len(st.session_state.api_keys_list)} 組 API Key。")
    st.sidebar.caption(f"目前使用：第 {st.session_state.current_key_index + 1} / {len(st.session_state.api_keys_list)} 組")
else:
    st.sidebar.warning("請先輸入至少 1 組 Gemini API Key。")

st.sidebar.selectbox(
    "🤖 AI 模型",
    MODEL_OPTIONS,
    index=MODEL_OPTIONS.index(st.session_state.valid_model_name)
    if st.session_state.valid_model_name in MODEL_OPTIONS else 0,
    key="valid_model_name",
)

st.sidebar.markdown("---")

if has_google_sheets_config():
    st.sidebar.success("Google Sheets 後台收集：已設定")
else:
    st.sidebar.warning("Google Sheets 後台收集：未設定")

if st.session_state.save_status:
    st.sidebar.caption(st.session_state.save_status)

if st.session_state.user_nickname:
    st.sidebar.markdown("---")
    st.sidebar.write(f"👤 學員編號：**{st.session_state.user_nickname}**")

    if st.sidebar.button("🔄 重新開始 / 切換使用者"):
        reset_app(clear_keys=True)
        st.rerun()


# =========================================================
# 11. 主畫面
# =========================================================
st.title(APP_TITLE)

# ------------------------------
# 階段 1：登入
# ------------------------------
if st.session_state.app_phase == "login":
    st.markdown("### 先顧好自己，AI 才能幫上忙。")

    if not has_api_key:
        st.warning("⚠️ 請先在左側邊欄輸入自己的 Gemini API Key。")

    if not has_google_sheets_config():
        st.warning("⚠️ 後台 Google Sheets 尚未設定。App 仍可使用，但不會自動寫入研習數據。")

    with st.expander("資料使用與隱私提醒", expanded=True):
        st.markdown("""
本工具會記錄您的學員編號、登入時間、登出時間、使用分鐘數、累積使用次數與完整對話紀錄，並寫入研習後台 Google Sheets。

請避免輸入可識別學生、家長、同事或學校個案的個人資料。  
若內容涉及立即安全風險，請優先聯絡真人支持與緊急資源。
""")

        st.checkbox(
            "我了解上述提醒，並同意在此工具中進行自我照顧練習。",
            key="privacy_consent_checkbox",
        )
        st.session_state.privacy_consent = bool(st.session_state.privacy_consent_checkbox)

    tab1, tab2 = st.tabs(["✨ 新的充電", "📂 載入過去紀錄"])

    can_enter = has_api_key and st.session_state.privacy_consent

    with tab1:
        nickname_input = st.text_input(
            "請輸入學員編號：",
            placeholder="例如：A001 或 大業國小王老師",
            disabled=not can_enter,
            key="new_login",
        )

        if st.button("🚀 進入充電站", type="primary", disabled=not can_enter):
            if nickname_input.strip():
                st.session_state.user_nickname = nickname_input.strip()
                st.session_state.start_time = utc_now()
                st.session_state.logout_time = None
                st.session_state.session_id = str(uuid.uuid4())
                st.session_state.sheets_row_number = None
                st.session_state.usage_count = None
                save_session_to_google_sheets(final=False)
                st.session_state.app_phase = "initial_checkin"
                st.rerun()
            else:
                st.error("❌ 學員編號不能為空。")

    with tab2:
        st.info("上傳先前下載的「充電記憶 JSON」，教練會延續能量走勢與摘要記憶。")

        uploaded_file = st.file_uploader(
            "上傳您的充電紀錄",
            type=["json"],
            disabled=not can_enter,
        )

        if uploaded_file is not None and can_enter:
            try:
                data = json.load(uploaded_file)

                if "history" in data and "energy_log" in data:
                    st.success(f"✅ 成功讀取紀錄。歡迎回來，{data.get('nickname', '老師')}。")

                    if st.button("🚀 繼續今日充電", type="primary"):
                        st.session_state.user_nickname = data.get("nickname", "老師")
                        st.session_state.history = data.get("history", [])
                        st.session_state.energy_log = data.get("energy_log", [])
                        st.session_state.strengths_data = data.get("strengths_data", {})
                        st.session_state.memory_summary = data.get("memory_summary", "")
                        st.session_state.start_time = utc_now()
                        st.session_state.logout_time = None
                        st.session_state.session_id = str(uuid.uuid4())
                        st.session_state.sheets_row_number = None
                        st.session_state.usage_count = None
                        save_session_to_google_sheets(final=False)
                        st.session_state.app_phase = "initial_checkin"
                        st.rerun()
                else:
                    st.error("❌ 檔案格式不正確，找不到必要紀錄。")

            except Exception as e:
                st.error(f"❌ 讀取失敗：{e}")


# ------------------------------
# 階段 2：開始前測
# ------------------------------
elif st.session_state.app_phase == "initial_checkin":
    if not has_api_key:
        st.error("❌ API Key 已清空，請在左側欄重新輸入。")
        st.stop()

    st.info(f"歡迎您，{st.session_state.user_nickname}。在開始對話前，先感受一下現在的身心狀態。")
    render_window_reference()

    initial_score = st.slider("👉 您現在的能量落在哪個區間？", 0, 10, 5)

    if st.button("💾 記錄並開始對話", type="primary"):
        add_energy_score("前", initial_score)
        st.session_state.system_prompt = build_coach_system_prompt(initial_score)

        state_msg = get_state_message(initial_score)

        if len(st.session_state.history) == 0:
            welcome_msg = f"""（為您拉開一張舒適的椅子，也把語速放慢一點。）

{state_msg}

這裡不需要表現得很好，也沒有人會評價您。今天最耗能、最辛苦的事情是什麼呢？如果願意，我們可以從一小段開始。"""

            st.session_state.history = [
                {"role": "assistant", "content": welcome_msg}
            ]

        else:
            if not st.session_state.memory_summary and len(st.session_state.history) > RECENT_TURNS_FOR_CHAT:
                with st.spinner("正在整理先前記憶..."):
                    maybe_update_memory(force=True)

            resume_msg = f"""（重新為您倒一杯溫水。）

歡迎回來。{state_msg}

距離上次聊聊又過了一段時間。今天的你，最需要先被接住的是哪一部分呢？"""

            st.session_state.history.append({"role": "assistant", "content": resume_msg})

        save_session_to_google_sheets(final=False)
        st.session_state.app_phase = "chatting"
        st.rerun()


# ------------------------------
# 階段 3：對話中
# ------------------------------
elif st.session_state.app_phase == "chatting":
    if not has_api_key:
        st.error("❌ API Key 已清空，請在左側欄重新輸入。")
        st.stop()

    col1, col2 = st.columns([4, 1])
    with col2:
        if st.button("🏁 結束對話", help="結束本次充電並進行後測"):
            save_session_to_google_sheets(final=False)
            st.session_state.app_phase = "final_checkin"
            st.rerun()

    if st.session_state.memory_summary:
        with st.expander("🧠 教練的壓縮記憶", expanded=False):
            st.write(st.session_state.memory_summary)

    visible_history = st.session_state.history[-80:]
    if len(st.session_state.history) > 80:
        st.info("目前畫面僅顯示最近 80 則訊息；完整紀錄仍會保留在下載檔與後台紀錄。")

    for msg in visible_history:
        role = "assistant" if msg["role"] == "assistant" else "user"
        with st.chat_message(role):
            st.write(msg["content"])

    if user_in := st.chat_input("分享您的感受..."):
        st.session_state.history.append({"role": "user", "content": user_in})

        with st.chat_message("user"):
            st.write(user_in)

        if detect_crisis(user_in):
            with st.chat_message("assistant"):
                st.write(CRISIS_MESSAGE)
            st.session_state.history.append({"role": "assistant", "content": CRISIS_MESSAGE})
            save_session_to_google_sheets(final=False)
            st.rerun()

        with st.spinner("⏳ 教練傾聽中..."):
            try:
                resp_text = send_message_safely(user_in)
                st.session_state.history.append({"role": "assistant", "content": resp_text})

                maybe_update_memory()
                st.rerun()

            except Exception as e:
                st.error(f"❌ 發生錯誤：{e}")

    st.caption(
        f"完整紀錄目前 {len(st.session_state.history)} 則；"
        f"實際送給 Gemini 的內容會控制在摘要記憶 + 最近 {RECENT_TURNS_FOR_CHAT} 則對話。"
    )


# ------------------------------
# 階段 4：結束後測
# ------------------------------
elif st.session_state.app_phase == "final_checkin":
    if not has_api_key:
        st.error("❌ API Key 已清空，請在左側欄重新輸入。")
        st.stop()

    st.markdown("### 🏁 梳理完畢，您現在感覺如何？")
    st.info("經過剛剛的梳理與對話，請再次評估您現在的神經系統狀態。")
    render_window_reference()

    final_score = st.slider("👉 對話後的能量區間：", 0, 10, 5)

    if st.button("📊 生成我的能量走勢與優勢雷達圖", type="primary"):
        with st.spinner("✨ 正在整理能量軌跡、VIA 六大美德雷達圖，並寫入研習後台..."):
            add_energy_score("後", final_score)
            maybe_update_memory(force=True)
            st.session_state.strengths_data = analyze_strengths()
            save_session_to_google_sheets(final=True)

        st.session_state.app_phase = "show_chart"
        st.rerun()


# ------------------------------
# 階段 5：顯示圖表與下載
# ------------------------------
elif st.session_state.app_phase == "show_chart":
    st.success("🎉 恭喜您完成了一次自我照顧的練習。來看看這次留下的軌跡。")

    col_chart1, col_chart2 = st.columns(2)

    with col_chart1:
        st.markdown("#### 🔋 您的能量流動軌跡")

        if st.session_state.energy_log:
            df_chart = pd.DataFrame(st.session_state.energy_log)

            line = alt.Chart(df_chart).mark_line(color="#424242", size=4).encode(
                x=alt.X(
                    "階段:N",
                    sort=alt.EncodingSortField(field="排序", order="ascending"),
                    title="對話階段",
                    axis=alt.Axis(labelAngle=-45, labelFontSize=12),
                ),
                y=alt.Y("分數:Q", scale=alt.Scale(domain=[0, 10]), title="狀態分數"),
            )

            points = alt.Chart(df_chart).mark_circle(size=150, color="#1E88E5").encode(
                x=alt.X("階段:N", sort=alt.EncodingSortField(field="排序", order="ascending")),
                y=alt.Y("分數:Q"),
                tooltip=["階段", "分數"],
            )

            band_red = alt.Chart(pd.DataFrame({"y1": [7], "y2": [10]})).mark_rect(
                color="#ffcccc", opacity=0.4
            ).encode(y="y1:Q", y2="y2:Q")

            band_green = alt.Chart(pd.DataFrame({"y1": [4], "y2": [7]})).mark_rect(
                color="#ccffcc", opacity=0.4
            ).encode(y="y1:Q", y2="y2:Q")

            band_blue = alt.Chart(pd.DataFrame({"y1": [0], "y2": [4]})).mark_rect(
                color="#cce5ff", opacity=0.4
            ).encode(y="y1:Q", y2="y2:Q")

            first_stage = df_chart["階段"].iloc[0]
            labels = pd.DataFrame({
                "x": [first_stage, first_stage, first_stage],
                "y": [9, 5.5, 2],
                "text": [
                    "紅區：過度激發",
                    "綠區：容納之窗",
                    "藍區：過低激發",
                ],
                "color": ["#d32f2f", "#2e7d32", "#1565c0"],
            })

            text_layer = alt.Chart(labels).mark_text(
                align="left",
                dx=10,
                fontSize=13,
                fontWeight="bold",
                opacity=0.55,
            ).encode(
                x="x:N",
                y="y:Q",
                text="text:N",
                color=alt.Color("color:N", scale=None),
            )

            final_chart = alt.layer(
                band_red,
                band_green,
                band_blue,
                text_layer,
                line,
                points,
            ).properties(height=350)

            st.altair_chart(final_chart, use_container_width=True)
        else:
            st.info("尚無能量紀錄。")

    with col_chart2:
        st.markdown("#### 🌟 您的六大美德優勢 VIA")

        if st.session_state.strengths_data:
            ordered_strengths = {
                key: st.session_state.strengths_data.get(key, 0)
                for key in VIA_KEYS
            }

            df_radar = pd.DataFrame({
                "r": list(ordered_strengths.values()),
                "theta": list(ordered_strengths.keys()),
            })

            fig = px.line_polar(
                df_radar,
                r="r",
                theta="theta",
                line_close=True,
                range_r=[0, 10],
            )

            fig.update_traces(
                fill="toself",
                fillcolor="rgba(255, 165, 0, 0.35)",
                line_color="darkorange",
            )

            fig.update_layout(
                polar=dict(radialaxis=dict(visible=True, range=[0, 10])),
                showlegend=False,
                margin=dict(l=40, r=40, t=20, b=20),
                height=350,
            )

            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("這次沒有成功產生 VIA 雷達圖。您仍可以下載完整紀錄。")

    st.markdown("""
> **教練的悄悄話**  
> 情緒是流動的，而您的力量也不是只在狀態好的時候才存在。  
> 即使在耗能的時刻，您仍可能展現了某些值得被看見的優勢。
""")

    st.markdown("---")

    export_data = build_export_data()
    json_str = json.dumps(export_data, ensure_ascii=False, indent=2)

    col_a, col_b = st.columns(2)

    with col_a:
        st.download_button(
            label="📥 下載專屬充電記憶 JSON",
            data=json_str.encode("utf-8-sig"),
            file_name=f"ChargeCoach_Memory_{sanitize_filename(st.session_state.user_nickname)}_{now_tw().strftime('%Y%m%d')}.json",
            mime="application/json",
            type="primary",
        )

    with col_b:
        if st.button("🏠 登出 / 下一位使用者"):
            reset_app(clear_keys=True)
            st.rerun()
