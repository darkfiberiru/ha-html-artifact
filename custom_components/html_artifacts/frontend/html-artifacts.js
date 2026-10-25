/*
 * HTML Artifacts - Home Assistant frontend.
 *
 * Defines three things in one module:
 *   - bridgeMain():        the script injected into every artifact iframe; it
 *                          exposes window.ha (kv, timers, entities, services, notify).
 *   - ArtifactRunner:      mounts an artifact in a sandboxed iframe and serves
 *                          the bridge's requests, enforcing the artifact's grants.
 *   - <html-artifacts-panel> (sidebar editor) and <html-artifact-card> (dashboard).
 *
 * Trust boundary: the iframe is sandboxed WITHOUT allow-same-origin, so artifact
 * code can only reach Home Assistant through the postMessage bridge below.
 */
(() => {
  "use strict";

  const WS = "html_artifacts/";
  const LS_LAST = "html_artifacts:last";

  // ------------------------------------------------------------------------
  // Helpers
  // ------------------------------------------------------------------------

  const esc = (s) =>
    String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const globToRegExp = (pattern) =>
    new RegExp(
      "^" + pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".") + "$",
      "i"
    );
  const matchesAny = (patterns, value) => (patterns || []).some((p) => globToRegExp(p).test(value));

  const pickState = (s) =>
    s
      ? {
          entity_id: s.entity_id,
          state: s.state,
          attributes: s.attributes,
          last_changed: s.last_changed,
          last_updated: s.last_updated,
        }
      : null;

  function buildCsp(grants) {
    const net = !!(grants && grants.network);
    return [
      "default-src 'none'",
      "script-src 'unsafe-inline' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://unpkg.com",
      "style-src 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com",
      "font-src data: https://fonts.gstatic.com https://cdnjs.cloudflare.com",
      net ? "img-src data: blob: https:" : "img-src data: blob:",
      net ? "media-src data: blob: https:" : "media-src data: blob:",
      net ? "connect-src https:" : "connect-src 'none'",
      "base-uri 'none'",
      "form-action 'none'",
    ].join("; ");
  }

  function injectIntoHead(html, snippet) {
    let m = /<head[^>]*>/i.exec(html);
    if (m) return html.slice(0, m.index + m[0].length) + snippet + html.slice(m.index + m[0].length);
    m = /<html[^>]*>/i.exec(html);
    if (m) {
      const at = m.index + m[0].length;
      return html.slice(0, at) + "<head>" + snippet + "</head>" + html.slice(at);
    }
    m = /<!doctype[^>]*>/i.exec(html);
    if (m) {
      const at = m.index + m[0].length;
      return html.slice(0, at) + "<meta charset=utf-8>" + snippet + html.slice(at);
    }
    return "<!doctype html><meta charset=utf-8>" + snippet + html;
  }

  function pendingCaps(a) {
    const r = a.caps_requested || {};
    const g = a.caps_granted || {};
    const missing = (k) => (r[k] || []).some((x) => !(g[k] || []).includes(x));
    return missing("entities") || missing("services") || (r.notify && !g.notify) || (r.network && !g.network);
  }

  // ------------------------------------------------------------------------
  // The bridge: runs INSIDE the sandboxed iframe (serialised with toString()).
  // It must not reference anything from the surrounding module.
  // ------------------------------------------------------------------------

  function bridgeMain() {
    "use strict";
    const pending = new Map();
    let seq = 0;
    let ready = false;
    const kvSubs = new Set();
    const timerSubs = new Set();
    const firedSubs = new Set();
    const entitySubs = [];
    const timers = new Map();

    const post = (msg) => parent.postMessage(Object.assign({ ha: 1 }, msg), "*");
    const call = (method, params) =>
      new Promise((resolve, reject) => {
        const id = ++seq;
        pending.set(id, { resolve, reject });
        post({ id, method, params: params || {} });
      });
    const stamp = (t) => {
      t._recv = performance.now();
      return t;
    };

    function view(t) {
      const dt = t.state === "running" ? (performance.now() - t._recv) / 1000 : 0;
      const elapsed = Math.min(t.duration, t.elapsed_now + dt);
      let cur = -1;
      t.steps.forEach((s, i) => {
        if (s.at <= elapsed + 0.25) cur = i;
      });
      const nxt = cur + 1 < t.steps.length ? cur + 1 : -1;
      const nextAt = nxt >= 0 ? t.steps[nxt].at : t.duration;
      return Object.assign({}, t, {
        elapsed,
        remaining: Math.max(0, t.duration - elapsed),
        currentIndex: cur,
        current: cur >= 0 ? t.steps[cur] : null,
        nextIndex: nxt,
        next: nxt >= 0 ? t.steps[nxt] : null,
        nextIn: Math.max(0, nextAt - elapsed),
      });
    }
    const snapshot = () => Array.from(timers.values()).map(view);
    const emitTimers = () => {
      const snap = snapshot();
      timerSubs.forEach((cb) => {
        try {
          cb(snap);
        } catch (e) {
          console.error(e);
        }
      });
    };
    const keep = (t) => {
      timers.set(t.id, stamp(t));
      emitTimers();
      return view(timers.get(t.id));
    };

    function applyTheme(theme) {
      if (!theme) return;
      const root = document.documentElement;
      root.setAttribute("data-theme", theme.dark ? "dark" : "light");
      root.style.colorScheme = theme.dark ? "dark" : "light";
      Object.keys(theme.vars || {}).forEach((k) => root.style.setProperty(k, theme.vars[k]));
    }

    function onEvent(ev) {
      switch (ev.type) {
        case "kv":
          kvSubs.forEach((cb) => cb({ key: ev.key, value: ev.value, deleted: !!ev.deleted }));
          break;
        case "timer":
          if (ev.change === "delete") {
            timers.delete(ev.timer.id);
            emitTimers();
          } else {
            keep(ev.timer);
          }
          break;
        case "timer_fired":
          firedSubs.forEach((cb) => cb({ kind: ev.kind, timer: view(stamp(ev.timer)), step: ev.step, index: ev.index, missed: ev.missed }));
          break;
        case "state":
          entitySubs.forEach((s) => {
            if (s.ids.has(ev.entity_id)) s.cb(ev.state, ev.entity_id);
          });
          break;
        case "theme":
          applyTheme(ev.theme);
          break;
        case "grants":
          ha.grants = ev.grants;
          break;
        default:
      }
    }

    window.addEventListener("message", (e) => {
      if (e.source !== parent) return;
      const m = e.data;
      if (!m || m.ha !== 1) return;
      if (m.id) {
        const p = pending.get(m.id);
        if (!p) return;
        pending.delete(m.id);
        if (m.error) {
          const err = new Error(m.error.message || "error");
          err.code = m.error.code;
          p.reject(err);
        } else {
          p.resolve(m.result);
        }
      } else if (m.event) {
        onEvent(m.event);
      }
    });

    const control = (id, action, extra) =>
      call("timer.control", Object.assign({ id, action }, extra || {})).then((t) => (t ? keep(t) : null));

    const ha = {
      version: 1,
      user: null,
      artifact: null,
      grants: null,
      ready: null,
      kv: {
        get: (key) => call("kv.get", { key }),
        set: (key, value) => call("kv.set", { key, value }),
        delete: (key) => call("kv.delete", { key }),
        list: () => call("kv.list"),
        subscribe(cb) {
          kvSubs.add(cb);
          return () => kvSubs.delete(cb);
        },
      },
      timers: {
        create: (opts) => call("timer.create", opts || {}).then(keep),
        list: () => call("timer.list").then((list) => list.map(keep)),
        start: (id) => control(id, "start"),
        pause: (id) => control(id, "pause"),
        resume: (id) => control(id, "resume"),
        reset: (id) => control(id, "reset"),
        skip: (id, toIndex) => control(id, "skip", { to_index: toIndex }),
        extend: (id, seconds) => control(id, "extend", { seconds }),
        remove: (id) => control(id, "delete"),
        view,
        fromDurations(items) {
          let at = 0;
          const steps = items.map((it) => {
            const step = { at, label: it.label, detail: it.detail, notify: it.notify };
            at += it.seconds;
            return step;
          });
          return { steps, duration: at };
        },
        subscribe(cb) {
          timerSubs.add(cb);
          if (ready) cb(snapshot());
          return () => timerSubs.delete(cb);
        },
        onFired(cb) {
          firedSubs.add(cb);
          return () => firedSubs.delete(cb);
        },
      },
      entities: {
        get: (ids) => call("entities.get", { entity_ids: ids }).then((m) => (Array.isArray(ids) ? m : m[ids])),
        subscribe(ids, cb) {
          const sub = { ids: new Set(), cb };
          entitySubs.push(sub);
          call("entities.subscribe", { entity_ids: ids })
            .then((m) => {
              Object.keys(m).forEach((id) => {
                sub.ids.add(id);
                cb(m[id], id);
              });
            })
            .catch((e) => console.warn("ha.entities.subscribe:", e.message));
          return () => {
            const i = entitySubs.indexOf(sub);
            if (i >= 0) entitySubs.splice(i, 1);
          };
        },
      },
      callService: (domain, service, data, target) => call("service.call", { domain, service, data, target }),
      notify: (message, title) => call("notify", { message, title }),
    };

    ha.ready = call("hello").then((info) => {
      ha.user = info.user;
      ha.artifact = info.artifact;
      ha.grants = info.grants;
      timers.clear();
      info.timers.forEach((t) => timers.set(t.id, stamp(t)));
      applyTheme(info.theme);
      ready = true;
      emitTimers();
      return info;
    });
    window.ha = ha;

    try {
      new ResizeObserver(() =>
        post({ notify: "resize", height: Math.ceil(document.documentElement.scrollHeight) })
      ).observe(document.documentElement);
    } catch (e) {
      /* no ResizeObserver */
    }
  }

  function composeDocument(html, grants) {
    const snippet =
      '<meta http-equiv="Content-Security-Policy" content="' +
      esc(buildCsp(grants)) +
      '"><script>(' +
      bridgeMain.toString() +
      ")();<\/script>";
    return injectIntoHead(html, snippet);
  }

  // ------------------------------------------------------------------------
  // Runner: one iframe + the parent side of the bridge
  // ------------------------------------------------------------------------

  class ArtifactRunner {
    constructor(opts) {
      this.hass = opts.hass;
      this.host = opts.host;
      this.autoHeight = !!opts.autoHeight;
      this.height = opts.height || 0;
      this.onRemoteChange = opts.onRemoteChange || null;
      this.artifact = null;
      this.grants = {};
      this.iframe = null;
      this._wanted = new Set();
      this._last = new Map();
      this._unsub = null;
      this._dark = null;
      this._onMessage = this._onMessage.bind(this);
    }

    async start() {
      window.addEventListener("message", this._onMessage);
      try {
        this._unsub = await this.hass.connection.subscribeMessage((ev) => this._onServerEvent(ev), {
          type: WS + "subscribe",
        });
      } catch (err) {
        console.error("html_artifacts: subscribe failed", err);
      }
    }

    destroy() {
      window.removeEventListener("message", this._onMessage);
      if (this._unsub) {
        this._unsub();
        this._unsub = null;
      }
      if (this.iframe) this.iframe.remove();
      this.iframe = null;
    }

    mount(artifact, html) {
      this.artifact = artifact;
      this.grants = artifact.caps_granted || {};
      this._wanted.clear();
      this._last.clear();
      const frame = document.createElement("iframe");
      frame.setAttribute("sandbox", "allow-scripts allow-forms allow-modals allow-downloads");
      frame.setAttribute("allow", "screen-wake-lock; clipboard-write; fullscreen");
      frame.setAttribute("title", artifact.title || artifact.id);
      const h = this.autoHeight ? "300px" : this.height ? this.height + "px" : "100%";
      frame.style.cssText = `border:0;width:100%;height:${h};display:block;background:transparent`;
      frame.srcdoc = composeDocument(html !== undefined ? html : artifact.html, this.grants);
      if (this.iframe) this.iframe.remove();
      this.iframe = frame;
      this.host.replaceChildren(frame);
    }

    updateHass(hass) {
      this.hass = hass;
      if (!this.iframe) return;
      for (const id of this._wanted) {
        const s = hass.states[id];
        if (s !== this._last.get(id)) {
          this._last.set(id, s);
          this._post({ event: { type: "state", entity_id: id, state: pickState(s) } });
        }
      }
      const dark = !!(hass.themes && hass.themes.darkMode);
      if (this._dark !== null && dark !== this._dark) {
        this._post({ event: { type: "theme", theme: this._theme() } });
      }
    }

    _theme() {
      const dark = !!(this.hass.themes && this.hass.themes.darkMode);
      this._dark = dark;
      const cs = getComputedStyle(this.host);
      const vars = {};
      [
        "--primary-color", "--accent-color", "--primary-text-color", "--secondary-text-color",
        "--card-background-color", "--primary-background-color", "--divider-color",
        "--error-color", "--success-color", "--warning-color",
      ].forEach((name) => {
        const v = cs.getPropertyValue(name).trim();
        if (v) vars[name] = v;
      });
      return { dark, vars };
    }

    _post(msg) {
      if (this.iframe && this.iframe.contentWindow) {
        this.iframe.contentWindow.postMessage(Object.assign({ ha: 1 }, msg), "*");
      }
    }

    _ws(type, extra) {
      return this.hass.connection.sendMessagePromise(
        Object.assign({ type: WS + type, artifact_id: this.artifact.id }, extra || {})
      );
    }

    _onServerEvent(ev) {
      const id = this.artifact && this.artifact.id;
      if (!id) return;
      if (ev.type === "kv") {
        if (ev.artifact_id === id) this._post({ event: ev });
      } else if (ev.type === "timer" || ev.type === "timer_fired") {
        if (ev.timer && ev.timer.artifact_id === id) this._post({ event: ev });
      } else if (ev.type === "artifact" && ev.artifact && ev.artifact.id === id) {
        this.grants = ev.artifact.caps_granted || {};
        this._post({ event: { type: "grants", grants: this.grants } });
        if (this.onRemoteChange) this.onRemoteChange(ev);
      }
    }

    async _onMessage(e) {
      if (!this.iframe || e.source !== this.iframe.contentWindow) return;
      const m = e.data;
      if (!m || m.ha !== 1) return;
      if (m.notify === "resize") {
        if (this.autoHeight && Number.isFinite(m.height)) this.iframe.style.height = Math.max(60, m.height) + "px";
        return;
      }
      if (!m.id) return;
      try {
        const result = await this._dispatch(m.method, m.params || {});
        this._post({ id: m.id, result: result === undefined ? null : result });
      } catch (err) {
        const e2 = err || {};
        this._post({ id: m.id, error: { code: e2.code || "error", message: e2.message || String(err) } });
      }
    }

    _forbidden(what) {
      return Object.assign(new Error(`This artifact has not been granted ${what}. Approve it under Permissions.`), {
        code: "forbidden",
      });
    }

    _checkEntity(id) {
      if (typeof id !== "string" || !matchesAny(this.grants.entities, id)) throw this._forbidden(`access to ${id}`);
    }

    _expand(ids) {
      const out = [];
      for (const id of [].concat(ids)) {
        if (/[*?]/.test(id)) {
          const re = globToRegExp(id);
          Object.keys(this.hass.states)
            .filter((k) => re.test(k) && matchesAny(this.grants.entities, k))
            .slice(0, 200)
            .forEach((k) => out.push(k));
        } else {
          out.push(id);
        }
      }
      return out;
    }

    async _dispatch(method, p) {
      switch (method) {
        case "hello": {
          const t = await this._ws("timers/list");
          const u = this.hass.user || {};
          return {
            user: { id: u.id, name: u.name, is_admin: !!u.is_admin },
            artifact: { id: this.artifact.id, title: this.artifact.title, version: this.artifact.version },
            grants: this.grants,
            timers: t.timers,
            theme: this._theme(),
          };
        }
        case "kv.get":
          return (await this._ws("kv/get", { key: p.key })).value;
        case "kv.set":
          await this._ws("kv/set", { key: p.key, value: p.value });
          return true;
        case "kv.delete":
          await this._ws("kv/delete", { key: p.key });
          return true;
        case "kv.list":
          return (await this._ws("kv/list")).values;
        case "timer.list":
          return (await this._ws("timers/list")).timers;
        case "timer.create":
          return (
            await this._ws("timers/create", {
              label: p.label,
              steps: p.steps || [],
              duration: p.duration,
              start: !!p.start,
              notify_targets: p.notify_targets,
            })
          ).timer;
        case "timer.control":
          return (
            await this._ws("timers/control", {
              timer_id: p.id,
              action: p.action,
              to_index: p.to_index,
              seconds: p.seconds,
            })
          ).timer;
        case "entities.get": {
          const out = {};
          for (const id of [].concat(p.entity_ids)) {
            this._checkEntity(id);
            out[id] = pickState(this.hass.states[id]);
          }
          return out;
        }
        case "entities.subscribe": {
          const out = {};
          for (const id of this._expand(p.entity_ids)) {
            this._checkEntity(id);
            const s = this.hass.states[id];
            this._wanted.add(id);
            this._last.set(id, s);
            out[id] = pickState(s);
          }
          return out;
        }
        case "service.call": {
          if (typeof p.domain !== "string" || typeof p.service !== "string") throw this._forbidden("that service");
          if (!matchesAny(this.grants.services, `${p.domain}.${p.service}`)) {
            throw this._forbidden(`the service ${p.domain}.${p.service}`);
          }
          await this.hass.callService(p.domain, p.service, p.data || {}, p.target);
          return true;
        }
        case "notify":
          await this._ws("notify", { title: p.title, message: p.message });
          return true;
        default:
          throw Object.assign(new Error("unknown method " + method), { code: "unknown_method" });
      }
    }
  }

  // ------------------------------------------------------------------------
  // Sidebar panel
  // ------------------------------------------------------------------------

  const STARTER = (name) => `<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${esc(name)}</title>
<style>
  :root { --bg: #fff; --fg: #1b1b1f; --accent: #2c6a4c; }
  :root[data-theme="dark"] { --bg: #16181a; --fg: #e8e8ea; --accent: #84c7a3; }
  body { margin: 0; padding: 20px; font: 16px system-ui, sans-serif; background: var(--bg); color: var(--fg); }
  button { font: inherit; padding: 10px 16px; border-radius: 10px; border: 0; background: var(--accent); color: var(--bg); }
</style>
</head>
<body>
<h1>${esc(name)}</h1>
<p>Taps so far, shared by every device: <b id="n">...</b></p>
<button id="tap">Tap</button>
<script>
(async () => {
  await ha.ready;
  const show = (v) => (document.getElementById("n").textContent = v || 0);
  show(await ha.kv.get("taps"));
  ha.kv.subscribe(({ key, value }) => key === "taps" && show(value));
  document.getElementById("tap").onclick = async () =>
    ha.kv.set("taps", ((await ha.kv.get("taps")) || 0) + 1);
})();
<\/script>
</body>
</html>
`;

  const PANEL_CSS = `
    :host { display: block; height: 100%; color: var(--primary-text-color); background: var(--primary-background-color); }
    * { box-sizing: border-box; }
    .app { display: grid; grid-template-columns: 240px 1fr; height: 100%; min-height: 0; }
    @media (max-width: 800px) { .app { grid-template-columns: 1fr; grid-template-rows: auto 1fr; } aside { max-height: 28vh; } }
    aside { border-right: 1px solid var(--divider-color); overflow: auto; background: var(--card-background-color); }
    .side-head { display: flex; align-items: center; gap: 8px; padding: 10px 12px; position: sticky; top: 0; background: var(--card-background-color); border-bottom: 1px solid var(--divider-color); }
    .side-head b { flex: 1; }
    ul { list-style: none; margin: 0; padding: 0; }
    li { padding: 10px 14px; cursor: pointer; border-bottom: 1px solid var(--divider-color); display: grid; gap: 2px; }
    li:hover { background: color-mix(in srgb, var(--primary-color) 8%, transparent); }
    li.sel { background: color-mix(in srgb, var(--primary-color) 16%, transparent); }
    li .t { font-weight: 600; display: flex; align-items: center; gap: 8px; }
    li .s { font-size: 12px; color: var(--secondary-text-color); }
    .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--warning-color, #e6a700); flex: none; }
    .main { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
    .bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding: 10px 12px; border-bottom: 1px solid var(--divider-color); }
    .bar input { flex: 1 1 160px; min-width: 120px; }
    input, select, textarea, button { font: inherit; color: var(--primary-text-color); background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 8px; padding: 7px 10px; }
    button { cursor: pointer; }
    button.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: transparent; }
    button.icon { padding: 4px 10px; }
    button:disabled { opacity: .5; cursor: default; }
    .pill { font-size: 12px; font-weight: 700; padding: 3px 10px; border-radius: 999px; border: 1.5px solid; }
    .pill.pending { color: var(--warning-color, #b07a00); }
    .pill.ok { color: var(--success-color, #2e7d32); }
    .banner { padding: 10px 14px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; background: color-mix(in srgb, var(--warning-color, #e6a700) 20%, transparent); border-bottom: 1px solid var(--divider-color); }
    .split { flex: 1; display: grid; grid-template-columns: 1fr 1fr; min-height: 0; }
    @media (max-width: 1100px) { .split { grid-template-columns: 1fr; grid-template-rows: 1fr 1fr; } }
    #editor { border: 0; border-right: 1px solid var(--divider-color); border-radius: 0; resize: none; font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; tab-size: 2; white-space: pre; background: var(--code-editor-background-color, var(--card-background-color)); min-height: 0; }
    .preview { min-height: 0; overflow: auto; background: var(--card-background-color); }
    .empty { padding: 24px; color: var(--secondary-text-color); }
    dialog { border: 1px solid var(--divider-color); border-radius: 14px; background: var(--card-background-color); color: var(--primary-text-color); max-width: 520px; width: calc(100% - 32px); }
    dialog label { display: grid; gap: 4px; margin: 10px 0; font-size: 14px; }
    dialog label.chk { display: flex; gap: 8px; align-items: center; }
    dialog textarea { width: 100%; font: 13px ui-monospace, monospace; }
    .row { display: flex; gap: 8px; align-items: center; margin-top: 14px; }
    .grow { flex: 1; }
    .muted { color: var(--secondary-text-color); font-size: 13px; }
    #toast { position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%); background: var(--primary-text-color); color: var(--card-background-color); padding: 8px 16px; border-radius: 999px; opacity: 0; transition: opacity .2s; pointer-events: none; }
    #toast.on { opacity: .92; }
  `;

  const PANEL_HTML = `
    <style>${PANEL_CSS}</style>
    <div class="app">
      <aside>
        <div class="side-head"><button id="menu" class="icon" hidden>☰</button><b>Artifacts</b><button id="new" class="icon" title="New artifact">+</button></div>
        <ul id="list"></ul>
      </aside>
      <section class="main">
        <div class="bar">
          <input id="title" placeholder="Title" disabled>
          <span id="pill" class="pill" hidden></span>
          <select id="hist" title="Version history" disabled></select>
          <button id="perms" disabled>Permissions</button>
          <button id="del" disabled>Delete</button>
          <button id="save" class="primary" disabled>Save</button>
        </div>
        <div id="banner" class="banner" hidden></div>
        <div class="split">
          <textarea id="editor" spellcheck="false" placeholder="Pick an artifact on the left, or create one with +" disabled></textarea>
          <div id="preview" class="preview"><div class="empty">The live preview appears here.</div></div>
        </div>
      </section>
      <dialog id="dlg">
        <h3 style="margin:0 0 4px">Permissions</h3>
        <div id="dlg-req" class="muted"></div>
        <label>Entities this page may read (one pattern per line)<textarea id="g-ent" rows="4" placeholder="sensor.oven_*"></textarea></label>
        <label>Services this page may call (domain.service per line)<textarea id="g-svc" rows="4" placeholder="light.turn_on"></textarea></label>
        <label class="chk"><input type="checkbox" id="g-notify"> Send phone notifications and timer alerts</label>
        <label class="chk"><input type="checkbox" id="g-net"> Allow network access (external images, fetch)</label>
        <div class="row"><button id="g-fill">Fill from requested</button><span class="grow"></span><button id="g-cancel">Cancel</button><button id="g-save" class="primary">Save</button></div>
      </dialog>
      <div id="toast"></div>
    </div>
  `;

  class HtmlArtifactsPanel extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: "open" });
      this._list = [];
      this._cur = null;
      this._dirty = false;
      this._saving = false;
      this._built = false;
      this._started = false;
      this._runner = null;
      this._previewTimer = null;
    }

    $(id) {
      return this.shadowRoot.getElementById(id);
    }

    set hass(hass) {
      this._hass = hass;
      if (!this._built) this._build();
      if (this._runner) this._runner.updateHass(hass);
      if (!this._started) {
        this._started = true;
        this._start();
      }
    }
    get hass() {
      return this._hass;
    }

    set narrow(v) {
      this._narrow = v;
      if (this._built) this.$("menu").hidden = !v;
    }

    disconnectedCallback() {
      if (this._unsub) this._unsub();
      this._unsub = null;
      if (this._runner) this._runner.destroy();
      this._runner = null;
      this._started = false;
    }

    _ws(type, extra) {
      return this._hass.connection.sendMessagePromise(Object.assign({ type: WS + type }, extra || {}));
    }

    _toast(text) {
      const t = this.$("toast");
      t.textContent = text;
      t.classList.add("on");
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => t.classList.remove("on"), 2800);
    }

    _build() {
      this._built = true;
      this.shadowRoot.innerHTML = PANEL_HTML;
      this.$("menu").hidden = !this._narrow;
      this.$("menu").onclick = () =>
        this.dispatchEvent(new Event("hass-toggle-menu", { bubbles: true, composed: true }));
      this.$("new").onclick = () => this._create();
      this.$("save").onclick = () => this._save();
      this.$("del").onclick = () => this._delete();
      this.$("perms").onclick = () => this._openPerms();
      this.$("title").oninput = () => this._markDirty();
      const editor = this.$("editor");
      editor.oninput = () => {
        this._markDirty();
        clearTimeout(this._previewTimer);
        this._previewTimer = setTimeout(() => this._renderPreview(), 500);
      };
      editor.onkeydown = (e) => {
        if (e.key === "Tab") {
          e.preventDefault();
          const { selectionStart: s, selectionEnd: en } = editor;
          editor.setRangeText("  ", s, en, "end");
          editor.dispatchEvent(new Event("input"));
        } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
          e.preventDefault();
          this._save();
        }
      };
      this.$("list").onclick = (e) => {
        const li = e.target.closest("li[data-id]");
        if (li) this._guardedOpen(li.dataset.id);
      };
      this.$("hist").onchange = (e) => {
        const v = Number(e.target.value);
        e.target.value = "";
        if (v) this._revert(v);
      };
      this.$("g-cancel").onclick = () => this.$("dlg").close();
      this.$("g-fill").onclick = () => this._fillGrants(this._cur.caps_requested || {});
      this.$("g-save").onclick = () => this._saveGrants();
      this._runner = new ArtifactRunner({ hass: this._hass, host: this.$("preview") });
    }

    async _start() {
      try {
        await this._runner.start();
        this._list = (await this._ws("list")).artifacts;
        this._renderList();
        this._unsub = await this._hass.connection.subscribeMessage((ev) => this._onEvent(ev), {
          type: WS + "subscribe",
        });
        const last = localStorage.getItem(LS_LAST);
        const first = this._list.find((a) => a.id === last) || this._list[0];
        if (first) await this._open(first.id);
      } catch (err) {
        this._toast("Could not load artifacts: " + (err.message || err.code));
      }
    }

    _renderList() {
      const list = this.$("list");
      if (!this._list.length) {
        list.innerHTML = '<li><span class="s">No artifacts yet. Ask Claude to publish one, or press +.</span></li>';
        return;
      }
      list.innerHTML = this._list
        .map(
          (a) => `<li data-id="${esc(a.id)}" class="${this._cur && this._cur.id === a.id ? "sel" : ""}">
            <span class="t">${pendingCaps(a) ? '<span class="dot" title="Permissions requested"></span>' : ""}${esc(a.title)}</span>
            <span class="s">v${a.version} · ${esc(a.updated_by)}</span></li>`
        )
        .join("");
    }

    _markDirty() {
      this._dirty = true;
      this.$("save").disabled = !this._cur;
    }

    _banner(text, actions) {
      const b = this.$("banner");
      b.replaceChildren();
      if (!text) {
        b.hidden = true;
        return;
      }
      const span = document.createElement("span");
      span.textContent = text;
      b.append(span);
      (actions || []).forEach(([label, fn]) => {
        const btn = document.createElement("button");
        btn.textContent = label;
        btn.onclick = fn;
        b.append(btn);
      });
      b.hidden = false;
    }

    async _guardedOpen(id) {
      if (this._cur && this._cur.id === id) return;
      if (this._dirty && !confirm("Discard unsaved changes?")) return;
      await this._open(id);
    }

    async _open(id) {
      try {
        this._cur = await this._ws("get", { artifact_id: id });
      } catch (err) {
        this._toast(err.message || "Could not open artifact");
        return;
      }
      this._dirty = false;
      localStorage.setItem(LS_LAST, id);
      this.$("editor").value = this._cur.html;
      this.$("title").value = this._cur.title;
      for (const el of ["editor", "title", "hist", "perms", "del"]) this.$(el).disabled = false;
      this.$("save").disabled = true;
      this._banner(null);
      this._renderMeta();
      this._renderList();
      this._renderPreview();
      this._refreshHistory();
    }

    _renderMeta() {
      const pill = this.$("pill");
      const a = this._cur;
      const needs = pendingCaps(a);
      const r = a.caps_requested || {};
      const wants = (r.entities || []).length || (r.services || []).length || r.notify || r.network;
      pill.hidden = !wants;
      pill.className = "pill " + (needs ? "pending" : "ok");
      pill.textContent = needs ? "Permissions requested" : "Permissions approved";
    }

    _renderPreview() {
      if (!this._cur || !this._runner) return;
      this._runner.mount(this._cur, this.$("editor").value);
    }

    async _refreshHistory() {
      const sel = this.$("hist");
      sel.innerHTML = '<option value="">History</option>';
      if (!this._cur) return;
      try {
        const { history } = await this._ws("history", { artifact_id: this._cur.id });
        sel.innerHTML =
          '<option value="">History (v' + this._cur.version + ")</option>" +
          history
            .map((h) => `<option value="${h.version}">v${h.version} · ${esc(h.updated_by)} · ${new Date(h.updated * 1000).toLocaleString()}</option>`)
            .join("");
      } catch (err) {
        /* history is optional */
      }
    }

    async _save(overwriteVersion) {
      if (!this._cur) return;
      const id = this._cur.id;
      this._saving = true;
      try {
        this._cur = await this._ws("save", {
          artifact_id: id,
          html: this.$("editor").value,
          title: this.$("title").value.trim() || id,
          base_version: overwriteVersion || this._cur.version,
        });
        this._dirty = false;
        this.$("save").disabled = true;
        this._banner(null);
        this._renderMeta();
        this._refreshHistory();
        this._toast("Saved v" + this._cur.version);
      } catch (err) {
        if (err && err.code === "conflict" && err.current) {
          this._conflict(err.current);
        } else {
          this._toast((err && err.message) || "Save failed");
        }
      } finally {
        this._saving = false;
      }
    }

    _conflict(current) {
      this._banner(`Changed by ${current.updated_by} (v${current.version}) while you were editing.`, [
        ["Load their version", () => this._open(current.id)],
        ["Overwrite with mine", () => this._save(current.version)],
      ]);
    }

    async _revert(version) {
      if (!confirm(`Restore v${version}? It becomes a new version; nothing is lost.`)) return;
      try {
        await this._ws("revert", { artifact_id: this._cur.id, version });
        await this._open(this._cur.id);
      } catch (err) {
        this._toast(err.message || "Revert failed");
      }
    }

    async _create() {
      const name = prompt("Name for the new artifact");
      if (!name) return;
      const id = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64);
      if (!id) {
        this._toast("Use letters or digits in the name");
        return;
      }
      if (this._list.some((a) => a.id === id)) {
        this._guardedOpen(id);
        return;
      }
      try {
        await this._ws("save", { artifact_id: id, html: STARTER(name), title: name });
        await this._open(id);
      } catch (err) {
        this._toast(err.message || "Could not create artifact");
      }
    }

    async _delete() {
      if (!this._cur || !confirm(`Delete "${this._cur.title}", its saved state and its timers? This cannot be undone.`)) return;
      try {
        await this._ws("delete", { artifact_id: this._cur.id });
      } catch (err) {
        this._toast(err.message || "Delete failed (admin only)");
      }
    }

    _openPerms() {
      const r = this._cur.caps_requested || {};
      const parts = [];
      if ((r.entities || []).length) parts.push("entities: " + r.entities.join(", "));
      if ((r.services || []).length) parts.push("services: " + r.services.join(", "));
      if (r.notify) parts.push("notifications");
      if (r.network) parts.push("network access");
      this.$("dlg-req").textContent = parts.length
        ? "The page asks for " + parts.join("; ") + "."
        : "The page has not asked for any permissions.";
      this._fillGrants(this._cur.caps_granted || {});
      this.$("dlg").showModal();
    }

    _fillGrants(g) {
      this.$("g-ent").value = (g.entities || []).join("\n");
      this.$("g-svc").value = (g.services || []).join("\n");
      this.$("g-notify").checked = !!g.notify;
      this.$("g-net").checked = !!g.network;
    }

    async _saveGrants() {
      const lines = (id) => this.$(id).value.split("\n").map((s) => s.trim()).filter(Boolean);
      try {
        const updated = await this._ws("set_grants", {
          artifact_id: this._cur.id,
          grants: {
            entities: lines("g-ent"),
            services: lines("g-svc"),
            notify: this.$("g-notify").checked,
            network: this.$("g-net").checked,
          },
        });
        this._cur = Object.assign({}, this._cur, updated);
        this.$("dlg").close();
        this._renderMeta();
        this._renderPreview();
        this._toast("Permissions saved");
      } catch (err) {
        this._toast(err.message || "Only administrators can change permissions");
      }
    }

    _onEvent(ev) {
      if (ev.type !== "artifact") return;
      const a = ev.artifact;
      if (ev.change === "delete") {
        this._list = this._list.filter((x) => x.id !== a.id);
      } else {
        const i = this._list.findIndex((x) => x.id === a.id);
        if (i >= 0) this._list[i] = a;
        else this._list.unshift(a);
      }
      const isCur = this._cur && this._cur.id === a.id;
      if (isCur && ev.change === "delete") {
        this._cur = null;
        this._dirty = false;
        this.$("editor").value = "";
        this.$("title").value = "";
        for (const el of ["editor", "title", "hist", "perms", "del", "save"]) this.$(el).disabled = true;
        this.$("pill").hidden = true;
        this.$("preview").innerHTML = '<div class="empty">Artifact deleted.</div>';
        this._banner(null);
      } else if (isCur && !this._saving) {
        if (a.version !== this._cur.version) {
          if (this._dirty) {
            this._banner(`${a.updated_by} published v${a.version} while you have unsaved edits.`, [
              ["Load their version", () => this._open(a.id)],
              ["Keep editing", () => this._banner(null)],
            ]);
          } else {
            this._open(a.id);
            this._toast(`Updated by ${a.updated_by} (v${a.version})`);
          }
        } else {
          this._cur = Object.assign({}, this._cur, { caps_granted: a.caps_granted, caps_requested: a.caps_requested });
          this._renderMeta();
        }
      }
      this._renderList();
    }
  }

  // ------------------------------------------------------------------------
  // Dashboard card:  type: custom:html-artifact-card / artifact: my-id / height: 500
  // ------------------------------------------------------------------------

  class HtmlArtifactCard extends HTMLElement {
    setConfig(config) {
      if (!config || !config.artifact) throw new Error("Set 'artifact' to the id of an artifact");
      this._config = config;
    }

    static getStubConfig() {
      return { artifact: "", height: 500 };
    }

    getCardSize() {
      const h = Number(this._config && this._config.height) || 400;
      return Math.max(2, Math.round(h / 50));
    }

    set hass(hass) {
      this._hass = hass;
      if (!this._runner) this._ensure();
      else this._runner.updateHass(hass);
    }

    connectedCallback() {
      if (this._hass && !this._runner) this._ensure();
    }

    disconnectedCallback() {
      if (this._runner) this._runner.destroy();
      this._runner = null;
    }

    async _ensure() {
      if (!this._hass || !this._config || this._runner) return;
      if (!this._root) {
        this._root = this.attachShadow({ mode: "open" });
        this._root.innerHTML =
          "<style>:host{display:block}ha-card{overflow:hidden}.err{padding:16px;color:var(--error-color,#b00020)}</style>" +
          '<ha-card><div id="host"></div></ha-card>';
      }
      const auto = String(this._config.height).toLowerCase() === "auto";
      this._runner = new ArtifactRunner({
        hass: this._hass,
        host: this._root.getElementById("host"),
        autoHeight: auto,
        height: auto ? 0 : Number(this._config.height) || 500,
        onRemoteChange: () => this._load(),
      });
      await this._runner.start();
      await this._load();
    }

    async _load() {
      const host = this._root.getElementById("host");
      try {
        const art = await this._hass.connection.sendMessagePromise({
          type: WS + "get",
          artifact_id: this._config.artifact,
        });
        this._runner.mount(art);
      } catch (err) {
        host.innerHTML = `<div class="err">Artifact "${esc(this._config.artifact)}" could not be loaded: ${esc((err && err.message) || "error")}</div>`;
      }
    }
  }

  if (!customElements.get("html-artifacts-panel")) customElements.define("html-artifacts-panel", HtmlArtifactsPanel);
  if (!customElements.get("html-artifact-card")) {
    customElements.define("html-artifact-card", HtmlArtifactCard);
    window.customCards = window.customCards || [];
    window.customCards.push({
      type: "html-artifact-card",
      name: "HTML Artifact",
      description: "Shows an HTML artifact published from Claude.",
    });
  }

  window.__htmlArtifacts = { injectIntoHead, buildCsp, bridgeMain, globToRegExp, pendingCaps, composeDocument };
})();
