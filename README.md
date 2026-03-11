# 카카오톡 뉴스 요약 봇 (매일 08:00 KST, 0원)

구글 뉴스 RSS(해외) + 네이버 뉴스 오픈API(국내)에서 기사를 모으고, **Gemini API로 “근거 기반 요약”**을 만든 뒤, 카카오톡 **“나에게 보내기”**로 전송합니다. 실행은 GitHub Actions가 매일 자동으로 돌려서 **PC를 꺼도** 됩니다.

## 핵심 안전장치(할루시네이션 방지)

- 요약 입력은 **수집한 기사 텍스트(본문 추출/스니펫)**만 사용합니다.
- 프롬프트에서 **“SOURCE에 없는 사실을 쓰지 말 것 / 불확실하면 정보 부족”**을 강제합니다.
- 출력의 **URL은 수집한 URL만 남기도록 필터링**합니다. (모델이 새 링크를 만들어도 제거)

## 1번부터 실행 순서 (초보자용)

### 1) GitHub 저장소 만들기

1. GitHub에서 새 Repository 생성
2. 이 프로젝트 파일(`app.py`, `requirements.txt`, `.github/workflows/main.yml`, `README.md`)을 저장소 루트에 업로드

### 2) Gemini API 키 발급 (무료 티어)

1. [Google AI for Developers](https://ai.google.dev/)에서 Gemini API Key 발급
2. GitHub 저장소 → **Settings → Secrets and variables → Actions → New repository secret**
3. 아래 Secrets 추가
   - `GEMINI_API_KEY`: 발급받은 키
   - (선택) `GEMINI_MODEL`: 예) `gemini-2.0-flash` (비우면 기본값 사용)
   - (선택) `MAX_SOURCE_CHARS`: 기사 1개당 모델 입력 텍스트 길이 제한(기본 1400)

### 2-1) (선택) 네이버 뉴스 오픈API 추가(국내 뉴스 강화)

네이버는 **국내 뉴스** 수집에 사용합니다. (해외는 구글 뉴스 RSS가 주도)

1. `https://developers.naver.com`에서 애플리케이션 생성(검색 API)
2. 발급된 값 2개를 GitHub Secrets에 추가
   - `NAVER_CLIENT_ID`
   - `NAVER_CLIENT_SECRET`

> 네이버 키를 넣지 않으면, 봇은 자동으로 “구글 뉴스(해외)만”으로도 동작합니다.

### 3) 카카오 “나에게 보내기” 토큰 준비(중요)

GitHub Actions는 매일 자동 실행이라, 매번 로그인할 수 없습니다. 그래서 **Refresh Token을 Secrets에 저장**하고, 실행할 때마다 **Access Token을 갱신**하는 방식을 씁니다.

#### 3-1. 카카오 디벨로퍼스 앱 만들기

1. [Kakao Developers](https://developers.kakao.com/) 로그인
2. **내 애플리케이션 → 애플리케이션 추가하기**
3. 앱 생성 후 **플랫폼 키 → REST API 키(기본 키)** 값 확인

#### 3-2. 카카오톡 메시지 권한 켜기

1. 내 애플리케이션 → **제품 설정 → 카카오 로그인**
2. **동의항목**에서 `talk_message`(카카오톡 메시지 전송) 권한이 필요합니다.
3. 내 애플리케이션 → **제품 설정 → 카카오 로그인 → Redirect URI**에 임시 Redirect URI 등록  
   예: `https://localhost/oauth`

#### 3-3. Refresh Token 1회 발급(브라우저 + curl)

아래 값은 본인 값으로 바꿉니다.

- `REST_API_KEY`: 카카오 REST API 키
- `REDIRECT_URI`: 방금 등록한 Redirect URI

1) 브라우저에서 아래 주소를 열어 **인가 코드(code)**를 얻습니다(로그인/동의 1회).

`https://kauth.kakao.com/oauth/authorize?client_id=REST_API_KEY&redirect_uri=REDIRECT_URI&response_type=code&scope=talk_message`

2) 주소창에 `...code=XXXX` 형태로 나온 **code 값을 복사**한 뒤 토큰 발급:

```bash
curl -X POST "https://kauth.kakao.com/oauth/token" ^
  -H "Content-Type: application/x-www-form-urlencoded;charset=utf-8" ^
  -d "grant_type=authorization_code" ^
  -d "client_id=REST_API_KEY" ^
  -d "redirect_uri=REDIRECT_URI" ^
  -d "code=인가코드"
```

응답 JSON에서 `refresh_token`을 복사합니다.

#### 3-4. GitHub Secrets 등록

GitHub 저장소 → Settings → Secrets and variables → Actions → New repository secret

- `KAKAO_REST_API_KEY`: 카카오 REST API 키
- `KAKAO_REFRESH_TOKEN`: 위 단계에서 발급된 refresh_token
- (선택) `MAX_ARTICLES`: 예) `10`
- (선택) `INTL_TARGET`: 해외(구글) 기사 비중(기본 6, 총합은 MAX_ARTICLES)

### 4) GitHub Actions 실행 확인

1. GitHub 저장소 → **Actions** 탭
2. `kakao-news-briefing` 워크플로우 선택
3. **Run workflow**로 수동 실행 테스트
4. 성공하면 매일 **08:00(KST)** 자동 실행됩니다.

## 로컬에서 실행(선택)

1. Python 3.11 설치
2. 의존성 설치

```bash
pip install -r requirements.txt
```

3. `.env` 파일 생성(로컬 전용, GitHub에는 올리지 마세요)

```text
GEMINI_API_KEY=...
KAKAO_REST_API_KEY=...
KAKAO_REFRESH_TOKEN=...
NAVER_CLIENT_ID=...           # 선택
NAVER_CLIENT_SECRET=...       # 선택
MAX_ARTICLES=10               # 선택
INTL_TARGET=6                 # 선택
```

4. 실행

```bash
python app.py
```

## 주의

- 기사 본문 추출은 사이트에 따라 실패할 수 있어요(로그인/유료벽 등). 실패 시 RSS 스니펫을 사용합니다.
- 무료 티어 한도/정책은 서비스 제공자 정책에 따라 바뀔 수 있습니다.
