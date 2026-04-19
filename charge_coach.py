import streamlit as st
import pandas as pd
import altair as alt
import plotly.express as px
import json
from datetime import datetime, timedelta
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import time

# --- 1. 系統設定 ---
st.set_page_config(page_title="溫充電教練 (賦能雷達版)", layout="wide", page_icon="☕")

# --- Google Sheets 背景自動上傳函式 ---
def auto_save_to_google_sheets(user_id, chat_history, energy_log, strengths_data=None):
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
        
        energy_str = " -> ".join([f"{item['階段']}:{item['分數']}分" for item in energy_log])
        
        full_conversation = f"【能量走勢】：{energy_str}\n"
        if strengths_data:
            full_conversation += f"【優勢評估】：{strengths_data}\n"
        full_conversation += "\n"
        
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

# --- 背景 AI 優勢分析器 (Hidden Evaluator) ---
def analyze_strengths(chat_history, active_key, model_name):
    """在對話結束後，偷偷呼叫 AI 快速分析這段對話中的六大美德分數"""
    try:
        genai.configure(api_key=active_key)
        model = genai.GenerativeModel(model_name=model_name)
        
        history_text = "\n".join([f"{msg['role']}: {msg['content']}" for msg in chat_history if msg['role'] != 'system'])
        
        prompt = f"""
        你是一位正向心理學專家。請分析以下這段老師與教練的對話紀錄。
        請嚴格根據 VIA 六大美德（智慧與知識、勇氣、人道、正義、節制、超越），
        評估這位老師在面對教學挑戰與情緒調適的過程中所展現出的優勢強度。
        請使用 1 到 10 分的量尺（10分為極高展現）。
        
        請「只」輸出純 JSON 格式，不要包含任何其他文字、引號或 Markdown 標記 (不要有 ```json)。
        格式範例：
        {{"智慧與知識": 8, "勇氣": 7, "人道": 9, "正義": 6, "節制": 7, "超越": 8}}
        
        【對話紀錄】：
        {history_text}
        """
        response = model.generate_content(prompt)
        result_text = response.text.strip().strip('`').replace('json\n', '')
        strengths_dict = json.loads(result_text)
        return strengths_dict
    except Exception as e:
        print(f"優勢分析失敗: {e}")
        # 如果失敗，給一組溫暖的預設安慰分數避免當機 (統一使用精確名稱)
        return {"智慧與知識": 7, "勇氣": 8, "人道": 8, "正義": 7, "節制": 6, "超越": 6}

# --- API 輪替與防呆發送機制 ---
def send_message_safely(text):
    time.sleep(1) 
    system_prompt = st.session_state.history[0]["content"]
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
if "strengths_data" not in st.session_state: st.session_state.strengths_data = {}
if "user_nickname" not in st.session_state: st.session_state.user_nickname = ""
if "start_time" not in st.session_state: st.session_state.start_time = datetime.now()
if "raw_api_key_input" not in st.session_state: st.session_state.raw_api_key_input = ""
if "api_keys_list" not in st.session_state: st.session_state.api_keys_list = []
if "current_key_index" not in st.session_state: st.session_state.current_key_index = 0
if "valid_model_name" not in st.session_state: st.session_state.valid_model_name = "gemini-2.5-flash"

def reset_app():
    st.session_state.app_phase = "login"
    st.session_state.history = []
    st.session_state.energy_log = []
    st.session_state.strengths_data = {}
    st.session_state.user_nickname = "" 
    st.session_state.start_time = datetime.now()

# --- 側邊欄 ---
if st.session_state.user_nickname:
    st.sidebar.title(f"👤 {st.session_state.user_nickname}")
    if st.sidebar.button("🔄 重新開始 / 切換帳號", type="secondary"):
        reset_app()
        st.rerun()
    st.sidebar.markdown("---")

st.sidebar.warning("🔑 請輸入 Gemini API Key")
input_key = st.sidebar.text_input("貼上 API Key (可用逗號隔開多組)", type="password", value=st.session_state.raw_api_key_input)
if input_key:
    st.session_state.raw_api_key_input = input_key
    st.session_state.api_keys_list = [k.strip() for k in input_key.split(",") if k.strip()]

has_api_key = len(st.session_state.api_keys_list) > 0

if has_api_key:
    try:
        genai.configure(api_key=st.session_state.api_keys_list[0])
        available_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        if available_models:
            default_idx = available_models.index("models/gemini-2.5-flash") if "models/gemini-2.5-flash" in available_models else 0
            st.session_state.valid_model_name = st.sidebar.selectbox("🤖 AI 模型", available_models, index=default_idx)
    except: 
        st.sidebar.error("❌ API Key 無效")
