#!/usr/bin/env python3
"""Static package checks, project-local install, and read-only audit commands."""

import argparse
import ctypes
from datetime import datetime, timezone
import hashlib
from html import escape
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit


PACKAGE_NAME = "traf-seo-master"
CORE = ("SKILL.md", "scripts/seo-master.py", "references/capabilities.json",
        "references/upstreams.json")
SITE_CORE = ("scripts/site-audits.py", "references/checks.json",
             "references/source-coverage.json")
HOSTS = {"codex": ".agents", "claude": ".claude", "cursor": ".cursor",
         "copilot": ".github", "gemini": ".gemini", "openclaw": ".agents"}
LIGHTHOUSE_VERSION = "13.6.0"
PAPA_COMMIT = "8307ac54dfb4fbc76332e8aadeb7cd8ea4a529ea"
PAPA_ROOT = "assets/papa-seo"
CATEGORIES = ("performance", "seo", "accessibility", "best-practices")


def emit(value):
    print(json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False))


def plain_text(value):
    return isinstance(value, str) and bool(value.strip())


def reject_non_json_constant(value):
    raise ValueError("non-JSON numeric constant: " + value)


def finite_json_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON number exceeds finite float range")
    return number


def no_symlink_components(path):
    """Check existing ancestors without resolving away a symlink first."""
    path = Path(path)
    if not path.is_absolute():
        path = Path.cwd() / path
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("symlink path is not allowed: " + str(part))
    return Path(os.path.abspath(path))


