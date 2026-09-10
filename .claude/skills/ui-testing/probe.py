"""Headless-browser probe for Leasure. Runs a small scripted scenario against the
local server and writes screenshots + a JSON report. Usage:

    python probe.py --out DIR [--base http://127.0.0.1:8642] [--scenario home|device|all]
    python probe.py --out DIR --script my_steps.py     # custom steps (see run_custom)

A custom script defines `def steps(page, shot):` where `shot(name)` saves a
screenshot. Console errors, failed requests and uncaught page errors are always
collected and written to report.json.
"""
import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PANELS = ("home", "music", "library", "downloads", "device")


def open_panel(page, name: str):
    """Side nav in templates/index.html: <button data-panel="home|music|library|downloads|device">."""
    page.locator(f'#nav-buttons button[data-panel="{name}"]').click()
    page.wait_for_timeout(700)


def scenario_home(page, shot):
    page.goto(page.base_url, wait_until="networkidle", timeout=30000)
    page.wait_for_selector("text=/HOME/i", timeout=10000)
    shot("home")
    return {"title": page.title(), "status_bar": page.locator("body").inner_text()[:300]}


def scenario_device(page, shot):
    page.goto(page.base_url, wait_until="networkidle", timeout=30000)
    open_panel(page, "device")
    page.wait_for_timeout(1000)
    shot("device")
    drives = page.locator(".drive-card").count()
    return {"drive_cards": drives}


def run_custom(page, shot, script_path: str):
    spec = importlib.util.spec_from_file_location("custom_steps", script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.steps(page, shot)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--base", default="http://127.0.0.1:8642")
    ap.add_argument("--scenario", default="all")
    ap.add_argument("--script")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=900)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report = {"base": args.base, "console_errors": [], "page_errors": [], "failed_requests": [], "results": {}}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": args.width, "height": args.height})
        page.base_url = args.base
        page.on("console", lambda m: report["console_errors"].append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: report["page_errors"].append(str(e)))
        page.on("requestfailed", lambda r: report["failed_requests"].append(f"{r.method} {r.url} {r.failure}"))
        page.on("response", lambda r: report["failed_requests"].append(f"{r.status} {r.url}") if r.status >= 400 else None)

        n = [0]

        def shot(name):
            n[0] += 1
            path = out / f"{n[0]:02d}-{name}.png"
            page.screenshot(path=str(path), full_page=False)
            return str(path)

        started = time.time()
        try:
            if args.script:
                report["results"]["custom"] = run_custom(page, shot, args.script)
            else:
                if args.scenario in ("home", "all"):
                    report["results"]["home"] = scenario_home(page, shot)
                if args.scenario in ("device", "all"):
                    report["results"]["device"] = scenario_device(page, shot)
        except Exception as e:  # keep the report even when a step fails
            report["exception"] = repr(e)
            shot("failure")
        finally:
            report["seconds"] = round(time.time() - started, 1)
            browser.close()

    (out / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2))
    print("screenshots:", sorted(str(p) for p in out.glob("*.png")))
    return 1 if report.get("exception") else 0


if __name__ == "__main__":
    sys.exit(main())
