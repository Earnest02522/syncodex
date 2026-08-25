# -*- coding: utf-8 -*-
"""
syncodex — Sync Codex config, skills & model catalog to remote servers over SSH.

A local-first web console: Python stdlib HTTP server + one HTML page.
Zero third-party dependencies by default — it shells out to the system OpenSSH
client (ssh/scp), which ships with Windows 10+, macOS and virtually every Linux.
Paramiko is OPTIONAL and only needed for password-based auth (typing a server
password in the web UI) or encrypted private keys without an ssh-agent.

Security:
  * The HTTP server binds to 127.0.0.1 only (never exposed to the network).
  * auth.json / API keys are never synced.
  * Passwords / passphrases are stored in plain text in the local config.json
    (git-ignored). Never commit that file.

Design:
  * `model_provider` and `[model_providers.*]` NEVER enter the shared config, so
    every server keeps its own provider. Switching models locally with ccswitch
    will not break the server ("Model provider `codex` not found" is avoided).
  * The server config.toml is rebuilt as: shared header (portable keys) +
    machine-specific tail (config.server.tail.toml, auto-generated on first sync).

API:
  GET  /                       -> console page
  GET  /api/status             -> status
  GET  /api/log?since=N        -> incremental logs
  GET  /api/detect             -> detect local codex home candidates
  POST /api/extract            -> extract portable shared config
  POST /api/preview            -> diff preview     {"targets":["name", ...]}
  POST /api/sync               -> sync             {"targets":[...]}
  POST /api/test               -> test connection  {"targets":[...]}
  POST /api/restart            -> restart app-server {"targets":[...]}
  POST /api/targets            -> create/update target {"target":{...}}
  POST /api/targets/delete     -> delete target    {"name":"..."}
  POST /api/settings           -> update settings  {...}
"""
import base64
import difflib
import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:  # optional dependency: only needed for password auth / encrypted keys
    import paramiko
    HAS_PARAMIKO = True
except Exception:  # pragma: no cover - depends on environment
    paramiko = None
    HAS_PARAMIKO = False

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "state.json"
SHARED_PATH = BASE / "config.shared.toml"
TAR_PATH = BASE / "skills.tar"

DEFAULT_SHARED_KEYS = [
    "model", "model_reasoning_effort",
    "disable_response_storage", "model_catalog_json",
    "auto_review_model_override",
]

_LOCK = threading.Lock()
_LOG = deque(maxlen=3000)
_LOG_SEQ = 0
_RUNNING = {"sync": False, "test": False, "restart": False}


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------
def now_str():
    return datetime.now().strftime("%H:%M:%S")


def log(level, msg, target=None):
    global _LOG_SEQ
    with _LOCK:
        _LOG_SEQ += 1
        _LOG.append({"i": _LOG_SEQ, "t": now_str(), "level": level,
                     "msg": msg, "target": target})


def get_log(since=0):
    with _LOCK:
        return [x for x in _LOG if x["i"] > since], _LOG_SEQ


# --------------------------------------------------------------------------
# config / state
# --------------------------------------------------------------------------
def default_config():
    return {
        "port": 8765,
        "codex_home": str(Path.home() / ".codex"),
        "catalog_file": "cc-switch-model-catalog.json",
        "sync_model": True,
        "sync_skills": True,
        "sync_catalog": True,
        "mirror_skills": True,
        "shared_keys": list(DEFAULT_SHARED_KEYS),
        "targets": [],
    }


def load_config():
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg = default_config()
            cfg.update(data)
            return cfg
        except Exception as e:
            log("err", f"config.json 解析失败，使用默认配置: {e}")
    return default_config()


