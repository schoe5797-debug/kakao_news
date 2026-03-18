import html as ihtml
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from urllib.parse import urlparse, quote

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from readability import Document


@dataclass(frozen=True)
class Article:
    idx: int
    title: str
    url: str
    source: str
    published: str
    text: str
    category: str  # 'semiconductor' | 'energy' | 'macro' | 'global_event'


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
KST = timezone(timedelta(hours=9))

# ── 구글 RSS (해외) ──────────────────────────────────────────
GOOGLE_RSS_QUERIES = [
    # 반도체
    ("semiconductor market", "semiconductor"),
    ("NVIDIA stock earnings", "semiconductor"),
    ("Samsung Electronics chip", "semiconductor"),
    ("TSMC foundry", "semiconductor"),
    ("AI chip demand", "semiconductor"),
    # 에너지
    ("nuclear energy stock", "energy"),
    ("CEG Constellation Energy", "energy"),
    ("Doosan Enerbility", "energy"),
    ("LNG oil price", "energy"),
    ("renewable energy policy", "energy"),
    # 거시경제
    ("Federal Reserve interest rate", "macro"),
    ("US China trade tariff", "macro"),
    ("global inflation economy", "macro"),
    ("Korea economy export", "macro"),
    # 글로벌 이벤트 (전쟁·지정학·팬데믹 등)
    ("Middle East war oil", "global_event"),
    ("geopolitical risk market", "global_event"),
    ("pandemic outbreak disease", "global_event"),
]

# ── 네이버 뉴스 검색 쿼리 (국내) ────────────────────────────
NAVER_QUERIES = [
    ("삼성전자 반도체", "semiconductor"),
    ("SK하이닉스 HBM", "semiconductor"),
    ("엔비디아 주가", "semiconductor"),
    ("반도체 수출 경기", "semiconductor"),
    ("와이씨 주가", "semiconductor"),
    ("두산에너빌리티 원전", "energy"),
    ("한국 원자력 에너지", "energy"),
    ("CEG 에너지 주가", "energy"),
    ("LNG 천연가스 가격", "energy"),
    ("한국 경제 수출 환율", "macro"),
    ("코스피 외국인 수급", "macro"),
    ("미국 금리 한국 영향", "macro"),
    ("중동 전쟁 한국 경제", "global_event"),
    ("지정학 리스크 주식", "global_event"),
]

# ── 네이버 주요 신문사 RSS ───────────────────────────────────
NAVER_NEWSPAPER_RSS = [
    ("https://rss.hankyung.com/economy.xml", "macro"),
    ("https://rss.hankyung.com/it.xml", "semiconductor"),
    ("https://www.mk.co.kr/rss/30000001/", "macro"),
    ("https://www.mk.co.kr/rss/30200030/", "semiconductor"),
    ("https://feeds.feedburner.com/mt/economy", "macro"),
    ("https://www.sedaily.com/RSS/economic.xml", "macro"),
]

FALLBACK_TOPICS_PROMPT = """오늘은 {today}입니다.
수집된 뉴스가 없습니다. 아래 보유 종목과 관련된 오늘 날짜 기준 주요 이슈 및 투자 인사이트를 직접 분석해주세요.

보유 종목: 삼성전자, 와이씨, 두산에너빌리티, 엔비디아, CEG(Constellation Energy)

다음 카테고리별로 최신 동향과 투자 인사이트를 작성해주세요:
- 반도체: AI 반도체 수요, 메모리 가격 동향, 주요 기업 실적
- 에너지: 원전 정책, LNG 가격, 재생에너지 동향
- 거시경제: 금리 정책, 환율, 무역 이슈
- 글로벌 이벤트: 지정학적 리스크, 주요 이벤트

마지막에 오늘의 투자 인사이트를 종목별로 정리해주세요.
"""


# ── 헬퍼 함수 ────────────────────────────────────────────────
def _env(key: str) -> str:
    """환경 변수를 읽어옵니다. 없으면 RuntimeError."""
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"환경 변수 '{key}'가 설정되지 않았습니다. .env 파일을 확인하세요.")
    return val


def _env_str(key: str, default: str = "") -> str:
    """환경 변수를 읽어옵니다. 없으면 default 반환."""
    return os.getenv(key, default)


def _env_int(key: str, default: int = 0) -> int:
    """환경 변수를 정수로 읽어옵니다. 없으면 default 반환."""
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


# ── 날짜 파싱 및 오늘 기사 여부 판단 ────────────────────────
def parse_published(published: str) -> Optional[datetime]:
    """RSS published 문자열을 datetime으로 변환합니다."""
    if not published:
        return None
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",   # RFC 2822 (RSS 표준)
        "%a, %d %b %Y %H:%M:%S GMT",
        "%Y-%m-%dT%H:%M:%S%z",         # ISO 8601
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(published, fmt)
        except ValueError:
            continue
    return None


