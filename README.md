# Simplenote → Naver Blog drafts

Simplenote 작업 노트를 네이버 블로그의 **임시저장 초안**으로 옮기는 macOS용 자동화 도구입니다. Threads 게시물 가져오기도 보조 기능으로 지원하며, Simplenote는 Automattic의 공식 `simplenote-mcp`를 사용합니다.

- LLM을 사용하지 않습니다.
- 네이버 글을 자동 발행하지 않습니다.
- 게시물 1개를 네이버 초안 1개로 저장합니다.
- 답글과 리포스트는 기본적으로 제외합니다.
- Simplenote에서는 설정한 태그 중 하나 이상이 붙은 노트만 큐로 읽습니다. `simplenote_tags`를 권장하며 기존 `simplenote_tag` 단일 설정도 계속 지원합니다.
- Simplenote의 첫 줄은 제목, 나머지 줄은 본문으로 사용합니다.
- 이미지, 캐러셀, 동영상을 함께 옮깁니다.
- 선택적으로 각 새 글의 생성형 표지를 본문 맨 앞에 넣고 대표 이미지로 확인합니다.
- 설정한 footer 링크와 이미지를 모든 새 초안의 맨 아래에 추가할 수 있습니다.
- SQLite로 처리 이력을 관리해 중단 후 재개해도 중복 초안을 만들지 않습니다.

> 네이버는 현재 공식 블로그 글쓰기 API를 제공하지 않으므로 Playwright로 SmartEditor 화면을 조작합니다. 네이버 UI가 바뀌면 선택자 보정이 필요할 수 있습니다.

## 안전장치

- Threads 원문과 Simplenote 본문을 LLM으로 다시 쓰지 않습니다.
- Simplenote 원본은 읽기 전용이며 노트의 태그나 내용을 수정하지 않습니다.
- 제목은 첫 번째 비어 있지 않은 줄의 앞 100자를 기계적으로 사용합니다. 첫 줄이 `직접 쓰는 AI교양`이면 원문 작성일을 붙여 `직접 쓰는 AI교양 – YYYY.MM.DD`로 저장합니다.
- 상단의 `저장` 또는 `임시저장`으로 확인된 컨트롤만 클릭합니다.
- `발행`이 포함된 컨트롤은 저장 버튼으로 인정하지 않습니다.
- 임시저장 수가 98개 이상이면 기존 초안 보호를 위해 새 초안을 만들지 않습니다.
- 성공한 게시물만 SQLite에 기록합니다. 실패한 게시물은 다음 실행에서 다시 시도합니다.
- Threads 토큰은 설정 파일이 아니라 macOS Keychain에 저장합니다.
- 네이버 비밀번호를 받거나 저장하지 않고 전용 Chromium 프로필의 로그인 세션만 사용합니다.

## 요구사항