def save_config():
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(CFG, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_sync": {}, "last_test": {}, "last_restart": {}}


def save_state(st):
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


CFG = load_config()
STATE = load_state()


def shared_keys():
    keys = set(CFG.get("shared_keys", DEFAULT_SHARED_KEYS))
    keys.discard("model_provider")  # provider NEVER leaves the local machine
    if not CFG.get("sync_model", True):
        keys.discard("model")
        keys.discard("model_reasoning_effort")
    return keys


def reload_shared_keys():
    global SHARED_KEYS
    SHARED_KEYS = shared_keys()


SHARED_KEYS = shared_keys()


def run(cmd, timeout=120, input_text=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           input=input_text, errors="replace")
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "command not found: " + str(cmd[0] if cmd else "")
    except Exception as e:
        return 1, "", str(e)


def ssh_bin():
    return CFG.get("ssh") or shutil.which("ssh") or "ssh"


def scp_bin():
    return CFG.get("scp") or shutil.which("scp") or "scp"


# --------------------------------------------------------------------------
# local helpers
# --------------------------------------------------------------------------
def local_config_text():
    cfg = Path(CFG["codex_home"]) / "config.toml"
    if cfg.exists():
        return cfg.read_text(encoding="utf-8", errors="replace")
    return ""


def local_provider():
    for ln in local_config_text().splitlines():
        m = re.match(r"^\s*model_provider\s*=\s*\"?([A-Za-z0-9_.-]+)", ln)
        if m:
            return m.group(1)
    return ""


def extract_shared_lines(lines):
    """Keep only top-level portable keys; drop every [section]. """
    out = []
    for ln in lines:
        s = ln.strip()
        if s.startswith("["):
            continue
        m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=", ln)
        if m and m.group(1) in SHARED_KEYS:
            out.append(ln)
    while out and not out[0].strip():
        out.pop(0)
    return out


def extract_shared():
    """Write config.shared.toml from the local config.toml. Returns (ok, content, msg, note)."""
    text = local_config_text()
    if not text:
        return False, "", "未找到本地 config.toml，请先确认「本地 codex 文件夹」正确", ""
    lines = extract_shared_lines(text.splitlines())
    if not lines:
        return False, "", "本地 config.toml 中没有可同步的共享键", ""
    content = "\n".join(lines) + "\n"
    SHARED_PATH.write_text(content, encoding="utf-8")
    prov = local_provider()
    note = ("服务器将保留各自的 provider，本地当前 provider=" + (prov or "无") +
            " 不会同步过去") if prov else "未检测到本地 model_provider"
    return True, content, f"已提取 {len(lines)} 行共享配置", note


def local_skills_list():
    root = Path(CFG["codex_home"]) / "skills"
    files = []
    if root.exists():
        for p in sorted(root.rglob("*")):
            if p.is_file():
                rel = p.relative_to(root).as_posix()
                if rel.startswith(".system/"):
                    continue
                files.append(rel)
    return sorted(files)


def catalog_slugs(text):
    """{slug: display_name} from cc-switch-model-catalog.json; None if unparseable."""
    try:
        data = json.loads(text)
    except Exception:
        return None
    models = data.get("models", []) if isinstance(data, dict) else []
    out = {}
    for m in models:
        slug = m.get("slug") or m.get("model") or ""
        if slug:
            out[slug] = m.get("display_name") or m.get("displayName") or slug
    return out


def local_catalog_slugs():
    cat = Path(CFG["codex_home"]) / CFG.get("catalog_file", "cc-switch-model-catalog.json")
    if not cat.exists():
        return None, None
    return cat, catalog_slugs(cat.read_text(encoding="utf-8", errors="replace"))


def diff_text(a, b, a_name, b_name):
    if not a and not b:
        return "（两边均为空）"
    lines = list(difflib.unified_diff(a, b, fromfile=a_name, tofile=b_name, lineterm=""))
    if not lines:
        return "（无差异）"
    return "\n".join(lines)


# --------------------------------------------------------------------------
# SSH transports
# --------------------------------------------------------------------------
def _base_ssh_args(target, extra=None):
    args = [ssh_bin(), "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "NumberOfPasswordPrompts=0",
            "-o", "ClearAllForwardings=yes"]
    if target.get("port"):
        args += ["-p", str(target["port"])]
    if target.get("key_path"):
        args += ["-i", str(target["key_path"]), "-o", "IdentitiesOnly=yes"]
    if extra:
        args += extra
    args += ["%s@%s" % (target["username"], target["host"])]
    return args


class SystemSSHBackend:
    """Zero-dependency backend: uses the system OpenSSH client (ssh/scp).
    Good for key-based auth (or ssh-config / agent)."""

    name = "system-ssh"

    def __init__(self, target):
        self.target = target

    def connect(self):
        rc, out, err = self.exec("echo CONNECT_OK", timeout=25)
        if rc == 0 and "CONNECT_OK" in (out + err):
            return None
        return (err or out or "ssh 连接失败").strip()[:300]

    def exec(self, script, timeout=120):
        b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
        cmd = _base_ssh_args(self.target) + [f"echo {b64} | base64 -d | bash -l -s"]
        return run(cmd, timeout=timeout)

    def put(self, local, remote, timeout=300):
        cmd = [scp_bin(), "-o", "ConnectTimeout=10",
               "-o", "StrictHostKeyChecking=accept-new",
               "-o", "NumberOfPasswordPrompts=0",
               "-o", "ClearAllForwardings=yes"]
        if self.target.get("port"):
            cmd += ["-P", str(self.target["port"])]
        if self.target.get("key_path"):
            cmd += ["-i", str(self.target["key_path"])]
        cmd += [str(local),
                "%s@%s:%s" % (self.target["username"], self.target["host"], remote)]
        return run(cmd, timeout=timeout)

    def close(self):
        pass


class ParamikoBackend:
    """Optional backend: pure-Python SSH (password auth / encrypted keys).
    Used automatically when paramiko is installed and the target needs it."""

    name = "paramiko"

    def __init__(self, target):
        self.target = target
        self._client = None

    @staticmethod
    def _load_pkey(key_path, passphrase):
        last = None
        for cls in (paramiko.Ed25519Key, paramiko.RSAKey,
                    paramiko.ECDSAKey, paramiko.DSSKey):
            try:
                return cls.from_private_key_file(str(key_path), password=passphrase or None)
            except Exception as e:
                last = e
        raise ValueError(f"无法解析私钥 {key_path}: {last}")

    def connect(self):
        try:
            t = self.target
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            kw = dict(hostname=t["host"], port=int(t.get("port") or 22),
                      username=t.get("username") or "root",
                      timeout=12, banner_timeout=15, auth_timeout=15,
                      allow_agent=True, look_for_keys=False)
            key_path = t.get("key_path") or ""
            auth = t.get("auth", "key")
            if auth == "password":
                if key_path:
                    kw["pkey"] = self._load_pkey(key_path, t.get("passphrase") or "")
                    if t.get("password"):
                        kw["password"] = t["password"]
                else:
                    kw["password"] = t.get("password") or ""
            else:
                if key_path:
                    kw["pkey"] = self._load_pkey(key_path, t.get("passphrase") or "")
                else:
                    kw["look_for_keys"] = True
            client.connect(**kw)
            self._client = client
            return None
        except Exception as e:
            return str(e)

    def exec(self, script, timeout=120):
        if self._client is None:
            return 1, "", "not connected"
        try:
            chan = self._client.get_transport().open_session()
            chan.settimeout(1.0)
            chan.exec_command("bash -l -s")
            chan.sendall(script.encode("utf-8"))
            chan.shutdown_write()
            out, err = b"", b""
            deadline = time.time() + timeout
            while time.time() < deadline:
                if chan.recv_ready():
                    out += chan.recv(65536)
                if chan.recv_stderr_ready():
                    err += chan.recv_stderr(65536)
                if chan.exit_status_ready():
                    break
                time.sleep(0.05)
            if not chan.exit_status_ready():
                try:
                    chan.close()
                except Exception:
                    pass
                return 124, out.decode("utf-8", "replace"), (err.decode("utf-8", "replace") + "\ntimeout")
            while chan.recv_ready():
                out += chan.recv(65536)
            while chan.recv_stderr_ready():
                err += chan.recv_stderr(65536)
            rc = chan.recv_exit_status()
            return rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        except Exception as e:
            return 1, "", str(e)

    def put(self, local, remote, timeout=300):
        if self._client is None:
            return 1, "", "not connected"
        try:
            sftp = self._client.open_sftp()
            try:
                sftp.put(str(local), remote)
            finally:
                sftp.close()
            return 0, "uploaded", ""
        except Exception as e:
            return 1, "", str(e)

    def close(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None


def backend_requirement(target):
    """Return an error string if the target cannot work without extra deps."""
    auth = target.get("auth", "key")
    if auth == "password" and not HAS_PARAMIKO:
        return "该目标使用「密码认证」，需要安装 paramiko：pip install paramiko（密钥认证无需安装）"
    if target.get("passphrase") and not HAS_PARAMIKO:
        return "该目标的私钥带密码短语，需要 paramiko 或先把密钥加载进 ssh-agent：pip install paramiko"
    return ""


def make_backend(target):
    err = backend_requirement(target)
    if err:
        return None, err
    if target.get("auth", "key") == "password" and HAS_PARAMIKO:
        return ParamikoBackend(target), None
    return SystemSSHBackend(target), None


def open_backend(target):
    b, err = make_backend(target)
    if err:
        return None, err
    e = b.connect()
    if e:
        try:
            b.close()
        except Exception:
            pass
        return None, e
    return b, None

# --------------------------------------------------------------------------
# remote helpers
# --------------------------------------------------------------------------
def remote_cat(b, cd, name):
    rc, out, err = b.exec(f"cat {cd}/{name} 2>/dev/null || echo __NONE__", timeout=30)
    if rc == 0 and "__NONE__" not in out:
        return out
    return None


def remote_skills_list(b, cd):
    script = (
        f"cd {cd}/skills 2>/dev/null && "
        f"find . -type f ! -path './.system/*' | sed 's|^\\./||' | sort "
        f"|| echo __NO_SKILLS__"
    )
    rc, out, err = b.exec(script, timeout=60)
    if rc != 0:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip() and ln.strip() != "__NO_SKILLS__"]


def merge_script(cd):
    pat = "|".join(re.escape(k) for k in sorted(SHARED_KEYS, key=len, reverse=True))
    return "\n".join([
        "set -u",
        f"cd {cd} || exit 1",
        "mkdir -p sync",
        f"pat='^({pat})[[:space:]]*='",
        "if [ ! -f config.server.tail.toml ]; then",
        "  if [ -f config.toml ]; then",
        "    cp -f config.toml config.toml.bak-pre-sync",
        "    grep -v -E \"$pat\" config.toml > config.server.tail.toml",
        "    echo TAIL_CREATED",
        "  else",
        "    echo NO_CONFIG",
        "  fi",
        "else",
        "  echo TAIL_EXISTS",
        "fi",
        "if [ -f config.server.tail.toml ]; then",
        "  cat sync/config.shared.toml config.server.tail.toml > config.toml",
        "  echo MERGE_OK",
        "else",
        "  echo MERGE_SKIPPED",
        "fi",
    ]) + "\n"


def restart_script(target):
    node_ov = (target.get("node_path") or "").strip()
    codex_ov = (target.get("codex_path") or "").strip()
    return "\n".join([
        "set -u",
        f"NODE_OVERRIDE='{node_ov}'",
        f"CODEX_OVERRIDE='{codex_ov}'",
        "ME=$(whoami)",
        'NODE="${NODE_OVERRIDE:-$(command -v node)}"',
        'CODEX="${CODEX_OVERRIDE:-$(command -v codex)}"',
        '[ -n "$NODE" ] || NODE="$HOME/tools/node-v22.11.0-linux-x64/bin/node"',
        '[ -n "$CODEX" ] || CODEX="$HOME/.npm-global/bin/codex"',
        'export PATH="$(dirname "$NODE"):$(dirname "$CODEX"):$PATH"',
        'echo "user=$ME"',
        'echo "node=$NODE"',
        'echo "codex=$CODEX"',
        "pkill -u \"$ME\" -f 'features.code_mode_host=true app-server' 2>/dev/null || true",
        "sleep 2",
        'cd "$HOME"',
        'nohup "$CODEX" -c features.code_mode_host=true app-server --listen unix:// >> "$HOME/.codex/app-server-control/app-server.log" 2>&1 &',
        "sleep 3",
        'if pgrep -u "$ME" -f "features.code_mode_host=true app-server" >/dev/null; then echo APP_SERVER_RUNNING; else echo APP_SERVER_FAILED; fi',
    ]) + "\n"


# --------------------------------------------------------------------------
# preview
# --------------------------------------------------------------------------
def preview_target(name, target):
    res = {"name": name, "host": target["host"], "reachable": True}
    local_shared = (SHARED_PATH.read_text(encoding="utf-8").splitlines()
                    if SHARED_PATH.exists() else [])
    res["has_local_shared"] = bool(local_shared)
    b, err = open_backend(target)
    if not b:
        res["reachable"] = False
        res["error"] = err
        res["shared_diff"] = f"（无法连接：{err}）"
        res["skills_diff"] = ""
        res["catalog_diff"] = ""
        res["skills_add"] = res["skills_del"] = 0
        res["catalog_changed"] = False
        res["catalog_add"] = res["catalog_del"] = 0
        res["tail_exists"] = False
        res["tail_lines"] = "0"
        res["provider_note"] = ""
        res["local_provider"] = local_provider()
        return res
    try:
        cd = target["codex_dir"]
        remote_shared = remote_cat(b, cd, "sync/config.shared.toml") or []
        if isinstance(remote_shared, str):
            remote_shared = remote_shared.splitlines()
        res["shared_diff"] = diff_text(local_shared, remote_shared,
                                       "本地 config.shared.toml",
                                       f"{name}:config.shared.toml")
        lf = local_skills_list()
        rf = sorted(set(remote_skills_list(b, cd)))
        res["skills_diff"] = diff_text(lf, rf,
                                       f"本地 skills ({len(lf)} 文件)",
                                       f"{name} skills ({len(rf)} 文件)")
        res["skills_add"] = len(set(lf) - set(rf))
        res["skills_del"] = len(set(rf) - set(lf))
        # model catalog
        lc_path, lc = local_catalog_slugs()
        rctext = remote_cat(b, cd, CFG.get("catalog_file", "cc-switch-model-catalog.json"))
        rc_slugs = catalog_slugs(rctext) if rctext else None
        if lc is None:
            res["catalog_changed"] = False
            res["catalog_add"] = res["catalog_del"] = 0
            res["catalog_diff"] = "本地模型目录 JSON 不存在或解析失败" if not lc_path else "本地模型目录 JSON 解析失败"
        elif rc_slugs is None:
            res["catalog_changed"] = True
            res["catalog_add"] = len(lc)
            res["catalog_del"] = 0
            res["catalog_diff"] = "（服务器上还没有模型目录，将整体推送）"
        else:
            added = [k for k in sorted(lc) if k not in rc_slugs]
            removed = [k for k in sorted(rc_slugs) if k not in lc]
            res["catalog_changed"] = bool(added or removed)
            res["catalog_add"] = len(added)
            res["catalog_del"] = len(removed)
            res["catalog_diff"] = ("\n".join(
                [f"+ {k} ({lc[k]})" for k in added] +
                [f"- {k} ({rc_slugs[k]})" for k in removed]
            ) or "（无差异：本地与服务器模型目录一致）")
        prov = local_provider()
        res["local_provider"] = prov or ""
        res["provider_note"] = (f"服务器保留各自的 provider（本地 {prov} 不会同步过去）"
                                if prov else "未检测到本地 provider")
        rc, out, err = b.exec(f"test -f {cd}/config.server.tail.toml && echo YES || echo NO", timeout=30)
        res["tail_exists"] = "YES" in out
        if res["tail_exists"]:
            rc, out, err = b.exec(f"wc -l < {cd}/config.server.tail.toml", timeout=30)
            res["tail_lines"] = out.strip()
        else:
            res["tail_lines"] = "0（首次同步时自动生成）"
        res["transport"] = b.name
    finally:
        b.close()
    return res


# --------------------------------------------------------------------------
# sync
# --------------------------------------------------------------------------
def sync_skills(b, target_name, cd):
    src = Path(CFG["codex_home"]) / "skills"
    if not src.exists():
        log("warn", "本地 skills 目录不存在，跳过 skills 同步", target_name)
        return True
    def _filter(ti):
        parts = ti.name.split("/")
        if ".system" in parts:
            return None
        return ti
    try:
        if TAR_PATH.exists():
            TAR_PATH.unlink()
        with tarfile.open(str(TAR_PATH), "w") as tf:
            tf.add(str(src), arcname="skills", filter=_filter)
    except Exception as e:
        log("err", f"本地 skills 打包失败: {e}", target_name)
        return False
    r, o, e = b.put(str(TAR_PATH), f"{cd}/sync/skills.tar", timeout=600)
    if r != 0:
        log("err", f"skills 上传失败: {e.strip()}", target_name)
        return False
    if CFG.get("mirror_skills", True):
        script = "\n".join([
            "set -u",
            f"cd {cd} || exit 1",
            "mkdir -p sync",
            "[ -d skills/.system ] && mv skills/.system sync/.system || true",
            "rm -rf skills",
            "mkdir -p skills",
            "[ -d sync/.system ] && mv sync/.system skills/.system || true",
            "tar -xf sync/skills.tar -C skills",
            "rm -f sync/skills.tar",
            "echo SKILLS_OK",
        ]) + "\n"
    else:
        script = "\n".join([
            "set -u",
            f"cd {cd} || exit 1",
            "mkdir -p skills sync",
            "tar -xf sync/skills.tar -C skills",
            "rm -f sync/skills.tar",
            "echo SKILLS_OK",
        ]) + "\n"
    rc, out, err = b.exec(script, timeout=180)
    if rc == 0 and "SKILLS_OK" in (out + err):
        log("ok", "skills 同步完成（已排除 .system）" +
            ("，远端多余文件已镜像删除" if CFG.get("mirror_skills", True) else ""),
            target_name)
        return True
    log("err", f"远端解包失败: {(err or out).strip()}", target_name)
    return False


def sync_catalog(b, target_name, cd):
    cat = Path(CFG["codex_home"]) / CFG.get("catalog_file", "cc-switch-model-catalog.json")
    if not cat.exists():
        log("warn", f"本地未找到 {cat.name}，跳过模型目录同步", target_name)
        return True
    old_text = remote_cat(b, cd, CFG.get("catalog_file", "cc-switch-model-catalog.json"))
    r, o, e = b.put(str(cat), f"{cd}/{cat.name}", timeout=180)
    if r != 0:
        log("err", f"模型目录推送失败: {e.strip()}", target_name)
        return False
    msg = f"模型目录已推送 ({cat.name})"
    if old_text:
        old = catalog_slugs(old_text)
        new = catalog_slugs(cat.read_text(encoding="utf-8", errors="replace"))
        if old is not None and new is not None:
            added = [k for k in sorted(new) if k not in old]
            removed = [k for k in sorted(old) if k not in new]
            if added or removed:
                parts = []
                if added:
                    parts.append("新增: " + ", ".join(added))
                if removed:
                    parts.append("移除: " + ", ".join(removed))
                msg += "（" + "；".join(parts) + "）"
    log("ok", msg, target_name)
    return True


def sync_target(name, target):
    ok = True
    log("info", f"===== 开始同步 {name} ({target['host']}) =====", name)
    b, err = open_backend(target)
    if not b:
        log("err", f"连接失败: {err}", name)
        return False
    try:
        cd = target["codex_dir"]
        rc, out, err = b.exec(f"mkdir -p {cd}/sync", timeout=30)
        if rc != 0:
            log("err", f"无法创建远端目录: {(err or out).strip()}", name)
            return False
        if CFG.get("sync_skills", True) and not sync_skills(b, name, cd):
            ok = False
        if CFG.get("sync_catalog", True) and not sync_catalog(b, name, cd):
            ok = False
        if SHARED_PATH.exists():
            r, o, e = b.put(str(SHARED_PATH), f"{cd}/sync/config.shared.toml", timeout=180)
            if r != 0:
                log("err", f"共享配置推送失败: {e.strip()}", name)
                ok = False
            else:
                log("ok", "共享配置已推送", name)
        else:
            log("err", "本地 config.shared.toml 不存在，请先点「① 提取共享配置」", name)
            return False
        rc, out, err = b.exec(merge_script(cd), timeout=60)
        txt = (out + err).strip()
        if "TAIL_CREATED" in txt:
            log("ok", "已生成 config.server.tail.toml（保留服务器自己的 provider；原配置备份 .bak-pre-sync）", name)
        elif "TAIL_EXISTS" in txt:
            log("info", "config.server.tail.toml 已存在（含服务器自己的 provider）", name)
        elif "NO_CONFIG" in txt:
            log("warn", "服务器没有 config.toml，跳过合并（请先在服务器上手动配置一次）", name)
        else:
            log("err", f"初始化 tail 失败: {txt}", name)
        if "MERGE_OK" in txt:
            log("ok", "config.toml 已合并（共享头只含可移植键 + 本机尾含 provider）", name)
        elif "MERGE_SKIPPED" in txt:
            log("warn", "config.toml 合并已跳过（无 tail）", name)
        else:
            log("err", f"config.toml 合并失败: {txt}", name)
            ok = False
    finally:
        b.close()
    STATE["last_sync"][name] = datetime.now().isoformat(timespec="seconds")
    save_state(STATE)
    log("info", f"===== {name} 同步{'完成' if ok else '存在错误'} =====", name)
    return ok


# --------------------------------------------------------------------------
# test / restart
# --------------------------------------------------------------------------
def test_target(name, target):
    b, err = open_backend(target)
    ok = bool(b)
    detail = ""
    if b:
        try:
            cd = target["codex_dir"]
            rc, out, err = b.exec(
                "echo SSH_OK; "
                f"if [ -d {cd} ]; then echo CODEX_DIR_OK; else echo CODEX_DIR_MISSING; fi; "
                f"if [ -f {cd}/config.toml ]; then echo HAS_CONFIG; else echo NO_CONFIG; fi; "
                f"if [ -d {cd}/skills ]; then echo HAS_SKILLS; else echo NO_SKILLS; fi",
                timeout=30)
            txt = (out + err).strip()
            detail = txt
            ok = "SSH_OK" in txt
        finally:
            b.close()
    else:
        detail = err
    STATE["last_test"][name] = {"ok": ok,
                                "time": datetime.now().isoformat(timespec="seconds"),
                                "detail": detail[:300]}
    save_state(STATE)
    if ok:
        log("ok", f"{name} ({target['host']}) 连接成功" +
            ("；codex_dir 存在" if "CODEX_DIR_OK" in detail else "；⚠ codex_dir 不存在"),
            name)
        if "HAS_CONFIG" not in detail:
            log("warn", f"{name} 的 codex_dir 下没有 config.toml（首次同步会跳过合并）", name)
    else:
        log("err", f"{name} ({target['host']}) 连接失败: {detail}", name)
    return ok


def restart_target(name, target):
    b, err = open_backend(target)
    if not b:
        log("err", f"连接失败: {err}", name)
        return False
    log("info", f"开始重启 {name} ({target['host']}) 的 Codex app-server（会短暂断开该服务器上的远程会话）", name)
    try:
        script = restart_script(target)
        custom = (target.get("restart_script") or "").strip()
        if custom:
            script = custom + "\necho APP_SERVER_RUNNING"
        rc, out, err = b.exec(script, timeout=90)
        txt = (out + err).strip()
        ok = "APP_SERVER_RUNNING" in txt
        for ln in txt.splitlines():
            s = ln.strip()
            if s:
                log("info", f"[{name}] {s}", name)
        STATE.setdefault("last_restart", {})[name] = datetime.now().isoformat(timespec="seconds")
        save_state(STATE)
        if ok:
            log("ok", f"{name} app-server 已重启（模型列表已重新加载）", name)
        else:
            log("err", f"{name} app-server 重启后未检测到进程，请检查服务器日志", name)
        return ok
    finally:
        b.close()


def run_sync(names):
    try:
        for name in names:
            t = target_by_name(name)
            if not t:
                log("err", f"未知目标: {name}")
                continue
            try:
                sync_target(name, t)
            except Exception as e:
                log("err", f"同步 {name} 异常: {e}")
        log("info", "全部同步任务结束")
    finally:
        with _LOCK:
            _RUNNING["sync"] = False


def run_test(names):
    try:
        for name in names:
            t = target_by_name(name)
            if t:
                try:
                    test_target(name, t)
                except Exception as e:
                    log("err", f"测试 {name} 异常: {e}")
        log("info", "连接测试结束")
    finally:
        with _LOCK:
            _RUNNING["test"] = False


def run_restart(names):
    try:
        for name in names:
            t = target_by_name(name)
            if not t:
                log("err", f"未知目标: {name}")
                continue
            try:
                restart_target(name, t)
            except Exception as e:
                log("err", f"重启 {name} 异常: {e}")
        log("info", "重启任务结束")
    finally:
        with _LOCK:
            _RUNNING["restart"] = False

# --------------------------------------------------------------------------
# config / targets helpers
# --------------------------------------------------------------------------
def target_by_name(name):
    for t in CFG.get("targets", []):
        if t.get("name") == name:
            return t
    return None


def mask_target(t):
    d = {k: v for k, v in t.items() if k not in ("password", "passphrase")}
    d["has_password"] = bool(t.get("password"))
    d["has_passphrase"] = bool(t.get("passphrase"))
    d["needs_paramiko"] = bool(backend_requirement(t))
    return d


def status_payload():
    cfgp = Path(CFG["codex_home"]) / "config.toml"
    mtime = None
    if cfgp.exists():
        mtime = datetime.fromtimestamp(cfgp.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    shared = (SHARED_PATH.read_text(encoding="utf-8", errors="replace")
              if SHARED_PATH.exists() else "")
    targets = []
    for t in CFG.get("targets", []):
        st = STATE.get("last_test", {}).get(t.get("name"), {})
        targets.append({
            "name": t.get("name"), "host": t.get("host"),
            "port": t.get("port", 22), "username": t.get("username"),
            "auth": t.get("auth", "key"), "codex_dir": t.get("codex_dir"),
            "key_path": t.get("key_path", ""),
            "node_path": t.get("node_path", ""),
            "codex_path": t.get("codex_path", ""),
            "restart_script": t.get("restart_script", ""),
            "has_password": bool(t.get("password")),
            "has_passphrase": bool(t.get("passphrase")),
            "needs_paramiko": bool(backend_requirement(t)),
            "last_sync": STATE.get("last_sync", {}).get(t.get("name")),
            "last_restart": STATE.get("last_restart", {}).get(t.get("name")),
            "last_test": st.get("time"), "reachable": st.get("ok"),
        })
    return {
        "ok": True,
        "port": CFG.get("port", 8765),
        "codex_home": CFG.get("codex_home", ""),
        "catalog_file": CFG.get("catalog_file", "cc-switch-model-catalog.json"),
        "sync_model": CFG.get("sync_model", True),
        "sync_skills": CFG.get("sync_skills", True),
        "sync_catalog": CFG.get("sync_catalog", True),
        "mirror_skills": CFG.get("mirror_skills", True),
        "shared_keys": sorted(CFG.get("shared_keys", DEFAULT_SHARED_KEYS)),
        "paramiko_available": HAS_PARAMIKO,
        "ssh_bin": ssh_bin(),
        "local_config_mtime": mtime,
        "local_provider": local_provider(),
        "shared_exists": SHARED_PATH.exists(),
        "shared": shared,
        "targets": targets,
        "running": dict(_RUNNING),
        "server_time": now_str(),
    }


def detect_candidates():
    home = Path.home()
    cands = [str(home / ".codex")]
    if os.name == "nt":
        extra = [str(Path(os.environ.get("USERPROFILE", "")) / ".codex"),
                 str(Path(os.environ.get("CODEX_HOME", ""))) if os.environ.get("CODEX_HOME") else ""]
        for c in extra:
            if c and c not in cands:
                cands.append(c)
    return [c for c in cands if c], [c for c in cands if c and Path(c).exists()]


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "syncodex/1.0"

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, (BASE / "index.html").read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/status":
            self._json(200, status_payload())
            return
        if path == "/api/log":
            since = 0
            try:
                since = int(self.path.split("since=")[1].split("&")[0])
            except Exception:
                pass
            lines, seq = get_log(since)
            self._json(200, {"lines": lines, "next": seq})
            return
        if path == "/api/detect":
            cands, existing = detect_candidates()
            self._json(200, {"candidates": cands, "existing": existing})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._read_json()
        if path == "/api/extract":
            ok, content, msg, note = extract_shared()
            self._json(200, {"ok": ok, "shared": content, "message": msg, "note": note})
            return
        if path == "/api/preview":
            targets = body.get("targets") or []
            results = {}
            for name in targets:
                t = target_by_name(name)
                if t:
                    results[name] = preview_target(name, t)
            self._json(200, {"results": results})
            return
        if path == "/api/sync":
            targets = body.get("targets") or []
            if not targets:
                self._json(400, {"error": "no targets"})
                return
            with _LOCK:
                if _RUNNING["sync"] or _RUNNING["restart"]:
                    self._json(409, {"error": "已有同步/重启任务在运行"})
                    return
                _RUNNING["sync"] = True
            threading.Thread(target=run_sync, args=(targets,), daemon=True).start()
            self._json(200, {"ok": True, "started": True})
            return
        if path == "/api/test":
            targets = body.get("targets") or []
            with _LOCK:
                if _RUNNING["sync"] or _RUNNING["restart"]:
                    self._json(409, {"error": "已有任务在运行，请稍候"})
                    return
                _RUNNING["test"] = True
            threading.Thread(target=run_test, args=(targets,), daemon=True).start()
            self._json(200, {"ok": True, "started": True})
            return
        if path == "/api/restart":
            targets = body.get("targets") or []
            if not targets:
                self._json(400, {"error": "no targets"})
                return
            with _LOCK:
                if _RUNNING["sync"] or _RUNNING["restart"]:
                    self._json(409, {"error": "已有同步/重启任务在运行"})
                    return
                _RUNNING["restart"] = True
            threading.Thread(target=run_restart, args=(targets,), daemon=True).start()
            self._json(200, {"ok": True, "started": True})
            return
        if path == "/api/targets":
            t = body.get("target", {}) or {}
            name = str(t.get("name") or "").strip()
            if not name:
                self._json(400, {"error": "缺少服务器名称"})
                return
            if not str(t.get("host") or "").strip():
                self._json(400, {"error": "缺少主机地址"})
                return
            existing = target_by_name(name)
            merged = dict(existing) if existing else {}
            for k, v in t.items():
                if k in ("password", "passphrase") and v in (None, ""):
                    continue  # blank = keep current secret
                merged[k] = v
            merged["name"] = name
            merged["host"] = str(t.get("host")).strip()
            merged["username"] = str(t.get("username") or "root").strip()
            merged["port"] = int(t.get("port") or 22)
            merged["auth"] = t.get("auth", "key")
            merged["codex_dir"] = str(t.get("codex_dir") or "").strip() or "/root/.codex"
            merged.setdefault("key_path", "")
            merged.setdefault("node_path", "")
            merged.setdefault("codex_path", "")
            merged.setdefault("restart_script", "")
            CFG["targets"] = [x for x in CFG.get("targets", []) if x.get("name") != name]
            CFG["targets"].append(merged)
            save_config()
            log("info", f"已保存服务器「{name}」({merged['host']})")
            self._json(200, {"ok": True, "target": mask_target(merged)})
            return
        if path == "/api/targets/delete":
            name = str(body.get("name") or "").strip()
            before = len(CFG.get("targets", []))
            CFG["targets"] = [x for x in CFG.get("targets", []) if x.get("name") != name]
            save_config()
            if len(CFG["targets"]) == before:
                self._json(404, {"error": "目标不存在"})
                return
            log("info", f"已删除服务器「{name}」")
            self._json(200, {"ok": True})
            return
        if path == "/api/settings":
            for k in ("codex_home", "catalog_file"):
                if body.get(k):
                    CFG[k] = str(body[k]).strip()
            for k in ("sync_model", "sync_skills", "sync_catalog", "mirror_skills"):
                if k in body:
                    CFG[k] = bool(body[k])
            if isinstance(body.get("shared_keys"), list) and body["shared_keys"]:
                CFG["shared_keys"] = [str(x).strip() for x in body["shared_keys"] if str(x).strip()]
            reload_shared_keys()
            save_config()
            log("info", "设置已保存")
            self._json(200, {"ok": True, "status": status_payload()})
            return
        self._json(404, {"error": "not found"})


def main():
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    port = int(CFG.get("port", 8765))
    import sys
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except Exception:
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    log("info", f"服务已启动: http://127.0.0.1:{port}（仅本机可访问）")
    print(f"syncodex 已启动: http://127.0.0.1:{port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
