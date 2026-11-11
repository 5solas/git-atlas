# git-atlas 프로토타입 (Python)

superproject 하나와 그 안의 모든 서브모듈(중첩 포함)을 한 번에 검색하는 도구의 프로토타입이다.
명령과 출력 형식을 여기서 확정한 뒤 Rust로 옮긴다.

- 입력은 **원격 URL** 또는 **superproject 체크아웃**. `git submodule update --init`을 하지 않아도 된다.
- 작업 트리를 만들지 않는다. 각 저장소를 bare 캐시(`~/.cache/git-atlas`, Windows는 `%LOCALAPPDATA%\git-atlas`)로 받고,
  지정한 ref 시점에 **superproject가 고정한 서브모듈 커밋**을 기준으로 검색한다.
- 상대 서브모듈 URL(`../net.git`)은 git 규칙대로 부모 저장소 URL 기준으로 푼다.
- 체크아웃에 이미 초기화된 서브모듈(`.git/modules/<name>`)이 있으면 그것을 쓰고 받지 않는다.
- 필요: Python 3.10+, git. 외부 패키지 없음.

## 사용

```bash
python prototype/git_atlas.py -C https://example.com/org/super.git tree
```

`-C`가 없으면 현재 폴더를 superproject로 본다. 모든 명령은 `-r REF`(태그, 브랜치, 커밋, 기본 HEAD)를 받는다.

| 명령 | 하는 일 |
|---|---|
| `tree` | 저장소 트리: 경로, 고정 커밋, URL, 오류 |
| `sync` | 모든 저장소를 캐시에 받거나 갱신(fetch) |
| `grep PATTERN [PATHSPEC...]` | 파일 내용 검색(`git grep`). `-i -w -l -F -E -P -e` |
| `files PATTERN` | 파일 이름 검색. 기본 정규식, `-g`는 glob |
| `desc [PATTERN]` | 저장소 설명 검색: 이름, 경로, URL, `.gitmodules`의 branch 등, README 첫 문단 |
| `log PATTERN...` | 커밋 메시지 검색. ref 이전 이력 전체(서브모듈은 고정 커밋 이전 이력) |
| `jira KEY...` | Jira 키가 ref에 반영됐는지 판정. 하나라도 없으면 종료 1 |
| `keys` | 커밋 메시지의 Jira 키 모으기. `--project KB,OPS`, `-v` |

공통 옵션: `--only GLOB` / `--exclude GLOB`(저장소 경로), `--json`, `--fetch`, `--offline`, `-j N`, `--cache DIR`, `-q`.

### 이슈가 릴리스에 들어갔나

```bash
python prototype/git_atlas.py -C https://example.com/org/super.git jira KB-123 KB-130 -r v1.2
```

```
KB-123  포함  (v1.2, 커밋 1개, 저장소: libs/net)
    libs/net  769c56ea8a  2026-11-11  홍길동  KB-123 타임아웃 60초
KB-130  없음  (v1.2)
```

`v1.2` 태그 시점에 superproject가 가리키는 각 서브모듈 커밋까지의 이력을 본다.
서브모듈 저장소의 다른 브랜치에만 있는 커밋은 포함으로 치지 않는다.

### 두 릴리스 사이의 변경 (릴리스 노트)

```bash
python prototype/git_atlas.py keys --from v1.1 -r v1.2 --project KB -v
python prototype/git_atlas.py log "timeout" --from v1.1 -r v1.2
```

`--from`이 있으면 저장소마다 `v1.1 때 고정 커밋..v1.2 때 고정 커밋` 범위만 본다.
새로 생긴 서브모듈은 전체 이력, 포인터가 그대로인 서브모듈은 건너뛴다.

## 종료 코드

0 찾음, 1 못 찾음(`jira`는 하나라도 없음), 2 오류.
일부 서브모듈을 받지 못하면 stderr에 경고를 내고 나머지로 계속한다.

## 캐시와 최신성

- 처음 보는 저장소는 bare로 클론한다. 이후에는 고정 커밋이 캐시에 없을 때만 가져온다.
- 브랜치 최신 상태가 필요하면 `--fetch` 또는 `sync`.
- 인증 프롬프트는 끈다(`GIT_TERMINAL_PROMPT=0`). 비공개 저장소는 credential helper나 ssh-agent를 미리 설정한다.

## 테스트

```bash
python -m pytest -q prototype/tests
```

임시 폴더에 중첩 서브모듈, 상대 URL, 태그 두 개(v1, v2)가 있는 저장소를 만들어 검사한다.

## Rust 이전 시 메모

- git 호출을 그대로 유지할지(`git` 프로세스), `gix`로 바꿀지 결정. grep은 `grep`/`ignore` 크레이트 + 객체 읽기.
- 저장소 단위 병렬은 `rayon`. 캐시 잠금은 프로세스 간 잠금(`fd-lock`)으로.
- 색인(커밋 메시지·Jira 키를 SQLite에 저장)을 두면 `jira`/`keys` 반복 질의가 빨라진다.
