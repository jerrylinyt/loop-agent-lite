#!/usr/bin/env python3
"""engine.ralph — Ralph runner 監督層(supervisor)。

Dashboard 以 `python -m engine.ralph …` spawn 這支程式來驅動公司內既有的 ralph.sh。
與 engine.loop 的協調式迴圈不同:ralph 自己就是完整迴圈引擎(挑 story、跑 agent、
commit、更新 prd/progress),我們無法改它、也不該把 loop 的共識/validate/防竄改機制
套上去(那對 ralph 是誤傷:ralph 的正常行為就是 agent 自己改 prd、自己 commit)。

因此本監督層「不指揮、只看著」:
- spawn `sh ralph.sh <iters> <tool> <model>`(引數排列可設定),cwd=target repo,獨立 process group。
- stdout/stderr 逐行 → workspace 共用 console.log(🤖 前綴)與 logs/ralph-run.log。
- 每隔數秒輪詢 prd(.json/.md)、progress.txt、git HEAD,把進度投影進 state.json 的 `ralph` 區塊。
- ralph 退出後判定 completed / iterations_exhausted / failed;被 Dashboard 停止則標記 interrupted。

真相層全部在 ralph 自己的檔案(prd/progress/git);本監督層對這些檔案唯讀,只寫 workspace 內的
state.json / console.log / logs。沿用 engine.loop 的所有 workspace 安全原語(O_NOFOLLOW、
單 writer 鎖、原子寫、名稱規則),但不啟用 loop 的 reset/tamper/consensus 邏輯。
"""

import argparse
import atexit
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from engine import loop as loop_mod
from engine.paths import expose_project_package

# ===== 常數 =====
POLL_INTERVAL_SEC = 3.0            # state 投影輪詢間隔(與 SSE 節流同級)
STALL_AFTER_SEC = 20 * 60          # 無 stdout/檔案/HEAD 變化達此秒數 → 標記 stalled(只警示)
STORY_DISPLAY_CAP = 500            # state.json 內最多保留的 story 筆數(統計仍取全部)
PROGRESS_MAX_BYTES = 2 * 1024 * 1024   # progress.txt 一次投影尾段上限(供 tail 讀取自有限制)
RALPH_RUN_LOG = "ralph-run.log"
SENTINEL_RE = re.compile(r"<promise>\s*COMPLETE\s*</promise>", re.IGNORECASE)
ITERATION_RE = re.compile(r"Ralph\s+Iteration\s+(\d+)\s+of\s+(\d+)", re.IGNORECASE)
TOOL_RE = re.compile(r"[A-Za-z0-9._-]+")
MODEL_MAX_CHARS = 200
ITERATIONS_MAX = 1_000_000
SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

ARGS_STYLES = {
    # 公司版:位置參數 `<iters> <tool> <model>`
    "positional": ["{iterations}", "{tool}", "{model}"],
    # snarktank 原版:`--tool <tool> <iters>`(e2e 用真正 clone 驅動)
    "snarktank": ["--tool", "{tool}", "{iterations}"],
}
DEFAULT_ARGS_STYLE = "positional"


class PrdParseError(ValueError):
    """PRD 內容無法解析成合法 story 清單。"""


# ===== 純函式(供單元測試) =====
def _clean_story(raw, index):
    """把單筆 story 正規化成投影用的最小 dict;缺欄位以保守預設補齊。"""
    if not isinstance(raw, dict):
        return None
    passes = bool(raw.get("passes"))
    story_id = raw.get("id")
    story_id = str(story_id) if story_id not in (None, "") else f"US-{index + 1:03d}"
    title = raw.get("title") or raw.get("name") or raw.get("description") or story_id
    priority = raw.get("priority")
    if isinstance(priority, bool) or not isinstance(priority, int):
        priority = None
    story = {"id": story_id[:200], "title": str(title)[:400], "passes": passes,
             "priority": priority}
    if isinstance(raw.get("description"), str):
        story["description"] = raw["description"][:2000]
    acceptance = raw.get("acceptanceCriteria")
    if isinstance(acceptance, list):
        story["acceptanceCriteria"] = [str(item)[:500] for item in acceptance[:50]
                                       if isinstance(item, (str, int, float))]
    return story


def _summarize_stories(stories):
    """回傳 (顯示用截斷清單, 總數, 完成數);統計用全部、清單只截斷顯示。"""
    total = len(stories)
    done = sum(1 for story in stories if story.get("passes"))
    return stories[:STORY_DISPLAY_CAP], total, done


def parse_prd_json(text: str) -> dict:
    """解析 snarktank 式 prd.json(top-level dict.userStories 或純 story 陣列)。"""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise PrdParseError(f"prd.json 不是合法 JSON:{e}") from e
    if isinstance(data, dict):
        raw_stories = data.get("userStories")
        if raw_stories is None and isinstance(data.get("stories"), list):
            raw_stories = data["stories"]
        project = data.get("project")
        branch_name = data.get("branchName") or data.get("branch_name")
    elif isinstance(data, list):
        raw_stories = data
        project = branch_name = None
    else:
        raise PrdParseError("prd.json 頂層必須是 object 或 story 陣列")
    if not isinstance(raw_stories, list):
        raise PrdParseError("prd.json 缺少 userStories 陣列")
    stories = [story for story in (_clean_story(item, index)
                                   for index, item in enumerate(raw_stories))
               if story is not None]
    display, total, done = _summarize_stories(stories)
    return {"prd_format": "json",
            "project": str(project) if project else "",
            "branch_name": str(branch_name) if branch_name else "",
            "stories": display, "stories_total": total, "stories_done": done}


_MD_CHECKBOX_RE = re.compile(r"^\s*[-*]\s*\[(?P<mark>[ xX])\]\s*(?P<text>.+?)\s*$")


