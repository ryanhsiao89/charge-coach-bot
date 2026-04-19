import streamlit as st
import pandas as pd
import altair as alt
from datetime import datetime, timedelta
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import time

# --- 1. 系統設定 ---
st.set_page_config(page_title="溫充電教練 (動態互動版)", layout="wide", page_icon="☕")

# --- Google Sheets 背景自動上傳函式 ---
def auto_save_to_google_sheets(user_id, chat_history, energy_log):
    if not chat_history:
        return False
    try:
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds_dict = dict(st.secrets["gcp_service_account"])
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
        
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        sheet = client.open("2025創傷知情研習數據") 
        
        try:
            worksheet = sheet.worksheet("ChargeCoach")
        except gspread.WorksheetNotFound:
            worksheet = sheet.add_worksheet(title="ChargeCoach", rows="1000", cols="10")
            worksheet.append_row(["登入時間", "登出時間", "學員編號", "使用分鐘數", "能量變化", "完整對話紀錄"])
        
        tw_fix = timedelta(hours=8)
        start_t = st.session_state.get('start_time', datetime.now())
        login_str = (start_t + tw_fix).strftime("%Y-%m-%d %H:%M:%S")
        logout_str = (datetime.now() + tw_fix).strftime("%Y-%m-%d %H:%M:%S")
        duration_mins = round((datetime.now() - start_t).total_seconds() / 60, 2)
        
        # 紀錄能量走勢
        energy_str = " -> ".join([f"{item['階段']}:{item['分數']}分" for item in energy_log])
        
        full_conversation = f"【能量走勢】：{energy_str}\n\n"
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
                
        data_row = [login_str, logout_str, user_id, duration_mins, energy_str, full_conversation]
        
        if row_to_update:
            worksheet.update(f'A{row_to_update}:F{row_to_update}', [data_row])
        else:
            worksheet.append_row(data_row)
        return True
    except Exception as e:
        print(f"背景上傳失敗: {e}")
        return False

# --- API 輪替與防呆發送機制 (角色防護 + 歷史修正版) ---
def send_message_safely(text):
    time.sleep(1) 
    
    # 【關鍵防護 1】抽離系統設定，防止教練角色混亂
    system_prompt = st.session_state.history[0]["content"]
    
    # 【關鍵防護 2】組合歷史紀錄時，扣除「最後一筆 (剛輸入的話)」，避免 API 判定連發兩次 User 而當機！
    gemini_history = []
    for msg in st.session_state.history[1:-1]:
        g_role = "model" if msg["role"] == "assistant" else "user"
        gemini_history.append({"role": g_role, "parts": [msg["content"]]})
        
    api_keys = st.session_state.api_keys_list
    total_keys = len(api_keys)
    
    for i in range(total_keys):
        current_key_index = (st.session_state.current_key_index + i) % total_keys
        active_key = api_keys[current_key_index]
        try:
            genai.configure(api_key=active_key)
            
            # 將教練設定鎖死在底層
            model = genai.GenerativeModel(
                model_name=st.session_state.valid_model_name,
                system_instruction=system_prompt,
                safety_settings={
                    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
                    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
                    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
                }
            )
            chat_session = model.start_chat(history=gemini_history)
            response = chat_session.send_message(text)
            st.session_state.current_key_index = current_key_index
            return response.text
        except Exception as e:
            error_msg = str(e).lower()
            st.toast(f"⚠️ Key {current_key_index + 1} 發生狀況，切換中...", icon="🔄")
            if i == total_keys - 1:
                if "429" in error_msg or "quota" in error_msg:
                    st.warning("🐌 您輸入的速度太快了。請稍等 1 分鐘後再試！")
                    return None
                else:
                    raise e

# 初始化 Session State
if "app_phase" not in st.session_state: st.session_state.app_phase = "login"
if "history" not in st.session_state: st.session_state.history = []
if "energy_log" not in st.session_state: st.session_state.energy_log = []
if "user_nickname" not in st.session_state: st.session_state.user_nickname = ""
if "start_time" not in st.session_state: st.session_state.start_time = datetime.now()
if "raw_api_key_input" not in st.session_state: st.session_state.raw_api_key_input = ""
if "api_keys_list" not in st.session_state: st.session_state.api_keys_list = []
if "current_key_index" not in st.session_state: st.session_state.current_key_index = 0
if "valid_model_name" not in st.session_state: st.session_state.valid_model_name = "gemini-2.5-flash"

# 導覽/重置功能
def reset_app():
    st.session_state.app_phase = "login"
    st.session_state.history = []
    st.session_state.energy_log = []
    st.session_state.start_time = datetime.now()

# --- 側邊欄：金鑰與重置 ---
st.sidebar.title(f"👤 {st.session_state.user_nickname if st.session_state.user_nickname else '尚未登入'}")
if st.sidebar.button("🔄 重新開始", type="secondary"):
    reset_app()
    st.rerun()

st.sidebar.markdown("---")
st.sidebar.warning("🔑 請輸入 Gemini API Key")
input_key = st.sidebar.text_input("貼上 API Key (可用逗號隔開多組)", type="password", value=st.session_state.raw_api_key_input)
if input_key:
    st.session_state.raw_api_key_input = input_key
    st.session_state.api_keys_list = [k.strip() for k in input_key.split(",") if k.strip()]

