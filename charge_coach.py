import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import time

# --- 1. 系統設定 ---
st.set_page_config(page_title="溫充電教練 (教師專屬)", layout="wide", page_icon="☕")

# --- Google Sheets 背景自動上傳函式 ---
def auto_save_to_google_sheets(user_id, chat_history):
    if not chat_history:
        return False
    try:
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds_dict = dict(st.secrets["gcp_service_account"])
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
        
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        
        target_sheet_name = "2025創傷知情研習數據" 
        sheet = client.open(target_sheet_name)
        
        try:
            worksheet = sheet.worksheet("ChargeCoach")
        except gspread.WorksheetNotFound:
            worksheet = sheet.add_worksheet(title="ChargeCoach", rows="1000", cols="10")
            worksheet.append_row(["登入時間", "登出時間", "學員編號", "使用分鐘數", "累積使用次數", "完整對話紀錄"])
        
        tw_fix = timedelta(hours=8)
        start_t = st.session_state.get('start_time', datetime.now())
        login_str = (start_t + tw_fix).strftime("%Y-%m-%d %H:%M:%S")
        end_t = datetime.now()
        logout_str = (end_t + tw_fix).strftime("%Y-%m-%d %H:%M:%S")
        duration_mins = round((end_t - start_t).total_seconds() / 60, 2)
        
        full_conversation = "【溫充電教練紀錄】\n\n"
        for msg in chat_history:
            role = msg.get("role", "Unknown")
            content = msg.get("content", "")
            full_conversation += f"[{role}]: {content}\n"

        records = worksheet.get_all_records()
        row_to_update = None
        col_logins = worksheet.col_values(1) 
        col_ids = worksheet.col_values(3)    
        
        for i in range(1, len(col_logins)): 
            if i < len(col_ids) and col_logins[i] == login_str and str(col_ids[i]) == str(user_id):
                row_to_update = i + 1 
                break
                
        login_count = col_ids.count(str(user_id))
        if row_to_update is None:
            login_count += 1 
            
        data_row = [login_str, logout_str, user_id, duration_mins, login_count, full_conversation]
        
        if row_to_update:
            worksheet.update(f'A{row_to_update}:F{row_to_update}', [data_row])
        else:
            worksheet.append_row(data_row)
        return True
    except Exception as e:
        print(f"背景上傳失敗: {e}")
        return False

# --- API 輪替與防呆發送機制 ---
def send_message_safely(text):
    time.sleep(1) # 強制減速
    gemini_history = []
    for msg in st.session_state.history:
        g_role = "model" if msg["role"] == "assistant" else "user"
        gemini_history.append({"role": g_role, "parts": [msg["content"]]})
        
    api_keys = st.session_state.api_keys_list
    total_keys = len(api_keys)
    
    for i in range(total_keys):
        current_key_index = (st.session_state.current_key_index + i) % total_keys
        active_key = api_keys[current_key_index]
        
        try:
            genai.configure(api_key=active_key)
            model = genai.GenerativeModel(
                model_name=st.session_state.valid_model_name,
                safety_settings={
                    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
                    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
                    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
                    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
                }
            )
            chat_session = model.start_chat(history=gemini_history)
            response = chat_session.send_message(text)
            st.session_state.current_key_index = current_key_index
            return response.text
        except Exception as e:
            error_msg = str(e).lower()
            st.toast(f"⚠️ Key {current_key_index + 1} 發生狀況，嘗試切換...", icon="🔄")
            if i == total_keys - 1:
                if "429" in error_msg or "quota" in error_msg:
                    st.warning("🐌 哎呀！您輸入的速度太快了。請稍等 1 分鐘後再試喔！")
                    return None
                else:
                    raise e

# 初始化 Session State
if "history" not in st.session_state: st.session_state.history = []
if "user_nickname" not in st.session_state: st.session_state.user_nickname = ""
if "start_time" not in st.session_state: st.session_state.start_time = datetime.now()
if "chat_session_initialized" not in st.session_state: st.session_state.chat_session_initialized = False
if "raw_api_key_input" not in st.session_state: st.session_state.raw_api_key_input = ""
if "api_keys_list" not in st.session_state: st.session_state.api_keys_list = []
if "current_key_index" not in st.session_state: st.session_state.current_key_index = 0
if "valid_model_name" not in st.session_state: st.session_state.valid_model_name = "gemini-1.5-pro-latest"

# --- 2. 登入區 ---
if not st.session_state.user_nickname:
    st.title("☕ 教職員溫充電教練")
    st.markdown("### 先顧好自己，AI 才能幫上忙。")
    st.info("這是一個專為老師設計的安全空間，請輸入您的代號開始。")
    
    nickname_input = st.text_input("請輸入您的編號或稱呼：", placeholder="例如：大業國小王老師") 
    
    if st.button("🚀 進入充電站"):
        if nickname_input.strip():
            st.session_state.user_nickname = nickname_input
            st.session_state.start_time = datetime.now()
            st.rerun()
        else:
            st.error("❌ 稱呼不能為空！")
    st.stop()

# --- 3. 側邊欄設定 ---
st.sidebar.title(f"👤 {st.session_state.user_nickname}")
st.sidebar.markdown("*(系統已開啟自動存檔功能)*")
st.sidebar.markdown("---")

