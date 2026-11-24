"""Captures the web surfaces of the demo video: the board, the README and JUDGE.md on github.com, the explorers.

    python demo/surfaces.py [<name> ...] [--ref <branch>]

Each surface is the public page at its real URL, in Chrome, 1280 CSS pixels wide, at 3 device pixels for one
CSS pixel: a picture is 3840 pixels wide, so a zoom of the 1920x1080 cut stays sharp. One picture is the full
page in one state. The file `<name>.json` has the URL, the time, and the rectangle (CSS pixels) of each
element that the cut frames, read from the page: no position is typed by hand.

No hash and no address is typed here: each URL comes from `evidence/demo/capture.json`,
`evidence/judge/data.json` and `deployments/manifest.json`.

Output: demo/footage/web/<name>.png and <name>.json. `--ref`: the branch of the github.com pages (default main).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

Image.MAX_IMAGE_PIXELS = None

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "demo" / "footage" / "web"
CHROME = "/usr/bin/google-chrome"
REPOSITORY = "https://github.com/gekomarchi/lifeline"
BOARD = "https://gekomarchi.github.io/lifeline/board/#theme=dark"
EXPLORER = {
    "lasna": "https://lasna.reactscan.net",
    "base-sepolia": "https://base-sepolia.blockscout.com",
    "unichain-sepolia": "https://unichain-sepolia.blockscout.com",
}
WIDTH, HEIGHT, SCALE = 1280, 720, 3
TILE, STICKY = 1200, 300  # the height of one step of the window, and the most that the bars at its top take
# The advertising hosts of the explorer pages: the capture blocks them, as a browser with an ad blocker does.
# No other request is blocked, and no element of a page is changed.
AD_HOSTS = ("adx.ws", "servedbyadbutler.com", "spfsrv.com", "id5-sync.com",
            "blockscout-marketing-banners.s3.us-east-1.amazonaws.com")

# JavaScript helpers of each page: `rect(element)` in CSS pixels of the page, and finders by text.
HELPERS = """
window.__rect = (e) => { if (!e) return null; const r = e.getBoundingClientRect();
  return [r.left + scrollX, r.top + scrollY, r.width, r.height].map((v) => Math.round(v * 100) / 100); };
window.__union = (list) => { const rs = list.filter(Boolean).map(__rect); if (!rs.length) return null;
  const x0 = Math.min(...rs.map((r) => r[0])), y0 = Math.min(...rs.map((r) => r[1]));
  const x1 = Math.max(...rs.map((r) => r[0] + r[2])), y1 = Math.max(...rs.map((r) => r[1] + r[3]));
  return [x0, y0, x1 - x0, y1 - y0]; };
window.__heading = (text) => [...document.querySelectorAll('article h1, article h2, article h3')]
  .find((h) => h.textContent.trim() === text);
window.__block = (h) => h.closest('.markdown-heading') || h;
// The elements from a heading to the next heading of the same level or above.
window.__section = (text) => { const h = __heading(text); if (!h) return []; const out = [__block(h)];
  let e = __block(h).nextElementSibling;
  while (e && !(e.matches('.markdown-heading') && e.querySelector('h1, h2' + (h.tagName === 'H3' ? ', h3' : ''))))
  { out.push(e); e = e.nextElementSibling; } return out; };