def is_today_or_recent(published: str, today_kst: datetime, max_hours: int = 24) -> bool:
    """기사가 오늘(KST 기준) 발행됐는지 확인합니다.
    published 파싱 실패 시 True를 반환해 기사를 보존합니다 (누락 방지).
    """
    dt = parse_published(published)
    if dt is None:
        return True  # 날짜 불명확 → 포함 (안전 처리)
    # timezone-aware 비교
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    today_utc_start = today_kst.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    diff = today_kst.astimezone(timezone.utc) - dt
    # 오늘 자정 이후이거나, max_hours 이내면 포함
    return dt >= today_utc_start or diff.total_seconds() <= max_hours * 3600


# ── 기사 본문 추출 ───────────────────────────────────────────
def fetch_article_text(url: str, max_chars: int = 1500) -> str:
    """URL에서 기사 본문을 추출합니다."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        resp.raise_for_status()
        doc = Document(resp.text)
        soup = BeautifulSoup(doc.summary(), "html.parser")
        text = soup.get_text(separator=" ", strip=True)
        return text[:max_chars]
    except Exception as e:
        print(f"  [본문 추출 실패] {url}: {e}")
        return ""


# ── 구글 뉴스 RSS 수집 ───────────────────────────────────────
def collect_google_rss(max_per_query: int = 3, today_kst: Optional[datetime] = None) -> List[Article]:
    """Google News RSS에서 오늘 기사를 수집합니다."""
    articles: List[Article] = []
    idx = 0
    if today_kst is None:
        today_kst = datetime.now(KST)

    for query, category in GOOGLE_RSS_QUERIES:
        encoded_query = quote(query)
        # after: 파라미터로 구글 RSS 단계에서도 최근 기사만 요청
        rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en&gl=US&ceid=US:en&after={today_kst.strftime('%Y-%m-%d')}"
        try:
            feed = feedparser.parse(rss_url)
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

                # 오늘 날짜 기사가 아니면 건너뜀
                if not is_today_or_recent(published, today_kst):
                    print(f"  [Google RSS 날짜 필터] 스킵: {published[:16]} | {title[:40]}")
                    continue

                text = fetch_article_text(url)
                articles.append(Article(
                    idx=idx,
                    title=title,
                    url=url,
                    source=source,
                    published=published,
                    text=text,
                    category=category,
                ))
                idx += 1
                collected += 1
                print(f"  [Google RSS] {category} | {title[:50]}")
        except Exception as e:
            print(f"  [Google RSS 실패] {query}: {e}")

    return articles


# ── 네이버 뉴스 검색 수집 ────────────────────────────────────
def collect_naver_search(max_per_query: int = 3, today_kst: Optional[datetime] = None) -> List[Article]:
    """네이버 뉴스 검색 API로 오늘 기사를 수집합니다."""
    articles: List[Article] = []
    idx = 10000
    if today_kst is None:
        today_kst = datetime.now(KST)

    client_id = os.getenv("NAVER_CLIENT_ID")
    client_secret = os.getenv("NAVER_CLIENT_SECRET")

    if not client_id or not client_secret:
        print("  [네이버 API] 키 미설정, 네이버 검색 건너뜀")
        return articles

    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }

    for query, category in NAVER_QUERIES:
        try:
            # 넉넉하게 가져온 뒤 날짜 필터 적용
            resp = requests.get(
                "https://openapi.naver.com/v1/search/news.json",
                headers=headers,
                params={"query": query, "display": max_per_query * 3, "sort": "date"},
                timeout=10,
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
            collected = 0
            for item in items:
                if collected >= max_per_query:
                    break
                title = BeautifulSoup(item.get("title", ""), "html.parser").get_text()
                url = item.get("originallink") or item.get("link", "")
                published = item.get("pubDate", "")
                source = urlparse(url).netloc.replace("www.", "")
                description = BeautifulSoup(item.get("description", ""), "html.parser").get_text()

                if not title or not url:
                    continue

                # 오늘 날짜 기사가 아니면 건너뜀
                if not is_today_or_recent(published, today_kst):
                    print(f"  [Naver 날짜 필터] 스킵: {published[:16]} | {title[:40]}")
                    continue

                text = fetch_article_text(url) or description
                articles.append(Article(
                    idx=idx,
                    title=title,
                    url=url,
                    source=source,
                    published=published,
                    text=text,
                    category=category,
                ))
                idx += 1
                collected += 1
                print(f"  [Naver Search] {category} | {title[:50]}")
        except Exception as e:
            print(f"  [Naver Search 실패] {query}: {e}")

    return articles


# ── 네이버 신문사 RSS 수집 ───────────────────────────────────
def collect_naver_newspaper_rss(max_per_feed: int = 5, today_kst: Optional[datetime] = None) -> List[Article]:
    """네이버 주요 신문사 RSS에서 오늘 기사를 수집합니다."""
    articles: List[Article] = []
    idx = 20000
    if today_kst is None:
        today_kst = datetime.now(KST)

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

                # 오늘 날짜 기사가 아니면 건너뜀
                if not is_today_or_recent(published, today_kst):
                    print(f"  [Newspaper 날짜 필터] 스킵: {published[:16]} | {title[:40]}")
                    continue

                text = fetch_article_text(url)
                articles.append(Article(
                    idx=idx,
                    title=title,
                    url=url,
                    source=source,
                    published=published,
                    text=text,
                    category=category,
                ))
                idx += 1
                collected += 1
                print(f"  [Newspaper RSS] {category} | {title[:50]}")
        except Exception as e:
            print(f"  [Newspaper RSS 실패] {rss_url}: {e}")

    return articles


# ── 중복 제거 ────────────────────────────────────────────────
def deduplicate(articles: List[Article]) -> List[Article]:
    """URL 기준으로 중복 기사를 제거합니다."""
    seen_urls: set = set()
    result: List[Article] = []
    for a in articles:
        if a.url not in seen_urls:
            seen_urls.add(a.url)
            result.append(a)
    return result


# ── 전체 수집 진입점 ─────────────────────────────────────────
def collect_articles(max_articles: int = 60) -> List[Article]:
    """모든 소스에서 기사를 수집하고 중복을 제거합니다."""
    all_articles: List[Article] = []
    today_kst = datetime.now(KST)

    print("[1/3] 구글 뉴스 RSS 수집 중...")
    all_articles += collect_google_rss(max_per_query=3, today_kst=today_kst)

    print("[2/3] 네이버 뉴스 검색 수집 중...")
    all_articles += collect_naver_search(max_per_query=3, today_kst=today_kst)

    print("[3/3] 네이버 신문사 RSS 수집 중...")
    all_articles += collect_naver_newspaper_rss(max_per_feed=5, today_kst=today_kst)

    all_articles = deduplicate(all_articles)
    print(f"[중복 제거 후] {len(all_articles)}개")

    return all_articles[:max_articles]


def gemini_summarize(prompt: str) -> str:
    from google import genai
    client = genai.Client(api_key=_env("GEMINI_API_KEY"))
    resp = client.models.generate_content(
        model=_env_str("GEMINI_MODEL", "gemini-2.0-flash"),
        contents=prompt,
    )
    return (getattr(resp, "text", "")).strip()


def build_summary(articles: List[Article], today: str) -> str:
    if not articles:
        prompt = FALLBACK_TOPICS_PROMPT.format(today=today)
        return gemini_summarize(prompt)

    # 오늘 기사가 있는 카테고리 / 없는 카테고리 분류
    CATEGORIES = ["semiconductor", "energy", "macro", "global_event"]
    filled = {cat for cat in CATEGORIES if any(a.category == cat for a in articles)}
    empty  = [cat for cat in CATEGORIES if cat not in filled]

    CATEGORY_KR = {
        "semiconductor": "반도체",
        "energy": "에너지",
        "macro": "거시경제",
        "global_event": "글로벌 이벤트",
    }

    def fmt(cat: str) -> str:
        cat_articles = [a for a in articles if a.category == cat]
        if cat_articles:
            return "\n".join(
                f"- [{a.source}] {a.title}\n  {a.text[:800]}"
                for a in cat_articles
            )
        return ""  # 빈 카테고리는 별도 처리

    # ── 빈 카테고리: Gemini에게 최근 핫이슈 요청 ────────────
    hot_issue_sections = ""
    if empty:
        empty_kr = ", ".join(CATEGORY_KR[c] for c in empty)
        hot_issue_prompt = f"""오늘은 {today}입니다.
