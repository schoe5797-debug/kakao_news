import html as ihtml
import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
from urllib.parse import urlparse, quote

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from readability import Document


# ── Article 데이터클래스 ─────────────────────────────────────
@dataclass(frozen=True)
class Article:
    idx: int
    title: str
    url: str
    source: str
    published: str
    published_dt: datetime        # 파싱된 datetime (정렬·필터용)
    text: str
    category: str                 # semiconductor | energy | auto | macro | global_event | ai
    is_critical: bool = False     # 리스크 신호어 감지 여부
    critical_signal: str = ""     # 어떤 신호어에 걸렸는지


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
KST = timezone(timedelta(hours=9))
MAX_AGE_SECONDS = int(os.getenv("MAX_AGE_HOURS", "24")) * 3600


# ── 구글 RSS 쿼리 (종목 직접 연관) ──────────────────────────
GOOGLE_RSS_QUERIES = [
    # 반도체
    ("Samsung Electronics semiconductor earnings", "semiconductor"),
    ("Samsung Electronics memory chip HBM", "semiconductor"),
    ("SK Hynix HBM memory", "semiconductor"),
    ("NVIDIA stock earnings AI chip", "semiconductor"),
    ("NVIDIA GPU data center", "semiconductor"),
    ("WC Co semiconductor Korea", "semiconductor"),
    ("TSMC AI chip demand foundry", "semiconductor"),
    # 에너지
    ("Doosan Enerbility nuclear reactor", "energy"),
    ("Korea nuclear power plant policy", "energy"),
    ("Constellation Energy CEG nuclear stock", "energy"),
    ("LNG natural gas price", "energy"),
    # 자동차부품 (한온시스템)
    ("Hanon Systems automotive HVAC", "auto"),
    ("Hanon Systems earnings EV thermal", "auto"),
    ("automotive parts EV cooling supply chain", "auto"),
    # 거시경제
    ("Federal Reserve interest rate decision", "macro"),
    ("US China trade tariff semiconductor", "macro"),
    ("Korea export economy won dollar", "macro"),
    ("Korea KOSPI foreign investor", "macro"),
    # 글로벌 이벤트
    ("Middle East oil geopolitical risk", "global_event"),
    ("geopolitical risk semiconductor supply", "global_event"),
    # AI / AGI
    ("AGI artificial general intelligence 2025", "ai"),
    ("OpenAI Google DeepMind AI breakthrough", "ai"),
    ("AI regulation policy latest", "ai"),
]

# ── 네이버 뉴스 검색 쿼리 (종목 직접 연관) ──────────────────
NAVER_QUERIES = [
    # 반도체
    ("삼성전자 반도체 실적", "semiconductor"),
    ("삼성전자 HBM 메모리", "semiconductor"),
    ("SK하이닉스 HBM", "semiconductor"),
    ("엔비디아 주가 실적", "semiconductor"),
    ("와이씨 반도체 주가", "semiconductor"),
    # 에너지
    ("두산에너빌리티 원전 수주", "energy"),
    ("한국 원전 수출 정책", "energy"),
    ("CEG Constellation Energy 주가", "energy"),
    ("LNG 천연가스 가격", "energy"),
    # 자동차부품
    ("한온시스템 주가 실적", "auto"),
    ("한온시스템 전기차 부품", "auto"),
    ("자동차부품 EV 열관리", "auto"),
    # 거시경제
    ("코스피 외국인 수급 환율", "macro"),
    ("미국 금리 한국 증시", "macro"),
    ("반도체 수출 무역 관세", "macro"),
    # 글로벌 이벤트
    ("중동 지정학 리스크 유가", "global_event"),
    # AI / AGI
    ("AGI 인공일반지능 최신", "ai"),
    ("AI 인공지능 최신 뉴스", "ai"),
    ("챗GPT 클로드 제미나이 최신", "ai"),
]

# ── 네이버 신문사 RSS ────────────────────────────────────────
NAVER_NEWSPAPER_RSS = [
    ("https://rss.hankyung.com/economy.xml", "macro"),
    ("https://rss.hankyung.com/it.xml", "semiconductor"),
    ("https://www.mk.co.kr/rss/30000001/", "macro"),
    ("https://www.mk.co.kr/rss/30200030/", "semiconductor"),
    ("https://feeds.feedburner.com/mt/economy", "macro"),
    ("https://www.sedaily.com/RSS/economic.xml", "macro"),
]

