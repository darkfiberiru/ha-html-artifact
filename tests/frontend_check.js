// Checks the pure helpers and the iframe bridge protocol without a browser.
// Run:  node tests/frontend_check.js
"use strict";
const vm = require("vm");
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const src = fs.readFileSync(
  path.join(__dirname, "../custom_components/html_artifacts/frontend/html-artifacts.js"),
  "utf8"
);

// ---- load the module into a stub browser environment -----------------------
const registry = {};
const win = { addEventListener() {}, removeEventListener() {}, customCards: [] };
const ctx = {
  window: win,
  customElements: { get: (n) => registry[n], define: (n, c) => { registry[n] = c; } },
  HTMLElement: class {},
  Event: class {},
  document: {},
  localStorage: {},
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
  console,
  setTimeout,
  clearTimeout,
};
vm.createContext(ctx);
vm.runInContext(src, ctx);
const T = win.__htmlArtifacts;
assert(T, "module exposes test hooks");
assert(registry["html-artifacts-panel"] && registry["html-artifact-card"], "custom elements defined");

let passed = 0;
const ok = (name, fn) => Promise.resolve().then(fn).then(() => { passed++; console.log("  ok  " + name); });

(async () => {
  await ok("injectIntoHead: existing <head>", () => {
    const out = T.injectIntoHead("<!doctype html><html><head><title>x</title></head><body></body></html>", "<S>");
    assert(out.includes("<head><S><title>"));
    assert(out.startsWith("<!doctype html>"));
  });
  await ok("injectIntoHead: html without head", () => {
    assert.strictEqual(T.injectIntoHead("<html><body>a</body></html>", "<S>"), "<html><head><S></head><body>a</body></html>");
  });
  await ok("injectIntoHead: doctype only, and bare fragment", () => {
    assert(T.injectIntoHead("<!doctype html><p>a</p>", "<S>").includes("<S><p>a</p>"));
    const frag = T.injectIntoHead("<p>a</p>", "<S>");
    assert(frag.startsWith("<!doctype html>") && frag.endsWith("<S><p>a</p>"));
  });
  await ok("CSP: closed by default, https only when network is granted", () => {
    const closed = T.buildCsp({});
    assert(closed.includes("connect-src 'none'") && closed.includes("img-src data: blob:") && !closed.includes("img-src data: blob: https:"));
    const open = T.buildCsp({ network: true });
    assert(open.includes("connect-src https:") && open.includes("img-src data: blob: https:"));
    assert(closed.includes("default-src 'none'") && closed.includes("form-action 'none'"));
  });
  await ok("glob patterns", () => {
    assert(T.globToRegExp("sensor.oven_*").test("sensor.oven_temp"));
    assert(!T.globToRegExp("sensor.oven_*").test("sensor.fridge"));
    assert(T.globToRegExp("light.?itchen").test("light.kitchen"));
    assert(!T.globToRegExp("a.b").test("axb")); // '.' is literal
  });
  await ok("pendingCaps", () => {
    assert(T.pendingCaps({ caps_requested: { notify: true }, caps_granted: {} }));
    assert(!T.pendingCaps({ caps_requested: { notify: true, entities: ["a.*"] }, caps_granted: { notify: true, entities: ["a.*"] } }));
    assert(!T.pendingCaps({ caps_requested: {}, caps_granted: {} }));
  });
  await ok("composed document compiles and carries CSP + bridge", () => {
    const doc = T.composeDocument("<!doctype html><html><head></head><body><p>hi</p></body></html>", {});
    assert(doc.includes('http-equiv="Content-Security-Policy"'));
    const m = /<script>([\s\S]*?)<\/script>/.exec(doc);
    assert(m, "bridge script present");
    new vm.Script(m[1]); // throws on a syntax error
    assert(doc.indexOf("Content-Security-Policy") < doc.indexOf("<p>hi</p>"), "CSP precedes page content");
  });

  // ---- bridge protocol -----------------------------------------------------
  let now = 0;
  const posted = [];
  const listeners = [];
  const parent = { postMessage: (m) => posted.push(m) };
  const winHandlers = {};
  const docHandlers = {};
  const bwin = {
    addEventListener: (t, fn) => {
      if (t === "message") listeners.push(fn);
      (winHandlers[t] = winHandlers[t] || []).push(fn);
    },
  };
  const root = { setAttribute() {}, style: { setProperty() {} } };
  const timeouts = [];
  const fakeConsole = { error() {}, log() {} };
  const sandbox = {
    parent, window: bwin, performance: { now: () => now }, console: fakeConsole,
    document: { documentElement: root, addEventListener: (t, fn) => (docHandlers[t] = docHandlers[t] || []).push(fn) },
    setTimeout: (fn, ms) => timeouts.push({ fn, ms }),
  };
  vm.createContext(sandbox);
  vm.runInContext("(" + T.bridgeMain.toString() + ")()", sandbox);
  const ha = bwin.ha;
  const deliver = (m) => listeners.forEach((fn) => fn({ source: parent, data: Object.assign({ ha: 1 }, m) }));
  const tick = () => new Promise((r) => setImmediate(r));
  // Objects built inside the vm sandbox have a different Object.prototype, so
  // compare them via JSON rather than deepStrictEqual.
  const plain = (x) => JSON.parse(JSON.stringify(x));

  const timerFromServer = {
    id: "t1", artifact_id: "demo", label: "Dinner", state: "running", duration: 1000,
    steps: [{ at: 0, label: "A" }, { at: 300, label: "B" }, { at: 600, label: "C" }],
    elapsed_now: 100, notify_targets: [],
  };

  await ok("hello handshake populates ha.*", async () => {
    assert.strictEqual(posted[0].method, "hello");
    deliver({
      id: posted[0].id,
      result: { user: { name: "Alex" }, artifact: { id: "demo" }, grants: { notify: true }, timers: [timerFromServer], theme: { dark: true, vars: {} } },
    });
    await ha.ready;
    assert.strictEqual(ha.user.name, "Alex");
    assert.strictEqual(ha.grants.notify, true);
  });

  await ok("timers.subscribe gets a snapshot with live fields", () => {
    let snap = null;
    ha.timers.subscribe((s) => { snap = s; });
    assert.strictEqual(snap.length, 1);
    now += 2000; // 2s of local time passes
    const v = ha.timers.view(snap[0]);
    assert(Math.abs(v.elapsed - 102) < 0.01, "elapsed advances with local time: " + v.elapsed);
    assert.strictEqual(v.currentIndex, 0);
    assert.strictEqual(v.next.label, "B");
    assert(Math.abs(v.nextIn - 198) < 0.01);
  });

  await ok("a paused timer does not advance locally", () => {
    const paused = Object.assign({}, timerFromServer, { state: "paused" });
    deliver({ event: { type: "timer", change: "pause", timer: paused } });
    let snap;
    ha.timers.subscribe((s) => { snap = s; })();
    now += 5000;
    assert.strictEqual(ha.timers.view(snap[0]).elapsed, 100);
  });

  await ok("kv round trip, errors carry the server's code", async () => {
    const p = ha.kv.get("checked");
    const msg = posted[posted.length - 1];
    assert.deepStrictEqual([msg.method, msg.params.key], ["kv.get", "checked"]);
    deliver({ id: msg.id, result: [1, 2] });
    assert.deepStrictEqual(await p, [1, 2]);

    const q = ha.callService("light", "toggle", {});
    deliver({ id: posted[posted.length - 1].id, error: { code: "forbidden", message: "nope" } });
    await assert.rejects(q, (e) => e.code === "forbidden" && e.message === "nope");
  });

  await ok("kv change events reach subscribers", () => {
    const seen = [];
    ha.kv.subscribe((e) => seen.push(e));
    deliver({ event: { type: "kv", key: "k", value: 5, deleted: false } });
    assert.deepStrictEqual(plain(seen), [{ key: "k", value: 5, deleted: false }]);
  });

  await ok("messages from anyone but the parent are ignored", () => {
    const before = Object.keys(posted).length;
    let called = false;
    ha.kv.subscribe(() => { called = true; });
    listeners.forEach((fn) => fn({ source: {}, data: { ha: 1, event: { type: "kv", key: "x", value: 1 } } }));
    assert.strictEqual(called, false);
    assert.strictEqual(Object.keys(posted).length, before);
  });

  await ok("fromDurations builds offsets", () => {
    const r = ha.timers.fromDurations([{ label: "a", seconds: 60 }, { label: "b", seconds: 120 }]);
    assert.deepStrictEqual(plain(r.steps.map((s) => s.at)), [0, 60]);
    assert.strictEqual(r.duration, 180);
  });

  await ok("fired events and entity subscriptions", async () => {
    const firedSeen = [];
    ha.timers.onFired((e) => firedSeen.push(e));
    deliver({ event: { type: "timer_fired", kind: "step", timer: Object.assign({}, timerFromServer), step: { label: "B" }, index: 1, missed: 0 } });
    assert.strictEqual(firedSeen[0].step.label, "B");

    const states = [];
    ha.entities.subscribe(["sensor.oven"], (s, id) => states.push([id, s && s.state]));
    const msg = posted[posted.length - 1];
    assert.strictEqual(msg.method, "entities.subscribe");
    deliver({ id: msg.id, result: { "sensor.oven": { state: "20" } } });
    await tick();
    deliver({ event: { type: "state", entity_id: "sensor.oven", state: { state: "180" } } });
    deliver({ event: { type: "state", entity_id: "sensor.other", state: { state: "1" } } });
    assert.deepStrictEqual(states, [["sensor.oven", "20"], ["sensor.oven", "180"]]);
  });

  // ---- diagnostics (what artifact_check reports) -----------------------------
  await ok("bridge reports errors, rejections, console.error and CSP blocks, capped at 20", () => {
    const reports = () => posted.filter((m) => m.notify === "report");
    const before = reports().length;
    winHandlers.error[0]({ message: "ReferenceError: x is not defined", lineno: 42 });
    winHandlers.error[0]({ target: { tagName: "IMG", src: "https://example.org/a.png" } });
    winHandlers.unhandledrejection[0]({ reason: new Error("kaput") });
    docHandlers.securitypolicyviolation[0]({ violatedDirective: "connect-src", blockedURI: "https://api.example", lineNumber: 9 });
    const logged = [];
    fakeConsole.error("oops", { message: "inner" });
    const mine = reports().slice(before).map(plain);
    assert.deepStrictEqual(mine.map((m) => m.level), ["error", "error", "error", "warning", "console"]);
    assert.strictEqual(mine[0].message, "ReferenceError: x is not defined");
    assert.strictEqual(mine[0].line, 42);
    assert(mine[1].message.includes("Failed to load <img> https://example.org/a.png"));
    assert(mine[2].message.includes("kaput"));
    assert(mine[3].message.includes("connect-src") && mine[3].line === 9);
    assert.strictEqual(mine[4].message, "oops inner");
    for (let i = 0; i < 40; i++) winHandlers.error[0]({ message: "spam " + i });
    assert(reports().length <= 20, "bridge stops reporting after 20: " + reports().length);
    assert.strictEqual(typeof logged, "object");
  });
  await ok("bridge announces 'settled' 1.5s after load", () => {
    winHandlers.load[0]();
    const t = timeouts.find((x) => x.ms === 1500);
    assert(t, "settle timer scheduled");
    t.fn();
    assert(posted.some((m) => m.notify === "settled"));
  });
  await ok("composeWithMeta maps document lines back to the author's lines", () => {
    const html = "<!doctype html>\n<html>\n<head>\n<title>t</title>\n</head>\n<body>\n<script>\nboom();\n</script>\n</body>\n</html>";
    const meta = T.composeWithMeta(html, {});
    const lines = meta.doc.split("\n");
    const lineOfBoom = lines.findIndex((l) => l.trim() === "boom();") + 1;
    assert(lineOfBoom > 8, "bridge pushes the script down in the composed doc");
    assert.strictEqual(T.userLine(meta, lineOfBoom), 8, "boom() is line 8 of the author's html");
    assert.strictEqual(T.userLine(meta, 2), 2, "lines before the bridge are unchanged");
    assert.strictEqual(T.userLine(meta, meta.injectLine + 3), null, "lines inside the bridge map to nothing");
    assert.strictEqual(T.userLine(meta, 0), null);
    assert.strictEqual(T.composeDocument(html, {}), meta.doc);
    // pages without a <head> still map correctly
    const bare = T.composeWithMeta("<p>a</p>\n<script>\nx()\n</script>", {});
    const ln = bare.doc.split("\n").findIndex((l) => l.trim() === "x()") + 1;
    assert.strictEqual(T.userLine(bare, ln), 3);
  });
  await ok("dry-run stubs never touch Home Assistant and keep pages from crashing", () => {
    const art = { id: "a", title: "A", version: 4, caps_granted: { notify: true } };
    assert.strictEqual(plain(T.dryReply("hello", {}, art)).artifact.version, 4);
    assert.deepStrictEqual(plain(T.dryReply("hello", {}, art)).timers, []);
    for (const m of ["service.call", "notify", "kv.set", "kv.delete"]) assert.strictEqual(T.dryReply(m, {}, art), true, m);
    for (const m of ["kv.list", "entities.get", "entities.subscribe"]) assert.deepStrictEqual(plain(T.dryReply(m, {}, art)), {}, m);
    assert.strictEqual(T.dryReply("kv.get", { key: "x" }, art), null);
    const timer = plain(T.dryReply("timer.create", { label: "Rice", duration: 60, steps: [{ at: 0, label: "go" }] }, art));
    assert.strictEqual(timer.state, "idle", "a dry-run timer never starts");
    assert.strictEqual(timer.label, "Rice");
    assert.deepStrictEqual(plain(T.dryReply("timer.list", {}, art)), []);
  });

  console.log(`\n${passed} frontend checks passed`);
})().catch((err) => {
  console.error("\nFAILED:", err);
  process.exit(1);
});
