// axstream bar: frameless always-on-top pill at the bottom of the screen.
// Hold Ctrl+Option to talk; release to act. The renderer owns the WebSocket
// to the Python bridge; this process only does window + global key state.

const { app, BrowserWindow, screen } = require("electron");
const path = require("path");
const { uIOhook, UiohookKey } = require("uiohook-napi");

const WIDTH = 640;
const HEIGHT = 76;

let win = null;

function createWindow() {
  const { workArea } = screen.getPrimaryDisplay();
  win = new BrowserWindow({
    width: WIDTH,
    height: HEIGHT,
    x: Math.round(workArea.x + (workArea.width - WIDTH) / 2),
    y: workArea.y + workArea.height - HEIGHT - 12,
    frame: false,
    transparent: true,
    resizable: false,
    alwaysOnTop: true,
    hasShadow: false,
    focusable: false, // never steal focus from the app being controlled
    webPreferences: { preload: path.join(__dirname, "preload.js") },
  });
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  win.setAlwaysOnTop(true, "screen-saver");
  win.loadFile("index.html");
}

// -- hold-to-talk: both Ctrl and Option (Alt) held ------------------------
const CTRL = new Set([UiohookKey.Ctrl, UiohookKey.CtrlRight]);
const ALT = new Set([UiohookKey.Alt, UiohookKey.AltRight]);
let ctrlDown = false;
let altDown = false;
let talking = false;

function updateTalkState() {
  const now = ctrlDown && altDown;
  if (now === talking) return;
  talking = now;
  if (win) win.webContents.send(talking ? "ptt-down" : "ptt-up");
}

uIOhook.on("keydown", (e) => {
  if (CTRL.has(e.keycode)) ctrlDown = true;
  if (ALT.has(e.keycode)) altDown = true;
  updateTalkState();
});
uIOhook.on("keyup", (e) => {
  if (CTRL.has(e.keycode)) ctrlDown = false;
  if (ALT.has(e.keycode)) altDown = false;
  updateTalkState();
});

app.whenReady().then(() => {
  createWindow();
  uIOhook.start();
});

app.on("will-quit", () => uIOhook.stop());
app.on("window-all-closed", () => app.quit());
