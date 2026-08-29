import os
import sys

# Windows 콘솔 한글 및 이모지 출력 인코딩 오류 방지
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import re
import html
import json
import time
import logging
import urllib.parse
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
import feedparser
from google import genai
from google.genai import types
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from typing import List, Optional

# 로드 (.env 로컬 설정 대비)
load_dotenv()

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("DailyBriefingAgent")


def _clean_env_val(val):
    """환경 변수 값에서 공백, 개행(\r, \n)을 제거하여 클리닝합니다."""
    if not val:
        return val
    return str(val).strip().replace("\r", "").replace("\n", "")


_PRIORITY_MODELS = [
    'gemini-3.7-flash',
    'gemini-3.6-flash',
    'gemini-3.5-flash',
    'gemini-2.5-flash',
    'gemini-flash-latest',
    'gemini-2.5-flash-lite',
    'gemini-2.0-flash',
    'gemini-2.0-flash-lite',
    'gemini-1.5-flash',
    'gemini-1.5-pro',
]

def _try_repair_json(text):
    """사소한 JSON 포맷 오류(trailing comma, 제어문자 등)를 자동 복구합니다."""
    # 1. 제어 문자 제거 (탭/줄바꿈 제외)
    cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    # 2. trailing comma 제거: }, ] 또는 }, } 앞의 쉼표
    cleaned = re.sub(r',\s*([\]\}])', r'\1', cleaned)
    # 3. 1차 시도
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # 4. 큰따옴표 내부의 줄바꿈을 \\n으로 이스케이프
    try:
        fixed = re.sub(r'(?<=")([^"]*?)\n([^"]*?)(?=")', lambda m: m.group(1) + '\\n' + m.group(2), cleaned)
        return json.loads(fixed)
    except json.JSONDecodeError:
        return None


# ==========================================
# 1단계: 뉴스 수집 (Collector)
# ==========================================
def clean_html(text):
    """HTML 태그 및 HTML 엔티티를 제거합니다."""
    if not text:
        return ""
    clean = re.sub(r'<[^>]+>', '', text)
    clean = html.unescape(clean)
    clean = re.sub(r'\s+', ' ', clean)
    return clean.strip()

def clean_google_title(title):
    """Google News 제목 끝에 붙는 언론사 이름(예: - 전자신문)을 정제합니다."""
    if not title:
        return ""
    parts = title.rsplit(" - ", 1)
    if len(parts) > 1:
        return parts[0].strip()
    return title.strip()

def collect_google_news(keyword, limit=20):
    """Google News RSS를 통해 기사를 수집합니다."""
    encoded_keyword = urllib.parse.quote(keyword)
    rss_url = f"https://news.google.com/rss/search?q={encoded_keyword}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        feed = feedparser.parse(rss_url)
        articles = []
        for entry in feed.entries[:limit]:
            title = clean_google_title(entry.title)
            desc = clean_html(entry.get("summary", ""))
            articles.append({
                "title": title,
                "link": entry.link,
                "description": desc or title,
                "source": "Google News",
                "pub_date": entry.get("published", "")
            })
        return articles
    except Exception as e:
        logger.error(f"Google News RSS 수집 에러 ({keyword}): {e}")
        return []

def collect_naver_news(keyword, client_id, client_secret, limit=20):
    """네이버 뉴스 검색 API를 통해 기사를 수집합니다."""
    if not client_id or not client_secret:
        return []
    encoded_keyword = urllib.parse.quote(keyword)
    url = f"https://openapi.naver.com/v1/search/news.json?query={encoded_keyword}&display={limit}&sort=sim"
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret
    }
    try:
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            data = response.json()
            articles = []
            for item in data.get("items", []):
                title = clean_html(item["title"])
                desc = clean_html(item["description"])
                articles.append({
                    "title": title,
                    "link": item["link"],
                    "description": desc or title,
                    "source": "Naver News",
                    "pub_date": item.get("pubDate", "")
                })
            return articles
        return []
    except Exception as e:
        logger.error(f"네이버 뉴스 API 수집 에러 ({keyword}): {e}")
        return []