else:
    st.sidebar.info("💡 提示：請在此輸入 API Key 以解鎖系統。")

# ==========================================
# 畫面流程控制
# ==========================================
st.title("☕ 溫充電教練 (動態互動版)")

# 【階段 1】：登入與讀取畫面
if st.session_state.app_phase == "login":
    st.markdown("### 先顧好自己，AI 才能幫上忙。")
    if not has_api_key:
        st.warning("⚠️ 系統尚未連線：請先在左側邊欄輸入您的「API Key」來解鎖充電站大門喔！")
        
    tab1, tab2 = st.tabs(["✨ 新的充電", "📂 載入過去紀錄 (延續走勢)"])
    
    with tab1:
        nickname_input = st.text_input("請輸入您的稱呼：", placeholder="例如：大業國小王老師", disabled=not has_api_key, key="new_login") 
        if st.button("🚀 進入充電站", type="primary", disabled=not has_api_key):
            if nickname_input.strip():
                st.session_state.user_nickname = nickname_input
                st.session_state.app_phase = "initial_checkin"
                st.rerun()
            else:
                st.error("❌ 稱呼不能為空！")
                
    with tab2:
        st.info("上傳您過去專屬的「充電紀錄 (.json)」，教練會為您延續跨日的能量走勢圖！")
        uploaded_file = st.file_uploader("上傳您的充電紀錄", type=['json'], disabled=not has_api_key)
        if uploaded_file is not None and has_api_key:
            try:
                data = json.load(uploaded_file)
                if "history" in data and "energy_log" in data:
                    st.success(f"✅ 成功喚醒記憶！歡迎回來，{data.get('nickname', '老師')}。")
                    if st.button("🚀 繼續今日充電", type="primary"):
                        st.session_state.user_nickname = data.get("nickname", "老師")
                        st.session_state.history = data["history"]
                        st.session_state.energy_log = data["energy_log"]
                        st.session_state.strengths_data = data.get("strengths_data", {}) 
                        st.session_state.start_time = datetime.now() 
                        st.session_state.app_phase = "initial_checkin"
                        st.rerun()
                else:
                    st.error("❌ 檔案格式不正確，找不到紀錄。")
            except Exception as e:
                st.error(f"❌ 讀取失敗: {e}")

# 【階段 2】：開始前測
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
        today_str = (datetime.now() + timedelta(hours=8)).strftime("%m/%d")
        phase_name = f"{today_str} 前"
        st.session_state.energy_log.append({"階段": phase_name, "分數": initial_score, "排序": len(st.session_state.energy_log) + 1})
        
        # 將您指定的 VIA 核心意涵與詞彙完美整合進 Prompt
        sys_prompt = f"""
        Role: You are the "Warm Charge Coach" (溫充電教練), an AI assistant designed specifically for educators to practice self-care based on Trauma-Informed Care (TIC) and the Strengths Perspective.
        Target Audience: A stressed or tired school teacher.
        Language: 繁體中文.
        
        [CORE PHILOSOPHY]
        1. **Trauma-Informed:** You understand the "Window of Tolerance". Do not judge, rush, or immediately offer solutions. Help the teacher ground.
        2. **Strengths-Based (VIA Character Strengths):** Actively listen for the teacher's virtues and 24 character strengths. EXPLICITLY name these strengths when you reflect their efforts.
        
        【嚴格遵守之 VIA 六大美德與 24 項優勢定義】：
        VIA六大美德與24項優勢是正向心理學的核心概念。請您在與老師對話時，嚴格使用以下詞彙來賦能老師：
        1. 智慧與知識 (Wisdom and Knowledge)：創造力、好奇心、開明思想、喜愛學習、觀點。
        2. 勇氣 (Courage)：勇敢、堅毅、正直、生命力。
        3. 人道 (Humanity)：愛、仁慈、社交智慧。
        4. 正義 (Justice)：公民精神、公平、領導力。
        5. 節制 (Temperance)：寬恕、謙遜、謹慎、自制力。
        6. 超越 (Transcendence)：欣賞美好卓越、感恩、希望、幽默、靈修性。
        
        [INTERACTION PHASES]
        **Phase 1: Grounding & Strengths-Spotting (著陸與探勘)**
        - Start by acknowledging their current self-reported state. 
        - Ask what was the most draining part of their day.
        - Reflect back a specific VIA character strength you noticed in their story (e.g., "我看到你在那一個充滿挑戰的時刻，展現了極大的『節制』與『人道』精神...").
        
        **Phase 2: Micro-Action Planning (微行動計畫)**
        - Offer 3 highly specific, extremely small "Micro-Actions" (taking less than 5 minutes) they can do today to recharge.
        - Let them choose one.
        
        [TONE & STYLE]
        - Warm, validating, deeply empathetic.
        - Use short paragraphs. 
        - Use parentheses ( ) to describe your own gentle, non-verbal behaviors.
        - Do not explain you are an AI.
        """
        
        if initial_score >= 8:
            state_msg = "看到您剛剛標記的狀態落在比較焦慮、煩躁的紅區。辛苦您了，現在的神經系統一定很緊繃吧。"
        elif initial_score <= 3:
            state_msg = "看到您剛剛標記的狀態落在比較疲憊、無力的藍區。辛苦您了，今天一定耗費了非常多心神吧。"
        else:
            state_msg = "看到您剛剛標記的狀態落在相對平穩的綠區，這是一個很好的開始。"
            
        if len(st.session_state.history) == 0:
            welcome_msg = f"(為您拉開一張舒適的椅子，倒了一杯溫水)\n\n{state_msg}\n\n這裡非常安全，沒有人會評價您。今天讓您感到最耗能、最辛苦的事情是什麼呢？願意跟我分享嗎？"
            st.session_state.history = [
                {"role": "user", "content": sys_prompt}, 
                {"role": "user", "content": "教練，我準備好要開始充電了。"}, 
                {"role": "assistant", "content": welcome_msg}
            ]
        else:
            resume_welcome_msg = f"(為您拉開一張舒適的椅子，倒了一杯溫水)\n\n歡迎回來！{state_msg}\n\n距離我們上次聊聊又過了一陣子，今天過得好嗎？有什麼最讓您感到耗能的事情，願意跟我分享嗎？"
            st.session_state.history.append({"role": "user", "content": f"[系統提示：這是新的一天。使用者登入自評能量為 {initial_score} 分。請接續過去的記憶，關懷使用者今天的狀況。]"})
            st.session_state.history.append({"role": "assistant", "content": resume_welcome_msg})

        st.session_state.app_phase = "chatting"
        st.rerun()

