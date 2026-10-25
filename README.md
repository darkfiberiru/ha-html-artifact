# HTML Artifacts for Home Assistant

A place for **HTML pages that Claude generates** and that live inside Home Assistant:
editable by hand, shared across every device, and able to do things a static page can't.

Claude publishes a page (over MCP or REST). Home Assistant stores it, shows it in a sidebar
panel or dashboard card, and gives it a small `window.ha` API:

| Capability | What it gives a page |
|---|---|
| **Shared state** (`ha.kv`) | JSON state kept on the server, identical on every device, live-updating, survives restarts. Checklists, counters, notes, form values, scores. |
| **Server-side timers** (`ha.timers`) | Timers with named steps that keep running with the page closed, resume on any device, survive HA restarts, and push alerts + a live countdown to your phone. |
| **Entities & services** (`ha.entities`, `ha.callService`) | Read and subscribe to HA entities, call services, scoped to what you approved. |
| **Phone notifications** (`ha.notify`) | Notifications through your `notify.mobile_app_*` services. |
| **Asset files** (`ha.assets`) | Images, 3D models and data files stored on the HA server next to the page (up to 64 MB per artifact), for pages bigger than the 2 MB HTML limit. |

The cooking page is just one example. Anything Claude can write as one HTML file can use these:
trackers, kitchen displays, chore boards, planners, status walls, games.

```
 Claude ──MCP / REST──►  html_artifacts  ──►  sidebar panel  /  dashboard card
 (read, publish,         ├ versioned store     (code editor + live preview,
  merge, drive timers)   ├ shared kv state      permissions, history, revert)
                         ├ timer engine ──► phone notifications + bus events
                         └ sandboxed iframe, postMessage bridge = window.ha
```

## Install

1. Copy `custom_components/html_artifacts` into your HA config, or add this repo to HACS as a custom repository.
2. Restart, then **Settings > Devices & services > Add integration > HTML Artifacts**.
3. **Configure**: pick the `notify.mobile_app_*` service(s) for timer alerts.
4. An **Artifacts** entry appears in the sidebar. Requires HA 2024.11 or newer.

## Updates

The integration updates itself from GitHub and shows up under **Settings > System > Updates** like any other
update, with release notes and an **Install** button. After installing, restart Home Assistant (or turn on
*Restart automatically* under **Configure**).

- **Private repo:** under **Configure**, paste a GitHub token. Use a fine-grained token limited to this one
  repository with read-only *Contents* access. It is stored in HA's config entries and only sent to `api.github.com`.
- **What counts as a new version:** the latest GitHub *release* if there is one, otherwise the `version` in
  `manifest.json` on the default branch. To ship an update, bump `version` in both `manifest.json` and `const.py`
  (a test fails if they differ) and push; optionally tag a release for release notes.
- **HACS wins.** If HACS lists this repo as installed, the built-in updater doesn't create its entity (and removes an old one) so the two never disagree. Tick *Use the built-in updater even when HACS manages this integration* under **Configure** to override; it applies after a restart.
- **Checked every 6 hours**, or run *Check for updates* on the entity.
- Installing swaps `custom_components/html_artifacts` as a unit: the new copy is unpacked and validated first, and the old one is put back if the swap fails.
  Archives with links, unsafe paths or a different integration are refused. Your artifacts, state and timers live in HA storage and are untouched.
- If the folder is a symlink or mount (a git checkout), the install button refuses and tells you to `git pull` instead.

## Let Claude publish

**MCP (recommended).** Add HA's *Model Context Protocol Server* integration and select the
**HTML Artifacts** API, then point Claude Desktop / Claude Code at it. Claude gets
`artifact_authoring_guide`, `artifact_list`, `artifact_read`, `artifact_publish`, `artifact_kv_get/set`,
`artifact_timer_list/create/control` and `artifact_assets_list/import`, `artifact_asset_put/delete`. If your MCP server setup only allows one API at a
time, use REST below.

