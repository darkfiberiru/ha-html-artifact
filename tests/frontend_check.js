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
  const bwin = { addEventListener: (t, fn) => t === "message" && listeners.push(fn) };
  const root = { setAttribute() {}, style: { setProperty() {} } };
  const sandbox = {
    parent, window: bwin, document: { documentElement: root }, performance: { now: () => now }, console,
    TextDecoder, Blob, URL: { createObjectURL: (b) => "blob:test/" + b.size },
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

  await ok("assets: get as text/json/blob, url is cached", async () => {
    const enc = (t) => { const u = new TextEncoder().encode(t); return u.buffer.slice(u.byteOffset, u.byteOffset + u.byteLength); };
    const p = ha.assets.get("data/a.json", "json");
    let msg = posted[posted.length - 1];
    assert.deepStrictEqual([msg.method, msg.params.path], ["assets.get", "data/a.json"]);
    deliver({ id: msg.id, result: { buffer: enc('{"n":3}'), type: "application/json" } });
    assert.strictEqual((await p).n, 3);

    const t = ha.assets.get("notes.txt", "text");
    deliver({ id: posted[posted.length - 1].id, result: { buffer: enc("hello"), type: "text/plain" } });
    assert.strictEqual(await t, "hello");

    const u1 = ha.assets.url("img/x.png");
    const u2 = ha.assets.url("img/x.png");
    assert.strictEqual(u1, u2, "same promise for the same path");
    msg = posted[posted.length - 1];
    deliver({ id: msg.id, result: { buffer: new ArrayBuffer(7), type: "image/png" } });
    assert.strictEqual(await u1, "blob:test/7");

    const missing = ha.assets.get("nope.png");
    deliver({ id: posted[posted.length - 1].id, error: { code: "not_found", message: "asset 'nope.png' does not exist" } });
    await assert.rejects(missing, (e) => e.code === "not_found");
  });

  console.log(`\n${passed} frontend checks passed`);
})().catch((err) => {
  console.error("\nFAILED:", err);
  process.exit(1);
});
