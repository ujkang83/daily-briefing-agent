import os
import sys

# Windows 콘솔 한글 및 이모지 출력 인코딩 오류 방지
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import io
import re
import html
import json
import time
import logging
import urllib.parse
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage

# 한국 표준시 (KST) 타임존 설정
KST = timezone(timedelta(hours=9))

import requests
import feedparser
from google import genai
from google.genai import types
from PIL import Image, ImageDraw, ImageFont
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
    'gemini-3.5-flash-lite',
    'gemini-flash-latest',
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
    """Google News RSS를 통해 기사를 수집합니다. 하루 이내의 최신 기사만 수집하기 위해 when:1d 필터를 적용합니다."""
    encoded_keyword = urllib.parse.quote(f"{keyword} when:1d")
    rss_url = f"https://news.google.com/rss/search?q={encoded_keyword}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        feed = feedparser.parse(rss_url)
        articles = []
        for entry in feed.entries[:limit]:
            title = clean_google_title(entry.title)
            desc = clean_html(entry.get("summary", ""))
            source_name = entry.get("source", {}).get("title", "Google News")
            articles.append({
                "title": title,
                "link": entry.link,
                "description": desc or title,
                "source": source_name,
                "pub_date": entry.get("published", "")
            })
        return articles
    except Exception as e:
        logger.error(f"Google News RSS 수집 에러 ({keyword}): {e}")
        return []

def collect_naver_news(keyword, client_id, client_secret, limit=20):
    """네이버 뉴스 검색 API를 통해 기사를 수집합니다. 최신순으로 정렬하기 위해 sort=date를 적용합니다."""
    if not client_id or not client_secret:
        return []
    encoded_keyword = urllib.parse.quote(keyword)
    url = f"https://openapi.naver.com/v1/search/news.json?query={encoded_keyword}&display={limit}&sort=date"
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
            representative["related_articles"] = [
                {"title": x.get("title", ""), "link": x.get("link", ""), "source": x.get("source", "Google News")}
                for x in cluster if x.get("link", "") != representative.get("link", "")
            ]
            unique_articles.append(representative)
            
        logger.info(f"중복 뉴스 정제 완료: {len(articles)}개 -> {len(unique_articles)}개 뉴스 그룹 도출")
        return unique_articles
    except Exception as e:
        logger.error(f"뉴스 유사도 정제 처리 중 에러 발생: {e}")
        return articles


# ==========================================
# 3단계: 요약 & 분석 (AI Engine — google.genai SDK)
# ==========================================
# ==========================================
# 3단계: 요약 & 분석 (AI Engine — google.genai SDK)
# ==========================================
class RelatedCompany(BaseModel):
    name: str = Field(description="관련 기업명 (예: SK하이닉스, 엔비디아, 현대차, 한화에어로스페이스 등)")
    ticker: Optional[str] = Field(default="", description="종목 코드 또는 글로벌 티커 (예: 000660, NVDA, 비상장 등)")
    relevance: str = Field(description="해당 기업이 이 이슈/정책/기술과 왜 직접적으로 관련 있는지, 실질적인 수혜/리스크/비즈니스 연관성을 1~2문장으로 명확히 분석")

class BriefingItem(BaseModel):
    headline: str = Field(description="핵심 헤드라인 (단순 기사 제목 복사가 아닌 사건의 본질과 구조적 파급력을 압축한 지적 헤드라인)")
    summary: str = Field(description="핵심 팩트 요약 (무슨 일이 일어났는지 핵심 팩트를 1~2문장으로 명확히 전달)")
    impact: str = Field(description="심층 인사이트 및 파급효과 ('Why It Matters & So What?' - 산업 밸류체인, 시장 가격, 기업 실적에 미칠 실질적 영향 및 구조적 시사점을 2문장 내외로 날카롭게 분석. 상투적 표현 절대 금지)")
    related_companies: List[RelatedCompany] = Field(default_factory=list, description="해당 뉴스와 직접적인 수혜/피해/공급망 등 명확하고 구체적인 기업 연관성이 있는 경우에만 작성 (최대 1~2개). 근거가 약하거나 정치/사회/스포츠 등 기업 연관성이 없는 일반 뉴스는 억지로 끼워 넣지 말고 반드시 빈 리스트([])로 작성")
    source_url: str = Field(description="제공된 뉴스 기사 목록의 실제 원문 링크 URL (경제 지표 요약 항목은 빈 문자열). 가짜 URL이나 포털/리그 대표 도메인은 절대 사용 금지")
    source_name: str = Field(description="제공된 뉴스 기사 목록의 실제 언론사 이름 (예: 연합뉴스, 한국경제 등). 경제 지표 요약 항목은 빈 문자열")