def parse_prd_md(text: str) -> dict:
    """解析 prd.md 的 checkbox 清單:`- [ ]`=未完成、`- [x]`=完成。"""
    stories = []
    project = ""
    branch_name = ""
    for line in text.splitlines():
        if not project:
            heading = re.match(r"^\s*#\s+(.+?)\s*$", line)
            if heading:
                project = heading.group(1)[:200]
                continue
        branch = re.match(r"^\s*(?:branchName|branch)\s*[:：]\s*(\S+)", line, re.IGNORECASE)
        if branch and not branch_name:
            branch_name = branch.group(1)[:200]
            continue
        match = _MD_CHECKBOX_RE.match(line)
        if not match:
            continue
        index = len(stories)
        stories.append({
            "id": f"US-{index + 1:03d}",
            "title": match.group("text")[:400],
            "passes": match.group("mark").lower() == "x",
            "priority": index + 1,
        })
    if not stories:
        raise PrdParseError("prd.md 找不到任何 `- [ ]` / `- [x]` checkbox story")
    display, total, done = _summarize_stories(stories)
    return {"prd_format": "md", "project": project, "branch_name": branch_name,
            "stories": display, "stories_total": total, "stories_done": done}


def _prd_format_for(prd_path: str) -> str:
    """由副檔名決定 PRD 格式;非 .md 一律當 json(snarktank 預設)。"""
    return "md" if str(prd_path).lower().endswith(".md") else "json"


def load_prd(ralph_dir: Path, prd_path: str) -> dict:
    """安全讀取 ralph_dir 下的 PRD 並投影;任何錯誤回傳帶 prd_error 的空投影而非丟例外。"""
    fmt = _prd_format_for(prd_path)
    empty = {"prd_format": fmt, "prd_path": str(prd_path), "project": "", "branch_name": "",
             "stories": [], "stories_total": 0, "stories_done": 0, "prd_error": None}
    try:
        resolved = _safe_prd_path(ralph_dir, prd_path)
    except (OSError, ValueError) as e:
        return {**empty, "prd_error": f"PRD 路徑不安全:{e}"}
    try:
        text = loop_mod.read_regular_text(resolved, "prd")
    except FileNotFoundError:
        return {**empty, "prd_error": f"找不到 PRD:{prd_path}"}
    except (OSError, ValueError, UnicodeDecodeError) as e:
        return {**empty, "prd_error": f"PRD 無法讀取:{e}"}
    try:
        parsed = parse_prd_md(text) if fmt == "md" else parse_prd_json(text)
    except PrdParseError as e:
        return {**empty, "prd_error": str(e)}
    parsed["prd_path"] = str(prd_path)
    parsed["prd_error"] = None
    return parsed


def _safe_prd_path(ralph_dir: Path, prd_path: str) -> Path:
    """把 prd_path 限制在 ralph_dir 內、逐段拒絕 symlink 與 traversal 後回傳實體路徑。"""
    if not isinstance(prd_path, str) or not prd_path:
        raise ValueError("PRD 路徑不可為空")
    candidate = Path(prd_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"PRD 路徑 {prd_path!r} 必須是 ralph 目錄內的相對路徑")
    root = Path(ralph_dir).resolve()
    current = root
    for part in candidate.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"PRD 路徑 {prd_path!r} 不可經由 symbolic link")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as e:
        raise ValueError(f"PRD 路徑 {prd_path!r} 不得逸出 ralph 目錄") from e
    return root / candidate


def build_ralph_argv(base_cmd, args_template, *, iterations, tool, model, prd_path="") -> list:
    """組出直接 exec(不經 shell)的 argv;placeholder 以驗證過的純量取代,空值 token 丟棄。"""
    try:
        base = shlex.split(str(base_cmd))
    except ValueError as e:
        raise ValueError(f"ralph 命令格式錯誤:{e}") from e
    if not base:
        raise ValueError("ralph 命令不可為空")
    values = {"{iterations}": str(int(iterations)), "{tool}": str(tool),
              "{model}": str(model or ""), "{prd}": str(prd_path or "")}
    argv = list(base)
    for token in args_template or []:
        token = str(token)
        if token in values:                       # 整個 token 是 placeholder
            replaced = values[token]
            if replaced == "":                    # 空值(例:未給 model)→ 丟棄整個 token
                continue
            argv.append(replaced)
            continue
        for placeholder, replaced in values.items():   # placeholder 內嵌在字串裡
            token = token.replace(placeholder, replaced)
        argv.append(token)
    return argv


def resolve_args_template(style, explicit=None) -> list:
    """由 args_style 取得 template;style=custom 時採 explicit 清單。"""
    if style == "custom":
        if not isinstance(explicit, list) or not all(isinstance(item, str) for item in explicit):
            raise ValueError("args_style=custom 時 args_template 必須是字串陣列")
        return explicit
    if style not in ARGS_STYLES:
        raise ValueError(f"args_style 只能是 {sorted(ARGS_STYLES)} 或 custom")
    return list(ARGS_STYLES[style])


