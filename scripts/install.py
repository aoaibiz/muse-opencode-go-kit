#!/usr/bin/env python3
"""Muse Window × OpenCode Go Kit の payload を対象フォルダーへ導入する。

使い方:
  python3 scripts/install.py plan     --target DIR [--kit DIR]
  python3 scripts/install.py apply    --target DIR [--kit DIR]
  python3 scripts/install.py verify   --target DIR [--kit DIR]
  python3 scripts/install.py rollback --target DIR [--kit DIR]
  python3 scripts/install.py recover  --target DIR [--kit DIR]

導入内容:
  - payload/AGENTS.md の本文を、対象の AGENTS.md へマーカー付きブロックで追加する
    （既存のバイト列はブロックの外側でそのまま保持する）。
  - payload/skills/<name>/SKILL.md を対象の skills/<name>/SKILL.md へ置く。

状態とバックアップは対象の .muse-window-kit/ にだけ置く。ネットワークは使わない。
失敗時は段階と安全なエラーコードを出し、ファイル本文や秘密は出力しない。
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile

BLOCK_START = "<!-- muse-window-kit:start -->"
BLOCK_END = "<!-- muse-window-kit:end -->"
STATE_DIR_NAME = ".muse-window-kit"


class KitError(Exception):
    def __init__(self, code, stage, message):
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.message = message


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def decode_utf8(data, label):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise KitError("ENCODING_INVALID", "load", "UTF-8 として読めないファイルがあります: " + label)


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def write_bytes_atomic(path, data):
    import stat as stat_mod
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if os.path.exists(path):
            os.chmod(tmp, stat_mod.S_IMODE(os.stat(path).st_mode))
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise KitError("WRITE_FAILED", "write", "書き込みに失敗しました: %s" % e.__class__.__name__)


def load_manifest(kit_dir):
    mpath = os.path.join(kit_dir, "manifest.json")
    if not os.path.isfile(mpath):
        raise KitError("MANIFEST_MISSING", "load", "manifest.json がありません")
    try:
        with open(mpath, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise KitError("MANIFEST_BROKEN", "load", "manifest.json が壊れています")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise KitError("MANIFEST_BROKEN", "load", "manifest.json の形式が不正です")
    return manifest


def validate_payload_rel(rel):
    parts = rel.split("/")
    if rel.startswith("/") or "\\" in rel or any(p in ("", ".", "..") for p in parts):
        raise KitError("MANIFEST_BROKEN", "load", "manifest のパスが不正です: " + rel)
    if rel == "AGENTS.md":
        return
    if len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md":
        return
    raise KitError("MANIFEST_BROKEN", "load", "manifest に許可されていない配置先があります: " + rel)


def payload_files(kit_dir):
    """manifest の files キー（payload/ からの相対パス）→ 実パス。"""
    manifest = load_manifest(kit_dir)
    files = {}
    for rel in manifest["files"]:
        validate_payload_rel(rel)
        files[rel] = os.path.join(kit_dir, "payload", *rel.split("/"))
    return manifest, files


def check_payload_checksums(kit_dir):
    manifest, files = payload_files(kit_dir)
    for rel, path in files.items():
        if not os.path.isfile(path):
            raise KitError("PAYLOAD_MISSING", "load", "payload に必要なファイルがありません: " + rel)
        if sha256_bytes(read_bytes(path)) != manifest["files"][rel]:
            raise KitError("CHECKSUM_MISMATCH", "load", "payload のチェックサムが manifest と一致しません: " + rel)
    return manifest, files


def target_paths(target_dir, rel):
    """payload 相対パス → 対象側の配置パス。"""
    if rel == "AGENTS.md":
        return os.path.join(target_dir, "AGENTS.md")
    return os.path.join(target_dir, *rel.split("/"))


def state_dir(target_dir):
    return os.path.join(target_dir, STATE_DIR_NAME)


def load_state(target_dir):
    sd = state_dir(target_dir)
    if os.path.islink(sd):
        raise KitError("SYMLINK_REFUSED", "load", "状態ディレクトリがシンボリックリンクです")
    spath = os.path.join(sd, "state.json")
    if os.path.islink(spath):
        raise KitError("SYMLINK_REFUSED", "load", "状態記録がシンボリックリンクです")
    if not os.path.isfile(spath):
        return None
    try:
        with open(spath, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise KitError("STATE_BROKEN", "load", "導入の状態記録が壊れています")
    if not isinstance(state, dict):
        raise KitError("STATE_BROKEN", "load", "導入の状態記録の形式が不正です")
    return state


def save_state(target_dir, state):
    sd = ensure_state_dir(target_dir)
    spath = os.path.join(sd, "state.json")
    assert_safe_path(target_dir, spath, "state.json")
    write_bytes_atomic(
        spath,
        json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def render_block(payload_agents_text):
    return BLOCK_START + "\n" + payload_agents_text.rstrip("\n") + "\n" + BLOCK_END + "\n"


def split_block(text):
    """AGENTS.md のテキストを (前, ブロック内, 後) に分ける。ブロックが無ければ (text, None, '')。"""
    if BLOCK_START not in text:
        return text, None, ""
    before, rest = text.split(BLOCK_START, 1)
    if BLOCK_END not in rest:
        raise KitError("BLOCK_BROKEN", "inspect", "AGENTS.md のブロック終端がありません")
    inner, after = rest.split(BLOCK_END, 1)
    inner = inner.strip("\n")
    after = after.lstrip("\n")
    return before, inner, after


def check_parent_links(target_dir, dest, rel):
    parent = os.path.dirname(dest)
    current = parent
    while True:
        if os.path.islink(current):
            raise KitError("SYMLINK_REFUSED", "inspect", "配置先の親フォルダーがシンボリックリンクです: " + rel)
        if os.path.abspath(current) == os.path.abspath(target_dir):
            break
        nxt = os.path.dirname(current)
        if nxt == current:
            break
        current = nxt


def assert_safe_path(target_dir, path, label):
    """対象フォルダー内のパスであることと、リンクをたどらないことを確かめる。

    配置先・状態・バックアップ・ロック・復元先の全てに同じ検査を使う。
    """
    if os.path.islink(path):
        raise KitError("SYMLINK_REFUSED", "inspect", "シンボリックリンクは扱いません: " + label)
    check_parent_links(target_dir, path, label)
    real_target = os.path.realpath(target_dir)
    real_path = os.path.realpath(path)
    try:
        inside = os.path.commonpath([real_target, real_path]) == real_target
    except ValueError:
        inside = False
    if not inside:
        raise KitError("PATH_OUTSIDE_TARGET", "inspect", "対象フォルダーの外を指しています: " + label)


def ensure_state_dir(target_dir):
    """状態ディレクトリを安全に用意する（所有者だけが入れる 0700）。"""
    sd = state_dir(target_dir)
    if os.path.islink(sd):
        raise KitError("SYMLINK_REFUSED", "inspect", "状態ディレクトリがシンボリックリンクです")
    if os.path.isdir(sd):
        assert_safe_path(target_dir, sd, STATE_DIR_NAME)
        try:
            os.chmod(sd, 0o700)
        except OSError:
            pass
        return sd
    os.makedirs(sd, mode=0o700)
    try:
        os.chmod(sd, 0o700)
    except OSError:
        pass
    return sd


def migrate_permissions(target_dir):
    """旧版が作った状態フォルダーとバックアップの権限を、安全側へ移す。

    旧版は状態フォルダーを 0755、バックアップを 0644 で残すことがある。
    更新（apply）の時に、境界検査を通したうえで、フォルダーは 0700、
    ファイルは 0600 へ寄せる。失敗したら黙って成功扱いにせず、エラーで止める。
    """
    sd = ensure_state_dir(target_dir)

    def tighten(path, mode, label):
        if os.path.islink(path):
            raise KitError("SYMLINK_REFUSED", "inspect", "シンボリックリンクは扱いません: " + label)
        try:
            os.chmod(path, mode)
        except OSError:
            raise KitError("PERMISSION_MIGRATION_FAILED", "apply", "権限の移行に失敗しました: " + label)

    tighten(sd, 0o700, STATE_DIR_NAME)
    spath = os.path.join(sd, "state.json")
    if os.path.lexists(spath):
        tighten(spath, 0o600, "state.json")
    backup_dir = os.path.join(sd, "backup")
    if os.path.lexists(backup_dir):
        if os.path.islink(backup_dir) or not os.path.isdir(backup_dir):
            raise KitError("SYMLINK_REFUSED", "inspect", "バックアップ先がシンボリックリンクです")
        assert_safe_path(target_dir, backup_dir, "backup")
        tighten(backup_dir, 0o700, "backup")
        for root, dirs, files in os.walk(backup_dir):
            for d in dirs:
                p = os.path.join(root, d)
                assert_safe_path(target_dir, p, "backup")
                tighten(p, 0o700, "backup")
            for f in files:
                p = os.path.join(root, f)
                assert_safe_path(target_dir, p, "backup")
                tighten(p, 0o600, "backup")


def compute_plan(kit_dir, target_dir):
    manifest, files = check_payload_checksums(kit_dir)
    plan = []
    payload_agents = read_bytes(files["AGENTS.md"]).decode("utf-8")
    for rel in sorted(files):
        dest = target_paths(target_dir, rel)
        assert_safe_path(target_dir, dest, rel)
        if rel == "AGENTS.md":
            if os.path.isfile(dest):
                current = decode_utf8(read_bytes(dest), rel)
                _, inner, _ = split_block(current)
                if inner is None:
                    plan.append((rel, "append-block"))
                elif inner == payload_agents.rstrip("\n"):
                    plan.append((rel, "unchanged"))
                else:
                    state = load_state(target_dir)
                    recorded = (state or {}).get("block_sha256")
                    if recorded and sha256_bytes(inner.encode("utf-8")) == recorded:
                        plan.append((rel, "update-block"))
                    else:
                        raise KitError(
                            "LOCAL_EDIT_CONFLICT",
                            "inspect",
                            "AGENTS.md の導入ブロックが導入後に編集されています。統合案を提示してから進めてください",
                        )
            else:
                plan.append((rel, "create"))
        else:
            if os.path.isfile(dest):
                if read_bytes(dest) == read_bytes(files[rel]):
                    plan.append((rel, "unchanged"))
                else:
                    state = load_state(target_dir)
                    recorded = ((state or {}).get("files") or {}).get(rel)
                    if recorded and sha256_bytes(read_bytes(dest)) == recorded:
                        plan.append((rel, "update"))
                    else:
                        raise KitError(
                            "LOCAL_EDIT_CONFLICT",
                            "inspect",
                            "配置先のファイルが導入後に編集されています: " + rel,
                        )
            else:
                plan.append((rel, "create"))
    return manifest, plan


def acquire_lock(target_dir):
    sd = ensure_state_dir(target_dir)
    lock = os.path.join(sd, "lock")
    assert_safe_path(target_dir, lock, "lock")
    try:
        os.makedirs(lock)
    except FileExistsError:
        raise KitError("LOCKED", "apply", "別の導入処理が動いているか、ロックが残っています")
    return lock


def release_lock(lock):
    try:
        os.rmdir(lock)
    except OSError:
        pass


def cmd_plan(kit_dir, target_dir):
    _, plan = compute_plan(kit_dir, target_dir)
    for rel, action in plan:
        print("%s: %s" % (action, rel))
    return 0


def cmd_apply(kit_dir, target_dir):
    manifest, plan = compute_plan(kit_dir, target_dir)
    _, files = payload_files(kit_dir)
    ensure_state_dir(target_dir)
    lock = acquire_lock(target_dir)
    try:
        migrate_permissions(target_dir)
        backup_dir = os.path.join(ensure_state_dir(target_dir), "backup")
        if os.path.islink(backup_dir):
            raise KitError("SYMLINK_REFUSED", "inspect", "バックアップ先がシンボリックリンクです")
        state = load_state(target_dir) or {"files": {}}
        state["status"] = "PARTIAL"
        state["version"] = manifest["version"]
        state.setdefault("existed", {})
        state.setdefault("applied_hashes", {})
        for rel, _action in plan:
            dest0 = target_paths(target_dir, rel)
            if rel not in state["existed"]:
                state["existed"][rel] = os.path.isfile(dest0)
        save_state(target_dir, state)
        payload_agents = read_bytes(files["AGENTS.md"]).decode("utf-8")
        for rel, action in plan:
            dest = target_paths(target_dir, rel)
            if action == "unchanged":
                state["applied_hashes"][rel] = sha256_bytes(read_bytes(dest))
                save_state(target_dir, state)
                continue
            bpath = os.path.join(backup_dir, *rel.split("/"))
            assert_safe_path(target_dir, bpath, "backup/" + rel)
            if os.path.isfile(dest) and not os.path.lexists(bpath):
                import stat as stat_mod
                parent = os.path.dirname(bpath)
                os.makedirs(parent, mode=0o700, exist_ok=True)
                try:
                    os.chmod(parent, 0o700)
                except OSError:
                    pass
                shutil.copyfile(dest, bpath)
                os.chmod(bpath, stat_mod.S_IMODE(os.stat(dest).st_mode))
            if rel == "AGENTS.md":
                block = render_block(payload_agents)
                if action == "create":
                    write_bytes_atomic(dest, block.encode("utf-8"))
                else:
                    current = decode_utf8(read_bytes(dest), rel)
                    before, _, after = split_block(current)
                    merged = before
                    if merged and not merged.endswith("\n"):
                        merged += "\n"
                    merged += block
                    if after:
                        merged += after if after.endswith("\n") else after + "\n"
                    write_bytes_atomic(dest, merged.encode("utf-8"))
                state["block_sha256"] = sha256_bytes(payload_agents.rstrip("\n").encode("utf-8"))
            else:
                write_bytes_atomic(dest, read_bytes(files[rel]))
                state["files"][rel] = manifest["files"][rel]
            state["applied_hashes"][rel] = sha256_bytes(read_bytes(dest))
            save_state(target_dir, state)
        state["status"] = "INSTALLED"
        save_state(target_dir, state)
    finally:
        release_lock(lock)
    print("apply: ok (version %s)" % manifest["version"])
    return 0


def cmd_verify(kit_dir, target_dir):
    manifest, files = check_payload_checksums(kit_dir)
    problems = []
    payload_agents = read_bytes(files["AGENTS.md"]).decode("utf-8")
    dest = os.path.join(target_dir, "AGENTS.md")
    assert_safe_path(target_dir, dest, "AGENTS.md")
    if not os.path.isfile(dest):
        problems.append("AGENTS.md がありません")
    else:
        _, inner, _ = split_block(decode_utf8(read_bytes(dest), "AGENTS.md"))
        if inner is None:
            problems.append("AGENTS.md に導入ブロックがありません")
        elif inner != payload_agents.rstrip("\n"):
            problems.append("AGENTS.md のブロックが payload と一致しません")
    for rel in sorted(files):
        if rel == "AGENTS.md":
            continue
        d = target_paths(target_dir, rel)
        assert_safe_path(target_dir, d, rel)
        if not os.path.isfile(d):
            problems.append("ファイルがありません: " + rel)
        elif sha256_bytes(read_bytes(d)) != manifest["files"][rel]:
            problems.append("内容が manifest と一致しません: " + rel)
    if problems:
        for p in problems:
            print("verify: NG - " + p)
        return 1
    print("verify: ok")
    return 0


def restore_from_backup(kit_dir, target_dir):
    state = load_state(target_dir)
    if state is None:
        raise KitError("NOT_INSTALLED", "rollback", "導入の記録がありません")
    backup_dir = os.path.join(ensure_state_dir(target_dir), "backup")
    if os.path.islink(backup_dir):
        raise KitError("SYMLINK_REFUSED", "inspect", "バックアップ先がシンボリックリンクです")
    manifest = load_manifest(kit_dir)
    existed = state.get("existed") or {}
    applied = state.get("applied_hashes") or {}
    # 境界検査：復元・削除先とバックアップ元が対象内でリンクでないことを先に全件確かめる。
    for rel in sorted(manifest["files"]):
        assert_safe_path(target_dir, target_paths(target_dir, rel), rel)
        assert_safe_path(target_dir, os.path.join(backup_dir, *rel.split("/")), "backup/" + rel)
    # 復元前の照合：導入後に編集されたファイルがあれば、変更せず停止する。
    for rel in sorted(manifest["files"]):
        dest = target_paths(target_dir, rel)
        bpath = os.path.join(backup_dir, *rel.split("/"))
        if rel in applied:
            if not os.path.isfile(dest) or sha256_bytes(read_bytes(dest)) != applied[rel]:
                raise KitError(
                    "LOCAL_EDIT_CONFLICT",
                    "rollback",
                    "導入後に編集されたファイルがあります。復元を停止しました: " + rel,
                )
        elif existed.get(rel):
            if not os.path.isfile(dest):
                raise KitError("LOCAL_EDIT_CONFLICT", "rollback", "導入前のファイルが見当たりません: " + rel)
            if os.path.isfile(bpath) and sha256_bytes(read_bytes(dest)) != sha256_bytes(read_bytes(bpath)):
                raise KitError(
                    "LOCAL_EDIT_CONFLICT",
                    "rollback",
                    "導入後に編集されたファイルがあります。復元を停止しました: " + rel,
                )
        else:
            if os.path.isfile(dest):
                raise KitError(
                    "LOCAL_EDIT_CONFLICT",
                    "rollback",
                    "導入していない場所にファイルがあります。復元を停止しました: " + rel,
                )
    for rel in sorted(manifest["files"]):
        dest = target_paths(target_dir, rel)
        bpath = os.path.join(backup_dir, *rel.split("/"))
        if existed.get(rel):
            if os.path.isfile(bpath):
                write_bytes_atomic(dest, read_bytes(bpath))
        else:
            if rel == "AGENTS.md":
                if os.path.isfile(dest):
                    text = decode_utf8(read_bytes(dest), rel)
                    before, inner, after = split_block(text)
                    if inner is not None:
                        merged = before + after
                        if merged.strip():
                            write_bytes_atomic(dest, merged.encode("utf-8"))
                        else:
                            os.unlink(dest)
            else:
                if os.path.isfile(dest):
                    os.unlink(dest)
    state["status"] = "ROLLED_BACK"
    save_state(target_dir, state)


def cmd_rollback(kit_dir, target_dir):
    ensure_state_dir(target_dir)
    lock = acquire_lock(target_dir)
    try:
        restore_from_backup(kit_dir, target_dir)
    finally:
        release_lock(lock)
    print("rollback: ok")
    return 0


def cmd_recover(kit_dir, target_dir):
    state = load_state(target_dir)
    if state is None or state.get("status") != "PARTIAL":
        print("recover: 復旧が必要な状態ではありません")
        return 0
    ensure_state_dir(target_dir)
    lock = acquire_lock(target_dir)
    try:
        restore_from_backup(kit_dir, target_dir)
    finally:
        release_lock(lock)
    print("recover: ok（直前の状態へ戻しました）")
    return 0


def force_utf8_stdio():
    """標準出力・標準エラー出力の文字コードを UTF-8 にそろえる。

    Windows のロケール（cp1252 など）では、パイプへ向けた標準出力の
    既定がロケールの文字コードになり、日本語の案内を print した時に
    UnicodeEncodeError で落ちて終了コードまで変わってしまう。案内文は
    日本語が正なので、環境の既定に任せず UTF-8 で出す。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv=None):
    force_utf8_stdio()
    parser = argparse.ArgumentParser(description="Muse Window × OpenCode Go Kit installer")
    parser.add_argument("command", choices=["plan", "apply", "verify", "rollback", "recover"])
    parser.add_argument("--target", required=True)
    parser.add_argument("--kit", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    args = parser.parse_args(argv)
    target_dir = os.path.abspath(args.target)
    if not os.path.isdir(target_dir):
        print("error: TARGET_MISSING stage=inspect 対象フォルダーがありません", file=sys.stderr)
        return 2
    handlers = {
        "plan": cmd_plan,
        "apply": cmd_apply,
        "verify": cmd_verify,
        "rollback": cmd_rollback,
        "recover": cmd_recover,
    }
    try:
        return handlers[args.command](args.kit, target_dir)
    except KitError as e:
        print("error: %s stage=%s %s" % (e.code, e.stage, e.message), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