def snapshot(root):
    """Include directories and file modes, rejecting links and special files."""
    root = no_symlink_components(root)
    if not root.is_dir():
        raise ValueError("package directory does not exist: " + str(root))
    result = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        relative = path.relative_to(root).as_posix()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("symlink is not allowed in package: " + relative)
        if stat.S_ISDIR(info.st_mode):
            result[relative] = {"kind": "directory"}
        elif stat.S_ISREG(info.st_mode):
            result[relative] = {"kind": "file", "size": info.st_size,
                                "mode": stat.S_IMODE(info.st_mode),
                                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        else:
            raise ValueError("special file is not allowed: " + relative)
    return result


def load_catalogue(package, name, array, errors):
    path = package / "references" / name
    try:
        value = json.loads(path.read_text(encoding="utf-8"),
                           parse_constant=reject_non_json_constant, parse_float=finite_json_float)
    except (OSError, UnicodeError, ValueError) as exc:
        errors.append("cannot read " + name + ": " + str(exc))
        return []
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
        errors.append(name + " requires schema_version 1")
        return []
    if not isinstance(value.get(array), list) or not value[array]:
        errors.append(name + " requires a nonempty " + array + " array")
        return []
    return value[array]


def site_audit_module():
    spec = importlib.util.spec_from_file_location(
        "traf_seo_site_audits", Path(__file__).parent / "site-audits.py")
    module = importlib.util.module_from_spec(spec)
    previous_bytecode = sys.dont_write_bytecode
    try:
        # A portable package is immutable input, including during doctor/install.
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous_bytecode
    return module


def papa_runtime(package):
    root = package / PAPA_ROOT
    manifest = snapshot(root)
    origin = json.loads((root / "origin.json").read_text(encoding="utf-8"))
    files = {name: row["sha256"] for name, row in manifest.items()
             if row["kind"] == "file" and name != "origin.json"}
    if origin.get("commit") != PAPA_COMMIT or files != origin.get("files_sha256"):
        raise ValueError("pinned papa-seo runtime files do not match origin.json")
    return root, origin


def papa_parameter_count(package, checks):
    registry_path = package / PAPA_ROOT / "references/registry.json"
    registry = read_audit_object(registry_path)["params"]
    crosswalk = read_audit_object(package / "references/papa-seo-map.json")
    if crosswalk["upstream_registry_sha256"] != hashlib.sha256(registry_path.read_bytes()).hexdigest():
        raise ValueError("papa-seo crosswalk registry hash differs")
    expected = {item["id"]: item for item in registry}
    rows = crosswalk["parameters"]
    if len(rows) != len(expected) or {row["id"] for row in rows} != set(expected):
        raise ValueError("papa-seo crosswalk needs exactly one row per parameter")
    for row in rows:
        if not row["master_ids"] or not set(row["master_ids"]) <= set(checks):
            raise ValueError("unresolved Master IDs for " + row["id"])
        source = expected[row["id"]]
        for field in ("scope", "signal", "source_kind"):
            if row[field] != source[field]:
                raise ValueError("papa-seo crosswalk changes source " + field + " for " + row["id"])
        if row["upstream_status"] != source["status"]:
            raise ValueError("papa-seo crosswalk changes source status for " + row["id"])
    return len(rows)


def check_package(package):
    package = Path(package)
    errors = []
    try:
        manifest = snapshot(package)
    except (OSError, ValueError) as exc:
        return {"status": "invalid", "errors": [str(exc)], "package": str(package)}, [], None
    for relative in CORE:
        if manifest.get(relative, {}).get("kind") != "file":
            errors.append("missing core file: " + relative)
    components = load_catalogue(package, "upstreams.json", "components", errors)
    source_ids = set()
    for component in components:
        if not isinstance(component, dict) or not plain_text(component.get("id")):
            errors.append("upstream requires a nonempty id")
            continue
        source_id = component["id"]
        if source_id in source_ids:
            errors.append("duplicate source id: " + source_id)
        source_ids.add(source_id)
        for field in ("url", "kind"):
            if not plain_text(component.get(field)):
                errors.append(source_id + " requires " + field)
        if plain_text(component.get("url")):
            parsed = urlsplit(component["url"])
            if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
                errors.append(source_id + " requires a public http/https source URL")
    capabilities = load_catalogue(package, "capabilities.json", "capabilities", errors)
    capability_ids = set()
    for capability in capabilities:
        if not isinstance(capability, dict) or not plain_text(capability.get("id")):
            errors.append("capability requires a nonempty id")
            continue
        cap_id = capability["id"]
        if cap_id in capability_ids:
            errors.append("duplicate capability id: " + cap_id)
        capability_ids.add(cap_id)
        for field in ("title", "implementation", "verification"):
            if field not in capability or capability[field] in (None, "", [], {}):
                errors.append(cap_id + " requires " + field)
        reference = capability.get("reference")
        if not plain_text(reference):
            errors.append(cap_id + " requires reference")
        else:
            parts = PurePosixPath(reference)
            if parts.is_absolute() or ".." in parts.parts or "\\" in reference:
                errors.append(cap_id + " reference escapes package: " + reference)
            elif manifest.get(parts.as_posix(), {}).get("kind") != "file":
                errors.append(cap_id + " missing reference: " + reference)
        bindings = capability.get("source_ids")
        if not isinstance(bindings, list) or not bindings:
            errors.append(cap_id + " requires nonempty source_ids")
        else:
            for source_id in bindings:
                if not isinstance(source_id, str) or source_id not in source_ids:
                    errors.append(cap_id + " has unknown source id: " + str(source_id))
    master = {}
    checks = {}
    version = None
    # Legacy fixture/packages keep their declared small surface. Any updated
    # entry, version or new core file requires the complete master graph.
    updated = any(relative in manifest for relative in SITE_CORE)
    try:
        updated = updated or b'"site-audit"' in (package / "scripts/seo-master.py").read_bytes()
        metadata = json.loads((package / "references/capabilities.json").read_text())
        version = metadata.get("package_version") if isinstance(metadata, dict) else None
        if type(version) is int and version >= 2:
            updated = True
        if isinstance(version, str) and all(part.isdigit() for part in version.split(".")):
            updated = updated or tuple(map(int, version.split("."))) >= (0, 2, 0)
    except (OSError, UnicodeError, ValueError):
        pass  # Existing core/catalogue diagnostics already retain these failures.
    if updated:
        absent = [relative for relative in SITE_CORE
                  if manifest.get(relative, {}).get("kind") != "file"]
        errors.extend("missing master core file: " + relative for relative in absent)
        if not absent:
            try:
                registry, source_coverage, checks, sources = site_audit_module().package_data(package)
                master = {"master_check_count": len(checks),
                          "source_disposition_count": len(sources),
                          "source_population_counts": source_coverage.get("source_population_counts", {}),
                          "source_populations_non_additive": source_coverage.get("populations_non_additive", True),
                          "registry_version": registry["registry_version"]}
            except (OSError, ValueError, UnicodeError, TypeError, KeyError) as exc:
                errors.append("invalid master graph: " + str(exc))
    if (type(version) is int and version >= 4) or any(
            name == PAPA_ROOT or name.startswith(PAPA_ROOT + "/") for name in manifest):
        try:
            _, origin = papa_runtime(package)
            master["quick_audit"] = {"upstream_version": origin["version"], "commit": origin["commit"],
                                     "mapped_parameters": papa_parameter_count(package, checks)}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append("invalid papa-seo runtime: " + str(exc))
    report = {"status": "invalid" if errors else "static-ready", "package": str(package),
              "capability_count": len(capabilities), "source_count": len(components),
              "authentication": "unverified", "host_runtime": "unverified", "errors": errors,
              **master}
    return report, capabilities, manifest


def place_exclusively(source, destination):
    """Atomic no-replace directory placement, including a concurrently created target."""
    if os.name == "nt":
        os.rename(source, destination)  # Windows rename refuses an existing target.
        return
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = rename(os.fsencode(source), os.fsencode(destination), 0x00000004)
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        rename = library.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                           ctypes.c_char_p, ctypes.c_uint]
        result = rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    else:
        raise ValueError("atomic no-replace install is unavailable on this platform")
    if result:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))