# ── 보유 종목 관련도 필터 키워드 ────────────────────────────
STOCK_KEYWORDS = [
    "삼성전자", "samsung electronics", "samsung",
    "두산에너빌리티", "doosan enerbility", "doosan",
    "와이씨", "wc co",
    "한온시스템", "hanon systems", "hanon",
    "엔비디아", "nvidia",
    "ceg", "constellation energy",
    "반도체", "semiconductor", "hbm", "ai chip", "gpu",
    "원전", "nuclear", "lng", "천연가스",
    "열관리", "hvac", "ev thermal", "전기차",
    "금리", "interest rate", "환율", "kospi", "tariff", "관세",
    "geopolit", "지정학", "유가", "oil price",
    "agi", "artificial general intelligence", "openai", "deepmind",
    "chatgpt", "claude", "gemini", "llm", "인공지능",
]

# ── 리스크 신호어 ────────────────────────────────────────────
RISK_SIGNALS = [
    "소송", "제소", "기소", "벌금", "과징금", "규제", "제재",
    "lawsuit", "indicted", "penalty", "sanction", "investigation",
    "파업", "해고", "구조조정", "사퇴", "경질", "내부고발",
    "strike", "layoff", "restructuring", "resign", "whistleblower",
    "유출", "해킹", "침해", "내부자", "기밀",
    "leak", "breach", "hacked", "espionage", "theft",
    "어닝쇼크", "적자전환", "하향조정", "신용등급",
    "earnings miss", "downgrade", "credit rating", "write-off",
    "공급 차질", "생산 중단", "리콜", "결함",
    "supply disruption", "production halt", "recall", "defect",
    "수출 규제", "블랙리스트", "거래 제한",
    "export ban", "blacklist", "trade restriction",
]

# ── 키워드 빈도 집계 불용어 ──────────────────────────────────
STOPWORDS = {
    "및", "등", "위한", "대한", "관련", "통해", "따른", "기반", "있는", "있다",
    "하는", "이후", "이번", "지난", "올해", "내년", "최근", "현재", "계속",
    "한국", "미국", "중국", "글로벌", "시장", "기업", "투자", "주가", "뉴스",
    "the", "a", "an", "of", "in", "to", "and", "for", "is", "on",
    "at", "by", "with", "from", "as", "that", "this", "its", "are",
    "will", "said", "says", "new", "year", "also", "after",
}

FALLBACK_TOPICS_PROMPT = """오늘은 {today}입니다.
수집된 뉴스가 없습니다. 아래 보유 종목과 관련된 오늘 날짜 기준 주요 이슈 및 투자 인사이트를 직접 분석해주세요.

보유 종목: 삼성전자, 와이씨, 두산에너빌리티, 한온시스템, 엔비디아, CEG(Constellation Energy)

다음 카테고리별로 최신 동향과 투자 인사이트를 작성해주세요:
- 반도체: AI 반도체 수요, 메모리 가격 동향, 주요 기업 실적
- 에너지: 원전 정책, LNG 가격, 재생에너지 동향
- 자동차부품: 전기차 열관리, 한온시스템 동향
- 거시경제: 금리 정책, 환율, 무역 이슈
- 글로벌 이벤트: 지정학적 리스크, 주요 이벤트

마지막에 오늘의 투자 인사이트를 종목별로 정리해주세요.
"""


# ── 헬퍼 함수 ────────────────────────────────────────────────
def _env(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"환경 변수 '{key}'가 설정되지 않았습니다. .env 파일을 확인하세요.")
    return val