if st.session_state.api_keys_list:
    try:
        genai.configure(api_key=st.session_state.api_keys_list[0])
        available_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        if available_models:
            # 設定 2.5-flash 為預設選項
            default_idx = available_models.index("models/gemini-2.5-flash") if "models/gemini-2.5-flash" in available_models else 0
            st.session_state.valid_model_name = st.sidebar.selectbox("🤖 AI 模型", available_models, index=default_idx)
    except: 
        st.sidebar.error("❌ API Key 無效")

if not st.session_state.api_keys_list:
    st.info("💡 請先在左側輸入 API Key 以啟動系統。")
    st.stop()

# ==========================================
# 畫面流程控制 (Phase Logic)
# ==========================================
st.title("☕ 溫充電教練 (動態互動版)")

# 【階段 1】：登入畫面
if st.session_state.app_phase == "login":
    st.markdown("### 先顧好自己，AI 才能幫上忙。")
    nickname_input = st.text_input("請輸入您的稱呼：", placeholder="例如：大業國小王老師") 
    if st.button("🚀 進入充電站", type="primary"):
        if nickname_input.strip():
            st.session_state.user_nickname = nickname_input
            st.session_state.app_phase = "initial_checkin"
            st.rerun()
        else:
            st.error("❌ 稱呼不能為空！")

# 【階段 2】：開始前測 (自評狀態)
elif st.session_state.app_phase == "initial_checkin":
    st.info(f"歡迎您，{st.session_state.user_nickname}！在開始對話前，請先感受一下現在的身心狀態。")
    
    st.markdown("""
    **💡 容納之窗參考指標：**
    * **8~10分 (紅區)**：過度激患 (焦慮、煩躁、恐慌、想發脾氣)
    * **4~7分 (綠區)**：容納之窗 (平靜、安全、能自我調節)
    * **0~3分 (藍區)**：過低激患 (疲憊、無力、麻木、大腦當機)
    """)
    
    initial_score = st.slider("👉 您現在的能量落在哪個區間？", 0, 10, 5)
    
    if st.button("💾 記錄並開始對話", type="primary"):
        st.session_state.energy_log.append({"階段": "開始前", "分數": initial_score, "次序": 1})
        
        # 初始化 AI Prompt
        sys_prompt = f"""
        Role: You are the "Warm Charge Coach" (溫充電教練), an AI assistant designed specifically for educators to practice self-care based on Trauma-Informed Care (TIC) and the Strengths Perspective.
        Target Audience: A stressed or tired school teacher.
        Language: 繁體中文.
        
        [CORE PHILOSOPHY]
        1. **Trauma-Informed:** You understand the "Window of Tolerance". Do not judge, rush, or immediately offer solutions. Help the teacher ground.
        2. **Strengths-Based:** Look for and highlight the strengths, efforts, or positive intentions in what the teacher shares.
        
        [INTERACTION PHASES]
        **Phase 1: Grounding & Strengths-Spotting (著陸與探勘)**
        - Start by acknowledging their current self-reported state. 
        - Ask what was the most draining part of their day.
        - Reflect back a strength or positive effort you noticed in their story.
        
        **Phase 2: Micro-Action Planning (微行動計畫)**
        - Offer 3 highly specific, extremely small "Micro-Actions" (taking less than 5 minutes) they can do today to recharge.
        - Let them choose one.
        
        [TONE & STYLE]
        - Warm, validating, deeply empathetic.
        - Use short paragraphs. 
        - Use parentheses ( ) to describe your own gentle, non-verbal behaviors.
        - Do not explain you are an AI.
        """
        
        # 根據老師的自評給出不同的溫暖開場白
        if initial_score >= 8:
            state_msg = "看到您剛剛標記的狀態落在比較焦慮、煩躁的紅區。辛苦您了，現在的神經系統一定很緊繃吧。"
        elif initial_score <= 3:
            state_msg = "看到您剛剛標記的狀態落在比較疲憊、無力的藍區。辛苦您了，今天一定耗費了非常多心神吧。"
        else:
            state_msg = "看到您剛剛標記的狀態落在相對平穩的綠區，這是一個很好的開始。"
            
        welcome_msg = f"(為您拉開一張舒適的椅子，倒了一杯溫水)\n\n{state_msg}\n\n這裡非常安全，沒有人會評價您。今天讓您感到最耗能、最辛苦的事情是什麼呢？願意跟我分享嗎？"
        
        st.session_state.history = [
            {"role": "user", "content": sys_prompt}, 
            {"role": "assistant", "content": welcome_msg}
        ]
        st.session_state.app_phase = "chatting"
        st.rerun()