def collect_all_news(keywords, naver_id=None, naver_secret=None, limit_per_keyword=15):
    """여러 키워드에 대해 뉴스를 통합 수집 및 링크 중복 제거합니다."""
    all_articles = []
    seen_links = set()
    
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(",") if k.strip()]
        
    for kw in keywords:
        g_news = collect_google_news(kw, limit=limit_per_keyword)
        n_news = collect_naver_news(kw, naver_id, naver_secret, limit=limit_per_keyword)
        
        for art in g_news + n_news:
            link = art["link"]
            if link not in seen_links:
                seen_links.add(link)
                all_articles.append(art)
                
    logger.info(f"뉴스 수집 완료: 총 {len(all_articles)}개 기사 수집됨 (중복 링크 제거)")
    return all_articles

def get_economic_indicators():
    """야후 파이낸스 API를 통해 주요 경제 지표(코스피, 코스닥, 환율, 나스닥, 다우)를 수집합니다."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    symbols = {
        '코스피 (KOSPI)': '^KS11',
        '코스닥 (KOSDAQ)': '^KQ11',
        '원/달러 환율': 'USDKRW=X',
        '나스닥 (NASDAQ)': '^IXIC',
        '다우존스 (DOW)': '^DJI'
    }
    indicators = {}
    for name, sym in symbols.items():
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=1d"
        try:
            r = requests.get(url, headers=headers, timeout=5)
            if r.status_code == 200:
                data = r.json()
                if 'chart' in data and 'result' in data['chart'] and data['chart']['result']:
                    meta = data['chart']['result'][0]['meta']
                    price = meta.get('regularMarketPrice')
                    prev_close = meta.get('previousClose') or meta.get('chartPreviousClose')
                    if price is not None and prev_close is not None:
                        change = price - prev_close
                        pct = (change / prev_close) * 100 if prev_close else 0
                        indicators[name] = {
                            'price': price,
                            'change': change,
                            'pct': pct
                        }
                        continue
            logger.warning(f"경제 지표 수집 실패 ({name}): API 응답 이상")
        except Exception as e:
            logger.error(f"경제 지표 수집 중 에러 발생 ({name}): {e}")
    return indicators


# ==========================================
# 2단계: 정제 & 유사도 클러스터링 (Processor)
# ==========================================
def cluster_and_deduplicate_articles(articles, similarity_threshold=0.25):
    """TF-IDF character n-gram 및 Cosine Similarity를 이용해 중복 기사를 쳐내고 대표 기사만 추립니다."""
    if not articles:
        return []
    if len(articles) == 1:
        return [articles[0]]

    corpus = [f"{art.get('title', '')} {art.get('description', '')}" for art in articles]

    try:
        # 한국어 조사 극복을 위한 char n-gram 벡터화
        vectorizer = TfidfVectorizer(
            analyzer='char',
            ngram_range=(2, 3),
            min_df=1,
            sublinear_tf=True
        )
        tfidf_matrix = vectorizer.fit_transform(corpus)
        sim_matrix = cosine_similarity(tfidf_matrix, tfidf_matrix)
        
        visited = set()
        unique_articles = []
        clusters = []

        for i in range(len(articles)):
            if i in visited:
                continue
                
            cluster = [articles[i]]
            visited.add(i)
            
            for j in range(i + 1, len(articles)):
                if j in visited:
                    continue
                if sim_matrix[i][j] >= similarity_threshold:
                    cluster.append(articles[j])
                    visited.add(j)
            
            clusters.append(cluster)

        for cluster in clusters:
            representative = max(cluster, key=lambda x: len(x.get("title", "")) + len(x.get("description", "")))
            representative["cluster_size"] = len(cluster)
            unique_articles.append(representative)
            
        logger.info(f"중복 뉴스 정제 완료: {len(articles)}개 -> {len(unique_articles)}개 뉴스 그룹 도출")
        return unique_articles
    except Exception as e:
        logger.error(f"뉴스 유사도 정제 처리 중 에러 발생: {e}")
        return articles


# ==========================================
# 3단계: 요약 & 분석 (AI Engine — google.genai SDK)
# ==========================================
class BriefingItem(BaseModel):
    headline: str = Field(description="핵심 요약 제목 (기사 제목을 그대로 쓰지 말고 간결히 재구성)")
    summary: str = Field(description="2~3문장 핵심 요약")
    impact: str = Field(description="시사점/파급효과 1줄")
    source_url: str = Field(description="원문 기사 URL, 경제 지표 요약 항목은 빈 문자열")

class BriefingSection(BaseModel):
    category: str = Field(description="카테고리명 (거시 경제 & 주요 지표, 주요 기업 동향, AX · RX · 디지털 트윈 & 로보틱스, 국제 정세, 국내 정치, 스포츠 중 하나)")
    items: List[BriefingItem]

class DailyBriefing(BaseModel):
    title: str = Field(description="브리핑 전체 제목")
    sections: List[BriefingSection]
    closing_comment: str = Field(description="마무리 코멘트 1~2문장")
    short_summary_for_sns: str = Field(description="500자 내외 SNS 요약")


class AIEngine:
    def __init__(self, api_key):
        self.client = None
        if api_key:
            # API 키 클리닝 (gRPC 공백 에러 차단)
            clean_key = "".join(c for c in str(api_key).strip() if 32 < ord(c) < 127)
            if clean_key:
                try:
                    self.client = genai.Client(api_key=clean_key)
                except Exception as e:
                    logger.error(f"Gemini Client 초기화 에러: {e}")
        self._models_cache = None

    def _get_available_models(self):
        if self._models_cache is None:
            try:
                self._models_cache = [m.name for m in self.client.models.list() if hasattr(m, 'name')]
            except Exception as e:
                logger.warning(f"모델 리스트 동적 조회 실패: {e}")
                self._models_cache = []

        available = set(self._models_cache)
        candidates = []
        for p in _PRIORITY_MODELS:
            full_name = f"models/{p}"
            if not available or full_name in available or p in available:
                candidates.append(p)
        return candidates or ['gemini-3.6-flash', 'gemini-1.5-flash']

    def generate_briefing(self, articles, indicators=None, additional_notes="", max_retries=2):
        if not self.client:
            return {"error": "API Client가 초기화되지 않았습니다."}

        articles_text = ""
        for idx, art in enumerate(articles):
            articles_text += f"[{idx+1}] 제목: {art['title']}\n링크: {art['link']}\n설명: {art['description']}\n\n"

        indicators_text = ""
        if indicators:
            indicators_text = "현재 주요 경제 지표:\n"
            for name, val in indicators.items():
                indicators_text += f"- {name}: {val['price']:,.2f} (전일비 {val['change']:+,.2f}, {val['pct']:+.2f}%)\n"
            indicators_text += "\n"

        prompt = f"""당신은 일일 뉴스 브리핑 편집장입니다.