def _env_str(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_int(key: str, default: int = 0) -> int:
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


# ── 날짜 파싱 ────────────────────────────────────────────────
_DATE_FORMATS = [
    "%a, %d %b %Y %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S GMT",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%S",
]


def parse_published(published: str) -> Optional[datetime]:
    """RSS published 문자열 → timezone-aware datetime."""
    if not published:
        return None
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(published, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def age_seconds(dt: datetime, now: datetime) -> float:
    return max((now - dt).total_seconds(), 0.0)


def is_recent(published_dt: Optional[datetime], now: datetime, max_age: int = MAX_AGE_SECONDS) -> bool:
    """날짜 파싱 실패 시 False → 날짜 불명 기사 차단."""
    if published_dt is None:
        return False
    return age_seconds(published_dt, now) <= max_age


# ── 기사 본문 추출 ───────────────────────────────────────────
def fetch_article_text(url: str, max_chars: int = 1500) -> str:
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
        resp.raise_for_status()
        doc = Document(resp.text)
        soup = BeautifulSoup(doc.summary(), "html.parser")
        return soup.get_text(separator=" ", strip=True)[:max_chars]
    except Exception as e:
        print(f"  [본문 추출 실패] {url}: {e}")
        return ""


# ── 관련도 필터 ──────────────────────────────────────────────
def is_relevant(article: Article) -> bool:
    """ai 카테고리는 무조건 통과, 나머지는 종목 키워드 필터."""
    if article.category == "ai":
        return True
    haystack = (article.title + " " + article.text).lower()
    return any(kw.lower() in haystack for kw in STOCK_KEYWORDS)


# ── 리스크 신호어 감지 ───────────────────────────────────────
def detect_risk_from_raw(title: str, text: str) -> Tuple[bool, str]:
    haystack = (title + " " + text).lower()
    for signal in RISK_SIGNALS:
        if signal.lower() in haystack:
            return True, signal
    return False, ""


# ── 키워드 빈도 집계 ─────────────────────────────────────────
def extract_top_keywords(articles: List[Article], top_n: int = 10) -> List[Tuple[str, int]]:
    """제목(가중치 3배) + 본문에서 자주 등장하는 키워드 top_n개 반환."""
    counter: Counter = Counter()
    for a in articles:
        text = (a.title + " ") * 3 + a.text
        words = re.findall(r"[가-힣]{2,}|[A-Za-z]{3,}", text)
        for w in words:
            wl = w.lower()
            if wl not in STOPWORDS:
                counter[wl] += 1
    return counter.most_common(top_n)


def format_keywords(keywords: List[Tuple[str, int]]) -> str:
    return ", ".join(f"{kw}({cnt}회)" for kw, cnt in keywords)


# ── 구글 뉴스 RSS 수집 ───────────────────────────────────────
def collect_google_rss(max_per_query: int = 3, now: Optional[datetime] = None) -> List[Article]:
    articles: List[Article] = []
    idx = 0
    if now is None:
        now = datetime.now(timezone.utc)

    for query, category in GOOGLE_RSS_QUERIES:
        encoded_query = quote(query)
        # when:1d + ts= 파라미터로 캐시 우회
        rss_url = (
            f"https://news.google.com/rss/search"
            f"?q={encoded_query}+when:1d&hl=en&gl=US&ceid=US:en&ts={int(time.time())}"
        )
        try:
            feed = feedparser.parse(rss_url, etag=None, modified=None)
            collected = 0
            for entry in feed.entries:
                if collected >= max_per_query:
                    break
                title = entry.get("title", "").strip()
                url = entry.get("link", "").strip()
                published = entry.get("published", "")
                source = urlparse(url).netloc.replace("www.", "")

                if not title or not url:
                    continue

                published_dt = parse_published(published)
                if not is_recent(published_dt, now):
                    age_str = f"{age_seconds(published_dt, now)/3600:.1f}h ago" if published_dt else "날짜불명"
                    print(f"  [Google RSS 스킵] {age_str} | {title[:40]}")
                    continue

                text = fetch_article_text(url)
                is_crit, signal = detect_risk_from_raw(title, text)
                articles.append(Article(
                    idx=idx, title=title, url=url, source=source,
                    published=published, published_dt=published_dt,
                    text=text, category=category,
                    is_critical=is_crit, critical_signal=signal,
                ))
                idx += 1
                collected += 1
                flag = "⚠️" if is_crit else "✓"
                age_h = age_seconds(published_dt, now) / 3600
                print(f"  [Google RSS {flag}] {age_h:.1f}h ago | {category} | {title[:45]}")
        except Exception as e:
            print(f"  [Google RSS 실패] {query}: {e}")

    return articles


# ── 네이버 뉴스 검색 수집 ────────────────────────────────────
def collect_naver_search(max_per_query: int = 3, now: Optional[datetime] = None) -> List[Article]:
    articles: List[Article] = []
    idx = 10000
    if now is None:
        now = datetime.now(timezone.utc)

    client_id = os.getenv("NAVER_CLIENT_ID")
    client_secret = os.getenv("NAVER_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("  [네이버 API] 키 미설정, 건너뜀")
        return articles

    headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}

    for query, category in NAVER_QUERIES:
        try:
            resp = requests.get(
                "https://openapi.naver.com/v1/search/news.json",
                headers={**headers, "Cache-Control": "no-cache"},
                params={"query": query, "display": max_per_query * 5, "sort": "date"},
                timeout=10,
            )
            resp.raise_for_status()
            collected = 0
            for item in resp.json().get("items", []):
                if collected >= max_per_query:
                    break
                title = BeautifulSoup(item.get("title", ""), "html.parser").get_text()
                url = item.get("originallink") or item.get("link", "")
                published = item.get("pubDate", "")
                source = urlparse(url).netloc.replace("www.", "")
                description = BeautifulSoup(item.get("description", ""), "html.parser").get_text()

                if not title or not url:
                    continue

                published_dt = parse_published(published)
                if not is_recent(published_dt, now):
                    age_str = f"{age_seconds(published_dt, now)/3600:.1f}h ago" if published_dt else "날짜불명"
                    print(f"  [Naver 스킵] {age_str} | {title[:40]}")
                    continue

                text = fetch_article_text(url) or description
                is_crit, signal = detect_risk_from_raw(title, text)
                articles.append(Article(
                    idx=idx, title=title, url=url, source=source,
                    published=published, published_dt=published_dt,
                    text=text, category=category,
                    is_critical=is_crit, critical_signal=signal,
                ))
                idx += 1
                collected += 1
                flag = "⚠️" if is_crit else "✓"
                age_h = age_seconds(published_dt, now) / 3600
                print(f"  [Naver {flag}] {age_h:.1f}h ago | {category} | {title[:45]}")
        except Exception as e:
            print(f"  [Naver Search 실패] {query}: {e}")

    return articles


# ── 네이버 신문사 RSS 수집 ───────────────────────────────────
def collect_naver_newspaper_rss(max_per_feed: int = 5, now: Optional[datetime] = None) -> List[Article]:
    articles: List[Article] = []
    idx = 20000
    if now is None:
        now = datetime.now(timezone.utc)

    for rss_url, category in NAVER_NEWSPAPER_RSS:
        try:
            feed = feedparser.parse(rss_url)
            source = urlparse(rss_url).netloc.replace("www.", "").replace("rss.", "")
            collected = 0
            for entry in feed.entries:
                if collected >= max_per_feed:
                    break
                title = entry.get("title", "").strip()
                url = entry.get("link", "").strip()
                published = entry.get("published", "")

                if not title or not url:
                    continue

                published_dt = parse_published(published)
                if not is_recent(published_dt, now):
                    age_str = f"{age_seconds(published_dt, now)/3600:.1f}h ago" if published_dt else "날짜불명"
                    print(f"  [Newspaper 스킵] {age_str} | {title[:40]}")
                    continue

                text = fetch_article_text(url)
                is_crit, signal = detect_risk_from_raw(title, text)
                articles.append(Article(
                    idx=idx, title=title, url=url, source=source,
                    published=published, published_dt=published_dt,
                    text=text, category=category,
                    is_critical=is_crit, critical_signal=signal,
                ))
                idx += 1
                collected += 1
                flag = "⚠️" if is_crit else "✓"
                age_h = age_seconds(published_dt, now) / 3600
                print(f"  [Newspaper {flag}] {age_h:.1f}h ago | {category} | {title[:45]}")
        except Exception as e:
            print(f"  [Newspaper RSS 실패] {rss_url}: {e}")

    return articles


# ── 중복 제거 + 관련도 필터 + 정렬 ─────────────────────────
def deduplicate_and_sort(articles: List[Article]) -> List[Article]:
    """URL 기준 중복 제거 → 관련도 필터 → 리스크 기사 우선 + 최신순 정렬."""
    seen: set = set()
    unique: List[Article] = []
    for a in articles:
        if a.url not in seen:
            seen.add(a.url)
            unique.append(a)

    before = len(unique)
    unique = [a for a in unique if is_relevant(a)]
    print(f"[관련도 필터] {before}개 → {len(unique)}개")

    # 리스크 기사 먼저, 그 다음 최신순
    unique.sort(key=lambda a: (not a.is_critical, -a.published_dt.timestamp()))
    return unique


# ── 전체 수집 ────────────────────────────────────────────────
def collect_articles(max_articles: int = 60) -> List[Article]:
    now = datetime.now(timezone.utc)
    print(f"[기준 시각] {now.astimezone(KST).strftime('%Y-%m-%d %H:%M')} KST")
    print(f"[필터 기준] {MAX_AGE_SECONDS // 3600}시간 이내 기사만 수집")

    all_articles: List[Article] = []
    print("[1/3] 구글 뉴스 RSS 수집 중...")
    all_articles += collect_google_rss(max_per_query=3, now=now)
    print("[2/3] 네이버 뉴스 검색 수집 중...")
    all_articles += collect_naver_search(max_per_query=3, now=now)
    print("[3/3] 네이버 신문사 RSS 수집 중...")
    all_articles += collect_naver_newspaper_rss(max_per_feed=5, now=now)

    all_articles = deduplicate_and_sort(all_articles)
    print(f"[중복 제거·정렬 후] {len(all_articles)}개")

    critical_count = sum(1 for a in all_articles if a.is_critical)
    if critical_count:
        print(f"[⚠️ 리스크 기사] {critical_count}개 감지")

    return all_articles[:max_articles]


# ── Gemini 호출 ──────────────────────────────────────────────
def gemini_summarize(prompt: str) -> str:
    from google import genai
    client = genai.Client(api_key=_env("GEMINI_API_KEY"))
    resp = client.models.generate_content(
        model=_env_str("GEMINI_MODEL", "gemini-2.0-flash"),
        contents=prompt,
    )
    return (getattr(resp, "text", "")).strip()


# ── 요약 생성 ────────────────────────────────────────────────
def build_summary(articles: List[Article], today: str) -> str:
    if not articles:
        return gemini_summarize(FALLBACK_TOPICS_PROMPT.format(today=today))

    # ai 카테고리는 별도 처리, 투자 카테고리만 분류
    CATEGORIES = ["semiconductor", "energy", "auto", "macro", "global_event"]
    filled = {cat for cat in CATEGORIES if any(a.category == cat for a in articles)}
    empty  = [cat for cat in CATEGORIES if cat not in filled]

    CATEGORY_KR = {
        "semiconductor": "반도체",
        "energy": "에너지",
        "auto": "자동차부품(한온시스템)",
        "macro": "거시경제",
        "global_event": "글로벌 이벤트",
    }

    def fmt(cat: str) -> str:
        """기존 원본과 동일한 포맷 유지: [출처] 제목 + 본문 800자.
        리스크 기사는 앞에 [⚠️ 신호어] 표시 추가."""
        cat_articles = [a for a in articles if a.category == cat]
        if not cat_articles:
            return ""
        lines = []
        for a in cat_articles:
            prefix = "[⚠️ " + a.critical_signal + "] " if a.is_critical else ""
            lines.append(f"- {prefix}[{a.source}] {a.title}\n  {a.text[:800]}")
        return "\n".join(lines)

    # ── 키워드 빈도 집계 (투자 관련 기사 대상, ai 제외) ─────
    invest_articles = [a for a in articles if a.category != "ai"]
    top_keywords = extract_top_keywords(invest_articles, top_n=10)
    keyword_str = format_keywords(top_keywords)
    print(f"[상위 키워드] {keyword_str}")

    # ── 빈 카테고리: Gemini에게 최근 핫이슈 요청 ────────────
    hot_issue_sections = ""
    if empty:
        empty_kr = ", ".join(CATEGORY_KR[c] for c in empty)
        hot_issue_prompt = f"""오늘은 {today}입니다.
아래 카테고리에 대해 오늘 날짜({today}) 기준으로 **최근 며칠 사이 뉴스에 가장 많이 등장한 기업 이슈 또는 시장 이슈**를 카테고리별로 요약해주세요.

조건:
- 보유 종목 우선 언급: 삼성전자, 와이씨, 두산에너빌리티, 한온시스템, 엔비디아, CEG(Constellation Energy)
- 보유 종목 이슈가 없다면 해당 분야에서 가장 화제가 된 기업/이슈를 대신 소개
- 각 카테고리 3~5줄 이내
- 출처가 불분명한 내용은 "추정" 또는 "알려진 바에 따르면" 등으로 표현

요약이 필요한 카테고리: {empty_kr}

각 카테고리 형식:
===== [카테고리명] 최근 핫이슈 (자체 분석) =====
(내용)
"""
        hot_issue_sections = gemini_summarize(hot_issue_prompt)

    # ── 리스크 기사 블록 (중대 이슈 최우선 분석용) ──────────
    critical_articles = [a for a in invest_articles if a.is_critical]
    critical_section = ""
    if critical_articles:
        critical_block = "\n".join(
            "[⚠️ '" + a.critical_signal + "' 감지 | " + a.source + "] " + a.title + "\n" + a.text[:800]
            for a in critical_articles
        )
        critical_section = (
            "\n⚠️⚠️ 아래는 중대 이슈 신호어가 감지된 기사입니다. "
            "최우선으로 상세 분석하고, 보유 종목에 미치는 영향을 반드시 서술해주세요:\n"
            + critical_block + "\n---\n"
        )

    # ── 1단계: 기사별 심층 분석 (기존 원본 프롬프트 구조 유지) ─
    article_blocks = "\n\n".join(
        f"===== {CATEGORY_KR[cat]} =====\n{fmt(cat)}"
        for cat in CATEGORIES
        if cat in filled
    )

    per_article_prompt = f"""오늘은 {today}입니다.
아래 기사들을 **각각 개별적으로** 분석해주세요.

분석 조건:
- 보유 종목: 삼성전자, 와이씨, 두산에너빌리티, 한온시스템, 엔비디아, CEG(Constellation Energy)
- 영어 기사는 한국어로 번역

**날짜 규칙 (엄격히 준수)**
- 오늘({today}) 발행된 기사만 분석합니다.
- 오늘 날짜가 아닌 기사는 분석하지 말고 "날짜 미해당 - 생략"으로 표시하세요.
- 예외: 오늘 기사가 직접 언급하거나 이어지는 전날 이슈는 "(전일 이슈 지속)"으로 1줄만 표시.

**서술 방식**
- 기사마다 제목을 먼저 쓰고, 아래 항목 중 해당되는 것만 자연스럽게 서술하세요.
  - 무슨 일이 일어났는지
  - 관련 이해관계자들의 입장 (있을 경우)
  - 왜 이 이슈가 중요한지 / 배경
  - 앞으로 어떻게 흘러갈지 전망
  - 보유 종목 영향도 (긍정/부정/중립 + 이유)
- 단순 수치 발표나 단신이라면 2~3줄로 간결하게 마무리해도 됩니다.
{critical_section}
{article_blocks}
"""
    per_article_analysis = gemini_summarize(per_article_prompt)

    # ── AI/AGI 3줄 요약 (별도 Gemini 호출, 기존 흐름에 추가) ─
    ai_articles = [a for a in articles if a.category == "ai"]
    if ai_articles:
        ai_block = "\n".join(
            f"- [{a.source}] {a.title}\n  {a.text[:600]}"
            for a in ai_articles
        )
        ai_summary = gemini_summarize(f"""오늘은 {today}입니다.
아래 AI/AGI 관련 기사들을 **딱 3줄**로 핵심만 요약해주세요.
투자자 관점에서 중요한 내용 위주로 작성해주세요.

{ai_block}
""")
    else:
        ai_summary = gemini_summarize(f"""오늘은 {today}입니다.
오늘 날짜 기준 AI/AGI 분야에서 가장 중요한 최신 소식을 **딱 3줄**로 요약해주세요.
투자자 관점에서 중요한 내용 위주로 작성해주세요.
""")

    # ── 2단계: 카테고리별 종합 요약 + 투자 인사이트
    #           (기존 원본 출력 형식 완전 유지) ──────────────
    hot_section_note = ""
    if hot_issue_sections:
        empty_kr = ", ".join(CATEGORY_KR[c] for c in empty)
        hot_section_note = f"""
※ [{empty_kr}] 카테고리는 오늘 수집된 기사가 없어 최근 핫이슈로 대체합니다:

{hot_issue_sections}
"""

    risk_note = (
        "\n- ⚠️ 리스크 기사가 감지된 종목은 투자 인사이트에서 대응 방향을 반드시 포함해주세요."
        if critical_articles else ""
    )

    category_summary_prompt = f"""오늘은 {today}입니다.
아래는 오늘 수집된 뉴스의 기사별 심층 분석 결과입니다.
{hot_section_note}
이를 바탕으로 **카테고리별 종합 요약**과 **투자 인사이트**를 작성해주세요.

작성 조건:
- 보유 종목: 삼성전자, 와이씨, 두산에너빌리티, 한온시스템, 엔비디아, CEG(Constellation Energy)
- 비슷한 기사는 묶어서 중복 없이 정리
- 각 카테고리 3줄 이내 핵심 요약
- 기사 없는 카테고리는 위 핫이슈 내용을 바탕으로 요약
- 마지막에 오늘의 투자 인사이트 (전체 종합, 종목별 대응 방향 포함){risk_note}

출력 형식:
===== 💾 반도체 요약 =====
(3줄 이내, 기사 없으면 최근 핫이슈 기반)

===== ⚡ 에너지 요약 =====
(3줄 이내, 기사 없으면 최근 핫이슈 기반)

===== 🚗 자동차부품(한온시스템) 요약 =====
(3줄 이내, 기사 없으면 최근 핫이슈 기반)

===== 📊 거시경제 요약 =====
(3줄 이내, 기사 없으면 최근 핫이슈 기반)

===== 🌏 글로벌 이벤트 요약 =====
(3줄 이내, 기사 없으면 최근 핫이슈 기반)

===== 💡 오늘의 투자 인사이트 =====
(종목별 대응 방향 포함, 5줄 이내)

--- 기사별 분석 원문 ---
{per_article_analysis}
"""
    category_summary = gemini_summarize(category_summary_prompt)

    # 핫이슈 섹션이 있으면 마지막에 별도 첨부 (기존 원본과 동일)
    hot_appendix = (
        f"\n\n{'='*60}\n\n🔥 기사 없는 카테고리 - 최근 핫이슈 (자체 분석)\n\n{'='*60}\n\n{hot_issue_sections}"
        if hot_issue_sections else ""
    )

    return (
        f"{category_summary}"
        f"\n\n{'='*60}\n\n🤖 AI/AGI 최신 소식 (3줄 요약)\n\n{'='*60}\n\n{ai_summary}"
        f"\n\n{'='*60}\n\n📋 기사별 상세 분석\n\n{'='*60}\n\n{per_article_analysis}"
        f"{hot_appendix}"
    )


# ── HTML 생성 ────────────────────────────────────────────────
CATEGORY_LABEL = {
    "semiconductor": "💾 반도체",
    "energy": "⚡ 에너지",
    "auto": "🚗 자동차부품(한온시스템)",
    "macro": "📊 거시경제",
    "global_event": "🌏 글로벌 이벤트",
    "ai": "🤖 AI/AGI",
}


def build_html(header: str, summary: str, articles: List[Article]) -> str:
    sections = ""
    for cat, label in CATEGORY_LABEL.items():
        cat_articles = [a for a in articles if a.category == cat]
        if not cat_articles:
            continue

        def article_li(a: Article) -> str:
            critical_tag = '<span class="critical">⚠️</span> ' if a.is_critical else ""
            time_str = a.published_dt.astimezone(KST).strftime("%H:%M")
            return (
                f'<li>{critical_tag}'
                f'<span class="age">{time_str}</span> '
                f'<a href="{a.url}" target="_blank">{ihtml.escape(a.title)}</a> '
                f'<span class="source">({a.source})</span>'
                f'</li>'
            )

        items = "\n".join(article_li(a) for a in cat_articles)
        sections += f"<h2>{label}</h2><ul>{items}</ul>\n"

    summary_html = ihtml.escape(summary)
    summary_html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", summary_html)
    summary_html = summary_html.replace("\n", "<br>")

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{ihtml.escape(header)}</title>
<style>
  body {{ font-family: 'Apple SD Gothic Neo', sans-serif; max-width: 860px; margin: 40px auto; padding: 0 20px; line-height: 1.8; color: #222; }}
  h1 {{ font-size: 1.5em; border-bottom: 2px solid #333; padding-bottom: 8px; }}
  h2 {{ font-size: 1.15em; margin-top: 32px; color: #444; }}
  .summary-box {{ background: #f4f8ff; border-left: 4px solid #3a7bd5; padding: 20px 24px; border-radius: 6px; margin: 20px 0; }}
  ul {{ padding-left: 18px; }}
  li {{ margin: 6px 0; }}
  a {{ color: #1a5cc8; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .source {{ color: #999; font-size: 0.82em; }}
  .age {{ color: #e07000; font-size: 0.85em; font-weight: bold; margin-right: 4px; }}
  .critical {{ color: #d00; font-weight: bold; margin-right: 4px; }}
  .updated {{ color: #aaa; font-size: 0.85em; margin-top: 4px; }}
</style>
</head>
<body>
<h1>📰 {ihtml.escape(header)}</h1>
<p class="updated">업데이트: {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST</p>
<h2>🤖 AI 투자 브리핑</h2>
<div class="summary-box">{summary_html}</div>
{sections}
</body>
</html>"""


# ── 카카오 전송 ──────────────────────────────────────────────
def kakao_send_to_me(access_token: str, text: str, page_url: str) -> None:
    template_object = {
        "object_type": "text",
        "text": text,
        "link": {"web_url": page_url, "mobile_web_url": page_url},
        "buttons": [{"title": "뉴스 요약 크게보기",
                     "link": {"web_url": page_url, "mobile_web_url": page_url}}],
    }
    resp = requests.post(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        headers={"Authorization": f"Bearer {access_token}"},
        data={"template_object": json.dumps(template_object, ensure_ascii=False)},
    )
    print(f"[Kakao] status={resp.status_code}, body={resp.text}")
    if resp.status_code != 200:
        raise RuntimeError(f"카카오 전송 실패: {resp.status_code} {resp.text}")


# ── 메인 ─────────────────────────────────────────────────────
def main() -> None:
    load_dotenv()

    today = datetime.now(KST).strftime("%Y-%m-%d")
    header = f"[투자 뉴스 브리핑] {today}"
    max_articles = _env_int("MAX_ARTICLES", 60)

    print(f"[수집 시작] 최대 {max_articles}개")
    articles = collect_articles(max_articles)
    print(f"[수집 완료] {len(articles)}개")

    summary = build_summary(articles, today)
    print("[요약 완료]")

    html_content = build_html(header, summary, articles)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_content)
    print("[HTML 생성 완료]")

    GITHUB_ID = "schoe5797-debug"
    REPO_NAME = "kakao_news"
    page_url = f"https://{GITHUB_ID}.github.io/{REPO_NAME}/"
    print(f"[page_url] {page_url}")

    token_resp = requests.post(
        "https://kauth.kakao.com/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": _env("KAKAO_REST_API_KEY"),
            "client_secret": _env("KAKAO_CLIENT_SECRET"),
            "refresh_token": _env("KAKAO_REFRESH_TOKEN"),
        },
    ).json()
    print(f"[Kakao Token] {token_resp}")

    access_token = token_resp.get("access_token")
    if not access_token:
        raise RuntimeError(f"액세스 토큰 갱신 실패: {token_resp}")

    # 카톡 메시지 (핵심 요약 앞 200자만) - 기존 원본과 동일
    short_summary = summary[:200].replace("\n", " ")
    kakao_text = f"{header}\n\n{short_summary}...\n\n자세한 내용은 아래 버튼을 눌러주세요."
    kakao_send_to_me(access_token, kakao_text, page_url)


if __name__ == "__main__":
    main()
