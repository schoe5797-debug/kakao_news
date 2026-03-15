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


def _env(name: str, default: Optional[str] = None) -> str:
    val = os.getenv(name, default)
    if val is None or val.strip() == "":
        raise RuntimeError(f"Missing env: {name}")
    return val


def _env_optional(name: str) -> Optional[str]:
    val = os.getenv(name)
    return val.strip() if val and val.strip() else None


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw.strip()) if raw and raw.strip() else default


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    return raw.strip() if raw and raw.strip() else default


def fetch_google_rss(query: str) -> List[dict]:
    """구글 뉴스 RSS로 해외 기사 수집"""
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=en&gl=US&ceid=US:en"
    try:
        feed = feedparser.parse(url)
        return getattr(feed, "entries", [])
    except Exception as e:
        print(f"[Google RSS] 실패 ({query}): {e}")
        return []


def fetch_naver_news(query: str) -> List[dict]:
    """네이버 뉴스 API로 국내 기사 수집"""
    client_id = _env_optional("NAVER_CLIENT_ID")
    client_secret = _env_optional("NAVER_CLIENT_SECRET")
    if not client_id or not client_secret:
        return []
    try:
        r = requests.get(
            "https://openapi.naver.com/v1/search/news.json",
            headers={
                "X-Naver-Client-Id": client_id,
                "X-Naver-Client-Secret": client_secret,
            },
            params={"query": query, "display": 5, "sort": "date"},
            timeout=15,
        )
        return r.json().get("items", [])
    except Exception as e:
        print(f"[Naver] 실패 ({query}): {e}")
        return []


def fetch_newspaper_rss(rss_url: str) -> List[dict]:
    """신문사 RSS 수집"""
    try:
        feed = feedparser.parse(rss_url)
        return getattr(feed, "entries", []) if not getattr(feed, "bozo", 0) else []
    except Exception as e:
        print(f"[RSS] 실패 ({rss_url}): {e}")
        return []


def extract_article_text(url: str) -> str:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
        doc = Document(r.text)
        soup = BeautifulSoup(doc.summary(), "html.parser")
        return soup.get_text("\n", strip=True)[:5000]
    except Exception as e:
        print(f"[extract] 실패 ({url}): {e}")
        return ""


def collect_articles(max_articles: int) -> List[Article]:
    raw: List[tuple] = []  # (title, url, category, source)
    seen_urls: set = set()

    # 1) 구글 RSS (해외)
    for query, category in GOOGLE_RSS_QUERIES:
        for e in fetch_google_rss(query)[:3]:
            url = (e.get("link") or "").strip()
            title = re.sub(r"<[^>]+>", "", e.get("title") or "").strip()
            if url and url not in seen_urls:
                seen_urls.add(url)
                raw.append((title, url, category, urlparse(url).netloc, e.get("published", "")))

    # 2) 네이버 뉴스 API
    for query, category in NAVER_QUERIES:
        for item in fetch_naver_news(query):
            url = (item.get("originallink") or item.get("link") or "").strip()
            title = re.sub(r"<[^>]+>", "", item.get("title") or "").strip()
            if url and url not in seen_urls:
                seen_urls.add(url)
                raw.append((title, url, category, urlparse(url).netloc, item.get("pubDate", "")))

    # 3) 신문사 RSS
    for rss_url, category in NAVER_NEWSPAPER_RSS:
        for e in fetch_newspaper_rss(rss_url)[:3]:
            url = (e.get("link") or "").strip()
            title = re.sub(r"<[^>]+>", "", e.get("title") or "").strip()
            if url and url not in seen_urls:
                seen_urls.add(url)
                raw.append((title, url, category, urlparse(url).netloc, e.get("published", "")))

    # 본문 추출 후 Article 생성
    articles = []
    for idx, (title, url, category, source, published) in enumerate(raw[:max_articles]):
        text = extract_article_text(url)
        if title:
            articles.append(Article(
                idx=idx, title=title, url=url,
                source=source, published=published,
                text=text, category=category,
            ))
    return articles


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
        return "오늘 수집된 뉴스가 없습니다."

    def fmt(cat: str) -> str:
        return "\n".join(
            f"- [{a.source}] {a.title}\n  {a.text[:800]}"
            for a in articles if a.category == cat
        ) or "해당 없음"

    prompt = f"""오늘은 {today}입니다. 아래는 카테고리별로 수집된 뉴스입니다.

투자자 관점에서 다음 조건에 맞게 한국어로 분석해주세요:
1. 보유 종목: 삼성전자, 와이씨, 두산에너빌리티, 엔비디아, CEG(Constellation Energy)
2. 영어 기사는 한국어로 번역하여 요약
3. 비슷한 내용의 기사는 묶어서 정리
4. 각 카테고리별 핵심 내용 3줄 이내 요약
5. 보유 종목에 미칠 영향 분석 (긍정/부정/중립)
6. 오늘의 투자 인사이트 (전체 종합)
7. 글로벌 이벤트(전쟁, 감염병 등)가 있다면 보유 종목과의 연관성 분석

===== 반도체 =====
{fmt("semiconductor")}

===== 에너지 =====
{fmt("energy")}

===== 거시경제 =====
{fmt("macro")}

===== 글로벌 이벤트 =====
{fmt("global_event")}
"""
    return gemini_summarize(prompt)


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