아래의 경제 지표 데이터와 뉴스 기사 목록을 분석하여, 카테고리별로 분류하고 핵심 내용을 요약한 브리핑 원고를 JSON 형식으로 작성해 주세요.

{indicators_text}[뉴스 기사 목록]
{articles_text}
{additional_notes}

[카테고리 분류 규칙]
아래 6개 카테고리 중 해당하는 기사가 있는 카테고리만 포함하세요. 기사가 없는 카테고리는 생략합니다:
1. "거시 경제 & 주요 지표" - 경제, 금융, 환율, 주식시장, 금리, 부동산, 물가 관련
2. "주요 기업 동향" - 기업 투자, M&A, 실적 발표, 신사업, 경영 전략 관련
3. "AX · RX · 디지털 트윈 & 로보틱스" - AI, 로봇, 디지털 트윈, 자동화, 기술 혁신, 신기술 적용 사례 관련
4. "국제 정세" - 해외 정치, 외교, 무역, 지정학적 이슈 관련
5. "국내 정치" - 국내 정책, 입법, 선거, 주요 정치 현안 관련
6. "스포츠" - 스포츠 경기 결과, 이적, 기록, 하이라이트 관련

[작성 지침]
1. 같은 사건에 대한 중복 기사는 하나로 통합하고, 가장 대표적인 원문 링크를 사용하세요.
2. 각 기사 항목에 반드시 원문 기사 링크(source_url)를 포함하세요. 링크는 뉴스 기사 목록에 있는 링크를 그대로 사용하세요.
3. 모든 섹션의 각 항목에 시사점/파급효과(impact)를 1줄로 포함하세요. 특히 "AX · RX · 디지털 트윈 & 로보틱스" 섹션은 필수입니다.
4. headline은 기사 제목을 그대로 쓰지 말고, 핵심을 간결하게 재구성하세요.
5. summary는 2~3문장으로 핵심 내용을 요약하세요.
6. short_summary_for_sns는 전체 브리핑을 500자 내외로 요약한 모바일 알림용 텍스트입니다.
7. 경제 지표 데이터가 제공된 경우, "거시 경제 & 주요 지표" 섹션의 첫 번째 아이템으로 지표 요약을 포함하세요.

