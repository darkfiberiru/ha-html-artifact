"""Display type (card / page / both): panel list badges and filter, real-size previews, ha.host,
copy link, the card picker and the stub config.   python tests/panel_display.py"""
import ast
import json
import pathlib

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = (ROOT / "custom_components/html_artifacts/frontend/html-artifacts.js").read_text()
tree = ast.parse((ROOT / "tests/panel_layout.py").read_text())
HARNESS = next(n.value.value for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "HARNESS")

PAGE = "<!doctype html><title>t</title><body style='background:#222;color:#eee'><h1>x</h1><script>ha.ready.then(()=>{document.title=ha.host+':'+ha.hostHeight})</script></body>"
CAPS = {"entities": [], "services": [], "notify": False, "network": False}


def art(i, title, display, height=None):
    return {"id": i, "title": title, "version": 1, "updated_by": "claude", "updated": 1, "display": display,
            "card_height": height, "caps_requested": CAPS, "caps_granted": CAPS, "html": PAGE}


ARTS = [art("timer", "What's cooking", "card", 220), art("recipes", "Recipes", "page"),
        art("clock", "Clock", "both", "auto"), art("old", "Old", None)]
FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    html = HARNESS % ("height:100%", json.dumps(ARTS).replace("</", "<\\/"), JS)
    page.route("http://harness.test/**", lambda r: r.fulfill(status=200, content_type="text/html", body=html))
    page.goto("http://harness.test/")
    page.evaluate("mountPanel(false)")
    page.wait_for_timeout(600)
    q = lambda js: page.evaluate(js)
    root = "window.panel.shadowRoot"

    print("\n[panel list]")
    check(q(f"{root}.querySelector('.app').dataset.mode") == "list", "starts on the list")
    badges = q(f"[...{root}.querySelectorAll('#list li')].map(li => li.querySelector('.kind').textContent)")
    check(badges == ["Card", "Page", "Card + page", "Card + page"], f"each artifact shows its display type ({badges})")
    page.evaluate(f"{root}.querySelector('[data-f=card]').click()")
    shown = q(f"[...{root}.querySelectorAll('#list li')].map(li => li.dataset.id)")
    check(shown == ["timer", "clock", "old"], f"Cards filter keeps cards and both ({shown})")
    page.evaluate(f"{root}.querySelector('[data-f=page]').click()")
    shown = q(f"[...{root}.querySelectorAll('#list li')].map(li => li.dataset.id)")
    check(shown == ["recipes", "clock", "old"], f"Pages filter keeps pages and both ({shown})")
    page.evaluate(f"{root}.querySelector('[data-f=all]').click()")

    print("\n[real-size previews]")
    page.evaluate(f"{root}.querySelector('#list li[data-id=timer]').click()")
    page.wait_for_timeout(500)
    r = q(f"(() => {{ const b = {root}.querySelector('#preview').getBoundingClientRect(); return [Math.round(b.width), Math.round(b.height)]; }})()")
    check(r[1] == 220 and 300 <= r[0] <= 480, f"a card previews in a card-width frame at card_height ({r})")
    host = q(f"{root}.querySelector('#preview iframe').contentDocument === null ? 'sandboxed' : ''")
    page.wait_for_timeout(300)
    check(q("window.panel._runner.hostKind") == "card", "the runner reports host=card for a card")
    page.evaluate(f"{root}.querySelector('#back').click()")
    page.evaluate(f"{root}.querySelector('#list li[data-id=recipes]').click()")
    page.wait_for_timeout(500)
    r = q(f"(() => {{ const b = {root}.querySelector('#preview').getBoundingClientRect(); return [Math.round(b.width), Math.round(b.height)]; }})()")
    check(r[0] > 1000 and r[1] > 600, f"a page previews full size ({r})")
    check(q("window.panel._runner.hostKind") == "panel", "the runner reports host=panel for a page")
    page.evaluate(f"{root}.querySelector('#full').click()")
    check(q("window.panel._runner.hostKind") == "full", "full screen reports host=full")
    page.evaluate(f"{root}.querySelector('#exit').click()")
    check(q("window.panel._runner.hostKind") == "panel", "leaving full screen goes back to host=panel")

    print("\n[link and display settings]")
    page.evaluate("Object.defineProperty(navigator, 'clipboard', {value: {writeText: async (t) => { window.__copied = t; }}, configurable: true})")
    page.evaluate(f"{root}.querySelector('#link').click()")
    page.wait_for_timeout(200)
    check(q("window.__copied") == "http://harness.test/html-artifacts?artifact=recipes", f"Link copies the deep link ({q('window.__copied')})")
    page.evaluate(f"{root}.querySelector('#edit').click()")
    check(q(f"{root}.querySelector('#disp').value") == "page", "edit mode shows the display type")
    page.evaluate(f"{root}.querySelector('#disp').value = 'card'; {root}.querySelector('#cardh').value = '300'; {root}.querySelector('#disp').dispatchEvent(new Event('change'))")
    page.wait_for_timeout(300)
    sent = q("window.__msgs.filter(m => m.type.endsWith('/save')).at(-1)")
    check(sent and sent["display"] == "card" and sent["card_height"] == 300 and sent["html"] == PAGE, f"changing the display type saves display + card_height without new html ({sent and {k: sent[k] for k in ('display','card_height')}})")

    print("\n[ha.host inside the page]")
    p2 = browser.new_page(viewport={"width": 1280, "height": 800})
    p2.route("http://harness.test/**", lambda r: r.fulfill(status=200, content_type="text/html", body=html))
    p2.goto("http://harness.test/?artifact=timer")
    p2.evaluate("mountPanel(false)")
    p2.wait_for_timeout(1000)
    fr = [f for f in p2.frames if f != p2.main_frame]
    title = fr[0].evaluate("document.title") if fr else None
    check(title == "card:220", f"page sees ha.host='card' and ha.hostHeight=220 ({title})")
    check(fr[0].evaluate("document.documentElement.getAttribute('data-ha-host')") == "card", "data-ha-host attribute is set for CSS")

    print("\n[dashboard card and picker]")
    stub = page.evaluate("customElements.get('html-artifact-card').getStubConfig(hass)")
    check(stub == {"artifact": "timer", "height": 220}, f"new cards preselect the first card artifact with its card_height ({stub})")
    page.evaluate("""window.ed = document.createElement('html-artifact-card-editor'); document.body.appendChild(ed); ed.hass = hass; ed.setConfig({artifact: 'timer', height: 500});""")
    page.wait_for_timeout(400)
    opts = page.evaluate("[...ed.shadowRoot.querySelectorAll('#art option')].map(o => o.value)")
    check(opts == ["", "timer", "clock", "old"], f"picker lists cards first and hides pages ({opts})")
    page.evaluate("ed.shadowRoot.getElementById('pages').click()")
    opts = page.evaluate("[...ed.shadowRoot.querySelectorAll('#art option')].map(o => o.value)")
    check("recipes" in opts and opts.index("recipes") > opts.index("old"), f"'Show pages too' adds pages after the cards ({opts})")
    page.evaluate("ed.setConfig({artifact: 'recipes', height: 500})")
    check(page.evaluate("!ed.shadowRoot.getElementById('warn').hidden"), "choosing a page shows the 'built as a page' warning")
    page.evaluate("ed.setConfig({artifact: 'timer', height: 500})")
    check(page.evaluate("ed.shadowRoot.getElementById('warn').hidden"), "choosing a card shows no warning")
    # a card without a height of its own uses the artifact's card_height
    page.evaluate("""window.card = document.createElement('html-artifact-card'); card.setConfig({artifact: 'timer'}); document.body.appendChild(card); card.hass = hass;""")
    page.wait_for_timeout(700)
    h = page.evaluate("card.shadowRoot.querySelector('iframe').style.height")
    check(h == "220px", f"a card with no height uses the artifact's card_height ({h})")
    browser.close()

print()
print("display checks passed" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
raise SystemExit(1 if FAILS else 0)
