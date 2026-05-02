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
DAILY_QUOTA_COOLDOWN_SECONDS = 12 * 60 * 60

MIN_USER_MESSAGES_FOR_STRENGTHS = 3
MIN_USER_CHARS_FOR_STRENGTHS = 40

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
