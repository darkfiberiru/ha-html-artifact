// Drives the example pages through the REAL bridge inside jsdom, with a scripted
// fake server standing in for the Home Assistant side.
//
//   npm install jsdom            (anywhere; then set NODE_PATH to its node_modules)
//   NODE_PATH=/path/to/node_modules node tests/example_pages.js
"use strict";
const vm = require("vm");
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const { JSDOM } = require("jsdom");

const root = path.join(__dirname, "..");
const src = fs.readFileSync(path.join(root, "custom_components/html_artifacts/frontend/html-artifacts.js"), "utf8");
const win0 = { addEventListener() {}, removeEventListener() {}, customCards: [] };
const registry = {};
const ctx = {
  window: win0, console, setTimeout, clearTimeout, HTMLElement: class {}, Event: class {},
  customElements: { get: (n) => registry[n], define: (n, c) => { registry[n] = c; } },
  document: {}, localStorage: {}, getComputedStyle: () => ({ getPropertyValue: () => "" }),
};
vm.createContext(ctx);
vm.runInContext(src, ctx);
const { composeDocument } = win0.__htmlArtifacts;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function mount(file, handler) {
  const html = fs.readFileSync(path.join(root, "examples", file), "utf8");
  const sent = [];
  const errors = [];
  let win;
  const dom = new JSDOM(composeDocument(html, {}), {
    runScripts: "dangerously",
    pretendToBeVisual: true,
    url: "https://ha.test/",
    beforeParse(w) {
      win = w;
      w.postMessage = (m) => { sent.push(m); if (m.id) setTimeout(() => reply(m), 0); };
      w.addEventListener("error", (e) => errors.push(e.message));
    },
  });
  const send = (data) =>
    win.dispatchEvent(new win.MessageEvent("message", { data: Object.assign({ ha: 1 }, data), source: win.parent }));
  const reply = (m) => {
    try {
      const result = handler(m, send);
      send({ id: m.id, result: result === undefined ? null : result });
    } catch (e) {
      send({ id: m.id, error: { code: e.code || "error", message: e.message } });
    }
  };
  const $ = (id) => win.document.getElementById(id);
  const click = (el) => el.dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
  return { win, sent, errors, send, $, click, doc: win.document, close: () => win.close() };
}

const timerView = (over) =>
  Object.assign(
    {
      id: "t1", artifact_id: "cook", label: "Pesto Tilapia & Garlic Rice", state: "running", duration: 1500,
      steps: [0, 300, 720, 1020, 1440].map((at, i) => ({ at, label: ["Start the rice", "Prep the fish", "Sear", "Top and finish", "Serve"][i], detail: "d" + i, notify: true })),
      elapsed_now: 0, notify_targets: [],
    },
    over
  );

let passed = 0;
const ok = async (name, fn) => { await fn(); passed++; console.log("  ok  " + name); };