class BriefingSection(BaseModel):
    category: str = Field(description="카테고리명 (거시 경제 & 주요 지표, 주요 기업 동향, AX · RX · 디지털 트윈 & 로보틱스, 국제 정세, 국내 정치, 스포츠 중 하나)")
    items: List[BriefingItem]

class DailyBriefing(BaseModel):
    title: str = Field(description="브리핑 전체 제목 (예: 2026년 9월 7일 모닝 인텔리전스 리포트)")
    daily_summary: str = Field(description="오늘 글로벌 시장과 산업 전체를 관통하는 핵심 총평 단 1문장 (최상단 하이라이트)")
    executive_insights: List[str] = Field(description="오늘 하루 전체 뉴스를 종합 분석하여 도출한 3대 핵심 전략적 관전 포인트 (Executive Strategic Insights 3개 항목)")
    key_watchlist_companies: List[str] = Field(default_factory=list, description="오늘 브리핑의 경제/산업/기술 기사 중 실질적으로 주요한 영향을 받는 핵심 기업 2~4개 이름 (예: ['SK하이닉스', '현대차']. 근거가 없으면 무리하게 채우지 말 것)")
    image_prompt: Optional[str] = Field(default="", description="오늘의 핵심 테마를 표현하는 영문 이미지 생성 프롬프트")
    sections: List[BriefingSection]
    closing_comment: Optional[str] = Field(default="", description="전문가적 관점의 향후 관전 포인트 및 마무리 코멘트")
    short_summary_for_sns: str = Field(description="전체 브리핑 200자 내외 요약 (모바일/메신저 전송용)")


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
                self._models_cache = [m.name.replace('models/', '') for m in self.client.models.list() if hasattr(m, 'name')]
            except Exception as e:
                logger.warning(f"모델 리스트 동적 조회 실패: {e}")
                self._models_cache = []

        available = set(self._models_cache)
        candidates = []
        for p in _PRIORITY_MODELS:
            clean_p = p.replace('models/', '')
            if not available or clean_p in available:
                candidates.append(clean_p)
        # 신규 출시된 Gemini 모델 동적 추가
        for am in self._models_cache:
            if am not in candidates and ('flash' in am or 'pro' in am) and 'image' not in am and 'tts' not in am and 'preview' not in am:
                candidates.append(am)
        return candidates or ['gemini-3.7-flash', 'gemini-3.6-flash', 'gemini-1.5-flash']

    def generate_briefing(self, articles, indicators=None, additional_notes="", max_retries=2):
        if not self.client:
            return {"error": "API Client가 초기화되지 않았습니다."}

        # 오늘 날짜를 KST 기준으로 구해서 프롬프트에 제공
        from datetime import datetime, timezone, timedelta
        KST_local = timezone(timedelta(hours=9))
        today_str = datetime.now(KST_local).strftime("%Y년 %m월 %d일")

        articles_text = ""
        for idx, art in enumerate(articles):
            articles_text += f"[{idx+1}] 제목: {art['title']}\n출처: {art['source']}\n링크: {art['link']}\n설명: {art['description']}\n\n"

        indicators_text = ""
        if indicators:
            indicators_text = "현재 주요 경제 지표:\n"
            for name, val in indicators.items():
                indicators_text += f"- {name}: {val['price']:,.2f} (전일비 {val['change']:+,.2f}, {val['pct']:+.2f}%)\n"
            indicators_text += "\n"

        prompt = f"""당신은 글로벌 탑티어 전략 컨설팅 펌(맥킨지, BCG) 및 최고급 투자기관의 [수석 경제·산업 전략 애널리스트(Chief Strategy Analyst)]입니다.
오늘 날짜는 {today_str}입니다. 반드시 오늘 날짜({today_str}) 기준으로 브리핑 전체 제목과 최고 수준의 전략 인텔리전스 리포트를 JSON 형식으로 작성해 주세요.

당신의 핵심 임무는 단순한 뉴스 팩트 요약(받아쓰기)이 아닙니다.
개별 사건 이면에 숨겨진 구조적 변화(Why It Matters), 산업 밸류체인 및 시장에 미칠 실질적 파급효과(So What?), 그리고 직접적으로 연관된 핵심 기업(수혜주, 피해주, 공급망 파트너)의 비즈니스적 인과관계를 날카롭고 깊이 있게 도출하는 것입니다.

{indicators_text}[뉴스 기사 목록]
{articles_text}
{additional_notes}

[카테고리 분류 규칙]
반드시 다음 6개 카테고리로 분류하여 작성하세요:
1. "거시 경제 & 주요 지표" - 경제, 금융, 환율, 주식시장, 금리, 부동산, 물가, 통화정책 등 주요 경제 기사 (경제 지표 요약 외에도 실질적인 경제 관련 뉴스 기사 포함)
2. "주요 기업 동향" - 기업 실적 발표, M&A, 투자, 신사업, 경영 전략 관련(해외 IT 빅테크 및 국내 주요 대기업)
3. "AX · RX · 디지털 트윈 & 로보틱스" - AI 기술, 로봇, 디지털 트윈, 피지컬 AI, 자동화, 신기술 적용 사례 관련
4. "국제 정세" - 해외 정치, 외교, 무역 갈등, 지정학적 이슈 관련
5. "국내 정치" - 국내 정책, 입법, 주요 정치 현안 관련
6. "스포츠" - 국내외 스포츠(프로야구 KBO, 해외축구, 메이저리그 MLB, 골프 등)의 최신 경기 결과, 실제 스코어, 주요 선수 활약상 등 (제공된 실제 스포츠 기사 내용만 포함)

[심층 인사이트 및 엄격한 작성 지침]
1. ★철저한 기사 팩트 기반 작성 (환각/과거 기억 기반 날조 절대 금지)★:
   - AI 모델의 과거 학습 데이터(과거 특정 시점의 선수 소속팀, 구단 정보, 과거 사건 등)에 의존하여 추측하거나 지어내지 마십시오. (예: 과거 소속팀 기준 정보 서술 금지)
   - 반드시 제공된 [뉴스 기사 목록]에 명시된 최신 팩트(선수 이름, 실제 현재 소속팀, 경기 스코어, 일시, 사건 경과)만을 바탕으로 작성하십시오.
   - 기사에 명시되지 않은 사실을 임의로 상상하여 작성하거나, 오래된 과거 사실을 현재 진행형으로 단정짓는 행위를 절대 엄금합니다.

2. ★원문 기사 출처 및 링크(source_url, source_name) 매핑 엄수★:
   - 모든 기사 항목의 `source_url`과 `source_name`은 반드시 제공된 [뉴스 기사 목록]의 실제 `링크`와 `출처`를 정확히 그대로 매핑하십시오.
   - 뉴스 목록에 없는 링크나 포털/리그 대표 공식 홈페이지(예: koreabaseball.com, premierleague.com, mlb.com, naver.com 등) 같은 무의미한 일반 도메인 URL을 임의로 생성하는 행위는 절대 금지합니다.
   - 오직 수집된 실제 뉴스 기사에서 다룬 내용만 브리핑 아이템으로 작성하십시오.

3. ★관련 기업 분석(related_companies) - 억지 매칭 절대 금지★:
   - 기업 실적, 공급망(밸류체인), 주가/수혜/피해 등 구체적이고 명확한 비즈니스 인과관계가 확실한 경제/산업/기업/기술 뉴스에만 관련 기업을 작성하십시오.
   - 근거가 미약하거나 간접적인 추정에 불과한 경우, 또는 스포츠, 일반 정치/사회 뉴스처럼 기업과 직접 연관이 없는 기사는 **절대로 억지로 기업을 끼워 넣지 말고 반드시 빈 리스트(`[]`)**로 작성하십시오.

4. ★상투적이고 무의미한 표현 절대 금지★:
   - "기대된다", "주목된다", "관심이 쏠린다", "경쟁력 강화가 예상된다", "귀추가 주목된다" 같은 진부한 클리셰 문구는 절대 쓰지 마십시오.
   - 대신 "원인 -> 구조적 메커니즘 -> 밸류체인/가격/실적에 미치는 구체적 영향"의 인과관계를 논리적으로 기술하십시오.

5. ★최상단 3대 핵심 전략 인사이트(executive_insights) & 주목 기업(key_watchlist_companies)★:
   - `executive_insights`는 오늘 하루 뉴스 전체를 가로지르는 3대 거시적/산업적 관전 포인트(Trend & Structural Shift)를 전문 애널리스트 관점에서 깊이 있게 제시하십시오.
   - `key_watchlist_companies`는 오늘 브리핑의 경제/산업/기술 뉴스에서 실질적으로 주요한 영향을 받는 핵심 기업 2~4개 이름을 배열로 제시하십시오. (근거가 빈약하면 억지로 많이 채우지 마십시오.)

6. ★카테고리 구성 및 기사 수★:
   - 제공된 뉴스 기사 목록을 꼼꼼히 분류하여 각 카테고리별로 알차게 구성하세요.
   - 기사가 충분한 카테고리는 2~4개 아이템을 작성하고, 기사가 적은 카테고리는 실제 기사 수만큼만 작성하되 절대 임의로 거짓 기사를 지어내지 마십시오.
   - "거시 경제 & 주요 지표" 카테고리는 제공된 경제 지표 요약(첫 번째 아이템, source_url은 빈 문자열) 외에도 실질적인 경제 기사를 포함하여 구성하세요.
   - "스포츠" 카테고리는 제공된 뉴스 기사에 포함된 실제 최신 경기 결과, 스코어(점수/승패), 핵심 선수 활약상 등 팩트 중심으로 작성하세요.
   - 같은 사건에 대한 중복 기사는 하나로 통합하고, 가장 대표적인 원문 링크를 사용하세요.

응답은 반드시 아래 JSON 스키마를 따르며, 마크다운 코드 블록 없이 순수 JSON만 출력하세요:

{{
  "title": "String (브리핑 전체 제목)",
  "daily_summary": "String (오늘 브리핑 전체를 관통하는 핵심 총평 1문장)",
  "executive_insights": [
    "String (오늘의 1번째 핵심 전략 인사이트 - 맥락과 파급효과)",
    "String (오늘의 2번째 핵심 전략 인사이트 - 밸류체인 및 시장 변화)",
    "String (오늘의 3번째 핵심 전략 인사이트 - 향후 전개 시나리오)"
  ],
  "key_watchlist_companies": ["String (핵심 주목 기업 1)", "String (핵심 주목 기업 2)"],
  "image_prompt": "String (오늘의 테마를 표현하는 영문 이미지 생성 프롬프트)",
  "sections": [
    {{
      "category": "String (카테고리명 - 위 6개 중 정확히 일치하는 이름 사용)",
      "items": [
        {{
          "headline": "String (핵심 요약 제목)",
          "summary": "String (1~2문장 핵심 팩트 요약)",
          "impact": "String (심층 인사이트 및 밸류체인/실적 파급효과 2문장 내외)",
          "related_companies": [
            {{
              "name": "String (기업명)",
              "ticker": "String (티커/종목코드)",
              "relevance": "String (해당 기업과의 구체적 연관성 및 수혜/리스크 분석 1~2문장)"
            }}
          ],
          "source_url": "String (원문 기사 URL, 경제 지표 요약 항목은 빈 문자열)",
          "source_name": "String (출처 언론사 이름, 예: 연합뉴스, 한국경제 등. 경제 지표 요약 항목은 빈 문자열)"
        }}
      ]
    }}
  ],
  "closing_comment": "String (전문가적 관점의 마무리 총평 1문장)",
  "short_summary_for_sns": "String (200자 내외 SNS 요약)"
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


# ==========================================
# 3.5단계: 일일 요약 이미지 생성기 (AI Image Generator)
# ==========================================
def generate_summary_image(ai_engine, briefing_data, indicators=None):
    """
    Gemini AI 모델을 사용하여 일일 요약 이미지를 생성합니다.
    AI 이미지 생성이 불가능하거나 오류 발생 시 None을 반환합니다 (인포그래픽 제외).
    """
    image_prompt = briefing_data.get("image_prompt", "")

    if not ai_engine or not ai_engine.client or not image_prompt:
        return None

    logger.info("AI 일일 요약 이미지 생성 시도...")
    candidates = ['gemini-2.5-flash-image', 'gemini-3.1-flash-image', 'gemini-3-pro-image']
    
    for model_name in candidates:
        try:
            logger.info(f"Gemini 이미지 생성 시도: {model_name}")
            resp = ai_engine.client.models.generate_content(
                model=model_name,
                contents=image_prompt
            )
            if resp and resp.candidates and resp.candidates[0].content and resp.candidates[0].content.parts:
                for part in resp.candidates[0].content.parts:
                    if hasattr(part, 'inline_data') and part.inline_data and part.inline_data.data:
                        logger.info(f"Gemini AI 생성 이미지 획득 성공 ({model_name})")
                        img_bytes = part.inline_data.data
                        try:
                            with open("daily_summary_latest.jpg", "wb") as f:
                                f.write(img_bytes)
                        except Exception:
                            pass
                        return img_bytes
        except Exception as e:
            logger.warning(f"Gemini 이미지 모델({model_name}) 생성 실패: {e}")
            break

    logger.info("AI 이미지 생성 미지원/실패로 이미지 없이 브리핑을 진행합니다.")
    return None


def format_briefing_to_html(briefing_data, indicators=None, has_image=False):
    """
    브리핑 데이터를 최고급 전략 인텔리전스 리포트 HTML 이메일 문서로 변환합니다.
    - 최상단: 브리핑 헤더 & 핵심 총평 & 오늘의 3대 전략 인사이트 & 핵심 주목 기업 (Watchlist)
    - 글로벌 주요 경제 지표 테이블
    - 6대 카테고리별 섹션: 팩트 요약, 심층 인사이트(Why It Matters), 관련 기업 & 밸류체인 분석, 원문 링크
    """
    title = briefing_data.get("title", "오늘의 일일 브리핑")
    daily_summary = briefing_data.get("daily_summary") or briefing_data.get("short_summary_for_sns") or ""
    executive_insights = briefing_data.get("executive_insights", [])
    watchlist_companies = briefing_data.get("key_watchlist_companies", [])
    sections = briefing_data.get("sections", [])
    closing = briefing_data.get("closing_comment", "")
    
    now_kst = datetime.now(KST)
    today_str = now_kst.strftime("%Y년 %m월 %d일")
    weekday_kr = ["월", "화", "수", "목", "금", "토", "일"]
    today_weekday = weekday_kr[now_kst.weekday()]

    parts = []

    # ── HTML 시작 ──
    parts.append(f"""<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin: 0; padding: 0; background-color: #F1F5F9; font-family: 'Apple SD Gothic Neo', 'Noto Sans KR', 'Malgun Gothic', Arial, sans-serif; -webkit-font-smoothing: antialiased;">

