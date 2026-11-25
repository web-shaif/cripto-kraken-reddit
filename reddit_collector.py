"""Read-only collector for an approved Reddit Data API application."""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field
from email.utils import parsedate_to_datetime
import json
import math
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from ssl import create_default_context
from urllib.parse import parse_qsl, urljoin

MAX_NEWS_URL = 2048


@dataclass(frozen=True)
class RedditPost:
    reddit_id: str
    subreddit: str
    title: str
    permalink: str
    published_at: int
    first_seen_at: int
    article_url: str | None = None
    kind: str = "community discussion"


class NewsParseError(ValueError):
    pass


def _canonical_url(raw: str, base: str) -> str:
    absolute = urljoin(base, raw.strip())
    parts = urlsplit(absolute)
    if parts.scheme != "https" or not parts.netloc:
        raise NewsParseError("News links must be absolute HTTPS URLs")
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"ref", "source"}
    ]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", urlencode(kept), ""))


def canonical_article_url(raw: str) -> str | None:
    if not raw or len(raw) > MAX_NEWS_URL or any(char.isspace() for char in raw):
        return None
    try:
        candidate = _canonical_url(raw, "")
        parts = urlsplit(candidate)
        if not parts.hostname or parts.username or parts.password or parts.port not in (None, 443):
            return None
    except (NewsParseError, ValueError):
        return None
    return candidate


AWAITING_AUTHORIZATION = "Reddit Data API approval and OAuth credentials are required."
COMMUNITY_DISCUSSION = "community discussion"
RETENTION_SECONDS = 48 * 3600
_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_API = "https://oauth.reddit.com"
_POST_ID = re.compile(r"[a-z0-9]{1,16}")
_SUBREDDIT = re.compile(r"[A-Za-z0-9_]{2,21}")


@dataclass(frozen=True)
class RedditCredentials:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    username: str

    def __post_init__(self):
        if (not self.client_id or not self.client_secret or ":" in self.client_id
                or not re.fullmatch(r"[A-Za-z0-9_-]{3,20}", self.username)):
            raise ValueError("Invalid Reddit OAuth credentials or Reddit username")


class RedditNotConfigured(RuntimeError):
    pass


class RedditCollectionError(RuntimeError):
    pass


class RedditRateLimited(RedditCollectionError):
    def __init__(self, retry_at: int):
        self.retry_at = retry_at
        super().__init__("Reddit rate limit reached")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(request, timeout):
    opener = build_opener(_NoRedirect(), HTTPSHandler(context=create_default_context()))
    return opener.open(request, timeout=timeout)


