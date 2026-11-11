"""git_atlas.py 프로토타입 테스트. 임시 폴더에 서브모듈(중첩, 상대 URL)이 있는 저장소를 만들어 검사한다."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import git_atlas as ga  # noqa: E402

ENV = {
    "GIT_AUTHOR_NAME": "tester", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "tester", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}


def sh(cwd: Path, *args: str) -> str:
    import os
    env = dict(os.environ, **ENV)
    proc = subprocess.run(["git", "-c", "protocol.file.allow=always", "-c", "init.defaultBranch=main", *args],
                          cwd=cwd, env=env, capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()
    return proc.stdout.decode()


def commit(repo: Path, files: dict[str, str], msg: str) -> None:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", msg)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """
    origin/
      super.git   ── libs/net (../net.git) ── vendor/zlib (../zlib.git)
                  └─ libs/ui  (../ui.git)
    v1: KB-1(net), KB-2(ui)   v2: KB-3(zlib), KB-4(net), ui 그대로, super에 OPS-7
    """
    base = tmp_path_factory.mktemp("w")
    origin = base / "origin"
    origin.mkdir()
    work = base / "work"
    work.mkdir()

    def make(name: str) -> Path:
        r = work / name
        r.mkdir()
        sh(r, "init", "-q")
        return r

    zlib, net, ui, sup = make("zlib"), make("net"), make("ui"), make("super")
    commit(zlib, {"README.md": "# zlib\n\n압축 라이브러리 사본.\n", "inflate.c": "int inflate(void) { return 0; }\n"},
           "zlib init")
    sh(origin, "clone", "-q", "--bare", str(zlib), "zlib.git")
    commit(net, {"README.md": "# net\n\nHTTP 클라이언트와 재시도 로직.\n", "src/http.c": "// timeout 30\n"},
           "KB-1 HTTP 타임아웃 추가")
    sh(net, "submodule", "add", "-q", str(origin / "zlib.git"), "vendor/zlib")
    sh(net, "config", "-f", ".gitmodules", "submodule.vendor/zlib.url", "../zlib.git")
    sh(net, "remote", "add", "origin", str(origin / "net.git"))
    commit(net, {}, "zlib 서브모듈")
    sh(origin, "clone", "-q", "--bare", str(net), "net.git")
    commit(ui, {"README.md": "# ui\n\n화면 위젯 모음.\n", "button.txt": "Button TODO\n"}, "KB-2: 버튼")
    sh(origin, "clone", "-q", "--bare", str(ui), "ui.git")

    sh(sup, "remote", "add", "origin", str(origin / "super.git"))
    commit(sup, {"README.md": "# super\n\n제품 전체.\n"}, "init")
    # 절대 경로로 add 한 뒤 .gitmodules를 상대 URL로 바꾼다
    sh(sup, "submodule", "add", "-q", str(origin / "net.git"), "libs/net")
    sh(sup, "submodule", "add", "-q", str(origin / "ui.git"), "libs/ui")
    sh(sup, "config", "-f", ".gitmodules", "submodule.libs/net.url", "../net.git")
    sh(sup, "config", "-f", ".gitmodules", "submodule.libs/ui.url", "../ui.git")
    sh(sup, "config", "-f", ".gitmodules", "submodule.libs/net.branch", "main")
    commit(sup, {}, "서브모듈 추가")
    sh(sup, "tag", "v1")

    # v2 변경
    zclone = work / "zlib"
    commit(zclone, {"inflate.c": "int inflate(void) { return 1; } // fix\n"}, "KB-3 inflate 버그 수정")
    sh(zclone, "push", "-q", str(origin / "zlib.git"), "HEAD:main")
    netsub = work / "net" / "vendor" / "zlib"
    sh(netsub, "fetch", "-q", "origin")
    sh(netsub, "checkout", "-q", "origin/main")
    commit(net, {"src/http.c": "// timeout 60\n"}, "KB-4 타임아웃 60초 (UTF-8 로그)")
    sh(net, "push", "-q", str(origin / "net.git"), "HEAD:main")
    supnet = sup / "libs" / "net"
    sh(supnet, "fetch", "-q", "origin")
    sh(supnet, "checkout", "-q", "origin/main")
    commit(sup, {"CHANGES.txt": "v2\n"}, "OPS-7 릴리스 v2")
    sh(sup, "tag", "v2")
    sh(origin, "clone", "-q", "--bare", str(sup), "super.git")
    return {"origin": origin, "base": base}


def run(capsys, *args) -> tuple[int, str, str]:
    code = ga.main(list(args))
    o = capsys.readouterr()
    return code, o.out, o.err


@pytest.fixture
def url_args(world, tmp_path):
    return ["-C", str(world["origin"] / "super.git"), "--cache", str(tmp_path / "cache"), "-q"]


# ── URL 해석 ──

@pytest.mark.parametrize("url,base,want", [
    ("../b.git", "https://h/org/a.git", "https://h/org/b.git"),
    ("../../x/b.git", "https://h/org/a.git", "https://h/x/b.git"),
    ("./sub.git", "https://h/org/a.git", "https://h/org/a.git/sub.git"),
    ("../b.git", "git@h:org/a.git", "git@h:org/b.git"),
    ("../b.git", "git@h:a.git", "git@h:b.git"),
    ("../b.git", "/srv/git/a.git", "/srv/git/b.git"),
    ("../b.git", "C:\\repos\\a", "C:/repos/b.git"),
    ("https://o/z.git", "https://h/a.git", "https://o/z.git"),
])
def test_resolve_url(url, base, want):
    assert ga.resolve_url(url, base) == want


def test_keys_in_filters_noise():
    assert ga.keys_in("KB-4 타임아웃 (UTF-8, SHA-1) fixes OPS-12, KB-4", None) == ["KB-4", "OPS-12"]
    assert ga.keys_in("KB-4 and OPS-12", {"KB"}) == ["KB-4"]
    assert ga.keys_in("xKB-4 KB-40", {"KB"}) == ["KB-40"]


# ── URL 입력 ──

def test_tree_from_url(capsys, url_args):
    code, out, _ = run(capsys, "tree", "--json", *url_args)
    assert code == 0
    repos = {r["path"]: r for r in json.loads(out)}
    assert set(repos) == {"", "libs/net", "libs/net/vendor/zlib", "libs/ui"}
    assert repos["libs/net"]["url"].endswith("origin/net.git")
    assert repos["libs/net/vendor/zlib"]["url"].endswith("origin/zlib.git")
    assert not any(r["error"] for r in repos.values())


def test_grep_all_repos_and_ref(capsys, url_args):
    code, out, _ = run(capsys, "grep", "timeout", *url_args)
    assert code == 0 and "libs/net/src/http.c:1:// timeout 60" in out
    code, out, _ = run(capsys, "grep", "timeout", "-r", "v1", *url_args)
    assert "libs/net/src/http.c:1:// timeout 30" in out
    code, out, _ = run(capsys, "grep", "return 1", "-F", *url_args)
    assert "libs/net/vendor/zlib/inflate.c:1:" in out
    code, out, _ = run(capsys, "grep", "nothing-here-xyz", *url_args)
    assert code == 1 and out == ""


def test_grep_pathspec_and_only(capsys, url_args):
    code, out, _ = run(capsys, "grep", "TODO", "*.txt", *url_args)
    assert out.strip() == "libs/ui/button.txt:1:Button TODO"
    code, out, _ = run(capsys, "grep", "-i", "readme|ui", "-E", "--only", "libs/ui", "-l", *url_args)
    assert all(line.startswith("libs/ui/") for line in out.split())


def test_files(capsys, url_args):
    code, out, _ = run(capsys, "files", "-g", "*.c", *url_args)
    assert sorted(out.split()) == ["libs/net/src/http.c", "libs/net/vendor/zlib/inflate.c"]
    code, out, _ = run(capsys, "files", "readme", "-i", *url_args)
    assert len(out.split()) == 4


def test_desc(capsys, url_args):
    code, out, _ = run(capsys, "desc", "재시도", *url_args)
    assert code == 0 and out.startswith("libs/net  [libs/net]")
    code, out, _ = run(capsys, "desc", "--json", *url_args)
    infos = {i["repo"]: i for i in json.loads(out)}
    assert infos["libs/net"]["branch"] == "main"
    assert "압축" in infos["libs/net/vendor/zlib"]["readme"]


def test_log(capsys, url_args):
    code, out, _ = run(capsys, "log", "타임아웃", *url_args)
    assert code == 0
    assert out.count("libs/net ") == 2
    code, out, _ = run(capsys, "log", "타임아웃", "--from", "v1", "-r", "v2", *url_args)
    assert out.count("libs/net ") == 1 and "60초" in out


def test_jira_at_tags(capsys, url_args):
    code, out, _ = run(capsys, "jira", "KB-1", "KB-3", "-r", "v1", *url_args)
    assert code == 1
    assert "KB-1  포함" in out and "KB-3  없음" in out
    code, out, _ = run(capsys, "jira", "kb-3", "KB-4", "OPS-7", "-r", "v2", *url_args)
    assert code == 0
    assert "libs/net/vendor/zlib" in out
    code, out, _ = run(capsys, "jira", "KB-1", "--json", *url_args)
    data = json.loads(out)
    assert data["KB-1"]["found"] and data["KB-1"]["commits"][0]["repo"] == "libs/net"


def test_jira_does_not_match_longer_key(capsys, url_args):
    code, out, _ = run(capsys, "jira", "KB-10", *url_args)
    assert code == 1 and "KB-10  없음" in out


def test_keys_release_range(capsys, url_args):
    code, out, _ = run(capsys, "keys", "--from", "v1", "-r", "v2", "--json", *url_args)
    assert code == 0
    assert list(json.loads(out)) == ["KB-3", "KB-4", "OPS-7"]
    code, out, _ = run(capsys, "keys", "--project", "KB", *url_args)
    assert [line.split()[0] for line in out.splitlines()] == ["KB-1", "KB-2", "KB-3", "KB-4"]


def test_offline_uses_cache(capsys, url_args):
    run(capsys, "sync", *url_args)
    code, out, _ = run(capsys, "grep", "Button", "--offline", *url_args)
    assert code == 0 and "libs/ui/button.txt" in out


def test_missing_ref(capsys, url_args):
    code, _, err = run(capsys, "tree", "-r", "no-such-tag", *url_args)
    assert code == 2 and "ref를 찾을 수 없음" in err


# ── 서브모듈 init 없이 최상위만 체크아웃 ──

def test_superproject_checkout_without_submodules(capsys, world, tmp_path):
    co = tmp_path / "checkout"
    sh(tmp_path, "clone", "-q", str(world["origin"] / "super.git"), str(co))
    assert not any((co / "libs" / "net").iterdir())  # 비어 있음
    code, out, _ = run(capsys, "jira", "KB-3", "-C", str(co), "--cache", str(tmp_path / "c"), "-q")
    assert code == 0 and "libs/net/vendor/zlib" in out


def test_checkout_uses_initialized_submodule(capsys, world, tmp_path):
    co = tmp_path / "checkout"
    sh(tmp_path, "clone", "-q", str(world["origin"] / "super.git"), str(co))
    sh(co, "submodule", "update", "--init", "-q", "libs/ui")
    code, out, _ = run(capsys, "tree", "--json", "-C", str(co), "--cache", str(tmp_path / "c"), "-q")
    repos = {r["path"]: r for r in json.loads(out)}
    assert repos["libs/ui"]["local"] is True
    assert repos["libs/net"]["local"] is False