def install(args):
    report, _, expected = check_package(args.package)
    if report["errors"]:
        emit(report)
        return 1
    package = no_symlink_components(args.package)
    project = no_symlink_components(args.project)
    if not project.is_dir():
        raise ValueError("--project must be an existing project directory")
    target = no_symlink_components(project / HOSTS[args.host] / "skills" / PACKAGE_NAME)
    if target == package or package in target.parents or target in package.parents:
        raise ValueError("installation and source package must not overlap")
    if target.exists():
        if snapshot(target) != expected:
            raise ValueError("existing install differs; owner bytes were preserved: " + str(target))
        emit({"status": "already-identical", "host": args.host, "target": str(target),
              "host_runtime": "unverified"})
        return 0
    if args.dry_run:
        emit({"status": "dry-run", "host": args.host, "target": str(target),
              "host_runtime": "unverified"})
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    no_symlink_components(target.parent)
    stage = Path(tempfile.mkdtemp(prefix=".traf-seo-master-stage-", dir=target.parent))
    try:
        shutil.copytree(package, stage, dirs_exist_ok=True, symlinks=True)
        if snapshot(stage) != expected or snapshot(package) != expected:
            raise ValueError("package changed during staging; installation was not placed")
        no_symlink_components(target)
        place_exclusively(stage, target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    emit({"status": "installed", "host": args.host, "target": str(target),
          "file_count": sum(item["kind"] == "file" for item in expected.values()),
          "host_runtime": "unverified"})
    return 0


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=True, indent=2, allow_nan=False)
        handle.write("\n")


def validate_audit_url(url):
    if any(character.isspace() or ord(character) < 32 for character in url):
        raise ValueError("URL must not contain whitespace or control characters")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("audit requires an http/https URL with a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are not allowed")
    if parsed.query or parsed.fragment:
        raise ValueError("query and fragment URLs are excluded to avoid saving access tokens")
    try:
        parsed.port
    except ValueError:
        raise ValueError("URL contains an invalid port") from None


def valid_score(value):
    return type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value)


