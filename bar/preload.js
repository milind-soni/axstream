const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("ptt", {
  onDown: (fn) => ipcRenderer.on("ptt-down", fn),
  onUp: (fn) => ipcRenderer.on("ptt-up", fn),
});
