import html as ihtml
import json
import os
import re
import textwrap
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from urllib.parse import urlparse

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


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
KST = timezone(timedelta(hours=9))

RSS_URLS = [
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://rss.cnn.com/rss/edition.rss",
    "https://www.yonhapnewstv.co.kr/browse/feed/",
]


def _env(name: str, default: Optional[str] = None) -> str:
    val = os.getenv(name, default)
    if val is None or val.strip() == "":
        raise RuntimeError(f"Missing: {name}")
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


def fetch_rss_entries(rss_urls: List[str]) -> List[dict]:
    entries = []
    for url in rss_urls:
        feed = feedparser.parse(url)
        if not getattr(feed, "bozo", 0):
            entries.extend(getattr(feed, "entries", []))
    seen, uniq = set(), []
    for e in entries:
        link = (e.get("link") or "").strip()
        if link and link not in seen:
            seen.add(link)
            uniq.append(e)
    return uniq


def fetch_naver_news_entries(queries: List[str]) -> List[dict]:
    client_id = _env_optional("NAVER_CLIENT_ID")
    client_secret = _env_optional("NAVER_CLIENT_SECRET")
    if not client_id or not client_secret:
        return []
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }
    out = []
    for q in queries:
        try:
            r = requests.get(
                "https://openapi.naver.com/v1/search/news.json",
                headers=headers,
                params={"query": q, "display": 10, "sort": "date"},
                timeout=15,
            )
            for item in r.json().get("items", []):
                out.append({
                    "title": item.get("title"),
                    "link": item.get("originallink") or item.get("link"),
                    "published": item.get("pubDate"),
                    "_provider": "naver",
                })
        except Exception as e:
            print(f"[Naver] 쿼리 실패 ({q}): {e}")
            continue
    return out


def extract_article_text(url: str) -> str:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
        doc = Document(r.text)
        soup = BeautifulSoup(doc.summary(), "html.parser")
        return soup.get_text("\n", strip=True)[:6000]
    except Exception as e:
        print(f"[extract] 본문 추출 실패 ({url}): {e}")
        return ""


def collect_articles(max_articles: int) -> List[Article]:
    entries = fetch_rss_entries(RSS_URLS)
    entries += fetch_naver_news_entries(["오늘 뉴스", "주요 뉴스"])
    articles = []
    for idx, e in enumerate(entries[:max_articles]):
        url = (e.get("link") or "").strip()
        title = re.sub(r"<[^>]+>", "", e.get("title") or "").strip()
        source = urlparse(url).netloc
        published = e.get("published", "")
        text = extract_article_text(url)
        if title and url:
            articles.append(Article(idx=idx, title=title, url=url, source=source, published=published, text=text))
    return articles


def gemini_summarize(prompt: str) -> str:
    from google import genai
    client = genai.Client(api_key=_env("GEMINI_API_KEY"))
    resp = client.models.generate_content(
        model=_env_str("GEMINI_MODEL", "gemini-2.0-flash"),
        contents=prompt,
    )
    return (getattr(resp, "text", "")).strip()


def build_summary(articles: List[Article]) -> str:
    if not articles:
        return "오늘 수집된 뉴스가 없습니다."

    article_texts = ""
    for a in articles:
        article_texts += f"\n[{a.idx+1}] {a.title}\n출처: {a.source}\n{a.text[:1000]}\n"

    prompt = f"""다음 뉴스 기사들을 한국어로 간결하게 요약해주세요.
각 기사별로 핵심 내용을 2-3줄로 요약하고, 전체적인 오늘의 주요 뉴스 흐름도 마지막에 정리해주세요.

{article_texts}
"""
    return gemini_summarize(prompt)


def build_html(header: str, summary: str, articles: List[Article]) -> str:
    article_links = ""
    for a in articles:
        article_links += f'<li><a href="{a.url}" target="_blank">{ihtml.escape(a.title)}</a> <span class="source">({a.source})</span></li>\n'

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{ihtml.escape(header)}</title>
<style>
  body {{ font-family: sans-serif; max-width: 800px; margin: 40px auto; padding: 0 20px; line-height: 1.7; }}
  h1 {{ font-size: 1.4em; color: #333; }}
  pre {{ white-space: pre-wrap; background: #f9f9f9; padding: 20px; border-radius: 8px; }}
  ul {{ padding-left: 20px; }}
  .source {{ color: #888; font-size: 0.85em; }}
</style>
</head>
<body>
<h1>{ihtml.escape(header)}</h1>
<h2>📰 뉴스 요약</h2>
<pre>{ihtml.escape(summary)}</pre>
<h2>🔗 원문 기사 목록</h2>
<ul>
{article_links}
</ul>
</body>
</html>"""


def kakao_send_to_me(access_token: str, text: str, page_url: str) -> None:
    template_object = {
        "object_type": "text",
        "text": text,
        "link": {
            "web_url": page_url,
            "mobile_web_url": page_url,
        },
        "buttons": [
            {
                "title": "뉴스 요약 크게보기",
                "link": {
                    "web_url": page_url,
                    "mobile_web_url": page_url,
                },
            }
        ],
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

    max_articles = _env_int("MAX_ARTICLES", 10)
    header = f"[뉴스 브리핑] {datetime.now(KST).strftime('%Y-%m-%d')}"

    print(f"[수집 시작] 최대 {max_articles}개 기사")
    articles = collect_articles(max_articles)
    print(f"[수집 완료] {len(articles)}개 기사")

    summary = build_summary(articles)
    print(f"[요약 완료]")

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

    kakao_send_to_me(access_token, f"{header}\n오늘 뉴스 {len(articles)}건이 도착했습니다!", page_url)


if __name__ == "__main__":
    main()