if st.session_state.chat_session_initialized:
    st.sidebar.markdown("### 🏠 導覽")
    if st.sidebar.button("🔄 重新充電 (清除對話)", type="secondary"):
        st.session_state.history = []
        st.session_state.chat_session_initialized = False
        st.session_state.start_time = datetime.now() 
        st.rerun()

st.sidebar.markdown("---")
st.sidebar.warning("🔑 請輸入您的 Gemini API Key")
input_key = st.sidebar.text_input("在此貼上您的 API Key (可用逗號隔開多組)", type="password", value=st.session_state.raw_api_key_input)

if input_key:
    st.session_state.raw_api_key_input = input_key
    st.session_state.api_keys_list = [k.strip() for k in input_key.split(",") if k.strip()]

if not st.session_state.api_keys_list:
    st.info("💡 提示：請先輸入 API Key。")
    st.stop() 

if st.session_state.api_keys_list:
    try:
        genai.configure(api_key=st.session_state.api_keys_list[0])
        available_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        if available_models:
            st.session_state.valid_model_name = st.sidebar.selectbox("🤖 AI 模型", available_models)
    except: 
        st.sidebar.error("❌ API Key 無效，請檢查。")

# --- 4. 對話主畫面 ---
st.title("☕ 溫充電教練 (Warm Charge Coach)")
st.markdown("這裡沒有評價，沒有標準答案。只有陪伴與優勢的尋找。")

if st.session_state.api_keys_list and st.session_state.valid_model_name:

    if not st.session_state.chat_session_initialized:
        sys_prompt = f"""
        Role: You are the "Warm Charge Coach" (溫充電教練), an AI assistant designed specifically for educators to practice self-care based on Trauma-Informed Care (TIC) and the Strengths Perspective.
        Target Audience: A stressed or tired school teacher.
        Language: 繁體中文.
        
        [CORE PHILOSOPHY]
        1. **Trauma-Informed:** You understand the "Window of Tolerance" (容納之窗). You do not judge, rush, or immediately offer solutions. Your priority is to help the teacher feel safe and grounded.
        2. **Strengths-Based:** You believe every teacher has inherent resilience. You actively look for and highlight the strengths, efforts, or positive intentions in what the teacher shares.
        
        [INTERACTION PHASES (MUST FOLLOW IN ORDER)]
        
        **Phase 1: Assessment & Grounding (評估與著陸)**
        - Ask the teacher how they are feeling right now in terms of their "Window of Tolerance" (e.g., calm/in the window, anxious/hyperaroused, or exhausted/hypoaroused).
        - If they are outside their window (anxious or exhausted), gently guide them through a very brief, 1-minute grounding exercise (e.g., taking a deep breath, noticing 3 things around them) before discussing their problems.
        
        **Phase 2: Strengths-Spotting (優勢探勘)**
        - Ask what was the most draining or challenging part of their day.
        - *CRITICAL:* When they share their struggle, do NOT give advice on how to fix the student or the problem. Instead, reflect back a strength or positive effort you noticed in their story.
        - Ask them: "In the past, when you faced similar exhausting days, what helped you get through it?"
        
        **Phase 3: Micro-Action Planning (微行動計畫)**
        - Based on their identified strengths or past successful coping strategies, offer 3 highly specific, extremely small "Micro-Actions" (微行動) they can do *today* to recharge. These must take less than 5 minutes.
        - Let them choose one, emphasizing that "even choosing to do nothing is a valid choice for self-care."
        
        [TONE & STYLE]
        - Warm, validating, slow-paced, and deeply empathetic.
        - Use short paragraphs. 
        - You MUST use parentheses ( ) to describe your own gentle, non-verbal behaviors or tone (e.g., "(遞上一杯溫水的想像，語氣輕柔)", "(專注且不帶評價地傾聽)").
        - Do not explain you are an AI.
        """
        
        welcome_msg = f"(為您拉開一張舒適的椅子，掛著溫暖的微笑)\n\n{st.session_state.user_nickname} 您好，我是您的專屬『溫充電教練』。教育現場就像戰場，在照顧學生之前，我們得先照顧好自己。\n\n現在的您，感覺是在『容納之窗』裡面（覺得平靜、還可以應付），還是有點『過度激患』(焦慮、煩躁)，或是『過低激患』(疲憊、無力) 呢？"
        
        st.session_state.history = [{"role": "user", "content": sys_prompt}, {"role": "assistant", "content": welcome_msg}]
        st.session_state.chat_session_initialized = True
        auto_save_to_google_sheets(st.session_state.user_nickname, st.session_state.history)

    for msg in st.session_state.history:
        role = "assistant" if msg["role"] == "assistant" else "user"
        if "Role: You are the \"Warm Charge Coach\"" not in msg["content"]:
            with st.chat_message(role):
                st.write(msg["content"])

    if user_in := st.chat_input("分享您現在的感受... (可用括號描述動作，例如：(嘆氣) 我覺得很煩躁)"):
        st.session_state.history.append({"role": "user", "content": user_in})
        with st.chat_message("user"):
            st.write(user_in)
            
        with st.spinner("⏳ 教練傾聽中..."):
            try:
                resp_text = send_message_safely(user_in)
                if resp_text: 
                    st.session_state.history.append({"role": "assistant", "content": resp_text})
                    auto_save_to_google_sheets(st.session_state.user_nickname, st.session_state.history)
                    st.rerun()
            except Exception as e:
                st.error(f"❌ 發生嚴重錯誤: {e}")