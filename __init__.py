import traceback

try:
    import asyncio

    from .config import Config
    from .pixiv_client import PixivClient, WebSessionError, is_pximg_url

    _config = Config()
    _client_instance = PixivClient(_config)

    from server import PromptServer
    from aiohttp import web
    import aiohttp

    routes = PromptServer.instance.routes
    _client_lock = asyncio.Lock()

    async def _run_client(func, *args, **kwargs):
        async with _client_lock:
            return await asyncio.to_thread(func, *args, **kwargs)

    async def _api_response(func, *args, **kwargs):
        try:
            return web.json_response(await _run_client(func, *args, **kwargs))
        except WebSessionError as e:
            return web.json_response(
                {"error": str(e), "code": "web_session_required"}, status=401
            )
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=401)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    def _client_action(method, value):
        _client_instance.ensure_logged_in()
        method(value)
        return {"ok": True}

    # ── Auth ──────────────────────────────────────────────────────────────────

    @routes.get("/pixiv/status")
    async def pixiv_status(request):
        # Consider logged-in if we have a saved token — actual auth happens lazily on first API call
        logged_in = _client_instance._logged_in or bool(_config.get_refresh_token())
        username = ""
        if _client_instance._logged_in:
            try:
                username = str(_client_instance.api.user_id)
            except Exception:
                pass
        return web.json_response({"logged_in": logged_in, "username": username})

    # Temporary store for PKCE verifier during auth flow
    _pending_verifier = {}

    @routes.post("/pixiv/auth/login")
    async def pixiv_auth_login(request):
        verifier, challenge = _client_instance.generate_pkce()
        auth_url = _client_instance.get_login_url(challenge)
        _pending_verifier["current"] = verifier
        return web.json_response({"auth_url": auth_url})

    @routes.post("/pixiv/auth/callback")
    async def pixiv_auth_callback(request):
        data = await request.json()
        redirect_url = data.get("redirect_url", "")
        verifier = _pending_verifier.pop("current", None)
        if not verifier:
            return web.json_response({"error": "No pending auth"}, status=400)
        try:
            code = _client_instance.extract_code(redirect_url)
            await _run_client(_client_instance.login_with_code, code, verifier)
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    @routes.post("/pixiv/auth/set_token")
    async def pixiv_set_token(request):
        data = await request.json()
        token = data.get("refresh_token", "").strip()
        if not token:
            return web.json_response({"error": "refresh_token 不能为空"}, status=400)
        try:
            await _run_client(_client_instance.login_with_refresh_token, token)
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    @routes.post("/pixiv/auth/set_web_session")
    async def pixiv_set_web_session(request):
        data = await request.json()
        session_id = data.get("php_session_id", "")
        try:
            await _run_client(_client_instance.set_web_session, session_id)
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    # ── Content ───────────────────────────────────────────────────────────────

    @routes.get("/pixiv/recommended")
    async def pixiv_recommended(request):
        rating = request.query.get("rating", "all")
        if rating not in {"all", "safe", "r18"}:
            return web.json_response({"error": "invalid rating"}, status=400)
        next_url = request.query.get("next_url")
        return await _api_response(
            _client_instance.get_recommended,
            next_url=next_url,
            rating=rating,
        )

    @routes.get("/pixiv/followed")
    async def pixiv_followed(request):
        next_url = request.query.get("next_url")
        return await _api_response(_client_instance.get_followed, next_url=next_url)

    @routes.get("/pixiv/illust/{illust_id}")
    async def pixiv_illust(request):
        try:
            illust_id = int(request.match_info["illust_id"])
        except ValueError:
            return web.json_response({"error": "invalid illust_id"}, status=400)
        return await _api_response(_client_instance.get_illust, illust_id)

    @routes.get("/pixiv/ranking")
    async def pixiv_ranking(request):
        mode = request.query.get("mode", "day")
        next_url = request.query.get("next_url")
        return await _api_response(
            _client_instance.get_ranking, mode=mode, next_url=next_url
        )

    @routes.get("/pixiv/bookmarks")
    async def pixiv_bookmarks(request):
        restrict = request.query.get("restrict", "public")
        if restrict not in {"public", "private"}:
            return web.json_response({"error": "invalid restrict"}, status=400)
        next_url = request.query.get("next_url")
        return await _api_response(
            _client_instance.get_bookmarks,
            next_url=next_url,
            restrict=restrict,
        )

    @routes.get("/pixiv/bookmarked_artists")
    async def pixiv_bookmarked_artists(request):
        next_url = request.query.get("next_url")
        return await _api_response(
            _client_instance.get_bookmarked_artists, next_url=next_url
        )

    @routes.get("/pixiv/artist/{artist_id}/works")
    async def pixiv_artist_works(request):
        try:
            artist_id = int(request.match_info["artist_id"])
        except ValueError:
            return web.json_response({"error": "invalid artist_id"}, status=400)
        next_url = request.query.get("next_url")
        return await _api_response(
            _client_instance.get_artist_works,
            artist_id=artist_id,
            next_url=next_url,
        )

    @routes.post("/pixiv/bookmark")
    async def pixiv_add_bookmark(request):
        data = await request.json()
        illust_id = data.get("illust_id")
        if not illust_id:
            return web.json_response({"error": "illust_id required"}, status=400)
        try:
            illust_id = int(illust_id)
        except (TypeError, ValueError):
            return web.json_response({"error": "invalid illust_id"}, status=400)
        return await _api_response(
            _client_action, _client_instance.api.illust_bookmark_add, illust_id
        )

    @routes.post("/pixiv/bookmark_delete")
    async def pixiv_del_bookmark(request):
        data = await request.json()
        illust_id = data.get("illust_id")
        if not illust_id:
            return web.json_response({"error": "illust_id required"}, status=400)
        try:
            illust_id = int(illust_id)
        except (TypeError, ValueError):
            return web.json_response({"error": "invalid illust_id"}, status=400)
        return await _api_response(
            _client_action, _client_instance.api.illust_bookmark_delete, illust_id
        )

    @routes.get("/pixiv/search/illusts")
    async def pixiv_search_illusts(request):
        word = request.query.get("word", "")
        next_url = request.query.get("next_url")
        if not word and not next_url:
            return web.json_response({"error": "word required"}, status=400)
        return await _api_response(
            _client_instance.search_illusts, word, next_url=next_url
        )

    @routes.get("/pixiv/search/users")
    async def pixiv_search_users(request):
        word = request.query.get("word", "")
        next_url = request.query.get("next_url")
        if not word and not next_url:
            return web.json_response({"error": "word required"}, status=400)
        return await _api_response(
            _client_instance.search_users, word, next_url=next_url
        )

    @routes.post("/pixiv/follow")
    async def pixiv_add_follow(request):
        data = await request.json()
        user_id = data.get("user_id")
        if not user_id:
            return web.json_response({"error": "user_id required"}, status=400)
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return web.json_response({"error": "invalid user_id"}, status=400)
        return await _api_response(
            _client_action, _client_instance.api.user_follow_add, user_id
        )

    # ── Image Proxy ───────────────────────────────────────────────────────────

    @routes.get("/pixiv/image_proxy")
    async def pixiv_image_proxy(request):
        url = request.query.get("url", "")
        if not is_pximg_url(url):
            return web.Response(status=403, text="Only pximg.net URLs allowed")
        session = PromptServer.instance.client_session
        if session is None or session.closed:
            return web.Response(status=503, text="HTTP client is not ready")
        try:
            async with session.get(
                url,
                headers={"Referer": "https://www.pixiv.net/"},
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=False,
            ) as resp:
                if resp.status != 200:
                    return web.Response(status=resp.status)
                data = await resp.read()
                content_type = resp.headers.get("Content-Type", "image/jpeg")
                content_type = content_type.split(";", 1)[0]
                return web.Response(
                    body=data,
                    content_type=content_type,
                    headers={"Cache-Control": "public, max-age=86400"},
                )
        except Exception as e:
            return web.Response(status=502, text=str(e))

    # ── Node registration ─────────────────────────────────────────────────────

    from .pixiv_node import PixivBrowser
    from . import pixiv_node as _pn_mod
    _pn_mod._client = _client_instance  # inject client — avoids circular import

    NODE_CLASS_MAPPINGS = {"PixivBrowser": PixivBrowser}
    NODE_DISPLAY_NAME_MAPPINGS = {"PixivBrowser": "Pixiv Browser"}
    WEB_DIRECTORY = "./web"

    print("[PixivBrowser] Loaded successfully")

except ImportError as e:
    print(f"[PixivBrowser] ERROR: Missing dependency — {e}")
    print("[PixivBrowser] Run: pip install pixivpy3 Pillow")
    NODE_CLASS_MAPPINGS = {}
    NODE_DISPLAY_NAME_MAPPINGS = {}
except Exception as e:
    print(f"[PixivBrowser] ERROR during load: {e}")
    traceback.print_exc()
    NODE_CLASS_MAPPINGS = {}
    NODE_DISPLAY_NAME_MAPPINGS = {}