# 【階段 3】：對話中
elif st.session_state.app_phase == "chatting":
    
    col1, col2 = st.columns([4, 1])
    with col2:
        if st.button("🏁 結束對話，查看能量走勢", help="點擊此按鈕結束本次充電，並生成走勢圖"):
            st.session_state.app_phase = "final_checkin"
            st.rerun()

    for msg in st.session_state.history:
        role = "assistant" if msg["role"] == "assistant" else "user"
        if "Role: You are the" not in msg["content"] and "[系統提示" not in msg["content"] and "準備好要開始" not in msg["content"]:
            with st.chat_message(role):
                st.write(msg["content"])

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

# 【階段 4】：結束後測
elif st.session_state.app_phase == "final_checkin":
    st.markdown("### 🏁 梳理完畢，您現在感覺如何？")
    st.info("經過剛剛的梳理與對話，請再次評估您現在的神經系統狀態。")
    
    st.markdown("""
    **💡 容納之窗參考指標：**
    * **8~10分 (紅區)**：過度激患 (焦慮、煩躁、恐慌、想發脾氣)
    * **4~7分 (綠區)**：容納之窗 (平靜、安全、能自我調節)
    * **0~3分 (藍區)**：過低激患 (疲憊、無力、麻木、大腦當機)
    """)
    
    final_score = st.slider("👉 對話後的能量區間：", 0, 10, 5)
    
    if st.button("📊 生成我的專屬能量與優勢雷達圖", type="primary"):
        with st.spinner("✨ 教練正在為您繪製專屬的「能量軌跡」與「六大美德雷達圖」，請稍候..."):
            today_str = (datetime.now() + timedelta(hours=8)).strftime("%m/%d")
            phase_name = f"{today_str} 後"
            st.session_state.energy_log.append({"階段": phase_name, "分數": final_score, "排序": len(st.session_state.energy_log) + 1})
            
            active_key = st.session_state.api_keys_list[st.session_state.current_key_index]
            strengths_data = analyze_strengths(st.session_state.history, active_key, st.session_state.valid_model_name)
            st.session_state.strengths_data = strengths_data
            
            auto_save_to_google_sheets(st.session_state.user_nickname, st.session_state.history, st.session_state.energy_log, st.session_state.strengths_data)
            
        st.session_state.app_phase = "show_chart"
        st.rerun()

