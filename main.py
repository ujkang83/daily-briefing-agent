import os
import sys
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
import google.generativeai as genai
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from dotenv import load_dotenv

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

_AVAILABLE_MODELS_CACHE = None
_PRIORITY_MODELS = [
    'models/gemini-2.5-flash',
    'models/gemini-2.0-flash',
    'models/gemini-1.5-flash',
    'models/gemini-flash-latest',
    'models/gemini-3.5-flash',
    'models/gemini-2.0-flash-lite',
    'models/gemini-1.5-pro',
    'models/gemini-2.5-pro'
]

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
                {"title": x.get("title", ""), "link": x.get("link", "")} 
                for x in cluster if x.get("link", "") != representative.get("link", "")
            ]
            unique_articles.append(representative)
            
        logger.info(f"중복 뉴스 정제 완료: {len(articles)}개 -> {len(unique_articles)}개 뉴스 그룹 도출")
        return unique_articles
    except Exception as e:
        logger.error(f"뉴스 유사도 정제 처리 중 에러 발생: {e}")
        return articles


# ==========================================
# 3단계: 요약 & 분석 (AI Engine)
# ==========================================
class AIEngine:
    def __init__(self, api_key):
        if api_key:
            # API 키 클리닝 (gRPC 공백 에러 차단)
            clean_key = "".join(c for c in str(api_key).strip() if 32 < ord(c) < 127)
        else:
            clean_key = ""
        self.api_key = clean_key
        if self.api_key:
            try:
                genai.configure(api_key=self.api_key)
            except Exception as e:
                logger.error(f"API Key 설정 에러: {e}")

    def _get_available_models(self):
        global _AVAILABLE_MODELS_CACHE
        if not _AVAILABLE_MODELS_CACHE:
            try:
                _AVAILABLE_MODELS_CACHE = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
            except Exception as e:
                logger.warning(f"모델 리스트 동적 조회 실패: {e}")
                
        available = _AVAILABLE_MODELS_CACHE or []
        candidates = [p for p in _PRIORITY_MODELS if not available or p in available]
        for am in available:
            if am not in candidates:
                candidates.append(am)
        return candidates or ['models/gemini-2.0-flash', 'models/gemini-1.5-flash']

    def generate_briefing(self, articles, additional_notes="", tone="친근하고 유익한 톤", max_retries=2):
        if not self.api_key:
            return {"error": "API Key가 설정되어 있지 않습니다."}

        articles_text = ""
        for idx, art in enumerate(articles):
            articles_text += f"[{idx+1}] 제목: {art['title']}\n링크: {art['link']}\n설명: {art['description']}\n\n"

        prompt = f"""
당신은 IT/경제 전문 블로그 필진이자 뉴스 분석가입니다.
아래의 기사들을 종합하여 일일 브리핑 원고를 작성해 주세요.

[뉴스 기사 목록]
{articles_text}

[추가 요청/참고사항]
{additional_notes}

[어조 스타일]
{tone}

[요구사항]
1. 각 개별 뉴스에 대한 3줄 요약(summary_lines) 및 시사점(implication)을 도출해 주세요.
2. 마크다운 스타일의 블로그 포스팅 내용(title, introduction, sections, conclusion, hashtags)을 만들어 주세요.
3. 슬랙/텔레그램 모바일용 500자 내외의 요약본(short_summary_for_sns)을 작성해 주세요.

응답은 반드시 아래의 JSON 스키마 규격을 충족해야 하며, 어떠한 마크다운 코드 블록 지시자(예: ```json) 없이 순수 JSON 텍스트만 전달해야 합니다.

{{
  "title": "String (전체 제목)",
  "introduction": "String (서론)",
  "article_summaries": [
    {{
      "original_title": "String (기사 제목)",
      "summary_lines": [
        "String (요약 1)",
        "String (요약 2)",
        "String (요약 3)"
      ],
      "implication": "String (시사점)"
    }}
  ],
  "sections": [
    {{
      "subtitle": "String (소제목)",
      "paragraphs": [
        "String (본문 문단 1)"
      ]
    }}
  ],
  "conclusion": "String (결론)",
  "hashtags": ["tag1", "tag2"],
  "short_summary_for_sns": "String (모바일 알림용 한눈에 요약된 내용)"
}}
"""
        candidates = self._get_available_models()
        last_error = None

        for model_name in candidates:
            for i in range(max_retries):
                try:
                    logger.info(f"Gemini API 호출 시도 - 모델: {model_name}, 시도: {i+1}")
                    model = genai.GenerativeModel(model_name)
                    gen_config = {
                        "temperature": 0.5,
                        "max_output_tokens": 8192,
                        "response_mime_type": "application/json"
                    }
                    api_start = time.time()
                    response = model.generate_content(
                        prompt,
                        generation_config=genai.types.GenerationConfig(**gen_config),
                        request_options={'retry': None}
                    )
                    api_duration = time.time() - api_start
                    if response and response.text:
                        logger.info(f"Gemini API 호출 성공: {api_duration:.2f}초 소요")
                        return json.loads(response.text)
                except Exception as e:
                    api_duration = time.time() - api_start if 'api_start' in locals() else 0
                    err_str = str(e)
                    logger.warning(f"Gemini API 호출 실패: {model_name}, 에러: {err_str}, {api_duration:.2f}초")
                    last_error = e
                    if any(x in err_str for x in ["404", "NotFound", "429", "quota", "ResourceExhausted", "503", "demand", "ServiceUnavailable", "500"]):
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

