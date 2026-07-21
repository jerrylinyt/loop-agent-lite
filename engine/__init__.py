"""loop-agent-lite 專案內引擎；Dashboard 由根目錄 ``dashboard.py`` 啟動。"""

from engine import platform_compat as _platform_compat

_platform_compat.configure_standard_streams()

__version__ = "0.1.0"