def normalize_lhr(raw):
    """Pure summary of upstream facts, interpreting scores by native display mode.

    Lighthouse 13.6 audit-result.d.ts defines manual, informative, notApplicable,
    and error as distinct from scored binary/numeric/metricSavings results.
    Its audit.js also emits informative scores of 1; retain that native value
    without interpreting informative results as a passed or failed check.
    """
    problems = []
    summary = {"schema_version": 1, "scope": "single-page-lighthouse-lab-audit",
               "categories": {}, "failures": [], "unknown_audits": [],
               "not_applicable": [], "manual_checks": [], "informative": [],
               "collection_errors": [],
               "runtime_error": raw.get("runtimeError"), "errors": problems}
    unscored_groups = {"notApplicable": "not_applicable", "manual": "manual_checks",
                       "informative": "informative"}
    category_data = raw.get("categories")
    if not isinstance(category_data, dict):
        category_data = {}
    for category_id in CATEGORIES:
        item = category_data.get(category_id)
        score = item.get("score") if isinstance(item, dict) else None
        if not valid_score(score):
            score = None
            problems.append("category score unavailable: " + category_id)
        summary["categories"][category_id] = {
            "score": score, "status": "scored" if score is not None else "unknown"}
    if raw.get("runtimeError"):
        problems.append("Lighthouse reported runtimeError")
    if raw.get("lighthouseVersion") != LIGHTHOUSE_VERSION:
        problems.append("Lighthouse version differs from pinned " + LIGHTHOUSE_VERSION)
    audits = raw.get("audits")
    if not isinstance(audits, dict) or not audits:
        problems.append("Lighthouse audit results unavailable or invalid")
    else:
        for audit_id, item in audits.items():
            if not isinstance(item, dict):
                problems.append("invalid audit result: " + audit_id)
                continue
            score = item.get("score")
            record = {"id": audit_id, "title": item.get("title"),
                      "score": score if valid_score(score) else None,
                      "score_display_mode": item.get("scoreDisplayMode")}
            for field in ("displayValue", "explanation", "errorMessage"):
                if field in item:
                    record[field] = item[field]
            mode = item.get("scoreDisplayMode")
            if mode == "error" or "errorMessage" in item:
                summary["collection_errors"].append(record)
                problems.append("audit collection error: " + audit_id)
            elif isinstance(mode, str) and mode in unscored_groups:
                summary[unscored_groups[mode]].append(record)
            elif mode not in ("binary", "numeric", "metricSavings"):
                summary["unknown_audits"].append(record)
                problems.append("unsupported audit score mode: " + audit_id)
            elif not valid_score(score):
                summary["unknown_audits"].append(record)
                problems.append("audit score unavailable: " + audit_id)
            elif score < 1:
                summary["failures"].append(record)
    return summary


