"""
Registratore Azioni - sostituto di "Registratore azioni utente" (PSR) di Windows.

A ogni clic, trascinamento o azione da tastiera salva uno screenshot (con un
riquadro colorato in base al tipo di azione) in una cartella di progetto e,
alla fine, genera una pagina HTML con tutti i passi e le descrizioni modificabili.
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
import shutil
import sys
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from ctypes import wintypes
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog

import mss
from PIL import Image, ImageChops, ImageDraw, ImageOps
from pynput import keyboard, mouse
from pynput.keyboard import Key
from pynput.mouse import Button

APP = "Registratore Azioni"
VERSION = "1.3"
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

# Colore del riquadro per ogni tipo di azione (RGB). Stessi colori nella pagina HTML.
AZIONI = {
    "sinistro": ("Clic sinistro", (239, 68, 68)),
    "destro": ("Clic destro", (59, 130, 246)),
    "centrale": ("Clic centrale", (34, 197, 94)),
    "doppio": ("Doppio clic", (217, 70, 239)),
    "drag": ("Trascinamento", (245, 158, 11)),
    "tasto": ("Tastiera", (20, 184, 166)),
}

TEMA_HTML = "scuro"  # tema di partenza delle pagine HTML (lo sceglie l'utente nell'app)


def risorsa(nome: str) -> Path:
    """File incluso nell'exe (o accanto allo script, quando si lancia da Python)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / nome


def logo_base64() -> str:
    """Icona arrotondata (o quella di riserva), piccola, da mettere nell'HTML."""
    try:
        buf = io.BytesIO()
        icona_pil(96).save(buf, format="PNG", optimize=True)
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


def opzioni_salvataggio(fmt: str, q: int) -> dict:
    return {"JPEG": {"quality": q, "optimize": True},
            "WEBP": {"quality": q, "method": 4},
            "PNG": {"optimize": True}}[fmt]


def elenca_progetti(radice: Path) -> list:
    """Cartelle dentro `radice` che contengono un passi.json, dalla piu' recente."""
    trovati = []
    try:
        for d in Path(radice).iterdir():
            if d.is_dir() and (d / "passi.json").is_file():
                trovati.append(d)
    except OSError:
        pass
    return sorted(trovati, key=lambda d: d.stat().st_mtime, reverse=True)