<!-- 외부 컨테이너 -->
<div style="max-width: 660px; margin: 20px auto; background-color: #FFFFFF; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.08); overflow: hidden;">

  <!-- ===== 헤더 ===== -->
  <div style="background: linear-gradient(135deg, #0F172A 0%, #1E293B 100%); padding: 32px 24px; text-align: center;">
    <div style="display: inline-block; background-color: rgba(99, 102, 241, 0.25); color: #C7D2FE; font-size: 11px; font-weight: 700; padding: 4px 12px; border-radius: 20px; margin-bottom: 10px; letter-spacing: 0.5px; border: 1px solid rgba(165, 180, 252, 0.3);">EXECUTIVE STRATEGIC INTELLIGENCE</div>
    <h1 style="color: #FFFFFF; font-size: 21px; margin: 0 0 10px 0; font-weight: 700; line-height: 1.4;">📢 {title}</h1>
    <span style="display: inline-block; color: #94A3B8; font-size: 13px; letter-spacing: 0.3px;">{today_str} ({today_weekday})</span>
  </div>

  <!-- ===== 본문 영역 ===== -->
  <div style="padding: 24px 20px;">""")

    # ── 1. 일일 요약 이미지 (생성된 경우만) ──
    if has_image:
        parts.append("""
    <div style="margin-bottom: 22px; text-align: center;">
      <img src="cid:summary_image" alt="오늘의 일일 브리핑 요약" style="width: 100%; max-width: 620px; height: auto; border-radius: 10px; display: block; box-shadow: 0 4px 12px rgba(0,0,0,0.08); margin: 0 auto;" />
    </div>""")

    # ── 2. 오늘의 핵심 브리핑 총평 & 3대 전략 인사이트 (최상단) ──
    if daily_summary or executive_insights:
        summary_html = daily_summary.replace("\n", "<br>") if daily_summary else ""
        margin_b = "14px" if executive_insights else "0"
        parts.append(f"""
    <!-- 전략적 인텔리전스 총평 카드 -->
    <div style="margin-bottom: 26px; padding: 20px 22px; background: linear-gradient(135deg, #EEF2FF 0%, #E0E7FF 100%); border-radius: 12px; border-left: 5px solid #6366F1; box-shadow: 0 2px 8px rgba(99, 102, 241, 0.08);">
      <div style="font-size: 12px; font-weight: 700; color: #4F46E5; margin-bottom: 6px; letter-spacing: 0.5px;">🎯 오늘의 핵심 브리핑 총평 (CORE THESIS)</div>
      <div style="font-size: 15px; font-weight: 700; color: #1E293B; line-height: 1.6; margin-bottom: {margin_b};">{summary_html}</div>""")

        if executive_insights:
            parts.append("""
      <div style="padding-top: 12px; border-top: 1px solid rgba(99, 102, 241, 0.2);">
        <div style="font-size: 12px; font-weight: 700; color: #4338CA; margin-bottom: 8px;">💡 오늘의 3대 전략적 관전 포인트 (KEY STRATEGIC INSIGHTS)</div>""")
            for idx, insight in enumerate(executive_insights[:3]):
                clean_insight = str(insight).strip()
                parts.append(f"""
        <div style="font-size: 13px; color: #334155; line-height: 1.5; margin-bottom: 6px;">
          <span style="font-weight: 700; color: #6366F1;">⚡ [{idx+1}]</span> {clean_insight}
        </div>""")
            parts.append("""
      </div>""")

        # 핵심 주목 기업 배지 (Watchlist)
        if watchlist_companies:
            badges_html = " ".join([f'<span style="display: inline-block; background-color: #FFFFFF; color: #312E81; font-size: 11px; font-weight: 700; padding: 3px 9px; border-radius: 6px; margin: 3px 4px 3px 0; border: 1px solid #C7D2FE; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">{c}</span>' for c in watchlist_companies[:5]])
            parts.append(f"""
      <div style="margin-top: 12px; padding-top: 10px; border-top: 1px dashed rgba(99, 102, 241, 0.2);">
        <div style="font-size: 11px; font-weight: 700; color: #4338CA; margin-bottom: 6px;">🔥 오늘의 핵심 주목 기업 (KEY WATCHLIST)</div>
        <div>{badges_html}</div>
      </div>""")

        parts.append("""
    </div>""")

    # ── 3. 글로벌 주요 경제 지표 테이블 ──
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

    # ── 4. 카테고리별 섹션 ──
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
            related_companies = item.get("related_companies", [])
            source_url = item.get("source_url", "")
            source_name = item.get("source_name", "")

            parts.append(f"""
      <div style="margin-bottom: 16px; padding: 16px 18px; background-color: #F8FAFC; border-radius: 8px; border-left: 3px solid {badge_color}; box-shadow: 0 1px 3px rgba(0,0,0,0.04);">
        <div style="font-size: 15px; font-weight: 700; color: #1E293B; margin-bottom: 8px; line-height: 1.4;">{headline}</div>
        <div style="font-size: 14px; color: #475569; line-height: 1.7; margin-bottom: 10px;">{summary}</div>""")

            # 심층 인사이트 박스
            if impact:
                parts.append(f"""
        <div style="margin: 10px 0; padding: 10px 12px; background-color: #F0FDF4; border-radius: 6px; border-left: 3px solid #10B981;">
          <div style="font-size: 11px; font-weight: 700; color: #047857; margin-bottom: 4px; letter-spacing: 0.3px;">💡 심층 인사이트 & 시사점 (Why It Matters)</div>
          <div style="font-size: 13px; color: #065F46; line-height: 1.6; font-weight: 500;">{impact}</div>
        </div>""")

            # 관련 기업 & 밸류체인 분석
            if related_companies:
                parts.append("""
        <div style="margin: 10px 0; padding: 10px 12px; background-color: #F1F5F9; border-radius: 6px; border: 1px solid #E2E8F0;">
          <div style="font-size: 11px; font-weight: 700; color: #2563EB; margin-bottom: 6px; letter-spacing: 0.3px;">🏢 관련 기업 & 밸류체인 분석</div>""")
                for comp in related_companies:
                    cname = comp.get("name") if isinstance(comp, dict) else getattr(comp, "name", "")
                    cticker = comp.get("ticker") if isinstance(comp, dict) else getattr(comp, "ticker", "")
                    crelevance = comp.get("relevance") if isinstance(comp, dict) else getattr(comp, "relevance", "")
                    ticker_label = f" ({cticker})" if cticker else ""
                    if cname:
                        parts.append(f"""
          <div style="font-size: 12px; color: #334155; line-height: 1.5; margin-bottom: 5px;">
            <span style="display: inline-block; background: #EEF2FF; color: #4338CA; font-weight: 700; padding: 2px 7px; border-radius: 4px; font-size: 11px; margin-right: 6px;">{cname}{ticker_label}</span>
            <span>{crelevance}</span>
          </div>""")
                parts.append("""
        </div>""")

            if source_url:
                source_label = f" ({source_name})" if source_name else ""
                parts.append(f"""
        <div style="margin-top: 8px;">
          <a href="{source_url}" target="_blank" style="font-size: 12px; color: #6366F1; text-decoration: none; font-weight: 500;">관련 기사 보기{source_label} →</a>
        </div>""")

            # 연관 기사 렌더링
            related_articles = item.get("related_articles", [])
            if related_articles:
                parts.append(f"""
        <div style="margin-top: 10px; padding-top: 8px; border-top: 1px dashed #E2E8F0;">
          <div style="font-size: 11px; color: #64748B; font-weight: 600; margin-bottom: 4px;">🔗 연관 기사</div>""")
                for rel_art in related_articles:
                    rel_title = rel_art.get("title", "")
                    rel_url = rel_art.get("link", "")
                    rel_src = rel_art.get("source", "")
                    rel_src_label = f" ({rel_src})" if rel_src else ""
                    if rel_url:
                        parts.append(f"""
          <div style="margin-bottom: 3px; font-size: 11px;">
            <a href="{rel_url}" target="_blank" style="color: #475569; text-decoration: none; font-weight: 400;">• {rel_title}{rel_src_label} →</a>
          </div>""")
                parts.append("""
        </div>""")

            parts.append("""
      </div>""")

        parts.append("""
    </div>""")

    # ── 5. 마무리 코멘트 ──
    if closing:
        closing_html = closing.replace("\n", "<br>")
        parts.append(f"""
    <div style="margin-top: 24px; padding: 14px 16px; background-color: #F8FAFC; border-radius: 8px; border: 1px solid #E2E8F0; text-align: center;">
      <div style="font-size: 11px; font-weight: 700; color: #64748B; margin-bottom: 4px;">📌 전략적 종합 관전 포인트</div>
      <p style="font-size: 13px; color: #475569; line-height: 1.6; margin: 0; font-style: italic;">{closing_html}</p>
    </div>""")

    # ── 6. 푸터 ──
    parts.append(f"""
    <hr style="border: 0; border-top: 1px solid #E2E8F0; margin: 24px 0 16px 0;">
  </div>

  <!-- ===== 푸터 ===== -->
  <div style="background-color: #F8FAFC; padding: 18px 24px; text-align: center; border-top: 1px solid #E2E8F0;">
    <p style="font-size: 11px; color: #94A3B8; margin: 0; line-height: 1.5;">Daily Strategic Intelligence Agent · Powered by Gemini AI<br>{today_str} ({today_weekday}) 자동 생성</p>
  </div>

