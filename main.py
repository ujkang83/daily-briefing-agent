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

from concurrent.futures import ThreadPoolExecutor
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

SPAM_KEYWORDS = [
    "토토", "사설토토", "슬롯", "바카라", "카지노", "먹튀", "꽁머니", "파워볼", "홀덤",
    "포커", "릴게임", "릴시티", "토토사이트", "토토 콩", "커스텀 슬롯", "축구중계 토토",
    "스포츠 토토", "토토 노하우", "배팅", "베팅", "검증방", "보증업체", "안전놀이터",
    "casino", "baccarat", "gambling", "slot", "toto"
]

SPAM_DOMAINS = [
    "histoire-pour-tous.fr", "wordpress.com", "blogspot.com"
]

def is_spam_article(title="", description="", url="", source_name=""):
    """사설 불법 토토, 슬롯, 카지노 SEO 스팸 기사 및 도메인을 감지하고 엄격히 차단합니다."""
    text = f"{title} {description} {source_name}".lower()
    url_lower = (url or "").lower()
    
    for kw in SPAM_KEYWORDS:
        if kw.lower() in text or kw.lower() in url_lower:
            return True
            
    for domain in SPAM_DOMAINS:
        if domain.lower() in url_lower or domain.lower() in text:
            return True

    return False

MEDIA_DOMAIN_MAP = {
    "biz.chosun.com": "조선비즈",
    "sports.chosun.com": "스포츠조선",
    "chosun.com": "조선일보",
    "yna.co.kr": "연합뉴스",
    "yonhapnewstv.co.kr": "연합뉴스TV",
    "donga.com": "동아일보",
    "sports.donga.com": "스포츠동아",
    "joongang.co.kr": "중앙일보",
    "isplus.com": "일간스포츠",
    "hani.co.kr": "한겨레",
    "khan.co.kr": "경향신문",
    "mk.co.kr": "매일경제",
    "hankyung.com": "한국경제",
    "sedaily.com": "서울경제",
    "news1.kr": "뉴스1",
    "newsis.com": "뉴시스",
    "spotvnews.co.kr": "스포티비뉴스",
    "xportsnews.com": "엑스포츠뉴스",
    "sportsseoul.com": "스포츠서울",
    "osen.co.kr": "OSEN",
    "mydaily.co.kr": "마이데일리",
    "newsen.com": "뉴스엔",
    "mt.co.kr": "머니투데이",
    "edaily.co.kr": "이데일리",
    "etnews.com": "전자신문",
    "zdnet.co.kr": "지디넷코리아",
    "ytn.co.kr": "YTN",
    "sbs.co.kr": "SBS",
    "kbs.co.kr": "KBS",
    "mbc.co.kr": "MBC",
    "jtbc.co.kr": "JTBC",
    "naver.com": "네이버 뉴스",
    "daum.net": "다음 뉴스"
}

def get_media_name_from_url(url, default_name=""):
    """URL 도메인을 분석하여 언론사 브랜드명으로 자동 변환합니다. 포털 도메인(네이버/다음)인 경우 기수집된 구체적 언론사명을 우선합니다."""
    is_generic = not default_name or default_name.strip() in ["Google News", "구글 뉴스", "Naver News", "네이버 뉴스", "Unknown", ""]

    if not url:
        return "" if is_generic else default_name

    try:
        netloc = urllib.parse.urlparse(url).netloc.lower()
        # 포털 도메인(naver.com, daum.net)인 경우 이미 구체적 언론사명이 있으면 언론사명 우선
        if ("naver.com" in netloc or "daum.net" in netloc) and not is_generic:
            return default_name

        for domain, name in sorted(MEDIA_DOMAIN_MAP.items(), key=lambda x: len(x[0]), reverse=True):
            if domain in ["naver.com", "daum.net"] and not is_generic:
                continue
            if domain in netloc:
                return name
    except Exception:
        pass

    if not is_generic:
        return default_name
    return ""

def is_valid_article_url(url, title="", description="", source_name=""):
    """
    단순 공식 홈페이지, 포털 스포츠 대표/섹션 메인(KBO, MLB, EPL, 네이버스포츠 메인 등) 및
    개별 기사가 아닌 껍데기 링크, 불법 토토/슬롯 스팸 기사를 걸러냅니다.
    """
    if not url or not isinstance(url, str) or not url.startswith("http"):
        return False
        
    if is_spam_article(title=title, description=description, url=url, source_name=source_name):
        return False

    try:
        parsed = urllib.parse.urlparse(url)
        netloc = parsed.netloc.lower()
        path = parsed.path.strip("/")
        query = parsed.query.lower()
        
        for domain in SPAM_DOMAINS:
            if domain.lower() in netloc or domain.lower() in (title + description).lower():
                return False

        # 1. 경로(path)가 비어있거나 index/home/main 등 단순 메인 페이지인 경우
        if not path or path.lower() in ["", "index.html", "index.htm", "index.php", "home", "main", "default.aspx"]:
            if not any(param in query for param in ["aid=", "article_id=", "idxno=", "id=", "no=", "gno="]):
                return False

        # 2. 포털 및 언론사 스포츠 섹션 메인/분류 페이지 필터링
        section_paths = {
            "sports", "kbaseball", "wbaseball", "kfootball", "wfootball",
            "baseball", "football", "soccer", "basketball", "volleyball", "golf",
            "esports", "general", "sports/index", "news/sports", "section", "all",
            "sports/all", "category"
        }
        clean_path = path.lower().rstrip("/")
        if clean_path in section_paths or clean_path.endswith("/index") or clean_path.endswith("/index.nhn"):
            if not any(param in query for param in ["aid=", "article_id=", "idxno=", "id=", "no=", "gno="]):
                return False
            
        # 3. 알려진 스포츠 리그/기관 단순 메인 도메인 및 랜딩 페이지 검증
        generic_domains = [
            "kbo.or.kr", "koreabaseball.com", "mlb.com", "premierleague.com",
            "kleague.com", "nba.com", "korea.kr"
        ]
        for gd in generic_domains:
            if gd in netloc:
                path_lower = path.lower()
                if not any(sub in path_lower for sub in ["article", "news", "story", "game", "match", "view"]):
                    return False
                    
        # 4. 개별 기사 식별자(숫자, 기사 식별 키워드) 확인
        has_article_keyword = any(kw in clean_path for kw in ["article", "view", "read", "story", "news", "detail", "mnews", "v/"])
        has_digits = bool(re.search(r'\d{3,}', clean_path) or re.search(r'\d{3,}', query))
        has_article_param = any(param in query for param in ["aid=", "article_id=", "idxno=", "id=", "no=", "gno="])
        
        if "news.google.com" not in netloc:
            if not (has_digits or has_article_keyword or has_article_param):
                return False

        return True
    except Exception:
        return False