응답은 반드시 아래 JSON 스키마를 따르며, 마크다운 코드 블록 없이 순수 JSON만 출력하세요:

{{
  "title": "String (브리핑 전체 제목)",
  "sections": [
    {{
      "category": "String (카테고리명 - 위 6개 중 정확히 일치하는 이름 사용)",
      "items": [
        {{
          "headline": "String (핵심 요약 제목)",
          "summary": "String (2~3문장 요약)",
          "impact": "String (시사점/파급효과 1줄)",
          "source_url": "String (원문 기사 URL, 경제 지표 요약 항목은 빈 문자열)"
        }}
      ]
    }}
  ],
  "closing_comment": "String (마무리 코멘트 1~2문장)",
  "short_summary_for_sns": "String (500자 내외 SNS 요약)"
}}"""

        candidates = self._get_available_models()
        last_error = None

        for model_name in candidates:
            for i in range(max_retries):
                try:
                    logger.info(f"Gemini API 호출 시도 - 모델: {model_name}, 시도: {i+1}")
                    api_start = time.time()
                    response = self.client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            temperature=0.4,
                            max_output_tokens=8192,
                            response_mime_type="application/json",
                            response_schema=DailyBriefing
                        )
                    )
                    api_duration = time.time() - api_start
                    if response:
                        logger.info(f"Gemini API 호출 성공: {api_duration:.2f}초 소요")
                        if hasattr(response, 'parsed') and response.parsed:
                            try:
                                return response.parsed.model_dump()
                            except Exception as pe:
                                logger.warning(f"parsed 객체 dump 실패, text 직접 파싱 시도: {pe}")
                        
                        if response.text:
                            cleaned_text = response.text.strip()
                            if cleaned_text.startswith("```json"):
                                cleaned_text = cleaned_text[7:]
                            elif cleaned_text.startswith("```"):
                                cleaned_text = cleaned_text[3:]
                            if cleaned_text.endswith("```"):
                                cleaned_text = cleaned_text[:-3]
                            cleaned_text = cleaned_text.strip()
                            try:
                                return json.loads(cleaned_text)
                            except json.JSONDecodeError as je:
                                logger.warning(f"JSON 직접 파싱 실패, 자동 복구 시도: {je}")
                                repaired = _try_repair_json(cleaned_text)
                                if repaired is not None:
                                    logger.info("JSON 자동 복구 성공")
                                    return repaired
                                raise je
                except Exception as e:
                    api_duration = time.time() - api_start if 'api_start' in locals() else 0
                    err_str = str(e)
                    logger.warning(f"Gemini API 호출 실패: {model_name}, 에러: {err_str}, {api_duration:.2f}초")
                    last_error = e
                    # JSON 파싱 에러(JSONDecodeError) 또는 API 호출 에러 발생 시 fallback을 위해 break 진행
                    if isinstance(e, json.JSONDecodeError) or any(x in err_str for x in ["404", "NotFound", "429", "quota", "ResourceExhausted", "503", "demand", "ServiceUnavailable", "500"]):
                        break
                    else:
                        time.sleep(1)

        return {"error": f"모든 AI 모델 호출 실패. 최종에러: {last_error}"}


# ==========================================
# 4단계: 전송 & 배포 (Delivery)
# ==========================================
def send_slack_message(webhook_url, text):
    if not webhook_url:
        return False
    try:
        response = requests.post(webhook_url, json={"text": text}, timeout=5)
        return response.status_code == 200
    except Exception as e:
        logger.error(f"Slack 발송 에러: {e}")
        return False

def send_telegram_message(bot_token, chat_id, text):
    if not bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        response = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=5)
        return response.status_code == 200
    except Exception as e:
        logger.error(f"Telegram 발송 에러: {e}")
        return False

def send_discord_message(webhook_url, text):
    if not webhook_url:
        return False
    try:
        chunks = [text[i:i+1900] for i in range(0, len(text), 1900)] if len(text) > 1900 else [text]
        success = True
        for chunk in chunks:
            response = requests.post(webhook_url, json={"content": chunk}, timeout=5)
            if response.status_code not in [200, 204]:
                success = False
            time.sleep(0.5)
        return success
    except Exception as e:
        logger.error(f"Discord 발송 에러: {e}")
        return False


# ==========================================
# 카테고리별 배지 색상 매핑
# ==========================================
_BADGE_COLORS = {
    "거시 경제 & 주요 지표": ("#059669", "📊"),
    "주요 기업 동향":       ("#2563EB", "🏢"),
    "AX · RX · 디지털 트윈 & 로보틱스": ("#6366F1", "🤖"),
    "국제 정세":            ("#475569", "🌍"),
    "국내 정치":            ("#334155", "🏛️"),
    "스포츠":               ("#EA580C", "⚽"),
}

def _get_badge_style(category):
    """카테고리명에 맞는 배지 색상과 아이콘을 반환합니다."""
    if category in _BADGE_COLORS:
        return _BADGE_COLORS[category]
    # 부분 매칭 폴백
    for key, val in _BADGE_COLORS.items():
        if any(k in category for k in key.split()):
            return val
    return ("#475569", "📌")


def format_briefing_to_html(briefing_data, indicators=None):
    """
    브리핑 데이터를 프리미엄 디자인의 완전한 HTML 이메일 문서로 변환합니다.
    카테고리별 배지, 경제 지표 테이블, 원문 링크를 포함한 통합 뷰를 생성합니다.
    """
    title = briefing_data.get("title", "오늘의 일일 브리핑")
    sections = briefing_data.get("sections", [])
    closing = briefing_data.get("closing_comment", "")
    today_str = datetime.now().strftime("%Y년 %m월 %d일")
    weekday_kr = ["월", "화", "수", "목", "금", "토", "일"]
    today_weekday = weekday_kr[datetime.now().weekday()]

    parts = []

    # ── HTML 시작 ──
    parts.append(f"""<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin: 0; padding: 0; background-color: #F4F6F8; font-family: 'Apple SD Gothic Neo', 'Noto Sans KR', 'Malgun Gothic', Arial, sans-serif; -webkit-font-smoothing: antialiased;">

