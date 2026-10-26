"""
Registratore Azioni - sostituto di "Registratore azioni utente" (PSR) di Windows.

A ogni clic, trascinamento o azione da tastiera salva uno screenshot (con un
riquadro rosso sul punto cliccato) in una cartella di progetto e, alla fine,
genera una pagina HTML con tutti i passi e le descrizioni modificabili.
Lo scorrimento della rotellina viene ignorato.
"""
import ctypes
import datetime
import html
import json
import math
import os
import io
import base64
import logging
import logging.handlers
import platform
import queue
import re
import sys
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from ctypes import wintypes
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog

import mss
from PIL import Image, ImageDraw
from pynput import keyboard, mouse
from pynput.keyboard import Key
from pynput.mouse import Button

APP = "Registratore Azioni"
VERSION = "1.1"
EMAIL = "domenico.castaldod29@gmail.com"

# Coordinate del mouse e pixel dello schermo devono coincidere (schermi HiDPI)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RegistratoreAzioni.App")
except Exception:
    pass

TEMA_HTML = "scuro"  # tema di partenza delle pagine HTML (lo sceglie l'utente nell'app)


def risorsa(nome: str) -> Path:
    """File incluso nell'exe (o accanto allo script, quando si lancia da Python)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / nome


def logo_base64() -> str:
    """icon.png ridotto, per metterlo nell'HTML. Stringa vuota se non c'e' l'icona."""
    try:
        im = Image.open(risorsa("icon.png")).convert("RGBA")
        im.thumbnail((96, 96), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return ""


# --------------------------------------------------------------------------
# Configurazione
# --------------------------------------------------------------------------
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "RegistratoreAzioni"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE = CONFIG_DIR / "log.txt"

_log = logging.getLogger("registratore")
try:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _h = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=200_000, backupCount=1, encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _log.addHandler(_h)
    _log.setLevel(logging.INFO)
except Exception:
    pass

MODALITA_DEFAULT = {
    "Minima": {"formato": "WEBP", "qualita": 40, "scala": 0.5},
    "Leggera": {"formato": "JPEG", "qualita": 50, "scala": 0.6},
    "Normale": {"formato": "JPEG", "qualita": 65, "scala": 0.75},
    "Nitida": {"formato": "JPEG", "qualita": 85, "scala": 1.0},
}

CONFIG_DEFAULT = {
    "cartella_radice": "",
    "modalita_corrente": "Normale",
    "modalita": MODALITA_DEFAULT,
    "tasto_pausa": "f10",
    "tasto_fine": "f9",
    "registra_testo_digitato": True,
    "pausa_digitazione_sec": 1.2,
    "tema": "scuro",
    "nascondi_barra": True,
}


def unique_path(p: Path) -> Path:
    """Restituisce un percorso che non esiste ancora (aggiunge _2, _3, ...)."""
    p = Path(p)
    if not p.exists():
        return p
    i = 2
    while True:
        cand = p.with_name(f"{p.stem}_{i}{p.suffix}")
        if not cand.exists():
            return cand
        i += 1


def scrivi_json_atomico(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def carica_config() -> dict:
    cfg = json.loads(json.dumps(CONFIG_DEFAULT))
    if CONFIG_FILE.exists():
        try:
            dati = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            for k, v in dati.items():
                if k == "modalita" and isinstance(v, dict):
                    cfg["modalita"].update(v)
                else:
                    cfg[k] = v
        except Exception:
            # Config rovinata: la metto da parte, non la cancello
            try:
                CONFIG_FILE.rename(unique_path(CONFIG_FILE.with_name("config_rovinata.json")))
            except OSError:
                pass
    return cfg


def salva_config(cfg: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        scrivi_json_atomico(CONFIG_FILE, cfg)
    except OSError:
        pass


def pulisci_nome(nome: str) -> str:
    nome = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", nome).strip().rstrip(".")
    return nome


# --------------------------------------------------------------------------
# Helper Windows
# --------------------------------------------------------------------------
def finestra_attiva() -> str:
    try:
        u = ctypes.windll.user32
        u.GetForegroundWindow.restype = ctypes.c_void_p
        u.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
        u.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        h = u.GetForegroundWindow()
        n = u.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, buf, n + 1)
        return buf.value.strip()
    except Exception:
        return ""


def nascondi_da_cattura(hwnd: int, nascondi: bool = True) -> bool:
    """Esclude la finestra dagli screenshot (Windows 10 versione 2004 o successive)."""
    try:
        u = ctypes.windll.user32
        u.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        u.SetWindowDisplayAffinity.restype = wintypes.BOOL
        return bool(u.SetWindowDisplayAffinity(wintypes.HWND(hwnd), 0x11 if nascondi else 0))
    except Exception:
        return False


def rettangolo_finestra(hwnd: int):
    try:
        r = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return (0, 0, 0, 0)


# --------------------------------------------------------------------------
# Nomi tasti
# --------------------------------------------------------------------------
NOMI_TASTI = {
    Key.enter: "Invio", Key.tab: "Tab", Key.esc: "Esc", Key.backspace: "Backspace",
    Key.delete: "Canc", Key.space: "Spazio", Key.up: "Freccia su", Key.down: "Freccia giù",
    Key.left: "Freccia sinistra", Key.right: "Freccia destra", Key.home: "Home",
    Key.end: "Fine", Key.page_up: "Pag su", Key.page_down: "Pag giù",
    Key.insert: "Ins", Key.print_screen: "Stamp",
}
TASTI_IGNORATI = {Key.caps_lock, Key.num_lock, Key.scroll_lock, Key.menu}


def gruppo_modificatore(key):
    if key in (Key.ctrl, Key.ctrl_l, Key.ctrl_r):
        return "Ctrl"
    if key in (Key.alt, Key.alt_l, Key.alt_r):
        return "Alt"
    if key == Key.alt_gr:
        return "AltGr"
    if key in (Key.shift, Key.shift_l, Key.shift_r):
        return "Maiusc"
    if key in (Key.cmd, Key.cmd_l, Key.cmd_r):
        return "Win"
    return None


def nome_tasto(key) -> str:
    if isinstance(key, keyboard.KeyCode):
        vk = getattr(key, "vk", None)
        if vk and (48 <= vk <= 57 or 65 <= vk <= 90):
            return chr(vk)
        ch = key.char
        if ch and ch.isprintable():
            return ch.upper()
        return f"tasto {vk}" if vk else "?"
    if key in NOMI_TASTI:
        return NOMI_TASTI[key]
    nome = getattr(key, "name", str(key))
    return nome.replace("_", " ").upper() if re.fullmatch(r"f\d+", nome) else nome.replace("_", " ").title()


# --------------------------------------------------------------------------
# Registratore
# --------------------------------------------------------------------------
class Registratore:
    def __init__(self, cartella: Path, nome_modo: str, modo: dict, cfg: dict):
        self.dir = Path(cartella)
        self.nome_modo = nome_modo
        self.modo = modo
        self.cfg = cfg
        self.steps = []
        self.errori = []
        self.q = queue.Queue()
        self.ui_events = queue.Queue()
        self.running = False
        self.paused = False
        self.lock = threading.Lock()
        self.buf = ""
        self.timer = None
        self.mods = set()
        self.press = None
        self.last_click = None
        self.last_special = (None, 0.0)
        self.own_rect = (0, 0, 0, 0)
        self.mouse_ctl = mouse.Controller()
        self.contatore = 0
        self.creato = datetime.datetime.now().isoformat(timespec="seconds")
        self.k_pausa = getattr(Key, str(cfg.get("tasto_pausa", "f10")).lower(), Key.f10)
        self.k_fine = getattr(Key, str(cfg.get("tasto_fine", "f9")).lower(), Key.f9)
        self.pausa_dig = float(cfg.get("pausa_digitazione_sec", 1.2))
        self.listeners = []
        self.worker = None

    # ---- ciclo di vita ----
    def avvia(self):
        (self.dir / "immagini").mkdir(parents=True, exist_ok=True)
        self.running = True
        self.worker = threading.Thread(target=self._worker, daemon=True)
        self.worker.start()
        self.listeners = [
            mouse.Listener(on_click=self._on_click),
            keyboard.Listener(on_press=self._on_press, on_release=self._on_release),
        ]
        for l in self.listeners:
            l.daemon = True
            l.start()
        self._salva_json()

    def ferma(self):
        self.running = False
        for l in self.listeners:
            try:
                l.stop()
            except Exception:
                pass
        self._flush_typing()
        self.q.join()
        self.q.put(None)
        if self.worker:
            self.worker.join(timeout=5)
        self._salva_json()

    def alterna_pausa(self):
        self._flush_typing()
        self.paused = not self.paused
        self.press = None
        return self.paused

    # ---- mouse ----
    def _dentro_finestra(self, x, y):
        l, t, r, b = self.own_rect
        return l <= x < r and t <= y < b

    def _on_click(self, x, y, button, pressed):
        if not self.running or self.paused:
            return
        if self._dentro_finestra(x, y):
            self.press = None
            return
        now = time.time()
        if pressed:
            self.press = (x, y, now, button)
            self._flush_typing()
            nome = {Button.left: "sinistro", Button.right: "destro",
                    Button.middle: "centrale"}.get(button, "extra")
            lc = self.last_click
            if (lc and now - lc[0] < 0.45 and button == lc[3]
                    and abs(x - lc[1]) < 8 and abs(y - lc[2]) < 8):
                self.q.put({"kind": "doppio"})
                self.last_click = None
            else:
                self.q.put({"kind": "click", "x": x, "y": y, "button": nome})
                self.last_click = (now, x, y, button)
        else:
            p, self.press = self.press, None
            if p and math.hypot(x - p[0], y - p[1]) > 25:
                self.q.put({"kind": "drag", "x": p[0], "y": p[1], "x2": x, "y2": y})

    # ---- tastiera ----
    def _on_release(self, key):
        g = gruppo_modificatore(key)
        if g:
            self.mods.discard(g)

    def _on_press(self, key):
        if key == self.k_fine:
            self.ui_events.put("fine")
            return
        if key == self.k_pausa:
            self.ui_events.put("pausa")
            return
        g = gruppo_modificatore(key)
        if g:
            self.mods.add(g)
            return
        if not self.running or self.paused or key in TASTI_IGNORATI:
            return

        mods = self.mods
        altgr_like = "AltGr" in mods or ("Ctrl" in mods and "Alt" in mods)
        char = getattr(key, "char", None)

        # Scorciatoie (Ctrl+S, Alt+Tab, Win+D ...)
        if ("Ctrl" in mods or "Alt" in mods or "Win" in mods) and not (altgr_like and char and char.isprintable()):
            self._flush_typing()
            parti = [m for m in ("Ctrl", "Alt", "Maiusc", "Win") if m in mods]
            testo = "+".join(parti + [nome_tasto(key)])
            self._special(f"Scorciatoia {testo}", ("combo", testo))
            return

        # Testo normale
        if char is not None and char.isprintable():
            self._aggiungi_testo(char if self.cfg.get("registra_testo_digitato", True) else "•")
            return
        if key == Key.space:
            self._aggiungi_testo(" ")
            return
        if key == Key.backspace:
            with self.lock:
                if self.buf:
                    self.buf = self.buf[:-1]
                    self._arma_timer()
                    return
        if key in (Key.enter, Key.tab):
            self._flush_typing(finale=NOMI_TASTI[key])
            return
        self._flush_typing()
        self._special(f"Premuto {nome_tasto(key)}", ("tasto", nome_tasto(key)))

    def _aggiungi_testo(self, ch):
        with self.lock:
            self.buf += ch
            self._arma_timer()

    def _arma_timer(self):
        # va chiamata con self.lock preso (o da chi non ne ha bisogno)
        if self.timer:
            self.timer.cancel()
        self.timer = threading.Timer(self.pausa_dig, self._flush_typing)
        self.timer.daemon = True
        self.timer.start()

    def _flush_typing(self, finale=None):
        with self.lock:
            if self.timer:
                self.timer.cancel()
                self.timer = None
            testo, self.buf = self.buf, ""
        if not testo and not finale:
            return
        if testo and finale:
            desc = f"Digitato «{testo}» e premuto {finale}"
        elif testo:
            desc = f"Digitato «{testo}»"
        else:
            self._special(f"Premuto {finale}", ("fin", finale))
            return
        self.q.put({"kind": "tasto", "testo": desc})

    def _special(self, desc, ident):
        now = time.time()
        prec, t = self.last_special
        self.last_special = (ident, now)
        if ident == prec and now - t < 0.6:
            return  # tasto tenuto premuto: niente raffica di foto
        self.q.put({"kind": "tasto", "testo": desc})

    # ---- thread che cattura e salva ----
    def _worker(self):
        with mss.mss() as sct:
            while True:
                ev = self.q.get()
                try:
                    if ev is None:
                        return
                    self._elabora(sct, ev)
                except Exception as e:  # un errore non deve fermare la registrazione
                    self.errori.append(repr(e))
                    _log.exception("Errore nel salvataggio di un passo")
                finally:
                    self.q.task_done()

    def _elabora(self, sct, ev):
        kind = ev["kind"]
        if kind == "doppio":
            if self.steps and self.steps[-1]["tipo"] == "click":
                s = self.steps[-1]
                s["testo"] = s["testo"].replace("Clic", "Doppio clic", 1)
                self._salva_json()
            return

        finestra = finestra_attiva()
        if kind in ("click", "drag"):
            px, py = ev["x"], ev["y"]
        else:
            px, py = self.mouse_ctl.position
        mons = sct.monitors
        mon = mons[1] if len(mons) > 1 else mons[0]
        for m in mons[1:]:
            if m["left"] <= px < m["left"] + m["width"] and m["top"] <= py < m["top"] + m["height"]:
                mon = m
                break

        shot = sct.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        scala = float(self.modo.get("scala", 1.0))
        if scala < 0.999:
            img = img.resize((max(1, int(img.width * scala)), max(1, int(img.height * scala))),
                             Image.Resampling.LANCZOS)

        rosso = (235, 30, 30)
        spessore = max(3, round(4 * max(scala, 0.5)))
        raggio = max(16, int(38 * scala))
        d = ImageDraw.Draw(img)

        def a_img(x, y):
            return ((x - mon["left"]) * scala, (y - mon["top"]) * scala)

        if kind == "click":
            cx, cy = a_img(px, py)
            d.rectangle([cx - raggio, cy - raggio, cx + raggio, cy + raggio], outline=rosso, width=spessore)
            testo = f"Clic {ev['button']}"
        elif kind == "drag":
            sx, sy = a_img(ev["x"], ev["y"])
            ex, ey = a_img(ev["x2"], ev["y2"])
            d.line([sx, sy, ex, ey], fill=rosso, width=spessore)
            d.rectangle([sx - raggio // 2, sy - raggio // 2, sx + raggio // 2, sy + raggio // 2],
                        outline=rosso, width=spessore)
            d.rectangle([ex - raggio, ey - raggio, ex + raggio, ey + raggio], outline=rosso, width=spessore)
            testo = "Trascinamento (dal quadratino piccolo al riquadro grande)"
        else:
            testo = ev["testo"]

        fmt = str(self.modo.get("formato", "JPEG")).upper()
        q = int(self.modo.get("qualita", 65))
        ext = {"JPEG": ".jpg", "WEBP": ".webp", "PNG": ".png"}.get(fmt, ".jpg")
        if fmt not in ("JPEG", "WEBP", "PNG"):
            fmt = "JPEG"

        while True:
            self.contatore += 1
            path = self.dir / "immagini" / f"{self.contatore:04d}{ext}"
            if not path.exists():
                break
        opts = {"JPEG": {"quality": q, "optimize": True},
                "WEBP": {"quality": q, "method": 4},
                "PNG": {"optimize": True}}[fmt]
        with open(path, "xb") as f:  # "x" = mai sovrascrivere
            img.save(f, format=fmt, **opts)

        self.steps.append({
            "n": len(self.steps) + 1,
            "tipo": kind if kind != "tasto" else "tasto",
            "testo": testo,
            "finestra": finestra,
            "ora": datetime.datetime.now().strftime("%H:%M:%S"),
            "img": f"immagini/{path.name}",
        })
        self._salva_json()
        if len(self.steps) % 5 == 0:
            self.scrivi_html()

    # ---- salvataggi ----
    def _salva_json(self):
        try:
            scrivi_json_atomico(self.dir / "passi.json", {
                "progetto": self.dir.name,
                "titolo": self.dir.name,
                "creato": self.creato,
                "modalita": self.nome_modo,
                "passi": self.steps,
            })
        except OSError as e:
            self.errori.append(repr(e))
            _log.exception("Errore nel salvataggio di passi.json")

    def scrivi_html(self):
        try:
            genera_html(self.dir, self.dir.name, self.steps, self.nome_modo, self.creato)
        except OSError as e:
            self.errori.append(repr(e))
            _log.exception("Errore nella creazione di index.html")


# --------------------------------------------------------------------------
# Pagina HTML (una per progetto: contiene i dati, si modifica e si salva da sola)
# --------------------------------------------------------------------------
HTML_TEMPLATE = r"""<!doctype html>
<html lang="it" data-tema="__TEMA__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITOLO__</title>
__FAVICON__
<style>
:root,:root[data-tema=chiaro]{--bg:#f7f6f2;--tx:#1b1b1a;--mut:#77756e;--ln:#dcdad2;--ac:#c2410c}
:root[data-tema=scuro]{--bg:#151514;--tx:#e9e7e1;--mut:#8f8d85;--ln:#2e2d2a;--ac:#f08a52}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.55 "Segoe UI",system-ui,sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:34px 22px 90px}
header{display:flex;gap:14px;align-items:center;padding-bottom:16px;border-bottom:2px solid var(--tx)}
.logo{width:44px;height:44px;object-fit:contain;flex:none}
h1{margin:0;font-size:1.45rem;line-height:1.2;letter-spacing:-.01em;outline:none}
#sub,.m,.n,#stato{font-family:Consolas,"Cascadia Mono",monospace}
#sub{color:var(--mut);font-size:.78rem;margin-top:3px}
.tools{margin-left:auto;display:flex;gap:6px;align-items:center;flex-wrap:wrap;justify-content:flex-end}
.tools button{background:none;border:1px solid var(--ln);color:var(--tx);padding:5px 10px;font:inherit;font-size:.8rem;cursor:pointer}
.tools button:hover{border-color:var(--tx)}
#stato{font-size:.72rem;color:var(--mut);margin-right:6px}
.passo{display:grid;grid-template-columns:52px 1fr;padding:22px 0;border-bottom:1px solid var(--ln);break-inside:avoid}
.n{font-weight:600;font-size:.95rem;color:var(--ac);padding-top:1px}
.d{margin:0;font-weight:600;outline:none}
.m{margin:2px 0 12px;color:var(--mut);font-size:.74rem}
figure{margin:0;position:relative}
img.s{width:100%;height:auto;display:block;border:1px solid var(--ln);cursor:zoom-in}
.op{position:absolute;top:8px;right:8px;width:28px;height:28px;display:grid;place-items:center;background:var(--bg);color:var(--tx);border:1px solid var(--ln);text-decoration:none;opacity:0;transition:opacity .12s}
figure:hover .op{opacity:1}
@media (hover:none){.op{opacity:1}}
.edit .d,.edit h1{border-bottom:1px dashed var(--mut)}
.edit .d:focus,.edit h1:focus{background:color-mix(in srgb,var(--ac) 14%,transparent)}
#lb{position:fixed;inset:0;background:rgba(0,0,0,.93);display:flex;align-items:center;justify-content:center;overflow:auto;z-index:10}
#lb[hidden]{display:none}
#lb img{max-width:94vw;max-height:86vh;cursor:zoom-in}
#lb img.z{max-width:none;max-height:none;cursor:zoom-out}
#lb button,#lb a{position:fixed;background:rgba(255,255,255,.1);color:#fff;border:0;width:42px;height:42px;font-size:22px;display:grid;place-items:center;cursor:pointer;text-decoration:none}
#lb button:hover,#lb a:hover{background:rgba(255,255,255,.22)}
#lbx{top:14px;right:14px}#lbo{top:14px;right:64px}
#lbp{left:14px;top:50%}#lbn{right:14px;top:50%}
#lbc{position:fixed;bottom:0;left:0;right:0;padding:12px 70px;color:#ddd;font-size:.85rem;text-align:center;background:linear-gradient(transparent,rgba(0,0,0,.7))}
@media print{:root[data-tema]{--bg:#fff;--tx:#000;--mut:#555;--ln:#bbb;--ac:#000}.tools,.op,#lb{display:none!important}}
</style>
</head>
<body>
<div class="wrap">
<header>
__LOGO__
<div><h1 id="titolo"></h1><div id="sub"></div></div>
<div class="tools">
<span id="stato"></span>
<button id="bmod" title="Modifica titolo e descrizioni: si salvano nel file">Modifica</button>
<button id="btema">Tema</button>
<button onclick="print()">PDF</button>
</div>
</header>
<div id="lista"></div>
</div>

<div id="lb" hidden>
<button id="lbx" title="Chiudi (Esc)">✕</button>
<a id="lbo" target="_blank" title="Apri l'immagine da sola">↗</a>
<button id="lbp" title="Precedente (←)">‹</button>
<button id="lbn" title="Successivo (→)">›</button>
<img id="lbi" alt="">
<div id="lbc"></div>
</div>

__DATI__
<script>
const MA='<!'+'--DATI-->', MB='<!'+'--/DATI-->';
const D=JSON.parse(document.getElementById('dati').textContent);
const $=s=>document.querySelector(s);
const KEY='h:'+location.href;
let dirH=null, tSave=null, cur=-1;
const pad=n=>String(n).padStart(2,'0');
const stato=t=>{$('#stato').textContent=t;};

/* ---- tema ---- */
try{const t=localStorage.getItem('tema');if(t)document.documentElement.dataset.tema=t;}catch(e){}
$('#btema').onclick=()=>{
  const t=document.documentElement.dataset.tema==='scuro'?'chiaro':'scuro';
  document.documentElement.dataset.tema=t;
  try{localStorage.setItem('tema',t);}catch(e){}
};

/* ---- pagina ---- */
function render(){
  $('#titolo').textContent=D.titolo;
  document.title=D.titolo;
  let dt=D.creato;
  try{dt=new Date(D.creato).toLocaleString('it-IT',{dateStyle:'short',timeStyle:'short'});}catch(e){}
  $('#sub').textContent=D.passi.length+' passi · '+dt+' · '+D.modalita;
  const L=$('#lista');L.textContent='';
  D.passi.forEach((p,i)=>{
    const a=document.createElement('article');a.className='passo';
    const n=document.createElement('div');n.className='n';n.textContent=pad(p.n);
    const c=document.createElement('div');
    const d=document.createElement('p');d.className='d';d.textContent=p.testo;d.dataset.i=i;
    const m=document.createElement('p');m.className='m';m.textContent=p.ora+(p.finestra?' · '+p.finestra:'');
    const f=document.createElement('figure');
    const im=document.createElement('img');im.className='s';im.loading='lazy';im.src=p.img;im.alt='Passo '+p.n;
    im.addEventListener('click',()=>apriLb(i));
    const o=document.createElement('a');o.className='op';o.href=p.img;o.target='_blank';o.title="Apri l'immagine da sola";o.textContent='↗';
    f.append(im,o);c.append(d,m,f);a.append(n,c);L.append(a);
  });
  if(!D.passi.length)L.textContent='Nessun passo registrato.';
  editabili();
}
function editabili(){
  const on=!!dirH;
  document.body.classList.toggle('edit',on);
  document.querySelectorAll('.d,#titolo').forEach(el=>{el.contentEditable=on?'true':'false';});
  $('#bmod').textContent=on?'Modifica attiva':'Modifica';
}

/* ---- lettore immagini ---- */
function apriLb(i){
  cur=i;const p=D.passi[i];
  $('#lbi').className='';$('#lbi').src=p.img;$('#lbo').href=p.img;
  $('#lbc').textContent=pad(p.n)+' / '+pad(D.passi.length)+'  ·  '+p.testo;
  $('#lb').hidden=false;document.body.style.overflow='hidden';
}
function chiudiLb(){$('#lb').hidden=true;$('#lbi').src='';document.body.style.overflow='';cur=-1;}
function vai(d){const n=cur+d;if(n>=0&&n<D.passi.length)apriLb(n);}
$('#lbx').onclick=chiudiLb;
$('#lbp').onclick=()=>vai(-1);
$('#lbn').onclick=()=>vai(1);
$('#lbi').onclick=e=>{e.stopPropagation();e.target.classList.toggle('z');};
$('#lb').onclick=e=>{if(e.target.id==='lb')chiudiLb();};
document.addEventListener('keydown',e=>{
  if(cur>=0){
    if(e.key==='Escape')chiudiLb();
    else if(e.key==='ArrowRight')vai(1);
    else if(e.key==='ArrowLeft')vai(-1);
    return;
  }
  if(e.key==='Enter'&&e.target.isContentEditable){e.preventDefault();e.target.blur();}
});

/* ---- modifica con salvataggio diretto nei file della cartella ---- */
function idb(){return new Promise((res,rej)=>{const r=indexedDB.open('registratore',1);r.onupgradeneeded=()=>r.result.createObjectStore('h');r.onsuccess=()=>res(r.result);r.onerror=()=>rej(r.error);});}
async function idbGet(k){const db=await idb();return new Promise(res=>{const q=db.transaction('h').objectStore('h').get(k);q.onsuccess=()=>res(q.result);q.onerror=()=>res(null);});}
async function idbSet(k,v){const db=await idb();return new Promise(res=>{const t=db.transaction('h','readwrite');t.objectStore('h').put(v,k);t.oncomplete=()=>res();});}
async function scrivi(fh,testo){const w=await fh.createWritable();await w.write(testo);await w.close();}

$('#bmod').onclick=async()=>{
  if(dirH)return;
  try{
    let h=null;
    try{h=await idbGet(KEY);}catch(e){}
    if(h){try{if((await h.requestPermission({mode:'readwrite'}))!=='granted')h=null;}catch(e){h=null;}}
    if(!h){
      h=await showDirectoryPicker({mode:'readwrite'});
      await h.getFileHandle('passi.json');
      await h.getFileHandle('index.html');
      try{await idbSet(KEY,h);}catch(e){}
    }
    dirH=h;editabili();stato('Salvataggio automatico attivo');
  }catch(e){
    if(e.name!=='AbortError')alert('Scegli la cartella di questo progetto (quella che contiene passi.json e index.html).');
  }
};

document.addEventListener('input',e=>{
  const el=e.target;if(!dirH)return;
  if(el.id==='titolo')D.titolo=el.textContent.trim()||D.titolo;
  else if(el.classList&&el.classList.contains('d'))D.passi[+el.dataset.i].testo=el.textContent;
  else return;
  stato('Modifiche non salvate…');
  clearTimeout(tSave);tSave=setTimeout(salva,700);
});
window.addEventListener('beforeunload',e=>{if(tSave){e.preventDefault();e.returnValue='';}});

async function salva(){
  tSave=null;
  try{
    stato('Salvo…');
    const fh=await dirH.getFileHandle('passi.json');
    const orig=JSON.parse(await (await fh.getFile()).text());
    let haBackup=true;
    try{await dirH.getFileHandle('passi.originale.json');}catch(e){haBackup=false;}
    if(!haBackup){
      const b=await dirH.getFileHandle('passi.originale.json',{create:true});
      await scrivi(b,JSON.stringify(orig,null,2));
    }
    orig.titolo=D.titolo;
    (orig.passi||[]).forEach((p,i)=>{
      const n=D.passi[i];
      if(n&&n.testo!==p.testo){if(p.testo_originale===undefined)p.testo_originale=p.testo;p.testo=n.testo;}
    });
    await scrivi(fh,JSON.stringify(orig,null,2));
    const ih=await dirH.getFileHandle('index.html');
    let t=await (await ih.getFile()).text();
    const a=t.indexOf(MA),b=t.indexOf(MB);
    if(a>=0&&b>a){
      const blocco=MA+'<scr'+'ipt id="dati" type="application/json">'+JSON.stringify(D).replace(/</g,'\\u003c')+'<\/scr'+'ipt>'+MB;
      await scrivi(ih,t.slice(0,a)+blocco+t.slice(b+MB.length));
    }
    document.title=D.titolo;
    stato('Salvato '+new Date().toLocaleTimeString('it-IT'));
  }catch(e){stato('Errore: '+e.message);}
}

render();
(async()=>{
  if(!window.showDirectoryPicker){$('#bmod').hidden=true;return;}
  try{
    const h=await idbGet(KEY);
    if(h&&(await h.queryPermission({mode:'readwrite'}))==='granted'){dirH=h;editabili();stato('Salvataggio automatico attivo');}
  }catch(e){}
})();
</script>
</body>
</html>
"""


def genera_html(cartella: Path, titolo: str, steps: list, nome_modo: str, creato: str) -> Path:
    dati = {"titolo": titolo, "creato": creato, "modalita": nome_modo, "passi": steps}
    json_dati = json.dumps(dati, ensure_ascii=False).replace("<", "\\u003c")
    blocco = f'<!--DATI--><script id="dati" type="application/json">{json_dati}</script><!--/DATI-->'
    logo = logo_base64()
    favicon = f'<link rel="icon" href="data:image/png;base64,{logo}">' if logo else ""
    img_logo = f'<img class="logo" alt="" src="data:image/png;base64,{logo}">' if logo else ""
    tema = TEMA_HTML if TEMA_HTML in ("scuro", "chiaro") else "scuro"
    pagina = (HTML_TEMPLATE
              .replace("__TEMA__", tema)
              .replace("__TITOLO__", html.escape(titolo))
              .replace("__FAVICON__", favicon)
              .replace("__LOGO__", img_logo)
              .replace("__DATI__", blocco))
    out = cartella / "index.html"
    tmp = cartella / "index.html.tmp"
    tmp.write_text(pagina, encoding="utf-8")
    os.replace(tmp, out)
    return out



# --------------------------------------------------------------------------
# Guida e report per le segnalazioni
# --------------------------------------------------------------------------
GUIDA = f"""COME SI USA
1. Scrivi il nome del progetto e premi «Avvia registrazione». La prima volta scegli la cartella dove salvare tutti i progetti: da lì in poi la ricorda.
2. Lavora normalmente. A ogni clic, trascinamento o azione da tastiera viene salvato uno screenshot, con un riquadro rosso sul punto cliccato. La rotellina del mouse non conta.
3. Quando scrivi, il testo viene raggruppato: un passo per riga (per esempio «show ip route» + Invio), non uno per lettera.
4. F10 = pausa / riprendi.  F9 = fine e salva. Puoi usare anche i pulsanti della barra in alto a destra.
5. Alla fine puoi rinominare il progetto, poi si apre la pagina HTML.

QUALITÀ DEGLI SCREENSHOT
Minima, Leggera, Normale, Nitida: più sono leggere, meno spazio occupano. «Normale» va bene quasi sempre. Le modalità si possono cambiare in %APPDATA%\\RegistratoreAzioni\\config.json.

LA PAGINA HTML
• Clic su un'immagine: si apre nel lettore (frecce ← → per scorrere, Esc per chiudere, clic per ingrandire). La freccetta ↗ in alto a destra la apre da sola in una nuova scheda.
• «Modifica»: scegli una volta la cartella del progetto. Poi titolo e descrizioni si salvano da sole nel file (serve Chrome o Edge).
• «PDF» stampa la pagina per salvarla in PDF. «Tema» passa da scuro a chiaro.

COSA VIENE CREATO
Nella cartella del progetto: index.html (la pagina), passi.json (i dati), immagini/ (gli screenshot) e, dopo la prima modifica, passi.originale.json (copia di sicurezza delle descrizioni originali). Non viene mai sovrascritto nulla: se un nome esiste già, il programma aggiunge _2, _3…

COSE DA SAPERE
• Tutto quello che scrivi e vedi finisce nel progetto, password comprese: metti in pausa quando entri in account personali.
• Un programma aperto come amministratore potrebbe non essere registrato.
• Viene catturato solo il monitor dove si trova il mouse.
• La barra del registratore viene nascosta dagli screenshot (su Windows 10 recente o Windows 11). Si può disattivare con "nascondi_barra": false nel config.json.

OPEN SOURCE E SEGNALAZIONI
Registratore Azioni {VERSION} è open source e mi fa piacere che giri: più persone lo provano, più errori si trovano. Se qualcosa non funziona o hai un'idea per migliorarlo, scrivimi a {EMAIL}.
Con «Segnala un problema» il programma crea un file di report con i dati tecnici utili per sistemare l'errore (versione, Windows, schermi, impostazioni ed errori registrati). Non contiene screenshot né il testo digitato. Leggilo prima di inviarlo e allegalo alla mail, insieme a due righe su cosa stavi facendo.
"""


def _anonimizza(testo: str) -> str:
    for valore, sostituto in ((str(Path.home()), "~"), (os.environ.get("USERNAME", ""), "utente")):
        if valore:
            testo = testo.replace(valore, sostituto)
    return testo


def crea_report(cfg: dict, barra_nascosta=None) -> Path:
    """Crea un file di testo con dati tecnici utili per risolvere un errore.
    Niente screenshot, niente testo digitato, niente percorsi personali."""
    righe = [f"REPORT - {APP} {VERSION}",
             f"Creato: {datetime.datetime.now().isoformat(timespec='seconds')}",
             f"Sistema: {platform.platform()} ({platform.version()})",
             f"Python: {sys.version.split()[0]}  |  exe PyInstaller: {'sì' if getattr(sys, 'frozen', False) else 'no'}",
             f"Tk: {tk.TkVersion}"]
    try:
        with mss.mss() as sct:
            for i, m in enumerate(sct.monitors):
                nome = "tutti gli schermi" if i == 0 else f"schermo {i}"
                righe.append(f"Monitor ({nome}): {m['width']}x{m['height']} a ({m['left']}, {m['top']})")
    except Exception as e:
        righe.append(f"Monitor: errore nel leggerli ({e!r})")
    stato = {None: "non verificato", True: "sì", False: "no (non supportato o fallito)"}[barra_nascosta]
    righe.append(f"Barra nascosta dagli screenshot: {stato}")
    sicuro = {k: v for k, v in cfg.items() if k != "cartella_radice"}
    righe += ["", "IMPOSTAZIONI (senza percorsi)", json.dumps(sicuro, ensure_ascii=False, indent=2), "",
              "ULTIME RIGHE DEL LOG"]
    try:
        righe += LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-200:] or ["(vuoto)"]
    except OSError:
        righe.append("(nessun log)")
    testo = _anonimizza("\n".join(righe)) + "\n"

    cartella = Path.home() / "Desktop"
    if not cartella.is_dir():
        cartella = Path.home()
    nome = f"report_registratore_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt"
    percorso = unique_path(cartella / nome)
    with open(percorso, "xb") as f:
        f.write(testo.encode("utf-8"))
    return percorso


# --------------------------------------------------------------------------
# Interfaccia (tema scuro / chiaro)
# --------------------------------------------------------------------------
TEMI = {
    "scuro": {"bg": "#171716", "fg": "#e9e7e1", "mut": "#8f8d85", "campo": "#242422",
              "linea": "#37362f", "acc": "#f08a52", "acc_fg": "#171716", "rec": "#ff5a4d"},
    "chiaro": {"bg": "#f7f6f2", "fg": "#1b1b1a", "mut": "#77756e", "campo": "#ffffff",
               "linea": "#d6d4cb", "acc": "#c2410c", "acc_fg": "#ffffff", "rec": "#d11a0e"},
}
FONT = ("Segoe UI", 10)
FONT_B = ("Segoe UI Semibold", 10)
FONT_T = ("Segoe UI Semibold", 14)
FONT_S = ("Segoe UI", 9)


class App:
    def __init__(self):
        global TEMA_HTML
        self.cfg = carica_config()
        self.tema = self.cfg.get("tema") if self.cfg.get("tema") in TEMI else "scuro"
        TEMA_HTML = self.tema
        self.root = tk.Tk()
        self.root.title(APP)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._chiudi)
        self.root.report_callback_exception = self._errore_tk
        self.barra_nascosta = None
        self.rec = None
        self.in_chiusura = False
        self.frame = None
        self.logo_img = None
        self.icon_img = None
        self._icona()
        self._schermata_avvio()

    @property
    def c(self):
        return TEMI[self.tema]

    def _icona(self):
        p = risorsa("icon.png")
        if not p.exists():
            return
        try:
            from PIL import ImageTk
            im = Image.open(p).convert("RGBA")
            self.icon_img = ImageTk.PhotoImage(im.resize((64, 64), Image.Resampling.LANCZOS))
            self.root.iconphoto(True, self.icon_img)
            self.logo_img = ImageTk.PhotoImage(im.resize((34, 34), Image.Resampling.LANCZOS))
        except Exception:
            pass

    # ---- piccoli componenti ----
    def _bottone(self, parent, testo, cmd, primario=False, **kw):
        c = self.c
        bg = c["acc"] if primario else c["campo"]
        fg = c["acc_fg"] if primario else c["fg"]
        return tk.Button(parent, text=testo, command=cmd, relief="flat", bd=0, cursor="hand2",
                         font=FONT_B if primario else FONT, bg=bg, fg=fg,
                         activebackground=c["fg"] if primario else c["linea"],
                         activeforeground=c["bg"] if primario else c["fg"],
                         highlightthickness=0, padx=14, pady=7, **kw)

    def _etichetta(self, parent, testo="", mut=False, font=FONT, **kw):
        c = self.c
        return tk.Label(parent, text=testo, font=font, bg=c["bg"], fg=c["mut"] if mut else c["fg"], **kw)

    def _nuovo_frame(self, padding):
        if self.frame is not None:
            self.frame.destroy()
        self.root.configure(bg=self.c["bg"])
        self.frame = tk.Frame(self.root, bg=self.c["bg"], padx=padding, pady=padding)
        self.frame.pack(fill="both", expand=True)
        return self.frame

    # ---- schermata iniziale ----
    def _schermata_avvio(self):
        c = self.c
        nome_prec = self.var_nome.get() if hasattr(self, "var_nome") and self.frame else ""
        self.root.attributes("-topmost", False)
        self.root.title(APP)
        f = self._nuovo_frame(22)
        self.root.geometry("")

        testa = tk.Frame(f, bg=c["bg"])
        testa.pack(fill="x", pady=(0, 18))
        if self.logo_img:
            tk.Label(testa, image=self.logo_img, bg=c["bg"]).pack(side="left", padx=(0, 10))
        self._etichetta(testa, "Registratore azioni", font=FONT_T).pack(side="left")
        simbolo = "☀ chiaro" if self.tema == "scuro" else "☾ scuro"
        b = tk.Button(testa, text=simbolo, command=self._cambia_tema, relief="flat", bd=0, cursor="hand2",
                      font=FONT_S, bg=c["bg"], fg=c["mut"], activebackground=c["bg"], activeforeground=c["fg"])
        b.pack(side="right")

        self._etichetta(f, "Nome del progetto", mut=True, font=FONT_S).pack(anchor="w")
        self.var_nome = tk.StringVar(value=nome_prec)
        e = tk.Entry(f, textvariable=self.var_nome, font=FONT, width=44, bg=c["campo"], fg=c["fg"],
                     insertbackground=c["fg"], relief="flat", highlightthickness=1,
                     highlightbackground=c["linea"], highlightcolor=c["acc"])
        e.pack(fill="x", ipady=6, pady=(3, 14))
        e.focus_set()

        self._etichetta(f, "Cartella dei progetti", mut=True, font=FONT_S).pack(anchor="w")
        riga = tk.Frame(f, bg=c["bg"])
        riga.pack(fill="x", pady=(3, 14))
        self.var_root = tk.StringVar(value=self.cfg.get("cartella_radice") or "(da scegliere)")
        tk.Label(riga, textvariable=self.var_root, font=FONT, bg=c["bg"], fg=c["fg"], anchor="w",
                 wraplength=300, justify="left").pack(side="left", fill="x", expand=True)
        self._bottone(riga, "Cambia…", self._scegli_root).pack(side="right")

        self._etichetta(f, "Qualità degli screenshot", mut=True, font=FONT_S).pack(anchor="w")
        seg = tk.Frame(f, bg=c["bg"])
        seg.pack(anchor="w", pady=(3, 4))
        self.seg = {}
        corrente = self.cfg["modalita_corrente"]
        self.var_modo = tk.StringVar(value=corrente if corrente in self.cfg["modalita"] else "Normale")
        for nome in self.cfg["modalita"]:
            lab = tk.Label(seg, text=nome, font=FONT, padx=12, pady=5, cursor="hand2")
            lab.pack(side="left", padx=(0, 4))
            lab.bind("<Button-1>", lambda _e, n=nome: self._sel_modo(n))
            self.seg[nome] = lab
        self.var_desc = tk.StringVar()
        tk.Label(f, textvariable=self.var_desc, font=FONT_S, bg=c["bg"], fg=c["mut"]).pack(anchor="w", pady=(0, 16))
        self._sel_modo(self.var_modo.get(), salva=False)

        self._bottone(f, "Avvia registrazione", self._avvia, primario=True).pack(fill="x")
        pausa = str(self.cfg.get("tasto_pausa", "f10")).upper()
        fine = str(self.cfg.get("tasto_fine", "f9")).upper()
        self._etichetta(f, f"{pausa} pausa / riprendi  ·  {fine} fine e salva", mut=True,
                        font=FONT_S).pack(anchor="w", pady=(10, 0))
        piede = tk.Frame(f, bg=c["bg"])
        piede.pack(fill="x", pady=(16, 0))
        for testo, cmd in (("Guida", self._guida), ("Segnala un problema", self._segnala)):
            tk.Button(piede, text=testo, command=cmd, relief="flat", bd=0, cursor="hand2", font=FONT_S,
                      bg=c["bg"], fg=c["mut"], activebackground=c["bg"], activeforeground=c["fg"],
                      padx=0).pack(side="left", padx=(0, 16))
        self._etichetta(piede, f"v{VERSION} · open source", mut=True, font=FONT_S).pack(side="right")
        self.root.bind("<Return>", lambda _e: self._avvia())

        if not self.cfg.get("cartella_radice"):
            self.root.after(300, lambda: self._scegli_root(prima_volta=True))

    def _sel_modo(self, nome, salva=True):
        c = self.c
        self.var_modo.set(nome)
        for n, lab in self.seg.items():
            attivo = n == nome
            lab.config(bg=c["acc"] if attivo else c["campo"], fg=c["acc_fg"] if attivo else c["fg"])
        m = self.cfg["modalita"].get(nome, {})
        self.var_desc.set(f"{m.get('formato', 'JPEG')} · qualità {m.get('qualita', 65)} · "
                          f"dimensione {int(float(m.get('scala', 1)) * 100)}%")
        if salva:
            self.cfg["modalita_corrente"] = nome
            salva_config(self.cfg)

    def _cambia_tema(self):
        global TEMA_HTML
        self.tema = "chiaro" if self.tema == "scuro" else "scuro"
        TEMA_HTML = self.tema
        self.cfg["tema"] = self.tema
        salva_config(self.cfg)
        self._schermata_avvio()

    def _scegli_root(self, prima_volta=False):
        titolo = "Scegli la cartella dove salvare tutti i progetti" if prima_volta else "Cambia cartella dei progetti"
        d = filedialog.askdirectory(title=titolo, parent=self.root, mustexist=False)
        if d:
            self.cfg["cartella_radice"] = str(Path(d))
            salva_config(self.cfg)
            self.var_root.set(self.cfg["cartella_radice"])

    # ---- avvio registrazione ----
    def _avvia(self):
        if self.rec is not None:
            return
        nome = pulisci_nome(self.var_nome.get())
        if not nome:
            messagebox.showwarning(APP, "Scrivi un nome per il progetto.", parent=self.root)
            return
        if not self.cfg.get("cartella_radice"):
            self._scegli_root(prima_volta=True)
            if not self.cfg.get("cartella_radice"):
                return
        try:
            radice = Path(self.cfg["cartella_radice"])
            radice.mkdir(parents=True, exist_ok=True)
            cartella = unique_path(radice / nome)  # mai su una cartella già esistente
            cartella.mkdir()
        except OSError as e:
            messagebox.showerror(APP, f"Non riesco a creare la cartella:\n{e}", parent=self.root)
            return

        nome_modo = self.var_modo.get()
        self.rec = Registratore(cartella, nome_modo, self.cfg["modalita"][nome_modo], self.cfg)
        self.in_chiusura = False
        try:
            self.rec.avvia()
        except Exception as e:
            messagebox.showerror(APP, f"Errore all'avvio:\n{e}", parent=self.root)
            self.rec = None
            return
        self._schermata_registrazione()

    def _schermata_registrazione(self):
        c = self.c
        self.root.unbind("<Return>")
        f = self._nuovo_frame(8)
        self.lbl_stato = tk.Label(f, text="● REC", font=FONT_B, bg=c["bg"], fg=c["rec"])
        self.lbl_stato.pack(side="left", padx=(4, 10))
        self.lbl_passi = tk.Label(f, text="0 passi", font=FONT, bg=c["bg"], fg=c["fg"], width=9, anchor="w")
        self.lbl_passi.pack(side="left", padx=(0, 8))
        self.btn_pausa = self._bottone(f, "Pausa", self._toggle_pausa)
        self.btn_pausa.config(width=8, padx=6, pady=4)
        self.btn_pausa.pack(side="left", padx=2)
        fine = self._bottone(f, "Fine", self._finisci, primario=True)
        fine.config(width=6, padx=6, pady=4)
        fine.pack(side="left", padx=2)
        self.root.title(f"REC · {self.rec.dir.name}")
        self.root.geometry("")
        self.root.update_idletasks()
        w = self.root.winfo_reqwidth()
        self.root.geometry(f"+{self.root.winfo_screenwidth() - w - 40}+12")
        self.root.attributes("-topmost", True)
        self.barra_nascosta = None
        if self.cfg.get("nascondi_barra", True):
            try:
                self.barra_nascosta = nascondi_da_cattura(int(self.root.frame(), 16), True)
            except Exception:
                self.barra_nascosta = False
            _log.info("Barra nascosta dagli screenshot: %s", self.barra_nascosta)
        self._poll()

    def _poll(self):
        if self.rec is None or self.in_chiusura:
            return
        try:
            hwnd = int(self.root.frame(), 16)
            l, t, r, b = rettangolo_finestra(hwnd)
            self.rec.own_rect = (l - 6, t - 6, r + 6, b + 6)
        except Exception:
            pass
        while True:
            try:
                ev = self.rec.ui_events.get_nowait()
            except queue.Empty:
                break
            if ev == "pausa":
                self._toggle_pausa()
            elif ev == "fine":
                self._finisci()
                return
        self.lbl_passi.config(text=f"{len(self.rec.steps)} passi")
        self.root.after(150, self._poll)

    def _toggle_pausa(self):
        if not self.rec:
            return
        c = self.c
        in_pausa = self.rec.alterna_pausa()
        self.lbl_stato.config(text="❚❚ PAUSA" if in_pausa else "● REC", fg=c["mut"] if in_pausa else c["rec"])
        self.btn_pausa.config(text="Riprendi" if in_pausa else "Pausa")

    # ---- fine ----
    def _finisci(self):
        if not self.rec or self.in_chiusura:
            return
        self.in_chiusura = True
        self.lbl_stato.config(text="Salvo…", fg=self.c["mut"])
        self.root.update()
        rec = self.rec
        rec.ferma()
        self.root.attributes("-topmost", False)

        attuale = rec.dir.name
        nuovo = simpledialog.askstring(
            "Registrazione terminata",
            f"{len(rec.steps)} passi salvati.\nNome del progetto (lascia uguale per tenerlo):",
            initialvalue=attuale, parent=self.root)
        nuovo = pulisci_nome(nuovo or "")
        if nuovo and nuovo != attuale:
            destinazione = unique_path(rec.dir.parent / nuovo)
            try:
                os.rename(rec.dir, destinazione)  # non sovrascrive: ho scelto un nome libero
                rec.dir = destinazione
            except OSError as e:
                messagebox.showwarning(APP, f"Non sono riuscito a rinominare, resta «{attuale}».\n{e}", parent=self.root)

        rec._salva_json()
        rec.scrivi_html()
        index = rec.dir / "index.html"
        errori = rec.errori[:]
        cartella = rec.dir
        self.rec = None
        msg = f"Salvato in:\n{cartella}"
        if errori:
            msg += f"\n\n(Ci sono stati {len(errori)} errori durante la registrazione, ad esempio: {errori[0]})"
        messagebox.showinfo(APP, msg, parent=self.root)
        try:
            os.startfile(index)
        except Exception:
            pass
        self.var_nome.set("")
        self._schermata_avvio()

    def _errore_tk(self, exc, val, tb):
        import traceback
        _log.error("Errore nell'interfaccia:\n%s", "".join(traceback.format_exception(exc, val, tb)))

    def _guida(self):
        c = self.c
        w = tk.Toplevel(self.root)
        w.title("Guida - " + APP)
        w.configure(bg=c["bg"])
        w.geometry("640x560")
        t = tk.Text(w, wrap="word", font=FONT, bg=c["bg"], fg=c["fg"], relief="flat", bd=0,
                    padx=22, pady=18, spacing1=2, spacing3=4, highlightthickness=0, insertbackground=c["bg"])
        sb = tk.Scrollbar(w, command=t.yview)
        t.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        t.pack(side="left", fill="both", expand=True)
        t.tag_configure("titolo", font=FONT_B, foreground=c["acc"], spacing1=12)
        for riga in GUIDA.strip().splitlines():
            if riga and riga == riga.upper() and not riga.startswith(("•", "F10")):
                t.insert("end", riga + "\n", "titolo")
            else:
                t.insert("end", riga + "\n")
        t.config(state="disabled")

    def _segnala(self):
        try:
            percorso = crea_report(self.cfg, self.barra_nascosta)
        except Exception as e:
            _log.exception("Impossibile creare il report")
            messagebox.showerror(APP, f"Non sono riuscito a creare il report:\n{e}\n\nScrivimi lo stesso a {EMAIL}", parent=self.root)
            return
        messagebox.showinfo(
            APP,
            f"Ho creato il file di report:\n{percorso}\n\n"
            "Contiene dati tecnici (versione, Windows, schermi, impostazioni, errori), "
            "ma nessuno screenshot e nessun testo digitato. Puoi aprirlo e leggerlo.\n\n"
            f"Ora si apre la mail verso {EMAIL}: descrivi cosa è successo e allega il file.",
            parent=self.root)
        try:
            subprocess.Popen(f'explorer /select,"{percorso}"')
        except Exception:
            pass
        oggetto = urllib.parse.quote(f"Segnalazione {APP} {VERSION}")
        corpo = urllib.parse.quote("Cosa stavo facendo e cosa è andato storto:\n\n(allega il file di report)")
        try:
            webbrowser.open(f"mailto:{EMAIL}?subject={oggetto}&body={corpo}")
        except Exception:
            pass

    def _chiudi(self):
        if self.rec is not None and not self.in_chiusura:
            if messagebox.askyesno(APP, "Registrazione in corso. Vuoi terminarla e salvare?", parent=self.root):
                self._finisci()
            return
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