def format_briefing_to_html(briefing_data):
    """
    브리핑 데이터를 네이버 블로그 및 이메일 발송에 적합한 가독성 높은 HTML 포맷으로 변환합니다.
    """
    title = briefing_data.get("title", "오늘의 일일 브리핑")
    intro = briefing_data.get("introduction", "")
    sections = briefing_data.get("sections", [])
    conclusion = briefing_data.get("conclusion", "")
    hashtags = briefing_data.get("hashtags", [])
    
    html_parts = []
    html_parts.append('<div style="font-family: \'Noto Sans KR\', Arial, sans-serif; line-height: 1.8; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; border: 1px solid #eee; border-radius: 12px; box-shadow: 0 4px 10px rgba(0,0,0,0.05);">')
    
    # 제목
    html_parts.append(f'<h1 style="color: #1A73E8; font-size: 24px; border-bottom: 2px solid #1A73E8; padding-bottom: 12px; margin-top: 0;">📢 {title}</h1>')
    
    # 서론
    if intro:
        intro_html = intro.replace("\n", "<br>")
        html_parts.append(f'<p style="font-size: 16px; font-weight: 500; color: #555; background-color: #f8f9fa; padding: 15px; border-radius: 8px; border-left: 4px solid #1A73E8;">{intro_html}</p>')
    
    # 본문 세션
    for sec in sections:
        subtitle = sec.get("subtitle", "")
        if subtitle:
            html_parts.append(f'<h3 style="color: #202124; font-size: 18px; margin-top: 25px; margin-bottom: 12px; border-left: 4px solid #03C75A; padding-left: 10px;">{subtitle}</h3>')
            
        paragraphs = sec.get("paragraphs", [])
        for p in paragraphs:
            p_html = p.replace("\n", "<br>")
            html_parts.append(f'<p style="font-size: 15px; color: #333; margin-bottom: 10px; text-align: justify;">{p_html}</p>')
            
    # 결론
    if conclusion:
        conclusion_html = conclusion.replace("\n", "<br>")
        html_parts.append('<hr style="border: 0; border-top: 1px solid #eee; margin: 25px 0;">')
        html_parts.append(f'<p style="font-size: 15px; font-style: italic; color: #666;">{conclusion_html}</p>')
        
    # 해시태그
    if hashtags:
        tag_line = " ".join([f"#{tag.strip()}" for tag in hashtags])
        html_parts.append(f'<p style="color: #1A73E8; font-size: 14px; margin-top: 20px; font-weight: bold;">{tag_line}</p>')
        
    html_parts.append('</div>')
    return "\n".join(html_parts)