def _seconds(header) -> int:
    try:
        return max(0, math.ceil(float(header)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _retry_after(header, now: int) -> int:
    try:
        return max(0, math.ceil(parsedate_to_datetime(header).timestamp() - now))
    except (TypeError, ValueError, OverflowError):
        return _seconds(header)


class RedditCollector:
    def __init__(self, credentials_provider, *, opener=_open):
        self._credentials_provider = credentials_provider
        self._opener = opener
        self._credentials = None
        self._token = ""
        self._expires_at = 0
        self.retry_at = 0

    def _request(self, url, authorization, now, *, data=None):
        if now < self.retry_at:
            raise RedditRateLimited(self.retry_at)
        headers = {
            "Authorization": authorization, "Accept": "application/json",
            "User-Agent": f"macos:com.openai.kraken-paper:v0.3.0 (by /u/{self._credentials.username})",
        }
        request = Request(url, data=data, headers=headers)
        try:
            with self._opener(request, 12) as response:
                remaining = response.headers.get("X-Ratelimit-Remaining")
                if remaining is not None and remaining.strip() in {"0", "0.0"}:
                    self.retry_at = now + (_seconds(response.headers.get("X-Ratelimit-Reset")) or 60)
                raw = response.read(2_000_001)
        except HTTPError as exc:
            code = exc.code
            retry = max(_retry_after(exc.headers.get("Retry-After"), now),
                        _seconds(exc.headers.get("X-Ratelimit-Reset")))
            exc.close()
            if code == 429 or (code == 503 and retry):
                self.retry_at = now + (retry or 900)
                raise RedditRateLimited(self.retry_at) from None
            if code in (400, 401, 403) and (code != 400 or data is not None):
                self._token = ""
                raise RedditNotConfigured(f"Reddit OAuth/access denied (HTTP {code}). {AWAITING_AUTHORIZATION}") from None
            raise RedditCollectionError(f"Reddit HTTP {code}") from None
        except (TimeoutError, URLError, OSError) as exc:
            raise RedditCollectionError(f"Reddit request failed ({type(exc).__name__})") from None
        try:
            if len(raw) > 2_000_000:
                raise ValueError
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, RecursionError):
            raise RedditCollectionError("Reddit returned invalid JSON") from None
        return payload

    def _authenticate(self, now):
        credentials = self._credentials_provider()
        if credentials is None:
            raise RedditNotConfigured(AWAITING_AUTHORIZATION)
        if credentials != self._credentials:
            self._credentials = credentials
            self._token = ""
        if self._token and now < self._expires_at:
            return
        pair = f"{credentials.client_id}:{credentials.client_secret}".encode()
        basic = base64.b64encode(pair).decode("ascii")
        payload = self._request(_TOKEN_URL, f"Basic {basic}", now, data=b"grant_type=client_credentials")
        token = payload.get("access_token")
        lifetime = _seconds(payload.get("expires_in"))
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/=-]+", token) or not lifetime:
            raise RedditNotConfigured(AWAITING_AUTHORIZATION)
        self._token, self._expires_at = token, now + max(1, lifetime - 30)

    def _listing(self, path, params, now):
        url = f"{_API}{path}?{urlencode(params)}"
        payload = self._request(url, f"Bearer {self._token}", now)
        data = payload.get("data")
        if payload.get("kind") != "Listing" or not isinstance(data, dict) or not isinstance(data.get("children"), list):
            raise RedditCollectionError("Reddit returned an invalid listing")
        children = []
        for child in data["children"]:
            if not isinstance(child, dict) or child.get("kind") != "t3" or not isinstance(child.get("data"), dict):
                raise RedditCollectionError("Reddit returned an invalid post")
            children.append(child["data"])
        cursor = data.get("after")
        if cursor is not None and (not isinstance(cursor, str) or not re.fullmatch(r"t3_[a-z0-9]{1,16}", cursor)):
            raise RedditCollectionError("Reddit returned an invalid listing cursor")
        return children, cursor

    def fetch(self, subreddits: tuple[str, ...], now: int, *, since: int | None = None) -> list[RedditPost]:
        if not subreddits:
            return []
        if any(not _SUBREDDIT.fullmatch(name) for name in subreddits):
            raise ValueError("Invalid subreddit name")
        self._authenticate(now)
        names = {name.casefold() for name in subreddits}
        path = "/r/" + "+".join(subreddits) + "/new"
        params = {"limit": 100, "raw_json": 1}
        posts = []
        for _ in range(10):
            children, cursor = self._listing(path, params, now)
            page = [_post(item, names, now) for item in children]
            page = [item for item in page if item is not None]
            posts.extend(page)
            if since is None or cursor is None or any(item.published_at < since for item in page):
                return posts
            params["after"] = cursor
        raise RedditCollectionError("Reddit listing exceeded the backfill limit")

    def removed_ids(self, ids: tuple[str, ...], now: int) -> set[str]:
        if not ids:
            return set()
        self._authenticate(now)
        removed = set()
        for index in range(0, len(ids), 50):
            batch = set(ids[index:index + 50])
            children, _ = self._listing("/api/info", {
                "id": ",".join(f"t3_{identity}" for identity in sorted(batch)), "raw_json": 1,
            }, now)
            visible = {post.get("id") for post in children if not _removed(post)}
            removed.update(batch - visible)
        return removed


def _removed(item: dict) -> bool:
    return bool(item.get("removed_by_category") or item.get("selftext") in ("[deleted]", "[removed]"))


def _post(item, names, now) -> RedditPost | None:
    identity, subreddit = item.get("id"), item.get("subreddit")
    title, path = item.get("title"), item.get("permalink")
    if not isinstance(identity, str) or not _POST_ID.fullmatch(identity):
        return None
    if not isinstance(subreddit, str) or subreddit.casefold() not in names:
        return None
    if not isinstance(title, str) or not isinstance(path, str) or _removed(item):
        return None
    path_re = rf"/r/{re.escape(subreddit)}/comments/{identity}(?:/[A-Za-z0-9_%+-]+)?/?"
    if not re.fullmatch(path_re, path, re.IGNORECASE):
        return None
    created = item.get("created_utc")
    if type(created) not in (float, int) or not now - RETENTION_SECONDS <= created <= now + 300:
        return None
    title = " ".join(title.split())[:300]
    if not title:
        return None
    destination = item.get("url_overridden_by_dest") or item.get("url")
    article_url = canonical_article_url(destination) if isinstance(destination, str) and not item.get("is_self") else None
    host = (urlsplit(article_url).hostname or "") if article_url else ""
    if any(host == domain or host.endswith("." + domain) for domain in ("reddit.com", "redd.it", "redditmedia.com")):
        article_url = None
    return RedditPost(identity, subreddit, title, f"https://www.reddit.com{path}", int(created), now,
                      article_key(article_url))


def article_key(raw: str | None) -> str | None:
    if not raw or (url := canonical_article_url(raw)) is None:
        return None
    parts = urlsplit(url)
    return urlunsplit(("https", parts.hostname, parts.path.rstrip("/") or "/", parts.query, ""))


def group_discussions(posts) -> tuple[dict, ...]:
    groups = {}
    for post in posts:
        key = post.article_url or post.permalink
        group = groups.setdefault(key, {"kind": COMMUNITY_DISCUSSION, "article_url": post.article_url, "posts": []})
        group["posts"].append(asdict(post))
    return tuple(groups.values())