(async () => {
  // ---------------------------------------------------------------- cook timer
  await ok("cook-timer: start, pause, skip, finish, shared check-offs", async () => {
    const page = mount("cook-timer.html", (m) => {
      switch (m.method) {
        case "hello": return { user: { name: "Alex" }, artifact: { id: "cook" }, grants: { notify: true }, timers: [], theme: null };
        case "kv.get": return null;
        case "kv.set": return true;
        case "timer.create": return timerView({});
        case "timer.control": {
          const { action } = m.params;
          if (action === "pause") return timerView({ state: "paused", elapsed_now: 12 });
          if (action === "resume") return timerView({ state: "running", elapsed_now: 12 });
          if (action === "skip") return timerView({ state: "running", elapsed_now: 300 });
          if (action === "extend") return timerView({ state: "running", elapsed_now: 300, duration: 1560 });
          return timerView({ state: "idle" });
        }
        default: throw new Error("unexpected " + m.method);
      }
    });
    await sleep(50);
    assert.strictEqual(page.$("state").textContent, "Ready");
    assert.strictEqual(page.$("go").textContent, "Start");
    assert.strictEqual(page.$("next").disabled, true);

    page.click(page.$("go"));
    await sleep(60);
    const create = page.sent.find((m) => m.method === "timer.create");
    assert(create, "Start creates a server-side timer");
    assert.strictEqual(create.params.start, true);
    assert.strictEqual(create.params.steps.length, 5);
    assert.strictEqual(create.params.duration, 1500);
    assert.strictEqual(page.$("state").textContent, "Cooking");
    assert.strictEqual(page.$("stepName").textContent, "Start the rice");
    assert(/^5:00|^4:5\d/.test(page.$("count").firstChild.textContent), "countdown to Prep: " + page.$("count").firstChild.textContent);
    assert(page.$("countLabel").textContent.includes("Prep the fish"));
    assert.strictEqual(page.$("go").textContent, "Pause");
    assert.strictEqual(page.$("next").disabled, false);
    assert.strictEqual(page.doc.querySelectorAll("#steps li.cur").length, 1);

    page.click(page.$("go")); // pause
    await sleep(60);
    assert.strictEqual(page.sent.filter((m) => m.method === "timer.control").pop().params.action, "pause");
    assert.strictEqual(page.$("state").textContent, "Paused");
    assert.strictEqual(page.$("go").textContent, "Resume");

    page.click(page.$("go")); // resume
    await sleep(60);
    page.click(page.$("next")); // skip to Prep
    await sleep(60);
    assert.strictEqual(page.$("stepName").textContent, "Prep the fish");
    assert.strictEqual(page.doc.querySelectorAll("#steps li.done").length, 1);

    // another device finishes the timer: this page follows without a request
    page.send({ event: { type: "timer", change: "finish", timer: timerView({ state: "finished", elapsed_now: 1500 }) } });
    await sleep(300);
    assert.strictEqual(page.$("state").textContent, "Done");
    assert.strictEqual(page.$("stepName").textContent, "Dinner is ready");
    assert.strictEqual(page.$("go").textContent, "Cook again");

    // ingredient check-off round trip
    const boxes = page.doc.querySelectorAll("#ing input");
    assert.strictEqual(boxes.length, 9);
    boxes[2].checked = true;
    boxes[2].dispatchEvent(new page.win.Event("change", { bubbles: true }));
    await sleep(30);
    const set = page.sent.filter((m) => m.method === "kv.set").pop();
    assert.deepStrictEqual([set.params.key, JSON.parse(JSON.stringify(set.params.value))], ["checked", [2]]);
    page.send({ event: { type: "kv", key: "checked", value: [0, 2], deleted: false } });
    await sleep(30);
    const state = [...page.doc.querySelectorAll("#ing input")].map((b) => b.checked);
    assert.deepStrictEqual(state.slice(0, 4), [true, false, true, false]);
    assert.deepStrictEqual(page.errors, []);
    page.close();
  });

  await ok("cook-timer: reopening on another device shows the running timer immediately", async () => {
    const running = timerView({ state: "running", elapsed_now: 800 }); // 13m20s in: "Sear" is current
    const page = mount("cook-timer.html", (m) => {
      if (m.method === "hello") return { user: { name: "Kid" }, artifact: { id: "cook" }, grants: {}, timers: [running], theme: null };
      if (m.method === "kv.get") return null;
      throw new Error("unexpected " + m.method);
    });
    await sleep(80);
    assert.strictEqual(page.$("state").textContent, "Cooking");
    assert.strictEqual(page.$("stepName").textContent, "Sear");
    assert(page.$("countLabel").textContent.includes("Top and finish"));
    assert(/^3:[34]\d$/.test(page.$("count").firstChild.textContent), page.$("count").firstChild.textContent); // 1020-800 = 220s
    assert(page.$("note").textContent.includes("Ask the household admin"), "explains missing notify grant");
    page.close();
  });

  // --------------------------------------------------------------- house board
  await ok("house-board: entities, service call, shared notes", async () => {
    const states = {
      "sensor.kitchen_temperature": { entity_id: "sensor.kitchen_temperature", state: "21.5", attributes: { friendly_name: "Kitchen", unit_of_measurement: "C" } },
      "light.lamp": { entity_id: "light.lamp", state: "on", attributes: { friendly_name: "Lamp" } },
    };
    const page = mount("house-board.html", (m) => {
      switch (m.method) {
        case "hello": return { user: { name: "Alex" }, artifact: { id: "board" }, grants: {}, timers: [], theme: null };
        case "entities.subscribe": return states;
        case "kv.get": return null;
        case "kv.set": return true;
        case "service.call": return true;
        default: throw new Error("unexpected " + m.method);
      }
    });
    await sleep(80);
    assert.strictEqual(page.$("who").textContent, "Signed in as Alex");
    assert(page.$("temps").textContent.includes("Kitchen") && page.$("temps").textContent.includes("21.5"));
    const lamp = page.doc.querySelector("#lights button");
    assert(lamp.classList.contains("on"));

    page.click(lamp);
    await sleep(30);
    const call = page.sent.find((m) => m.method === "service.call");
    assert.deepStrictEqual(
      JSON.parse(JSON.stringify([call.params.domain, call.params.service, call.params.target])),
      ["light", "toggle", { entity_id: "light.lamp" }]
    );
    page.send({ event: { type: "state", entity_id: "light.lamp", state: { entity_id: "light.lamp", state: "off", attributes: { friendly_name: "Lamp" } } } });
    await sleep(30);
    assert(!page.doc.querySelector("#lights button").classList.contains("on"));

    const notes = page.$("notes");
    notes.value = "Bins out Thursday";
    notes.dispatchEvent(new page.win.Event("input"));
    await sleep(750);
    assert.strictEqual(page.sent.filter((m) => m.method === "kv.set").pop().params.value, "Bins out Thursday");
    page.send({ event: { type: "kv", key: "notes", value: "Bins out Thursday\nMilk", deleted: false } });
    await sleep(30);
    assert.strictEqual(notes.value, "Bins out Thursday\nMilk");
    assert.deepStrictEqual(page.errors, []);
    page.close();
  });

  console.log(`\n${passed} example-page checks passed`);
  process.exit(0);
})().catch((err) => {
  console.error("\nFAILED:", err);
  process.exit(1);
});
