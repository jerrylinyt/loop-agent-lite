"""專案內 runtime 路徑；所有預設可寫資料固定放在 loop-agent-lite checkout。"""
import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent


def default_workspace_root() -> Path:
    """預設固定使用專案內 workspace；顯式 override 只供隔離執行與測試。"""
    configured = os.environ.get("LOOP_AGENT_WORKSPACE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return (PROJECT_ROOT / "workspace").resolve()


def default_personal_config() -> Path:
    """個人設定預設固定放在專案根目錄。"""
    configured = os.environ.get("LOOP_AGENT_DASHBOARD_CONFIG")
    if configured:
        return Path(configured).expanduser().resolve()
    return (PROJECT_ROOT / "dashboard.config.local.json").resolve()


def legacy_config_path() -> Path:
    """回傳舊版單檔設定位置，供首次啟動遷移。"""
    return (PROJECT_ROOT / "dashboard.config.json").resolve()


def expose_project_package(env: dict) -> dict:
    """子程序補上固定的專案 root，讓 ``python -m engine.*`` 可直接執行。"""
    existing = [value for value in env.get("PYTHONPATH", "").split(os.pathsep) if value]
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys([str(PROJECT_ROOT), *existing]))
    return env