def stop_own_process_group(process):
    """Only the new session created by this invocation is eligible for termination."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        # A child may ignore TERM even after its parent has already exited.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
    else:
        process.kill()
        process.wait()


def audit(args):
    validate_audit_url(args.url)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("--timeout must be a positive finite number")
    if os.name != "posix":
        raise ValueError("audit requires POSIX process-group cleanup; this host is unverified")
    executable = args.lighthouse.absolute()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("--lighthouse must name an existing executable file")
    output = no_symlink_components(args.out)
    if output.exists():
        raise ValueError("audit requires a fresh output directory; existing bytes preserved")
    output.mkdir(parents=True)
    temporary = output / "tmp"
    temporary.mkdir()
    raw_path = output / "raw-lighthouse.json"
    command = [str(executable), args.url, "--output=json", "--output-path=" + str(raw_path),
               "--only-categories=" + ",".join(CATEGORIES), "--quiet", "--chrome-flags=--headless"]
    started = time.monotonic()
    receipt = {"schema_version": 1, "status": "error", "started_at": utc_now(),
               "url": args.url, "command": command, "returncode": None, "timed_out": False,
               "timeout_seconds": args.timeout, "expected_lighthouse_version": LIGHTHOUSE_VERSION,
               "lighthouse_version": None, "stdout": "lighthouse-stdout.log",
               "stderr": "lighthouse-stderr.log", "raw": "raw-lighthouse.json",
               "summary": "audit-summary.json", "errors": []}
    process = None
    interrupted = False
    try:
        with (output / receipt["stdout"]).open("xb") as stdout, (output / receipt["stderr"]).open("xb") as stderr:
            process = subprocess.Popen(command, stdout=stdout, stderr=stderr,
                                       env={**os.environ, "TMPDIR": str(temporary)},
                                       start_new_session=True, cwd=output)
            try:
                process.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                receipt["timed_out"] = True
                receipt["errors"].append("Lighthouse exceeded the configured timeout; no retry")
                stop_own_process_group(process)
            except KeyboardInterrupt:
                interrupted = True
                receipt["errors"].append("audit interrupted; own process group stopped")
                stop_own_process_group(process)
            receipt["returncode"] = process.returncode
    except OSError as exc:
        receipt["errors"].append("could not execute Lighthouse: " + str(exc))
    raw = {}
    try:
        if raw_path.is_symlink():
            raise ValueError("raw Lighthouse output must not be a symlink")
        raw_bytes = raw_path.read_bytes()
        receipt["raw_sha256"] = hashlib.sha256(raw_bytes).hexdigest()
        raw = json.loads(raw_bytes, parse_constant=reject_non_json_constant,
                         parse_float=finite_json_float)
        if not isinstance(raw, dict):
            raise ValueError("Lighthouse JSON must be an object")
    except (OSError, ValueError, UnicodeError) as exc:
        receipt["errors"].append("raw Lighthouse output unavailable or invalid: " + str(exc))
        raw = {}
    summary = normalize_lhr(raw)
    receipt["lighthouse_version"] = raw.get("lighthouseVersion")
    if receipt["returncode"] not in (0, None):
        receipt["errors"].append("Lighthouse exited nonzero")
    receipt["errors"].extend(summary["errors"])
    if receipt["timed_out"]:
        receipt["status"] = "timeout"
    elif interrupted:
        receipt["status"] = "interrupted"
    elif not receipt["errors"] and receipt["returncode"] == 0:
        receipt["status"] = "completed"
    receipt["finished_at"] = utc_now()
    receipt["duration_seconds"] = round(time.monotonic() - started, 3)
    summary["status"] = receipt["status"]
    write_json(output / "audit-summary.json", summary)
    write_json(output / "audit-receipt.json", receipt)
    emit(receipt)
    return 0 if receipt["status"] == "completed" else (130 if interrupted else 1)


def read_audit_object(path):
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle, parse_constant=reject_non_json_constant, parse_float=finite_json_float)
    if not isinstance(data, dict):
        raise ValueError(path.name + " must contain a JSON object")
    return data


def public_input_url(url):
    parsed = urlsplit(url)
    valid = (parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username
             and parsed.password is None and not any(c.isspace() or ord(c) < 32 for c in url))
    if not valid:
        raise ValueError("each selected target must be a public HTTP(S) URL without credentials")
    return url


def selected_quick_audit_input(args):
    if args.base_url is not None and args.html is None:
        raise ValueError("--base-url describes an offline --html file")
    if args.html is not None:
        path = no_symlink_components(args.html)
        if not path.is_file() or path.suffix.lower() not in (".html", ".htm"):
            raise ValueError("--html must name an existing HTML file")
        if path.stat().st_size > 5 * 1024 * 1024:
            raise ValueError("--html exceeds the collector's 5 MiB input limit")
        main_args = [str(path)]
        if args.base_url:
            main_args.extend(["--base-url", public_input_url(args.base_url)])
        return "supplied-html", [], main_args, str(path)
    if args.pages is not None:
        path = no_symlink_components(args.pages)
        if not path.is_file():
            raise ValueError("--pages must name an existing URL-list file")
        with path.open("rb") as handle:
            body = handle.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ValueError("--pages exceeds the collector's 1 MiB input limit")
        urls = [line.strip() for line in body.decode("utf-8-sig").splitlines()
                if line.strip() and not line.lstrip().startswith("#")]
        if not 1 <= len(urls) <= 20:
            raise ValueError("--pages requires 1 to 20 URLs")
        urls = list(dict.fromkeys(public_input_url(url) for url in urls))
        return "declared-url-list", urls, [], str(path)
    url = public_input_url(args.site or args.url)
    if args.site:
        parsed = urlsplit(url)
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("--site requires an origin; use --url for a specific page")
        return "site-sample", [url], ["--site", url, "--limit", str(args.limit)], url
    return "single-url", [url], [url], url


def report_priorities(report):
    # Select the original queue rows, and cite an actual affected page instead
    # of reusing the first occurrence of a parameter from any page in the run.
    pages = {page.get("url") or page["target"]: page for page in report["pages"]
             if not page.get("error")}
    site = report.get("site") or {}
    items = []
    for finding in report["fix_first"][:5]:
        item = dict(finding)
        targets = [item["page"]] if item.get("page") else item.get("pages", [])
        if not targets and len(pages) == 1:
            targets = list(pages)
        containers = [(target, pages[target]) for target in targets if target in pages]
        if not targets:
            containers.append((None, site))
        for target, container in containers:
            rows = (row for group in container.get("groups", []) for row in group.get("params", []))
            row = next((row for row in rows if row["id"] == item["id"]
                        and row["verdict"] == item["verdict"]), None)
            if row is not None:
                item["evidence_target"] = target
                for key in ("evidence", "kind", "source"):
                    if row.get(key) is not None:
                        item[key] = row[key]
                break
        items.append(item)
    return items


def quick_audit(args):
    if not 1 <= args.limit <= 100 or not math.isfinite(args.timeout) or not 0 < args.timeout <= 900:
        raise ValueError("--limit must be 1 to 100 and --timeout must be positive and at most 900 seconds")
    if sys.version_info < (3, 10):
        raise ValueError("quick-audit requires Python 3.10 or newer")
    scope, urls, main_args, source = selected_quick_audit_input(args)
    output = no_symlink_components(args.out)
    if output.exists():
        raise ValueError("output directory already exists; choose a fresh path")
    vendor, origin = papa_runtime(Path(__file__).resolve().parent.parent)
    supplied_html = None
    if scope == "supplied-html":
        with Path(source).open("rb") as handle:
            supplied_html = handle.read(5 * 1024 * 1024 + 1)
        if len(supplied_html) > 5 * 1024 * 1024:
            raise ValueError("--html exceeds the collector's 5 MiB input limit")
    output.mkdir(mode=0o700, parents=True)
    input_path = output / "input.html"
    if supplied_html is not None:
        input_path.write_bytes(supplied_html)
        main_args[0] = str(input_path)
    if scope == "declared-url-list":
        target_list = output / "targets.txt"
        target_list.write_text("\n".join(urls) + "\n", encoding="utf-8")
        main_args = ["--pages", str(target_list)]
    html_path = output / "audit.html"
    json_path = output / "audit.json"
    request_path = output / "request.json"
    page_limit = args.limit if args.site else max(1, len(urls))
    request_budget = 16 + 3 * page_limit
    argv = main_args + ["--out", str(html_path), "--json", str(json_path), "--brief",
                        "--delay", "2", "--state-dir", str(output), "--lang", args.lang]
    for keys in args.keys:
        argv.extend(["--keys", keys])
    write_json(request_path, {"argv": argv, "urls": urls, "site_sample": bool(args.site),
                              "request_budget": request_budget})
    receipt = {"schema_version": 1, "status": "error", "scope": scope, "source": source,
               "started_at": utc_now(), "collector_version": origin["version"],
               "collector_commit": origin["commit"], "network_requests": None,
               "request_budget": request_budget, "returncode": None, "errors": []}
    if supplied_html is not None:
        receipt["input_sha256"] = hashlib.sha256(supplied_html).hexdigest()
    stdout_path, stderr_path = output / "collector-stdout.json", output / "collector-stderr.log"
    # Secrets and host Python setup are outside this subprocess. The bundle is
    # the only non-stdlib code loaded. All temporary files stay with the run.
    environment = {name: value for name, value in os.environ.items()
                   if name in ("SYSTEMROOT", "WINDIR", "LANG", "LC_ALL")}
    environment.update({"TMPDIR": str(output), "TEMP": str(output), "TMP": str(output)})
    with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        process = subprocess.Popen([sys.executable, "-I", "-B",
                                    str(vendor / "scripts/traf_worker.py"), str(request_path)],
                                   cwd=output, env=environment, stdout=stdout, stderr=stderr,
                                   start_new_session=(os.name == "posix"))
        try:
            receipt["returncode"] = process.wait(timeout=args.timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            stop_own_process_group(process)
            receipt["returncode"] = process.returncode
            receipt["status"] = "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "interrupted"
            receipt["errors"].append(receipt["status"])
    state_path = output / "collection-state.json"
    if state_path.is_file():
        state = read_audit_object(state_path)
        receipt.update(network_requests=state["requests"], network_attempts=state["attempts"],
                       network_refusals=state["denied"])
    if receipt["status"] not in ("timeout", "interrupted"):
        try:
            brief = read_audit_object(stdout_path)
            if brief.get("error"):
                receipt["errors"].append({"code": brief.get("code"), "message": brief["error"]})
            else:
                raw = read_audit_object(json_path)
                if raw.get("version") != origin["version"] or not html_path.is_file():
                    raise ValueError("collector report/version mismatch")
                receipt["status"] = ("observed-http-error" if receipt["returncode"] == 2
                                     else "partial" if brief.get("pages_failed") or receipt.get("network_refusals")
                                     else "completed" if receipt["returncode"] == 0 else "error")
                failed = brief.get("pages_failed", [])
                receipt.update(coverage=raw["coverage"], attempted_pages=brief["pages"],
                               observed_pages=brief["pages"] - len(failed),
                               site_scan=brief.get("site_scan"), failed_pages=failed,
                               totals=raw["totals"], fix_first=report_priorities(raw))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            receipt["errors"].append(str(exc))
    if not html_path.exists():
        reason = escape(json.dumps(receipt["errors"], ensure_ascii=False))
        html_path.write_text('<!doctype html><html lang="en"><meta charset="utf-8">'
                             '<title>TRAF SEO Master audit</title><h1>Audit could not complete</h1>'
                             f'<p>{reason}</p></html>\n', encoding="utf-8")
    receipt["finished_at"] = utc_now()
    receipt["report"] = str(html_path)
    receipt["files_sha256"] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                               for path in (html_path, json_path, stdout_path, stderr_path, state_path,
                                            request_path, input_path, output / "targets.txt")
                               if path.is_file()}
    write_json(output / "receipt.json", receipt)
    # Temporary pacing markers belong to this run. Keep the actual outputs.
    for marker in output.glob("papa-seo-pace-*.ts"):
        marker.unlink()
    emit(receipt)
    return {"completed": 0, "observed-http-error": 2, "interrupted": 130}.get(receipt["status"], 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "coverage", "install"):
        command = commands.add_parser(name)
        command.add_argument("--package", type=Path,
                             default=Path(__file__).resolve().parent.parent)
        if name == "install":
            command.add_argument("--host", required=True, choices=HOSTS)
            command.add_argument("--project", type=Path, required=True)
            command.add_argument("--dry-run", action="store_true")
    command = commands.add_parser("audit")
    command.add_argument("--url", required=True)
    command.add_argument("--lighthouse", type=Path, required=True)
    command.add_argument("--out", type=Path, required=True)
    command.add_argument("--timeout", type=float, default=180)
    command = commands.add_parser("quick-audit", help="read-only HTML/JSON audit using pinned papa-seo")
    selected = command.add_mutually_exclusive_group(required=True)
    selected.add_argument("--url")
    selected.add_argument("--pages", type=Path)
    selected.add_argument("--site")
    selected.add_argument("--html", type=Path)
    command.add_argument("--out", type=Path, required=True)
    command.add_argument("--base-url")
    command.add_argument("--limit", type=int, default=10, help="sitemap sample pages (default 10, max 100)")
    command.add_argument("--keys", action="append", default=[], help="keyword phrases, separated by semicolons")
    command.add_argument("--lang", choices=("auto", "ru", "en"), default="auto")
    command.add_argument("--timeout", type=float, default=240)
    for name in ("site-add", "site-audit", "site-missing"):
        command = commands.add_parser(name)
        command.add_argument("--package", type=Path,
                             default=Path(__file__).resolve().parent.parent)
        if name == "site-missing":
            target = command.add_mutually_exclusive_group(required=True)
            target.add_argument("--run", type=Path)
            target.add_argument("--sites", type=Path)
        else:
            command.add_argument("--profile", type=Path, required=True)
            command.add_argument("--out", type=Path, required=True)
            if name == "site-audit":
                command.add_argument("--evidence", type=Path, required=True)
                command.add_argument("--as-of", required=True)
    args = parser.parse_args()
    try:
        if args.command.startswith("site-"):
            result = site_audit_module().run(args)
            emit(result)
            return 1 if result.get("status") == "PARTIAL" else 0
        if args.command == "install":
            return install(args)
        if args.command == "audit":
            return audit(args)
        if args.command == "quick-audit":
            return quick_audit(args)
        report, capabilities, _ = check_package(args.package)
        if args.command == "coverage":
            report["capabilities"] = capabilities
        emit(report)
        return 1 if report.get("errors") else 0
    except (OSError, ValueError) as exc:
        emit({"status": "error", "error": str(exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
