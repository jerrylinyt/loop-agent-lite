"""安裝與 checkout 共用的 runtime 路徑；避免把可寫資料放進 site-packages。"""
import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
_checkout_candidate = PACKAGE_ROOT.parent
CHECKOUT_ROOT = _checkout_candidate if (_checkout_candidate / "pyproject.toml").is_file() else None
USER_DATA_ROOT = Path(os.environ.get(
    "LOOP_AGENT_HOME", Path.home() / ".local" / "share" / "loop-agent-lite"
)).expanduser().resolve()


def default_workspace_root() -> Path:
    """Editable checkout 沿用既有 workspace；wheel 安裝則使用使用者資料目錄。"""
    configured = os.environ.get("LOOP_AGENT_WORKSPACE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return ((CHECKOUT_ROOT / "workspace") if CHECKOUT_ROOT else (USER_DATA_ROOT / "workspace")).resolve()


def default_personal_config() -> Path:
    """個人設定必須可寫；checkout 保持舊位置，wheel 不寫入套件目錄。"""
    configured = os.environ.get("LOOP_AGENT_DASHBOARD_CONFIG")
    if configured:
        return Path(configured).expanduser().resolve()
    return ((CHECKOUT_ROOT / "dashboard.config.local.json") if CHECKOUT_ROOT
            else (USER_DATA_ROOT / "dashboard.config.local.json")).resolve()


def legacy_config_path() -> Path:
    """回傳舊版單檔設定位置，供首次啟動遷移。"""
    return ((CHECKOUT_ROOT / "dashboard.config.json") if CHECKOUT_ROOT
            else (USER_DATA_ROOT / "dashboard.config.json")).resolve()


def expose_checkout_package(env: dict) -> dict:
    """開發 checkout 的子程序補上 import root；正式安裝時不改 PYTHONPATH。"""
    if CHECKOUT_ROOT is None:
        return env
    existing = [value for value in env.get("PYTHONPATH", "").split(os.pathsep) if value]
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys([str(CHECKOUT_ROOT), *existing]))
    return env