<!-- 외부 컨테이너 -->
<div style="max-width: 640px; margin: 20px auto; background-color: #FFFFFF; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); overflow: hidden;">

  <!-- ===== 헤더 ===== -->
  <div style="background: linear-gradient(135deg, #1E293B 0%, #334155 100%); padding: 32px 28px; text-align: center;">
    <h1 style="color: #FFFFFF; font-size: 21px; margin: 0 0 10px 0; font-weight: 700; line-height: 1.4;">📢 {title}</h1>
    <span style="display: inline-block; background-color: rgba(255,255,255,0.15); color: #CBD5E1; font-size: 13px; padding: 5px 16px; border-radius: 20px; letter-spacing: 0.3px;">{today_str} ({today_weekday})</span>
  </div>

  <!-- ===== 본문 영역 ===== -->
  <div style="padding: 28px 24px;">""")

    # ── 경제 지표 테이블 ──
    if indicators:
        parts.append("""
    <!-- 경제 지표 섹션 -->
    <div style="margin-bottom: 28px;">
      <div style="display: inline-block; background-color: #059669; color: #FFFFFF; font-size: 12px; font-weight: 700; padding: 4px 12px; border-radius: 4px; margin-bottom: 14px; letter-spacing: 0.5px;">📈 글로벌 주요 경제 지표</div>
      <table style="width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 4px;">
        <tr style="background-color: #F8FAFC; border-bottom: 2px solid #E2E8F0;">
          <th style="padding: 10px 8px; text-align: left; color: #64748B; font-weight: 600; font-size: 12px;">지표</th>
          <th style="padding: 10px 8px; text-align: right; color: #64748B; font-weight: 600; font-size: 12px;">현재가</th>
          <th style="padding: 10px 8px; text-align: right; color: #64748B; font-weight: 600; font-size: 12px;">전일비</th>
          <th style="padding: 10px 8px; text-align: right; color: #64748B; font-weight: 600; font-size: 12px;">등락률</th>
        </tr>""")
        for name, val in indicators.items():
            price_str = f"{val['price']:,.2f}"
            change = val['change']
            pct = val['pct']
            if change > 0:
                color = "#DC2626"; arrow = "▲"
            elif change < 0:
                color = "#2563EB"; arrow = "▼"
            else:
                color = "#64748B"; arrow = "-"
            parts.append(f"""
        <tr style="border-bottom: 1px solid #F1F5F9;">
          <td style="padding: 10px 8px; font-weight: 600; color: #1E293B;">{name}</td>
          <td style="padding: 10px 8px; text-align: right; font-weight: 700; color: #1E293B;">{price_str}</td>
          <td style="padding: 10px 8px; text-align: right; color: {color}; font-weight: 700;">{arrow} {abs(change):,.2f}</td>
          <td style="padding: 10px 8px; text-align: right; color: {color}; font-weight: 700;">{pct:+.2f}%</td>
        </tr>""")
        parts.append("""
      </table>
    </div>
    <hr style="border: 0; border-top: 1px solid #E2E8F0; margin: 0 0 24px 0;">""")

    # ── 카테고리별 섹션 ──
    for sec in sections:
        category = sec.get("category", "기타")
        items = sec.get("items", [])
        if not items:
            continue

        badge_color, badge_icon = _get_badge_style(category)

        parts.append(f"""
    <!-- 섹션: {category} -->
    <div style="margin-bottom: 28px;">
      <div style="display: inline-block; background-color: {badge_color}; color: #FFFFFF; font-size: 12px; font-weight: 700; padding: 4px 12px; border-radius: 4px; margin-bottom: 14px; letter-spacing: 0.5px;">{badge_icon} {category}</div>""")

        for item in items:
            headline = item.get("headline", "")
            summary = (item.get("summary", "") or "").replace("\n", "<br>")
            impact = item.get("impact", "")
            source_url = item.get("source_url", "")

            parts.append(f"""
      <div style="margin-bottom: 14px; padding: 16px; background-color: #F8FAFC; border-radius: 8px; border-left: 3px solid {badge_color};">
        <div style="font-size: 15px; font-weight: 700; color: #1E293B; margin-bottom: 8px; line-height: 1.4;">{headline}</div>
        <div style="font-size: 14px; color: #475569; line-height: 1.7; margin-bottom: 8px;">{summary}</div>""")

            if impact:
                parts.append(f"""
        <div style="font-size: 13px; color: {badge_color}; font-weight: 600; margin-bottom: 8px;">💡 {impact}</div>""")

            if source_url:
                parts.append(f"""
        <a href="{source_url}" target="_blank" style="font-size: 12px; color: #6366F1; text-decoration: none; font-weight: 500;">관련 기사 보기 →</a>""")

            parts.append("""
      </div>""")

        parts.append("""
    </div>""")

    # ── 마무리 코멘트 ──
    if closing:
        closing_html = closing.replace("\n", "<br>")
        parts.append(f"""
    <hr style="border: 0; border-top: 1px solid #E2E8F0; margin: 24px 0;">
    <p style="font-size: 14px; color: #64748B; font-style: italic; line-height: 1.7; text-align: center; margin: 0;">{closing_html}</p>""")

    # ── 푸터 ──
    parts.append(f"""
  </div>

  <!-- ===== 푸터 ===== -->
  <div style="background-color: #F8FAFC; padding: 18px 24px; text-align: center; border-top: 1px solid #E2E8F0;">
    <p style="font-size: 11px; color: #94A3B8; margin: 0; line-height: 1.5;">Daily Intelligence Agent · Powered by Gemini AI<br>{today_str} ({today_weekday}) 자동 생성</p>
  </div>

