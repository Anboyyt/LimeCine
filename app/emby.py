"""极简 Emby / Jellyfin / LiMe 客户端：探活、列库、取海报、写回库封面。"""
from __future__ import annotations

import os
import tempfile
from typing import Any, Dict, List, Optional

import requests

from app.compat.log import logger
from app.config import APP_NAME, VERSION

USER_AGENT = f"{APP_NAME}/{VERSION}"


class EmbyError(Exception):
    pass


class EmbyClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 30):
        if not base_url:
            raise EmbyError("服务器地址为空")
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.timeout = timeout
        self.s = requests.Session()
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if api_key:
            headers["X-Emby-Token"] = api_key
            headers["X-MediaBrowser-Token"] = api_key
        self.s.headers.update(headers)

    def close(self) -> None:
        try:
            self.s.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "EmbyClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _get(self, path: str, **params: Any) -> Dict[str, Any]:
        try:
            r = self.s.get(self._url(path), params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise EmbyError(f"连接服务器失败: {e}") from e
        if r.status_code == 401:
            raise EmbyError("API Key 无效或已过期")
        if r.status_code == 404:
            raise EmbyError(f"{path} 返回 404：这个地址不像 Emby/Jellyfin/LiMe")
        if r.status_code >= 400:
            raise EmbyError(f"{path} 返回 {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise EmbyError(f"{path} 返回的不是 JSON") from e

    # ---- 探活 ----
    def public_info(self) -> Dict[str, Any]:
        return self._get("/System/Info/Public")

    def probe(self) -> Dict[str, Any]:
        info = self.public_info()
        return {
            "ok": True,
            "server_name": info.get("ServerName") or info.get("ProductName") or "",
            "version": info.get("Version") or "",
            "product": info.get("ProductName") or "",
        }

    # ---- 库 ----
    def libraries(self) -> List[Dict[str, Any]]:
        data = self._get("/Library/MediaFolders")
        items = data.get("Items") or []
        out: List[Dict[str, Any]] = []
        for it in items:
            out.append({
                "id": it.get("Id") or it.get("id"),
                "name": it.get("Name") or it.get("name") or "",
                "type": it.get("CollectionType") or it.get("collectionType") or "",
            })
        return [x for x in out if x["id"]]

    # ---- 取带海报的条目 ----
    def poster_items(self, library_id: str, limit: int = 30) -> List[Dict[str, Any]]:
        data = self._get(
            "/Items",
            ParentId=library_id,
            Recursive="true",
            IncludeItemTypes="Movie,Series",
            SortBy="DateCreated",
            SortOrder="Descending",
            Limit=limit,
            Fields="ImageTags,PrimaryImageAspectRatio",
            ImageTypeLimit=1,
            EnableImageTypes="Primary",
        )
        out: List[Dict[str, Any]] = []
        for it in data.get("Items") or []:
            tags = it.get("ImageTags") or {}
            if not tags.get("Primary"):
                continue
            iid = it.get("Id") or it.get("id")
            if not iid:
                continue
            out.append({
                "id": str(iid),
                "name": it.get("Name") or "",
                "type": it.get("Type") or "",
                "tag": str(tags.get("Primary")),
            })
        return out

    def poster_url(self, item_id: str, tag: Optional[str] = None, width: int = 600) -> str:
        params: Dict[str, Any] = {"maxWidth": width}
        if tag:
            params["tag"] = tag
        req = requests.Request("GET", self._url(f"/Items/{item_id}/Images/Primary"), params=params)
        return req.prepare().url or self._url(f"/Items/{item_id}/Images/Primary")

    # ---- 下载 ----
    def download(self, url: str, dest: str, retries: int = 1) -> bool:
        last_error: Optional[str] = None
        for attempt in range(retries + 1):
            try:
                r = self.s.get(url, timeout=self.timeout)
            except requests.RequestException as e:
                last_error = str(e)
                continue
            if r.status_code != 200:
                last_error = f"HTTP {r.status_code}"
                continue
            if not r.content:
                last_error = "空响应"
                continue
            ctype = (r.headers.get("Content-Type") or "").lower()
            if ctype and not ctype.startswith("image/"):
                last_error = f"Content-Type={ctype}"
                continue
            try:
                d = os.path.dirname(dest)
                if d:
                    os.makedirs(d, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=d or ".", prefix=".poster-")
                with os.fdopen(fd, "wb") as f:
                    f.write(r.content)
                os.replace(tmp, dest)
                return True
            except OSError as e:
                last_error = str(e)
        logger.warning("下载海报失败 %s: %s", url, last_error)
        return False

    # ---- 写回库封面 ----
    def upload_cover(self, library_id: str, data: bytes, content_type: str = "image/png") -> None:
        try:
            r = self.s.post(
                self._url(f"/Items/{library_id}/Images/Primary"),
                data=data,
                headers={"Content-Type": content_type},
                timeout=max(self.timeout, 60),
            )
        except requests.RequestException as e:
            raise EmbyError(f"上传封面失败: {e}") from e
        if r.status_code not in (200, 204):
            raise EmbyError(f"上传封面返回 {r.status_code}: {r.text[:200]}")
        logger.info("封面已写回 library=%s bytes=%d", library_id, len(data))