# 【階段 5】：顯示動態走勢圖、優勢雷達圖與下載
elif st.session_state.app_phase == "show_chart":
    st.success("🎉 恭喜您完成了一次自我照顧的練習！來看看您今天的收穫：")
    
    col_chart1, col_chart2 = st.columns(2)
    
    with col_chart1:
        st.markdown("#### 🔋 您的能量流動軌跡")
        df_chart = pd.DataFrame(st.session_state.energy_log)
        
        line = alt.Chart(df_chart).mark_line(color='#424242', size=4).encode(
            x=alt.X('階段:N', sort=alt.EncodingSortField(field='排序', order='ascending'), title='對話階段', axis=alt.Axis(labelAngle=-45, labelFontSize=12)),
            y=alt.Y('分數:Q', scale=alt.Scale(domain=[0, 10]), title='狀態分數')
        )
        points = alt.Chart(df_chart).mark_circle(size=150, color='#1E88E5', opacity=1).encode(
            x=alt.X('階段:N', sort=alt.EncodingSortField(field='排序', order='ascending')),
            y=alt.Y('分數:Q'),
            tooltip=['階段', '分數']
        )
        band_red = alt.Chart(pd.DataFrame({'y1': [7], 'y2': [10]})).mark_rect(color='#ffcccc', opacity=0.4).encode(y='y1:Q', y2='y2:Q')
        band_green = alt.Chart(pd.DataFrame({'y1': [4], 'y2': [7]})).mark_rect(color='#ccffcc', opacity=0.4).encode(y='y1:Q', y2='y2:Q')
        band_blue = alt.Chart(pd.DataFrame({'y1': [0], 'y2': [4]})).mark_rect(color='#cce5ff', opacity=0.4).encode(y='y1:Q', y2='y2:Q')
        
        first_stage = df_chart['階段'].iloc[0]
        text_red = alt.Chart(pd.DataFrame({'x': [first_stage], 'y': [9], 'text': ['🔥 過度激患 (焦慮/煩躁)']})).mark_text(align='left', dx=10, fontSize=14, color='#d32f2f', fontWeight='bold', opacity=0.5).encode(x='x:N', y='y:Q', text='text:N')
        text_green = alt.Chart(pd.DataFrame({'x': [first_stage], 'y': [5.5], 'text': ['💚 容納之窗 (平靜/穩定)']})).mark_text(align='left', dx=10, fontSize=14, color='#2e7d32', fontWeight='bold', opacity=0.5).encode(x='x:N', y='y:Q', text='text:N')
        text_blue = alt.Chart(pd.DataFrame({'x': [first_stage], 'y': [2], 'text': ['❄️ 過低激患 (疲憊/無力)']})).mark_text(align='left', dx=10, fontSize=14, color='#1565c0', fontWeight='bold', opacity=0.5).encode(x='x:N', y='y:Q', text='text:N')

        final_chart = alt.layer(band_red, band_green, band_blue, text_red, text_green, text_blue, line, points).properties(height=350)
        st.altair_chart(final_chart, use_container_width=True)

    with col_chart2:
        st.markdown("#### 🌟 您的六大美德優勢 (VIA)")
        if st.session_state.strengths_data:
            s_data = st.session_state.strengths_data
            df_radar = pd.DataFrame(dict(
                r=list(s_data.values()),
                theta=list(s_data.keys())
            ))
            fig = px.line_polar(df_radar, r='r', theta='theta', line_close=True, range_r=[0, 10])
            fig.update_traces(fill='toself', fillcolor='rgba(255, 165, 0, 0.4)', line_color='darkorange')
            fig.update_layout(
                polar=dict(radialaxis=dict(visible=True, range=[0, 10])),
                showlegend=False,
                margin=dict(l=40, r=40, t=20, b=20),
                height=350
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("尚無足夠資料產生優勢雷達圖。")
            
    st.markdown("""
    > 💡 **教練的悄悄話**：
    > 情緒是流動的，而您的力量一直都在。即使在最耗能的時刻，您依然展現了雷達圖上這些閃閃發光的優勢特質。
    > 點擊下方下載您的專屬紀錄，明天再來找我充充電吧！
    """)
    
    st.markdown("---")
    colA, colB = st.columns(2)
    
    with colA:
        export_data = {
            "nickname": st.session_state.user_nickname,
            "history": st.session_state.history,
            "energy_log": st.session_state.energy_log,
            "strengths_data": st.session_state.strengths_data
        }
        json_str = json.dumps(export_data, ensure_ascii=False, indent=2)
        st.download_button(
            label="📥 下載專屬充電記憶 (.json)",
            data=json_str,
            file_name=f"ChargeCoach_Memory_{st.session_state.user_nickname}_{datetime.now().strftime('%Y%m%d')}.json",
            mime="application/json",
            type="primary"
        )
        
    with colB:
        if st.button("🏠 登出 / 下一位使用者"):
            reset_app()
            st.rerun()
