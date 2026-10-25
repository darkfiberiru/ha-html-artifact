"""Renders the real panel in Chromium (Playwright) with a fake `hass` and measures layout.

    python tests/panel_layout.py [--shots DIR]

Checks the things that went wrong on a real device: the preview must fill the screen,
view mode must hide the editor and list, and edit mode must show list + editor + preview.
"""
import json
import pathlib
import sys

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = (ROOT / "custom_components/html_artifacts/frontend/html-artifacts.js").read_text()
SHOTS = pathlib.Path(sys.argv[sys.argv.index("--shots") + 1]) if "--shots" in sys.argv else None

PAGE_HTML = "<!doctype html><title>t</title><body style='background:#1b1b1b;color:#eee;font:16px system-ui'><h1 id=h>Recipe page</h1><p>Short content.</p></body>"
ARTIFACTS = [
    {"id": "recipe", "title": "Pesto Tilapia", "version": 3, "updated_by": "claude", "updated": 1, "caps_requested": {"entities": [], "services": [], "notify": True, "network": False}, "caps_granted": {"entities": [], "services": [], "notify": False, "network": False}, "html": PAGE_HTML},
    {"id": "board", "title": "House Board", "version": 1, "updated_by": "user", "updated": 1, "caps_requested": {"entities": [], "services": [], "notify": False, "network": False}, "caps_granted": {"entities": [], "services": [], "notify": False, "network": False}, "html": PAGE_HTML},
]

HARNESS = """
<!doctype html><html><head><meta charset=utf-8>
<style>html,body{margin:0;height:100%%;background:#000}
 :root{--primary-text-color:#e1e1e1;--primary-background-color:#111;--card-background-color:#1c1c1c;--divider-color:#333;--primary-color:#03a9f4}
 #slot{%s}</style></head>
<body><div id=slot></div>
<script>
const ARTS = %s;
window.__sent = [];
const hass = {
  states: {}, themes: {darkMode: true},
  connection: {
    sendMessagePromise: async (m) => {
      window.__sent.push(m.type);
      if (m.type.endsWith('/timers/list')) return {timers: []};
      if (m.type.endsWith('/list')) return {artifacts: ARTS.map(a => Object.assign({}, a, {html: undefined}))};
      if (m.type.endsWith('/get')) { const a = ARTS.find(a => a.id === m.artifact_id); if (!a) throw new Error("artifact '" + m.artifact_id + "' does not exist"); return a; }
      if (m.type.endsWith('/history')) return {history: []};
      if (m.type.endsWith('/timers/list') || m.type.endsWith('/hello')) return {timers: []};
      return null;
    },
    subscribeMessage: async () => () => {},
  },
};
</script>
<script>%s</script>
<script>
window.mountPanel = (narrow) => {
  const el = document.createElement('html-artifacts-panel');
  el.narrow = narrow;
  document.getElementById('slot').appendChild(el);
  el.hass = hass;
  window.panel = el;
  return el;
};
</script></body></html>
"""

FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def box(page, js):
    return page.evaluate(js)