</div>
</body>
</html>""")

    return "\n".join(parts)


def send_email(title, html_body, image_bytes=None):
    """
    SMTP 서버를 통해 개인 수신 이메일로 뉴스레터 브리핑을 발송합니다.
    일일 요약 이미지가 제공된 경우 MIME multipart/related 인라인 첨부(Content-ID: <summary_image>)로 결합합니다.
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
        
    today_str = datetime.now(KST).strftime("%Y-%m-%d")
    
    # MIME 구조: multipart/related (HTML 본문 + 인라인 첨부 이미지)
    msg_root = MIMEMultipart("related")
    msg_root["Subject"] = f"📬 [Daily Briefing] {today_str} 모닝 인텔리전스 리포트 - {title}"
    msg_root["From"] = f"Daily Intelligence Agent <{sender_email}>"
    msg_root["To"] = ", ".join(recipients)

    msg_alt = MIMEMultipart("alternative")
    msg_root.attach(msg_alt)

    msg_alt.attach(MIMEText(html_body, "html", "utf-8"))

    # 일일 요약 이미지 인라인 첨부
    if image_bytes:
        try:
            img_part = MIMEImage(image_bytes, "jpeg")
            img_part.add_header("Content-ID", "<summary_image>")
            img_part.add_header("Content-Disposition", "inline", filename="daily_summary.jpg")
            msg_root.attach(img_part)
            logger.info("이메일 인라인 요약 이미지 첨부 완료 (Content-ID: <summary_image>)")
        except Exception as e:
            logger.warning(f"이미지 MIME 첨부 실패: {e}")

    try:
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=10)
        else:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=10)
            server.starttls()
            
        with server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, recipients, msg_root.as_string())
            logger.info(f"✅ 수신 이메일 발송 완료 -> {', '.join(recipients)}")
        return True
    except Exception as e:
        logger.error(f"❌ 이메일 발송 실패: {e}")
        return False


