#!/usr/bin/env python3
"""公開前の検査：manifest のチェックサム、文書リンク、混入しやすい秘密・固有情報のパターン。

使い方: python3 scripts/check_release.py [--root DIR]
問題があれば一覧を出して終了コード 1 を返す。
"""
import hashlib
import json
import os
import re
import sys

FORBIDDEN = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("api-key-prefix", re.compile(r"\boc_sk_[A-Za-z0-9]")),
    ("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("home-path", re.compile(r"/home/[A-Za-z0-9._-]+/")),
    ("windows-user-path", re.compile(r"C:\\Users\\[A-Za-z0-9._-]+")),
]
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def sha256_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if "--root" in sys.argv:
        root = os.path.abspath(sys.argv[sys.argv.index("--root") + 1])
    problems = []

    # 1. manifest と payload の一致
    mpath = os.path.join(root, "manifest.json")
    if not os.path.isfile(mpath):
        problems.append("manifest.json がありません")
        manifest = {"files": {}}
    else:
        with open(mpath, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    for rel, expected in sorted((manifest.get("files") or {}).items()):
        p = os.path.join(root, "payload", *rel.split("/"))
        if not os.path.isfile(p):
            problems.append("payload にファイルがありません: " + rel)
        elif sha256_file(p) != expected:
            problems.append("チェックサム不一致: payload/" + rel)
    payload_dir = os.path.join(root, "payload")
    for dirpath, _dirnames, filenames in os.walk(payload_dir):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, payload_dir).replace(os.sep, "/")
            if rel not in (manifest.get("files") or {}):
                problems.append("manifest に載っていない payload ファイル: " + rel)

    # 2. 文書リンクと混入パターンの走査
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in filenames:
            if not (name.endswith(".md") or name.endswith(".py") or name.endswith(".json") or name.endswith(".yml")):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            with open(full, "r", encoding="utf-8", errors="strict") as f:
                text = f.read()
            for label, pattern in FORBIDDEN:
                if pattern.search(text):
                    problems.append("混入パターン %s: %s" % (label, rel))
            if name.endswith(".md"):
                for m in LINK_RE.finditer(text):
                    url = m.group(1).strip()
                    if url.startswith(("http://", "https://", "#", "mailto:")):
                        continue
                    path_part = url.split("#")[0]
                    if not path_part:
                        continue
                    target = os.path.normpath(os.path.join(os.path.dirname(full), path_part))
                    if not os.path.exists(target):
                        problems.append("リンク切れ: %s -> %s" % (rel, url))

    # 3. スキルのメタデータ（name とフォルダー名の一致、description の有無）
    skills_dir = os.path.join(payload_dir, "skills")
    if os.path.isdir(skills_dir):
        for name in sorted(os.listdir(skills_dir)):
            skill_md = os.path.join(skills_dir, name, "SKILL.md")
            if not os.path.isfile(skill_md):
                problems.append("SKILL.md がありません: skills/" + name)
                continue
            with open(skill_md, "r", encoding="utf-8") as f:
                head = f.read(2000)
            m = re.search(r"^name:\s*(\S+)\s*$", head, re.M)
            if not m:
                problems.append("frontmatter に name がありません: skills/" + name)
            elif m.group(1) != name:
                problems.append("name とフォルダー名が一致しません: skills/" + name)
            if not re.search(r"^description:\s*\S", head, re.M):
                problems.append("frontmatter に description がありません: skills/" + name)

    # 4. 公開対象の許可リスト
    allowed_root = {
        "README.md", "INSTALL.md", "SECURITY.md", "LICENSE", "manifest.json",
        ".gitignore", ".gitattributes",
    }
    allowed_dirs = ("docs/", "payload/", "scripts/", "templates/", "tests/", ".github/")
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        if "__pycache__" in dirnames:
            dirnames.remove("__pycache__")
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if rel in allowed_root or any(rel.startswith(d) for d in allowed_dirs):
                continue
            problems.append("許可リストに無いファイル: " + rel)

    if problems:
        for p in problems:
            print("check: NG - " + p)
        return 1
    print("check: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