def _git_out(repo, *args):
    """讀取 git 純量輸出;失敗回空字串,不讓監督層因 git 噪音中斷。"""
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def project_ralph_block(repo, ralph_dir, prd_path, base_sha, *, iteration, max_iterations,
                        sentinel_complete, stalled, exit_code, exit_reason) -> dict:
    """組出 state.json 的 `ralph` 區塊:PRD 投影 + git 進度 + 迴圈狀態。"""
    prd = load_prd(Path(ralph_dir), prd_path)
    head_sha = _git_out(repo, "rev-parse", "HEAD")
    commit_count = 0
    last_commit = ""
    if base_sha and head_sha and base_sha != head_sha:
        count_text = _git_out(repo, "rev-list", "--count", f"{base_sha}..HEAD")
        commit_count = int(count_text) if count_text.isdigit() else 0
        last_commit = _git_out(repo, "log", "-1", "--pretty=%s")
    elif head_sha:
        last_commit = _git_out(repo, "log", "-1", "--pretty=%s")
    progress_bytes = _progress_size(Path(ralph_dir))
    return {
        "prd_format": prd.get("prd_format"),
        "prd_path": str(prd_path),
        "project": prd.get("project", ""),
        "branch_name": prd.get("branch_name", ""),
        "stories": prd.get("stories", []),
        "stories_total": prd.get("stories_total", 0),
        "stories_done": prd.get("stories_done", 0),
        "iteration": int(iteration),
        "max_iterations": int(max_iterations),
        "base_sha": base_sha or None,
        "head_sha": head_sha or None,
        "commit_count": commit_count,
        "last_commit": last_commit,
        "progress_bytes": progress_bytes,
        "sentinel_complete": bool(sentinel_complete),
        "stalled": bool(stalled),
        "exit_code": exit_code,
        "exit_reason": exit_reason,
        "prd_error": prd.get("prd_error"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _progress_size(ralph_dir: Path) -> int:
    """回傳 progress.txt 位元組數;不存在或不安全時回 0。"""
    try:
        path = _safe_prd_path(ralph_dir, "progress.txt")
        info = path.lstat()
    except (OSError, ValueError):
        return 0
    return info.st_size if stat.S_ISREG(info.st_mode) else 0


# 對外公開別名（dashboard 引用）：私有實作保留內部語意，公開名給跨模組使用。
def default_ralph_dir(ralph_cmd, repo):
    """未指定 ralph_dir 時:優先取 ralph.sh 所在目錄,否則落回 repo。"""
    try:
        base = shlex.split(str(ralph_cmd))
    except ValueError:
        base = []
    for token in base:
        if token.endswith(".sh"):
            script = Path(token).expanduser()
            if script.exists():
                return script.resolve().parent
    return Path(repo).expanduser().resolve()


safe_prd_path = _safe_prd_path


# ===== usage-limit 偵測(heuristic;pattern 可由 config 擴充) =====
# 判斷規則(見 docs/ralph-mode-design usage-limit 章節):某輪「命中 pattern」且「該輪無任何進展
# (HEAD/story 完成數/progress 位元組皆未動)」才算 limit iteration。tier-1 命中 1 次即確認;
# tier-2 需連續 2 輪。這道 no-progress gate 專門避開「agent 正在寫 rate-limit 相關程式碼且有
# commit」的誤判——有進展的輪即使命中字樣也不算 limit。
TIER1_PATTERNS = [
    r"usage limit reached\|\s*\d{9,13}",
    r'"type"\s*:\s*"rate_limit_error"|rate_limit_error|rate_limit_exceeded',
    r"exceeded your current quota|insufficient_quota",
    r"credit balance is too low",
    r"\b(?:5-hour|weekly|session) limit reached\b",
    r"usage limit.{0,60}\b(?:resets?|try again)\b",
    r"\b(?:insufficient|out of)\s+(?:free\s+)?credits?\b",
    r"overloaded_error",
]
TIER2_PATTERNS = [
    r"\brate.?limit",
    r"\btoo many requests\b",
    r"\b429\b",
    r"\b(?:usage|quota|credits?)\b.{0,40}\b(?:limit|exceeded|exhausted|reached|too low)\b",
]

MIN_WAIT_SEC = 60
MAX_WAIT_SEC = 6 * 3600
BACKOFF_BASE_SEC = 60
BACKOFF_CAP_SEC = 3600
MAX_TOTAL_WAIT_SEC = 24 * 3600
SETTLE_DELAY_SEC = 15
# ralph 的 kill 寬限必須明顯短於 Dashboard Job.stop 的 8 秒 SIGKILL，否則 Dashboard 會先
# SIGKILL 監督層、留下仍在 commit 的孤兒 ralph（監督層與 ralph 各自獨立 session）。
KILL_GRACE_SEC = 5
DEFAULT_AUTO_RESTART_MAX = 6


def compile_limit_patterns(extra=None):
    """回傳 [(compiled, tier, source)];extra 為 config 追加的 tier-2 regex(壞 regex 略過)。"""
    compiled = [(re.compile(pat, re.IGNORECASE), 1, "builtin") for pat in TIER1_PATTERNS]
    compiled += [(re.compile(pat, re.IGNORECASE), 2, "builtin") for pat in TIER2_PATTERNS]
    for pat in extra or []:
        try:
            compiled.append((re.compile(str(pat), re.IGNORECASE), 2, "custom"))
        except re.error:
            continue
    return compiled


_RESET_EPOCH_RE = re.compile(r"usage limit reached\|\s*(\d{9,13})", re.IGNORECASE)
_RESET_ISO_RE = re.compile(r"resets?\s+(?:at\s+)?(\d{4}-\d{2}-\d{2}T[\d:.+Zz-]+)", re.IGNORECASE)
_RESET_CLOCK_RE = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)
_RESET_RELATIVE_RE = re.compile(
    r"(?:try again|retry|resets?)\s*(?:in|after)?[:\s]*(\d+(?:\.\d+)?)\s*"
    r"(ms|s(?:ec(?:ond)?s?)?|m(?:in(?:ute)?s?)?|h(?:(?:ou)?rs?)?)\b", re.IGNORECASE)
_RESET_RETRY_AFTER_RE = re.compile(r"retry-after[:\s]+(\d+)\b", re.IGNORECASE)


def parse_reset_target(text: str, now: float):
    """從命中訊息解析「limit 何時解除」的 epoch 秒;無法解析回 None。優先序:epoch → ISO →
    時鐘(am/pm)→ 相對時間 → Retry-After。過去時間視為無法解析(交給退避)。"""
    match = _RESET_EPOCH_RE.search(text)
    if match:
        value = int(match.group(1))
        epoch = value / 1000 if value >= 1_000_000_000_000 else value
        return epoch if epoch > now else None
    match = _RESET_ISO_RE.search(text)
    if match:
        raw = match.group(1).replace("z", "Z")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            epoch = parsed.timestamp()
            return epoch if epoch > now else None
        except ValueError:
            pass
    match = _RESET_CLOCK_RE.search(text)
    if match:
        hour = int(match.group(1)) % 12
        minute = int(match.group(2) or 0)
        if match.group(3).lower() == "pm":
            hour += 12
        base = datetime.fromtimestamp(now)
        try:
            target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            target = None
        if target is not None:
            epoch = target.timestamp()
            if epoch <= now:
                epoch += 24 * 3600  # 下一個未來出現
            return epoch
    match = _RESET_RELATIVE_RE.search(text)
    if match:
        amount = float(match.group(1))
        unit = match.group(2).lower()
        factor = 0.001 if unit.startswith("ms") else 1 if unit.startswith("s") else \
            60 if unit.startswith("m") else 3600
        return now + amount * factor
    match = _RESET_RETRY_AFTER_RE.search(text)
    if match:
        return now + int(match.group(1))
    return None


def _env_float(name, default):
    """讀取測試用時間覆寫環境變數;非法值退回預設。"""
    try:
        return float(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return default


# ===== 監督層 =====
class RalphSupervisor:
    """ralph runner 的外圈監督:spawn → 監控(含 usage-limit 偵測)→ 等待/降級重啟 → 終態。

    ralph.sh 自成迴圈;此監督層對每個 ralph run 只 spawn/監控/投影。若偵測到 agent 用量上限,
    殺掉空轉的 ralph,再依設定「降級模型即刻重啟」或「等 reset 後重啟」,直到收斂或達安全上限。
    """

    def __init__(self, args):
        """驗證 workspace、取單 writer 鎖、備妥 console、偵測器與初始 state。"""
        self.args = args
        self.repo = Path(args.repo).expanduser().resolve()
        self.ralph_dir = Path(args.ralph_dir).expanduser().resolve()
        self.prd_path = args.prd_path
        self.ws = loop_mod.Workspace(args.name)
        loop_mod.configure_console(self.ws.dir / "console.log")
        self.run_log = self.ws.dir / "logs" / RALPH_RUN_LOG
        self.session_id = uuid.uuid4().hex
        self.base_sha = ""
        self.proc = None
        self._lock = threading.Lock()
        self._iteration = 0
        self._sentinel = False
        self._last_activity = time.monotonic()
        self._stopping = threading.Event()
        self._project_lock = threading.Lock()   # 序列化 state 寫入,避免兩執行緒交錯拆分 primary/checkpoint
        self.state = None
        # usage-limit 偵測器
        self._patterns = compile_limit_patterns(args.usage_limit_patterns) \
            if args.usage_limit_action != "off" else []
        self._limit_confirmed = threading.Event()
        self._limit_evidence = None
        self._det = None            # 目前 run 的逐輪偵測狀態
        self._tier2_streak = 0
        self._run_made_progress = False
        self._run_gen = 0           # run 世代;被殺 run 的殘留 reader thread 不得污染下一個 run
        self._force_kill_timer = None
        self._last_exit_code = None
        # 跨 run 的持久投影欄位
        self._active_model = args.model or ""
        self._restart_attempt = 0
        self._limit_block = None

    # ---- preflight ----
    def preflight(self):
        """啟動前最小檢查:repo 是 git repo、ralph 命令可解析、PRD 目錄存在。"""
        if not (self.repo / ".git").exists():
            loop_mod.fail(f"preflight:{self.repo} 不是 git repo")
        try:
            base = shlex.split(self.args.ralph_cmd)
        except ValueError as e:
            loop_mod.fail(f"preflight:ralph 命令格式錯誤:{e}")
        if not base:
            loop_mod.fail("preflight:ralph 命令不可為空")
        # base[0] 可能是 sh/bash;真正的 script 是後續參數。兩者都檢查存在性。
        executable = base[0]
        from shutil import which
        if which(executable) is None and not Path(executable).expanduser().exists():
            loop_mod.fail(f"preflight:找不到 ralph 執行檔 {executable}")
        for candidate in base[1:]:
            if candidate.endswith(".sh") and not Path(candidate).expanduser().exists():
                loop_mod.fail(f"preflight:找不到 ralph script {candidate}")
        if not self.ralph_dir.is_dir():
            loop_mod.fail(f"preflight:ralph 目錄不存在:{self.ralph_dir}")
        # 設了降級鏈卻無 {model} placeholder → 降級不會生效;大聲 fail 勝於默默無效。
        if (self.args.usage_limit_action == "downgrade" and self.args.fallback_models and
                not any("{model}" in token for token in self.args.args_template)):
            loop_mod.fail("preflight:啟用模型降級但 args_template 沒有 {model} placeholder,降級不會生效")

    # ---- state ----
    def _fresh_state(self):
        """建立 ralph runner 的初始 state;刻意不含 coordinator 收斂欄位。"""
        return {
            "runner": "ralph",
            "phase": "exec",
            "loop": {"pid": os.getpid(), "session_id": self.session_id,
                     "started_at": datetime.now().isoformat(timespec="seconds")},
            "repo_binding": str(self.repo),
            "config": {
                "runner": "ralph",
                "repo": str(self.repo),
                "ralph_cmd": self.args.ralph_cmd,
                "ralph_dir": str(self.ralph_dir),
                "iterations": int(self.args.iterations),
                "tool": self.args.tool,
                "model": self.args.model or "",
                "args_template": list(self.args.args_template),
                "prd_path": self.prd_path,
                "notify_cmd": self.args.notify_cmd or "",
                "usage_limit_action": self.args.usage_limit_action,
                "fallback_models": list(self.args.fallback_models),
                "auto_restart_max": int(self.args.auto_restart_max),
            },
            "ralph": {},
        }

    def _project(self, *, exit_code=None, exit_reason=None):
        """重算 ralph 區塊(核心 PRD/git 進度 + 持久 usage-limit 欄位)並原子落盤。"""
        with self._lock:
            iteration = self._iteration
            sentinel = self._sentinel
            last_activity = self._last_activity
        stalled = (exit_reason is None and self.proc is not None and
                   self.proc.poll() is None and
                   time.monotonic() - last_activity > STALL_AFTER_SEC)
        # _project 可能由 reader thread 與主執行緒同時呼叫;序列化「重算＋落盤」避免兩者交錯把
        # primary 與 last-good checkpoint 寫成不同版本,或舊投影覆蓋新投影。
        with self._project_lock:
            block = project_ralph_block(
                self.repo, self.ralph_dir, self.prd_path, self.base_sha,
                iteration=iteration, max_iterations=self.args.iterations,
                sentinel_complete=sentinel, stalled=stalled,
                exit_code=exit_code, exit_reason=exit_reason)
            block["active_model"] = self._active_model
            block["restart_attempt"] = self._restart_attempt
            block["usage_limit"] = self._limit_block
            self.state["ralph"] = block
            self.ws.save_state(self.state)
            return block

    # ---- usage-limit 偵測(在 reader thread 逐輪判定) ----
    def _fingerprint(self):
        """回傳 (HEAD, stories_done, progress_bytes) 作為「本輪是否有進展」的比較基準。"""
        head = _git_out(self.repo, "rev-parse", "HEAD")
        try:
            prd = load_prd(self.ralph_dir, self.prd_path)
            stories_done = prd.get("stories_done", 0)
        except Exception:  # noqa: BLE001 — 指紋計算失敗不影響輸出鏡射
            stories_done = 0
        return (head, stories_done, _progress_size(self.ralph_dir))

    def _progressed_since(self, start_fp):
        """本輪相對起點是否有實質進展:HEAD 前進、story 完成數增加或 progress 增長。"""
        head, done, size = self._fingerprint()
        return head != start_fp[0] or done > start_fp[1] or size > start_fp[2]

    def _begin_iteration(self, number):
        """在迭代 banner 邊界:先結算前一輪,再以目前指紋開啟新一輪偵測。"""
        if self._det is not None:
            self._evaluate_iteration(self._det)
        self._det = {"iter": number, "start_fp": self._fingerprint(), "matches": []}

    def _evaluate_iteration(self, det):
        """依 no-progress gate 判定該輪是否為 limit iteration,必要時確認 usage limit。"""
        if not det["matches"]:
            # 沒命中任何字樣的一輪打斷 tier-2 的「連續」性(即使該輪也無進展)。
            self._tier2_streak = 0
            if self._progressed_since(det["start_fp"]):
                self._run_made_progress = True
            return
        if self._progressed_since(det["start_fp"]):
            self._run_made_progress = True
            self._tier2_streak = 0   # 有 commit 的輪即使命中字樣也不是 limit
            return
        has_tier1 = any(match["tier"] == 1 for match in det["matches"])
        if has_tier1:
            self._confirm_limit(det)
            return
        self._tier2_streak += 1
        if self._tier2_streak >= 2:
            self._confirm_limit(det)

    def _confirm_limit(self, det):
        """記錄證據並喚醒主流程去殺 ralph、進入等待/降級。"""
        if self._limit_confirmed.is_set():
            return
        self._limit_evidence = {"iteration": det["iter"], "matches": det["matches"][-5:],
                                "at": datetime.now().isoformat(timespec="seconds")}
        self._limit_confirmed.set()
        loop_mod.log(f"⚠ 疑似 agent 用量上限(heuristic)｜iteration {det['iter']}｜"
                     f"命中 {det['matches'][-1]['pattern']}｜將收掉 ralph 並處理")

    # ---- stdout reader ----
    def _consume_output(self, proc, gen):
        """逐行鏡射 ralph stdout 到 console 與 run log,抽取迭代/完成訊號並跑 usage-limit 偵測。

        - binary 讀取 + errors="replace":一個非 UTF-8 位元組不會殺掉 reader、害 pipe 塞滿讓 ralph 永久阻塞。
        - 每行處理包 try/except:單行例外不中斷「排空 stdout」這件保命的事。
        - gen 綁定本 run:被殺 run 殘留的 reader(grandchild 逃離 pgid 使 join 逾時)不得寫進下一個 run 的 state,
          也只關自己那條 stdout(不是可能已換成新 run 的 self.proc)。
        - run log 以 O_APPEND 保留跨重啟的紀錄,每個 run 前加分隔行。"""
        sink = None
        try:
            loop_mod.ensure_real_directory(self.run_log.parent, "ralph run log 目錄")
            log_fd = loop_mod._open_regular(self.run_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            sink = os.fdopen(log_fd, "ab", closefd=True)
            sink.write(f"\n===== ralph run gen={gen} @ "
                       f"{datetime.now().isoformat(timespec='seconds')} =====\n".encode("utf-8"))
            sink.flush()
        except (OSError, ValueError) as e:
            loop_mod.log(f"⚠ ralph run log 無法開啟(不影響監控):{e}")
            sink = None
        try:
            for raw in proc.stdout:
                try:
                    line = raw.decode("utf-8", errors="replace").rstrip("\n")
                    loop_mod.agent_log(line)
                    if sink is not None:
                        sink.write(raw)
                        sink.flush()
                    self._handle_line(line, gen)
                except Exception as e:  # noqa: BLE001 — 單行失敗仍要續讀,否則 pipe 塞滿 ralph 阻塞
                    loop_mod.log(f"⚠ ralph 輸出處理失敗(續讀):{e}")
        except Exception:  # noqa: BLE001 — 讀 pipe 本身的例外也不能讓 reader 靜默死掉
            pass
        finally:
            if sink is not None:
                try:
                    sink.close()
                except OSError:
                    pass
            with self._lock:
                if gen == self._run_gen and self._det is not None:
                    try:
                        self._evaluate_iteration(self._det)   # 結算最後一輪(收尾才冒出的 limit)
                    except Exception:  # noqa: BLE001 — 收尾結算失敗不影響退出判定
                        pass
            try:
                proc.stdout.close()
            except (OSError, ValueError):
                pass

    def _handle_line(self, line, gen):
        """處理單行:迭代邊界、sentinel、usage-limit pattern。gen 過期的殘留 reader 直接略過。"""
        match = ITERATION_RE.search(line)
        changed = False
        confirm_now = False
        with self._lock:
            if gen != self._run_gen:
                return   # 殘留 reader:不得污染新 run 的偵測/迭代/完成狀態
            if match:
                self._iteration = int(match.group(1))
                self._begin_iteration(self._iteration)
                changed = True
            if SENTINEL_RE.search(line):
                self._sentinel = True
                changed = True
            for pattern, tier, source in self._patterns:
                if pattern.search(line):
                    if self._det is None:
                        self._det = {"iter": self._iteration, "start_fp": self._fingerprint(),
                                     "matches": []}
                    self._det["matches"].append({"tier": tier, "source": source,
                                                 "pattern": pattern.pattern,
                                                 "line": line[:300],
                                                 "iteration": self._iteration,
                                                 "at": datetime.now().isoformat(timespec="seconds")})
                    break
            self._last_activity = time.monotonic()
            confirm_now = self._limit_confirmed.is_set()
        if changed and not confirm_now:
            try:
                self._project()
            except Exception as e:  # noqa: BLE001 — 投影失敗不該中斷輸出鏡射
                loop_mod.log(f"⚠ ralph 進度投影失敗(不影響執行):{e}")

    # ---- 停止 / kill ----
    def _install_signal_handlers(self):
        """把 Dashboard 送來的 SIGINT/SIGTERM 轉成優雅停止並轉發給 ralph process group。

        關鍵:Dashboard Job.stop 會在 8 秒後 SIGKILL 監督層(但 ralph 在獨立 session,殺不到),
        因此這裡除了轉發 SIGINT,還獨立排一個 grace 後的 SIGKILL timer,保證 ralph 在監督層被
        SIGKILL 前就先死,不留孤兒 ralph。"""
        def handler(signum, _frame):
            self._stopping.set()
            loop_mod.log(f"⏹ 收到停止訊號({signum}),轉發給 ralph 並排定 {KILL_GRACE_SEC}s 後強制收掉")
            self._forward_signal(signal.SIGINT)
            if self._force_kill_timer is None:
                self._force_kill_timer = threading.Timer(KILL_GRACE_SEC,
                                                         lambda: self._forward_signal(signal.SIGKILL))
                self._force_kill_timer.daemon = True
                self._force_kill_timer.start()
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def _forward_signal(self, sig):
        """對 ralph 的 process group 送訊號;pgid 防線沿用 loop 的 safe_killpg。"""
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            loop_mod.safe_killpg(os.getpgid(self.proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _kill_ralph(self):
        """SIGINT 轉發 → 寬限 → SIGKILL 整個 process group;確保空轉 ralph 立即停手。"""
        if self.proc is None or self.proc.poll() is not None:
            return
        self._forward_signal(signal.SIGINT)
        grace = _env_float("RALPH_KILL_GRACE_SEC", KILL_GRACE_SEC)
        try:
            self.proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            self._forward_signal(signal.SIGKILL)
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def _interruptible_sleep(self, seconds):
        """可被停止訊號打斷的等待;回傳是否被要求停止。"""
        return self._stopping.wait(max(0.0, seconds))

    # ---- 單一 ralph run ----
    def _run_one_ralph(self, active_model):
        """spawn 一次 ralph 並監控直到結束/被停止/確認 usage limit。回傳終態字串。"""
        with self._lock:
            self._run_gen += 1          # 新世代:上一個(可能還在排空)reader 的寫入自此作廢
            gen = self._run_gen
            self._limit_confirmed.clear()
            self._limit_evidence = None
            self._det = None
            self._tier2_streak = 0
            self._run_made_progress = False
            self._iteration = 0
            self._sentinel = False
            self._last_activity = time.monotonic()
            # 等待後恢復的新 run:清掉過期的「waiting」限制區塊,避免 state 一直顯示等待並卡住 attention。
            if self._limit_block and self._limit_block.get("action") == "waiting":
                self._limit_block = None
        argv = build_ralph_argv(
            self.args.ralph_cmd, self.args.args_template,
            iterations=self.args.iterations, tool=self.args.tool,
            model=active_model, prd_path=self.prd_path)
        env = expose_project_package({**os.environ, "LOOP_WS": str(self.ws.dir),
                                      "RALPH_WS": str(self.ws.dir)})
        loop_mod.log(f"🚀 啟動 ralph｜model={active_model or '(預設)'}｜{shlex.join(argv)}")
        try:
            # binary stdout(不用 text=True):reader 自行以 errors="replace" 解碼,壞位元組不會殺 reader。
            self.proc = subprocess.Popen(
                argv, cwd=str(self.repo), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True, env=env)
        except OSError as e:
            loop_mod.fail(f"ralph 啟動失敗:{e}")
        proc = self.proc
        loop_mod.atomic_write_bytes(
            self.ws.dir / "startup_ready.json",
            json.dumps({"pid": os.getpid()}).encode("utf-8"))
        reader = threading.Thread(target=self._consume_output, args=(proc, gen), daemon=True)
        reader.start()
        while proc.poll() is None:
            if self._stopping.is_set() or self._limit_confirmed.is_set():
                break
            self._limit_confirmed.wait(POLL_INTERVAL_SEC)
            try:
                self._project()
            except Exception as e:  # noqa: BLE001 — 輪詢投影失敗不中斷監控
                loop_mod.log(f"⚠ ralph 進度投影失敗(不影響執行):{e}")
        if self._limit_confirmed.is_set() or self._stopping.is_set():
            self._kill_ralph()
        # reader 可能因 grandchild 逃離 pgid 而卡住;gen 綁定確保它即使晚死也不會污染下一個 run。
        reader.join(timeout=5)
        self._last_exit_code = proc.returncode
        if self._stopping.is_set():
            return "interrupted"
        if self._limit_confirmed.is_set():
            return "usage_limit"
        return self._classify_exit(self._last_exit_code)

    def _classify_exit(self, exit_code):
        """ralph 自行結束時判定終態:sentinel/全 pass → completed;達迭代 → exhausted;否則 failed。"""
        with self._lock:
            sentinel = self._sentinel
            iteration = self._iteration
        block = self.state.get("ralph", {})
        total = block.get("stories_total", 0)
        done = block.get("stories_done", 0)
        if sentinel or (total > 0 and done == total):
            return "completed"
        # 達迭代上限但未全部完成 → exhausted,即使 script 以 exit 0 收場(公司版可能不像 snarktank 回 1)。
        if self.args.iterations > 0 and iteration >= self.args.iterations:
            return "iterations_exhausted"
        if exit_code == 0:
            return "completed"
        return "failed"

    # ---- 外圈主流程 ----
    def run(self):
        """外圈:反覆 spawn ralph,遇 usage limit 就等待/降級後重啟,直到收斂或達安全上限。"""
        self.preflight()
        loop_mod.acquire_run_lock(self.ws.dir / ".run.lock", "run lock")
        (self.ws.dir / "startup_ready.json").unlink(missing_ok=True)
        self.base_sha = _git_out(self.repo, "rev-parse", "HEAD")
        self.state = self._fresh_state()

        def _mark_stopped():
            """退出時清 pid 並落盤,讓 Dashboard 的 ps 兜底判定為已停止。"""
            if self.state is not None:
                self.state["loop"]["pid"] = None
                try:
                    self.ws.save_state(self.state)
                except Exception:  # noqa: BLE001 — 收尾寫檔失敗不再拋出
                    pass
        atexit.register(_mark_stopped)
        self._install_signal_handlers()

        loop_mod.log(f"⚙️ 設定｜repo={self.repo}｜ralph_dir={self.ralph_dir}｜"
                     f"iterations={self.args.iterations}｜tool={self.args.tool}｜"
                     f"model={self.args.model or '(無)'}｜prd={self.prd_path}｜"
                     f"usage-limit={self.args.usage_limit_action}｜"
                     f"fallback={self.args.fallback_models or '(無)'}｜"
                     f"auto-restart≤{self.args.auto_restart_max}")
        self.state["ralph"] = project_ralph_block(
            self.repo, self.ralph_dir, self.prd_path, self.base_sha,
            iteration=0, max_iterations=self.args.iterations,
            sentinel_complete=False, stalled=False, exit_code=None, exit_reason=None)
        self.state["ralph"]["active_model"] = self._active_model
        self.state["ralph"]["restart_attempt"] = 0
        self.state["ralph"]["usage_limit"] = None
        self.ws.save_state(self.state)

        # 模型鏈:primary 在前,後接去重的 fallback。
        chain = [self.args.model or ""]
        for model in self.args.fallback_models:
            if model and model not in chain:
                chain.append(model)
        model_index = 0
        total_wait = 0.0
        final_reason = "failed"

        while True:
            self._active_model = chain[model_index]
            self._project()
            reason = self._run_one_ralph(self._active_model)
            if reason == "interrupted":
                final_reason = "interrupted"
                break
            if reason in ("completed", "iterations_exhausted", "failed"):
                final_reason = reason
                break
            # reason == "usage_limit"
            evidence = self._limit_evidence or {}
            if self._run_made_progress:
                self._restart_attempt = 0   # 有實質進展的 run 重置連續重啟計數
            self._restart_attempt += 1
            if self._restart_attempt > self.args.auto_restart_max or total_wait >= MAX_TOTAL_WAIT_SEC:
                self._record_limit(evidence, action="giveup", restart_attempt=self._restart_attempt,
                                   total_wait=total_wait)
                final_reason = "usage_limit_giveup"
                break
            can_downgrade = (self.args.usage_limit_action == "downgrade" and
                             model_index + 1 < len(chain))
            if can_downgrade:
                from_model = chain[model_index]
                model_index += 1
                to_model = chain[model_index]
                self._record_limit(evidence, action="downgraded", from_model=from_model,
                                   to_model=to_model, restart_attempt=self._restart_attempt,
                                   total_wait=total_wait)
                loop_mod.log(f"⤵ 用量上限｜降級模型 {from_model or '(預設)'} → {to_model}｜即刻重啟")
                if self._interruptible_sleep(_env_float("RALPH_SETTLE_SEC", SETTLE_DELAY_SEC)):
                    final_reason = "interrupted"
                    break
            else:
                wait_secs, source, parsed_iso = self._compute_wait(evidence, self._restart_attempt)
                total_wait += wait_secs
                self._record_limit(evidence, action="waiting", wait_secs=wait_secs, source=source,
                                   parsed_iso=parsed_iso, restart_attempt=self._restart_attempt,
                                   total_wait=total_wait)
                resume_at = self._limit_block.get("resume_at")
                loop_mod.log(f"⏳ 用量上限(heuristic)｜第 {self._restart_attempt}/"
                             f"{self.args.auto_restart_max} 次｜{source} 等待 {int(wait_secs)}s"
                             f"（約 {resume_at}）後重啟")
                actual = min(wait_secs, _env_float("RALPH_TEST_WAIT_CAP_SEC", float("inf")))
                if self._interruptible_sleep(actual):
                    final_reason = "interrupted"
                    break
                model_index = 0   # 等到新視窗後升回 primary

        if final_reason in ("completed", "iterations_exhausted", "failed", "usage_limit_giveup"):
            self.state["phase"] = "done"
        block = self._project(exit_code=self._last_exit_code, exit_reason=final_reason)
        self._finish_log(self._last_exit_code, final_reason, block)
        loop_mod.notify(self.args.notify_cmd, final_reason, self.ws.name)
        return 0 if final_reason in ("completed", "interrupted") else 1

    def _compute_wait(self, evidence, attempt):
        """由命中訊息解析 reset 時間;可解析則夾在 [60s,6h],否則採指數退避(base60,cap1h)。"""
        text = "\n".join(match.get("line", "") for match in evidence.get("matches", []))
        now = time.time()
        target = parse_reset_target(text, now)
        if target is not None:
            wait = min(MAX_WAIT_SEC, max(MIN_WAIT_SEC, target - now + 90))
            parsed_iso = datetime.fromtimestamp(target).isoformat(timespec="seconds")
            return wait, "parsed", parsed_iso
        wait = min(BACKOFF_CAP_SEC, BACKOFF_BASE_SEC * (2 ** max(0, attempt - 1)))
        return wait, "backoff", None

    def _record_limit(self, evidence, *, action, wait_secs=None, source=None, parsed_iso=None,
                      from_model=None, to_model=None, restart_attempt=0, total_wait=0.0):
        """組出 heuristic-labeled usage_limit 投影區塊並落盤,供 Dashboard 誠實呈現與調參。"""
        now = time.time()
        matches = evidence.get("matches", [])
        resume_at = None
        if action == "waiting" and wait_secs:
            resume_at = datetime.fromtimestamp(now + wait_secs).isoformat(timespec="seconds")
        self._limit_block = {
            "detection": "heuristic",
            "state": action,
            "action": action,
            "detected_at": evidence.get("at") or datetime.now().isoformat(timespec="seconds"),
            "matched": matches[-1]["line"] if matches else "",
            "matches": matches,
            "iteration": evidence.get("iteration"),
            "resume_at": resume_at,
            "wait_until": resume_at,
            "reset_source": source,
            "parsed_reset_at": parsed_iso,
            "wait_seconds": int(wait_secs) if wait_secs else None,
            "from_model": from_model,
            "to_model": to_model,
            "restart_attempt": restart_attempt,
            "restarts_max": int(self.args.auto_restart_max),
            "total_wait_secs": int(total_wait),
        }
        try:
            self._project()
        except Exception as e:  # noqa: BLE001 — 記錄投影失敗不影響外圈流程
            loop_mod.log(f"⚠ usage-limit 投影失敗(不影響執行):{e}")

    def _finish_log(self, exit_code, exit_reason, block):
        """輸出人類可讀的終態摘要。"""
        summary = {
            "completed": "✅ ralph 完成所有 story",
            "iterations_exhausted": "⏹ ralph 達最大迭代仍未全部完成",
            "failed": "⛔ ralph 以錯誤結束",
            "interrupted": "⏸ ralph 已被停止(現場保留,可重啟續跑)",
            "usage_limit_giveup": "🛑 連續用量上限達重啟上限,停止(可稍後手動重啟)",
        }.get(exit_reason, "ralph 已結束")
        loop_mod.log(f"{summary}｜rc={exit_code}｜"
                     f"story {block.get('stories_done', 0)}/{block.get('stories_total', 0)}｜"
                     f"iteration {block.get('iteration', 0)}/{self.args.iterations}｜"
                     f"重啟 {self._restart_attempt} 次")


def _json_list_arg(parser, raw, label):
    """解析 JSON 陣列 CLI 參數;空字串視為空陣列,型別錯誤 parser.error。"""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as e:
        parser.error(f"{label} 不是合法 JSON:{e}")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        parser.error(f"{label} 必須是字串陣列")
    return value


def parse_args(argv=None):
    """解析監督層命令列;引數由 Dashboard 的 spawn_ralph 組出。"""
    parser = argparse.ArgumentParser(prog="engine.ralph", description="Ralph runner 監督層")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--ralph-cmd", required=True, dest="ralph_cmd")
    parser.add_argument("--ralph-dir", dest="ralph_dir", default="")
    parser.add_argument("--iterations", type=int, required=True)
    parser.add_argument("--tool", required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--prd-path", dest="prd_path", default="prd.json")
    parser.add_argument("--args-template", dest="args_template_json", default="")
    parser.add_argument("--args-style", dest="args_style", default=DEFAULT_ARGS_STYLE)
    parser.add_argument("--notify-cmd", dest="notify_cmd", default="")
    parser.add_argument("--usage-limit-action", dest="usage_limit_action", default="restart",
                        choices=("restart", "downgrade", "off"))
    parser.add_argument("--auto-restart-max", dest="auto_restart_max", type=int,
                        default=DEFAULT_AUTO_RESTART_MAX)
    parser.add_argument("--fallback-models", dest="fallback_models_json", default="")
    parser.add_argument("--usage-limit-patterns", dest="usage_limit_patterns_json", default="")
    args = parser.parse_args(argv)
    # args_template 以 JSON 傳入(避免 shell 引號問題);未給時由 style 推導。
    if args.args_template_json:
        template = _json_list_arg(parser, args.args_template_json, "--args-template")
        args.args_template = template
    else:
        args.args_template = list(ARGS_STYLES.get(args.args_style, ARGS_STYLES[DEFAULT_ARGS_STYLE]))
    args.fallback_models = _json_list_arg(parser, args.fallback_models_json, "--fallback-models")
    args.usage_limit_patterns = _json_list_arg(
        parser, args.usage_limit_patterns_json, "--usage-limit-patterns")
    if args.auto_restart_max < 0:
        parser.error("--auto-restart-max 必須 ≥0")
    if not args.ralph_dir:
        args.ralph_dir = str(default_ralph_dir(args.ralph_cmd, args.repo))
    return args


def main(argv=None):
    """CLI 入口:驗證引數並跑完一個 ralph run(含 usage-limit 外圈)。"""
    args = parse_args(argv)
    if not loop_mod.valid_workspace_name(args.name):
        loop_mod.fail(f"workspace 名稱不合法:{loop_mod.WORKSPACE_NAME_RULE}")
    if not (0 < args.iterations <= ITERATIONS_MAX):
        loop_mod.fail(f"iterations 必須介於 1～{ITERATIONS_MAX}")
    if not TOOL_RE.fullmatch(args.tool or "") or (args.tool or "").startswith("-"):
        loop_mod.fail("tool 只能是英數與 . _ -,且不可以 - 開頭")
    model = args.model or ""
    if len(model) > MODEL_MAX_CHARS or any(ord(ch) < 32 for ch in model) or model.startswith("-"):
        loop_mod.fail(f"model 需 ≤{MODEL_MAX_CHARS} 字、不含控制字元且不可以 - 開頭")
    supervisor = RalphSupervisor(args)
    return supervisor.run()


if __name__ == "__main__":
    sys.exit(main())
