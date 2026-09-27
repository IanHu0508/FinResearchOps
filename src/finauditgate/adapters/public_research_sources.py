"""Bounded public A-share news/discussion acquisition, separate from research.

No model, browser session, private cookie, redirect, retry or PDF dependency is
used. Public search excerpts and issuer replies retain their distinct meanings.
"""

from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex, write_once
from finauditgate.private_storage import require_private_storage_root


MAX_REQUESTS = 12
TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 2_000_000
MAX_ITEM_CHARACTERS = 4000
MAX_PER_CHANNEL = 3
SEARCH_HOST = "search-api-web.eastmoney.com"
FORUM_HOST = "guba.eastmoney.com"
USER_AGENT = "FinResearchOpsPublicResearch/1.0"
SHANGHAI = timezone(timedelta(hours=8))


def _fetch_url_allowed(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in (SEARCH_HOST, FORUM_HOST)
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.fragment):
        return False
    if parsed.path == "/robots.txt":
        return not parsed.query
    if parsed.hostname == SEARCH_HOST:
        return parsed.path == "/search/jsonp"
    return not parsed.query and bool(re.fullmatch(r"/(?:list,\d{6},f|news,\d{6},\d+)\.html", parsed.path))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _default_fetch(url, *, timeout, max_bytes):
    """Injectable transport: return status/body(bytes)/headers/url, no retries."""
    if not _fetch_url_allowed(url):
        raise ValueError("PUBLIC_SOURCE_URL_FORBIDDEN")
    try:
        response = build_opener(_NoRedirect()).open(
            Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout)
    except HTTPError as exc:
        response = exc
    with response:
        return {"status": response.status, "body": response.read(max_bytes + 1),
                "headers": dict(response.headers.items()), "url": response.geturl()}


class _Plain(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("p", "br", "div", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _plain(value):
    if not isinstance(value, str):
        raise ValueError("SOURCE_TEXT_REQUIRED")
    parser = _Plain()
    parser.feed(value)
    return "".join(parser.parts).strip()


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("PUBLICATION_TIME_MISSING")
    # Only the timestamp shape actually returned by these public sources.
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)


def _public_reference(url):
    if not isinstance(url, str):
        return False
    try:
        value = urlsplit(url)
        return (value.scheme in ("http", "https") and not value.username and not value.password
                and value.port in (None, 80, 443) and value.hostname in (
                    FORUM_HOST, "finance.eastmoney.com", "stock.eastmoney.com", "hk.eastmoney.com"))
    except ValueError:
        return False


def _json_search(text, kind):
    match = re.fullmatch(r"sourceprobe\((.*)\)\s*;?", text.strip(), re.S)
    value = json.loads(match[1] if match else text)
    if not isinstance(value, dict) or value.get("code") != 0 or value.get("bizCode"):
        raise ValueError("SEARCH_NOT_AVAILABLE")
    result = value.get("result")
    rows = result.get(kind) if isinstance(result, dict) else None
    if not isinstance(rows, list):
        raise ValueError("SEARCH_ROWS_MISSING")
    return rows


def _embedded(text, name):
    match = re.search(r"\bvar\s+" + re.escape(name) + r"\s*=\s*", text)
    if not match:
        raise ValueError("PUBLIC_EMBEDDED_JSON_MISSING")
    return json.JSONDecoder().raw_decode(text[match.end():])[0]


def _search_url(code, company, start, end, kind):
    if kind == "media":
        tag, keyword, version = "cmsArticleWebOld", code, "curr"
        options = {"searchScope": "default", "sort": "time", "pageIndex": 1,
                   "pageSize": 20, "preTag": "", "postTag": ""}
    else:
        tag, keyword, version = "wenDongMiWeb", company, "9.8"
        options = {"webSearchScope": 3 if kind == "release" else 4,
            "pageindex": 1, "pagesize": 20, "startTime": start + " 00:00:00",
            "endTime": end + " 23:59:59", "sortOrder": 2,
            "preTag": "", "postTag": "", "gubaId": code}
    payload = {"uid": "", "keyword": keyword, "type": [tag], "client": "web",
               "clientType": "web", "clientVersion": version, "param": {tag: options}}
    return "https://" + SEARCH_HOST + "/search/jsonp?" + urlencode({
        "cb": "sourceprobe", "param": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))})


