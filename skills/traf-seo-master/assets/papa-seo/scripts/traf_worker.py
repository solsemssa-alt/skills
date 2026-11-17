"""Run the vendored read-only audit with Master scope and request limits."""

from collections import Counter
from html.parser import HTMLParser
import ipaddress
import json
from pathlib import Path
import re
import sys
from urllib.parse import urljoin

# The parent starts an isolated interpreter. Only this verified vendor bundle
# supplies the audit, its parser modules, and its catalogues.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit
import http_safe
import robots_check


COLLECTOR = {"name": "papa-seo", "ignores_star": False, "obeys_robots": True}


def audit_origins(urls, site_sample):
    origins = set()
    for url in urls:
        target = http_safe.validate_url(url)
        origins.add(target.origin)
        if (target.scheme, target.port) not in (("http", 80), ("https", 443)):
            continue
        hosts = {target.host}
        try:
            ipaddress.ip_address(target.host)
        except ValueError:
            if site_sample and "." in target.host:
                alias = target.host[4:] if target.host.startswith("www.") else "www." + target.host
                hosts.add(alias)
        for host in hosts:
            origins.update({("http", host, 80), ("https", host, 443)})
    return origins


class ScopedFetcher(http_safe.Fetcher):
    def __init__(self, *, origins, budget, **options):
        super().__init__(before_hop=self.check_hop, **options)
        self.origins = origins
        self.budget = budget
        self.attempts = 0
        self.denied = Counter()
        self.robots_responses = {}
        self.robots_policy = {}
        self.reading_robots = False

    def refuse(self, code, explanation):
        self.denied[code] += 1
        raise http_safe.SafeHttpError(code, explanation)

    def check_hop(self, target):
        if target.origin not in self.origins:
            self.refuse("outside_scope", "Request would leave the selected site origins")
        if self.attempts >= self.budget:
            self.refuse("request_budget", "The audit request budget has been reached")
        if not self.reading_robots:
            if target.origin not in self.robots_policy:
                self.robots_policy[target.origin] = robots_check.fetch_robots(
                    target.url, self, allow_local=self.allow_local)
            robots, _ = self.robots_policy[target.origin]
            if (robots.state == "unreachable" or robots.status in (None, 401, 403, 429)
                    or robots.parsed.get("truncated")):
                self.refuse("robots_unavailable", "A usable robots.txt policy could not be read")
            if not robots.check(COLLECTOR, target.url)["allowed"]:
                self.refuse("robots_disallowed", "robots.txt blocks the audit collector on this path")
            if self.attempts >= self.budget:
                self.refuse("request_budget", "The audit request budget has been reached")
        self.attempts += 1

    def fetch(self, url, **options):
        # Both the native report and the per-hop gate consume the same response.
        target = http_safe.validate_url(url, allow_local=self.allow_local)
        if target.request_path != "/robots.txt":
            return super().fetch(url, **options)
        if target.origin not in self.robots_responses:
            self.reading_robots = True
            try:
                self.robots_responses[target.origin] = super().fetch(url, **options)
            finally:
                self.reading_robots = False
        return self.robots_responses[target.origin]


class SourceQuotes(HTMLParser):
    """Retain original tag lexemes and line numbers for direct findings."""

    def __init__(self, raw):
        super().__init__(convert_charrefs=True)
        self.tags = {}
        self.feed(raw)
        self.close()

    def handle_starttag(self, tag, attributes):
        if tag not in {"html", "meta", "link", "title", "h1", "a", "img", "script"}:
            return
        candidates = self.tags.setdefault(tag, [])
        if len(candidates) < 2000:
            candidates.append((dict(attributes), self.get_starttag_text(), self.getpos()[0]))

    def match(self, tag, selector, target):
        for attributes, text, line in self.tags.get(tag, []):
            if selector(attributes):
                return {"kind": "html", "text": text[:512], "line": line, "target": target,
                        "truncated": len(text) > 512}
        return None


def tag_value(attributes, key):
    return (attributes.get(key) or "").casefold()


def contains_directive(value, directive):
    return directive in re.split(r"[^a-z0-9_-]+", value.casefold())