def run(browser, name, size, slot_css, narrow=False):
    page = browser.new_page(viewport=size)
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e) + " @ " + str(getattr(e, "stack", ""))[:700].replace("\n", " | ")))
    html = HARNESS % (slot_css, json.dumps(ARTIFACTS), JS)
    page.route("http://harness.test/", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
    page.goto("http://harness.test/")
    page.evaluate(f"mountPanel({str(narrow).lower()})")
    page.wait_for_timeout(600)
    return page, errs


def rect(page, sel):
    return page.evaluate(
        """(sel) => { const p = window.panel.shadowRoot; const e = p.querySelector(sel);
        if (!e) return null; const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
        return {x:r.x,y:r.y,w:r.width,h:r.height,display:cs.display}; }""", sel)


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    for label, size, slot, narrow in [
        ("desktop, parent gives definite height", {"width": 1280, "height": 800}, "height:100%", False),
        ("phone, parent gives definite height", {"width": 390, "height": 760}, "height:100%", True),
        ("phone, parent height NOT definite (HA quirk)", {"width": 390, "height": 760}, "", True),
    ]:
        print(f"\n[{label}]")
        page, errs = run(browser, label, size, slot, narrow)
        mode = page.evaluate("window.panel.shadowRoot.querySelector('.app').dataset.mode || ''")
        fr = rect(page, "#preview iframe")
        vh = size["height"]
        print(f"      mode='{mode}'  iframe={fr and (round(fr['w']), round(fr['h']))}  viewport={size['width']}x{vh}")
        check(not errs, "no page errors " + "; ".join(errs))
        check(fr is not None and fr["h"] >= vh - 90, f"preview iframe fills the screen ({fr and round(fr['h'])}px of {vh}px)")
        # ---- modes -----------------------------------------------------------------
        vw = size["width"]
        ed, side, ban, bar = rect(page, "#editor"), rect(page, "aside"), rect(page, "#banner"), rect(page, ".bar")
        check(ed["display"] == "none", "view mode hides the code editor")
        check(side["display"] == "none", "view mode hides the artifact list")
        check(ban is None or ban["h"] == 0 or ban["display"] == "none", "empty banner takes no space")
        check(bar["h"] < 64, f"view toolbar is one row ({round(bar['h'])}px)")
        check(page.evaluate("window.panel.shadowRoot.querySelector('#title').value") == "Pesto Tilapia", "title shown")

        page.click("#edit")
        page.wait_for_timeout(150)
        ed = rect(page, "#editor"); fr = rect(page, "#preview iframe"); side = rect(page, "aside")
        check(ed["display"] != "none" and ed["w"] > 100 and ed["h"] > 100, f"edit mode shows the editor ({round(ed['w'])}x{round(ed['h'])})")
        check(fr["w"] > 100 and fr["h"] > 100, f"edit mode keeps a usable preview ({round(fr['w'])}x{round(fr['h'])})")
        check((side["display"] != "none") == (not narrow), "edit mode shows the list only on wide screens")
        page.click("#done")
        page.wait_for_timeout(100)
        check(page.evaluate("window.panel.shadowRoot.querySelector('.app').dataset.mode") == "view", "Done returns to view")

        page.click("#full")
        page.wait_for_timeout(200)
        m = rect(page, ".main")
        inner = page.evaluate("[innerWidth, innerHeight]")
        check(page.evaluate("window.panel.shadowRoot.querySelector('.app').classList.contains('immersive')"), "full screen engages")
        check(m["x"] == 0 and m["y"] == 0 and round(m["w"]) == inner[0] and round(m["h"]) == inner[1], f"full screen covers the whole viewport ({round(m['w'])}x{round(m['h'])})")
        check(rect(page, "#exit")["display"] != "none", "exit button is visible in full screen")
        page.click("#exit")
        page.wait_for_timeout(100)
        check(not page.evaluate("window.panel.shadowRoot.querySelector('.app').classList.contains('immersive')"), "exit leaves full screen")

        page.click("#back")
        page.wait_for_timeout(100)
        lst = rect(page, "aside")
        check(lst["display"] != "none" and lst["w"] >= vw - 2, f"list mode shows a full-width list ({round(lst['w'])}px)")
        check(page.evaluate("window.panel.shadowRoot.querySelectorAll('#list li[data-id]').length") == 2, "list shows both artifacts")
        page.click("#list li[data-id='board']")
        page.wait_for_timeout(300)
        check(page.evaluate("window.panel.shadowRoot.querySelector('.app').dataset.mode") == "view", "picking an artifact opens it in view mode")
        check(page.evaluate("window.panel.shadowRoot.querySelector('#title').value") == "House Board", "the picked artifact is shown")
        fr = rect(page, "#preview iframe")
        check(fr["h"] >= vh - 90, f"picked artifact fills the screen ({round(fr['h'])}px of {vh}px)")
        if SHOTS:
            SHOTS.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(SHOTS / (label.split(",")[0].replace(" ", "_") + "_" + str(size["width"]) + ("" if slot else "_nodef") + ".png")))
        page.close()

    # ---- dashboard card + its visual editor -------------------------------------
    print("\n[dashboard card and visual editor]")
    page = browser.new_page(viewport={"width": 900, "height": 700})
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    html = HARNESS % ("height:100%", json.dumps(ARTIFACTS), JS)
    page.route("http://harness.test/", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
    page.goto("http://harness.test/")
    page.evaluate("""() => {
      window.events = [];
      const ed = document.createElement('html-artifact-card-editor');
      ed.addEventListener('config-changed', (e) => window.events.push(e.detail.config));
      document.body.appendChild(ed); ed.hass = hass; ed.setConfig({artifact: 'board', height: 500});
      window.ed = ed;
    }""")
    page.wait_for_timeout(300)
    opts = page.evaluate("[...ed.shadowRoot.querySelectorAll('#art option')].map(o => o.textContent)")
    check(len(opts) == 3 and "Pesto Tilapia" in opts[1], f"editor lists the artifacts by title ({opts})")
    check(page.evaluate("ed.shadowRoot.getElementById('art').value") == "board", "editor preselects the configured artifact")
    check(page.evaluate("ed.shadowRoot.getElementById('height').value") == "500", "editor shows the configured height")

    def change(sel, value):
        page.evaluate("([s, v]) => { const el = ed.shadowRoot.querySelector(s); el.value = v; el.dispatchEvent(new Event('change')); }", [sel, value])

    change("#art", "recipe")
    check(page.evaluate("events.at(-1)") == {"artifact": "recipe", "height": 500}, "choosing an artifact emits config-changed")
    change("#height", "auto")
    check(page.evaluate("events.at(-1)") == {"artifact": "recipe", "height": "auto"}, "height 'auto' accepted")
    change("#height", "620")
    check(page.evaluate("events.at(-1)") == {"artifact": "recipe", "height": 620}, "numeric height accepted")
    change("#height", "banana")
    check(page.evaluate("events.at(-1)") == {"artifact": "recipe", "height": 620}, "invalid height is ignored")
    change("#height", "")
    check(page.evaluate("events.at(-1)") == {"artifact": "recipe"}, "empty height falls back to the default")
    page.evaluate("ed.setConfig({artifact: 'gone'})")
    check("not found" in page.evaluate("ed.shadowRoot.getElementById('art').selectedOptions[0].textContent"), "a deleted artifact is shown as not found")

    check(page.evaluate("customElements.get('html-artifact-card').getConfigElement().tagName.toLowerCase()") == "html-artifact-card-editor", "card offers the visual editor")
    check(page.evaluate("customElements.get('html-artifact-card').getStubConfig(hass)") == {"artifact": "recipe", "height": 500}, "new cards start with the first artifact selected")
    page.evaluate("""() => {
      const c = document.createElement('html-artifact-card');
      document.body.appendChild(c); window.card = c;
      c.setConfig({artifact: '', height: 500}); c.hass = hass;
    }""")
    page.wait_for_timeout(200)
    check("Choose an artifact" in page.evaluate("card._root.getElementById('host').textContent"), "card without an artifact shows a hint instead of an error")
    page.evaluate("card.setConfig({artifact: 'recipe', height: 500})")
    page.wait_for_timeout(500)
    check(page.evaluate("card._root.querySelector('iframe') && card._root.querySelector('iframe').title") == "Pesto Tilapia", "card shows the chosen artifact")
    page.evaluate("card.setConfig({artifact: 'board', height: 300})")
    page.wait_for_timeout(500)
    check(page.evaluate("card._root.querySelector('iframe').title") == "House Board", "changing the artifact in the editor updates the card")
    check(page.evaluate("Math.round(card._root.querySelector('iframe').getBoundingClientRect().height)") == 300, "changing the height updates the card")
    page.evaluate("card.setConfig({artifact: 'Pesto Tilapia', height: 300})")
    page.wait_for_timeout(600)
    check(page.evaluate("card._root.querySelector('iframe') && card._root.querySelector('iframe').title") == "Pesto Tilapia", "card accepts the artifact title instead of the id")
    page.evaluate("card.setConfig({artifact: 'house  BOARD', height: 300})")
    page.wait_for_timeout(600)
    check(page.evaluate("card._root.querySelector('iframe') && card._root.querySelector('iframe').title") == "House Board", "card matches titles case- and spacing-insensitively")
    page.evaluate("card.setConfig({artifact: 'nope', height: 301})")
    page.wait_for_timeout(600)
    msg = page.evaluate("card._root.getElementById('host').textContent")
    check("recipe" in msg and "board" in msg and "Use the artifact" in msg, f"unknown artifact lists the valid ids ({msg[:110]})")
    check(not errs, "no page errors " + "; ".join(errs))
    page.close()
    browser.close()

print("\nFAILED" if FAILS else "\nlayout checks passed")
sys.exit(1 if FAILS else 0)