# 【階段 3】：對話中
elif st.session_state.app_phase == "chatting":
    
    # 頂部顯示離開按鈕
    col1, col2 = st.columns([4, 1])
    with col2:
        if st.button("🏁 結束對話，查看能量走勢", help="點擊此按鈕結束本次充電，並生成走勢圖"):
            st.session_state.app_phase = "final_checkin"
            st.rerun()

    # 顯示對話歷史
    for msg in st.session_state.history:
        role = "assistant" if msg["role"] == "assistant" else "user"
        if "Role: You are the \"Warm Charge Coach\"" not in msg["content"]:
            with st.chat_message(role):
                st.write(msg["content"])

    # 對話輸入框
    if user_in := st.chat_input("分享您的感受... (可用括號描述動作)"):
        st.session_state.history.append({"role": "user", "content": user_in})
        with st.chat_message("user"):
            st.write(user_in)
            
        with st.spinner("⏳ 教練傾聽中..."):
            try:
                resp_text = send_message_safely(user_in)
                if resp_text: 
                    st.session_state.history.append({"role": "assistant", "content": resp_text})
                    auto_save_to_google_sheets(st.session_state.user_nickname, st.session_state.history, st.session_state.energy_log)
                    st.rerun()
            except Exception as e:
                st.error(f"❌ 發生錯誤: {e}")

# 【階段 4】：結束後測 (自評狀態)
elif st.session_state.app_phase == "final_checkin":
    st.markdown("### 🏁 梳理完畢，您現在感覺如何？")
    st.info("經過剛剛的梳理與對話，請再次評估您現在的神經系統狀態。")
    
    final_score = st.slider("👉 對話後的能量區間：", 0, 10, 5)
    
    if st.button("📊 生成我的專屬能量走勢圖", type="primary"):
        st.session_state.energy_log.append({"階段": "結束後", "分數": final_score, "次序": 2})
        auto_save_to_google_sheets(st.session_state.user_nickname, st.session_state.history, st.session_state.energy_log)
        st.session_state.app_phase = "show_chart"
        st.rerun()

# 【階段 5】：顯示動態走勢圖
elif st.session_state.app_phase == "show_chart":
    st.success("🎉 恭喜您完成了一次自我照顧的練習！以下是您的能量軌跡：")
    
    df_chart = pd.DataFrame(st.session_state.energy_log)
    
    # 建立 Altair 容納之窗圖表
    # 1. 繪製折線與資料點
    line = alt.Chart(df_chart).mark_line(color='#424242', size=4).encode(
        x=alt.X('階段:N', sort=['開始前', '結束後'], title='對話階段', axis=alt.Axis(labelAngle=0, labelFontSize=14)),
        y=alt.Y('分數:Q', scale=alt.Scale(domain=[0, 10]), title='狀態分數')
    )
    points = alt.Chart(df_chart).mark_circle(size=200, color='#1E88E5', opacity=1).encode(
        x=alt.X('階段:N', sort=['開始前', '結束後']),
        y=alt.Y('分數:Q'),
        tooltip=['階段', '分數']
    )
    
    # 2. 繪製背景顏色帶 (紅、綠、藍)
    band_red = alt.Chart(pd.DataFrame({'y1': [7], 'y2': [10]})).mark_rect(color='#ffcccc', opacity=0.4).encode(y='y1:Q', y2='y2:Q')
    band_green = alt.Chart(pd.DataFrame({'y1': [4], 'y2': [7]})).mark_rect(color='#ccffcc', opacity=0.4).encode(y='y1:Q', y2='y2:Q')
    band_blue = alt.Chart(pd.DataFrame({'y1': [0], 'y2': [4]})).mark_rect(color='#cce5ff', opacity=0.4).encode(y='y1:Q', y2='y2:Q')
    
    # 3. 添加區域標籤
    text_red = alt.Chart(pd.DataFrame({'x': ['開始前'], 'y': [9], 'text': ['🔥 過度激患 (焦慮/煩躁)']})).mark_text(align='left', dx=10, fontSize=16, color='#d32f2f', fontWeight='bold', opacity=0.5).encode(x='x:N', y='y:Q', text='text:N')
    text_green = alt.Chart(pd.DataFrame({'x': ['開始前'], 'y': [5.5], 'text': ['💚 容納之窗 (平靜/穩定)']})).mark_text(align='left', dx=10, fontSize=16, color='#2e7d32', fontWeight='bold', opacity=0.5).encode(x='x:N', y='y:Q', text='text:N')
    text_blue = alt.Chart(pd.DataFrame({'x': ['開始前'], 'y': [2], 'text': ['❄️ 過低激患 (疲憊/無力)']})).mark_text(align='left', dx=10, fontSize=16, color='#1565c0', fontWeight='bold', opacity=0.5).encode(x='x:N', y='y:Q', text='text:N')

    # 合併所有圖層
    final_chart = alt.layer(band_red, band_green, band_blue, text_red, text_green, text_blue, line, points).properties(
        height=400
    )
    
    # 顯示圖表
    st.altair_chart(final_chart, use_container_width=True)
    
    st.markdown("""
    > 💡 **教練的悄悄話**：
    > 不論線條最後停在哪裡，請記得，情緒是流動的。
    > 覺察到自己的狀態，並願意花這 5 分鐘陪伴自己，您就已經做出了最棒的選擇。
    """)
    
    if st.button("🏠 回到首頁 / 下一位使用者"):
        reset_app()
        st.rerun()
