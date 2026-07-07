# 🐷 ReelFork

인스타 릴스에서 **공유** 버튼만 누르면 우리 노션 맛집 지도에 자동 저장되는 서비스.

**파이프라인**: iOS 단축어 → Apify(인스타 스크래핑) → Gemini(식당명 추출) → Naver(주소 조회) → Notion(저장)

---

## 프로젝트 구조

```
├── api/save.py        # POST /api/save  — 메인 API 핸들러
├── index.html         # 단축어 설치 안내 페이지
├── vercel.json        # Vercel 라우팅 설정
├── pyproject.toml     # Python 의존성 (uv)
└── .env.example       # 환경 변수 예시
```

---

## 환경 변수

Vercel 대시보드 → **Settings → Environment Variables** 에 추가.

| 변수명 | 설명 |
|---|---|
| `NOTION_API_KEY` | Notion Integration 토큰 (`secret_...`) |
| `NOTION_DB_ID` | 맛집 DB ID (URL에서 추출) |
| `GEMINI_API_KEY` | Google AI Studio API 키 |
| `APIFY_API_TOKEN` | Apify 토큰 |
| `NAVER_CLIENT_ID` | 네이버 개발자 앱 Client ID |
| `NAVER_CLIENT_SECRET` | 네이버 개발자 앱 Client Secret |

→ 자세한 셋업은 [NOTION_SETUP.md](./NOTION_SETUP.md) 참고

---

## API

### `POST /api/save`

```json
{ "url": "https://www.instagram.com/reel/xxxxx/", "nickname": "홍길동" }
```

**성공 (200)**
```json
{
  "success": true,
  "message": "✅ 홍대 몽카페 저장 완료!",
  "restaurant_name": "몽카페",
  "address": "서울 마포구 와우산로 ...",
  "naver_link": "https://...",
  "registered_by": "홍길동",
  "elapsed_s": 8.3
}
```

**실패**
```json
{ "success": false, "message": "❌ 스크래핑 실패: ..." }
```

### `GET /api/save` — 헬스체크

```json
{ "status": "ok", "version": "6.0.0", "pipeline": "Apify → Gemini → Naver → Notion" }
```

---

## 배포

```bash
npx vercel --prod
```

---

## 보안

- 모든 API 키는 Vercel 환경 변수로만 관리
- 코드에 키 하드코딩 금지
