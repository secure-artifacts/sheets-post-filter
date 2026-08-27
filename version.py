"""应用版本和更新地址。"""

from __future__ import annotations

import re

APP_VERSION = "1.4.1"
GITHUB_REPO = "secure-artifacts/sheets-post-filter"
UPDATE_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"


def version_tuple(value: str) -> tuple[int, ...]:
    parts = [int(item) for item in re.findall(r"\d+", str(value or ""))[:4]]
    parts = parts or [0]
    return tuple((parts + [0, 0, 0, 0])[:4])