window.__after = (text, selector, n = 0) => __section(text).filter((e) => e.matches(selector))[n];
window.__row = (table, text) => table && [...table.querySelectorAll('tr')].find((r) => r.textContent.includes(text));
// A leaf by its own text, with no regard to case: the explorers write labels in capitals with a style.
window.__own = (e) => [...e.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join(' ')
  .replace(/\\s+/g, ' ').trim().toLowerCase();
window.__leaf = (text, n = 0) => [...document.querySelectorAll('body *')]
  .filter((e) => e.getBoundingClientRect().height > 0 && __own(e).startsWith(text.toLowerCase()))[n];
window.__with = (text, n = 0) => { const e = __leaf(text, n); return e && [e, e.nextElementSibling]; };
window.__box = (text, least, most) => { let e = __leaf(text);
  while (e && !(e.getBoundingClientRect().height >= least && e.getBoundingClientRect().height <= most))
    e = e.parentElement;
  return e; };
// The text inside a rectangle of the page: each piece of text with its centre in the rectangle, in order.
window.__text = (rect) => { const out = []; const walker = document.createTreeWalker(document.body, 4);
  const range = document.createRange(); let node;
  while ((node = walker.nextNode())) { if (!node.textContent.trim() || !node.parentElement.checkVisibility())
      continue;  // not the text of a closed <details> block
    range.selectNodeContents(node);
    for (const r of range.getClientRects()) {
      const x = r.left + scrollX + r.width / 2, y = r.top + scrollY + r.height / 2;
      if (r.width && x >= rect[0] && x <= rect[0] + rect[2] && y >= rect[1] - 1 && y <= rect[1] + rect[3] + 1)
      { out.push(node.textContent.trim()); break; } } }
  return out.join(' ').replace(/\\s+/g, ' '); };
window.__has = (selector, text, root = document) => [...root.querySelectorAll(selector)]
  .find((e) => e.textContent.includes(text));
"""


def evidence() -> dict:
    """The addresses and hashes of the pages, from the evidence files."""
    capture = json.loads((ROOT / "evidence" / "demo" / "capture.json").read_text())
    judge = json.loads((ROOT / "evidence" / "judge" / "data.json").read_text())
    manifest = json.loads((ROOT / "deployments" / "manifest.json").read_text())
    rescue = next(r for r in judge["rescues"] if r["requestId"] == capture["requestId"])
    fleet = {record["label"]: record for record in manifest["enrollments"]}
    return {"capture": capture, "rescue": rescue, "fixtures": manifest["fixtures"], "fleet": fleet}


def settle(page, seconds: float = 2.0) -> None:
    """Scrolls through the page one time, so that each lazy part is loaded, and goes back to the top."""
    height = page.evaluate("document.documentElement.scrollHeight")
    for y in range(0, height, 500):
        page.evaluate(f"window.scrollTo(0, {y})")
        page.wait_for_timeout(250)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(int(seconds * 1000))


def regions(page, expressions: dict) -> dict:
    page.evaluate(HELPERS)
    found = {name: page.evaluate(expression) for name, expression in expressions.items()}
    absent = [name for name, rect in found.items() if rect is None]
    if absent:
        raise SystemExit(f"{page.url}: no element for {', '.join(absent)}")
    return found


def save(page, name: str, expressions: dict, extra: dict = None) -> None:
    """Writes the picture of the full page and the rectangles of its elements.

    The picture is made as a reader sees the page: the window goes down the page in steps, and each step
    gives the part of the window below the bars that stay at the top of the window (the file header of
    github.com, the top bar of the board). So a frame of another origin (the sequence diagram of github.com)
    is drawn, because it is in the window when its part is taken, and no bar covers the content."""
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(400)
    found = regions(page, expressions)
    # The text of each region as the page shows it: `claims.py` compares it with the evidence files.
    texts = {name: page.evaluate("(rect) => __text(rect)", rect) for name, rect in found.items()}
    width, height = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.scrollHeight]")
    page.set_viewport_size({"width": WIDTH, "height": TILE})
    full = Image.new("RGB", (WIDTH * SCALE, height * SCALE))
    part = OUT / f"{name}.part.png"
    done = 0  # the page is in the picture down to this CSS pixel
    while done < height:
        bar = 0
        if done:
            page.evaluate(f"window.scrollTo(0, {done - STICKY})")
            page.wait_for_timeout(700)
            bar = page.evaluate(
                "Math.max(0, ...[...document.querySelectorAll('body *')].filter((e) => { const p = "
                "getComputedStyle(e).position, r = e.getBoundingClientRect(); return (p === 'sticky' || "
                f"p === 'fixed') && r.top < 4 && r.top > -4 && r.width > {WIDTH} / 2 && r.height > 8 && r.height < "
                f"{STICKY}; }}).map((e) => Math.ceil(e.getBoundingClientRect().bottom)))")
        top = page.evaluate("scrollY")
        start = max(done - top, bar)  # the first line of the window that goes into the picture
        if top + start > done:
            raise SystemExit(f"{name}: a bar of {bar} CSS pixels covers the page at {done}")
        page.screenshot(path=str(part))
        tile = Image.open(part).convert("RGB")
        full.paste(tile.crop((0, start * SCALE, WIDTH * SCALE, tile.height)), (0, (top + start) * SCALE))
        done = top + TILE
    part.unlink()
    full.save(OUT / f"{name}.png")
    page.set_viewport_size({"width": WIDTH, "height": HEIGHT})
    page.evaluate("window.scrollTo(0, 0)")
    record = {"name": name, "url": page.url, "capturedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "page": [width, height], "scale": SCALE, "regions": found, "texts": texts, **(extra or {})}
    (OUT / f"{name}.json").write_text(json.dumps(record, indent=1) + "\n")
    print(f"{name}: {width}x{height} CSS pixels, {len(found)} regions, {page.url}", flush=True)


# ----------------------------------------------------------------------------------------------
# The surfaces
# ----------------------------------------------------------------------------------------------


def reactscan_address(page, data, ref) -> None:
    address = data["fixtures"]["armA.reactive"]["address"]
    page.goto(f"{EXPLORER['lasna']}/address/{address}", wait_until="domcontentloaded")
    page.get_by_text("Contract debt").wait_for(timeout=60000)
    page.get_by_text("Success").first.wait_for(timeout=60000)
    settle(page)
    save(page, "reactscan-arm-a", {
        "head": "__union([__leaf('0x'), __leaf('contract balance'), __leaf('[cover debt]'), __leaf('address type')])",
        "debt": "__union([__leaf('contract debt'), __leaf('[cover debt]')])",
        "reserves": "__union(__with('contract reserves'))",
        "tabs": "__union([__leaf('// transactions'), __leaf('// code')])",
        "counts": "__union([__leaf('// transactions'), __leaf('// reactive executions')])",
        "table": "__rect(__box('# hash', 300, 5000))",
        "status": "__union([__leaf('# status'), ...[...document.querySelectorAll('body *')].filter((e) => "
                  "__own(e) === 'success')])",
    }, {"address": address})


def reactscan_tx(page, data, ref) -> None:
    tx = data["rescue"]["relayed"]["tx"]
    page.goto(f"{EXPLORER['lasna']}/tx/{tx}", wait_until="domcontentloaded")
    page.get_by_text("Destination tx").wait_for(timeout=60000)
    settle(page)
    save(page, "reactscan-relayed", {
        "head": "__union([__leaf('0x'), __leaf('status'), ...__with('block timestamp')])",
        "overview": "__rect(__box('transaction hash', 300, 900))",
        "block": "__union(__with('block number'))",
        "callbacksTitle": "__rect(__leaf('// callbacks'))",
        "callbacks": "__rect(__box('destination tx', 400, 1200))",
        "callbackHead": "__union([__leaf('# 1'), __leaf('on-chain attested'), __leaf('requested'), __leaf('posted')])",
        "destination": "__union(__with('destination tx'))",
    }, {"tx": tx})


def reactscan_enroll(page, data, ref) -> None:
    tx = data["fleet"]["FastfillExecutor"]["txs"]["enroll"]
    page.goto(f"{EXPLORER['lasna']}/tx/{tx}", wait_until="domcontentloaded")
    page.get_by_text("Block number").wait_for(timeout=60000)
    page.get_by_text("Success").first.wait_for(timeout=60000)
    settle(page)
    save(page, "reactscan-enroll", {
        "head": "__union([__leaf('0x'), __leaf('status'), ...__with('block timestamp')])",
        "overview": "__rect(__box('transaction hash', 300, 900))",
        "block": "__union(__with('block number'))",
        "input": "__union(__with('input'))",
    }, {"tx": tx})


def blockscout_tx(name: str, key: str):
    def run(page, data, ref) -> None:
        tx = data["rescue"][key]["tx"]
        page.goto(f"{EXPLORER[data['rescue']['chain']]}/tx/{tx}", wait_until="domcontentloaded")
        page.get_by_text("Transaction hash").first.wait_for(timeout=60000)
        page.get_by_text("Timestamp").first.wait_for(timeout=60000)
        page.wait_for_timeout(4000)
        settle(page)
        row = ("(label) => { let e = [...document.querySelectorAll('*')].filter((x) => x.children.length < 3 && "
               "x.textContent.trim() === label).pop(); if (!e) return null; const top = e.getBoundingClientRect().top;"
               " const all = [...document.querySelectorAll('main *')].filter((x) => { const r = "
               "x.getBoundingClientRect(); return r.height > 0 && r.height < 60 && Math.abs(r.top - top) < 14; });"
               " return __union(all); }")
        save(page, name, {
            "title": "__rect(document.querySelector('h1'))",
            "status": f"({row})('Status and method')",
            "block": f"({row})('Block')",
            "timestamp": f"({row})('Timestamp')",
            "details": f"__union([({row})('Transaction hash'), ({row})('Timestamp')].map((r) => r && "
                       "({getBoundingClientRect: () => ({left: r[0] - scrollX, top: r[1] - scrollY, width: r[2], "
                       "height: r[3]})})))",
        }, {"tx": tx})
    return run


def readme(page, data, ref) -> None:
    page.goto(REPOSITORY if ref == "main" else f"{REPOSITORY}/tree/{ref}", wait_until="domcontentloaded")
    page.locator("article h1").first.wait_for(timeout=60000)
    settle(page)
    # The sequence diagram is a frame of github.com: it draws when it is in the window.
    page.evaluate(HELPERS)
    page.evaluate("__heading('How it works').scrollIntoView()")
    frame = page.frame_locator("article section iframe").first
    frame.locator("svg").first.wait_for(timeout=60000)
    frame.get_by_text("AIRBAG").first.wait_for(timeout=60000)
    page.wait_for_timeout(2500)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(1000)
    li = ("(heading, text) => __rect(__section(heading).flatMap((e) => [...e.querySelectorAll('li')])"
          ".find((e) => e.textContent.includes(text)))")
    save(page, "readme", {
        "top": "__union(__section(document.querySelector('article h1').textContent.trim()))",
        "pageHead": "__rect(document.querySelector('#repository-container-header') || document.querySelector('main'))",
        "problem": "__union(__section('Problem'))",
        "problemBuildathon": f"({li})('Problem', 'In this buildathon')",
        "problemOwn": f"({li})('Problem', 'own automation')",
        "diagram": "__rect(__after('How it works', 'section'))",
        "howTitle": "__rect(__block(__heading('How it works')))",
        "alternatives": "__union(__section('Lifeline against the alternatives'))",
        "armsIntro": "__rect(__after('Lifeline against the alternatives', 'p', 0))",
        "arms": "__rect(__after('Lifeline against the alternatives', 'markdown-accessiblity-table, table', 0))",
        "armsCallbacks": "__rect(__row(__after('Lifeline against the alternatives', "
                         "'markdown-accessiblity-table, table', 0), 'Callbacks delivered'))",
        "armsNotes": "__rect(__after('Lifeline against the alternatives', 'ul', 0))",
        "armB": f"({li})('Lifeline against the alternatives', 'was dead on Lasna')",
        "notRefiller": "__rect(__section('Lifeline against the alternatives').find((e) => e.matches('p') && "
                       "e.textContent.includes('not a wallet refiller')))",
        "comparison": "__rect(__after('Lifeline against the alternatives', 'markdown-accessiblity-table, table', 1))",
        "comparisonRestart": "__rect(__row(__after('Lifeline against the alternatives', "
                             "'markdown-accessiblity-table, table', 1), 'Restarts a stopped'))",
        "enroll": "__rect(__section('Operator guide').find((e) => e.textContent.includes('lifeline.ops enroll') "
                  "&& e.matches('div, pre')))",
        "enrollIntro": "__rect(__after('Operator guide', 'p', 0))",
        "limits": "__union(__section('Limits').slice(0, 2))",
        "limitsTitle": "__rect(__block(__heading('Limits')))",
        "notBuilt": "__union(__section('Not built'))",
        "demoProduction": "__union(__section('Demo versus production'))",
    }, {"ref": ref})


def judge(page, data, ref) -> None:
    page.goto(f"{REPOSITORY}/blob/{ref}/JUDGE.md", wait_until="domcontentloaded")
    page.locator("article h1").first.wait_for(timeout=60000)
    # The file tree at the left takes a third of the window: close it, as a reader does.
    tree = page.locator('[data-testid="collapse-file-tree-button"]')
    tree.first.wait_for(timeout=30000)
    tree.first.click()
    page.wait_for_timeout(1200)
    settle(page)
    table = "(heading, n) => __after(heading, 'markdown-accessiblity-table, table', n)"
    request, result = data["capture"]["requestId"], data["rescue"]["result"]["tx"]
    expressions = {
        "title": "__rect(__block(document.querySelector('article h1')))",
        "claimLimit": "__union(__section('Claim limit'))",
        "latencyTitle": "__rect(__block(__heading('Rescue latency')))",
        "latencySummary": f"__rect(({table})('Rescue latency', 0))",
        "latencySweep": f"__rect(__row(({table})('Rescue latency', 0), 'sweep'))",
        "latencyRows": f"__rect(({table})('Rescue latency', 1))",
        "latencySweepRow": f"__rect(__row(({table})('Rescue latency', 1), 'sweep'))",
        "quarantine": "__union(__section('Quarantine'))",
        "quarantineRescues": f"__rect(__row(({table})('Quarantine', 0), 'Rescues from the enrollment'))",
        "quarantineAfter": f"__rect(__row(({table})('Quarantine', 0), 'Principal paid after'))",
        "junkFlood": "__union(__section('Junk flood'))",
        "junkFailures": f"__rect(__row(({table})('Junk flood', 0), 'CallbackFailure'))",
        "junkUnexplained": f"__rect(__row(({table})('Junk flood', 0), 'do not explain'))",
        "junkSelfCare": f"__rect(__row(({table})('Junk flood', 0), 'SelfCare.added'))",
        "junkResultPaid": f"__rect(__row(({table})('Junk flood', 0), 'Result.paid'))",
        "junkReservesEnd": f"__rect(__row(({table})('Junk flood', 0), 'Reserves of the vault at the end'))",
        "junkRecovery": "__union(__section('Junk recovery').slice(0, 3))",
        "junkRecoverySteps": f"__rect(({table})('Junk recovery', 0))",
        "junkDeposit": f"__rect(__row(({table})('Junk recovery', 0), 'WhitelistContract(vault)'))",
        "junkVaultUp": f"__rect(__row(({table})('Junk recovery', 0), 'VaultUp'))",
        "junkResult": f"__rect(__row(({table})('Junk recovery', 0), 'Result(waiting.callback)'))",
        "guests": "__union(__section('Guests'))",
    }
    # The rows of the captured rescue: on github.com only after the branch with the new JUDGE.md is pushed.
    page.evaluate(HELPERS)
    captured = page.evaluate(f"!!__row(({table})('Destination rescues', 0), '{request}')")
    if captured:
        expressions["capturedRescue"] = f"__rect(__row(({table})('Destination rescues', 0), '{request}'))"
        expressions["capturedLatency"] = f"__rect(__row(({table})('Rescue latency', 1), '{result}'))"
    save(page, "judge", expressions, {"ref": ref, "hasCapturedRescue": captured})


def board_ready(page) -> None:
    page.goto(BOARD, wait_until="domcontentloaded")
    page.locator("#targets-body tr", has_text="FastfillExecutor").wait_for(timeout=90000)
    # Each chain is read: the rows have their reserves.
    page.wait_for_function(
        "() => [...document.querySelectorAll('#targets-body tbody tr')].filter((r) => /lREACT|ETH/.test("
        "r.textContent)).length >= 7", timeout=120000)
    page.wait_for_timeout(3000)


BOARD_REGIONS = {
    "topbar": "__rect(document.querySelector('header') || document.body.firstElementChild)",
    "funds": "__rect(__has('section.panel', 'Hub and vault funds'))",
    "targets": "__rect(document.querySelector('#targets'))",
    "targetsHead": "__union([document.querySelector('#targets .panel__head') || "
                   "document.querySelector('#targets-title'), document.querySelector('#targets-state')])",
    "filterQuarantined": "__rect(__has('#targets-state .seg__item', 'Quarantined'))",
    "filterAll": "__rect(__has('#targets-state .seg__item', 'All'))",
}


def board_rows(page) -> dict:
    labels = page.evaluate("[...document.querySelectorAll('#targets-body tbody tr')].map((r) => "
                           "(r.querySelector('td') || r).textContent.trim()).filter(Boolean)")
    out = {}
    for label in ("FastfillReactive", "FastfillExecutor", "experiment looping", "experiment armC.callback"):
        if any(label in text for text in labels):
            key = "row:" + label.replace("experiment ", "")
            row = ("[...document.querySelectorAll('#targets-body tbody tr')]"
                   f".find((r) => r.textContent.includes('{label}'))")
            out[key] = f"__rect({row})"
            out["details:" + label.replace("experiment ", "")] = f"__rect(__has('button', 'Details', {row}))"
            out["reserves:" + label.replace("experiment ", "")] = \
                f"__rect([...({row}).querySelectorAll('td')].slice(-2)[0])"
    return out


def board(page, data, ref) -> None:
    board_ready(page)
    save(page, "board", {**BOARD_REGIONS, **board_rows(page)})
    # The details of one fleet target: its policy and the hashes of its enrollment.
    row = page.locator("#targets-body tbody tr", has_text="FastfillExecutor").first
    row.get_by_role("button", name="Details").click()
    page.wait_for_timeout(1200)
    panel = ("[...document.querySelectorAll('#targets-body tbody tr')].find((r) => r.textContent.includes("
             "'FastfillExecutor')).nextElementSibling")
    save(page, "board-details", {**BOARD_REGIONS, **board_rows(page), "panel": f"__rect({panel})",
                                 "fleet": "__union([...document.querySelectorAll('#targets-body tbody tr')]"
                                          f".filter((r) => /Fastfill/.test(r.textContent)).concat([{panel}]))"})
    row.get_by_role("button", name="Details").click()
    page.wait_for_timeout(600)
    # The state filter "Quarantined".
    page.locator("#targets-state .seg__item", has_text="Quarantined").click()
    page.wait_for_timeout(1200)
    save(page, "board-quarantined", {**BOARD_REGIONS, **board_rows(page)})
    # The details of the quarantined canary: its quarantine rule, its debt and its reserves.
    row = page.locator("#targets-body tbody tr", has_text="experiment looping").first
    row.get_by_role("button", name="Details").click()
    page.wait_for_timeout(1200)
    panel = ("[...document.querySelectorAll('#targets-body tbody tr')].find((r) => r.textContent.includes("
             "'experiment looping')).nextElementSibling")
    cell = ("(label) => { const e = [...(" + panel + ").querySelectorAll('*')].find((x) => x.children.length === 0 "
            "&& x.textContent.trim().toLowerCase() === label); return e && e.parentElement; }")
    save(page, "board-quarantined-details", {
        **BOARD_REGIONS, **board_rows(page), "panel": f"__rect({panel})",
        "rule": f"__rect(({cell})('quarantine rule'))",
        "state": "__rect([...document.querySelectorAll('#targets-body tbody tr')].find((r) => "
                 "r.textContent.includes('experiment looping')).querySelectorAll('td')[2])",
    })


SURFACES = {
    "reactscan-arm-a": reactscan_address,
    "readme": readme,
    "judge": judge,
    "board": board,
    "reactscan-enroll": reactscan_enroll,
    "reactscan-relayed": reactscan_tx,
    "blockscout-debt": blockscout_tx("blockscout-debt", "debtEvent"),
    "blockscout-result": blockscout_tx("blockscout-result", "result"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("names", nargs="*", choices=[*SURFACES, []], help="surfaces to capture (default: each one)")
    parser.add_argument("--ref", default="main", help="branch of the github.com pages (default main)")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    data = evidence()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROME, headless=True, args=["--hide-scrollbars"])
        context = browser.new_context(viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=SCALE,
                                      color_scheme="dark", locale="en-US", timezone_id="UTC")
        context.route(lambda url: any(host in url.split("/")[2] for host in AD_HOSTS), lambda route: route.abort())
        try:
            for name in args.names or SURFACES:
                page = context.new_page()
                SURFACES[name](page, data, args.ref)
                page.close()
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