아래 카테고리에 대해 오늘 날짜({today}) 기준으로 **최근 며칠 사이 뉴스에 가장 많이 등장한 기업 이슈 또는 시장 이슈**를 카테고리별로 요약해주세요.

조건:
- 보유 종목 우선 언급: 삼성전자, 와이씨, 두산에너빌리티, 엔비디아, CEG(Constellation Energy)
- 보유 종목 이슈가 없다면 해당 분야에서 가장 화제가 된 기업/이슈를 대신 소개
- 각 카테고리 3~5줄 이내
- 출처가 불분명한 내용은 "추정" 또는 "알려진 바에 따르면" 등으로 표현

요약이 필요한 카테고리: {empty_kr}

각 카테고리 형식:
===== [카테고리명] 최근 핫이슈 (자체 분석) =====
(내용)
"""
        hot_issue_sections = gemini_summarize(hot_issue_prompt)

    # ── 1단계: 기사별 심층 분석 ──────────────────────────────
    article_blocks = "\n\n".join(
        f"===== {CATEGORY_KR[cat]} =====\n{fmt(cat)}"
        for cat in CATEGORIES
        if cat in filled
    )

    per_article_prompt = f"""오늘은 {today}입니다.
아래 기사들을 **각각 개별적으로** 분석해주세요.