class _Acquisition:
    def __init__(self, base, output_root, fetch):
        self.base, self.root, self.fetch = base, output_root, fetch
        self.requests, self.rows = [], []
        self.robots, self.blocked = {}, {}
        self.used_ids = {r.get("id") for r in base.get("sources", []) if isinstance(r, dict)}
        self.seen = set()
        self.channels = {k: {"status": "PARTIAL", "count": 0, "items": [],
                            "gaps": [], "rejections": [], "routes": []} for k in ("news", "social")}

    def gap(self, channel, reason):
        if reason not in self.channels[channel]["gaps"]:
            self.channels[channel]["gaps"].append(reason)

    def request(self, url, channel, route, *, robots=False):
        host = urlsplit(url).hostname
        if not _fetch_url_allowed(url):
            self.gap(channel, "URL_FORBIDDEN")
            return None
        if host in self.blocked:
            self.gap(channel, self.blocked[host])
            return None
        if not robots and host not in self.robots:
            check = self.request("https://" + host + "/robots.txt", channel, "robots", robots=True)
            if check is None:
                self.blocked[host] = "ROBOTS_UNAVAILABLE"
                return None
            if check[1]["status"] == 404:
                self.robots[host] = None
            elif check[1]["status"] == 200 and re.search(r"(?im)^\s*user-agent\s*:", check[0]):
                parser = RobotFileParser()
                parser.parse(check[0].splitlines())
                self.robots[host] = parser
            else:
                self.blocked[host] = "ROBOTS_UNCLEAR_OR_RESTRICTED"
                self.gap(channel, self.blocked[host])
                return None
        rule = self.robots.get(host)
        if not robots and rule is not None and not rule.can_fetch(USER_AGENT, url):
            self.gap(channel, "ROBOTS_DISALLOW")
            return None
        if len(self.requests) >= MAX_REQUESTS:
            self.gap(channel, "REQUEST_LIMIT")
            return None
        number = len(self.requests) + 1
        receipt = {"number": number, "url": url, "channel": channel, "route": route,
            "method": "GET", "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "timeout_seconds": TIMEOUT_SECONDS, "max_bytes": MAX_RESPONSE_BYTES,
            "redirects_followed": 0, "retries": 0, "cookies_supplied": False}
        self.requests.append(receipt)
        try:
            value = self.fetch(url, timeout=TIMEOUT_SECONDS, max_bytes=MAX_RESPONSE_BYTES)
            body, status = value["body"], value["status"]
            if not isinstance(body, bytes) or type(status) is not int:
                raise ValueError("FETCH_RESPONSE_INVALID")
            receipt.update(status=status, bytes_received=len(body),
                           body_sha256=sha256_hex(body[:MAX_RESPONSE_BYTES]))
            filename = f"http-{number:02d}.body"
            write_once(self.root / filename, body[:MAX_RESPONSE_BYTES])
            receipt["raw_file"] = filename
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("RESPONSE_TOO_LARGE")
            if value.get("url", url) != url:
                raise ValueError("REDIRECT_FORBIDDEN")
            if status in (401, 403, 429):
                self.blocked[host] = "HTTP_ACCESS_RESTRICTED"
                raise ValueError("HTTP_ACCESS_RESTRICTED")
            if status != 200 and not (robots and status == 404):
                raise ValueError("HTTP_STATUS_" + str(status))
            if robots and status == 404:
                return "", receipt
            text = body.decode("utf-8-sig", errors="strict")
            title = re.search(r"<title[^>]*>(.*?)</title>", text[:6000], re.I | re.S)
            if (title and re.search(r"验证码|安全验证|访问验证|访问受限|access denied|captcha", title[1], re.I)):
                self.blocked[host] = "PAGE_ACCESS_RESTRICTED"
                raise ValueError("PAGE_ACCESS_RESTRICTED")
            return text, receipt
        except Exception as exc:
            receipt["error"] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            self.gap(channel, route.upper() + ":" + receipt["error"])
            return None
        finally:
            write_once(self.root / f"http-{number:02d}.json", canonical_json_bytes(receipt))

    def accept(self, channel, kind, item, receipt):
        if self.channels[channel]["count"] >= MAX_PER_CHANNEL:
            return
        key = (kind, item["url"])
        if key in self.seen:
            return
        prefix = "AUTO_NEWS" if channel == "news" else "AUTO_SOCIAL"
        index = 1
        while f"{prefix}{index:02d}" in self.used_ids:
            index += 1
        identity = f"{prefix}{index:02d}"
        limits = {
            "media_news_index_excerpt": "媒体搜索索引片段，不是已获取的报道全文；同源转载不能算独立确认。",
            "company_event_index_excerpt": "公司发布/公告事件索引片段，不是独立媒体报道，也未声称取得公告全文；标题内活动日不能替代发布日期。",
            "public_investor_post": "单个公开投资者帖子，不代表全市场情绪；帖中判断不是公司事实，不使用事后累计互动数。",
            "investor_qa": "公开投资者问答：提问可能包含错误预设，公司答复是公司陈述，不是公众情绪或独立事实认证。",
        }[kind]
        texts = item.pop("texts")
        parts = ["自动公开资料 " + kind, "证券：" + self.symbol + "；公司：" + self.company,
                 "发布/可得时间：" + item["published_at"], "原URL：" + item["url"],
                 "标题：" + item.get("title", ""), "来源：" + item.get("publisher", "东方财富公开页面")]
        truncated = False
        for label, text in texts:
            value = text[:MAX_ITEM_CHARACTERS]
            truncated = truncated or len(text) > MAX_ITEM_CHARACTERS
            parts += [label + "：", value]
        if item.get("question_published_at"):
            parts += ["提问时间：" + item["question_published_at"], "公司答复时间：" + item["answer_published_at"]]
        parts += ["覆盖与用途限制：" + limits,
                  "本次按固定单页/最多三条有界采样，不能推断完整覆盖；现在访问的历史标示内容未认证历史版本或后续编辑。",
                  "抓取时间：" + receipt["retrieved_at"],
                  "原HTTP响应SHA256：" + receipt["body_sha256"]]
        if truncated:
            parts.append("长内容仅保留有界片段；未展示全部正文，原响应留存供核对。")
        content = "\n".join(parts) + "\n"
        row = {"id": identity, "origin": kind + " | " + item["url"], "content": content,
               "sha256": sha256_hex(content.encode()), "use": "research",
               "availability_note": "按源内标示时间" + item["published_at"] + "筛选至" + self.end
                   + "；日期按上海时间处理。抓取于" + receipt["retrieved_at"] + "，历史vintage未认证。"}
        candidate = {**self.base, "sources": [*self.base.get("sources", []), *self.rows, row]}
        if len(canonical_json_bytes(candidate)) > 256 * 1024 or len(candidate["sources"]) > 48:
            self.gap(channel, "COMBINED_SOURCE_BUNDLE_LIMIT")
            return
        self.rows.append(row)
        self.used_ids.add(identity)
        self.seen.add(key)
        self.channels[channel]["count"] += 1
        self.channels[channel]["items"].append({**item, "id": identity, "source_type": kind,
            "retrieved_at": receipt["retrieved_at"], "response_sha256": receipt["body_sha256"],
            "source_sha256": row["sha256"], "content_truncated": truncated})

    def rejection(self, channel, route, item, reason):
        self.channels[channel]["rejections"].append({"route": route,
            "source_id": str(item.get("id", item.get("post_id", item.get("code", "")))) if isinstance(item, dict) else "",
            "reason": reason})

    def timestamp(self, value):
        parsed = _stamp(value)
        if not self.start <= parsed.date().isoformat() <= self.end:
            raise ValueError("OUTSIDE_DATE_WINDOW")
        return parsed.isoformat()

    def search(self, channel, route):
        self.channels[channel]["routes"].append(route)
        fetched = self.request(_search_url(self.code, self.company, self.start, self.end, route), channel, route)
        if fetched is None:
            return
        text, receipt = fetched
        try:
            rows = _json_search(text, "cmsArticleWebOld" if route == "media" else "wenDongMiWeb")
        except (ValueError, KeyError, TypeError) as exc:
            self.gap(channel, route.upper() + ":" + str(exc))
            return
        for item in rows[:20]:
            if self.channels[channel]["count"] >= MAX_PER_CHANNEL:
                break
            try:
                if not isinstance(item, dict) or not _public_reference(item.get("url")):
                    raise ValueError("SOURCE_URL_INVALID")
                title, body = _plain(item.get("title")), _plain(item.get("content"))
                if route == "media":
                    if (self.company not in title + body
                            and re.search(r"(?<!\d)" + re.escape(self.code) + r"(?!\d)", title + body) is None):
                        raise ValueError("OTHER_ISSUER")
                    published = self.timestamp(item.get("date"))
                    metadata = {"url": item["url"], "title": title, "published_at": published,
                                "publisher": str(item.get("mediaName") or "媒体索引未标发布者"), "texts": [("报道索引片段", body)]}
                    kind = "media_news_index_excerpt"
                else:
                    if str(item.get("gubaId")) != self.code:
                        raise ValueError("OTHER_ISSUER")
                    expected_type = "3" if route == "release" else "1"
                    if item.get("type") != expected_type:
                        raise ValueError("OTHER_SOURCE_TYPE")
                    published = self.timestamp(item.get("responseTime"))
                    metadata = {"url": item["url"], "title": title, "published_at": published,
                                "publisher": "公司公开发布" if route == "release" else "公开投资者提问及公司答复"}
                    if route == "release":
                        created = self.timestamp(item.get("createTime"))
                        if created > published:
                            raise ValueError("PUBLICATION_TIME_CONFLICT")
                        metadata["texts"] = [("公司发布索引片段（非公告全文）", body)]
                        kind = "company_event_index_excerpt"
                    else:
                        question = self.timestamp(item.get("createTime"))
                        if question > published:
                            raise ValueError("ANSWER_PRECEDES_QUESTION")
                        metadata.update(question_published_at=question, answer_published_at=published,
                                        texts=[("投资者提问（未经验证的问句）", title), ("公司答复（公司陈述）", body)])
                        kind = "investor_qa"
                if not title or not body:
                    raise ValueError("SOURCE_TEXT_EMPTY")
                self.accept(channel, kind, metadata, receipt)
            except (ValueError, KeyError, TypeError) as exc:
                self.rejection(channel, route, item, str(exc))

    def forum(self):
        channel, route = "social", "forum"
        self.channels[channel]["routes"].append(route)
        fetched = self.request("https://" + FORUM_HOST + "/list," + self.code + ",f.html", channel, route)
        if fetched is None:
            return
        try:
            items = _embedded(fetched[0], "article_list")["re"]
            if not isinstance(items, list):
                raise ValueError("FORUM_ROWS_MISSING")
        except (ValueError, KeyError, TypeError) as exc:
            self.gap(channel, "FORUM:" + str(exc))
            return
        candidates = []
        for item in items[:100]:
            try:
                if item.get("stockbar_code") != self.code or item.get("post_type") != 0:
                    raise ValueError("OTHER_ISSUER_OR_SOURCE_TYPE")
                self.timestamp(item.get("post_publish_time"))
                if type(item.get("post_id")) is not int or item["post_id"] <= 0:
                    raise ValueError("POST_ID_INVALID")
                candidates.append(item)
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                self.rejection(channel, route, item, str(exc))
        candidates.sort(key=lambda x: (x["post_publish_time"], x["post_id"]), reverse=True)
        for candidate in candidates[:MAX_PER_CHANNEL]:
            url = f"https://{FORUM_HOST}/news,{self.code},{candidate['post_id']}.html"
            detail = self.request(url, channel, "forum_detail")
            if detail is None:
                continue
            try:
                post = _embedded(detail[0], "post_article")
                if (post.get("post_id") != candidate["post_id"] or post.get("post_type") != 0
                        or post.get("post_guba", {}).get("stockbar_code") != self.code):
                    raise ValueError("POST_IDENTITY_CHANGED")
                published = self.timestamp(post.get("post_publish_time"))
                if post["post_publish_time"] != candidate["post_publish_time"]:
                    raise ValueError("POST_PUBLICATION_CHANGED")
                body, title = _plain(post.get("post_content")), _plain(post.get("post_title"))
                if not body or not title:
                    raise ValueError("SOURCE_TEXT_EMPTY")
                self.accept(channel, "public_investor_post", {"url": url, "title": title,
                    "published_at": published, "publisher": "公开股吧用户：" + str(post.get("post_user", {}).get("user_nickname", "未提供")),
                    "texts": [("原帖子正文或有界片段（用户言论）", body)]}, detail[1])
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                self.rejection(channel, "forum_detail", candidate, str(exc))

    def collect(self):
        self.symbol = self.base.get("symbol", "")
        self.company = self.base.get("identity", {}).get("company_short_name") or self.base.get("company_short_name")
        try:
            if not re.fullmatch(r"\d{6}\.(?:SZ|SH)", self.symbol) or not isinstance(self.company, str) or not self.company.strip():
                raise ValueError("A_SHARE_IDENTITY_REQUIRED")
            if len(self.company) > 100:
                raise ValueError("COMPANY_NAME_TOO_LONG")
            end = date.fromisoformat(self.base["as_of"])
            self.code = self.symbol[:6]
            self.start, self.end = (end - timedelta(days=13)).isoformat(), end.isoformat()
            self.search("news", "media")
            self.forum()
            if self.channels["news"]["count"] < MAX_PER_CHANNEL:
                self.search("news", "release")
            if self.channels["social"]["count"] < MAX_PER_CHANNEL:
                self.search("social", "qa")
        except (ValueError, KeyError, TypeError) as exc:
            for channel in self.channels:
                self.gap(channel, str(exc))
        for channel in self.channels:
            item = self.channels[channel]
            item["status"] = "COMPLETED" if item["count"] == MAX_PER_CHANNEL else "PARTIAL"
            if item["count"] < MAX_PER_CHANNEL:
                self.gap(channel, "BOUNDED_SAMPLE_INCOMPLETE_NOT_PROOF_OF_NO_NEWS_OR_DISCUSSION")
        return {"rows": self.rows, "channels": self.channels, "requests": self.requests,
            "limits": {"max_requests": MAX_REQUESTS, "timeout_seconds": TIMEOUT_SECONDS,
                "max_response_bytes": MAX_RESPONSE_BYTES, "max_item_characters": MAX_ITEM_CHARACTERS,
                "max_per_channel": MAX_PER_CHANNEL, "window_days": 14,
                "window_start": getattr(self, "start", None), "window_end": getattr(self, "end", None),
                "timezone": "Asia/Shanghai", "pagination": "ONE_PAGE_PER_ROUTE",
                "retries": 0, "automatic_redirects": False, "historical_vintage_verified": False}}


def collect_sources(base_bundle, output_root, *, fetch=None):
    """Acquire up to three rows per channel without mutating the frozen bundle.

    ``fetch(url, timeout=15, max_bytes=2000000)`` returns a mapping with
    ``status`` (int), ``body`` (bytes), optional ``headers`` and ``url``.
    Source failures become visible gaps; private-storage violations remain hard
    errors before any request. The caller owns the aggregate acquisition receipt.
    """
    root = require_private_storage_root(Path(output_root), purpose="public-research-sources")
    root.mkdir(parents=True, exist_ok=True)
    return _Acquisition(base_bundle, root, _default_fetch if fetch is None else fetch).collect()