**REST.** `PUT /api/html_artifacts/<id>` with a long-lived token (see `tools/publish.py`):

```bash
export HA_URL=http://homeassistant.local:8123 HA_TOKEN=...
tools/publish.py pesto-tilapia examples/cook-timer.html --title "Pesto Tilapia" --caps '{"notify": true}'
tools/publish.py house-board  examples/house-board.html --caps '{"entities":["sensor.*temperature*","light.*"],"services":["light.toggle"]}'
```

**Hand edits and Claude edits don't clobber each other.** Updating an existing artifact requires
the `base_version` that was read. If you edited the page in the meantime, the publish is refused
with a `conflict` that carries your current HTML, so Claude merges instead of overwriting. Every
version is kept (last 20) and any can be restored from the panel.

## Asset files

A page is one HTML file of at most 2 MB, stored with its last 20 versions. Anything bulky (drawings,
photos, 3D models, data) goes in the artifact's **asset files** instead:

- Stored on disk in `<config>/html_artifacts/artifacts/<id>/`, not in `.storage` and not versioned.
  Deleted with the artifact. Never served from `/local`: the page gets them through the panel's
  authenticated bridge, so they work with the default no-network policy and need no permission.
- Limits: 8 MB per file, 64 MB and 1000 files per artifact. Paths like `img/plan.webp`, `models/base.stl`;
  allowed types are images, audio/video, json/txt/csv/md/css/js, fonts, pdf, stl/obj/glb/gltf/3mf/bin.
- In the page: `await ha.assets.get("models/base.stl")` (ArrayBuffer; or `"text"`, `"json"`, `"blob"`),
  `img.src = await ha.assets.url("img/plan.webp")`, `await ha.assets.list()`.

Getting files in, cheapest first (MCP):

1. `artifact_assets_import` with `source: "inbox:<name>.zip"`: unzips a zip already in
   `<config>/html_artifacts/inbox/` (for example copied there over the Samba share). The data never passes
   through the conversation; the zip is removed after a successful import.
2. `artifact_assets_import` with an `http(s)` URL of a zip that Home Assistant can reach (a LAN address is fine).
3. `artifact_asset_put` with base64, in chunks with `offset` and `final`, optionally checked with `sha256`.
   Every byte goes through Claude's context, so it suits small files.

Every zip member is checked before anything is written (plain relative paths, allowed types, no links,
size/count limits, no zip bombs). Over REST: `PUT /api/html_artifacts/<id>/assets/<path>` with the raw file,
or `tools/publish.py <id> page.html --assets <folder>`.

## `window.ha`

Wait for `await ha.ready`, then:

```js
// shared state: same on every phone, live
await ha.kv.set("checked", [1, 4]);   const v = await ha.kv.get("checked");
ha.kv.subscribe(({key, value, deleted}) => { ... });

// server-side timer: steps are offsets (seconds) from the start
const t = await ha.timers.create({
  label: "Pesto Tilapia",
  steps: [{at: 0, label: "Start rice"}, {at: 720, label: "Sear", detail: "4-5 min"}],
  duration: 1500, start: true });
ha.timers.subscribe(timers => ...);          // every change, from any device
const v = ha.timers.view(timers[0]);         // live: elapsed, remaining, current, next, nextIn
await ha.timers.pause(t.id);  resume  reset  skip(id[, toIndex])  extend(id, 60)  remove
ha.timers.fromDurations([{label: "Preheat", seconds: 240}, ...]);   // -> {steps, duration}
ha.timers.onFired(({kind, step}) => ...);    // while the page is open

// entities and services (need approval)
ha.entities.subscribe(["sensor.oven_*"], (state, entityId) => ...);
await ha.callService("light", "toggle", {}, {entity_id: "light.kitchen"});
await ha.notify("Oven is hot", "Kitchen");

// the artifact's own asset files (no permission needed)
const stl = await ha.assets.get("models/base.stl");          // ArrayBuffer
document.querySelector("img").src = await ha.assets.url("img/plan.webp");
```

