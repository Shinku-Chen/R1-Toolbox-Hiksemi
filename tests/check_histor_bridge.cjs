#!/usr/bin/env node
"use strict";

// Exercise the official hidden-window handshake without Electron, npm or a NAS.
// Run: node --test tests/check_histor_bridge.cjs
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const source = fs.readFileSync(path.join(root, "ui/assets/histor-bridge.js"), "utf8");
const html = fs.readFileSync(path.join(root, "ui/index.html"), "utf8");

function storage() {
  const values = new Map();
  return {
    getItem(key) { return values.has(String(key)) ? values.get(String(key)) : null; },
    setItem(key, value) { values.set(String(key), String(value)); },
    removeItem(key) { values.delete(String(key)); },
  };
}

function officialHost(mode = "manual") {
  const ipc = new EventEmitter();
  const host = { ipc, visible: false, acknowledged: false, ready: false, messages: [], order: [] };
  host.count = channel => host.messages.filter(message => message.channel === channel).length;
  host.acknowledge = () => {
    assert.ok(host.count("showWin") > 0, "the hidden window cannot acknowledge an unrequested show");
    host.visible = true;
    host.acknowledged = true;
    host.order.push("sendAppPath");
    ipc.emit("sendAppPath", {}, "/screen/squashfs-root");
  };
  ipc.send = (channel, ...args) => {
    host.messages.push({ channel, args });
    host.order.push(channel);
    if (channel === "showWin") {
      // Official BrowserWindow starts show:false; only its positioning
      // acknowledgement makes it visible. Each extra request would leak a poll.
      if (mode === "synchronous") host.acknowledge();
    } else if (channel === "deviceStatus") {
      assert.ok(host.acknowledged, "deviceStatus must wait for the host acknowledgement");
      host.ready = true;
    } else {
      assert.fail("the bridge sent an unrelated host command: " + channel);
    }
  };
  return host;
}

function page(host, session = storage(), overrides = {}) {
  const warnings = [];
  const sandbox = { console: { warn: (...args) => warnings.push(args) }, sessionStorage: session };
  if (host) {
    sandbox.require = name => {
      assert.equal(name, "electron");
      return { ipcRenderer: host.ipc };
    };
    sandbox.process = { type: "renderer", versions: { electron: "12.0.0" } };
  }
  Object.assign(sandbox, overrides);
  sandbox.window = sandbox;
  return { context: vm.createContext(sandbox), warnings };
}

function evaluate(target) {
  vm.runInContext(source, target.context, { filename: "histor-bridge.js", timeout: 1000 });
}

function staticPanelCanRun(target) {
  vm.runInContext("globalThis.staticPanelRan = true;", target.context);
  assert.equal(target.context.staticPanelRan, true);
}

test("the independent deferred bridge loads once before the dashboard", () => {
  const scripts = [...html.matchAll(/<script\b([^>]*)>[\s\S]*?<\/script\s*>/gi)]
    .map(match => ({ attributes: match[1], src: /\bsrc\s*=\s*(["'])(.*?)\1/i.exec(match[1])?.[2] }));
  const bridge = scripts.filter(script => script.src === "./assets/histor-bridge.js");
  const panel = scripts.filter(script => script.src === "./assets/panel.js");
  assert.equal(bridge.length, 1);
  assert.equal(panel.length, 1);
  assert.match(bridge[0].attributes, /(?:^|\s)defer(?:\s|=|$)/i);
  assert.match(panel[0].attributes, /(?:^|\s)defer(?:\s|=|$)/i);
  assert.ok(scripts.indexOf(bridge[0]) < scripts.indexOf(panel[0]));
});

test("a synchronous host acknowledgement cannot race the listener", () => {
  const host = officialHost("synchronous");
  const target = page(host);
  assert.equal(host.visible, false);
  evaluate(target);
  assert.equal(host.visible, true, "the official window must leave its hidden state");
  assert.equal(host.ready, true);
  assert.deepEqual(host.order, ["showWin", "sendAppPath", "deviceStatus"]);
  assert.equal(host.count("showWin"), 1);
  assert.equal(host.count("deviceStatus"), 1);
  staticPanelCanRun(target);
});

test("no acknowledgement means no premature deviceStatus or duplicate show request", () => {
  const host = officialHost();
  const target = page(host);
  evaluate(target);
  evaluate(target);
  assert.equal(host.visible, false);
  assert.equal(host.ready, false);
  assert.equal(host.count("showWin"), 1);
  assert.equal(host.count("deviceStatus"), 0);
  host.acknowledge();
  host.acknowledge();
  evaluate(target);
  assert.equal(host.visible, true);
  assert.equal(host.count("showWin"), 1);
  assert.equal(host.count("deviceStatus"), 1);
});

test("reload while requested listens to the existing poll without requesting another", () => {
  const session = storage();
  const host = officialHost();
  evaluate(page(host, session));
  assert.equal(host.count("showWin"), 1);
  assert.equal(host.count("deviceStatus"), 0);
  // A new document loses its listeners and globals, while sessionStorage and
  // the official main process's positioning poll survive the renderer reload.
  host.ipc.removeAllListeners();
  const reloaded = page(host, session);
  evaluate(reloaded);
  assert.equal(host.count("showWin"), 1, "reload must not leak another official positioning poll");
  assert.equal(host.count("deviceStatus"), 0);
  host.acknowledge();
  assert.equal(host.visible, true);
  assert.equal(host.count("deviceStatus"), 1);
  staticPanelCanRun(reloaded);
});

test("reload after acknowledgement does not repeat either handshake channel", () => {
  const session = storage();
  const host = officialHost("synchronous");
  evaluate(page(host, session));
  host.ipc.removeAllListeners();
  const reloaded = page(host, session);
  evaluate(reloaded);
  host.acknowledge();
  assert.equal(host.count("showWin"), 1);
  assert.equal(host.count("deviceStatus"), 1);
  staticPanelCanRun(reloaded);
});

test("an ordinary browser without Electron still loads the static panel", () => {
  const target = page(null);
  assert.doesNotThrow(() => evaluate(target));
  staticPanelCanRun(target);
});

test("bridge setup failures do not abort the independent dashboard script", async t => {
  const fail = () => { throw new Error("simulated unavailable Electron capability"); };
  for (const scenario of ["require", "ipc getter", "listener", "show request"]) {
    await t.test(scenario, () => {
      const host = officialHost();
      let overrides = {};
      if (scenario === "require") overrides.require = fail;
      if (scenario === "ipc getter") {
        overrides.require = () => Object.defineProperty({}, "ipcRenderer", { get: fail });
      }
      if (scenario === "listener") host.ipc.once = fail;
      if (scenario === "show request") host.ipc.send = fail;
      const target = page(host, storage(), overrides);
      assert.doesNotThrow(() => evaluate(target));
      staticPanelCanRun(target);
    });
  }
});

test("a failed ready notification does not escape its host callback or hide the UI", () => {
  const host = officialHost();
  const send = host.ipc.send;
  host.ipc.send = (channel, ...args) => {
    if (channel === "deviceStatus") throw new Error("simulated closed IPC channel");
    return send(channel, ...args);
  };
  const target = page(host);
  evaluate(target);
  assert.doesNotThrow(() => host.acknowledge());
  assert.equal(host.visible, true);
  staticPanelCanRun(target);
});
