# Notion 셋업 가이드

## 1. 데이터베이스 만들기

노션에서 새 페이지 → **데이터베이스 (전체 페이지)** 생성 후 아래 속성 추가.

| 속성명 | 타입 |
|---|---|
| 식당 이름 | **제목** (기본값) |
| 주소 | 텍스트 |
| 인스타 링크 | URL |
| 등록자 | 셀렉트 |
| 지역 | 셀렉트 |
| 가게 인스타 | URL |

> **지도 뷰**: `+ 보기 추가` → `지도` → 위치 속성: `주소`

---

## 2. Integration 만들기 (API 키 발급)

1. [notion.so/my-integrations](https://www.notion.so/my-integrations) 접속
2. **+ 새 통합** 클릭 → 이름 입력 → 저장
3. **내부 통합 토큰** 복사 → `NOTION_API_KEY` 에 입력

---

## 3. DB에 Integration 연결

맛집 DB 페이지 우측 상단 `···` → **연결** → 방금 만든 Integration 선택

> ⚠️ 이 단계 빠지면 API가 DB에 접근 못해요

---

## 4. DB ID 확인

DB 페이지 URL에서 추출:

```
https://notion.so/myworkspace/396c2a04e6fb800c889aec25b13dc3ad?v=...
                               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                               이 부분이 NOTION_DB_ID
```

---

## 5. Vercel 환경 변수 등록

Vercel 프로젝트 → **Settings → Environment Variables**

```
NOTION_API_KEY     = secret_xxx...
NOTION_DB_ID       = 396c2a04...
GEMINI_API_KEY     = AIzaSy...
APIFY_API_TOKEN    = apify_api_...
NAVER_CLIENT_ID    = (네이버 개발자센터에서 발급)
NAVER_CLIENT_SECRET = (네이버 개발자센터에서 발급)
```

등록 후 **Redeploy** 하면 끝!