def crea_copia_modificata(src: Path, titolo: str, testi: list) -> Path:
    """Crea accanto al progetto una cartella «nome_modificato» con le nuove descrizioni.
    L'originale non viene toccato."""
    src = Path(src)
    dati = json.loads((src / "passi.json").read_text(encoding="utf-8"))
    base = re.sub(r"_modificato(_\d+)?$", "", src.name)
    dest = unique_path(src.parent / f"{base}_modificato")
    dest.mkdir()
    if (src / "immagini").is_dir():
        shutil.copytree(src / "immagini", dest / "immagini")
    passi = dati.get("passi", [])
    for p, t in zip(passi, testi):
        t = t.strip() or p.get("testo", "")
        if t != p.get("testo"):
            p.setdefault("testo_originale", p.get("testo"))
            p["testo"] = t
    titolo = titolo.strip() or dati.get("titolo") or src.name
    dati["titolo"] = titolo
    scrivi_json_atomico(dest / "passi.json", dati)
    genera_html(dest, titolo, passi, dati.get("modalita", ""), dati.get("creato", ""))
    return dest


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
        self.ultimo_scroll = 0.0
        self.ultimo_box = None
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
            mouse.Listener(on_click=self._on_click, on_scroll=self._on_scroll),
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

    def sospendi(self):
        """Pausa e attende che i passi in coda siano salvati (serve alla schermata di fine)."""
        self._flush_typing()
        self.paused = True
        self.press = None
        self.q.join()

    def riprendi(self):
        self.paused = False
        self.press = None

    # ---- mouse ----
    def _on_scroll(self, x, y, dx, dy):
        # La rotellina non crea passi, ma sposta la pagina: ricordo quando e' successo
        self.ultimo_scroll = time.time()

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
                self.q.put({"kind": "click", "x": x, "y": y, "button": nome, "t": now})
                self.last_click = (now, x, y, button)
        else:
            p, self.press = self.press, None
            if p and math.hypot(x - p[0], y - p[1]) > 25:
                self.q.put({"kind": "drag", "x": p[0], "y": p[1], "x2": x, "y2": y, "t": p[2]})

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
            self._segna_doppio()
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

        spessore = max(3, round(4 * max(scala, 0.5)))
        raggio = max(16, int(38 * scala))
        d = ImageDraw.Draw(img)

        def a_img(x, y):
            return ((x - mon["left"]) * scala, (y - mon["top"]) * scala)

        # Se dopo il clic c'e' stata una rotellina, la pagina si e' spostata:
        # il riquadro cadrebbe nel punto sbagliato, quindi non lo disegno.
        spostata = kind in ("click", "drag") and self.ultimo_scroll > ev.get("t", 0)
        box = None

        if kind == "click":
            az = ev["button"] if ev["button"] in AZIONI else "centrale"
            colore = AZIONI[az][1]
            testo = f"Clic {ev['button']}"
            if not spostata:
                cx, cy = a_img(px, py)
                d.rectangle([cx - raggio, cy - raggio, cx + raggio, cy + raggio], outline=colore, width=spessore)
                box = (cx, cy)
        elif kind == "drag":
            az = "drag"
            colore = AZIONI[az][1]
            testo = "Trascinamento (dal quadratino piccolo al riquadro grande)"
            if not spostata:
                sx, sy = a_img(ev["x"], ev["y"])
                ex, ey = a_img(ev["x2"], ev["y2"])
                d.line([sx, sy, ex, ey], fill=colore, width=spessore)
                d.rectangle([sx - raggio // 2, sy - raggio // 2, sx + raggio // 2, sy + raggio // 2],
                            outline=colore, width=spessore)
                d.rectangle([ex - raggio, ey - raggio, ex + raggio, ey + raggio], outline=colore, width=spessore)
        else:
            az = "tasto"
            testo = ev["testo"]
            # Per la tastiera non c'e' un punto da cerchiare: una cornice colorata attorno a tutto lo schermo
            d.rectangle([0, 0, img.width - 1, img.height - 1], outline=AZIONI[az][1], width=spessore * 2)

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
        with open(path, "xb") as f:  # "x" = mai sovrascrivere
            img.save(f, format=fmt, **opzioni_salvataggio(fmt, q))

        step = {
            "n": len(self.steps) + 1,
            "tipo": kind,
            "az": az,
            "testo": testo,
            "finestra": finestra,
            "ora": datetime.datetime.now().strftime("%H:%M:%S"),
            "img": f"immagini/{path.name}",
        }
        self.steps.append(step)
        self.ultimo_box = ({"step": step, "path": path, "fmt": fmt, "q": q, "box": box,
                            "raggio": raggio, "spessore": spessore} if kind == "click" else None)
        self._salva_json()
        if len(self.steps) % 5 == 0:
            self.scrivi_html()

    def _segna_doppio(self):
        """Il secondo clic ravvicinato trasforma l'ultimo passo in «Doppio clic»:
        aggiungo un secondo riquadro, piu' grande e di un altro colore."""
        info, self.ultimo_box = self.ultimo_box, None
        if not info or not self.steps or self.steps[-1] is not info["step"]:
            return
        s = info["step"]
        s["testo"] = s["testo"].replace("Clic", "Doppio clic", 1)
        s["az"] = "doppio"
        if info["box"]:
            try:
                with Image.open(info["path"]) as im:
                    img = im.convert("RGB")
                cx, cy = info["box"]
                r = int(info["raggio"] * 1.55)
                ImageDraw.Draw(img).rectangle([cx - r, cy - r, cx + r, cy + r],
                                              outline=AZIONI["doppio"][1], width=info["spessore"])
                img.save(info["path"], format=info["fmt"], **opzioni_salvataggio(info["fmt"], info["q"]))
            except Exception as e:
                self.errori.append(repr(e))
                _log.exception("Errore nel disegnare il doppio clic")
        self._salva_json()

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
# Pagina HTML (una per progetto: contiene i dati)
# --------------------------------------------------------------------------
HTML_TEMPLATE = r"""<!doctype html>
<html lang="it" data-tema="__TEMA__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITOLO__</title>
__FAVICON__
<style>
:root,:root[data-tema=chiaro]{--bg:#f4f6fa;--tx:#1b2433;--mut:#66748c;--ln:#d5dbe6;--ac:#4a3fe0}
:root[data-tema=scuro]{--bg:#141b26;--tx:#e8edf5;--mut:#8c9ab0;--ln:#2a3649;--ac:#8fa9ff}
__COLORI_CSS__
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.55 "Segoe UI",system-ui,sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:34px 22px 90px}
header{display:flex;gap:14px;align-items:center;padding-bottom:16px;border-bottom:2px solid var(--tx)}
.logo{width:44px;height:44px;object-fit:contain;flex:none}
h1{margin:0;font-size:1.45rem;line-height:1.2;letter-spacing:-.01em;outline:none}
#sub,.m,.n{font-family:Consolas,"Cascadia Mono",monospace}
#sub{color:var(--mut);font-size:.78rem;margin-top:3px}
.tools{margin-left:auto;display:flex;gap:6px;align-items:center;flex-wrap:wrap;justify-content:flex-end}
.tools button{background:none;border:1px solid var(--ln);color:var(--tx);padding:5px 10px;font:inherit;font-size:.8rem;cursor:pointer}
.tools button:hover{border-color:var(--tx)}
.passo{display:grid;grid-template-columns:52px 1fr;padding:22px 0;border-bottom:1px solid var(--ln);break-inside:avoid}
.passo{--az:var(--ac)}
.n{font-weight:600;font-size:.95rem;color:var(--az);padding-top:1px}
#legenda{display:flex;flex-wrap:wrap;gap:6px 16px;padding:10px 0 2px;font-size:.74rem;color:var(--mut)}
#legenda span{display:inline-flex;align-items:center;gap:6px}
.lg{--az:var(--ac)}
#legenda i{width:12px;height:12px;border:2px solid var(--az);display:inline-block}
.d{margin:0;font-weight:600;outline:none}
.m{margin:2px 0 12px;color:var(--mut);font-size:.74rem}
figure{margin:0;position:relative}
img.s{width:100%;height:auto;display:block;border:1px solid var(--ln);border-top:3px solid var(--az);cursor:zoom-in}
.op{position:absolute;top:8px;right:8px;width:28px;height:28px;display:grid;place-items:center;background:var(--bg);color:var(--tx);border:1px solid var(--ln);text-decoration:none;opacity:0;transition:opacity .12s}
figure:hover .op{opacity:1}
@media (hover:none){.op{opacity:1}}
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
<button id="btema">Tema</button>
<button onclick="print()">PDF</button>
</div>
</header>
<div id="legenda"></div>
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
const D=JSON.parse(document.getElementById('dati').textContent);
const $=s=>document.querySelector(s);
let cur=-1;
const pad=n=>String(n).padStart(2,'0');

/* ---- tema ---- */
try{const t=localStorage.getItem('tema');if(t)document.documentElement.dataset.tema=t;}catch(e){}
$('#btema').onclick=()=>{
  const t=document.documentElement.dataset.tema==='scuro'?'chiaro':'scuro';
  document.documentElement.dataset.tema=t;
  try{localStorage.setItem('tema',t);}catch(e){}
};

/* ---- pagina ---- */
const NOMI_AZ=__NOMI_AZ__;
const az=p=>p.az||(p.tipo==='drag'?'drag':p.tipo==='tasto'?'tasto':'sinistro');
function legenda(){
  const L=$('#legenda');L.textContent='';
  const visti=[...new Set(D.passi.map(az))];
  Object.keys(NOMI_AZ).filter(k=>visti.includes(k)).forEach(k=>{
    const s=document.createElement('span');s.className='lg';s.dataset.az=k;
    const i=document.createElement('i');s.append(i,NOMI_AZ[k]);L.append(s);
  });
}
function render(){
  $('#titolo').textContent=D.titolo;
  document.title=D.titolo;
  let dt=D.creato;
  try{dt=new Date(D.creato).toLocaleString('it-IT',{dateStyle:'short',timeStyle:'short'});}catch(e){}
  $('#sub').textContent=D.passi.length+' passi · '+dt+' · '+D.modalita;
  const L=$('#lista');L.textContent='';
  D.passi.forEach((p,i)=>{
    const a=document.createElement('article');a.className='passo';a.dataset.az=az(p);
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
  legenda();
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
});

render();
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
    colori_css = "\n".join(
        f".passo[data-az={k}],.lg[data-az={k}]{{--az:#{r:02x}{g:02x}{b:02x}}}"
        for k, (_nome, (r, g, b)) in AZIONI.items())
    nomi_az = json.dumps({k: nome for k, (nome, _c) in AZIONI.items()}, ensure_ascii=False)
    pagina = (HTML_TEMPLATE
              .replace("__TEMA__", tema)
              .replace("__TITOLO__", html.escape(titolo))
              .replace("__FAVICON__", favicon)
              .replace("__LOGO__", img_logo)
              .replace("__COLORI_CSS__", colori_css)
              .replace("__NOMI_AZ__", nomi_az)
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
1. Scrivi il nome del progetto e premi «Avvia registrazione». I progetti vengono salvati nella cartella indicata in Impostazioni (la puoi cambiare quando vuoi).
2. Lavora normalmente. A ogni clic, trascinamento o azione da tastiera viene salvato uno screenshot con un riquadro colorato. La rotellina del mouse non conta.
3. Quando scrivi, il testo viene raggruppato: un passo per riga (per esempio «show ip route» + Invio), non uno per lettera.
4. F10 = pausa / riprendi.  F9 = fine. Puoi usare anche i pulsanti «Pausa» e «Fine» della barra in alto a destra.
5. Con «Fine» la registrazione si ferma ma non è ancora salvata: scrivi il nome del progetto e premi «Salva e apri» (si apre la pagina HTML), oppure «Riprendi registrazione» per continuare da dove eri. Anche chiudere la barra con la X porta a questa schermata.

I COLORI DEI RIQUADRI
Ogni tipo di azione ha il suo colore, uguale negli screenshot e nella pagina HTML:
• Rosso = clic sinistro
• Blu = clic destro
• Verde = clic centrale
• Viola = doppio clic (due riquadri, uno dentro l'altro)
• Arancione = trascinamento (dal quadratino piccolo al riquadro grande)
• Verde acqua = tastiera (cornice attorno a tutto lo schermo)
Se subito dopo un clic scorri con la rotellina, il riquadro non viene disegnato: la pagina si è spostata e finirebbe nel punto sbagliato.

QUALITÀ DEGLI SCREENSHOT
Minima, Leggera, Normale, Nitida: più sono leggere, meno spazio occupano. «Normale» va bene quasi sempre. Le modalità si possono cambiare in %APPDATA%\\RegistratoreAzioni\\config.json.

IMPOSTAZIONI
Cartella dei progetti, tema scuro o chiaro (vale anche per le pagine HTML) e se nascondere la barra del registratore dagli screenshot.

MODIFICARE UN PROGETTO
Dalla schermata iniziale, «Modifica un progetto»: scegli il progetto dalla lista, cambia il titolo e le descrizioni dei passi (ogni passo ha la sua miniatura) e premi «Salva come nuova cartella». Il programma crea «nomeprogetto_modificato» accanto all'originale, con immagini, pagina HTML e dati; l'originale non viene toccato.

LA PAGINA HTML
• Clic su un'immagine: si apre nel lettore (frecce ← → per scorrere, Esc per chiudere, clic per ingrandire). La freccetta ↗ in alto a destra la apre da sola in una nuova scheda.
• «PDF» stampa la pagina per salvarla in PDF. «Tema» passa da scuro a chiaro.

COSA VIENE CREATO
Nella cartella del progetto: index.html (la pagina), passi.json (i dati) e immagini/ (gli screenshot). Una copia modificata contiene le stesse cose; in passi.json le descrizioni cambiate conservano anche il testo originale. Non viene mai sovrascritto nulla: se un nome esiste già, il programma aggiunge _2, _3…

COSE DA SAPERE
• Tutto quello che scrivi e vedi finisce nel progetto, password comprese: metti in pausa quando entri in account personali.
• Un programma aperto come amministratore potrebbe non essere registrato.
• Viene catturato solo il monitor dove si trova il mouse.
• La barra del registratore viene nascosta dagli screenshot (su Windows 10 recente o Windows 11). Si può disattivare in Impostazioni.

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
# Grafica: sfondo sfumato, bottoni e campi arrotondati (disegnati con Pillow)
# --------------------------------------------------------------------------
SS = 3  # supersampling: bordi arrotondati senza scalini


def _rgb(h: str):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(int(v) for v in rgb)


def _mix(a, b, t: float):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def sfumatura(w: int, h: int, colori, verticale=True) -> Image.Image:
    n = len(colori) - 1
    lung = max(h if verticale else w, 2)
    riga = Image.new("RGB", (1, lung) if verticale else (lung, 1))
    for i in range(lung):
        u = i / (lung - 1) * n
        j = min(int(u), n - 1)
        riga.putpixel((0, i) if verticale else (i, 0), _mix(colori[j], colori[j + 1], u - j))
    return riga.resize((w, h), Image.Resampling.NEAREST)


def img_sfondo(w: int, h: int, t: dict) -> Image.Image:
    base = sfumatura(w, h, [_rgb(c) for c in t["grad"]], True)
    for colore, alpha, cx, cy, diam in t["bagliori"]:
        d = max(8, int(max(w, h) * diam))
        # il gradiente radiale di Pillow arriva a ~180 sul bordo del quadrato: lo porto a zero li'
        m = Image.radial_gradient("L").resize((d, d), Image.Resampling.BICUBIC)
        m = m.point(lambda v, a=alpha: int(max(0.0, 1 - v / 180) ** 2 * 255 * a))
        mask = Image.new("L", (w, h), 0)
        mask.paste(m, (int(w * cx - d / 2), int(h * cy - d / 2)))
        base.paste(Image.new("RGB", (w, h), _rgb(colore)), (0, 0), mask)
    return base.convert("RGBA")


def _capa_rr(w: int, h: int, r: int, fill=None, outline=None, ancho=1) -> Image.Image:
    capa = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    kw = {"width": ancho * SS} if outline else {}
    ImageDraw.Draw(capa).rounded_rectangle([0, 0, w * SS - 1, h * SS - 1], radius=r * SS,
                                           fill=fill, outline=outline, **kw)
    return capa.resize((w, h), Image.Resampling.LANCZOS)


def rr_su(base: Image.Image, box, r: int, fill=None, outline=None, ancho=1) -> None:
    x0, y0, x1, y1 = box
    base.alpha_composite(_capa_rr(x1 - x0, y1 - y0, r, fill, outline, ancho), (x0, y0))


def img_bottone(w: int, h: int, r: int, c1, c2) -> Image.Image:
    """Bottone pieno con sfumatura orizzontale c1 -> c2 e un leggero riflesso in alto."""
    img = sfumatura(w, h, [c1, c2], verticale=False).convert("RGBA")
    mask = _capa_rr(w, h, r, fill=(255, 255, 255, 255)).getchannel("A")
    luce = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    ImageDraw.Draw(luce).rectangle([0, 0, w, h // 2], fill=(255, 255, 255, 34))
    luce.putalpha(ImageChops.multiply(luce.getchannel("A"), mask))
    img.putalpha(mask)
    img.alpha_composite(luce)
    return img


def icona_default(n: int = 512) -> Image.Image:
    """Icona di riserva (quando non c'e' icon.png): fondo grigio-blu, foglio sfumato blu-magenta, pallino rosso."""
    s = n * 2
    im = Image.new("RGB", (s, s), (41, 56, 76))
    d = ImageDraw.Draw(im)
    x0, y0, x1, y1 = int(s * .24), int(s * .24), int(s * .76), int(s * .76)
    w, h = x1 - x0, y1 - y0
    foglio = Image.linear_gradient("L").resize((w, h)).transpose(Image.Transpose.ROTATE_90)
    blu, magenta = Image.new("RGB", (w, h), (14, 70, 254)), Image.new("RGB", (w, h), (240, 61, 239))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=int(s * .07), fill=255)
    im.paste(Image.composite(magenta, blu, foglio), (x0, y0), mask)
    for i, frac in enumerate((1.0, .62, .38)):
        y = y0 + int(h * (.22 + i * .2))
        d.rounded_rectangle([x0 + int(w * .16), y, x0 + int(w * (.16 + .68 * frac)), y + int(s * .016)],
                            radius=int(s * .008), fill="white")
    cx, cy, r = int(s * .70), int(s * .69), int(s * .095)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 77, 90))
    return im.resize((n, n), Image.Resampling.LANCZOS).convert("RGBA")


def icona_pil(dim: int) -> Image.Image:
    """icon.png (o quella di riserva) con gli angoli arrotondati, grande dim x dim."""
    try:
        im = Image.open(risorsa("icon.png")).convert("RGBA")
        im = ImageOps.fit(im, (512, 512), Image.Resampling.LANCZOS)
    except Exception:
        im = icona_default(512)
    if im.getpixel((2, 2))[3] > 200:  # angoli pieni: la arrotondo io (se sono gia' trasparenti, lascio com'e')
        mask = _capa_rr(512, 512, 112, fill=(255, 255, 255, 255)).getchannel("A")
        im.putalpha(ImageChops.multiply(im.getchannel("A"), mask))
    return im.resize((dim, dim), Image.Resampling.LANCZOS)


# --------------------------------------------------------------------------
# Interfaccia (tema scuro / chiaro: grigio-blu, accenti blu-viola-magenta come l'icona)
# --------------------------------------------------------------------------
TEMI = {
    "scuro": {
        "grad": ["#18212e", "#223044", "#2c3b52"],
        "bagliori": [("#7f42f7", 0.30, 1.0, 0.0, 1.1), ("#0e46fe", 0.55, 0.0, 1.08, 1.15)],
        "card": (255, 255, 255, 20), "card_bordo": (255, 255, 255, 38),
        "campo": "#1c2636", "campo_bordo": (255, 255, 255, 44), "card_campo": "#2a374c", "linea": "#3a4a62",
        "fg": "#f4f7fb", "mut": "#a9b8cf",
        "acc1": "#3d6fff", "acc2": "#c13af2", "acc_fg": "#ffffff",
        "vetro": (255, 255, 255, 28), "vetro_hover": (255, 255, 255, 56), "vetro_bordo": (255, 255, 255, 58),
        "rec": "#ff4d5a", "pausa": "#ffb020", "ok": "#8be9b0", "err": "#ffa08f",
    },
    "chiaro": {
        "grad": ["#f5f7fb", "#e6ebf6", "#d4daf2"],
        "bagliori": [("#ffffff", 0.65, 0.0, 0.0, 1.0), ("#7f42f7", 0.30, 1.0, 1.08, 1.15)],
        "card": (255, 255, 255, 150), "card_bordo": (255, 255, 255, 210),
        "campo": "#ffffff", "campo_bordo": (49, 65, 84, 70), "card_campo": "#eef1f8", "linea": "#cfd6e6",
        "fg": "#1b2740", "mut": "#5b6b88",
        "acc1": "#1f55ff", "acc2": "#b02fe0", "acc_fg": "#ffffff",
        "vetro": (49, 65, 84, 26), "vetro_hover": (49, 65, 84, 50), "vetro_bordo": (49, 65, 84, 60),
        "rec": "#e11d3f", "pausa": "#d97706", "ok": "#17724a", "err": "#b42318",
    },
}
FONT = ("Segoe UI", 10)
FONT_B = ("Segoe UI Semibold", 10)
FONT_T = ("Segoe UI Semibold", 15)
FONT_S = ("Segoe UI", 9)
FONT_SB = ("Segoe UI Semibold", 9)


class App:
    """Tutto in una sola finestra: cambia solo il contenuto (nessun popup di Windows,
    a parte la scelta della cartella dei progetti)."""

    def __init__(self):
        global TEMA_HTML
        from PIL import ImageTk
        self._ImageTk = ImageTk
        self.cfg = carica_config()
        self.tema = self.cfg.get("tema") if self.cfg.get("tema") in TEMI else "scuro"
        TEMA_HTML = self.tema
        if not self.cfg.get("cartella_radice"):
            self.cfg["cartella_radice"] = str(Path.home() / "Documents" / "Registratore Azioni")
            salva_config(self.cfg)

        self.root = tk.Tk()
        self.root.title(APP)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._chiudi)
        self.root.report_callback_exception = self._errore_tk
        try:  # schermi HiDPI: i pixel del disegno seguono la scala di Windows
            self.k = max(1.0, float(self.root.winfo_fpixels("1i")) / 96.0)
        except Exception:
            self.k = 1.0
        self.cv = tk.Canvas(self.root, highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)

        self.var_nome = tk.StringVar(master=self.root)
        self.var_fine = tk.StringVar(master=self.root)
        self.barra_nascosta = None
        self.rec = None
        self.in_chiusura = False
        self.scena = ""
        self.base = None
        self._refs = []
        self._widgets = []
        self._after_pulsa = None
        self._fase = False
        try:
            self.icon_img = ImageTk.PhotoImage(icona_pil(64))
            self.root.iconphoto(True, self.icon_img)
        except Exception:
            self.icon_img = None
        self._scena_avvio(pos="centro")

    @property
    def c(self):
        return TEMI[self.tema]

    # ---- mattoncini di disegno (le misure sono "logiche": qui si moltiplicano per la scala) ----
    def _px(self, v) -> int:
        return int(round(v * self.k))

    def _scena(self, nome, w, h, pos=None):
        """Svuota la finestra e prepara lo sfondo nuovo di w x h."""
        self.scena = nome
        if self._after_pulsa:
            try:
                self.root.after_cancel(self._after_pulsa)
            except Exception:
                pass
            self._after_pulsa = None
        for wd in self._widgets:
            try:
                wd.destroy()
            except Exception:
                pass
        self._widgets, self._refs = [], []
        for ev in ("<Return>", "<Escape>"):
            self.root.unbind(ev)
        try:
            self.root.unbind_all("<MouseWheel>")
        except Exception:
            pass
        self.cv.delete("all")
        W, H = self._px(w), self._px(h)
        self.cv.config(width=W, height=H)
        self.base = img_sfondo(W, H, self.c)
        self.w, self.h = w, h
        self._posiziona(W, H, pos)

    def _posiziona(self, W, H, pos):
        r = self.root
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        if pos == "alto_dx":
            x, y = sw - W - self._px(24), self._px(10)
        elif pos == "centro":
            x, y = (sw - W) // 2, max(10, (sh - H) // 3)
        else:  # resta dov'e'
            m = re.match(r"\d+x\d+([+-]\d+)([+-]\d+)", r.geometry())
            x, y = (int(m.group(1)), int(m.group(2))) if m else (100, 100)
            x, y = max(0, min(x, sw - W)), max(0, min(y, sh - H))
        r.geometry(f"{W}x{H}+{x}+{y}")

    def _rr(self, x0, y0, x1, y1, r, fill=None, outline=None):
        rr_su(self.base, (self._px(x0), self._px(y0), self._px(x1), self._px(y1)),
              self._px(r), fill, outline, 1)

    def _card(self, x0=16, y0=16, x1=None, y1=None):
        c = self.c
        self._rr(x0, y0, x1 if x1 is not None else self.w - 16, y1 if y1 is not None else self.h - 16,
                 20, c["card"], c["card_bordo"])

    def _campo_bg(self, x, y, w, h):
        c = self.c
        self._rr(x, y, x + w, y + h, 12, _rgb(c["campo"]) + (255,), c["campo_bordo"])

    def _sfondo_pronto(self):
        foto = self._ImageTk.PhotoImage(self.base)
        self._refs.append(foto)
        self.cv.create_image(0, 0, image=foto, anchor="nw")

    def _testo(self, x, y, s, font=FONT, fill=None, anchor="nw", width=None, justify="left"):
        return self.cv.create_text(self._px(x), self._px(y), text=s, font=font, fill=fill or self.c["fg"],
                                   anchor=anchor, width=self._px(width) if width else 0, justify=justify)

    def _etichetta(self, x, y, s):
        return self._testo(x, y, s, FONT_S, self.c["mut"])

    def _entry(self, x, y, w, h, var):
        c = self.c
        e = tk.Entry(self.cv, textvariable=var, font=FONT, bg=c["campo"], fg=c["fg"],
                     insertbackground=c["fg"], relief="flat", bd=0, highlightthickness=0)
        self.cv.create_window(self._px(x + 14), self._px(y + h / 2), window=e, anchor="w",
                              width=self._px(w - 28), height=self._px(h - 14))
        self._widgets.append(e)
        return e

    def _foto(self, img):
        foto = self._ImageTk.PhotoImage(img)
        self._refs.append(foto)
        return foto

    def _mani(self, oggetti, entra=None, esce=None, click=None):
        for o in oggetti:
            if entra:
                self.cv.tag_bind(o, "<Enter>", entra)
            if esce:
                self.cv.tag_bind(o, "<Leave>", esce)
            if click:
                self.cv.tag_bind(o, "<Button-1>", click)

    def _bottone(self, x, y, w, h, testo, cmd, primario=False, font=FONT_B):
        c = self.c
        W, H, R = self._px(w), self._px(h), self._px(h / 2)
        if primario:
            n = img_bottone(W, H, R, _rgb(c["acc1"]), _rgb(c["acc2"]))
            o = img_bottone(W, H, R, _mix(_rgb(c["acc1"]), (255, 255, 255), .18),
                            _mix(_rgb(c["acc2"]), (255, 255, 255), .18))
            col = c["acc_fg"]
        else:
            n = _capa_rr(W, H, R, c["vetro"], c["vetro_bordo"])
            o = _capa_rr(W, H, R, c["vetro_hover"], c["vetro_bordo"])
            col = c["fg"]
        pn, po = self._foto(n), self._foto(o)
        it = self.cv.create_image(self._px(x), self._px(y), image=pn, anchor="nw")
        tx = self.cv.create_text(self._px(x + w / 2), self._px(y + h / 2), text=testo, font=font, fill=col)
        self._mani((it, tx),
                   entra=lambda _e: (self.cv.itemconfig(it, image=po), self.cv.config(cursor="hand2")),
                   esce=lambda _e: (self.cv.itemconfig(it, image=pn), self.cv.config(cursor="")),
                   click=lambda _e: cmd())
        return it, tx

    def _link(self, x, y, s, cmd, anchor="nw", font=FONT_S):
        c = self.c
        t = self._testo(x, y, s, font, c["mut"], anchor)
        self._mani((t,),
                   entra=lambda _e: (self.cv.itemconfig(t, fill=c["fg"]), self.cv.config(cursor="hand2")),
                   esce=lambda _e: (self.cv.itemconfig(t, fill=c["mut"]), self.cv.config(cursor="")),
                   click=lambda _e: cmd())
        return t

    def _pillole(self, x, y, opzioni, selezionata, cmd, w=72, h=30, gap=8):
        """Scelta a pulsanti (al posto del menu a tendina). Restituisce la funzione che cambia la selezione."""
        c = self.c
        W, H, R = self._px(w), self._px(h), self._px(h / 2)
        on = self._foto(img_bottone(W, H, R, _rgb(c["acc1"]), _rgb(c["acc2"])))
        off = self._foto(_capa_rr(W, H, R, c["vetro"], c["vetro_bordo"]))
        voci = {}
        for i, nome in enumerate(opzioni):
            px_ = x + i * (w + gap)
            it = self.cv.create_image(self._px(px_), self._px(y), image=off, anchor="nw")
            tx = self.cv.create_text(self._px(px_ + w / 2), self._px(y + h / 2), text=nome, font=FONT)
            voci[nome] = (it, tx)
            self._mani((it, tx), click=lambda _e, n=nome: cmd(n),
                       entra=lambda _e: self.cv.config(cursor="hand2"), esce=lambda _e: self.cv.config(cursor=""))

        def seleziona(sel):
            for nome, (it, tx) in voci.items():
                attiva = nome == sel
                self.cv.itemconfig(it, image=on if attiva else off)
                self.cv.itemconfig(tx, fill=c["acc_fg"] if attiva else c["fg"],
                                   font=FONT_B if attiva else FONT)
        seleziona(selezionata)
        return seleziona

    def _intestazione(self, titolo, sotto=None):
        """Logo rotondo + titolo, in alto."""
        logo = self._foto(icona_pil(self._px(40)))
        self.cv.create_image(self._px(34), self._px(30), image=logo, anchor="nw")
        self._testo(84, 41 if sotto else 50, titolo, FONT_T, anchor="w")
        if sotto:
            self._testo(84, 62, sotto, FONT_S, self.c["mut"], anchor="w")

    def _msg(self, testo, ok=True):
        """Messaggio nella finestra (al posto dei popup)."""
        if getattr(self, "t_msg", None) and self.scena in ("avvio", "progetti", "modifica"):
            self.cv.itemconfig(self.t_msg, text=testo, fill=self.c["ok"] if ok else self.c["err"])

    # ---- schermata iniziale ----
    def _scena_avvio(self, pos=None, msg=None, ok=True):
        c = self.c
        self._scena("avvio", 380, 486, pos)
        self.root.title(APP)
        self.root.attributes("-topmost", False)
        try:
            nascondi_da_cattura(int(self.root.frame(), 16), False)  # la finestra normale puo' essere catturata
        except Exception:
            pass

        self._card()
        self._campo_bg(34, 118, 312, 42)
        self._sfondo_pronto()

        self._intestazione("Registratore azioni", "Un passo, uno screenshot")
        self._etichetta(34, 98, "Nome del progetto")
        e = self._entry(34, 118, 312, 42, self.var_nome)
        e.focus_set()

        self._etichetta(34, 178, "Qualità degli screenshot")
        corrente = self.cfg["modalita_corrente"]
        self.var_modo = corrente if corrente in self.cfg["modalita"] else "Normale"
        self.t_desc = self._testo(34, 242, "", FONT_S, c["mut"])
        self._sel_pill = self._pillole(34, 200, list(self.cfg["modalita"]), self.var_modo, self._sel_modo)
        self._sel_modo(self.var_modo, salva=False)

        self._bottone(34, 274, 312, 48, "Avvia registrazione", self._avvia, primario=True)
        pausa = str(self.cfg.get("tasto_pausa", "f10")).upper()
        fine = str(self.cfg.get("tasto_fine", "f9")).upper()
        self._bottone(34, 332, 312, 34, "Modifica un progetto", self._scena_progetti, font=FONT)
        self._testo(190, 376, f"{pausa} pausa / riprendi   ·   {fine} fine e salva", FONT_S, c["mut"], anchor="n")

        self.t_msg = self._testo(34, 398, "", FONT_S, c["ok"], width=312)
        if msg:
            self._msg(msg, ok)

        x = 34
        for testo, cmd in (("Impostazioni", self._scena_impostazioni), ("Guida", self._scena_guida),
                           ("Segnala un problema", self._segnala)):
            self._link(x, 452, testo, cmd)
            x += tkfont.Font(font=FONT_S).measure(testo) / self.k + 18
        self._testo(346, 452, f"v{VERSION}", FONT_S, c["mut"], anchor="ne")
        self.root.bind("<Return>", lambda _e: self._avvia())

    def _sel_modo(self, nome, salva=True):
        self.var_modo = nome
        self._sel_pill(nome)
        m = self.cfg["modalita"].get(nome, {})
        self.cv.itemconfig(self.t_desc, text=f"{m.get('formato', 'JPEG')} · qualità {m.get('qualita', 65)} · "
                                             f"dimensione {int(float(m.get('scala', 1)) * 100)}%")
        if salva:
            self.cfg["modalita_corrente"] = nome
            salva_config(self.cfg)

    # ---- impostazioni ----
    def _scena_impostazioni(self):
        c = self.c
        self._scena("impostazioni", 380, 450)
        self.root.title(f"Impostazioni · {APP}")
        self._card()
        self._sfondo_pronto()

        self._link(34, 36, "‹  Indietro", self._scena_avvio, font=FONT)
        self._testo(34, 74, "Impostazioni", FONT_T)

        self._etichetta(34, 124, "Cartella dei progetti")
        self.t_root = self._testo(34, 146, self.cfg.get("cartella_radice", ""), FONT_S, width=312)
        self._bottone(34, 188, 110, 32, "Cambia…", self._scegli_root, font=FONT)

        self._etichetta(34, 246, "Tema")
        self._pillole(34, 268, ["Scuro", "Chiaro"], self.tema.capitalize(), self._imposta_tema, w=96)

        self._etichetta(34, 320, "Nascondi la barra negli screenshot")
        self._pillole(34, 342, ["Sì", "No"], "Sì" if self.cfg.get("nascondi_barra", True) else "No",
                      self._imposta_barra, w=96)
        self._testo(34, 382, "Funziona su Windows 10 (2004 o successivi) e Windows 11.", FONT_S, c["mut"], width=312)
        self.root.bind("<Escape>", lambda _e: self._scena_avvio())

    def _imposta_tema(self, nome):
        global TEMA_HTML
        self.tema = nome.lower()
        TEMA_HTML = self.tema
        self.cfg["tema"] = self.tema
        salva_config(self.cfg)
        self._scena_impostazioni()

    def _imposta_barra(self, nome):
        self.cfg["nascondi_barra"] = nome == "Sì"
        salva_config(self.cfg)
        self._scena_impostazioni()

    def _scegli_root(self):
        iniziale = self.cfg.get("cartella_radice") or str(Path.home())
        d = filedialog.askdirectory(title="Scegli la cartella dei progetti", parent=self.root,
                                    mustexist=False, initialdir=iniziale if Path(iniziale).exists() else str(Path.home()))
        if d:
            self.cfg["cartella_radice"] = str(Path(d))
            salva_config(self.cfg)
            self.cv.itemconfig(self.t_root, text=self.cfg["cartella_radice"])

    # ---- guida (nella stessa finestra) ----
    def _scena_guida(self):
        c = self.c
        self._scena("guida", 540, 600)
        self.root.title(f"Guida · {APP}")
        self._card()
        self._campo_bg(30, 84, 480, 432)
        self._sfondo_pronto()

        self._link(34, 36, "‹  Indietro", self._scena_avvio, font=FONT)
        self._testo(506, 36, "Guida", FONT_T, anchor="ne")

        t = tk.Text(self.cv, wrap="word", font=FONT, bg=c["campo"], fg=c["fg"], relief="flat", bd=0,
                    padx=14, pady=10, spacing1=2, spacing3=4, highlightthickness=0, insertbackground=c["campo"])
        sb = tk.Scrollbar(self.cv, command=t.yview)
        t.config(yscrollcommand=sb.set)
        self.cv.create_window(self._px(34), self._px(88), window=t, anchor="nw", width=self._px(456), height=self._px(424))
        self.cv.create_window(self._px(490), self._px(88), window=sb, anchor="nw", width=self._px(16), height=self._px(424))
        self._widgets += [t, sb]
        t.tag_configure("titolo", font=FONT_B, foreground=c["acc1"], spacing1=12)
        for riga in GUIDA.strip().splitlines():
            if riga and riga == riga.upper() and not riga.startswith(("•", "F10")):
                t.insert("end", riga + "\n", "titolo")
            else:
                t.insert("end", riga + "\n")
        t.config(state="disabled")
        self._bottone(34, 530, 472, 44, "Ho capito", self._scena_avvio, primario=True)
        self.root.bind("<Escape>", lambda _e: self._scena_avvio())

    # ---- modifica di un progetto: salva una copia «nome_modificato» ----
    def _scena_progetti(self):
        c = self.c
        self._scena("progetti", 380, 480)
        self.root.title(f"Modifica un progetto · {APP}")
        self._card()
        self._campo_bg(34, 140, 312, 180)
        self._sfondo_pronto()

        self._link(34, 36, "‹  Indietro", self._scena_avvio, font=FONT)
        self._testo(34, 70, "Modifica un progetto", FONT_T)
        self._etichetta(34, 118, "Scegli il progetto (i più recenti in alto)")

        self._progetti = elenca_progetti(Path(self.cfg.get("cartella_radice", "")))
        lb = tk.Listbox(self.cv, font=FONT, bg=c["campo"], fg=c["fg"], relief="flat", bd=0, highlightthickness=0,
                        selectbackground=c["acc2"], selectforeground="#ffffff", activestyle="none",
                        exportselection=False)
        sb = tk.Scrollbar(self.cv, command=lb.yview)
        lb.config(yscrollcommand=sb.set)
        self.cv.create_window(self._px(40), self._px(146), window=lb, anchor="nw", width=self._px(280), height=self._px(168))
        self.cv.create_window(self._px(322), self._px(146), window=sb, anchor="nw", width=self._px(16), height=self._px(168))
        self._widgets += [lb, sb]
        for d in self._progetti:
            lb.insert("end", d.name)
        if self._progetti:
            lb.selection_set(0)
            lb.bind("<Double-Button-1>", lambda _e: self._modifica_selezionato())
        else:
            self._testo(190, 230, "Nessun progetto in questa cartella.\nUsa «Altra cartella…»", FONT_S,
                        c["mut"], anchor="center", justify="center")
        self.lb = lb
        self._bottone(34, 330, 140, 32, "Altra cartella…", self._altra_cartella, font=FONT)
        self._bottone(34, 374, 312, 44, "Modifica", self._modifica_selezionato, primario=True)
        self.t_msg = self._testo(34, 428, "", FONT_S, c["err"], width=312)
        self.root.bind("<Return>", lambda _e: self._modifica_selezionato())
        self.root.bind("<Escape>", lambda _e: self._scena_avvio())

    def _altra_cartella(self):
        d = filedialog.askdirectory(title="Scegli la cartella del progetto da modificare", parent=self.root,
                                    mustexist=True, initialdir=self.cfg.get("cartella_radice") or str(Path.home()))
        if not d:
            return
        d = Path(d)
        if (d / "passi.json").is_file():
            self._scena_modifica(d)
        else:
            self._msg("In quella cartella non c'è un passi.json: scegli la cartella del progetto.", ok=False)

    def _modifica_selezionato(self):
        if self.scena != "progetti":
            return
        sel = self.lb.curselection()
        if not sel:
            self._msg("Scegli un progetto dalla lista.", ok=False)
            return
        self._scena_modifica(self._progetti[int(sel[0])])

    def _scena_modifica(self, src):
        c = self.c
        try:
            dati = json.loads((Path(src) / "passi.json").read_text(encoding="utf-8"))
            passi = dati.get("passi", [])
        except Exception as e:
            self._msg(f"Non riesco a leggere il progetto: {e}", ok=False)
            return
        self._src = Path(src)
        base = re.sub(r"_modificato(_\d+)?$", "", self._src.name)
        self._scena("modifica", 600, 664)
        self.root.title(f"Modifica · {self._src.name}")
        self._card()
        self._campo_bg(34, 126, 532, 38)
        self._campo_bg(34, 176, 532, 376)
        self._sfondo_pronto()

        self._link(34, 36, "‹  Annulla", self._scena_avvio, font=FONT)
        self._testo(34, 70, f"Modifica «{self._src.name}»", FONT_T)
        self._etichetta(34, 106, "Titolo della pagina")
        self.var_titolo = tk.StringVar(master=self.root, value=dati.get("titolo") or self._src.name)
        self._entry(34, 126, 532, 38, self.var_titolo)

        sc = tk.Canvas(self.cv, bg=c["campo"], highlightthickness=0, bd=0)
        sb = tk.Scrollbar(self.cv, command=sc.yview)
        sc.config(yscrollcommand=sb.set)
        self.cv.create_window(self._px(40), self._px(182), window=sc, anchor="nw", width=self._px(504), height=self._px(364))
        self.cv.create_window(self._px(546), self._px(182), window=sb, anchor="nw", width=self._px(16), height=self._px(364))
        fr = tk.Frame(sc, bg=c["campo"])
        sc.create_window(0, 0, window=fr, anchor="nw", width=self._px(500))
        fr.bind("<Configure>", lambda _e: sc.configure(scrollregion=sc.bbox("all")))
        self._widgets += [sc, sb]
        self._miniature, self._vars_testi = [], []
        for i, p in enumerate(passi):
            riga = tk.Frame(fr, bg=c["campo"])
            riga.pack(fill="x", pady=(0, 8))
            thumb = None
            try:
                with Image.open(self._src / p.get("img", "")) as im:
                    im.draft("RGB", (320, 180))
                    im = im.convert("RGB")
                    im.thumbnail((self._px(104), self._px(60)), Image.Resampling.LANCZOS)
                    thumb = self._ImageTk.PhotoImage(im)
                self._miniature.append(thumb)
            except Exception:
                pass
            if thumb is None:  # miniatura mancante: riquadro vuoto (cosi' la larghezza resta in pixel)
                thumb = self._ImageTk.PhotoImage(Image.new("RGB", (self._px(104), self._px(60)), c["card_campo"]))
                self._miniature.append(thumb)
            lab = tk.Label(riga, image=thumb, bg=c["campo"], width=self._px(104), height=self._px(60))
            lab.pack(side="left", padx=(0, 10))
            col = tk.Frame(riga, bg=c["campo"])
            col.pack(side="left", fill="x", expand=True)
            tk.Label(col, text=f"{int(p.get('n', i + 1)):02d}  ·  {p.get('ora', '')}", font=FONT_S, bg=c["campo"],
                     fg=c["mut"], anchor="w").pack(fill="x")
            v = tk.StringVar(master=self.root, value=p.get("testo", ""))
            tk.Entry(col, textvariable=v, font=FONT, bg=c["card_campo"], fg=c["fg"], insertbackground=c["fg"],
                     relief="flat", bd=0, highlightthickness=1, highlightbackground=c["linea"],
                     highlightcolor=c["acc1"]).pack(fill="x", ipady=5)
            self._vars_testi.append(v)
        if not passi:
            tk.Label(fr, text="Questo progetto non ha passi.", font=FONT, bg=c["campo"], fg=c["mut"]).pack(pady=20)
        self.root.bind_all("<MouseWheel>", lambda e: sc.yview_scroll(int(-e.delta / 120), "units"))

        self._testo(34, 558, f"Si crea «{base}_modificato» accanto all'originale, che non cambia.",
                    FONT_S, c["mut"], width=532)
        self._bottone(34, 600, 340, 44, "Salva come nuova cartella", self._salva_modifica, primario=True)
        self._bottone(386, 600, 180, 44, "Annulla", self._scena_avvio)
        self.t_msg = self._testo(34, 576, "", FONT_S, c["err"], width=532)

    def _salva_modifica(self):
        if self.scena != "modifica":
            return
        try:
            dest = crea_copia_modificata(self._src, self.var_titolo.get(), [v.get() for v in self._vars_testi])
        except Exception as e:
            _log.exception("Errore nel salvare la copia modificata")
            self._msg(f"Non sono riuscito a salvare: {e}", ok=False)
            return
        try:
            os.startfile(dest / "index.html")
        except Exception:
            pass
        self._scena_avvio(msg=f"✓ Creata la cartella «{dest.name}» con le modifiche. L'originale non è cambiato.")

    # ---- avvio registrazione ----
    def _avvia(self):
        if self.rec is not None or self.scena != "avvio":
            return
        nome = pulisci_nome(self.var_nome.get())
        if not nome:
            self._msg("Scrivi un nome per il progetto.", ok=False)
            return
        try:
            radice = Path(self.cfg["cartella_radice"])
            radice.mkdir(parents=True, exist_ok=True)
            cartella = unique_path(radice / nome)  # mai su una cartella già esistente
            cartella.mkdir()
        except OSError as e:
            self._msg(f"Non riesco a creare la cartella: {e}", ok=False)
            return

        nome_modo = self.var_modo
        self.rec = Registratore(cartella, nome_modo, self.cfg["modalita"][nome_modo], self.cfg)
        self.in_chiusura = False
        try:
            self.rec.avvia()
        except Exception as e:
            self.rec = None
            self._msg(f"Errore all'avvio: {e}", ok=False)
            return
        self._scena_registrazione(prima_volta=True)
        self._poll()

    # ---- barra di registrazione ----
    def _scena_registrazione(self, prima_volta=False):
        c = self.c
        self._scena("rec", 392, 58, "alto_dx")
        self._rr(1, 1, self.w - 1, self.h - 1, 14, None, c["card_bordo"])
        self._sfondo_pronto()
        self.dot = self.cv.create_oval(self._px(16), self._px(21), self._px(32), self._px(37), width=0)
        self.t_stato = self._testo(42, 29, "", FONT_B, anchor="w")
        self.t_passi = self._testo(150, 29, "", FONT_S, c["mut"], anchor="w")
        _it, self.t_pausa = self._bottone(222, 12, 90, 34, "Pausa", self._toggle_pausa, font=FONT)
        self._bottone(320, 12, 62, 34, "Fine", self._ferma_e_chiedi, primario=True)
        self.root.attributes("-topmost", True)
        self.root.update_idletasks()
        self._aggiorna_rec()
        self.barra_nascosta = None
        if self.cfg.get("nascondi_barra", True):
            try:
                self.barra_nascosta = nascondi_da_cattura(int(self.root.frame(), 16), True)
            except Exception:
                self.barra_nascosta = False
            if prima_volta:
                _log.info("Barra nascosta dagli screenshot: %s", self.barra_nascosta)
        self._after_pulsa = self.root.after(550, self._pulsa)

    def _aggiorna_rec(self):
        if self.scena != "rec" or not self.rec:
            return
        c = self.c
        in_pausa = self.rec.paused
        self.cv.itemconfig(self.t_stato, text="In pausa" if in_pausa else "Registrando",
                           fill=c["pausa"] if in_pausa else c["fg"])
        self.cv.itemconfig(self.t_pausa, text="Riprendi" if in_pausa else "Pausa")
        col = c["pausa"] if in_pausa else c["rec"]
        self.cv.itemconfig(self.dot, fill=col)
        self.root.title(f"{'Pausa' if in_pausa else 'REC'} · {self.rec.dir.name}")
        n = len(self.rec.steps)
        self.cv.itemconfig(self.t_passi, text=f"{n} passo" if n == 1 else f"{n} passi")

    def _pulsa(self):
        if self.scena != "rec" or not self.rec:
            return
        c = self.c
        if self.rec.paused:
            col = c["pausa"]
        else:
            self._fase = not self._fase
            col = c["rec"] if self._fase else _hex(_mix(_rgb(c["rec"]), _rgb(c["grad"][0]), .6))
        self.cv.itemconfig(self.dot, fill=col)
        self._after_pulsa = self.root.after(550, self._pulsa)

    def _poll(self):
        if self.rec is None or self.in_chiusura:
            return
        if self.scena == "rec":
            try:
                l, t, r, b = rettangolo_finestra(int(self.root.frame(), 16))
                self.rec.own_rect = (l - 6, t - 6, r + 6, b + 6)
            except Exception:
                pass
        while True:
            try:
                ev = self.rec.ui_events.get_nowait()
            except queue.Empty:
                break
            if self.scena != "rec":
                continue  # schermata di fine: i tasti F9 / F10 non servono
            if ev == "pausa":
                self._toggle_pausa()
            elif ev == "fine":
                self._ferma_e_chiedi()
        self._aggiorna_rec()
        self.root.after(150, self._poll)

    def _toggle_pausa(self):
        if not self.rec or self.scena != "rec":
            return
        self.rec.alterna_pausa()
        self._aggiorna_rec()

    # ---- fine: la registrazione si ferma ma non si salva finché non lo decidi ----
    def _ferma_e_chiedi(self):
        if not self.rec or self.in_chiusura or self.scena != "rec":
            return
        self.cv.itemconfig(self.t_stato, text="Salvo i passi…")
        self.root.update_idletasks()
        self.rec.sospendi()
        self._scena_fine()

    def _scena_fine(self):
        c = self.c
        n = len(self.rec.steps)
        self._scena("fine", 380, 360, "centro")
        self.root.title(f"Registrazione ferma · {APP}")
        self.root.attributes("-topmost", False)
        self._card()
        self._campo_bg(34, 138, 312, 42)
        self._sfondo_pronto()

        self._intestazione("Registrazione ferma", f"{n} passo salvato" if n == 1 else f"{n} passi registrati")
        self._etichetta(34, 118, "Nome del progetto (puoi cambiarlo)")
        self.var_fine.set(self.rec.dir.name)
        e = self._entry(34, 138, 312, 42, self.var_fine)
        e.focus_set()
        e.select_range(0, "end")
        self._bottone(34, 200, 312, 48, "Salva e apri", self._salva, primario=True)
        self._bottone(34, 258, 312, 40, "Riprendi registrazione", self._riprendi)
        self._testo(190, 312, "Invio salva  ·  Esc riprende", FONT_S, c["mut"], anchor="n")
        self.root.bind("<Return>", lambda _e: self._salva())
        self.root.bind("<Escape>", lambda _e: self._riprendi())

    def _riprendi(self):
        if not self.rec or self.in_chiusura:
            return
        self.rec.riprendi()
        self._scena_registrazione()

    def _salva(self, chiudi=False):
        if not self.rec or self.in_chiusura:
            return
        self.in_chiusura = True
        rec = self.rec
        attuale = rec.dir.name
        nuovo = pulisci_nome(self.var_fine.get())
        rec.ferma()
        nota = ""
        if nuovo and nuovo != attuale:
            destinazione = unique_path(rec.dir.parent / nuovo)
            try:
                os.rename(rec.dir, destinazione)  # non sovrascrive: ho scelto un nome libero
                rec.dir = destinazione
            except OSError as e:
                nota = f" Non sono riuscito a rinominare, resta «{attuale}» ({e})."
        rec._salva_json()
        rec.scrivi_html()
        index = rec.dir / "index.html"
        n = len(rec.steps)
        errori = rec.errori[:]
        cartella = rec.dir
        self.rec = None
        self.in_chiusura = False
        if chiudi:
            self.root.destroy()
            return
        try:
            os.startfile(index)
        except Exception:
            pass
        msg = f"✓ {n} passo salvato in {cartella}.{nota}" if n == 1 else f"✓ {n} passi salvati in {cartella}.{nota}"
        if errori:
            msg += f" Ci sono stati {len(errori)} errori (il primo: {errori[0]})."
        self.var_nome.set("")
        self._scena_avvio(msg=msg, ok=not (errori or nota))

    def _errore_tk(self, exc, val, tb):
        import traceback
        _log.error("Errore nell'interfaccia:\n%s", "".join(traceback.format_exception(exc, val, tb)))

    # ---- segnalazioni ----
    def _segnala(self):
        try:
            percorso = crea_report(self.cfg, self.barra_nascosta)
        except Exception as e:
            _log.exception("Impossibile creare il report")
            self._msg(f"Non sono riuscito a creare il report ({e}). Scrivimi lo stesso a {EMAIL}.", ok=False)
            return
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
        self._msg(f"Report creato: {percorso.name}. Contiene dati tecnici, nessuno screenshot né testo "
                  f"digitato. Allegalo alla mail che si è aperta ({EMAIL}).")

    def _chiudi(self):
        if self.rec is not None and not self.in_chiusura:
            if self.scena == "rec":
                self._ferma_e_chiedi()  # la X non butta via niente: porta alla schermata di fine
                return
            if self.scena == "fine":
                self._salva(chiudi=True)
                return
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