# ==========================================
# 5단계: 자동화 메인 실행부 (Runner)
# ==========================================
def decode_news_urls(articles):
    """
    구글 뉴스 URL을 디코딩하여 국내 언론사 원래 사이트의 기사 링크로 보정합니다.
    """
    logger.info("구글 뉴스 URL 디코딩 시작...")
    try:
        from googlenewsdecoder import gnewsdecoder
    except ImportError:
        logger.warning("googlenewsdecoder 라이브러리가 임포트되지 않아 URL 디코딩을 건너뜁니다.")
        return articles

    for art in articles:
        orig_url = art.get("link", "")
        if orig_url and "news.google.com" in orig_url:
            try:
                decoded = gnewsdecoder(orig_url)
                if decoded.get("status") and decoded.get("decoded_url"):
                    art["link"] = decoded["decoded_url"]
                    logger.info(f"URL 디코딩 완료: {decoded['decoded_url']}")
            except Exception as e:
                logger.warning(f"URL 디코딩 에러 ({orig_url}): {e}")

        # 연관 기사 URL 디코딩
        for rel in art.get("related_articles", []):
            rel_url = rel.get("link", "")
            if rel_url and "news.google.com" in rel_url:
                try:
                    decoded = gnewsdecoder(rel_url)
                    if decoded.get("status") and decoded.get("decoded_url"):
                        rel["link"] = decoded["decoded_url"]
                        logger.info(f"연관 URL 디코딩 완료: {decoded['decoded_url']}")
                except Exception as e:
                    logger.warning(f"연관 URL 디코딩 에러 ({rel_url}): {e}")
    return articles


