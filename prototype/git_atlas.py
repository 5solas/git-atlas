#!/usr/bin/env python3
"""git-atlas 프로토타입: superproject와 모든 서브모듈(중첩 포함)을 한 번에 검색한다.

- 입력은 원격 URL 또는 superproject 체크아웃 경로. 서브모듈을 init/update 하지 않아도 된다.
- 작업 트리는 쓰지 않는다. 각 저장소를 bare 캐시로 받고, 지정한 ref 시점에 고정된(gitlink) 커밋을 검색한다.
- Python 표준 라이브러리와 git만 쓴다.

사용 예:
  git_atlas.py -C https://example.com/org/super.git tree
  git_atlas.py grep "TODO" -r v1.2
  git_atlas.py jira KB-123 KB-124 -r v1.2
  git_atlas.py keys --from v1.1 -r v1.2 --project KB
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

GIT = os.environ.get("GIT_ATLAS_GIT", "git")
GITLINK_MODE = "160000"
README_NAMES = ("readme.md", "readme", "readme.rst", "readme.txt", "readme.adoc")
# Jira 키 모양이지만 키가 아닌 것들 (--project를 주면 쓰지 않는다)
NOT_JIRA = {"UTF", "SHA", "ISO", "RFC", "MD", "AES", "CRC", "HTTP", "TLS", "SSL", "X", "U", "CP", "WIN", "GPL", "LGPL"}
KEY_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9_]{0,15})-([1-9][0-9]{0,6})(?![0-9])")


class AtlasError(Exception):
    pass


# ───────────────────────────────────────────────────────── git 실행

def git(args: list[str], git_dir: Path | str | None = None, cwd: Path | str | None = None,
        check: bool = True, ok_codes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    cmd = [GIT, "-c", "core.quotepath=off", "-c", "i18n.logOutputEncoding=utf-8"]
    if git_dir is not None:
        cmd += ["--git-dir", str(git_dir)]
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    proc = subprocess.run(cmd + args, cwd=cwd, capture_output=True, env=env)
    if check and proc.returncode not in ok_codes:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise AtlasError(f"git {' '.join(args[:3])} 실패 ({proc.returncode}): {err}")
    return proc


def out(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout.decode("utf-8", "replace")


def has_commit(git_dir: Path, sha: str) -> bool:
    return git(["cat-file", "-e", f"{sha}^{{commit}}"], git_dir, check=False).returncode == 0


# ───────────────────────────────────────────────────────── URL

def resolve_url(url: str, base: str) -> str:
    """git의 상대 서브모듈 URL 규칙(./, ../)으로 base 기준 URL을 만든다."""
    if not url.startswith(("./", "../")):
        return url
    remote = base.replace("\\", "/").rstrip("/")
    sep = "/"
    rest = url
    while True:
        if rest.startswith("./"):
            rest = rest[2:]
        elif rest.startswith("../"):
            rest = rest[3:]
            slash = remote.rfind("/")
            scheme = remote.find("://")
            if slash > (scheme + 2 if scheme >= 0 else -1):
                remote, sep = remote[:slash], "/"
            elif ":" in remote and scheme < 0 and remote.rfind(":") > 1:  # scp 형식 host:repo
                remote, sep = remote[: remote.rfind(":")], ":"
            else:
                raise AtlasError(f"상대 URL을 풀 수 없음: {url} (기준 {base})")
        else:
            break
    return f"{remote}{sep}{rest}" if rest else remote


def cache_key(url: str) -> str:
    norm = url.strip().rstrip("/")
    norm = norm[:-4] if norm.endswith(".git") else norm
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", re.split(r"[/:]", norm)[-1])[:40] or "repo"
    return f"{slug}-{hashlib.sha1(norm.encode()).hexdigest()[:10]}.git"


def default_cache() -> Path:
    if os.environ.get("GIT_ATLAS_CACHE"):
        return Path(os.environ["GIT_ATLAS_CACHE"])
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "git-atlas"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "git-atlas"


# ───────────────────────────────────────────────────────── 저장소 트리

@dataclass
class Repo:
    path: str                 # 최상위 기준 경로. 최상위는 ""
    name: str
    url: str | None
    commit: str | None
    depth: int = 0
    branch: str | None = None
    meta: dict = field(default_factory=dict)  # .gitmodules의 그 밖의 키
    git_dir: str | None = None
    local: bool = False       # 사용자 체크아웃의 git dir를 그대로 쓰는가
    error: str | None = None

    @property
    def label(self) -> str:
        return self.path or "."

    def file(self, rel: str) -> str:
        return f"{self.path}/{rel}" if self.path else rel


class Atlas:
    def __init__(self, target: str, cache: Path, jobs: int = 8, fetch: bool = False,
                 offline: bool = False, max_depth: int = 32, log=None):
        self.target = target
        self.cache = cache
        self.jobs = max(1, jobs)
        self.fetch = fetch
        self.offline = offline
        self.max_depth = max_depth
        self.log = log or (lambda msg: print(msg, file=sys.stderr))
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._fetched: set[str] = set()
        self.root_dir, self.root_url, self.root_local = self._open_root()

    # 최상위 열기 ───────────────────────────────

    def _open_root(self) -> tuple[Path, str, bool]:
        path = Path(self.target)
        bare = path.exists() and out(git(["rev-parse", "--is-bare-repository"], cwd=path, check=False)).strip() == "true"
        if path.exists() and not bare:  # 체크아웃. bare 저장소 경로는 URL처럼 캐시로 받는다
            git_dir = Path(out(git(["rev-parse", "--absolute-git-dir"], cwd=path)).strip())
            url = out(git(["config", "--get", "remote.origin.url"], git_dir, check=False)).strip()
            if self.fetch and url:
                self.log(f"가져오기: . ({url})")
                git(["fetch", "--quiet", "--tags", "origin"], git_dir, check=False)
            return git_dir, url or str(path.resolve()), True
        url = str(path.resolve()) if bare else self.target
        return self.ensure_clone(url), url, False

    def _lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def ensure_clone(self, url: str, want: str | None = None) -> Path:
        """url의 bare 캐시를 만들거나 갱신하고, want 커밋이 있도록 한다."""
        dest = self.cache / "repos" / cache_key(url)
        with self._lock(str(dest)):
            if not (dest / "HEAD").exists():
                if self.offline:
                    raise AtlasError(f"캐시에 없음(--offline): {url}")
                self.log(f"클론: {url}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_name(dest.name + f".tmp{os.getpid()}")
                git(["clone", "--bare", "--quiet", "--", url, str(tmp)])
                git(["config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*"], tmp)
                os.replace(tmp, dest)
                self._fetched.add(str(dest))
            elif self.fetch and str(dest) not in self._fetched and not self.offline:
                self.log(f"가져오기: {url}")
                git(["fetch", "--quiet", "--prune", "--tags", "origin"], dest)
                self._fetched.add(str(dest))
            if want and not has_commit(dest, want) and not self.offline:
                if str(dest) not in self._fetched:
                    git(["fetch", "--quiet", "--prune", "--tags", "origin"], dest, check=False)
                    self._fetched.add(str(dest))
                if not has_commit(dest, want):  # 브랜치에 없는 커밋: sha로 직접 요청
                    git(["fetch", "--quiet", "origin", want], dest, check=False)
            if want and not has_commit(dest, want):
                raise AtlasError(f"커밋 {want[:12]}을(를) 찾을 수 없음: {url}")
        return dest

    def resolve_ref(self, ref: str | None) -> str:
        ref = ref or "HEAD"
        for attempt in range(2):
            for cand in (ref, f"refs/tags/{ref}", f"origin/{ref}"):
                proc = git(["rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}"], self.root_dir, check=False)
                if proc.returncode == 0:
                    return out(proc).strip()
            if attempt == 0 and not self.offline:  # 모르는 ref면 한 번 가져와서 다시 본다
                git(["fetch", "--quiet", "--tags", "origin"], self.root_dir, check=False)
        raise AtlasError(f"ref를 찾을 수 없음: {ref}")

    # 트리 걷기 ────────────────────────────────

    def walk(self, ref: str | None = None) -> list[Repo]:
        root = Repo(path="", name=".", url=self.root_url, commit=self.resolve_ref(ref),
                    git_dir=str(self.root_dir), local=self.root_local)
        repos, level = [root], [root]
        with cf.ThreadPoolExecutor(self.jobs) as pool:
            while level:
                children: list[Repo] = []
                for parent in level:
                    if parent.depth < self.max_depth:
                        try:
                            children += self._children(parent)
                        except AtlasError as exc:
                            parent.error = str(exc)
                list(pool.map(self._attach, children))
                repos += children
                level = [c for c in children if c.git_dir and not c.error]
        return repos

    def _children(self, parent: Repo) -> list[Repo]:
        gd = Path(parent.git_dir)
        tree = out(git(["ls-tree", "-r", "-z", parent.commit], gd))
        links = {}
        for entry in tree.split("\0"):
            if entry.startswith(GITLINK_MODE + " "):
                meta, path = entry.split("\t", 1)
                links[path] = meta.split()[2]
        if not links:
            return []
        modules = self._gitmodules(gd, parent.commit)
        by_path = {m.get("path"): (name, m) for name, m in modules.items()}
        result = []
        for path, sha in sorted(links.items()):
            name, m = by_path.get(path, (path, {}))
            full = f"{parent.path}/{path}" if parent.path else path
            repo = Repo(path=full, name=name, url=None, commit=sha, depth=parent.depth + 1,
                        branch=m.get("branch"),
                        meta={k: v for k, v in m.items() if k not in ("path", "url", "branch")})
            raw_url = m.get("url")
            if parent.local:  # git submodule init이 기록한 URL이 있으면 그것이 우선
                override = out(git(["config", "--get", f"submodule.{name}.url"], gd, check=False)).strip()
                raw_url = override or raw_url
            if not raw_url:
                repo.error = ".gitmodules에 URL이 없음"
            else:
                try:
                    repo.url = resolve_url(raw_url, parent.url or "")
                except AtlasError as exc:
                    repo.error = str(exc)
            repo.meta["_parent_git_dir"] = str(gd) if parent.local else ""
            result.append(repo)
        return result

    @staticmethod
    def _gitmodules(git_dir: Path, commit: str) -> dict[str, dict]:
        proc = git(["config", "--blob", f"{commit}:.gitmodules", "--list", "-z"], git_dir, check=False)
        modules: dict[str, dict] = {}
        for item in out(proc).split("\0"):
            if not item or "\n" not in item:
                continue
            key, value = item.split("\n", 1)
            if not key.startswith("submodule."):
                continue
            name, _, sub = key[len("submodule."):].rpartition(".")
            modules.setdefault(name, {})[sub] = value
        return modules

    def _attach(self, repo: Repo) -> None:
        parent_gd = repo.meta.pop("_parent_git_dir", "")
        if repo.error:
            return
        # 1) 체크아웃에 이미 있는 서브모듈 git dir (.git/modules/<name>)
        if parent_gd:
            local = Path(parent_gd) / "modules" / repo.name
            if (local / "HEAD").exists() and has_commit(local, repo.commit):
                repo.git_dir, repo.local = str(local), True
                return
        # 2) 캐시
        try:
            repo.git_dir = str(self.ensure_clone(repo.url, repo.commit))
        except AtlasError as exc:
            repo.error = str(exc)


# ───────────────────────────────────────────────────────── 검색

def select(repos: list[Repo], only: list[str] | None, skip: list[str] | None) -> list[Repo]:
    def hit(repo: Repo, globs: list[str]) -> bool:
        return any(fnmatch.fnmatch(repo.label, g) or fnmatch.fnmatch(repo.label, g.rstrip("/") + "/*") for g in globs)
    chosen = [r for r in repos if r.git_dir and not r.error]
    if only:
        chosen = [r for r in chosen if hit(r, only)]
    if skip:
        chosen = [r for r in chosen if not hit(r, skip)]
    return chosen


def parallel(fn, repos: list[Repo], jobs: int) -> list[tuple[Repo, list]]:
    with cf.ThreadPoolExecutor(max(1, jobs)) as pool:
        return list(zip(repos, pool.map(fn, repos)))


def grep_repo(repo: Repo, patterns: list[str], opts, pathspec: list[str]) -> list[dict]:
    args = ["grep", "-n", "-I", "-z", "--full-name"]
    args += ["-i"] if opts.ignore_case else []
    args += ["-w"] if opts.word else []
    args += {"fixed": ["-F"], "extended": ["-E"], "perl": ["-P"]}.get(opts.mode, [])
    for p in patterns:
        args += ["-e", p]
    args += [repo.commit, "--"] + pathspec
    proc = git(args, repo.git_dir, ok_codes=(0, 1))
    hits = []
    prefix = repo.commit + ":"
    for line in out(proc).splitlines():
        parts = line.split("\0", 2)
        if len(parts) != 3:
            continue
        path = parts[0][len(prefix):] if parts[0].startswith(prefix) else parts[0]
        hits.append({"repo": repo.label, "path": repo.file(path), "line": int(parts[1]), "text": parts[2]})
    return hits


def files_repo(repo: Repo, pattern: str, opts) -> list[dict]:
    tree = out(git(["ls-tree", "-r", "-z", repo.commit], repo.git_dir))
    flags = re.IGNORECASE if opts.ignore_case else 0
    rx = None if opts.glob else re.compile(pattern, flags)
    hits = []
    for entry in tree.split("\0"):
        if "\t" not in entry:
            continue
        meta, path = entry.split("\t", 1)
        if meta.split()[1] != "blob":
            continue
        if opts.glob:
            name = path.rsplit("/", 1)[-1]
            cmp = (lambda a, b: fnmatch.fnmatch(a.lower(), b.lower())) if opts.ignore_case else fnmatch.fnmatchcase
            ok = cmp(path, pattern) or cmp(name, pattern)
        else:
            ok = bool(rx.search(path))
        if ok:
            hits.append({"repo": repo.label, "path": repo.file(path)})
    return hits


LOG_FMT = "%H%x00%an%x00%aI%x00%B%x1e"


def log_repo(repo: Repo, rng: str, greps: list[str], opts, regexp: str = "basic") -> list[dict]:
    args = ["log", f"--format={LOG_FMT}"]
    args += ["-E"] if regexp == "extended" else ["-F"] if regexp == "fixed" else []
    args += ["-i"] if getattr(opts, "ignore_case", False) else []
    args += ["--all-match"] if getattr(opts, "all_match", False) else []
    args += ["--no-merges"] if getattr(opts, "no_merges", False) else []
    args += [f"--grep={g}" for g in greps]
    args += [rng, "--"]
    proc = git(args, repo.git_dir)
    commits = []
    for rec in out(proc).split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, author, date, body = rec.split("\0", 3)
        commits.append({"repo": repo.label, "commit": sha, "author": author, "date": date,
                        "subject": body.split("\n", 1)[0], "message": body.strip()})
    return commits


def readme_summary(repo: Repo) -> str:
    names = out(git(["ls-tree", "-z", "--name-only", repo.commit], repo.git_dir)).split("\0")
    for want in README_NAMES:
        for name in names:
            if name.lower() == want:
                text = out(git(["cat-file", "blob", f"{repo.commit}:{name}"], repo.git_dir, check=False))
                paras, cur = [], []
                for line in text.splitlines():
                    s = line.strip()
                    if not s or s.startswith(("[![", "<", "```", "---", "===")):
                        if cur:
                            paras.append(" ".join(cur))
                            cur = []
                        continue
                    cur.append(s.lstrip("#= ").strip())
                if cur:
                    paras.append(" ".join(cur))
                # 첫 문단이 제목뿐이면 다음 문단까지
                joined = " / ".join(paras[:2]) if paras and len(paras[0]) < 60 else (paras[0] if paras else "")
                return joined[:300]
    return ""


def describe(repo: Repo) -> dict:
    info = {"repo": repo.label, "name": repo.name, "url": repo.url or "", "commit": repo.commit or "",
            "branch": repo.branch or ""}
    info.update({f"gitmodules.{k}": v for k, v in repo.meta.items()})
    if repo.git_dir and not repo.error:
        info["readme"] = readme_summary(repo)
        desc = Path(repo.git_dir) / "description"
        if desc.is_file():
            text = desc.read_text(encoding="utf-8", errors="replace").strip()
            if text and not text.startswith("Unnamed repository"):
                info["description"] = text
    return info


# ───────────────────────────────────────────────────────── Jira

def key_grep_regex(keys: list[str]) -> str:
    """git log --grep(POSIX ERE)용. 키 앞뒤 경계를 대략 맞추고, 정확한 판정은 Python에서 한다."""
    alts = "|".join(re.escape(k).replace("\\-", "-") for k in keys)
    return f"(^|[^A-Za-z0-9_])({alts})([^0-9]|$)"


def keys_in(text: str, projects: set[str] | None) -> list[str]:
    found = []
    for m in KEY_RE.finditer(text):
        proj = m.group(1)
        if projects is not None:
            if proj not in projects:
                continue
        elif proj in NOT_JIRA:
            continue
        key = f"{proj}-{m.group(2)}"
        if key not in found:
            found.append(key)
    return found


def ranges(atlas: Atlas, repos_new: list[Repo], from_ref: str | None) -> dict[str, str]:
    """저장소별 log 범위. --from이 있으면 그 시점의 고정 커밋..지금 고정 커밋."""
    if not from_ref:
        return {r.label: r.commit for r in repos_new}
    old = {r.label: r for r in atlas.walk(from_ref)}
    result = {}
    for r in repos_new:
        o = old.get(r.label)
        if o is None or not o.commit:
            result[r.label] = r.commit                 # 새로 생긴 서브모듈: 전체 이력
        elif o.commit == r.commit:
            continue                                    # 변화 없음
        elif has_commit(Path(r.git_dir), o.commit):
            result[r.label] = f"{o.commit}..{r.commit}"
        else:
            atlas.log(f"경고: {r.label}: 이전 커밋 {o.commit[:12]}이 없어 전체 이력을 봅니다")
            result[r.label] = r.commit
    return result


# ───────────────────────────────────────────────────────── 출력

def short(sha: str) -> str:
    return sha[:10]


def emit_json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def report_errors(repos: list[Repo], log) -> None:
    for r in repos:
        if r.error:
            log(f"경고: {r.label}: {r.error}")


# ───────────────────────────────────────────────────────── 명령

def cmd_tree(atlas: Atlas, a) -> int:
    repos = atlas.walk(a.ref)
    if a.json:
        emit_json([{k: v for k, v in asdict(r).items()} for r in repos])
        return 0
    for r in repos:
        indent = "  " * r.depth
        state = f"  ✗ {r.error}" if r.error else ("  (로컬)" if r.local else "")
        print(f"{indent}{r.label}  {short(r.commit or '-' * 10)}  {r.url or ''}{state}")
    return 0


def cmd_sync(atlas: Atlas, a) -> int:
    atlas.fetch = True
    repos = atlas.walk(a.ref)
    report_errors(repos, atlas.log)
    ok = sum(1 for r in repos if not r.error)
    print(f"저장소 {len(repos)}개 중 {ok}개 준비됨. 캐시: {atlas.cache}")
    return 0 if ok == len(repos) else 2


def cmd_grep(atlas: Atlas, a) -> int:
    repos = atlas.walk(a.ref)
    report_errors(repos, atlas.log)
    patterns = [a.pattern] + (a.e or [])
    results = parallel(lambda r: grep_repo(r, patterns, a, a.pathspec), select(repos, a.only, a.exclude), a.jobs)
    hits = [h for _, hs in results for h in hs]
    if a.json:
        emit_json(hits)
    elif a.files_with_matches:
        for path in dict.fromkeys(h["path"] for h in hits):
            print(path)
    else:
        for h in hits:
            print(f"{h['path']}:{h['line']}:{h['text']}")
    return 0 if hits else 1


def cmd_files(atlas: Atlas, a) -> int:
    repos = atlas.walk(a.ref)
    report_errors(repos, atlas.log)
    results = parallel(lambda r: files_repo(r, a.pattern, a), select(repos, a.only, a.exclude), a.jobs)
    hits = [h for _, hs in results for h in hs]
    if a.json:
        emit_json(hits)
    else:
        for h in hits:
            print(h["path"])
    return 0 if hits else 1


def cmd_desc(atlas: Atlas, a) -> int:
    repos = atlas.walk(a.ref)
    infos = [describe(r) for r in repos if not a.only or r in select(repos, a.only, None)]
    if a.pattern:
        rx = re.compile(a.pattern, 0 if a.case_sensitive else re.IGNORECASE)
        infos = [dict(i, matched=[k for k, v in i.items() if isinstance(v, str) and rx.search(v)]) for i in infos]
        infos = [i for i in infos if i["matched"]]
    if a.json:
        emit_json(infos)
    else:
        for i in infos:
            print(f"{i['repo']}  [{i['name']}]  {i['url']}")
            for k in ("branch", "description", "readme"):
                if i.get(k):
                    print(f"    {k}: {i[k]}")
            for k, v in i.items():
                if k.startswith("gitmodules."):
                    print(f"    {k}: {v}")
            if i.get("matched"):
                print(f"    일치: {', '.join(i['matched'])}")
    return 0 if infos else 1


def cmd_log(atlas: Atlas, a) -> int:
    repos = atlas.walk(a.ref)
    report_errors(repos, atlas.log)
    rng = ranges(atlas, repos, a.from_ref)
    chosen = [r for r in select(repos, a.only, a.exclude) if r.label in rng]
    mode = "fixed" if a.fixed else "extended"
    results = parallel(lambda r: log_repo(r, rng[r.label], a.pattern, a, mode), chosen, a.jobs)
    commits = [c for _, cs in results for c in cs]
    if a.json:
        emit_json(commits)
    else:
        for c in commits:
            print(f"{c['repo']}  {short(c['commit'])}  {c['date'][:10]}  {c['author']}  {c['subject']}")
    return 0 if commits else 1


def cmd_jira(atlas: Atlas, a) -> int:
    keys = list(dict.fromkeys(k.upper() for k in a.keys))
    bad = [k for k in keys if not re.fullmatch(r"[A-Z][A-Z0-9_]*-[1-9][0-9]*", k)]
    if bad:
        raise AtlasError(f"Jira 키 형식이 아님: {', '.join(bad)}")
    repos = atlas.walk(a.ref)
    report_errors(repos, atlas.log)
    rng = ranges(atlas, repos, a.from_ref)
    chosen = [r for r in select(repos, a.only, a.exclude) if r.label in rng]
    rx = key_grep_regex(keys)
    results = parallel(lambda r: log_repo(r, rng[r.label], [rx], a, "extended"), chosen, a.jobs)
    found: dict[str, list[dict]] = {k: [] for k in keys}
    projects = {k.rsplit("-", 1)[0] for k in keys}
    for _, commits in results:
        for c in commits:
            for k in keys_in(c["message"], projects):
                if k in found:
                    found[k].append(c)
    if a.json:
        emit_json({k: {"found": bool(v), "commits": v} for k, v in found.items()})
    else:
        where = f"{a.from_ref}..{a.ref or 'HEAD'}" if a.from_ref else (a.ref or "HEAD")
        for k, commits in found.items():
            if commits:
                repos_hit = sorted({c["repo"] for c in commits})
                print(f"{k}  포함  ({where}, 커밋 {len(commits)}개, 저장소: {', '.join(repos_hit)})")
                for c in commits:
                    print(f"    {c['repo']}  {short(c['commit'])}  {c['date'][:10]}  {c['author']}  {c['subject']}")
            else:
                print(f"{k}  없음  ({where})")
    return 0 if all(found.values()) else 1


def cmd_keys(atlas: Atlas, a) -> int:
    projects = {p.strip().upper() for p in a.project.split(",")} if a.project else None
    repos = atlas.walk(a.ref)
    report_errors(repos, atlas.log)
    rng = ranges(atlas, repos, a.from_ref)
    chosen = [r for r in select(repos, a.only, a.exclude) if r.label in rng]
    rx = "[A-Z][A-Z0-9_]*-[1-9]"
    results = parallel(lambda r: log_repo(r, rng[r.label], [rx], a, "extended"), chosen, a.jobs)
    table: dict[str, list[dict]] = {}
    for _, commits in results:
        for c in commits:
            for k in keys_in(c["message"], projects):
                table.setdefault(k, []).append(c)

    def order(k: str):
        p, n = k.rsplit("-", 1)
        return (p, int(n))
    keys = sorted(table, key=order)
    if a.json:
        emit_json({k: table[k] for k in keys})
    else:
        for k in keys:
            repos_hit = sorted({c["repo"] for c in table[k]})
            print(f"{k}  커밋 {len(table[k])}개  {', '.join(repos_hit)}")
            if a.verbose:
                for c in table[k]:
                    print(f"    {c['repo']}  {short(c['commit'])}  {c['date'][:10]}  {c['subject']}")
    return 0 if keys else 1


# ───────────────────────────────────────────────────────── CLI

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-C", "--repo", dest="target", default=None,
                        help="superproject 경로 또는 URL (기본: 현재 폴더)")
    common.add_argument("-r", "--ref", help="검색 기준 ref: 태그, 브랜치, 커밋 (기본 HEAD)")
    common.add_argument("--cache", type=Path, help="bare 캐시 폴더 (기본 ~/.cache/git-atlas, GIT_ATLAS_CACHE)")
    common.add_argument("-j", "--jobs", type=int, default=8, help="동시 작업 수")
    common.add_argument("--fetch", action="store_true", help="캐시된 저장소를 먼저 가져오기(fetch)")
    common.add_argument("--offline", action="store_true", help="네트워크를 쓰지 않음")
    common.add_argument("--max-depth", type=int, default=32, help="서브모듈 중첩 한도")
    common.add_argument("--only", action="append", metavar="GLOB", help="이 경로의 저장소만 (여러 번 가능)")
    common.add_argument("--exclude", action="append", metavar="GLOB", help="이 경로의 저장소 제외")
    common.add_argument("--json", action="store_true", help="JSON으로 출력")
    common.add_argument("-q", "--quiet", action="store_true", help="진행 메시지 숨김")

    p = argparse.ArgumentParser(prog="git-atlas", description="superproject와 모든 서브모듈을 한 번에 검색")
    p.add_argument("-C", dest="target0", metavar="REPO", help="superproject 경로 또는 URL (git -C처럼 앞에 써도 됨)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("tree", parents=[common], help="저장소 트리(서브모듈, 고정 커밋, URL)")
    s.set_defaults(func=cmd_tree)

    s = sub.add_parser("sync", parents=[common], help="모든 저장소를 캐시에 받거나 갱신")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("grep", parents=[common], help="파일 내용 검색 (git grep)")
    s.add_argument("pattern")
    s.add_argument("pathspec", nargs="*", help="경로 제한 (예: '*.c' src/)")
    s.add_argument("-e", action="append", help="패턴 추가 (OR)")
    s.add_argument("-i", "--ignore-case", action="store_true")
    s.add_argument("-w", "--word", action="store_true")
    s.add_argument("-l", "--files-with-matches", action="store_true")
    g = s.add_mutually_exclusive_group()
    g.add_argument("-F", dest="mode", action="store_const", const="fixed", help="고정 문자열")
    g.add_argument("-E", dest="mode", action="store_const", const="extended", help="확장 정규식")
    g.add_argument("-P", dest="mode", action="store_const", const="perl", help="Perl 정규식")
    s.set_defaults(func=cmd_grep, mode="basic")

    s = sub.add_parser("files", parents=[common], help="파일 이름 검색 (기본 정규식, --glob)")
    s.add_argument("pattern")
    s.add_argument("-g", "--glob", action="store_true", help="glob 패턴 (경로 또는 파일 이름)")
    s.add_argument("-i", "--ignore-case", action="store_true")
    s.set_defaults(func=cmd_files)

    s = sub.add_parser("desc", parents=[common], help="저장소 설명 검색: 이름, 경로, URL, branch, README 첫 문단")
    s.add_argument("pattern", nargs="?", help="정규식 (없으면 전체 목록)")
    s.add_argument("-s", "--case-sensitive", action="store_true")
    s.set_defaults(func=cmd_desc)

    history = argparse.ArgumentParser(add_help=False)
    history.add_argument("--from", dest="from_ref", metavar="REF",
                         help="이 ref 이후의 변경만 (서브모듈은 두 시점의 고정 커밋 사이)")
    history.add_argument("--no-merges", action="store_true")

    s = sub.add_parser("log", parents=[common, history], help="커밋 메시지 검색 (ref 이전 이력 전체)")
    s.add_argument("pattern", nargs="+", help="정규식 (여러 개면 OR, --all-match면 AND)")
    s.add_argument("-i", "--ignore-case", action="store_true")
    s.add_argument("-F", "--fixed", action="store_true", help="고정 문자열")
    s.add_argument("--all-match", action="store_true")
    s.set_defaults(func=cmd_log)

    s = sub.add_parser("jira", parents=[common, history], help="Jira 키가 ref에 반영됐는지 확인 (없으면 종료 1)")
    s.add_argument("keys", nargs="+", metavar="KEY")
    s.set_defaults(func=cmd_jira)

    s = sub.add_parser("keys", parents=[common, history], help="커밋 메시지의 Jira 키 모으기 (릴리스 노트)")
    s.add_argument("--project", help="프로젝트 키만 (쉼표 구분, 예: KB,OPS)")
    s.add_argument("-v", "--verbose", action="store_true", help="커밋 목록도 출력")
    s.set_defaults(func=cmd_keys)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    a = build_parser().parse_args(argv)
    log = (lambda msg: None) if a.quiet else (lambda msg: print(msg, file=sys.stderr, flush=True))
    try:
        atlas = Atlas(a.target or a.target0 or ".", a.cache or default_cache(), jobs=a.jobs, fetch=a.fetch,
                      offline=a.offline, max_depth=a.max_depth, log=log)
        return a.func(atlas, a)
    except AtlasError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
