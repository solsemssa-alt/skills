"""Run the vendored read-only audit with Master scope and request limits."""

from collections import Counter
import ipaddress
import json
from pathlib import Path
import sys

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


def qualify_optional_findings(page):
    previews = {"markup.og_required", "markup.jsonld_present", "meta.favicon_declared"}
    for row in page.get("rows", []):
        if row["verdict"] != audit.BAD:
            continue
        if row["id"] in previews:
            row.update(verdict=audit.INFO, verdict_label=audit.VERDICT_RU[audit.INFO],
                       fix="Проверьте, нужна ли эта возможность данному типу страницы.")
        elif row["id"] == "mobile.viewport_present":
            row.update(verdict=audit.WARN, verdict_label=audit.VERDICT_RU[audit.WARN],
                       observation="В исходном HTML отсутствует meta viewport.",
                       fix="Проверьте отображение в мобильном браузере.",
                       severity="low", severity_by_context=True)


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
        return audit.main(request["argv"], fetcher_factory=factory, page_policy=qualify_optional_findings)
    finally:
        with request_path.with_name("collection-state.json").open("x", encoding="utf-8") as output:
            json.dump({"requests": fetcher.requests_made if fetcher else 0,
                       "attempts": fetcher.attempts if fetcher else 0,
                       "denied": dict(fetcher.denied) if fetcher else {}}, output)
            output.write("\n")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