def send_email(title, html_body):
    """
    SMTP 서버를 통해 개인 수신 이메일 및 네이버 블로그 자동 포스팅용 이메일을 발송합니다.
    """
    smtp_server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    try:
        smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    except Exception:
        smtp_port = 587
    
    sender_email = os.environ.get("SENDER_EMAIL") or os.environ.get("SMTP_SENDER")
    sender_password = os.environ.get("SENDER_PASSWORD") or os.environ.get("SMTP_PASSWORD")
    receiver_email = os.environ.get("RECEIVER_EMAIL") or os.environ.get("SMTP_RECEIVER")
    naver_blog_email = os.environ.get("NAVER_BLOG_EMAIL")  # 네이버아이디@blog.naver.com
    naver_category = os.environ.get("NAVER_BLOG_CATEGORY", "일일 브리핑")
    
    if not sender_email or not sender_password:
        logger.warning("SMTP 이메일 계정 정보(SENDER_EMAIL / SENDER_PASSWORD)가 설정되어 있지 않아 발송을 건너뜁니다.")
        return False
        
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    # 1. 개인 이메일 발송 설정
    msg_personal = MIMEMultipart("alternative")
    msg_personal["Subject"] = f"📬 [Daily Briefing] {today_str} 모닝 인텔리전스 리포트 - {title}"
    msg_personal["From"] = f"Daily Intelligence Agent <{sender_email}>"
    msg_personal["To"] = receiver_email if receiver_email else ""
    msg_personal.attach(MIMEText(html_body, "html", "utf-8"))

    # 2. 네이버 블로그 자동 포스팅용 메일 설정
    msg_blog = MIMEMultipart("alternative")
    msg_blog["Subject"] = f"[{naver_category}] {title}"
    msg_blog["From"] = sender_email
    msg_blog["To"] = naver_blog_email if naver_blog_email else ""
    msg_blog.attach(MIMEText(html_body, "html", "utf-8"))

    success = False
    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(sender_email, sender_password)
            
            # 개인 메일 전송
            if receiver_email:
                server.sendmail(sender_email, receiver_email, msg_personal.as_string())
                logger.info("✅ 개인 수신 이메일 발송 완료")
                success = True
            
            # 네이버 블로그 포스팅 전송
            if naver_blog_email:
                server.sendmail(sender_email, naver_blog_email, msg_blog.as_string())
                logger.info("✅ 네이버 블로그 자동 포스팅 완료")
                success = True
                
        return success
    except Exception as e:
        logger.error(f"❌ 이메일/블로그 발송 실패: {e}")
        return False


# ==========================================
# 5단계: 자동화 메인 실행부 (Runner)
# ==========================================
def main():
    logger.info("========================================")
    logger.info("Daily Briefing Standalone Agent 기동")
    logger.info("========================================")

    # 설정 로드
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    naver_client_id = os.getenv("NAVER_CLIENT_ID")
    naver_client_secret = os.getenv("NAVER_CLIENT_SECRET")
    
    keywords_str = os.getenv("NEWS_KEYWORDS", "인공지능, 빅테크, IT 트렌드")
    keywords = [k.strip() for k in keywords_str.split(",") if k.strip()]
    
    slack_webhook = os.getenv("SLACK_WEBHOOK_URL")
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
    discord_webhook = os.getenv("DISCORD_WEBHOOK_URL")
    
    smtp_server = os.getenv("SMTP_SERVER")
    smtp_port = os.getenv("SMTP_PORT", "587")
    smtp_sender = os.getenv("SMTP_SENDER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    smtp_receiver = os.getenv("SMTP_RECEIVER")

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

    # 3단계 AI 생성
    logger.info("3단계: Gemini API를 사용하여 브리핑 생성 시작...")
    ai = AIEngine(gemini_api_key)
    briefing = ai.generate_briefing(unique_articles[:10])
    
    if "error" in briefing:
        logger.error(f"브리핑 생성 실패: {briefing['error']}")
        return

    logger.info(f"AI 브리핑 생성 성공: '{briefing.get('title')}'")

    # 4단계 전송
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
            
    # 이메일 및 네이버 블로그 메일 발송
    html_body = format_briefing_to_html(briefing)
    if send_email(title, html_body):
        sent_list.append("Email/NaverBlog")

    if sent_list:
        logger.info(f"배포 성공 채널 목록: {', '.join(sent_list)}")
    else:
        logger.warning("전송 설정된 배포 채널이 없어 발송되지 않았습니다.")
        
    logger.info("Daily Briefing Standalone Agent 업무 종료.")

if __name__ == "__main__":
    main()