- macOS
- Python 3.11 이상
- Node.js 22 이상
- Simplenote macOS 앱의 로컬 저장소 또는 공식 MCP API 로그인
- [uv](https://docs.astral.sh/uv/)
- Threads를 소스로 사용할 경우 자신의 게시물을 읽을 수 있는 Threads API 사용자 액세스 토큰
- 네이버 블로그 계정

## 설치

```bash
git clone https://github.com/indra622/simplenote-to-naver-drafts.git
cd simplenote-to-naver-drafts

cp config.example.toml config.toml
uv sync --extra dev
uv run playwright install chromium
npm install
```

`config.toml`은 Git에서 제외됩니다. 블로그가 하나라면 기본값을 그대로 사용해도 됩니다.

```toml
naver_blog_id = ""
timezone = "Asia/Seoul"
source = "simplenote"
headless = false
include_replies = false
include_reposts = false
max_posts_per_run = 20
simplenote_tags = ["naver"]
simplenote_start_date = ""
simplenote_provider = "local"
simplenote_store_path = "~/Library/Group Containers/PZYM8XX95Q.com.automattic.SimplenoteMac/Data/Simplenote.storedata"
simplenote_mcp_command = "node_modules/.bin/simplenote-mcp"
simplenote_scan_limit = 100
simplenote_max_notes_per_run = 2
require_generated_cover = false
naver_write_url = "https://blog.naver.com/{blog_id}/postwrite"
footer_url = "https://naver.me/5qLhk2hv"
footer_image_path = "assets/brand-connect-guide.png"
```

블로그가 여러 개라면 `naver_blog_id`와 해당 블로그의 글쓰기 URL을 설정하세요.
footer를 바꾸려면 `footer_url`과 `footer_image_path`를 수정하세요. 두 값을 모두 비우면 footer를 추가하지 않습니다.

일배치 소스를 Simplenote로 바꾸려면 `source = "simplenote"`로 설정하세요. `simplenote_max_notes_per_run = 2`는 한 번에 공개하는 글 수가 아니라, 하루에 만들어 둘 **임시저장 초안 수**의 상한입니다.
과거 노트를 제외하려면 `simplenote_start_date = "2026-09-21"`처럼 기준일을 설정하세요. 설정한 시간대의 해당 날짜 00:00 이후 생성된 노트만 처리하며, 이전 노트는 나중에 태그를 붙여도 제외합니다.

### Simplenote 공급자 선택

- `simplenote_provider = "local"`: macOS 앱의 로컬 저장소를 오프라인·읽기 전용으로 사용합니다. 실행 주체에 macOS의 다른 앱 데이터 접근 권한이 필요할 수 있습니다.
- `simplenote_provider = "api"`: 공식 MCP의 Simperium API 공급자를 사용합니다. 데스크톱 앱 동기화 상태와 무관해 LaunchAgent 일배치에는 이 방식이 더 안정적입니다.

API 모드는 공식 MCP 설정을 한 번 실행합니다. 이메일과 인증 코드는 터미널에만 입력하며 이 프로젝트나 채팅으로 전달되지 않습니다.

```bash
npm exec -- simplenote-mcp setup
```

## 로컬 인증 설정

이 저장소에는 토큰, 쿠키, 비밀번호가 포함되어 있지 않습니다.

Threads 액세스 토큰은 터미널의 가려진 입력창을 통해 macOS Keychain에 저장합니다.

```bash
uv run threads-to-naver setup-token
```

입력 중 문자가 보이지 않는 것이 정상입니다. 장기 토큰은 저장 후 24시간이 지난 일배치 실행에서 공식 갱신 API로 하루 한 번 이하 갱신됩니다.

네이버 로그인 세션은 전용 Chromium 프로필에 저장합니다.

```bash
uv run threads-to-naver login
```

열린 브라우저에서 직접 로그인하세요. 비밀번호는 이 프로그램에 전달되지 않습니다.

## 사용법

### Simplenote 큐 dry-run

Simplenote에서 네이버로 보낼 노트에 `naver` 태그를 붙인 뒤 실행합니다. 처리 완료 여부는 로컬 SQLite에 기록되므로 태그가 계속 남아 있어도 중복 초안을 만들지 않습니다.

```bash
uv run threads-to-naver simplenote-run --dry-run
uv run threads-to-naver simplenote-run --limit 2
```

`--dry-run`은 네이버 브라우저를 열지 않습니다. 노트 생성일을 기준으로 오래된 미처리 항목부터 가져옵니다. 첫 줄이 `직접 쓰는 AI교양`이면 `직접 쓰는 AI교양 – YYYY.MM.DD` 제목 규칙도 동일하게 적용됩니다.

### 설정된 일배치 소스 실행

```bash
uv run threads-to-naver daily --dry-run
uv run threads-to-naver daily
```

`source = "simplenote"`이면 Simplenote 태그 큐를, `source = "threads"`이면 전날 Threads 게시물을 처리합니다.

### 생성형 표지 준비

`require_generated_cover = true`로 설정하면 새 초안마다 생성된 표지가 필요합니다. 이 옵션은 모든 새 소스 항목에 적용됩니다. `daily`와 `daily --dry-run`은 현재 소스의 정확한 제목·본문·작성 시각에 맞는 캐시 파일을 먼저 확인합니다. 표지가 없거나 손상되면 네이버를 열거나 완료 상태를 기록하지 않고 실패합니다. 원본 노트가 수정되면 이전 표지는 재사용하지 않습니다.

```bash
.venv/bin/threads-to-naver cover-queue --output artifacts/cover-queue.json
```

이 명령은 이번 일배치 대상 중 표지가 필요한 항목을 `items` 배열로 출력 파일에 기록합니다. 각 항목에 `id`, `fingerprint`, `title`, `text`가 들어 있습니다. 파일 권한은 `0600`이며 원문을 포함하므로 공유하거나 로그에 출력하지 마세요. OpenClaw 에이전트가 이 파일을 비공개로 읽고, 설정된 `openai/gpt-image-2`의 `image_generate` 도구로 **항목마다 새 이미지를 생성**해야 합니다. 이 로컬 Python/launchd 프로세스는 해당 동적 도구를 직접 호출하지 않습니다. 권장 표지: 정사각형 프리미엄 에디토리얼 이미지, 따뜻한 아이보리 종이, 짙은 잉크 블루 그림자, 절제된 코랄·틸, 촉감 있는 3D와 인쇄의 혼합, 명확한 중심 피사체, 텍스트·로고·워터마크 없음.

검토한 PNG 또는 JPEG 이미지를 항목별로 설치합니다.

```bash
.venv/bin/threads-to-naver cover-install \
  --queue artifacts/cover-queue.json \
  --item-id 'simplenote:NOTE_ID_FROM_QUEUE' \
  --image /private/path/to/generated-cover.jpg
.venv/bin/threads-to-naver daily --dry-run
.venv/bin/threads-to-naver daily
```

이미지는 `~/.local/share/threads-to-naver/covers/<SHA256(item-id)>/<fingerprint>.jpg` 또는 `.png`에 복사되고, 같은 이름의 `.json` 파일에 이미지 체크섬이 저장됩니다. `daily`는 원본과 체크섬을 재검증한 뒤 표지를 본문과 기존 미디어·footer보다 먼저 업로드합니다. 네이버의 이미지별 `대표` 버튼이 선택된 상태이며 표지가 첫 이미지인지 저장 전후에 확인합니다. 생성 실패 시 큐 항목은 다음 실행에서도 남습니다. 같은 항목을 재시도할 때는 검증된 캐시를 재사용하므로 중복 생성하지 않습니다.

운영 환경에서는 OpenClaw 자동화 `simplenote-naver-drafts-ai-cover-11am`이 매일 11:00(Asia/Seoul)에 `cover-queue` → `image_generate` → `cover-install` → `daily --dry-run` → `daily`를 순서대로 실행합니다. 기존 launchd `local.threads-to-naver-drafts`는 중복 작성을 막기 위해 비활성화했습니다. 자동화 ID와 프롬프트는 OpenClaw 설정에서 관리하며 Git 저장소에는 포함하지 않습니다. 이미지 생성이 지연되면 같은 자동화 세션의 완료 이벤트에서 이어서 설치합니다. 원문과 생성 프롬프트를 예약 실행 로그에 남기지 마세요.

### 1. 먼저 dry-run

기본 실행 대상은 전날 게시물입니다. `--dry-run`은 Threads 데이터만 읽어 `artifacts/`에 JSON을 만들고 네이버 브라우저는 열지 않습니다.

```bash
uv run threads-to-naver run --dry-run
```

특정 날짜를 확인하려면:

```bash
uv run threads-to-naver run --date 2026-09-19 --dry-run
```

가장 최근 게시물 한 건만 확인하려면:

```bash
uv run threads-to-naver run --latest --dry-run
```

### 2. 네이버 초안 생성

```bash
# 전날 게시물
uv run threads-to-naver run

# 특정 날짜
uv run threads-to-naver run --date 2026-09-19

# 가장 최근 게시물 한 건
uv run threads-to-naver run --latest
```

### 3. 과거 게시물 백필

전체 과거 원 게시물을 오래된 순서대로 처리합니다.

```bash
uv run threads-to-naver backfill --dry-run
uv run threads-to-naver backfill
```

여러 묶음으로 나누려면 `--limit`을 사용합니다. 성공한 건은 즉시 SQLite에 기록되므로 같은 명령을 다시 실행하면 다음 미처리 게시물부터 재개합니다.

```bash
uv run threads-to-naver backfill --limit 50
```

### 4. 기존 임시저장 글에 footer 추가

현재 네이버 임시저장 글의 맨 아래에 설정된 링크와 이미지를 추가합니다. 네이버 초안 고유 ID와 footer 서명을 SQLite에 기록하므로 중단 후 재개해도 중복 추가하지 않습니다.

```bash
uv run threads-to-naver append-footer --dry-run
uv run threads-to-naver append-footer --limit 10
uv run threads-to-naver append-footer
```

링크와 이미지를 바꿀 때는 `config.toml`의 `footer_url`과 `footer_image_path`를 변경하세요. 이후 새로 생성되는 초안에는 변경된 footer가 적용됩니다.

### 5. 기존 고정 시리즈 제목에 원문 날짜 추가

제목이 정확히 `직접 쓰는 AI교양`인 임시저장 글을 Threads 원문 본문과 대조한 뒤, 원문 작성일을 붙입니다. 본문이 여러 원문과 일치하거나 일치하는 원문이 없으면 저장 전에 중단합니다.

```bash
uv run threads-to-naver retitle-series --dry-run
uv run threads-to-naver retitle-series --limit 10
uv run threads-to-naver retitle-series
```

성공한 글은 제목이 더 이상 고정 제목과 일치하지 않으므로 같은 명령을 다시 실행해도 재처리되지 않습니다.

## 매일 오전 11시 실행

프로젝트에 포함된 설치 스크립트로 macOS LaunchAgent를 생성합니다.

```bash
uv run python scripts/install_launchd.py --hour 11 --minute 0
launchctl bootstrap gui/$(id -u) \
  ~/Library/LaunchAgents/local.threads-to-naver-drafts.plist
```

이미 등록된 스케줄을 변경할 때는 기존 작업을 내린 뒤 다시 등록합니다.

```bash
launchctl bootout gui/$(id -u)/local.threads-to-naver-drafts
uv run python scripts/install_launchd.py --hour 11 --minute 0
launchctl bootstrap gui/$(id -u) \
  ~/Library/LaunchAgents/local.threads-to-naver-drafts.plist
```

실행 로그:

```text
~/Library/Logs/threads-to-naver/stdout.log
~/Library/Logs/threads-to-naver/stderr.log
```

## 로컬 데이터 위치

다음 데이터는 Git에 포함되지 않습니다.

- `config.toml`: 개인 설정
- `artifacts/`: dry-run JSON, 저장 화면 캡처, 다운로드한 미디어
- `~/.local/share/threads-to-naver/state.sqlite3`: Threads와 Simplenote의 중복 방지 기록
- `~/.local/share/threads-to-naver/browser-profile/`: 네이버 로그인 브라우저 프로필
- macOS Keychain의 `threads-to-naver-drafts` 항목: Threads 토큰
- `~/Library/Application Support/simplenote-mcp/`: API 모드 선택 시 공식 MCP 설정과 인증 파일

## 개발 및 검증

```bash
uv run pytest
uvx ruff check .
uv run python -m compileall -q src tests scripts
```

## 현재 한계

- 네이버 SmartEditor UI 변경 시 자동화가 실패할 수 있습니다.
- 로그인 만료, CAPTCHA, 네트워크 장애가 발생하면 수동 확인이 필요합니다.
- 임시저장 수가 98개에 도달하면 새 초안 생성을 중단합니다. 기존 초안을 발행하거나 삭제한 뒤 다시 실행하세요.
- 공개 발행은 의도적으로 구현하지 않았습니다.
- Simplenote 첨부파일은 공식 MCP 읽기 결과에 포함되지 않아 현재는 텍스트와 본문 URL만 옮깁니다.

개인 계정과 콘텐츠에 적용하기 전에 반드시 `--dry-run`과 최신 게시물 한 건으로 먼저 확인하세요.