분석 조건:
- 보유 종목: 삼성전자, 와이씨, 두산에너빌리티, 엔비디아, CEG(Constellation Energy)
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

{article_blocks}
"""

    per_article_analysis = gemini_summarize(per_article_prompt)

    # ── 2단계: 카테고리별 종합 요약 + 투자 인사이트 ──────────
    hot_section_note = ""
    if hot_issue_sections:
        empty_kr = ", ".join(CATEGORY_KR[c] for c in empty)
        hot_section_note = f"""
※ [{empty_kr}] 카테고리는 오늘 수집된 기사가 없어 최근 핫이슈로 대체합니다:

{hot_issue_sections}
"""

    category_summary_prompt = f"""오늘은 {today}입니다.
아래는 오늘 수집된 뉴스의 기사별 심층 분석 결과입니다.
{hot_section_note}
이를 바탕으로 **카테고리별 종합 요약**과 **투자 인사이트**를 작성해주세요.

작성 조건:
- 보유 종목: 삼성전자, 와이씨, 두산에너빌리티, 엔비디아, CEG(Constellation Energy)
- 비슷한 기사는 묶어서 중복 없이 정리
- 각 카테고리 3줄 이내 핵심 요약
- 기사 없는 카테고리는 위 핫이슈 내용을 바탕으로 요약
- 마지막에 오늘의 투자 인사이트 (전체 종합, 종목별 대응 방향 포함)

출력 형식:
===== 💾 반도체 요약 =====
(3줄 이내, 기사 없으면 최근 핫이슈 기반)

===== ⚡ 에너지 요약 =====
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

    # 핫이슈 섹션이 있으면 마지막에 별도 첨부
    hot_appendix = f"\n\n{'='*60}\n\n🔥 기사 없는 카테고리 - 최근 핫이슈 (자체 분석)\n\n{'='*60}\n\n{hot_issue_sections}" if hot_issue_sections else ""

    return (
        f"{category_summary}"
        f"\n\n{'='*60}\n\n📋 기사별 상세 분석\n\n{'='*60}\n\n{per_article_analysis}"
        f"{hot_appendix}"
    )


CATEGORY_LABEL = {
    "semiconductor": "💾 반도체",
    "energy": "⚡ 에너지",
    "macro": "📊 거시경제",
    "global_event": "🌏 글로벌 이벤트",
}


def build_html(header: str, summary: str, articles: List[Article]) -> str:
    # 카테고리별 기사 링크
    sections = ""
    for cat, label in CATEGORY_LABEL.items():
        cat_articles = [a for a in articles if a.category == cat]
        if not cat_articles:
            continue
        items = "\n".join(
            f'<li><a href="{a.url}" target="_blank">{ihtml.escape(a.title)}</a> '
            f'<span class="source">({a.source})</span></li>'
            for a in cat_articles
        )
        sections += f"<h2>{label}</h2><ul>{items}</ul>\n"

    # summary에서 마크다운 볼드(**text**) → <strong>
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


def kakao_send_to_me(access_token: str, text: str, page_url: str) -> None:
    template_object = {
        "object_type": "text",
        "text": text,
        "link": {"web_url": page_url, "mobile_web_url": page_url},
        "buttons": [{
            "title": "뉴스 요약 크게보기",
            "link": {"web_url": page_url, "mobile_web_url": page_url},
        }],
    }
    resp = requests.post(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        headers={"Authorization": f"Bearer {access_token}"},
        data={"template_object": json.dumps(template_object, ensure_ascii=False)},
    )
    print(f"[Kakao] status={resp.status_code}, body={resp.text}")
    if resp.status_code != 200:
        raise RuntimeError(f"카카오 전송 실패: {resp.status_code} {resp.text}")


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

    # 카톡 메시지 (핵심 요약 앞 200자만)
    short_summary = summary[:200].replace("\n", " ")
    kakao_text = f"{header}\n\n{short_summary}...\n\n자세한 내용은 아래 버튼을 눌러주세요."
    kakao_send_to_me(access_token, kakao_text, page_url)


if __name__ == "__main__":
    main()
