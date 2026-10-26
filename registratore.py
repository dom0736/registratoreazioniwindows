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
import queue
import re
import threading
import time
from ctypes import wintypes
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import mss
from PIL import Image, ImageDraw
from pynput import keyboard, mouse
from pynput.keyboard import Key
from pynput.mouse import Button

APP = "Registratore Azioni"

# Coordinate del mouse e pixel dello schermo devono coincidere (schermi HiDPI)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# --------------------------------------------------------------------------
# Configurazione
# --------------------------------------------------------------------------
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "RegistratoreAzioni"
CONFIG_FILE = CONFIG_DIR / "config.json"

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
                "creato": self.creato,
                "modalita": self.nome_modo,
                "passi": self.steps,
            })
        except OSError as e:
            self.errori.append(repr(e))

    def scrivi_html(self):
        try:
            genera_html(self.dir, self.dir.name, self.steps, self.nome_modo, self.creato)
        except OSError as e:
            self.errori.append(repr(e))


# --------------------------------------------------------------------------
# Pagina HTML
# --------------------------------------------------------------------------
HTML_TEMPLATE = r"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITOLO__</title>
<style>
:root{--bg:#f4f6fa;--card:#fff;--tx:#1d2330;--mut:#667089;--acc:#2563eb;--bd:#e2e6ee}
@media (prefers-color-scheme:dark){:root{--bg:#11141b;--card:#1a1f2c;--tx:#e8ebf2;--mut:#98a1b5;--acc:#7aa7ff;--bd:#2a3144}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:16px/1.5 system-ui,"Segoe UI",sans-serif}
header{max-width:1000px;margin:0 auto;padding:28px 20px 8px}
h1{margin:0 0 4px;font-size:1.7rem;outline:none}
.sub{color:var(--mut);font-size:.9rem}
.bar{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}
button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:8px 14px;font:inherit;cursor:pointer}
button.sec{background:transparent;color:var(--acc);border:1px solid var(--acc)}
main{max-width:1000px;margin:0 auto;padding:8px 20px 60px}
.step{display:flex;gap:14px;background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:14px;margin:14px 0;break-inside:avoid}
.num{flex:none;width:36px;height:36px;border-radius:50%;background:var(--acc);color:#fff;display:grid;place-items:center;font-weight:600}
.body{flex:1;min-width:0}
.desc{margin:0 0 2px;font-weight:600;outline:none;border-radius:4px;padding:2px 4px;margin-left:-4px}
.desc:focus{background:rgba(37,99,235,.12)}
.meta{margin:0 0 10px;color:var(--mut);font-size:.85rem}
img{max-width:100%;height:auto;border-radius:8px;border:1px solid var(--bd);display:block}
.hint{color:var(--mut);font-size:.85rem}
@media print{body{background:#fff;color:#000}.bar,.hint{display:none}.step{border-color:#ccc}}
</style>
</head>
<body>
<header>
<h1 id="titolo" contenteditable="true" data-id="titolo">__TITOLO__</h1>
<div class="sub">__SOTTO__</div>
<div class="bar">
<button onclick="window.print()">Stampa / Salva PDF</button>
<button class="sec" onclick="salvaCopia()">Salva copia modificata…</button>
</div>
<div class="hint">Clicca su un titolo o una descrizione per modificarli: le modifiche si ricordano in questo browser. Con «Salva copia modificata» le tieni in un nuovo file (salvalo nella stessa cartella delle immagini).</div>
</header>
<main>
__PASSI__
</main>
<script>
const KEY='rec:'+location.pathname;
let saved={};
try{saved=JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){}
document.querySelectorAll('[data-id]').forEach(el=>{
  const id=el.dataset.id;
  if(saved[id]!==undefined) el.textContent=saved[id];
  el.addEventListener('input',()=>{saved[id]=el.textContent;try{localStorage.setItem(KEY,JSON.stringify(saved))}catch(e){}});
});
async function salvaCopia(){
  const clone=document.documentElement.cloneNode(true);
  const txt='<!doctype html>\n'+clone.outerHTML;
  const base=(document.getElementById('titolo').textContent.trim()||'guida').replace(/[\\/:*?"<>|]/g,'_');
  const nome=base+'_modificata.html';
  if(window.showSaveFilePicker){
    try{
      const h=await showSaveFilePicker({suggestedName:nome,types:[{description:'Pagina HTML',accept:{'text/html':['.html']}}]});
      const w=await h.createWritable();await w.write(txt);await w.close();return;
    }catch(e){if(e.name==='AbortError')return;}
  }
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([txt],{type:'text/html'}));
  a.download=nome;a.click();
}
</script>
</body>
</html>
"""


def genera_html(cartella: Path, titolo: str, steps: list, nome_modo: str, creato: str) -> Path:
    blocchi = []
    for s in steps:
        meta = html.escape(s["ora"])
        if s.get("finestra"):
            meta += " · " + html.escape(s["finestra"])
        blocchi.append(
            f'<article class="step"><div class="num">{s["n"]}</div><div class="body">'
            f'<p class="desc" contenteditable="true" data-id="d{s["n"]}">{html.escape(s["testo"])}</p>'
            f'<p class="meta">{meta}</p>'
            f'<a href="{html.escape(s["img"])}" target="_blank">'
            f'<img loading="lazy" src="{html.escape(s["img"])}" alt="Passo {s["n"]}"></a>'
            f'</div></article>'
        )
    try:
        data = datetime.datetime.fromisoformat(creato).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        data = creato
    sotto = f"{len(steps)} passi · registrato il {data} · modalità {html.escape(nome_modo)}"
    pagina = (HTML_TEMPLATE
              .replace("__TITOLO__", html.escape(titolo))
              .replace("__SOTTO__", sotto)
              .replace("__PASSI__", "\n".join(blocchi) if blocchi else "<p>Nessun passo registrato.</p>"))
    out = cartella / "index.html"
    tmp = cartella / "index.html.tmp"
    tmp.write_text(pagina, encoding="utf-8")
    os.replace(tmp, out)
    return out


# --------------------------------------------------------------------------
# Interfaccia
# --------------------------------------------------------------------------
class App:
    def __init__(self):
        self.cfg = carica_config()
        self.root = tk.Tk()
        self.root.title(APP)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._chiudi)
        self.rec = None
        self.in_chiusura = False
        self.frame = None
        self._schermata_avvio()

    # ---- schermata iniziale ----
    def _schermata_avvio(self):
        if self.frame:
            self.frame.destroy()
        self.root.attributes("-topmost", False)
        self.root.geometry("")
        self.frame = ttk.Frame(self.root, padding=18)
        self.frame.pack()
        f = self.frame

        ttk.Label(f, text="Nome del progetto").grid(row=0, column=0, sticky="w")
        self.var_nome = tk.StringVar()
        e = ttk.Entry(f, textvariable=self.var_nome, width=42)
        e.grid(row=1, column=0, columnspan=2, sticky="we", pady=(2, 12))
        e.focus_set()

        ttk.Label(f, text="Cartella dove salvare i progetti").grid(row=2, column=0, sticky="w")
        self.var_root = tk.StringVar(value=self.cfg.get("cartella_radice") or "(da scegliere)")
        ttk.Label(f, textvariable=self.var_root, foreground="#555", wraplength=300).grid(row=3, column=0, sticky="w", pady=(2, 12))
        ttk.Button(f, text="Cambia…", command=self._scegli_root).grid(row=3, column=1, sticky="e", padx=(8, 0))

        ttk.Label(f, text="Qualità screenshot").grid(row=4, column=0, sticky="w")
        self.var_modo = tk.StringVar(value=self.cfg["modalita_corrente"] if self.cfg["modalita_corrente"] in self.cfg["modalita"] else "Normale")
        cb = ttk.Combobox(f, textvariable=self.var_modo, values=list(self.cfg["modalita"].keys()), state="readonly", width=20)
        cb.grid(row=5, column=0, sticky="w", pady=(2, 2))
        cb.bind("<<ComboboxSelected>>", self._modo_cambiato)
        self.var_desc = tk.StringVar()
        ttk.Label(f, textvariable=self.var_desc, foreground="#555").grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 12))
        self._modo_cambiato()

        ttk.Button(f, text="Avvia registrazione", command=self._avvia).grid(row=7, column=0, columnspan=2, sticky="we", pady=(4, 8))
        pausa = str(self.cfg.get("tasto_pausa", "f10")).upper()
        fine = str(self.cfg.get("tasto_fine", "f9")).upper()
        ttk.Label(f, text=f"Durante la registrazione: {pausa} = pausa/riprendi · {fine} = fine e salva",
                  foreground="#777").grid(row=8, column=0, columnspan=2, sticky="w")
        self.root.bind("<Return>", lambda _e: self._avvia())

        if not self.cfg.get("cartella_radice"):
            self.root.after(300, lambda: self._scegli_root(prima_volta=True))

    def _modo_cambiato(self, _e=None):
        nome = self.var_modo.get()
        m = self.cfg["modalita"].get(nome, {})
        self.var_desc.set(f"{m.get('formato', 'JPEG')} · qualità {m.get('qualita', 65)} · dimensione {int(float(m.get('scala', 1)) * 100)}%")
        self.cfg["modalita_corrente"] = nome
        salva_config(self.cfg)

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
        self.root.unbind("<Return>")
        self.frame.destroy()
        self.frame = ttk.Frame(self.root, padding=8)
        self.frame.pack()
        self.lbl_stato = tk.Label(self.frame, text="● REC", fg="#d11", font=("Segoe UI", 10, "bold"))
        self.lbl_stato.grid(row=0, column=0, padx=(0, 8))
        self.lbl_passi = ttk.Label(self.frame, text="0 passi")
        self.lbl_passi.grid(row=0, column=1, padx=(0, 8))
        self.btn_pausa = ttk.Button(self.frame, text="Pausa", width=9, command=self._toggle_pausa)
        self.btn_pausa.grid(row=0, column=2, padx=2)
        ttk.Button(self.frame, text="Fine", width=7, command=self._finisci).grid(row=0, column=3, padx=2)
        self.root.title(f"REC · {self.rec.dir.name}")
        self.root.update_idletasks()
        w = self.root.winfo_reqwidth()
        self.root.geometry(f"+{self.root.winfo_screenwidth() - w - 40}+12")
        self.root.attributes("-topmost", True)
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
        in_pausa = self.rec.alterna_pausa()
        self.lbl_stato.config(text="❚❚ PAUSA" if in_pausa else "● REC", fg="#c80" if in_pausa else "#d11")
        self.btn_pausa.config(text="Riprendi" if in_pausa else "Pausa")

    # ---- fine ----
    def _finisci(self):
        if not self.rec or self.in_chiusura:
            return
        self.in_chiusura = True
        self.lbl_stato.config(text="Salvo…", fg="#555")
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
        self._schermata_avvio()

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