</div>
</body>
</html>""")

    return "\n".join(parts)


def send_email(title, html_body):
    """
    SMTP 서버를 통해 개인 수신 이메일로 뉴스레터 브리핑을 발송합니다.
    """
    smtp_server = _clean_env_val(os.environ.get("SMTP_SERVER")) or "smtp.gmail.com"
    try:
        smtp_port = int(_clean_env_val(os.environ.get("SMTP_PORT")) or "587")
    except Exception:
        smtp_port = 587
    
    sender_email = _clean_env_val(os.environ.get("SENDER_EMAIL") or os.environ.get("SMTP_SENDER"))
    sender_password = _clean_env_val(os.environ.get("SENDER_PASSWORD") or os.environ.get("SMTP_PASSWORD"))
    receiver_email = _clean_env_val(os.environ.get("RECEIVER_EMAIL") or os.environ.get("SMTP_RECEIVER"))
    
    if not sender_email or not sender_password:
        logger.warning("SMTP 이메일 계정 정보(SENDER_EMAIL / SENDER_PASSWORD)가 설정되어 있지 않아 발송을 건너뜁니다.")
        return False
        
    # 수신자 메일이 누락된 경우, 발송인 자신에게 보내도록 안전하게 폴백(Fallback) 설정
    if not receiver_email:
        receiver_email = sender_email
        logger.info(f"RECEIVER_EMAIL이 비어 있어 발송자 계정({sender_email})으로 수신 메일을 발송합니다.")
        
    # 쉼표(,) 구분자로 여러 명 수신 지원
    recipients = [r.strip() for r in receiver_email.split(",") if r.strip()]
        
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📬 [Daily Briefing] {today_str} 모닝 인텔리전스 리포트 - {title}"
    msg["From"] = f"Daily Intelligence Agent <{sender_email}>"
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=10)
        else:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=10)
            server.starttls()
            
        with server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, recipients, msg.as_string())
            logger.info(f"✅ 수신 이메일 발송 완료 -> {', '.join(recipients)}")
        return True
    except Exception as e:
        logger.error(f"❌ 이메일 발송 실패: {e}")
        return False


# ==========================================
# 5단계: 자동화 메인 실행부 (Runner)
# ==========================================
def main():
    logger.info("========================================")
    logger.info("Daily Briefing Standalone Agent 기동")
    logger.info("========================================")

    # 설정 로드
    gemini_api_key = _clean_env_val(os.getenv("GEMINI_API_KEY"))
    naver_client_id = _clean_env_val(os.getenv("NAVER_CLIENT_ID"))
    naver_client_secret = _clean_env_val(os.getenv("NAVER_CLIENT_SECRET"))
    
    keywords_str = _clean_env_val(os.getenv("NEWS_KEYWORDS", "인공지능, 빅테크, IT 트렌드, 경제 증시, 국제 뉴스, 국내 정치, 스포츠"))
    keywords = [k.strip() for k in keywords_str.split(",") if k.strip()]
    
    slack_webhook = _clean_env_val(os.getenv("SLACK_WEBHOOK_URL"))
    telegram_token = _clean_env_val(os.getenv("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id = _clean_env_val(os.getenv("TELEGRAM_CHAT_ID"))
    discord_webhook = _clean_env_val(os.getenv("DISCORD_WEBHOOK_URL"))

    if not gemini_api_key:
        logger.error("GEMINI_API_KEY 환경변수가 정의되어 있지 않아 에이전트를 종료합니다.")
        sys.exit(1)

    # 1단계 기사 수집
    logger.info(f"1단계: 수집 대상 키워드 - {keywords}")
    articles = collect_all_news(keywords, naver_client_id, naver_client_secret)
    if not articles:
        logger.warning("수집된 뉴스 기사가 없어 종료합니다.")
        return

    # 2단계 중복 필터링
    logger.info("2단계: 유사도 그룹 분석 및 중복 제거 진행...")
    unique_articles = cluster_and_deduplicate_articles(articles, similarity_threshold=0.25)
    if not unique_articles:
        logger.warning("중복 제거 후 분석할 뉴스 기사가 없어 종료합니다.")
        return

    # 3단계 경제 지표 수집
    logger.info("3단계: 경제 지표 수집 시작...")
    indicators = get_economic_indicators()
    if indicators:
        logger.info(f"경제 지표 수집 완료: {len(indicators)}개 지표")
    else:
        logger.warning("경제 지표 수집 실패 - 지표 없이 브리핑을 진행합니다.")

    # 4단계 AI 브리핑 생성
    logger.info("4단계: Gemini API를 사용하여 카테고리별 브리핑 생성 시작...")
    ai = AIEngine(gemini_api_key)
    briefing = ai.generate_briefing(unique_articles[:20], indicators)
    
    if "error" in briefing:
        logger.error(f"브리핑 생성 실패: {briefing['error']}")
        return

    logger.info(f"AI 브리핑 생성 성공: '{briefing.get('title')}'")
    section_names = [s.get("category", "?") for s in briefing.get("sections", [])]
    logger.info(f"생성된 섹션: {', '.join(section_names)}")

    # 5단계 전송
    sns_text = briefing.get("short_summary_for_sns", "")
    title = briefing.get("title", "오늘의 일일 브리핑")
    
    sent_list = []
    
    if slack_webhook and sns_text:
        if send_slack_message(slack_webhook, f"📢 *{title}*\n\n{sns_text}"):
            sent_list.append("Slack")
            
    if telegram_token and telegram_chat_id and sns_text:
        if send_telegram_message(telegram_token, telegram_chat_id, f"📢 {title}\n\n{sns_text}"):
            sent_list.append("Telegram")
            
    if discord_webhook and sns_text:
        if send_discord_message(discord_webhook, f"📢 **{title}**\n\n{sns_text}"):
            sent_list.append("Discord")
            
    # 이메일 발송 (프리미엄 HTML 통합 뷰)
    html_body = format_briefing_to_html(briefing, indicators)
    if send_email(title, html_body):
        sent_list.append("Email")

    if sent_list:
        logger.info(f"배포 성공 채널 목록: {', '.join(sent_list)}")
    else:
        logger.warning("전송 설정된 배포 채널이 없어 발송되지 않았습니다.")
        
    logger.info("Daily Briefing Standalone Agent 업무 종료.")

if __name__ == "__main__":
    main()