def quote_for(row, source, page):
    check = row["id"]
    target = page.get("url") or page["target"]
    if check == "access.noindex_header":
        headers = page.get("facts", {}).get("page", {}).get("headers", {})
        if headers.get("x-robots-tag"):
            return {"kind": "http-header", "target": target,
                    "text": "X-Robots-Tag: " + headers["x-robots-tag"][:512]}
    if check == "access.noindex_meta":
        return source.match("meta", lambda a: tag_value(a, "name") in ("robots", "googlebot", "yandex", "yandexbot", "bingbot")
                            and any(contains_directive(tag_value(a, "content"), value)
                                    for value in ("noindex", "none")), target)
    if check.startswith("access.canonical_") and check not in ("access.canonical_present", "access.canonical_target_status"):
        return source.match("link", lambda a: "canonical" in tag_value(a, "rel").split(), target)
    if check == "markup.og_image_alt":
        return source.match("meta", lambda a: tag_value(a, "property") == "og:image"
                            and bool(tag_value(a, "content").strip()), target)
    if check == "links.nofollow_internal":
        examples = (row.get("evidence") or "").split("; ")
        return source.match("a", lambda a: "nofollow" in tag_value(a, "rel").split()
                            and bool(a.get("href")) and urljoin(target, a["href"]) in examples, target)
    if check in ("meta.title_count", "meta.title_length", "meta.title_stuffing", "meta.title_in_head"):
        return source.match("title", lambda _: True, target)
    if check in ("meta.h1_count", "meta.h1_is_logo", "meta.h1_empty_or_image"):
        return source.match("h1", lambda _: True, target)
    if check in ("markup.jsonld_parses", "markup.jsonld_type", "markup.jsonld_context"):
        return source.match("script", lambda a: tag_value(a, "type") == "application/ld+json", target)
    if check in ("mobile.viewport_zoom_blocked", "mobile.viewport_fixed_width", "mobile.viewport_device_width"):
        return source.match("meta", lambda a: tag_value(a, "name") == "viewport", target)
    # An absent tag, derived count, inventory, or network fact needs its own
    # evidence type rather than an invented HTML quotation.
    return None


def qualify_page(page, raw, index_intent):
    optional = {"markup.og_required", "markup.jsonld_present", "meta.favicon_declared"}
    noindex = {"access.noindex_meta", "access.noindex_header"}
    source = SourceQuotes(raw)
    metadata = {(tag_value(a, "property") or tag_value(a, "name")): tag_value(a, "content").strip()
                for a, _, _ in source.tags.get("meta", [])}
    og_declared = any(key.startswith("og:") for key in metadata)
    twitter_declared = any(key.startswith("twitter:") for key in metadata)
    for row in page.get("rows", []):
        row["master_diagnostic"] = True
        check, verdict = row["id"], row["verdict"]
        if verdict not in (audit.BAD, audit.WARN):
            continue
        needs_og_image = check.startswith("markup.og_image_") and not metadata.get("og:image")
        needs_og = check.startswith("markup.og_") and not og_declared
        needs_twitter = check.startswith("markup.twitter_") and not twitter_declared
        needs_twitter_image = check == "markup.twitter_image_alt" and not (
            metadata.get("twitter:image") or metadata.get("og:image"))
        if ((verdict == audit.BAD and check in optional) or needs_og_image or needs_og
                or needs_twitter or needs_twitter_image):
            row.update(verdict=audit.INFO, verdict_label=audit.VERDICT_RU[audit.INFO],
                       fix="Сверьте применимость предпросмотра или разметки для этой страницы.")
        elif check == "markup.og_image_alt":
            row["fix"] = "Задайте og:image:alt с описанием опубликованного изображения."
        elif verdict == audit.BAD and check == "mobile.viewport_present":
            row.update(verdict=audit.WARN, verdict_label=audit.VERDICT_RU[audit.WARN],
                       observation="В исходном HTML отсутствует meta viewport.",
                       fix="Проверьте отображение в мобильном браузере.",
                       severity="low", severity_by_context=True)
        if check in noindex and verdict == audit.BAD:
            if index_intent == "exclude":
                row.update(verdict=audit.INFO, verdict_label=audit.VERDICT_RU[audit.INFO],
                           fix="Директива соответствует указанному исключению URL из поиска.")
            elif index_intent == "unknown":
                row.update(verdict=audit.WARN, verdict_label=audit.VERDICT_RU[audit.WARN],
                           fix="Сверить назначение URL. Снять noindex для страницы, предназначенной для поиска.")
        quote = quote_for(row, source, page)
        if quote is not None:
            row["source_quote"] = quote
        else:
            if check.startswith(("access.final_status", "access.redirect", "speed.ttfb")):
                row["evidence_kind"] = "HTTP-ответ или заголовки"
            elif check.startswith(("access.robots", "access.sitemap", "ai.llms_txt")):
                row["evidence_kind"] = "отдельный ресурс или выборка адресов"
            elif check.startswith("content."):
                row["evidence_kind"] = "расчёт по извлечённому тексту"
            else:
                row["evidence_kind"] = "наблюдение по разобранной структуре HTML"


def main(path):
    request_path = Path(path)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    fetcher = None

    def factory(**options):
        nonlocal fetcher
        fetcher = ScopedFetcher(origins=audit_origins(request["urls"], request["site_sample"]),
                                budget=request["request_budget"], **options)
        return fetcher

    try:
        return audit.main(request["argv"], fetcher_factory=factory,
                          page_policy=lambda page, raw: qualify_page(page, raw, request["index_intent"]))
    finally:
        with request_path.with_name("collection-state.json").open("x", encoding="utf-8") as output:
            json.dump({"requests": fetcher.requests_made if fetcher else 0,
                       "attempts": fetcher.attempts if fetcher else 0,
                       "denied": dict(fetcher.denied) if fetcher else {}}, output)
            output.write("\n")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