def collect_google_news(keyword, limit=20):
    """Google News RSS를 통해 기사를 수집합니다. 토토/슬롯 스팸 키워드를 검색 쿼리 차단(-토토 -슬롯 -카지노 -먹튀)과 함께 수집합니다."""
    search_query = f"{keyword} -토토 -슬롯 -카지노 -먹튀 when:1d"
    encoded_keyword = urllib.parse.quote(search_query)
    rss_url = f"https://news.google.com/rss/search?q={encoded_keyword}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        feed = feedparser.parse(rss_url)
        articles = []
        for entry in feed.entries[:limit]:
            link = entry.link
            title = clean_google_title(entry.title)
            desc = clean_html(entry.get("summary", ""))
            source_name = entry.get("source", {}).get("title", "Google News")
            
            if not is_valid_article_url(link, title=title, description=desc, source_name=source_name):
                continue

            articles.append({
                "title": title,
                "link": link,
                "description": desc or title,
                "source": source_name,
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
                orig_link = item.get("originallink", "").strip()
                portal_link = item.get("link", "").strip()
                link = orig_link if (orig_link and is_valid_article_url(orig_link, title=title, description=desc)) else portal_link
                
                if not is_valid_article_url(link, title=title, description=desc, source_name="Naver News"):
                    continue

                articles.append({
                    "title": title,
                    "link": link,
                    "description": desc or title,
                    "source": get_media_name_from_url(link, "Naver News"),
                    "pub_date": item.get("pubDate", "")
                })
            return articles
        return []
    except Exception as e:
        logger.error(f"네이버 뉴스 API 수집 에러 ({keyword}): {e}")
        return []

def categorize_article(title="", description="", keyword=""):
    """키워드 및 기사 제목/본문을 분석하여 6대 카테고리 중 하나로 정확하게 분류합니다."""
    text = f"{title} {description} {keyword}".lower()

    # 1. 스포츠: 야구, 축구, 농구, 골프 등 실제 스포츠 종목 및 경기 결과
    sports_kw = [
        "kbo", "mlb", "epl", "k리그", "야구", "축구", "농구", "배구", "골프", "테니스",
        "손흥민", "이정후", "김도영", "오타니", "홈런", "경기 결과", "프로야구", "해외축구",
        "메이저리그", "골", "득점", "삼진", "타율", "잔여경기", "가을야구", "투수", "타자",
        "안타", "실책", "승점", "순위 싸움", "스토브리그", "fa 계약"
    ]
    non_sports_kw = [
        "채용", "공채", "신입", "대졸", "서류 접수", "sdv", "전동화", "인재 확보", "채용 전형",
        "디지털 전환", "영업이익", "분기 실적", "반도체 수율", "부동산 분양", "대통령실", "국회 본회의",
        "문제해결력"
    ]
    if any(k in text for k in sports_kw):
        # 기업 채용, 공채, SDV 등 비스포츠 키워드가 명확히 들어간 경우 스포츠에서 제외
        if not any(k in text for k in non_sports_kw):
            return "스포츠"

    # 2. 국내 정치
    politics_kw = ["국회", "대통령실", "여당", "야당", "의원", "입법", "국정감사", "당대표", "원내대표", "총선", "선거", "정치", "법안", "청문회"]
    if any(k in text for k in politics_kw):
        return "국내 정치"

    # 3. 국제 정세
    intl_kw = ["백악관", "트럼프", "바이든", "미국 대선", "중국 외교", "우크라이나", "중동", "nato", "지정학", "국제 정세", "외교부", "정상회담", "관세"]
    if any(k in text for k in intl_kw):
        return "국제 정세"

    # 4. AX · RX · 디지털 트윈 & 로보틱스
    tech_kw = ["인공지능", "생성형 ai", "llm", "로봇", "로보틱스", "디지털 트윈", "자율주행", "ax", "rx", "엔비디아", "openai", "온디바이스 ai", "agi"]
    if any(k in text for k in tech_kw):
        return "AX · RX · 디지털 트윈 & 로보틱스"

    # 5. 주요 기업 동향 (기업 채용, 신사업, 실적, M&A 등)
    corp_kw = ["실적", "영업이익", "매출", "m&a", "인수", "투자", "채용", "공채", "신입", "상장", "공시", "현대차", "기아", "삼성전자", "sk하이닉스", "lg", "사업 개편"]
    if any(k in text for k in corp_kw):
        return "주요 기업 동향"

    # 6. 거시 경제 & 주요 지표
    econ_kw = ["금리", "환율", "코스피", "코스닥", "나스닥", "부동산", "물가", "cpi", "한국은행", "fed", "연준", "통화정책", "금융", "증시", "기준금리"]
    if any(k in text for k in econ_kw):
        return "거시 경제 & 주요 지표"

    return "주요 기업 동향"


def select_balanced_articles_per_category(articles, max_per_cat=8):
    """6대 카테고리별로 고르게 기사를 선별하여 특정 분야의 기사 기근이나 편중을 방지합니다."""
    categories_order = [
        "거시 경제 & 주요 지표",
        "주요 기업 동향",
        "AX · RX · 디지털 트윈 & 로보틱스",
        "국제 정세",
        "국내 정치",
        "스포츠"
    ]
    by_cat = {c: [] for c in categories_order}

    for art in articles:
        cat = art.get("category")
        if not cat or cat not in by_cat:
            cat = categorize_article(art.get("title", ""), art.get("description", ""))
            art["category"] = cat
        if len(by_cat[cat]) < max_per_cat:
            by_cat[cat].append(art)

    balanced = []
    for c in categories_order:
        items = by_cat[c]
        balanced.extend(items)
        logger.info(f"선정된 기사 - 카테고리 '{c}': {len(items)}개 기사")

    return balanced


def collect_all_news(keywords, naver_id=None, naver_secret=None, limit_per_keyword=15):
    """여러 키워드에 대해 뉴스를 통합 수집하고 카테고리를 부여한 후 중복을 제거합니다."""
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
                art["category"] = categorize_article(art.get("title", ""), art.get("description", ""), kw)
                all_articles.append(art)
                
    logger.info(f"뉴스 수집 완료: 총 {len(all_articles)}개 기사 수집됨 (중복 링크 제거)")
    return all_articles

def get_economic_indicators():
    """야후 파이낸스 API를 통해 4대 카테고리(국내 지표, 해외 지표, 환율, 유가)의 주요 지표 10종을 수집합니다."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    
    # 4대 카테고리별 지표 및 심볼 정의
    categories_config = [
        {
            "category": "국내 지표",
            "items": [
                {"name": "코스피 (KOSPI)", "sym": "^KS11", "unit": "pt"},
                {"name": "코스닥 (KOSDAQ)", "sym": "^KQ11", "unit": "pt"},
                {"name": "삼성전자 (시총 1위)", "sym": "005930.KS", "unit": "원"},
                {"name": "SK하이닉스 (시총 2위)", "sym": "000660.KS", "unit": "원"},
            ]
        },
        {
            "category": "해외 지표",
            "items": [
                {"name": "나스닥 (NASDAQ)", "sym": "^IXIC", "unit": "pt"},
                {"name": "다우존스 (DOW)", "sym": "^DJI", "unit": "pt"},
            ]
        },
        {
            "category": "환율",
            "items": [
                {"name": "원/달러 환율", "sym": "USDKRW=X", "unit": "원"},
                {"name": "원/유로 환율", "sym": "EURKRW=X", "unit": "원"},
                {"name": "원/엔 환율 (100엔)", "sym": "JPYKRW=X", "unit": "원", "is_100yen": True},
            ]
        },
        {
            "category": "유가",
            "items": [
                {"name": "WTI 원유", "sym": "CL=F", "unit": "$"},
            ]
        }
    ]

    indicators = {}
    for cat_info in categories_config:
        cat_name = cat_info["category"]
        for item in cat_info["items"]:
            name = item["name"]
            sym = item["sym"]
            unit = item["unit"]
            is_100yen = item.get("is_100yen", False)
            
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
                            
                            # 100엔 환산 처리
                            if is_100yen:
                                price *= 100
                                change *= 100
                                
                            indicators[name] = {
                                'price': price,
                                'change': change,
                                'pct': pct,
                                'category': cat_name,
                                'unit': unit
                            }
                            continue
                logger.warning(f"경제 지표 수집 실패 ({name}): API 응답 이상")
            except Exception as e:
                logger.error(f"경제 지표 수집 중 에러 발생 ({name}): {e}")
    return indicators


# ==========================================
# 2단계: 정제 & 유사도 클러스터링 (Processor)
# ==========================================
def cluster_and_deduplicate_articles(articles, similarity_threshold=0.28):
    """TF-IDF 및 Cosine Similarity를 이용해 동일 사건 중복 기사를 정밀 클러스터링하고 대표 기사만 추립니다."""
    if not articles:
        return []
    if len(articles) == 1:
        return [articles[0]]

    # 스팸 필터링 적용
    cleaned_articles = [a for a in articles if is_valid_article_url(a.get("link"), title=a.get("title",""), description=a.get("description",""), source_name=a.get("source",""))]
    if not cleaned_articles:
        return []

    corpus = [f"{art.get('title', '')} {art.get('title', '')} {art.get('description', '')}" for art in cleaned_articles]

    try:
        vectorizer = TfidfVectorizer(
            analyzer='char_wb',
            ngram_range=(2, 4),
            min_df=1,
            sublinear_tf=True
        )
        tfidf_matrix = vectorizer.fit_transform(corpus)
        sim_matrix = cosine_similarity(tfidf_matrix, tfidf_matrix)
        
        visited = set()
        unique_articles = []
        clusters = []

        for i in range(len(cleaned_articles)):
            if i in visited:
                continue
                
            cluster = [cleaned_articles[i]]
            visited.add(i)
            
            for j in range(i + 1, len(cleaned_articles)):
                if j in visited:
                    continue
                if sim_matrix[i][j] >= similarity_threshold:
                    cluster.append(cleaned_articles[j])
                    visited.add(j)
            
            clusters.append(cluster)

        for cluster in clusters:
            representative = max(cluster, key=lambda x: len(x.get("title", "")) + len(x.get("description", "")))
            representative["cluster_size"] = len(cluster)
            representative["related_articles"] = [
                {"title": x.get("title", ""), "link": x.get("link", ""), "source": x.get("source", "Google News")}
                for x in cluster if x.get("link", "") != representative.get("link", "") and is_valid_article_url(x.get("link", ""), title=x.get("title",""), source_name=x.get("source",""))
            ]
            unique_articles.append(representative)
            
        logger.info(f"중복 뉴스 정제 완료: {len(cleaned_articles)}개 -> {len(unique_articles)}개 고유 뉴스 그룹 도출")
        return unique_articles
    except Exception as e:
        logger.error(f"뉴스 유사도 정제 처리 중 에러 발생: {e}")
        return cleaned_articles


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
    related_companies: List[RelatedCompany] = Field(default_factory=list, description="이 이슈와 직접적이고 명확하게 연관된 핵심 기업 (실적 발표, M&A, 주요 공급망 계약, 주요 밸류체인 관계 등 근거가 확실한 경우만 포함. 연관성이나 근거가 모호하거나 약하면 억지로 작성하지 말고 반드시 빈 리스트 []로 남겨둘 것)")
    article_id: str = Field(default="", description="이 브리핑 아이템의 바탕이 된 원문 기사의 고유 식별자 태그 (예: 'ART-01', 'ART-12'). 제공된 기사 목록에서 참조한 [ART-XX] 코드를 반드시 기재하십시오. 경제 지표 종합 요약 항목은 'INDICATOR')")
    source_url: Optional[str] = Field(default="", description="원문 기사 URL (파이썬 코드가 article_id로부터 자동 매핑하므로 빈 문자열이어도 무방)")
    source_name: Optional[str] = Field(default="", description="출처 언론사 이름 (파이썬 코드가 article_id로부터 자동 매핑)")

class BriefingSection(BaseModel):
    category: str = Field(description="카테고리명 (거시 경제 & 주요 지표, 주요 기업 동향, AX · RX · 디지털 트윈 & 로보틱스, 국제 정세, 국내 정치, 스포츠 중 하나)")
    items: List[BriefingItem]

class DailyBriefing(BaseModel):
    title: str = Field(description="브리핑 전체 제목 (예: 2026년 9월 7일 모닝 인텔리전스 리포트)")
    daily_summary: str = Field(description="오늘 글로벌 시장과 산업 전체를 관통하는 핵심 총평 단 1문장 (최상단 하이라이트)")
    executive_insights: List[str] = Field(description="오늘 하루 전체 뉴스를 종합 분석하여 도출한 3대 핵심 전략적 관전 포인트 (Executive Strategic Insights 3개 항목)")
    key_watchlist_companies: List[str] = Field(default_factory=list, description="오늘 브리핑 전체에서 가장 주목해야 할 핵심 관련 기업 3~5개 이름 (예: ['SK하이닉스', '엔비디아', '현대차'])")
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
        self.last_articles_by_id = {}

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

        # 각 기사에 결정론적 고유 식별자([ART-01], [ART-02], ...) 부여 및 역참조 맵 구축
        self.last_articles_by_id = {}
        for idx, art in enumerate(articles):
            art_id = f"ART-{idx+1:02d}"
            art["id"] = art_id
            self.last_articles_by_id[art_id] = art

        # 카테고리별로 기사 그룹화하여 명확한 섹션별 기사 리스트 제공
        by_cat = {}
        for art in articles:
            c = art.get("category") or "기타"
            by_cat.setdefault(c, []).append(art)

        articles_text = ""
        for cname, arts in by_cat.items():
            articles_text += f"\n=== [카테고리: {cname} 관련 수집 기사] ===\n"
            for art in arts:
                articles_text += f"[{art['id']}] 제목: {art['title']}\n출처: {art['source']}\n설명: {art['description']}\n\n"

        indicators_text = ""
        if indicators:
            indicators_text = "현재 주요 경제 지표 (4대 카테고리):\n"
            by_ind_cat = {}
            for name, val in indicators.items():
                cat = val.get("category", "기타 지표")
                by_ind_cat.setdefault(cat, []).append((name, val))
            for cat, items in by_ind_cat.items():
                indicators_text += f"[{cat}]\n"
                for name, val in items:
                    unit = val.get("unit", "")
                    indicators_text += f"- {name}: {val['price']:,.2f}{unit} (전일비 {val['change']:+,.2f}, {val['pct']:+.2f}%)\n"
            indicators_text += "\n"

        prompt = f"""당신은 글로벌 탑티어 전략 컨설팅 펌(맥킨지, BCG) 및 최고급 투자기관의 [수석 경제·산업 전략 애널리스트(Chief Strategy Analyst)]입니다.
오늘 날짜는 {today_str}입니다. 반드시 오늘 날짜({today_str}) 기준으로 브리핑 전체 제목과 최고 수준의 전략 인텔리전스 리포트를 JSON 형식으로 작성해 주세요.

당신의 핵심 임무는 단순한 뉴스 팩트 요약(받아쓰기)이 아닙니다.
개별 사건 이면에 숨겨진 구조적 변화(Why It Matters), 산업 밸류체인 및 시장에 미칠 실질적 파급효과(So What?), 그리고 직접적으로 연관된 핵심 기업(수혜주, 피해주, 공급망 파트너)의 비즈니스적 인과관계를 날카롭고 깊이 있게 도출하는 것입니다.

{indicators_text}[뉴스 기사 목록 (카테고리별 그룹화)]
{articles_text}
{additional_notes}

[카테고리 분류 규칙]
반드시 다음 6개 카테고리를 모두 포함하여 작성하세요:
1. "거시 경제 & 주요 지표" - 경제, 금융, 환율, 주식시장, 금리, 부동산, 물가, 통화정책 등 주요 경제 기사 (경제 지표 요약 외에도 실질적인 경제 관련 뉴스 기사 다수 포함)
2. "주요 기업 동향" - 기업 투자, M&A, 실적 발표, 신사업, 경영 전략, 대규모 채용 관련(해외 IT 빅테크 및 국내 삼성, SK, 현대차, 기아, LG 그룹 등 주요 기업)
3. "AX · RX · 디지털 트윈 & 로보틱스" - AI, 로봇, 디지털 트윈, 자동화, 기술 혁신, 신기술 적용 사례 관련
4. "국제 정세" - 해외 정치, 외교, 무역, 지정학적 이슈 관련
5. "국내 정치" - 국내 정책, 입법, 선거, 주요 정치 현안 관련
6. "스포츠" - 국내외 주요 스포츠(KBO 프로야구, 해외축구 EPL, 메이저리그 MLB, K리그, 골프, 농구, 테니스 등)의 최신 소식 (시즌 중에는 경기 결과/스코어/활약상, 시즌 종료/비시즌에는 FA/트레이드/스토브리그/감독선임/스프링캠프 소식을 팩트 기반으로 전달)

[심층 인사이트 및 작성 지침]
1. ★동일 사건 중복 및 반복 보도 절대 금지 (ZERO DUPLICATE EVENTS)★:
   - 동일한 사건, 동일한 경기, 동일한 정책 발표, 동일한 기업 이슈(예: 기아 채용 기사 등)를 다룬 뉴스는 반드시 단 1개의 대표 아이템으로만 작성하십시오.
   - 같은 카테고리 내에서든 다른 카테고리에서든, **동일하거나 유사한 사건을 여러 아이템에 걸쳐 중복/반복하여 작성하는 것을 엄격히 금지**합니다.
   - 각 아이템은 반드시 **서로 완전히 다른 독립적인 사건/이슈/경기**를 다루어야 하며, 브리핑 전체에 걸쳐 소재가 겹치지 않도록 다양한 시각의 뉴스를 선정하십시오.
2. ★상투적이고 무의미한 표현 절대 금지★:
   - "기대된다", "주목된다", "관심이 쏠린다", "경쟁력 강화가 예상된다", "귀추가 주목된다" 같은 진부한 클리셰 문구는 절대 쓰지 마십시오.
   - 대신 "원인 -> 구조적 메커니즘 -> 밸류체인/가격/실적에 미치는 구체적 영향"의 인과관계를 논리적으로 기술하십시오.
3. ★관련 기업 분석(related_companies) 근거 엄격 적용 (억지 작성 절대 금지)★:
   - 각 기사 항목마다 해당 이슈와 **직접적이고 명확하게 연관된 기업**(실적 발표, M&A, 주요 공급망 계약, 수혜/피해 인과관계가 명확한 기업)이 있는 경우에만 작성하십시오.
   - **연관성의 근거가 약하거나 모호하거나 억지스러운 경우, 절대로 억지로 끼워 넣지 말고 반드시 빈 리스트 (`[]`)로 남겨두십시오.** (억지 밸류체인 연결 절대 금지)
4. ★팩트 검증 및 구시대 정보 / 소속팀·선수 오류 절대 금지★:
   - 제공된 최신 뉴스 기사에 기록된 명확한 팩트에 기반하여 작성하십시오.
   - 과거 기억이나 이전 학습 데이터에 기반하여 **이미 이적했거나 소속이 바뀐 선수/감독의 과거 소속팀 오인, 과거 대표이사/소속 등을 잘못 작성하는 팩트 오류를 절대 일으키지 마십시오.** 기사 원문의 최신 소속 및 팩트 정보를 엄격히 검증하여 작성해야 합니다.
5. ★기사 식별자 [ART-XX] 결정론적 바인딩 (DETERMINISTIC ARTICLE ID BINDING)★:
   - 각 기사 항목마다 해당 내용의 바탕이 된 원문 기사의 [ART-XX] 식별자(예: 'ART-01', 'ART-08')를 반드시 `article_id` 필드에 정확히 기재하십시오.
   - "거시 경제 & 주요 지표" 카테고리의 첫 번째 종합 경제 지표 요약 아이템은 `article_id`에 'INDICATOR'를 기재하십시오.
   - 절대 가짜 ID나 존재하지 않는 번호를 지어내지 말고, 수집 기사 목록에 표시된 [ART-XX] 코드를 그대로 사용하십시오.
   - `source_url`과 `source_name`은 파이썬 코드가 `article_id`를 기반으로 원문 데이터베이스에서 100% 정확하게 자동 주입하므로, 빈 문자열("")로 두셔도 됩니다.
6. ★최상단 3대 핵심 전략 인사이트(executive_insights) & 주목 기업(key_watchlist_companies)★:
   - `executive_insights`는 오늘 하루 뉴스 전체를 가로지르는 3대 거시적/산업적 관전 포인트(Trend & Structural Shift)를 전문 애널리스트 관점에서 깊이 있게 제시하십시오.
   - `key_watchlist_companies`는 오늘 브리핑 전체에서 가장 핵심적으로 영향받는 대표 기업 3~5개 이름을 배열로 제시하십시오. (연관성이 확실한 기업만 도출)
7. 모든 카테고리(6개 분야)가 결과에 반드시 포함되어야 하며, 각 카테고리마다 아이템이 최소 3개 이상 작성되어야 합니다.
   - "거시 경제 & 주요 지표" 카테고리는 제공된 경제 지표 요약(첫 번째 아이템) 외에도 금리, 환율, 주식시장, 부동산, 물가, 통화정책 등 실질적인 경제 기사를 최소 3개 이상 추가하여 총 4개 이상의 아이템으로 구성하세요.
   - "스포츠" 카테고리:
     - **시즌 중(On-Season)**인 종목: 당일/전일 실제 경기 결과, 스코어(점수/승패), 주요 선수 활약상 및 순위 변동 등 경기 결과를 구체적으로 포함하세요.
     - **시즌 종료/휴식기(Off-Season / 비경기일)**인 종목: 억지로 경기 결과를 환각(가짜 스코어)하지 말고, 대형 FA 계약, 트레이드, 감독/스태프 교체, 선수단 개편, 스프링캠프훈련, 주요 수상 소식 등 뉴스 기사에 제공된 팩트 소식을 정확히 전달하세요.
     - 야구(KBO/MLB: 봄~가을), 축구(EPL/유럽: 가을~봄), 농구(겨울~봄), 골프 등 사계절 교차 종목이 존재하므로, 당일 진행 중인 종목 뉴스를 우선 활용하세요.
   - ★카테고리 매핑 원칙 및 비(非)스포츠 기사 스포츠 분류 절대 금지 (STRICT CATEGORY INTEGRITY)★:
     - 각 카테고리는 반드시 위에 카테고리별로 분류되어 제공된 기사 목록에서만 기사를 선정하여 작성하십시오.
     - 특히 "스포츠" 카테고리는 오직 [카테고리: 스포츠 관련 수집 기사] 목록에 제공된 실제 야구(KBO, MLB), 축구(손흥민, EPL, K리그), 농구, 골프 등 **실제 스포츠 경기 결과, 스코어, 선수 활약, 구단 순위 변동, 이적/FA 소식**만으로 작성하십시오.
     - 기업의 채용(대졸 신입/경력 채용, AI 문제해결력 검증 등), 일반 경영, SDV/모빌리티 기술, 재무 실적 발표 등은 절대 스포츠가 아닙니다. 스포츠 구단 모기업(기아, 삼성, 현대차, 한화 등)이라는 핑계로 기업 채용이나 일반 경영 뉴스를 스포츠 카테고리에 분류하는 것을 엄격히 금지합니다.
     - 동일 기업이나 동일 사건(예: 기아 채용 기사 여러 건 등)을 같은 카테고리 내에서 2개 이상의 아이템으로 중복하여 작성하는 것을 엄격히 금지합니다. 단 1개의 대표 아이템으로만 다루십시오.
8. 각 기사 항목의 원문 기사 식별자(article_id)를 통해 링크와 언론사가 자동 매핑됩니다. 경제 지표 요약 항목의 article_id는 'INDICATOR'로 지정하십시오.

응답은 반드시 아래 JSON 스키마를 따르며, 마크다운 코드 블록 없이 순수 JSON만 출력하세요:

{{
  "title": "String (브리핑 전체 제목)",
  "daily_summary": "String (오늘 브리핑 전체를 관통하는 핵심 총평 1문장)",
  "executive_insights": [
    "String (오늘의 1번째 핵심 전략 인사이트 - 맥락과 파급효과)",
    "String (오늘의 2번째 핵심 전략 인사이트 - 밸류체인 및 시장 변화)",
    "String (오늘의 3번째 핵심 전략 인사이트 - 향후 전개 시나리오)"
  ],
  "key_watchlist_companies": ["String (핵심 주목 기업 1)", "String (핵심 주목 기업 2)", "String (핵심 주목 기업 3)"],
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
          "article_id": "String ([ART-XX] 코드, 경제 지표 요약은 'INDICATOR')",
          "source_url": "String (원문 기사 URL, 파이썬 코드가 article_id 기반 자동 매핑하므로 빈 문자열 가능)",
          "source_name": "String (출처 언론사 이름, 파이썬 코드가 article_id 기반 자동 매핑하므로 빈 문자열 가능)"
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


def _render_indicator_cards(indicators):
    """
    경제 지표를 4대 카테고리(국내 지표, 해외 지표, 환율, 유가)의
    모바일/이메일 완벽 호환 요약 카드 그리드로 렌더링합니다.
    """
    if not indicators:
        return ""
        
    category_order = ["국내 지표", "해외 지표", "환율", "유가"]
    category_icons = {
        "국내 지표": "🇰🇷",
        "해외 지표": "🌐",
        "환율": "💱",
        "유가": "🛢️"
    }
    
    # 카테고리별로 지표 분류
    by_cat = {c: [] for c in category_order}
    for name, val in indicators.items():
        cat = val.get("category")
        if cat in by_cat:
            by_cat[cat].append((name, val))
        else:
            by_cat.setdefault("기타 지표", []).append((name, val))
            
    html_parts = []
    html_parts.append("""
    <!-- 경제 지표 스마트 요약 카드 섹션 -->
    <div style="margin-bottom: 28px;">
      <div style="display: inline-block; background-color: #059669; color: #FFFFFF; font-size: 12px; font-weight: 700; padding: 4px 12px; border-radius: 4px; margin-bottom: 12px; letter-spacing: 0.5px;">📈 글로벌 주요 경제 지표 요약</div>
    """)
    
    for cat in category_order:
        items = by_cat.get(cat, [])
        if not items:
            continue
            
        icon = category_icons.get(cat, "📊")
        html_parts.append(f"""
      <!-- 카테고리: {cat} -->
      <div style="font-size: 12px; font-weight: 700; color: #475569; margin: 12px 0 8px 2px;">{icon} {cat}</div>
      <table role="presentation" border="0" cellpadding="0" cellspacing="0" style="width: 100%; border-collapse: separate; border-spacing: 6px; margin-bottom: 6px;">
        """)
        
        # 2열 그리드로 행 구성
        for i in range(0, len(items), 2):
            html_parts.append("        <tr>")
            chunk = items[i:i+2]
            for name, val in chunk:
                price_str = f"{val['price']:,.2f}"
                change = val['change']
                pct = val['pct']
                unit = val.get('unit', '')
                
                # 등락 색상 및 기호 (상승 빨강, 하락 파랑)
                if change > 0:
                    color = "#DC2626"
                    arrow = "▲"
                    bg_badge = "#FEE2E2"
                elif change < 0:
                    color = "#2563EB"
                    arrow = "▼"
                    bg_badge = "#DBEAFE"
                else:
                    color = "#64748B"
                    arrow = "-"
                    bg_badge = "#F1F5F9"
                    
                html_parts.append(f"""
          <td style="width: 50%; vertical-align: top; padding: 0;">
            <div style="background-color: #F8FAFC; border: 1px solid #E2E8F0; border-left: 3px solid {color}; border-radius: 8px; padding: 10px 12px; box-shadow: 0 1px 2px rgba(0,0,0,0.02);">
              <div style="font-size: 11px; font-weight: 600; color: #64748B; margin-bottom: 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">{name}</div>
              <div style="font-size: 15px; font-weight: 800; color: #0F172A; margin-bottom: 4px; line-height: 1.2;">
                {price_str} <span style="font-size: 11px; font-weight: 500; color: #64748B;">{unit}</span>
              </div>
              <div style="display: inline-block; background-color: {bg_badge}; color: {color}; font-size: 11px; font-weight: 700; padding: 2px 6px; border-radius: 4px; line-height: 1.2;">
                {arrow} {abs(change):,.2f} ({pct:+.2f}%)
              </div>
            </div>
          </td>""")
            
            # 홀수 개 아이템일 때 우측 빈 칸 채우기
            if len(chunk) == 1:
                html_parts.append("""
          <td style="width: 50%; vertical-align: top; padding: 0;"></td>""")
                
            html_parts.append("        </tr>")
            
        html_parts.append("      </table>")
        
    html_parts.append("""
    </div>
    <hr style="border: 0; border-top: 1px solid #E2E8F0; margin: 4px 0 24px 0;">""")
    
    return "\n".join(html_parts)


def format_briefing_to_html(briefing_data, indicators=None, has_image=False):
    """
    브리핑 데이터를 최고급 전략 인텔리전스 리포트 HTML 이메일 문서로 변환합니다.
    - 최상단: 브리핑 헤더 & 핵심 총평 & 오늘의 3대 전략 인사이트 & 핵심 주목 기업 (Watchlist)
    - 4대 카테고리별 글로벌 주요 경제 지표 요약 카드
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

    # ── 3. 4대 카테고리별 글로벌 주요 경제 지표 요약 카드 ──
    if indicators:
        parts.append(_render_indicator_cards(indicators))

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

            if source_url and is_valid_article_url(source_url):
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
                    if rel_url and is_valid_article_url(rel_url):
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

    # ── 전체 브리핑 원고 HTML 문서(.html) 첨부파일 동봉 ──
    try:
        attachment_html = html_body
        if image_bytes:
            import base64
            b64_img = base64.b64encode(image_bytes).decode('utf-8')
            data_uri = f"data:image/jpeg;base64,{b64_img}"
            attachment_html = attachment_html.replace('cid:summary_image', data_uri)
            
        html_attachment = MIMEText(attachment_html, "html", "utf-8")
        html_filename = f"DailyBriefing_{today_str}.html"
        html_attachment.add_header("Content-Disposition", "attachment", filename=html_filename)
        msg_root.attach(html_attachment)
        logger.info(f"📎 브리핑 리포트 HTML 문서 첨부파일 동봉 완료: {html_filename}")
    except Exception as e:
        logger.warning(f"HTML 첨부파일 생성 중 오류 (계속 진행): {e}")

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
    구글 뉴스 URL을 ThreadPoolExecutor를 이용해 병렬로 디코딩하여
    국내 언론사 원문 사이트의 직접 기사 링크로 보정합니다.
    """
    logger.info("구글 뉴스 URL 병렬 디코딩 시작...")
    try:
        from googlenewsdecoder import gnewsdecoder
    except ImportError:
        logger.warning("googlenewsdecoder 라이브러리가 임포트되지 않아 URL 디코딩을 건너뜁니다.")
        return articles

    # 1. 디코딩 대상 구글 뉴스 URL 수집 (중복 제거)
    urls_to_decode = set()
    for art in articles:
        orig_url = art.get("link", "")
        if orig_url and "news.google.com" in orig_url:
            urls_to_decode.add(orig_url)
        for rel in art.get("related_articles", []):
            rel_url = rel.get("link", "")
            if rel_url and "news.google.com" in rel_url:
                urls_to_decode.add(rel_url)

    if not urls_to_decode:
        logger.info("디코딩할 구글 뉴스 URL이 없습니다.")
        return articles

    logger.info(f"디코딩 대상 구글 뉴스 URL 총 {len(urls_to_decode)}개 병렬 디코딩 진행 중...")

    decoded_cache = {}
    def _decode_single(u):
        try:
            res = gnewsdecoder(u)
            if res.get("status") and res.get("decoded_url"):
                return u, res["decoded_url"]
        except Exception as e:
            logger.debug(f"URL 디코딩 실패 ({u[:50]}...): {e}")
        return u, None

    with ThreadPoolExecutor(max_workers=10) as executor:
        for orig, dec in executor.map(_decode_single, list(urls_to_decode)):
            if dec:
                decoded_cache[orig] = dec

    logger.info(f"구글 뉴스 URL 디코딩 완료: {len(decoded_cache)}/{len(urls_to_decode)}개 성공")

    # 2. 기사 객체들에 디코딩 결과 반영
    for art in articles:
        orig_url = art.get("link", "")
        if orig_url in decoded_cache:
            dec_url = decoded_cache[orig_url]
            if is_valid_article_url(dec_url, title=art.get("title", ""), description=art.get("description", ""), source_name=art.get("source", "")):
                art["link"] = dec_url
                media_name = get_media_name_from_url(dec_url, art.get("source", ""))
                if media_name:
                    art["source"] = media_name

        for rel in art.get("related_articles", []):
            rel_url = rel.get("link", "")
            if rel_url in decoded_cache:
                dec_url = decoded_cache[rel_url]
                if is_valid_article_url(dec_url, title=rel.get("title", ""), source_name=rel.get("source", "")):
                    rel["link"] = dec_url
                    media_name = get_media_name_from_url(dec_url, rel.get("source", ""))
                    if media_name:
                        rel["source"] = media_name

    return articles


def resolve_article_links_by_id(briefing, articles_by_id=None, all_articles=None):
    """
    AI가 지정한 article_id([ART-XX])를 기반으로 수집 기사 DB에서 원문 링크(source_url),
    언론사명(source_name), 그리고 클러스터링된 연관 기사(related_articles)를 100% 결정론적으로 매핑합니다.
    LLM의 URL 환각이나 링크 뒤바뀜을 원천 차단합니다.
    """
    if not briefing or "sections" not in briefing:
        return briefing

    articles_by_id = articles_by_id or {}
    all_articles = all_articles or []

    # 한국어 불용어 (article_id 누락 시 비상 안전망 매칭용)
    STOPWORDS = {
        "기사", "소식", "뉴스", "경기", "선수", "결과", "시즌", "감독", "구단", "리그",
        "오늘", "이번", "기록", "관련", "리포트", "분석", "전망", "치열", "상황", "예상",
        "국내", "해외", "역대", "최근", "글로벌", "시장", "정세", "동향", "주요", "진행",
        "승리", "패배", "팀", "대결", "맞대결", "종료", "후반기", "전반기", "총력전", "출전",
        "단독", "종합", "속보", "브리핑", "진출", "확정", "시작", "예정", "선발", "교체"
    }

    # 구글 뉴스 디코더 임포트 시도 (잔존 Google News 링크 변환용)
    gnewsdecoder = None
    try:
        from googlenewsdecoder import gnewsdecoder as _gnd
        gnewsdecoder = _gnd
    except ImportError:
        pass

    for section in briefing.get("sections", []):
        items = section.get("items", [])

        for idx, item in enumerate(items):
            raw_id = (item.get("article_id") or "").strip()
            headline = item.get("headline", "")
            summary = item.get("summary", "")
            matched_article = None

            # 1. 경제 지표 종합 요약 항목 처리
            if "INDICATOR" in raw_id.upper() or "지표" in raw_id:
                item["source_url"] = ""
                item["source_name"] = ""
                item["related_articles"] = []
                continue

            # 2. [ART-XX] 정규식 패턴 정규화 매칭
            m = re.search(r'ART[-_]?0*(\d+)', raw_id, re.IGNORECASE)
            if m:
                norm_id = f"ART-{int(m.group(1)):02d}"
                if norm_id in articles_by_id:
                    matched_article = articles_by_id[norm_id]

            # 3. 비상 안전망: article_id 누락 또는 오작성 시 키워드 정밀 폴백
            if not matched_article and (articles_by_id or all_articles):
                candidates_pool = list(articles_by_id.values()) if articles_by_id else all_articles
                text = f"{headline} {summary}"
                words = set(w for w in re.findall(r'[가-힣a-zA-Z0-9]{2,}', text) if w not in STOPWORDS)
                if len(words) >= 3:
                    best_art = None
                    max_score = 0
                    for art in candidates_pool:
                        if is_spam_article(title=art.get("title", ""), description=art.get("description", ""), url=art.get("link", ""), source_name=art.get("source", "")):
                            continue
                        art_text = f"{art.get('title', '')} {art.get('description', '')}"
                        art_words = set(w for w in re.findall(r'[가-힣a-zA-Z0-9]{2,}', art_text) if w not in STOPWORDS)
                        overlap = len(words & art_words)
                        if overlap >= 3 and (overlap / len(words)) >= 0.35:
                            if overlap > max_score:
                                max_score = overlap
                                best_art = art
                    if best_art:
                        matched_article = best_art

            # 4. 매칭 결과 적용
            if matched_article:
                matched_url = matched_article.get("link", "")
                if matched_url and "news.google.com" in matched_url and gnewsdecoder:
                    try:
                        dec_res = gnewsdecoder(matched_url)
                        if dec_res.get("status") and dec_res.get("decoded_url"):
                            matched_url = dec_res["decoded_url"]
                    except Exception:
                        pass

                if is_valid_article_url(matched_url, title=headline, description=summary, source_name=matched_article.get("source", "")):
                    item["source_url"] = matched_url
                    item["source_name"] = get_media_name_from_url(matched_url, matched_article.get("source", ""))
                else:
                    item["source_url"] = ""
                    item["source_name"] = ""

                # 클러스터링된 진짜 연관 기사 주입
                rel_arts = matched_article.get("related_articles", [])
                verified_rel = []
                for r in rel_arts:
                    r_link = r.get("link", "")
                    if r_link and "news.google.com" in r_link and gnewsdecoder:
                        try:
                            dec_res = gnewsdecoder(r_link)
                            if dec_res.get("status") and dec_res.get("decoded_url"):
                                r_link = dec_res["decoded_url"]
                        except Exception:
                            pass
                    if r_link and is_valid_article_url(r_link, title=r.get("title", ""), source_name=r.get("source", "")):
                        verified_rel.append({
                            "title": r.get("title", ""),
                            "link": r_link,
                            "source": get_media_name_from_url(r_link, r.get("source", ""))
                        })
                item["related_articles"] = verified_rel
            else:
                # 매칭 실패 시 타 기사 억지 끼워넣기 금지 (원문 유지 또는 빈 문자열)
                orig_url = item.get("source_url", "")
                if orig_url and is_valid_article_url(orig_url, title=headline, description=summary):
                    item["source_name"] = get_media_name_from_url(orig_url, item.get("source_name", ""))
                else:
                    item["source_url"] = ""
                    item["source_name"] = ""
                item["related_articles"] = []

    return briefing


def verify_urls_liveness(briefing, max_workers=10, timeout=3.0):
    """
    브리핑에 포함된 모든 URL(source_url 및 related_articles의 link)을 병렬(ThreadPoolExecutor)로
    실제 HTTP 응답(200 OK / 리다이렉션)을 검증하여 404/사망 링크 및 메인홈페이지 튕김 링크를 제거합니다.
    """
    if not briefing or "sections" not in briefing:
        return briefing

    urls_to_check = set()
    for section in briefing.get("sections", []):
        for item in section.get("items", []):
            u = item.get("source_url", "").strip()
            if u and u.startswith("http"):
                urls_to_check.add(u)
            for r in item.get("related_articles", []):
                ru = r.get("link", "").strip()
                if ru and ru.startswith("http"):
                    urls_to_check.add(ru)

    if not urls_to_check:
        return briefing

    logger.info(f"실시간 링크 생존(Liveness) 검증 시작: 총 {len(urls_to_check)}개 고유 URL...")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }

    def check_url(url):
        if "news.google.com" in url:
            return url, True
        try:
            resp = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True)
            if resp.status_code in [405, 403]:
                resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True, stream=True)
                resp.close()

            if 200 <= resp.status_code < 400:
                final_url = resp.url.lower()
                parsed_final = urllib.parse.urlparse(final_url)
                final_path = parsed_final.path.strip("/")
                if not final_path or final_path in ["", "index.html", "index.htm", "main", "home"]:
                    orig_path = urllib.parse.urlparse(url).path.strip("/")
                    if orig_path and orig_path not in ["", "index.html", "index.htm", "main", "home"]:
                        logger.warning(f"기사 삭제 후 메인홈으로 리다이렉트된 링크 감지: {url} -> {final_url}")
                        return url, False
                return url, True
            else:
                logger.warning(f"사망 링크 감지 (HTTP {resp.status_code}): {url}")
                return url, False
        except Exception as e:
            logger.warning(f"링크 연결 검증 실패 ({url}): {e}")
            return url, False

    url_status = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_url = {executor.submit(check_url, u): u for u in urls_to_check}
        for future in future_to_url:
            try:
                u, is_alive = future.result()
                url_status[u] = is_alive
            except Exception:
                pass

    dead_count = sum(1 for v in url_status.values() if not v)
    logger.info(f"링크 생존 검증 완료: 정상 {len(url_status)-dead_count}개 / 사망·차단 {dead_count}개")

    # 사망 링크 제거 정화
    for section in briefing.get("sections", []):
        for item in section.get("items", []):
            src = item.get("source_url", "").strip()
            if src and not url_status.get(src, True):
                logger.info(f"사망 링크 정화 제거: '{item.get('headline')}' -> {src}")
                item["source_url"] = ""
                item["source_name"] = ""

            live_rels = []
            for r in item.get("related_articles", []):
                ru = r.get("link", "").strip()
                if ru and url_status.get(ru, True):
                    live_rels.append(r)
                elif ru:
                    logger.info(f"연관 기사 사망 링크 정화 제거: '{r.get('title')}' -> {ru}")
            item["related_articles"] = live_rels

    return briefing


def sanitize_briefing_categories(briefing):
    """
    1. 스포츠 카테고리에 잘못 분류된 비(非)스포츠 기사(기업 채용, 일반 경영, SDV, 반도체 등)를 감지하여
       올바른 카테고리로 재배치하거나 스포츠 섹션에서 퇴출합니다.
    2. 동일 사건/기업의 중복 아이템(예: 기아 채용 기사 2건 등)을 감지하여 1건만 유지합니다.
    """
    if not briefing or "sections" not in briefing:
        return briefing

    sports_keywords = {
        "야구", "축구", "농구", "배구", "골프", "테니스", "kbo", "mlb", "epl", "k리그",
        "선수", "경기", "감독", "구단", "홈런", "골", "득점", "승리", "패배", "리그",
        "포스트시즌", "타율", "삼진", "이적", "fa", "스토브리그", "트레이드", "챔피언스",
        "토트넘", "손흥민", "이정후", "김도영", "오타니", "탁구", "수영", "양궁", "올림픽",
        "월드컵", "아시안게임", "스코어", "안타", "투수", "타자", "잔여경기", "가을야구",
        "승점", "순위 싸움"
    }

    non_sports_keywords = {
        "채용", "공채", "신입", "대졸", "서류 접수", "sdv", "전동화", "반도체", "디지털 전환",
        "영업이익", "분기 실적", "m&a", "공시", "주총", "정치", "국회", "대통령", "금리", "아파트",
        "인재 확보", "채용 전형", "문제해결력 검증"
    }

    cleaned_sections = []
    ejected_items = []

    for section in briefing.get("sections", []):
        cat_name = section.get("category", "")
        items = section.get("items", [])

        if "스포츠" in cat_name:
            clean_sports_items = []
            for item in items:
                text = f"{item.get('headline', '')} {item.get('summary', '')} {item.get('impact', '')}".lower()
                has_sports = any(kw in text for kw in sports_keywords)
                has_non_sports = any(kw in text for kw in non_sports_keywords)

                # 스포츠 키워드가 전혀 없거나, 명백한 기업 채용/일반 비즈니스 기사인 경우
                if has_non_sports and not has_sports:
                    logger.warning(f"스포츠 카테고리에서 비스포츠 기사 감지 및 퇴출: '{item.get('headline')}'")
                    ejected_items.append(item)
                else:
                    clean_sports_items.append(item)
            section["items"] = clean_sports_items

        # 동일 카테고리 내 동일 사건/주제 중복 제거 (예: 기아 채용 2건 등)
        seen_topics = []
        deduped_items = []
        for item in section.get("items", []):
            h = item.get("headline", "")
            words = set(re.findall(r'[가-힣a-zA-Z0-9]{2,}', h))
            is_duplicate = False
            for prev_words in seen_topics:
                common = words & prev_words
                if len(common) >= 2 and any(kw in common for kw in ["채용", "공채", "실적", "손흥민", "이정후", "김도영", "kbo", "금리", "환율", "부동산"]):
                    is_duplicate = True
                    break
            if is_duplicate:
                logger.warning(f"카테고리 내 동일 사건 중복 아이템 제거: '{h}'")
                continue
            seen_topics.append(words)
            deduped_items.append(item)
        section["items"] = deduped_items
        cleaned_sections.append(section)

    # 퇴출된 비스포츠 아이템(예: 기아 채용 등)을 '주요 기업 동향' 섹션으로 정상 재배치
    if ejected_items:
        corp_section = next((s for s in cleaned_sections if "주요 기업 동향" in s.get("category", "")), None)
        if corp_section:
            for item in ejected_items:
                h = item.get("headline", "")
                words = set(re.findall(r'[가-힣a-zA-Z0-9]{2,}', h))
                if not any(len(words & set(re.findall(r'[가-힣a-zA-Z0-9]{2,}', s_item.get("headline", "")))) >= 2 for s_item in corp_section.get("items", [])):
                    corp_section["items"].append(item)
                    logger.info(f"퇴출된 기사를 '주요 기업 동향'으로 정상 재배치: '{h}'")

    briefing["sections"] = cleaned_sections
    return briefing


def main():
    logger.info("Daily Briefing Standalone Agent 시작...")
    
    gemini_api_key = _clean_env_val(os.environ.get("GEMINI_API_KEY"))
    naver_client_id = _clean_env_val(os.environ.get("NAVER_CLIENT_ID"))
    naver_client_secret = _clean_env_val(os.environ.get("NAVER_CLIENT_SECRET"))
    keywords_env = _clean_env_val(os.environ.get("NEWS_KEYWORDS")) or "인공지능, 빅테크, IT 트렌드, 거시 경제, 금융 증시, 금리 환율 부동산, 국제 정세, 국내 정치, KBO 프로야구, 해외축구 손흥민 EPL, 메이저리그 MLB"
    keywords = [k.strip() for k in keywords_env.split(",") if k.strip()]

    slack_webhook = _clean_env_val(os.environ.get("SLACK_WEBHOOK_URL"))
    telegram_token = _clean_env_val(os.environ.get("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id = _clean_env_val(os.environ.get("TELEGRAM_CHAT_ID"))
    discord_webhook = _clean_env_val(os.environ.get("DISCORD_WEBHOOK_URL"))

    if not gemini_api_key:
        logger.error("GEMINI_API_KEY 환경 변수가 설정되지 않았습니다.")
        return

    # 1단계 뉴스 수집
    logger.info("1단계: 최신 뉴스 수집 시작...")
    all_articles = collect_all_news(keywords, naver_client_id, naver_client_secret)
    if not all_articles:
        logger.error("수집된 뉴스가 없습니다. 종료합니다.")
        return

    # 2단계 정제 & 유사도 클러스터링
    logger.info("2단계: 뉴스 정제 및 유사도 클러스터링 시작...")
    unique_articles = cluster_and_deduplicate_articles(all_articles)

    # 2.5단계 구글 뉴스 URL 병렬 디코딩 (원문 언론사 직접 링크 변환)
    unique_articles = decode_news_urls(unique_articles)

    # 2.8단계 카테고리별 균형 잡힌 기사 세트 구성 (카테고리당 최대 8개)
    balanced_articles = select_balanced_articles_per_category(unique_articles, max_per_cat=8)

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
    briefing = ai.generate_briefing(balanced_articles, indicators)
    
    if "error" in briefing:
        logger.error(f"브리핑 생성 실패: {briefing['error']}")
        return

    logger.info(f"AI 브리핑 생성 성공: '{briefing.get('title')}'")
    
    # 4.5단계: 일일 요약 이미지 생성 (AI 생성 시도 + 고화질 인포그래픽 배너 폴백)
    logger.info("4.5단계: 일일 요약 이미지 생성 시작...")
    image_bytes = generate_summary_image(ai, briefing, indicators)

    # 4.7단계: 카테고리 적합성 검증 및 비스포츠 기사 퇴출/재배치, 동일 사건 중복 정화
    briefing = sanitize_briefing_categories(briefing)

    # 4.8단계: 기사 식별자(Article ID) 기반 100% 결정론적 원문 및 연관 기사 바인딩
    briefing = resolve_article_links_by_id(briefing, getattr(ai, "last_articles_by_id", {}), unique_articles)

    # 4.9단계: 실시간 HTTP 링크 생존(Liveness) 검증 (404/사망 링크 및 메인홈 튕김 제거)
    briefing = verify_urls_liveness(briefing)

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

