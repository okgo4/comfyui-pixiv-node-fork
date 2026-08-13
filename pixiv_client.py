import hashlib
import secrets
import base64
import re
import time
import requests
from types import SimpleNamespace
from urllib.parse import urlparse, parse_qs
from pixivpy3 import AppPixivAPI


def is_pximg_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        host == "pximg.net" or host.endswith(".pximg.net")
    )


class WebSessionError(RuntimeError):
    pass


class PixivClient:
    def __init__(self, config):
        self.config = config
        self.api = AppPixivAPI()
        self.http = requests.Session()
        self._logged_in = False
        self._auth_time = 0.0

    # ── Auth ──────────────────────────────────────────────────────────────────

    def generate_pkce(self):
        verifier = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b'=').decode()
        return verifier, challenge

    def _next_qs(self, next_url: str) -> dict:
        parsed = urlparse(next_url)
        # Filter out array-style keys like "viewed[]" — invalid as Python kwargs
        return {k: v[0] for k, v in parse_qs(parsed.query).items() if k.isidentifier()}

    def get_login_url(self, code_challenge: str) -> str:
        return (
            "https://app-api.pixiv.net/web/v1/login?"
            f"code_challenge={code_challenge}&"
            "code_challenge_method=S256&"
            "client=pixiv-android"
        )

    def extract_code(self, redirect_url: str) -> str:
        match = re.search(r'code=([^&]+)', redirect_url)
        if not match:
            raise ValueError(f"No code found in: {redirect_url}")
        return match.group(1)

    def login_with_code(self, code: str, code_verifier: str) -> str:
        self.api.auth(code=code, code_verifier=code_verifier)
        token = self.api.refresh_token
        self.config.save_refresh_token(token)
        self._logged_in = True
        self._auth_time = time.time()
        return token

    def login_with_refresh_token(self, token: str):
        self.api.auth(refresh_token=token)
        self.config.save_refresh_token(token)
        self._logged_in = True
        self._auth_time = time.time()

    def ensure_logged_in(self):
        token = self.api.refresh_token or self.config.get_refresh_token()
        if not token:
            raise RuntimeError("未登录，请先在弹窗中登录 Pixiv")
        # Access token expires after 3600s; refresh with a 5-min buffer
        if not self._logged_in or time.time() - self._auth_time > 3300:
            self.api.auth(refresh_token=token)
            self._logged_in = True
            self._auth_time = time.time()

    # ── Data fetch ────────────────────────────────────────────────────────────

    def get_recommended(self, next_url=None, rating="all"):
        self.ensure_logged_in()
        if rating == "r18":
            return self._get_discovery(rating)

        kwargs = self._next_qs(next_url) if next_url else {}
        result = self.api.illust_recommended(**kwargs)
        result.illusts = list(result.illusts or [])
        if rating == "safe":
            result.illusts = [
                illust for illust in result.illusts
                if not illust.x_restrict
            ]
            if not result.illusts:
                result.next_url = None
        return self._fmt_illusts(result)

    def _get_discovery(self, mode, session_id=None):
        session_id = session_id or self.config.get_web_session()
        if not session_id:
            raise WebSessionError(
                "R18 新发现使用 Pixiv Web 会话，请先配置 PHPSESSID"
            )
        try:
            response = self.http.get(
                "https://www.pixiv.net/ajax/discovery/artworks",
                params={"mode": mode, "limit": 60},
                cookies={"PHPSESSID": session_id},
                headers={
                    "Accept": "application/json",
                    "Referer": f"https://www.pixiv.net/discovery?mode={mode}",
                    "User-Agent": "Mozilla/5.0",
                },
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as e:
            raise WebSessionError(
                "Pixiv Web 会话无效或已过期，请重新配置 PHPSESSID"
            ) from e

        if payload.get("error"):
            raise WebSessionError(
                payload.get("message")
                or "Pixiv Web 会话无效或无权访问 R18 新发现"
            )
        items = ((payload.get("body") or {}).get("thumbnails") or {}).get("illust")
        if items is None:
            raise WebSessionError("Pixiv Discovery 返回了无法识别的数据")
        return {
            "illusts": [self._fmt_discovery_illust(item) for item in items],
            "next_url": None,
        }

    def set_web_session(self, session_id):
        session_id = session_id.strip()
        if not session_id or any(c.isspace() or c == ";" for c in session_id):
            raise ValueError("PHPSESSID 格式无效")
        self._get_discovery("safe", session_id=session_id)
        self.config.save_web_session(session_id)

    def _fmt_discovery_illust(self, item):
        thumbnail = item.get("url") or ""
        page = {
            "index": 0,
            "image_urls": {"medium": thumbnail, "large": thumbnail},
            "original_url": "",
        }
        return {
            "id": int(item["id"]),
            "title": item.get("title") or "",
            "width": item.get("width") or 0,
            "height": item.get("height") or 0,
            "image_urls": page["image_urls"],
            "original_url": "",
            "page_count": int(item.get("pageCount") or 1),
            "pages": [page],
            "is_bookmarked": bool(item.get("bookmarkData")),
            "user": {
                "id": int(item["userId"]),
                "name": item.get("userName") or "",
                "profile_image_urls": {
                    "medium": item.get("profileImageUrl") or "",
                },
                "is_followed": False,
            },
        }

    def get_illust(self, illust_id):
        self.ensure_logged_in()
        detail = self.api.illust_detail(illust_id).illust
        result = SimpleNamespace(illusts=[detail], next_url=None)
        return self._fmt_illusts(result)["illusts"][0]

    def get_followed(self, next_url=None):
        self.ensure_logged_in()
        kwargs = self._next_qs(next_url) if next_url else {"restrict": "public"}
        return self._fmt_illusts(self.api.illust_follow(**kwargs))

    def get_ranking(self, mode='day', next_url=None):
        self.ensure_logged_in()
        if mode in {"daily_ai", "daily_r18_ai"}:
            return self._get_ai_ranking(mode)
        kwargs = self._next_qs(next_url) if next_url else {"mode": mode}
        return self._fmt_illusts(self.api.illust_ranking(**kwargs))

    def _get_ai_ranking(self, mode):
        kwargs = {
            "params": {"mode": mode, "format": "json"},
            "headers": {
                "Accept": "application/json",
                "Referer": f"https://www.pixiv.net/ranking.php?mode={mode}",
                "User-Agent": "Mozilla/5.0",
            },
            "timeout": 30,
        }
        if mode == "daily_r18_ai":
            session_id = self.config.get_web_session()
            if not session_id:
                raise WebSessionError("R18 AI 排行榜需要 Pixiv Web 会话，请先配置 PHPSESSID")
            kwargs["cookies"] = {"PHPSESSID": session_id}

        try:
            response = self.http.get("https://www.pixiv.net/ranking.php", **kwargs)
            response.raise_for_status()
            items = response.json()["contents"]
        except (requests.RequestException, ValueError, KeyError) as e:
            if mode == "daily_r18_ai":
                raise WebSessionError(
                    "Pixiv Web 会话无效或已过期，请重新配置 PHPSESSID"
                ) from e
            raise
        return {
            "illusts": [self._fmt_ai_ranking_illust(item) for item in items],
            "next_url": None,
        }

    def _fmt_ai_ranking_illust(self, item):
        return self._fmt_discovery_illust({
            "id": item["illust_id"],
            "title": item.get("title"),
            "width": item.get("width"),
            "height": item.get("height"),
            "url": item.get("url"),
            "pageCount": item.get("illust_page_count"),
            "userId": item["user_id"],
            "userName": item.get("user_name"),
            "profileImageUrl": item.get("profile_img"),
        })

    def get_bookmarks(self, next_url=None, restrict="public"):
        self.ensure_logged_in()
        kwargs = self._next_qs(next_url) if next_url else {
            "user_id": self.api.user_id,
            "restrict": restrict,
        }
        return self._fmt_illusts(self.api.user_bookmarks_illust(**kwargs))

    def get_bookmarked_artists(self, next_url=None):
        self.ensure_logged_in()
        kwargs = self._next_qs(next_url) if next_url else {"user_id": self.api.user_id}
        return self._fmt_artists(self.api.user_following(**kwargs))

    def get_artist_works(self, artist_id, next_url=None):
        self.ensure_logged_in()
        kwargs = self._next_qs(next_url) if next_url else {"user_id": artist_id}
        return self._fmt_illusts(self.api.user_illusts(**kwargs))

    def _fmt_illusts(self, result):
        illusts = []
        for i in result.illusts:
            try:
                meta_pages = list(i.meta_pages or [])
            except Exception:
                meta_pages = []

            if meta_pages:
                pages = [
                    {
                        "index": index,
                        "image_urls": {
                            "medium": page.image_urls.medium,
                            "large": page.image_urls.large,
                        },
                        "original_url": page.image_urls.original or "",
                    }
                    for index, page in enumerate(meta_pages)
                ]
            else:
                try:
                    orig = (dict(i.meta_single_page) or {}).get("original_image_url") or ""
                except Exception:
                    orig = ""
                pages = [{
                    "index": 0,
                    "image_urls": {
                        "medium": i.image_urls.medium,
                        "large": i.image_urls.large,
                    },
                    "original_url": orig,
                }]

            illusts.append({
                "id": i.id,
                "title": i.title,
                "width": i.width,
                "height": i.height,
                "image_urls": {
                    "medium": i.image_urls.medium,
                    "large": i.image_urls.large,
                },
                "original_url": pages[0]["original_url"],
                "page_count": len(pages),
                "pages": pages,
                "is_bookmarked": bool(getattr(i, "is_bookmarked", False)),
                "user": {
                    "id": i.user.id,
                    "name": i.user.name,
                    "profile_image_urls": {"medium": i.user.profile_image_urls.medium},
                    "is_followed": bool(getattr(i.user, "is_followed", False)),
                },
            })
        return {"illusts": illusts, "next_url": result.next_url}

    def _fmt_artists(self, result):
        artists = [
            {
                "id": preview.user.id,
                "name": preview.user.name,
                "profile_image_urls": {
                    "medium": preview.user.profile_image_urls.medium,
                },
            }
            for preview in result.user_previews
        ]
        return {"artists": artists, "next_url": result.next_url}

    # ── Image download ────────────────────────────────────────────────────────

    def download_image_bytes(self, url: str) -> bytes:
        headers = {
            "Referer": "https://www.pixiv.net/",
            "User-Agent": "PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)",
        }
        response = self.http.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.content

    def search_illusts(self, word, next_url=None):
        self.ensure_logged_in()
        kwargs = self._next_qs(next_url) if next_url else {
            "word": word, "search_target": "partial_match_for_tags"
        }
        return self._fmt_illusts(self.api.search_illust(**kwargs))

    def search_users(self, word, next_url=None):
        self.ensure_logged_in()
        kwargs = self._next_qs(next_url) if next_url else {"word": word}
        return self._fmt_artists(self.api.search_user(**kwargs))

    def get_original_url(self, illust_id: int, page_index: int = 0) -> str:
        self.ensure_logged_in()
        detail = self.api.illust_detail(illust_id).illust
        pages = detail.meta_pages or []
        if pages:
            if page_index < 0 or page_index >= len(pages):
                raise IndexError(f"作品 {illust_id} 不存在第 {page_index + 1} 页")
            return pages[page_index].image_urls.original
        if page_index != 0:
            raise IndexError(f"作品 {illust_id} 只有一张图片")
        single = detail.meta_single_page.get("original_image_url")
        if single:
            return single
        raise ValueError(f"作品 {illust_id} 没有可用的原图地址")
