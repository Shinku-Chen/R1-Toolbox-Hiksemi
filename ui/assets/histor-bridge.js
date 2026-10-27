"use strict";

(() => {
  if (typeof window.require !== "function") return;

  try {
    const electron = window.require("electron");
    const ipc = electron && electron.ipcRenderer;
    if (!ipc || typeof ipc.once !== "function" || typeof ipc.send !== "function") {
      throw new Error("Electron ipcRenderer 不可用");
    }

    // Duplicate scripts must not create another official display polling loop.
    const pageKey = "__r1ToolboxHistorHandshakeStarted";
    if (window[pageKey]) return;
    window[pageKey] = true;

    // The main-process polling loop survives renderer reloads; do not restart it.
    const storageKey = "r1-toolbox.histor-window-handshake";
    let storage = null;
    let phase = null;
    try {
      storage = window.sessionStorage;
      phase = storage.getItem(storageKey);
    } catch (error) {
      console.warn("[R1 Toolbox] 显示握手状态无法保存；当前页面仍只请求一次。", error);
    }
    if (phase === "acknowledged") return;

    function savePhase(value) {
      phase = value;
      if (storage) {
        try {
          storage.setItem(storageKey, value);
        } catch (error) {
          console.warn("[R1 Toolbox] 显示握手状态无法保存；刷新页面可能重新请求显示。", error);
        }
      }
    }

    // HistorSub starts with a hidden window. Its main process acknowledges
    // showWin with sendAppPath only after positioning and showing the window.
    ipc.once("sendAppPath", () => {
      savePhase("acknowledged");
      try {
        ipc.send("deviceStatus", "R1 Toolbox ready");
      } catch (error) {
        console.warn("[R1 Toolbox] 已收到窗口显示确认，但状态确认发送失败；静态页面仍继续加载。", error);
      }
    });
    if (phase !== "requested") {
      savePhase("requested");
      ipc.send("showWin");
    }
  } catch (error) {
    console.warn("[R1 Toolbox] 窗口显示握手失败；静态页面仍继续加载。", error);
  }
})();