def main():
    logger.info("========================================")
    logger.info("Daily Briefing Standalone Agent 기동")
    logger.info("========================================")

    # 설정 로드
    gemini_api_key = _clean_env_val(os.getenv("GEMINI_API_KEY"))
    naver_client_id = _clean_env_val(os.getenv("NAVER_CLIENT_ID"))
    naver_client_secret = _clean_env_val(os.getenv("NAVER_CLIENT_SECRET"))
    
    keywords_str = _clean_env_val(os.getenv("NEWS_KEYWORDS", "인공지능 AI, 빅테크 IT 트렌드, 반도체 로보틱스, 거시 경제 금리 환율, 금융 증시, 부동산 시장 정책, 주요 기업 동향 실적, 국제 정세 외교, 국내 정치 현안, 프로야구 KBO 경기결과, 해외축구 경기결과, 스포츠 경기결과"))
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

    # 구글 뉴스 URL 디코딩 및 관련 기사 디코딩
    unique_articles = decode_news_urls(unique_articles)

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
    briefing = ai.generate_briefing(unique_articles[:80], indicators)
    
    if "error" in briefing:
        logger.error(f"브리핑 생성 실패: {briefing['error']}")
        return

    logger.info(f"AI 브리핑 생성 성공: '{briefing.get('title')}'")
    
    # 4.5단계: 일일 요약 이미지 생성 (AI 생성 시도 + 고화질 인포그래픽 배너 폴백)
    logger.info("4.5단계: 일일 요약 이미지 생성 시작...")
    image_bytes = generate_summary_image(ai, briefing, indicators)

    # 연관 기사 복원 및 매핑 진행
    unique_map = {art["link"]: art for art in unique_articles}
    for section in briefing.get("sections", []):
        for item in section.get("items", []):
            orig_url = item.get("source_url", "")
            if orig_url:
                matching_art = unique_map.get(orig_url)
                if matching_art:
                    item["related_articles"] = matching_art.get("related_articles", [])

    section_names = [s.get("category", "?") for s in briefing.get("sections", [])]
    logger.info(f"생성된 섹션: {', '.join(section_names)}")

    # 5단계 전송
    title = briefing.get("title", "오늘의 일일 브리핑")
    daily_summary = briefing.get("daily_summary") or ""
    sns_text = briefing.get("short_summary_for_sns", "")
    
    # 메신저 본문 구성: 1문장 핵심 요약을 최상단에 배치
    messenger_body = f"📢 *{title}*\n\n✨ *오늘의 핵심 요약:*\n{daily_summary}\n\n{sns_text}" if daily_summary else f"📢 *{title}*\n\n{sns_text}"
    
    sent_list = []
    
    if slack_webhook and sns_text:
        if send_slack_message(slack_webhook, messenger_body):
            sent_list.append("Slack")
            
    if telegram_token and telegram_chat_id and sns_text:
        if send_telegram_message(telegram_token, telegram_chat_id, messenger_body):
            sent_list.append("Telegram")
            
    if discord_webhook and sns_text:
        discord_body = messenger_body.replace("*", "**")
        if send_discord_message(discord_webhook, discord_body):
            sent_list.append("Discord")
            
    # 이메일 발송 (최상단 요약 이미지 + 1문장 핵심 요약 + 경제 지표 + 6대 섹션)
    html_body = format_briefing_to_html(briefing, indicators, has_image=(image_bytes is not None))
    if send_email(title, html_body, image_bytes=image_bytes):
        sent_list.append("Email")

    if sent_list:
        logger.info(f"배포 성공 채널 목록: {', '.join(sent_list)}")
    else:
        logger.warning("전송 설정된 배포 채널이 없어 발송되지 않았습니다.")
        
    logger.info("Daily Briefing Standalone Agent 업무 종료.")

if __name__ == "__main__":
    main()