The root element gets `data-theme="light|dark"` and the HA CSS variables, so pages can match the
theme. `ha.timers.view` counts from when the snapshot arrived, so phone and tablet clocks never
need to agree. The full reference Claude reads is `AUTHORING_GUIDE` in `llm_api.py`.

### Timers and your phone

A timer's step times are fixed offsets, so pausing, extending (`+1 min`) and skipping adjust them
consistently for everyone. For artifacts with the **notify** permission granted:

- an alert fires as each step is reached (time-sensitive on iOS, high priority on Android), with **+1 min** and **Pause** buttons;
- an ongoing notification shows the current/next step with a native countdown (Android `chronometer`) and **Next step / +1 min / Pause**; it clears when the timer ends;
- if HA was down when a step came due, only the latest step is announced, noting how many passed;
- every step also fires `html_artifacts_timer_step` / `html_artifacts_timer_finished` on the event bus for automations.

Notifications go to the services chosen under **Configure**, or a persistent notification if none.

## The panel

Open **Artifacts** in the sidebar. Picking an artifact shows the page **full size**; the toolbar has
**Edit** (list, code editor and live preview side by side, with Save, history, Delete and Permissions),
**Full screen** (hides Home Assistant's own chrome; **Esc** or the ✕ button exits) and **‹** back to the list.
The orange *Needs approval* badge opens the permissions dialog.

## Dashboard card

Add it from the dashboard editor (**Add card > HTML Artifact**) and pick the artifact and height in the visual editor,
or use YAML:

```yaml
type: custom:html-artifact-card
artifact: pesto-tilapia
height: 520        # or: auto
```

## Security model

- Pages run in `<iframe sandbox="allow-scripts ...">` **without** `allow-same-origin`: they cannot see your HA session, token or other pages. The only door is the `postMessage` bridge.
- A strict CSP is injected: no network (`connect-src 'none'`), images only from `data:`/`blob:`, scripts only inline or from cdnjs/jsDelivr/unpkg. Opt in per artifact with the `network` permission.
- A page starts with **no** access. It can *request* entities, services, notifications or network in `capabilities`; an **admin approves** them under Permissions (patterns like `light.*` allowed). Entity/service access is enforced in the bridge; notifications are enforced on the server.
- Artifacts cannot touch each other's state or timers; the server checks ownership.
- Limits: 2 MB per page, 500 keys x 256 KB state, 50 timers, 100 steps per timer.

## Examples

`examples/cook-timer.html` is a cook-along with a server-side timer and shared ingredient check-offs.
`examples/house-board.html` has nothing to do with cooking: live temperatures, light toggles and notes
shared by the household.

## Development and what has been verified

```bash
python -m unittest discover -s tests -v               # core logic + updater, no HA needed (41 tests)
node tests/frontend_check.js                          # bridge protocol, CSP, helpers (15 checks)
NODE_PATH=<jsdom>/node_modules node tests/example_pages.js   # example pages in jsdom through the real bridge
<venv-with-homeassistant>/bin/python tests/smoke_ha.py        # real HA 2026.2: websocket, MCP tools, REST, notifications
<venv-with-homeassistant>/bin/python tests/smoke_update.py    # real HA: update entity (GitHub faked)
python tests/panel_layout.py [--shots DIR]                    # real Chromium (pip install playwright): panel modes, sizing, card + visual editor
```

Verified against Home Assistant 2026.2.3: all modules import; websocket commands, the MCP tools
(including their JSON-schema conversion), REST views, admin gating, and the timer to
notification path run correctly. **Not yet verified:** the update entity against real GitHub and in the Updates UI, the panel and card inside a running Home Assistant
(layout is checked in headless Chromium with a fake `hass`), panel registration and static file serving through a running HA, the config/options flow UI,
and how the companion apps render the notification payload (the Android `chronometer`/`when`
countdown and action buttons follow the companion-app docs but are untested on a device).
