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
VERSION = "1.6"
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
    "ripresa": ("Ripresa", (99, 102, 241)),
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
    "backup_massimo": 10,
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


def scrivi_testo_atomico(path: Path, testo: str) -> None:
    """Scrive in un file temporaneo e poi lo sostituisce; se qualcosa va storto il temporaneo non resta."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(testo, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def scrivi_json_atomico(path: Path, data) -> None:
    scrivi_testo_atomico(path, json.dumps(data, ensure_ascii=False, indent=2))


# ---- pulizia: niente resti che si accumulano nel tempo ----
MARCHIO = "marchio_registratore.txt"   # file incluso nell'exe: riconosce le cartelle temporanee di QUESTO programma
_marchio_aperto = None
GIORNO = 86400


def togli_tmp(cartella: Path) -> None:
    """Elimina i file .tmp rimasti da una chiusura anomala (solo nella cartella indicata)."""
    try:
        for f in Path(cartella).glob("*.tmp"):
            try:
                f.unlink()
            except OSError:
                pass
    except OSError:
        pass


def pulisci_temporanei() -> None:
    """L'exe si scompatta in una cartella _MEIxxxx dentro Temp e la toglie da solo quando si chiude.
    Se il programma viene chiuso a forza, la cartella resta: qui tolgo quelle vecchie lasciate da questo
    stesso programma (riconosciute dal marchio), mai quelle di altri programmi né quella in uso."""
    global _marchio_aperto
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return
    try:  # tengo il marchio aperto: finché giro, nessun'altra copia può cancellare la mia cartella
        _marchio_aperto = open(risorsa(MARCHIO), "rb")
    except OSError:
        return
    ora = time.time()
    try:
        for d in Path(meipass).parent.glob("_MEI*"):
            try:
                if d == Path(meipass) or not d.is_dir() or ora - d.stat().st_mtime < GIORNO:
                    continue
                marchio, libero = d / MARCHIO, d / (MARCHIO + ".libero")
                if marchio.is_file():
                    os.replace(marchio, libero)  # fallisce se un'altra copia lo sta usando: allora non tocco nulla
                elif not libero.is_file():
                    continue  # non è una cartella di questo programma
                shutil.rmtree(d, ignore_errors=True)
            except OSError:
                continue
    except OSError:
        pass
    _log.info("Pulizia temporanei eseguita")


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
                vecchie = sorted(CONFIG_DIR.glob("config_rovinata*.json"), key=lambda f: f.stat().st_mtime)
                for f in vecchie[:-3]:  # ne tengo solo 3
                    f.unlink()
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


# ---- cartella «backup cancellati»: niente si perde subito, ma ha un limite ----
CARTELLA_BACKUP = "backup cancellati"
BACKUP_MASSIMO = 10
_GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
_MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
         "settembre", "ottobre", "novembre", "dicembre"]


def data_estesa(dt) -> str:
    return f"{_GIORNI[dt.weekday()]} {dt.day} {_MESI[dt.month - 1]} {dt.year}"


def pota_backup(bk: Path, massimo: int, tieni=None) -> None:
    """Tiene al massimo `massimo` voci nel backup: elimina le più vecchie."""
    massimo = max(1, int(massimo))
    try:
        voci = [d for d in Path(bk).iterdir() if d.is_dir()]
        voci.sort(key=lambda d: d.stat().st_mtime)
    except OSError:
        return
    for d in voci[:max(0, len(voci) - massimo)]:
        if tieni is not None and d == tieni:
            continue
        shutil.rmtree(d, ignore_errors=True)


def destinazione_backup(radice: Path, nome: str, motivo: str):
    bk = Path(radice) / CARTELLA_BACKUP
    bk.mkdir(parents=True, exist_ok=True)
    marca = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return bk, unique_path(bk / f"{nome}__{motivo}_{marca}")


def sposta_in_backup(cartella: Path, radice: Path, motivo: str, massimo: int) -> Path:
    """Sposta una cartella di progetto nel backup (mai cancellata subito)."""
    cartella = Path(cartella)
    bk, dest = destinazione_backup(radice, cartella.name, motivo)
    try:
        os.rename(cartella, dest)
    except OSError:  # per esempio un file aperto: copio e poi tolgo l'originale
        shutil.copytree(cartella, dest)
        shutil.rmtree(cartella, ignore_errors=True)
    try:
        os.utime(dest, None)  # la data della voce è quella di oggi: serve a capire quali sono le più vecchie
    except OSError:
        pass
    pota_backup(bk, massimo, tieni=dest)
    return dest


def copia_in_backup(cartella: Path, radice: Path, motivo: str, massimo: int) -> Path:
    """Copia di sicurezza (l'originale resta dov'è): si usa quando qualcosa va storto."""
    cartella = Path(cartella)
    bk, dest = destinazione_backup(radice, cartella.name, motivo)
    shutil.copytree(cartella, dest)
    pota_backup(bk, massimo, tieni=dest)
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


def hwnd_finestra(widget) -> int:
    """Finestra di primo livello (quella vera di Windows) di un widget Tk."""
    try:
        u = ctypes.windll.user32
        u.GetAncestor.restype = ctypes.c_void_p
        u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        h = int(widget.winfo_id())
        return int(u.GetAncestor(h, 2) or h)  # 2 = GA_ROOT
    except Exception:
        return 0


def nascosta_da_cattura(hwnd: int) -> bool:
    """Controlla che Windows abbia davvero escluso la finestra dagli screenshot."""
    try:
        u = ctypes.windll.user32
        u.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowDisplayAffinity.restype = wintypes.BOOL
        v = wintypes.DWORD(0)
        return bool(u.GetWindowDisplayAffinity(wintypes.HWND(hwnd), ctypes.byref(v))) and v.value == 0x11
    except Exception:
        return False


def colora_titolo(hwnd: int, sfondo, testo, scuro: bool) -> None:
    """Barra del titolo di Windows nello stesso colore dell'app (Windows 11; su Windows 10 almeno scura)."""
    try:
        dwm = ctypes.windll.dwmapi

        def attr(n, v):
            val = ctypes.c_int(v)
            return dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), n, ctypes.byref(val), 4)

        if attr(20, 1 if scuro else 0) != 0:   # DWMWA_USE_IMMERSIVE_DARK_MODE
            attr(19, 1 if scuro else 0)        # versione vecchia di Windows 10
        rgb = lambda c: c[0] | (c[1] << 8) | (c[2] << 16)
        attr(35, rgb(sfondo))  # colore della barra del titolo
        attr(34, rgb(sfondo))  # colore del bordo
        attr(36, rgb(testo))   # colore del testo
    except Exception:
        pass


def arrotonda_finestra(hwnd: int, w: int, h: int, diametro: int) -> None:
    """Angoli arrotondati per la barra senza cornice (senza trasparenza, quindi resta nascondibile agli screenshot)."""
    try:
        g, u = ctypes.windll.gdi32, ctypes.windll.user32
        g.CreateRoundRectRgn.restype = ctypes.c_void_p
        g.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
        u.SetWindowRgn.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]
        u.SetWindowRgn(wintypes.HWND(hwnd), g.CreateRoundRectRgn(0, 0, w + 1, h + 1, diametro, diametro), True)
    except Exception:
        pass


def togli_arrotondamento(hwnd: int) -> None:
    try:
        u = ctypes.windll.user32
        u.SetWindowRgn.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]
        u.SetWindowRgn(wintypes.HWND(hwnd), None, True)
    except Exception:
        pass


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
# ---- una sola istanza alla volta ----
_MUTEX = None
_EVENTO = None


def _istanza_unica() -> bool:
    """True se questa è la prima istanza. Se ce n'è già un'altra aperta, le chiede di riportarsi
    in primo piano, mostra un avviso e restituisce False (il programma deve chiudersi)."""
    global _MUTEX, _EVENTO
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.restype = ctypes.c_void_p
        k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateEventW.restype = ctypes.c_void_p
        k.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        k.SetEvent.argtypes = [ctypes.c_void_p]
        _MUTEX = k.CreateMutexW(None, False, "Local\\RegistratoreAzioni.IstanzaUnica")
        gia_aperto = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
        _EVENTO = k.CreateEventW(None, False, False, "Local\\RegistratoreAzioni.MostraFinestra")
        if not gia_aperto:
            return True
        if _EVENTO:
            k.SetEvent(_EVENTO)
        ctypes.windll.user32.MessageBoxW(
            0, f"{APP} è già aperto: l'ho riportato in primo piano.\n\n"
               "Se non lo vedi, controlla la barra delle applicazioni.",
            APP, 0x40 | 0x40000)  # icona info + sempre in primo piano
        return False
    except Exception:
        return True  # nel dubbio il programma si avvia lo stesso



class Registratore:
    def __init__(self, cartella: Path, nome_modo: str, modo: dict, cfg: dict, esistente: dict | None = None):
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
        self.maschera_barra = False  # True se Windows non nasconde la barra: la copro io negli screenshot
        self.mouse_ctl = mouse.Controller()
        self.contatore = 0
        self.creato = datetime.datetime.now().isoformat(timespec="seconds")
        self.k_pausa = getattr(Key, str(cfg.get("tasto_pausa", "f10")).lower(), Key.f10)
        self.k_fine = getattr(Key, str(cfg.get("tasto_fine", "f9")).lower(), Key.f9)
        self.pausa_dig = float(cfg.get("pausa_digitazione_sec", 1.2))
        self.listeners = []
        self.worker = None
        self.titolo = None          # None = il titolo è il nome della cartella
        self.steps_prima = None     # passi che c'erano prima di questa ripresa (None = registrazione nuova)
        self.immagini_prima = set()
        if esistente:  # si riprende un progetto già registrato: si continua a numerare da dove era
            self.steps = list(esistente.get("passi", []))
            self.creato = esistente.get("creato") or self.creato
            tit = esistente.get("titolo")
            self.titolo = tit if tit and tit != self.dir.name else None
            self.steps_prima = json.loads(json.dumps(self.steps))
            try:
                self.immagini_prima = {f.name for f in (self.dir / "immagini").iterdir()}
            except OSError:
                pass
            numeri = [int(m.group(1)) for nome in self.immagini_prima if (m := re.match(r"(\d+)", nome))]
            self.contatore = max(numeri, default=0)

    @property
    def ripresa(self) -> bool:
        return self.steps_prima is not None

    @staticmethod
    def _conta_reali(steps) -> int:
        return sum(1 for s in steps if s.get("tipo") != "ripresa")

    @property
    def n_passi(self) -> int:
        """Passi veri (i segnaposto «ripresa» non contano)."""
        return self._conta_reali(self.steps)

    @property
    def n_nuovi(self) -> int:
        """Passi registrati in questa sessione (in una ripresa: solo quelli nuovi)."""
        return self.n_passi - (self._conta_reali(self.steps_prima) if self.ripresa else 0)

    def _segnaposto_ripresa(self) -> dict:
        ora = datetime.datetime.now()
        return {"n": None, "tipo": "ripresa", "az": "ripresa",
                "testo": f"Registrazione ripresa · {data_estesa(ora)}, ore {ora:%H:%M}",
                "finestra": "", "ora": ora.strftime("%H:%M:%S"), "img": ""}

    def togli_ripresa_vuota(self):
        """Se dopo l'ultima ripresa non è stato registrato niente, il segnaposto non serve."""
        while self.steps and self.steps[-1].get("tipo") == "ripresa":
            self.steps.pop()

    def annulla_ripresa(self, radice: Path, massimo: int):
        """Toglie i passi di questa ripresa: il progetto torna com'era. I passi tolti vanno nel backup."""
        nuovi = self.steps[len(self.steps_prima):]
        nuovi_file = sorted(f.name for f in (self.dir / "immagini").iterdir() if f.name not in self.immagini_prima)
        dest = None
        if nuovi_file or self._conta_reali(nuovi):
            bk, dest = destinazione_backup(radice, self.dir.name, "ripresa-annullata")
            (dest / "immagini").mkdir(parents=True)
            for nome in nuovi_file:
                shutil.move(str(self.dir / "immagini" / nome), str(dest / "immagini" / nome))
            scrivi_json_atomico(dest / "passi.json", {
                "progetto": self.dir.name, "titolo": self.titolo or self.dir.name, "creato": self.creato,
                "modalita": self.nome_modo, "passi": nuovi})
            pota_backup(bk, massimo, tieni=dest)
        self.steps = json.loads(json.dumps(self.steps_prima))
        self._salva_json()
        self.scrivi_html()
        return dest

    # ---- ciclo di vita ----
    def avvia(self):
        (self.dir / "immagini").mkdir(parents=True, exist_ok=True)
        if self.ripresa:
            self.steps.append(self._segnaposto_ripresa())  # segna quando e in che giorno è ripresa
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

        if self.maschera_barra and any(self.own_rect):
            l, t, r_, b_ = self.own_rect
            ImageDraw.Draw(img).rectangle([(l - mon["left"]) * scala, (t - mon["top"]) * scala,
                                           (r_ - mon["left"]) * scala, (b_ - mon["top"]) * scala], fill=(22, 31, 45))

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
            # Per la tastiera non c'e' un punto da cerchiare: nessuna cornice sullo screenshot
            # (il testo digitato e il colore verde acqua stanno sul numero del passo, nella pagina HTML)

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
            "n": self.n_passi + 1,
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
        if self.n_passi % 5 == 0:
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
                "titolo": self.titolo or self.dir.name,
                "creato": self.creato,
                "modalita": self.nome_modo,
                "passi": self.steps,
            })
        except OSError as e:
            self.errori.append(repr(e))
            _log.exception("Errore nel salvataggio di passi.json")

    def scrivi_html(self):
        try:
            genera_html(self.dir, self.titolo or self.dir.name, self.steps, self.nome_modo, self.creato)
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
:root,:root[data-tema=scuro]{
  --fg:#f4f7fb;--mut:#a9b8cf;--card:rgba(255,255,255,.07);--cb:rgba(255,255,255,.13);
  --vetro:rgba(255,255,255,.11);--vetro-h:rgba(255,255,255,.2);--campo:#1c2636;--sel:rgba(193,58,242,.2);
  --a1:#3d6fff;--a2:#c13af2;--ok:#8be9b0;--err:#ffa08f;
  --bg:linear-gradient(160deg,#18212e 0%,#223044 55%,#2c3b52 100%);
  --glow1:rgba(14,70,254,.5);--glow2:rgba(127,66,247,.32);--sh:0 10px 30px rgba(0,0,0,.25)}
:root[data-tema=chiaro]{
  --fg:#1b2740;--mut:#5b6b88;--card:rgba(255,255,255,.72);--cb:rgba(255,255,255,.95);
  --vetro:rgba(49,65,84,.1);--vetro-h:rgba(49,65,84,.2);--campo:#ffffff;--sel:rgba(176,47,224,.14);
  --a1:#1f55ff;--a2:#b02fe0;--ok:#17724a;--err:#b42318;
  --bg:linear-gradient(160deg,#f5f7fb 0%,#e6ebf6 55%,#d4daf2 100%);
  --glow1:rgba(31,85,255,.16);--glow2:rgba(127,66,247,.22);--sh:0 10px 30px rgba(40,50,90,.12)}
__COLORI_CSS__
*{box-sizing:border-box}
body{margin:0;min-height:100vh;color:var(--fg);font:15px/1.55 "Segoe UI",system-ui,sans-serif;
  background:radial-gradient(60% 50% at 0% 100%,var(--glow1),transparent 70%),
             radial-gradient(55% 45% at 100% 0%,var(--glow2),transparent 70%),var(--bg);
  background-attachment:fixed}
.wrap{max-width:880px;margin:0 auto;padding:26px 18px 120px}
.top{display:flex;gap:14px;align-items:center;padding:16px 18px;border-radius:24px;
  background:var(--card);border:1px solid var(--cb);box-shadow:var(--sh);backdrop-filter:blur(10px)}
.logo{width:48px;height:48px;border-radius:14px;object-fit:contain;flex:none}
.tit{min-width:0;flex:1}
h1{margin:0;font-size:1.4rem;line-height:1.25;letter-spacing:-.01em;overflow-wrap:anywhere}
#sub{color:var(--mut);font-size:.8rem;margin-top:2px}
.tools{display:flex;gap:8px;flex:none}
.ib{width:40px;height:40px;border-radius:50%;border:1px solid var(--cb);background:var(--vetro);color:var(--fg);
  display:grid;place-items:center;cursor:pointer;text-decoration:none;padding:0;transition:background .12s}
.ib:hover{background:var(--vetro-h)}
.ib svg{width:20px;height:20px}
.hint{margin:12px 6px 0;color:var(--mut);font-size:.8rem}
#lista{display:grid;gap:16px;margin-top:18px}
.passo{--az:#3d6fff;display:grid;grid-template-columns:42px 1fr;gap:6px 12px;padding:18px;border-radius:24px;
  background:var(--card);border:1px solid var(--cb);box-shadow:var(--sh);break-inside:avoid}
.n{width:34px;height:34px;border-radius:50%;background:var(--az);color:#fff;display:grid;place-items:center;
  font:600 .85rem Consolas,"Cascadia Mono",monospace}
.ripresa{display:flex;align-items:center;gap:14px;padding:4px 6px;color:var(--mut);break-inside:avoid}
.ripresa::before,.ripresa::after{content:"";flex:1;height:1px;background:var(--cb)}
.ripresa p{margin:0;font-weight:600;font-size:.85rem;text-align:center;overflow-wrap:anywhere}
.d{margin:4px 0 0;font-weight:600;overflow-wrap:anywhere}
.m{margin:2px 0 12px;color:var(--mut);font-size:.78rem;overflow-wrap:anywhere}
[data-ed]{border-radius:8px;outline:none;cursor:text;padding:1px 6px;margin-left:-6px;margin-right:-6px;transition:background .12s}
[data-ed]:hover{background:var(--vetro)}
[data-ed]:focus{background:var(--vetro);box-shadow:0 0 0 2px var(--a1)}
[data-ed].cambiato{background:var(--sel)}
figure{margin:0;position:relative}
img.s{width:100%;height:auto;display:block;border-radius:14px;border:1px solid var(--cb);cursor:zoom-in}
.op{position:absolute;top:10px;right:10px;width:34px;height:34px;opacity:0;transition:opacity .12s;
  background:var(--campo);box-shadow:0 4px 14px rgba(0,0,0,.3)}
.op svg{width:17px;height:17px}
figure:hover .op{opacity:1}
@media (hover:none){.op{opacity:1}}
#sb{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);display:flex;gap:10px;align-items:center;
  padding:10px 12px 10px 20px;border-radius:999px;background:var(--campo);border:1px solid var(--cb);
  box-shadow:0 12px 36px rgba(0,0,0,.35);z-index:5;max-width:calc(100vw - 24px)}
#sb[hidden]{display:none}
#sbt{font-weight:600;font-size:.9rem}
.bt{border:0;border-radius:999px;padding:9px 18px;font:inherit;font-weight:600;font-size:.88rem;cursor:pointer;
  color:var(--fg);background:var(--vetro);white-space:nowrap}
.bt:hover{background:var(--vetro-h)}
.bt.p{color:#fff;background:linear-gradient(90deg,var(--a1),var(--a2))}
.bt.p:hover{filter:brightness(1.12);background:linear-gradient(90deg,var(--a1),var(--a2))}
.bt[hidden]{display:none}
#lb{position:fixed;inset:0;background:rgba(8,12,20,.94);display:flex;align-items:center;justify-content:center;overflow:auto;z-index:10}
#lb[hidden]{display:none}
#lb img{max-width:94vw;max-height:86vh;border-radius:12px;cursor:zoom-in}
#lb img.z{max-width:none;max-height:none;cursor:zoom-out;border-radius:0}
#lb button,#lb a{position:fixed;background:rgba(255,255,255,.12);color:#fff;border:0;border-radius:50%;width:44px;height:44px;
  font-size:22px;display:grid;place-items:center;cursor:pointer;text-decoration:none}
#lb button:hover,#lb a:hover{background:rgba(255,255,255,.24)}
#lbx{top:14px;right:14px}#lbo{top:14px;right:68px}
#lbp{left:14px;top:50%}#lbn{right:14px;top:50%}
#lbc{position:fixed;bottom:0;left:0;right:0;padding:14px 70px;color:#ddd;font-size:.85rem;text-align:center;background:linear-gradient(transparent,rgba(0,0,0,.7))}
@media (max-width:560px){.top{flex-wrap:wrap}.tools{margin-left:auto}.passo{grid-template-columns:1fr}}
@media print{
  :root[data-tema]{--fg:#000;--mut:#555;--card:#fff;--cb:#ccc;--sh:none}
  body{background:#fff}
  .tools,.op,#lb,#sb,.hint{display:none!important}
  .top,.passo{backdrop-filter:none;box-shadow:none}
  [data-ed]:hover,[data-ed].cambiato{background:none}
}

/* ---- pannello «Stampa e salva» ---- */
#stampa{position:fixed;inset:0;z-index:20;display:none;flex-direction:column;background:var(--campo);color:var(--fg)}
body.sp #stampa{display:flex}
@media screen{body.sp{overflow:hidden}}
.sp-testa{display:flex;align-items:center;gap:12px;padding:12px 18px;border-bottom:1px solid var(--cb)}
.sp-testa h2{margin:0;font-size:1.05rem;flex:1}
.sp-corpo{flex:1;min-height:0;display:flex}
.sp-lato{width:340px;flex:none;overflow:auto;padding:14px 18px 24px;border-right:1px solid var(--cb)}
.sp-lato h3{margin:20px 0 8px;font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;color:var(--mut)}
.sp-lato h3:first-child{margin-top:2px}
.riga{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:9px 0;font-size:.85rem}
.seg{display:inline-flex;flex-wrap:wrap;background:var(--vetro);border-radius:999px;padding:2px}
.seg button{border:0;background:none;color:var(--fg);font:inherit;font-size:.78rem;padding:5px 10px;border-radius:999px;cursor:pointer}
.seg button.on{background:linear-gradient(90deg,var(--a1),var(--a2));color:#fff}
.chk{display:flex;align-items:center;gap:8px;margin:8px 0;font-size:.85rem;cursor:pointer}
.chk input{accent-color:var(--a1);width:16px;height:16px;margin:0}
.lp{display:grid;gap:2px;max-height:280px;overflow:auto;margin-top:8px;border:1px solid var(--cb);border-radius:12px;padding:4px}
.lp label{display:grid;grid-template-columns:auto 52px 1fr;gap:8px;align-items:center;padding:4px 6px;border-radius:8px;cursor:pointer;font-size:.78rem}
.lp label:hover{background:var(--vetro)}
.lp img{width:52px;height:32px;object-fit:cover;border-radius:4px;background:var(--vetro)}
.lp span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sp-prev{flex:1;min-width:0;display:flex;flex-direction:column}
.sp-zoom{display:flex;align-items:center;gap:8px;padding:8px 18px;font-size:.8rem;color:var(--mut);border-bottom:1px solid var(--cb)}
.sp-zoom .bt{padding:4px 12px}
#zv{min-width:44px;text-align:center}
#pnv{flex:1;min-height:0;overflow:auto;padding:18px;background:rgba(128,128,128,.2)}
#fogli{width:max-content;margin:0 auto}
.sp-piede{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:12px 18px;border-top:1px solid var(--cb)}
.sp-piede .info{flex:1;min-width:200px;color:var(--mut);font-size:.8rem}
.bt:disabled{opacity:.45;cursor:default}
.foglio,.mis{font:13px/1.45 "Segoe UI",system-ui,sans-serif}
.foglio{position:relative;overflow:hidden;background:#fff;color:#111;margin:0 auto 16px;box-shadow:0 4px 22px rgba(0,0,0,.4);
  -webkit-print-color-adjust:exact;print-color-adjust:exact;break-after:page;page-break-after:always}
.foglio:last-child{break-after:auto;page-break-after:auto}
html[data-tema=scuro] .foglio.tema{background:#18212e;color:#f4f7fb}
html[data-tema=chiaro] .foglio.tema{background:#f5f7fb;color:#1b2740}
.mis{position:fixed;left:-99999px;top:0;visibility:hidden;pointer-events:none}
.fc,.mis{display:flex;flex-direction:column;gap:14px}
.fc>*,.mis>*{flex:none}
.ph{display:flow-root}
.ph h1{margin:0 0 2px;font-size:20px;line-height:1.25;overflow-wrap:anywhere}
.ph .sub{color:#666;font-size:11px;margin-bottom:6px}
.tema .ph .sub,.tema .pm,.tema .prip{color:#9aa9c2}
.pb{display:flex;flex-direction:column;gap:6px}
.pbh{display:flex;gap:10px;align-items:flex-start}
.pn{flex:none;width:26px;height:26px;border-radius:50%;background:var(--az,#3d6fff);color:#fff;display:grid;place-items:center;font:600 11px Consolas,"Cascadia Mono",monospace}
.pd{margin:2px 0 0;font-weight:600;overflow-wrap:anywhere}
.pm{margin:0;color:#666;font-size:11px;overflow-wrap:anywhere}
.pim img{display:block;max-width:100%;border:1px solid #c9c9c9;border-radius:6px}
.pb.fx .pim{flex:1;min-height:0;overflow:hidden}
.pb.fx .pim img{max-height:100%}
.prip{color:#666;font-size:11px;font-weight:600;border-top:1px solid #c9c9c9;padding-top:6px}
.pf{position:absolute;display:flex;justify-content:space-between;gap:12px;font-size:10px;color:#777}
.pft{max-width:70%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media (max-width:760px){
  .sp-corpo{flex-direction:column}
  .sp-lato{width:auto;max-height:46%;border-right:0;border-bottom:1px solid var(--cb)}
}
@media print{
  body.sp{background:#fff!important;min-height:0}
  body.sp .wrap,body.sp #sb,body.sp #lb,body.sp .mis{display:none!important}
  body.sp #stampa{position:static;display:block;background:none}
  body.sp .sp-testa,body.sp .sp-lato,body.sp .sp-zoom,body.sp .sp-piede{display:none!important}
  body.sp .sp-corpo,body.sp .sp-prev{display:block}
  body.sp #pnv{overflow:visible;padding:0;background:none}
  body.sp #fogli{zoom:1!important;width:auto;margin:0}
  body.sp .foglio{margin:0;box-shadow:none}
}
</style>
</head>
<body>
<div class="wrap">
<header class="top">
__LOGO__
<div class="tit"><h1 id="titolo"></h1><div id="sub"></div></div>
<div class="tools">
<button class="ib" id="btema" type="button"></button>
<button class="ib" id="bpdf" type="button" title="Stampa e salva (Ctrl+P)"></button>
</div>
</header>
<p class="hint">Clicca sul titolo o su una descrizione per modificarla: la copia si salva in una nuova cartella «_modificato».</p>
<div id="lista"></div>
</div>

<div id="sb" hidden>
<span id="sbt"></span>
<button class="bt" id="bann" type="button">Annulla</button>
<button class="bt p" id="bsal" type="button">Salva copia</button>
</div>

<div id="lb" hidden>
<button id="lbx" title="Chiudi (Esc)">✕</button>
<a id="lbo" target="_blank" title="Apri l'immagine da sola"></a>
<button id="lbp" title="Precedente (←)">‹</button>
<button id="lbn" title="Successivo (→)">›</button>
<img id="lbi" alt="">
<div id="lbc"></div>
</div>

<div id="stampa" role="dialog" aria-label="Stampa e salva">
<style id="pgstyle"></style>
<div class="sp-testa"><h2>Stampa e salva</h2><button class="bt" id="spx" type="button">Chiudi</button></div>
<div class="sp-corpo">
<div class="sp-lato">
<h3>Passi da includere</h3>
<div class="riga"><span id="spn"></span><span><button class="bt" id="spt" type="button">Tutti</button> <button class="bt" id="spz" type="button">Nessuno</button></span></div>
<div class="lp" id="lp"></div>
<h3>Pagina</h3>
<div class="riga">Formato<div class="seg" data-k="carta"><button type="button" data-v="a4">A4</button><button type="button" data-v="letter">Letter</button></div></div>
<div class="riga">Orientamento<div class="seg" data-k="orient"><button type="button" data-v="v">Verticale</button><button type="button" data-v="o">Orizzontale</button></div></div>
<div class="riga">Margini<div class="seg" data-k="margini"><button type="button" data-v="s">Stretti</button><button type="button" data-v="n">Normali</button><button type="button" data-v="l">Larghi</button></div></div>
<h3>Disposizione</h3>
<div class="riga">Passi per pagina<div class="seg" data-k="per"><button type="button" data-v="auto">Auto</button><button type="button" data-v="1">1</button><button type="button" data-v="2">2</button><button type="button" data-v="3">3</button><button type="button" data-v="4">4</button></div></div>
<div class="riga" id="rimm">Immagini<div class="seg" data-k="imm"><button type="button" data-v="g">Grande</button><button type="button" data-v="m">Media</button><button type="button" data-v="p">Piccola</button></div></div>
<h3>Contenuto</h3>
<label class="chk"><input type="checkbox" data-k="titolo">Titolo e data in alto</label>
<label class="chk"><input type="checkbox" data-k="descr">Descrizioni dei passi</label>
<label class="chk"><input type="checkbox" data-k="meta">Ora e finestra</label>
<label class="chk"><input type="checkbox" data-k="shot">Screenshot</label>
<label class="chk"><input type="checkbox" data-k="num">Numero di pagina</label>
<label class="chk"><input type="checkbox" data-k="bianco">Sfondo bianco (risparmia inchiostro)</label>
</div>
<div class="sp-prev">
<div class="sp-zoom">Anteprima
<button class="bt" id="zm" type="button" title="Riduci">−</button><span id="zv"></span><button class="bt" id="zp" type="button" title="Ingrandisci">+</button><button class="bt" id="za" type="button">Adatta</button></div>
<div id="pnv"><div id="fogli"></div></div>
</div>
</div>
<div class="sp-piede">
<span class="info"><span id="spi"></span><br>Per il PDF scegli «Salva come PDF» come stampante nella finestra che si apre.</span>
<button class="bt" id="spm" type="button" title="Elenco dei passi in testo Markdown (i link alle immagini sono relativi alla cartella del progetto)">Salva elenco (.md)</button>
<button class="bt p" id="sps" type="button">Stampa / Salva PDF…</button>
</div>
</div>
<div class="mis" id="misura"></div>

__DATI__
<script>
const D=JSON.parse(document.getElementById('dati').textContent);
const $=s=>document.querySelector(s);
const pad=n=>String(n).padStart(2,'0');
let cur=-1;
const base={titolo:D.titolo,testi:D.passi.map(p=>p.testo)};

/* ---- icone ---- */
const ICONE={
  sole:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  luna:'<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
  pdf:'<path d="M6 9V3h12v6"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8" rx="1"/>',
  apri:'<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>'
};
const svg=n=>'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'+ICONE[n]+'</svg>';

/* ---- tema ---- */
function iconaTema(){
  const scuro=document.documentElement.dataset.tema==='scuro';
  $('#btema').innerHTML=svg(scuro?'sole':'luna');
  $('#btema').title=scuro?'Tema chiaro':'Tema scuro';
}
try{const t=localStorage.getItem('tema');if(t==='scuro'||t==='chiaro')document.documentElement.dataset.tema=t;}catch(e){}
$('#btema').onclick=()=>{
  const t=document.documentElement.dataset.tema==='scuro'?'chiaro':'scuro';
  document.documentElement.dataset.tema=t;
  try{localStorage.setItem('tema',t);}catch(e){}
  iconaTema();
};
$('#bpdf').innerHTML=svg('pdf');
$('#bpdf').onclick=()=>apriStampa();
iconaTema();

/* ---- modifica cliccando sul testo ---- */
const az=p=>p.az||(p.tipo==='drag'?'drag':p.tipo==='tasto'?'tasto':'sinistro');
const reali=()=>D.passi.filter(p=>p.tipo!=='ripresa').length;
function edita(el,riferimento,leggi,scrivi){
  el.setAttribute('data-ed','');
  el.contentEditable='plaintext-only';
  if(el.contentEditable!=='plaintext-only')el.contentEditable='true';
  el.spellcheck=false;el.title='Clicca per modificare';
  let prima='';
  const stato=()=>el.classList.toggle('cambiato',leggi()!==riferimento());
  el.addEventListener('focus',()=>{prima=leggi();});
  el.addEventListener('input',()=>{scrivi(el.textContent);stato();aggiorna();});
  el.addEventListener('keydown',e=>{
    if(e.key==='Enter'){e.preventDefault();el.blur();}
    else if(e.key==='Escape'){e.preventDefault();scrivi(prima);el.textContent=prima;stato();aggiorna();el.blur();}
  });
  el.addEventListener('blur',()=>{
    let t=el.textContent.replace(/\s+/g,' ').trim();
    if(!t)t=riferimento();
    scrivi(t);el.textContent=t;stato();aggiorna();
  });
  stato();
}
function render(){
  const h=$('#titolo');h.textContent=D.titolo;
  edita(h,()=>base.titolo,()=>D.titolo,t=>{D.titolo=t;document.title=t;});
  document.title=D.titolo;
  let dt=D.creato;
  try{dt=new Date(D.creato).toLocaleString('it-IT',{dateStyle:'short',timeStyle:'short'});}catch(e){}
  $('#sub').textContent=reali()+' passi · '+dt+' · '+D.modalita;
  const L=$('#lista');L.textContent='';
  D.passi.forEach((p,i)=>{
    if(p.tipo==='ripresa'){
      const r=document.createElement('div');r.className='ripresa';
      const rt=document.createElement('p');rt.textContent=p.testo;
      edita(rt,()=>base.testi[i],()=>p.testo,x=>{p.testo=x;});
      r.append(rt);L.append(r);return;
    }
    const a=document.createElement('article');a.className='passo';a.dataset.az=az(p);
    const n=document.createElement('div');n.className='n';n.textContent=pad(p.n);
    const c=document.createElement('div');c.style.minWidth='0';
    const d=document.createElement('p');d.className='d';d.textContent=p.testo;
    edita(d,()=>base.testi[i],()=>p.testo,t=>{p.testo=t;});
    const m=document.createElement('p');m.className='m';m.textContent=p.ora+(p.finestra?' · '+p.finestra:'');
    const f=document.createElement('figure');
    const im=document.createElement('img');im.className='s';im.loading='lazy';im.src=p.img;im.alt='Passo '+p.n;
    im.addEventListener('click',()=>apriLb(i));
    const o=document.createElement('a');o.className='ib op';o.href=p.img;o.target='_blank';o.title="Apri l'immagine da sola";o.innerHTML=svg('apri');
    f.append(im,o);c.append(d,m,f);a.append(n,c);L.append(a);
  });
  if(!D.passi.length)L.textContent='Nessun passo registrato.';
}

/* ---- barra «Salva copia» ---- */
let nota='',occupato=false,timerNota=0;
function modifiche(){
  let n=D.passi.filter((p,i)=>p.testo!==base.testi[i]).length;
  if(D.titolo!==base.titolo)n++;
  return n;
}
function aggiorna(){
  const n=modifiche();
  const sb=$('#sb');
  if(!n&&!nota){sb.hidden=true;return;}
  sb.hidden=false;
  $('#sbt').textContent=nota||(n===1?'1 modifica':n+' modifiche');
  const mostra=n>0&&!occupato;
  $('#bann').hidden=!mostra;$('#bsal').hidden=!mostra;
}
function avviso(t,ms){
  nota=t;aggiorna();clearTimeout(timerNota);
  if(ms)timerNota=setTimeout(()=>{nota='';aggiorna();},ms);
}
$('#bann').onclick=()=>{
  D.titolo=base.titolo;D.passi.forEach((p,i)=>{p.testo=base.testi[i];});
  render();aggiorna();
};
window.addEventListener('beforeunload',e=>{if(modifiche()>0){e.preventDefault();e.returnValue='';}});

function nomeCartella(){
  try{
    if(location.protocol==='file:'){
      const p=decodeURIComponent(location.pathname).split('/').filter(Boolean);
      if(p.length>=2)return p[p.length-2];
    }
  }catch(e){}
  return D.progetto||'progetto';
}
async function esiste(dir,nome){
  try{await dir.getDirectoryHandle(nome);return true;}
  catch(e){if(e.name==='NotFoundError')return false;if(e.name==='TypeMismatchError')return true;throw e;}
}
async function testoDi(dir,nome){return await (await (await dir.getFileHandle(nome)).getFile()).text();}
async function scrivi(dir,nome,contenuto){
  const w=await (await dir.getFileHandle(nome,{create:true})).createWritable();
  await w.write(contenuto);await w.close();
}
async function salva(){
  if(occupato||!modifiche())return;
  if(!window.showDirectoryPicker){avviso('Per salvare la copia serve Microsoft Edge o Google Chrome.',9000);return;}
  let radice;
  try{radice=await window.showDirectoryPicker({id:'registratore-azioni',mode:'readwrite',startIn:'documents'});}
  catch(e){
    if(e.name==='AbortError')return;
    avviso(e.name==='SecurityError'
      ?'Il browser non permette quella cartella: scegli la cartella dei progetti (non Documenti o Desktop).'
      :'Non riesco ad aprire la cartella: '+e.message,9000);
    return;
  }
  occupato=true;aggiorna();
  let creata='';   /* se qualcosa va storto a metà, tolgo la copia incompleta: niente cartelle a metà in giro */
  try{
    const orig=nomeCartella();
    let src;
    try{src=await radice.getDirectoryHandle(orig);}
    catch(e){throw new Error('scegli la cartella che CONTIENE «'+orig+'», non il progetto stesso');}
    const radiceNome=orig.replace(/_modificato(_\d+)?$/,'')+'_modificato';
    let nome=radiceNome,k=2;
    while(await esiste(radice,nome))nome=radiceNome+'_'+(k++);
    const dest=await radice.getDirectoryHandle(nome,{create:true});
    creata=nome;

    const sorg=await src.getDirectoryHandle('immagini');
    const dimm=await dest.getDirectoryHandle('immagini',{create:true});
    const file=[];
    for await(const [n,h] of sorg.entries())if(h.kind==='file')file.push([n,h]);
    let fatti=0;
    for(const [n,h] of file){
      avviso('Copio le immagini '+(++fatti)+' / '+file.length+'…');
      const w=await (await dimm.getFileHandle(n,{create:true})).createWritable();
      await (await h.getFile()).stream().pipeTo(w);
    }

    const passi=D.passi.map((p,i)=>{
      const q=Object.assign({},p);
      if(p.testo!==base.testi[i]&&!q.testo_originale)q.testo_originale=base.testi[i];
      return q;
    });
    const dati={progetto:nome,titolo:D.titolo,creato:D.creato,modalita:D.modalita,passi:passi};
    await scrivi(dest,'passi.json',JSON.stringify(dati,null,2));

    const LT='<';  /* spezzo le etichette HTML: dentro uno script il browser le leggerebbe come vere */
    const blocco=LT+'!--DATI-->'+LT+'script id="dati" type="application/json">'+
      JSON.stringify({progetto:nome,titolo:D.titolo,creato:D.creato,modalita:D.modalita,passi:passi}).replace(/</g,'\\u003c')+
      LT+'/script>'+LT+'!--/DATI-->';
    const esc=s=>s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    let html=await testoDi(src,'index.html');
    html=html.replace(new RegExp(LT+'!--DATI-->[\\s\\S]*?'+LT+'!--/DATI-->'),()=>blocco)
             .replace(/<title>[\s\S]*?<\/title>/,()=>'<title>'+esc(D.titolo)+'</title>');
    await scrivi(dest,'index.html',html);

    creata='';
    base.titolo=D.titolo;base.testi=D.passi.map(p=>p.testo);
    D.passi.forEach((p,i)=>{if(passi[i].testo_originale)p.testo_originale=passi[i].testo_originale;});
    occupato=false;
    render();
    avviso('✓ Creata la cartella «'+nome+'». L\'originale non è cambiato.',10000);
  }catch(e){
    occupato=false;
    if(creata){try{await radice.removeEntry(creata,{recursive:true});}catch(_){}}
    avviso('Non sono riuscito a salvare: '+e.message,12000);
  }
}
$('#bsal').onclick=salva;

/* ---- lettore immagini ---- */
function apriLb(i){
  cur=i;const p=D.passi[i];
  $('#lbi').className='';$('#lbi').src=p.img;$('#lbo').href=p.img;
  $('#lbc').textContent=pad(p.n)+' / '+pad(reali())+'  ·  '+p.testo;
  $('#lb').hidden=false;document.body.style.overflow='hidden';
}
function chiudiLb(){$('#lb').hidden=true;$('#lbi').src='';document.body.style.overflow='';cur=-1;}
function vai(d){let n=cur+d;while(n>=0&&n<D.passi.length&&D.passi[n].tipo==='ripresa')n+=d;if(n>=0&&n<D.passi.length)apriLb(n);}
$('#lbo').innerHTML=svg('apri');$('#lbo').firstChild.style.cssText='width:20px;height:20px';
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
  }
});

/* ---- stampa e salva: anteprima impaginata, scelta dei passi, opzioni ---- */
const CARTE={a4:[793,1122],letter:[816,1056]};   /* px a 96 dpi, arrotondati per difetto: niente pagina bianca in più */
const MARG={s:24,n:48,l:72};
const S0={carta:'a4',orient:'v',margini:'n',per:'auto',imm:'g',titolo:true,descr:true,meta:true,shot:true,num:true,bianco:true};
const S=Object.assign({},S0);
try{const j=JSON.parse(localStorage.getItem('stampa')||'{}');for(const k in S0)if(typeof j[k]===typeof S0[k])S[k]=j[k];}catch(e){}
const SEL=new Set();
let selInit=false,zm=null,tok=0,tim=0,quanti=0;
const indiciReali=()=>D.passi.map((p,i)=>i).filter(i=>D.passi[i].tipo!=='ripresa');
const salvaS=()=>{try{localStorage.setItem('stampa',JSON.stringify(S));}catch(e){}};
const aperta=()=>document.body.classList.contains('sp');

function sincro(){
  document.querySelectorAll('#stampa .seg').forEach(g=>g.querySelectorAll('button').forEach(b=>b.classList.toggle('on',String(S[g.dataset.k])===b.dataset.v)));
  document.querySelectorAll('#stampa input[data-k]').forEach(c=>{c.checked=!!S[c.dataset.k];});
  $('#rimm').style.display=S.per==='auto'?'':'none';
}
function popolaLista(){
  const L=$('#lp');L.textContent='';
  D.passi.forEach((p,i)=>{
    if(p.tipo==='ripresa')return;
    const l=document.createElement('label');
    const c=document.createElement('input');c.type='checkbox';c.checked=SEL.has(i);c.dataset.i=i;
    c.onchange=()=>{c.checked?SEL.add(i):SEL.delete(i);pianifica();};
    const im=document.createElement('img');im.loading='lazy';im.src=p.img;im.alt='';
    const s=document.createElement('span');s.textContent=pad(p.n)+'  '+p.testo;s.title=p.testo;
    l.append(c,im,s);L.append(l);
  });
}
function scegliTutti(si){
  SEL.clear();if(si)indiciReali().forEach(i=>SEL.add(i));
  document.querySelectorAll('#lp input').forEach(c=>{c.checked=si;});
  pianifica();
}
function apriStampa(){
  if(!selInit){indiciReali().forEach(i=>SEL.add(i));selInit=true;}
  popolaLista();sincro();
  document.body.classList.add('sp');
  zm=null;pianifica(0);
}
function chiudiStampa(){document.body.classList.remove('sp');tok++;$('#fogli').textContent='';$('#misura').textContent='';}
function pianifica(ms){clearTimeout(tim);$('#sps').disabled=true;tim=setTimeout(costruisci,ms===undefined?200:ms);}

function applicaZoom(){
  const F=$('#fogli'),pg=F.firstElementChild;if(!pg)return;
  const adatta=Math.min(1,Math.max(.2,($('#pnv').clientWidth-36)/parseFloat(pg.style.width)));
  const z=zm||adatta;F.style.zoom=z;$('#zv').textContent=Math.round(z*100)+'%';
}
function cambiaZoom(f){const F=$('#fogli');zm=Math.min(2.5,Math.max(.2,(parseFloat(F.style.zoom)||1)*f));applicaZoom();}

async function costruisci(){
  const mio=++tok;
  let [W,H]=CARTE[S.carta];if(S.orient==='o')[W,H]=[H,W];
  const m=MARG[S.margini],cw=W-2*m,GAP=14,ch=H-2*m-(S.num?22:0);
  $('#pgstyle').textContent='@page{size:'+(S.carta==='a4'?'A4':'letter')+(S.orient==='o'?' landscape':'')+';margin:0}';

  /* elementi: un blocco per passo (con la riga «ripresa» in testa, se c'è) */
  const mis=$('#misura');mis.textContent='';mis.style.width=cw+'px';
  const fisso=S.per!=='auto';
  const els=[];let rip=null;
  D.passi.forEach((p,i)=>{
    if(p.tipo==='ripresa'){rip=p.testo;return;}
    if(!SEL.has(i))return;
    const d=document.createElement('div');d.className='pb';d.dataset.az=az(p);
    if(rip){const r=document.createElement('div');r.className='prip';r.textContent=rip;d.append(r);rip=null;}
    const h=document.createElement('div');h.className='pbh';
    const n=document.createElement('div');n.className='pn';n.textContent=pad(p.n);
    const t=document.createElement('div');t.style.minWidth='0';
    if(S.descr){const x=document.createElement('p');x.className='pd';x.textContent=p.testo;t.append(x);}
    if(S.meta){const x=document.createElement('p');x.className='pm';x.textContent=p.ora+(p.finestra?' · '+p.finestra:'');t.append(x);}
    h.append(n,t);d.append(h);
    if(S.shot){
      const w=document.createElement('div');w.className='pim';
      const im=document.createElement('img');im.src=p.img;im.alt='';
      if(!fisso)im.style.width={g:'100%',m:'75%',p:'50%'}[S.imm];
      w.append(im);d.append(w);
    }
    els.push(d);
  });
  quanti=els.length;
  $('#spn').textContent=quanti+' di '+indiciReali().length+' selezionati';

  const head=document.createElement('div');head.className='ph';
  const h1=document.createElement('h1');h1.textContent=D.titolo;
  const sub=document.createElement('div');sub.className='sub';
  let dt=D.creato;try{dt=new Date(D.creato).toLocaleString('it-IT',{dateStyle:'long',timeStyle:'short'});}catch(e){}
  sub.textContent=reali()+' passi · '+dt;
  head.append(h1,sub);

  if(S.titolo)mis.append(head);
  els.forEach(e=>mis.append(e));
  if(!fisso)await Promise.all([...mis.querySelectorAll('img')].map(i=>i.decode?i.decode().catch(()=>{}):0));
  if(mio!==tok)return;

  /* impaginazione */
  const pagine=[];let cur=null,usato=0;
  const nuova=()=>{cur=[];pagine.push(cur);usato=0;};
  nuova();
  if(S.titolo){cur.push(head);usato=head.offsetHeight+GAP;}
  if(!fisso){
    els.forEach(e=>{
      let h=e.offsetHeight;
      if(h>ch){  /* più alto di una pagina: rimpicciolisco l'immagine */
        const im=e.querySelector('img');
        if(im){const extra=h-im.offsetHeight;im.style.width='auto';im.style.maxHeight=Math.max(60,ch-extra-4)+'px';h=e.offsetHeight;}
      }
      const vuota=cur.length===0||(cur.length===1&&cur[0]===head);
      if(!vuota&&usato+h>ch)nuova();
      cur.push(e);usato+=h+GAP;
    });
  }else{
    const N=+S.per;let k=0,primo=true;
    while(k<els.length){
      if(!primo)nuova();
      const av=ch-(primo&&S.titolo?head.offsetHeight+GAP:0);
      const sh=Math.max(60,Math.floor((av-GAP*(N-1))/N));
      for(let j=0;j<N&&k<els.length;j++,k++){els[k].classList.add('fx');els[k].style.height=sh+'px';cur.push(els[k]);}
      primo=false;
    }
  }

  const F=$('#fogli');F.textContent='';
  if(!quanti&&!S.titolo){F.textContent='';mis.textContent='';$('#spi').textContent='Nessun passo selezionato.';$('#sps').disabled=true;return;}
  const tot=pagine.length;
  pagine.forEach((blocchi,pi)=>{
    const pg=document.createElement('div');pg.className='foglio'+(S.bianco?'':' tema');
    pg.style.cssText='width:'+W+'px;height:'+H+'px;padding:'+m+'px';
    const fc=document.createElement('div');fc.className='fc';
    blocchi.forEach(b=>fc.append(b));pg.append(fc);
    if(S.num){
      const f=document.createElement('div');f.className='pf';
      f.style.cssText='left:'+m+'px;right:'+m+'px;bottom:'+Math.max(8,Math.round(m/2.5))+'px';
      const a=document.createElement('span');a.className='pft';a.textContent=D.titolo;
      const b=document.createElement('span');b.textContent='Pagina '+(pi+1)+' di '+tot;
      f.append(a,b);pg.append(f);
    }
    F.append(pg);
  });
  mis.textContent='';
  applicaZoom();
  $('#spi').textContent=quanti+(quanti===1?' passo':' passi')+' · '+tot+(tot===1?' pagina':' pagine');
  $('#sps').disabled=!quanti;
}

function esportaMd(){
  const r=['# '+D.titolo,''];let rip=null;
  D.passi.forEach((p,i)=>{
    if(p.tipo==='ripresa'){rip=p.testo;return;}
    if(!SEL.has(i))return;
    if(rip){r.push('_'+rip+'_','');rip=null;}
    r.push('## '+pad(p.n)+(S.descr?'. '+p.testo.replace(/\s+/g,' '):''));
    if(S.meta)r.push(p.ora+(p.finestra?' · '+p.finestra:''));
    if(S.shot)r.push('','![Passo '+p.n+']('+p.img+')');
    r.push('');
  });
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([r.join('\n')],{type:'text/markdown;charset=utf-8'}));
  a.download=(D.titolo.replace(/[<>:"\/\\|?*\x00-\x1f]/g,'_').trim()||'progetto')+'.md';
  document.body.append(a);a.click();a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href),5000);   /* niente resti in memoria */
}

document.querySelectorAll('#stampa .seg button').forEach(b=>b.onclick=()=>{S[b.parentNode.dataset.k]=b.dataset.v;salvaS();sincro();pianifica();});
document.querySelectorAll('#stampa input[data-k]').forEach(c=>c.onchange=()=>{S[c.dataset.k]=c.checked;salvaS();pianifica();});
$('#spt').onclick=()=>scegliTutti(true);
$('#spz').onclick=()=>scegliTutti(false);
$('#spx').onclick=chiudiStampa;
$('#sps').onclick=()=>{if(quanti)print();};
$('#spm').onclick=()=>{if(quanti)esportaMd();};
$('#zm').onclick=()=>cambiaZoom(1/1.2);
$('#zp').onclick=()=>cambiaZoom(1.2);
$('#za').onclick=()=>{zm=null;applicaZoom();};
window.addEventListener('resize',()=>{if(aperta())applicaZoom();});
document.addEventListener('keydown',e=>{
  if((e.ctrlKey||e.metaKey)&&!e.shiftKey&&!e.altKey&&e.key.toLowerCase()==='p'&&!aperta()){e.preventDefault();apriStampa();return;}
  if(e.key==='Escape'&&aperta()&&cur<0)chiudiStampa();
});

const _lg=document.querySelector('img.logo'),_fv=document.getElementById('fav');
if(_lg&&_fv)_fv.href=_lg.src;  /* stessa immagine del logo: niente copia in più nel file */
render();
</script>
</body>
</html>
"""


def genera_html(cartella: Path, titolo: str, steps: list, nome_modo: str, creato: str) -> Path:
    dati = {"progetto": Path(cartella).name, "titolo": titolo, "creato": creato,
            "modalita": nome_modo, "passi": steps}
    json_dati = json.dumps(dati, ensure_ascii=False).replace("<", "\\u003c")
    blocco = f'<!--DATI--><script id="dati" type="application/json">{json_dati}</script><!--/DATI-->'
    logo = logo_base64()
    favicon = '<link id="fav" rel="icon" href="data:,">' if logo else ""
    img_logo = f'<img class="logo" alt="" src="data:image/png;base64,{logo}">' if logo else ""
    tema = TEMA_HTML if TEMA_HTML in ("scuro", "chiaro") else "scuro"
    colori_css = "\n".join(
        f".passo[data-az={k}],.pb[data-az={k}]{{--az:#{r:02x}{g:02x}{b:02x}}}"
        for k, (_nome, (r, g, b)) in AZIONI.items())
    pagina = (HTML_TEMPLATE
              .replace("__TEMA__", tema)
              .replace("__TITOLO__", html.escape(titolo))
              .replace("__FAVICON__", favicon)
              .replace("__LOGO__", img_logo)
              .replace("__COLORI_CSS__", colori_css)
              .replace("__DATI__", blocco))
    out = Path(cartella) / "index.html"
    scrivi_testo_atomico(out, pagina)
    return out


# --------------------------------------------------------------------------
# Guida e report per le segnalazioni
# --------------------------------------------------------------------------
GUIDA = f"""COME SI USA
1. Scrivi il nome del progetto e premi «Avvia registrazione». I progetti vengono salvati nella cartella indicata in Impostazioni (la puoi cambiare quando vuoi).
2. Lavora normalmente. A ogni clic, trascinamento o azione da tastiera viene salvato uno screenshot con un riquadro colorato. La rotellina del mouse non conta.
3. Quando scrivi, il testo viene raggruppato: un passo per riga (per esempio «show ip route» + Invio), non uno per lettera.
4. F10 = pausa / riprendi.  F9 = fine. Puoi usare anche i due pulsanti rotondi della barretta in alto a destra (pausa e fine); si sposta trascinandola. Il pallino è rosso mentre registri, arancione in pausa.
5. Con «Fine» la registrazione si ferma ma non è ancora salvata: scrivi il nome del progetto e premi «Salva e apri» (si apre la pagina HTML), oppure «Riprendi registrazione» per continuare da dove eri, oppure «Annulla registrazione» (chiede conferma). Anche chiudere la barretta (Alt+F4) porta a questa schermata.

I COLORI DEI RIQUADRI
Ogni tipo di azione ha il suo colore negli screenshot (e sul numero del passo nella pagina HTML):
• Rosso = clic sinistro
• Blu = clic destro
• Verde = clic centrale
• Viola = doppio clic (due riquadri, uno dentro l'altro)
• Arancione = trascinamento (dal quadratino piccolo al riquadro grande)
• Verde acqua = tastiera (colore del numero del passo; lo screenshot resta pulito, senza cornice)
Se subito dopo un clic scorri con la rotellina, il riquadro non viene disegnato: la pagina si è spostata e finirebbe nel punto sbagliato.

QUALITÀ DEGLI SCREENSHOT
Minima, Leggera, Normale, Nitida: più sono leggere, meno spazio occupano. «Normale» va bene quasi sempre. Le modalità si possono cambiare in %APPDATA%\\RegistratoreAzioni\\config.json, dove si trovano anche i tasti (tasto_pausa, tasto_fine) e la pausa che chiude un passo di testo (pausa_digitazione_sec).

ANNULLARE UNA REGISTRAZIONE
Dalla schermata di fine premi «Annulla registrazione»: il programma chiede conferma. Non cancella niente subito: la cartella va in «{CARTELLA_BACKUP}», dentro la cartella dei progetti, e la puoi recuperare riportandola fuori. Se ne tengono al massimo {BACKUP_MASSIMO} di base (il numero si cambia in Impostazioni): quando sono di più, vengono eliminate le più vecchie. Puoi svuotare la cartella a mano quando vuoi. Se c'è un errore mentre salvi, il programma fa una copia di sicurezza nella stessa cartella.

RIPRENDERE UN PROGETTO
Da «Apri un progetto» scegli il progetto e premi «Riprendi»: la registrazione riparte nella stessa cartella, anche il giorno dopo o a progetto finito, e i nuovi passi si aggiungono in fondo. Nella pagina HTML compare una riga con il giorno e l'ora in cui hai ripreso. Se annulli subito, il progetto resta com'era.

IMPOSTAZIONI
Cartella dei progetti, tema scuro o chiaro (vale anche per le pagine HTML), se nascondere la barra del registratore dagli screenshot e quanti backup tenere.

MODIFICARE UN PROGETTO
Si fa dalla pagina HTML. Aprila con «Apri un progetto» (o con doppio clic su index.html), poi clicca sul titolo o su una descrizione e scrivi: in basso compare «Salva copia». Il browser chiede una volta di scegliere la cartella che CONTIENE il progetto (la prima volta soltanto: poi se la ricorda) e di consentire le modifiche. Viene creata «nomeprogetto_modificato» accanto all'originale, con immagini, pagina e dati; l'originale non viene toccato. Serve Microsoft Edge o Google Chrome.

LA PAGINA HTML
• Clic su un'immagine: si apre nel lettore (frecce ← → per scorrere, Esc per chiudere, clic per ingrandire). La freccetta in alto a destra la apre da sola in una nuova scheda.
• Per modificare: clic sul titolo o su una descrizione, Invio conferma, Esc annulla quel testo; il pulsante «Annulla» in basso scarta tutte le modifiche non salvate.
• Sole/luna cambia tema (la scelta resta nel browser).
• L'icona della stampante (o Ctrl+P) apre «Stampa e salva».

STAMPA E SALVA IN PDF
Si apre un pannello con l'anteprima delle pagine, che si aggiorna a ogni scelta:
• Passi da includere: togli la spunta a quelli che non vuoi (Tutti / Nessuno).
• Pagina: A4 o Letter, verticale o orizzontale, margini stretti, normali o larghi.
• Disposizione: quanti passi per pagina (Auto, 1, 2, 3 o 4) e la dimensione delle immagini.
• Contenuto: titolo e data, descrizioni, ora e finestra, screenshot, numero di pagina, sfondo bianco (risparmia inchiostro) oppure i colori della pagina.
• «Stampa / Salva PDF…» apre la finestra di stampa vera e propria: per ottenere un PDF scegli «Salva come PDF» (o «Microsoft Print to PDF») al posto della stampante. Il file PDF prende il nome del titolo.
• «Salva elenco (.md)» scarica l'elenco dei passi scelti in testo (Markdown); i link alle immagini sono relativi, quindi salvalo dentro la cartella del progetto.
Le scelte restano memorizzate nel browser. Per PowerPoint o Word: salva prima il PDF e aprilo con il programma che vuoi.

COSA VIENE CREATO
Nella cartella del progetto: index.html (la pagina), passi.json (i dati) e immagini/ (gli screenshot). Una copia modificata contiene le stesse cose; in passi.json le descrizioni cambiate conservano anche il testo originale. Non viene mai sovrascritto nulla: se un nome esiste già, il programma aggiunge _2, _3…
In %APPDATA%\\RegistratoreAzioni ci sono solo config.json e log.txt (il log è limitato a poche centinaia di KB).

NIENTE FILE IN GIRO
Il programma non lascia resti che crescono nel tempo: i file temporanei di una chiusura anomala vengono tolti al prossimo avvio (compresa la cartella _MEI dell'exe dentro Temp, se più vecchia di un giorno), i backup dei progetti annullati hanno un limite, i vecchi report di «Segnala un problema» sul Desktop sono eliminati dopo 14 giorni e una «Salva copia» interrotta dal browser non lascia cartelle a metà.

COSE DA SAPERE
• Tutto quello che scrivi e vedi finisce nel progetto, password comprese: metti in pausa quando entri in account personali.
• Le scorciatoie (Ctrl+S, Alt+Tab, Win+D…) sono un passo ciascuna; un tasto tenuto premuto conta una volta sola.
• Se in config.json metti registra_testo_digitato a false, al posto delle lettere viene salvato solo un pallino (utile per le password).
• Il programma si apre una volta sola: se lo riavvii, riporta in primo piano quello già aperto.
• Un programma aperto come amministratore potrebbe non essere registrato.
• Viene catturato solo il monitor dove si trova il mouse.
• La barretta del registratore viene nascosta dagli screenshot; se Windows non lo permette, il programma la copre con un riquadro scuro. Si può disattivare in Impostazioni.

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
    for vecchio in cartella.glob("report_registratore_*.txt"):  # i report di più di 14 giorni fa non servono più
        try:
            if time.time() - vecchio.stat().st_mtime > 14 * GIORNO:
                vecchio.unlink()
        except OSError:
            pass
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
    a parte la scelta della cartella dei progetti). Durante la registrazione la finestra
    diventa una barretta senza cornice, piccola e trascinabile."""

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
        self._senza_bordo = False
        self._righe_msg = 1
        self._hover = False
        self._ico_stato = None
        self._tick = 0
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

    def _modo_finestra(self, senza_bordo: bool):
        """Passa dalla finestra normale alla barretta senza cornice (e viceversa)."""
        if senza_bordo == self._senza_bordo:
            return
        r = self.root
        try:
            r.withdraw()
            r.overrideredirect(senza_bordo)
            r.update_idletasks()
            r.deiconify()
            if not senza_bordo:
                r.focus_force()
        except tk.TclError:
            pass
        self._senza_bordo = senza_bordo

    def _scena(self, nome, w, h, pos=None, senza_bordo=False):
        """Svuota la finestra e prepara lo sfondo nuovo di w x h."""
        self.scena = nome
        self._hover = False
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
        self.cv.config(width=W, height=H, bg=self.c["grad"][0], cursor="")
        self.base = img_sfondo(W, H, self.c)
        self.w, self.h = w, h
        self._modo_finestra(senza_bordo)
        self._posiziona(W, H, pos)
        self.root.update_idletasks()
        hw = hwnd_finestra(self.root)
        if senza_bordo:
            arrotonda_finestra(hw, W, H, H)
        else:
            togli_arrotondamento(hw)
            colora_titolo(hw, _rgb(self.c["grad"][0]), _rgb(self.c["fg"]), self.tema == "scuro")

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

    def _campo_bg(self, x, y, w, h):
        c = self.c
        self._rr(x, y, x + w, y + h, 12, _rgb(c["campo"]) + (255,), c["campo_bordo"])

    def _sfondo_pronto(self, tag=None):
        foto = self._ImageTk.PhotoImage(self.base)
        self._refs.append(foto)
        self.cv.create_image(0, 0, image=foto, anchor="nw", tags=(tag,) if tag else ())

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
        """Logo arrotondato + titolo, in alto."""
        logo = self._foto(icona_pil(self._px(40)))
        self.cv.create_image(self._px(34), self._px(26), image=logo, anchor="nw")
        self._testo(84, 37 if sotto else 46, titolo, FONT_T, anchor="w")
        if sotto:
            self._testo(84, 58, sotto, FONT_S, self.c["mut"], anchor="w")

    def _msg(self, testo, ok=True):
        """Messaggio nella finestra (al posto dei popup)."""
        if self.scena == "avvio":
            servono = max(1, -(-len(testo) // 50))
            if servono > self._righe_msg:  # il messaggio non ci sta: ricostruisco la schermata piu' alta
                self._scena_avvio(msg=testo, ok=ok)
                return
        if getattr(self, "t_msg", None) and self.scena in ("avvio", "progetti"):
            self.cv.itemconfig(self.t_msg, text=testo, fill=self.c["ok"] if ok else self.c["err"])

    # ---- schermata iniziale ----
    def _scena_avvio(self, pos=None, msg=None, ok=True):
        c = self.c
        righe = max(1, -(-len(msg) // 50)) if msg else 1
        self._righe_msg = righe
        self._scena("avvio", 380, 446 + 14 * righe, pos)
        self.root.title(APP)
        self.root.attributes("-topmost", False)
        nascondi_da_cattura(hwnd_finestra(self.root), False)  # la finestra normale puo' essere catturata

        self._campo_bg(34, 114, 312, 42)
        self._sfondo_pronto()

        self._intestazione("Registratore azioni", "Un passo, uno screenshot")
        self._etichetta(34, 94, "Nome del progetto")
        e = self._entry(34, 114, 312, 42, self.var_nome)
        e.focus_set()

        self._etichetta(34, 174, "Qualità degli screenshot")
        corrente = self.cfg["modalita_corrente"]
        self.var_modo = corrente if corrente in self.cfg["modalita"] else "Normale"
        self.t_desc = self._testo(34, 236, "", FONT_S, c["mut"])
        self._sel_pill = self._pillole(34, 196, list(self.cfg["modalita"]), self.var_modo, self._sel_modo)
        self._sel_modo(self.var_modo, salva=False)

        self._bottone(34, 264, 312, 48, "Avvia registrazione", self._avvia, primario=True)
        pausa = str(self.cfg.get("tasto_pausa", "f10")).upper()
        fine = str(self.cfg.get("tasto_fine", "f9")).upper()
        self._bottone(34, 320, 312, 36, "Apri un progetto", self._scena_progetti, font=FONT)
        self._testo(190, 368, f"{pausa} pausa / riprendi   ·   {fine} fine e salva", FONT_S, c["mut"], anchor="n")

        y_msg = 390
        self.t_msg = self._testo(34, y_msg, "", FONT_S, c["ok"], width=312)
        y_linea = y_msg + 14 * righe + 10
        self.cv.create_line(self._px(34), self._px(y_linea), self._px(346), self._px(y_linea), fill=c["linea"])
        x = 34
        for testo, cmd in (("Impostazioni", self._scena_impostazioni), ("Guida", self._scena_guida),
                           ("Segnala un problema", self._segnala)):
            self._link(x, y_linea + 12, testo, cmd)
            x += tkfont.Font(font=FONT_S).measure(testo) / self.k + 18
        self._testo(346, y_linea + 12, f"v{VERSION}", FONT_S, c["mut"], anchor="ne")
        if msg:
            self._msg(msg, ok)
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
        self._scena("impostazioni", 380, 520)
        self.root.title(f"Impostazioni · {APP}")
        self._sfondo_pronto()

        self._link(34, 24, "‹  Indietro", self._scena_avvio, font=FONT)
        self._testo(34, 58, "Impostazioni", FONT_T)

        self._etichetta(34, 108, "Cartella dei progetti")
        self.t_root = self._testo(34, 130, self.cfg.get("cartella_radice", ""), FONT_S, width=312)
        self._bottone(34, 172, 110, 32, "Cambia…", self._scegli_root, font=FONT)

        self._etichetta(34, 230, "Tema")
        self._pillole(34, 252, ["Scuro", "Chiaro"], self.tema.capitalize(), self._imposta_tema, w=96)

        self._etichetta(34, 304, "Nascondi la barra negli screenshot")
        self._pillole(34, 326, ["Sì", "No"], "Sì" if self.cfg.get("nascondi_barra", True) else "No",
                      self._imposta_barra, w=96)
        self._testo(34, 366, "Funziona su Windows 10 (2004 o successivi) e Windows 11.", FONT_S, c["mut"], width=312)

        massimo = str(int(self.cfg.get("backup_massimo", BACKUP_MASSIMO)))
        opzioni = ["5", "10", "20", "50"]
        self._etichetta(34, 412, "Backup cancellati: quanti tenerne")
        self._pillole(34, 434, opzioni, massimo if massimo in opzioni else "10", self._imposta_backup, w=72)
        self._testo(34, 474, f"Le registrazioni annullate vanno nella cartella «{CARTELLA_BACKUP}»; "
                             "oltre questo numero si elimina la più vecchia.", FONT_S, c["mut"], width=312)
        self.root.bind("<Escape>", lambda _e: self._scena_avvio())

    def _imposta_tema(self, nome):
        global TEMA_HTML
        self.tema = nome.lower()
        TEMA_HTML = self.tema
        self.cfg["tema"] = self.tema
        salva_config(self.cfg)
        self._scena_impostazioni()

    def _imposta_backup(self, nome):
        self.cfg["backup_massimo"] = int(nome)
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
        self._scena("guida", 540, 590)
        self.root.title(f"Guida · {APP}")
        self._campo_bg(30, 70, 480, 432)
        self._sfondo_pronto()

        self._link(34, 24, "‹  Indietro", self._scena_avvio, font=FONT)
        self._testo(506, 24, "Guida", FONT_T, anchor="ne")

        t = tk.Text(self.cv, wrap="word", font=FONT, bg=c["campo"], fg=c["fg"], relief="flat", bd=0,
                    padx=14, pady=10, spacing1=2, spacing3=4, highlightthickness=0, insertbackground=c["campo"])
        sb = tk.Scrollbar(self.cv, command=t.yview)
        t.config(yscrollcommand=sb.set)
        self.cv.create_window(self._px(34), self._px(74), window=t, anchor="nw", width=self._px(456), height=self._px(424))
        self.cv.create_window(self._px(490), self._px(74), window=sb, anchor="nw", width=self._px(16), height=self._px(424))
        self._widgets += [t, sb]
        t.tag_configure("titolo", font=FONT_B, foreground=c["acc1"], spacing1=12)
        for riga in GUIDA.strip().splitlines():
            if riga and riga == riga.upper() and not riga.startswith(("•", "F10")):
                t.insert("end", riga + "\n", "titolo")
            else:
                t.insert("end", riga + "\n")
        t.config(state="disabled")
        self._bottone(34, 520, 472, 44, "Ho capito", self._scena_avvio, primario=True)
        self.root.bind("<Escape>", lambda _e: self._scena_avvio())

    # ---- apri un progetto (la modifica si fa dalla pagina HTML) ----
    def _scena_progetti(self):
        c = self.c
        self._scena("progetti", 380, 560)
        self.root.title(f"Apri un progetto · {APP}")
        self._campo_bg(34, 126, 312, 190)
        self._sfondo_pronto()

        self._link(34, 24, "‹  Indietro", self._scena_avvio, font=FONT)
        self._testo(34, 58, "Apri un progetto", FONT_T)
        self._etichetta(34, 104, "Scegli il progetto (i più recenti in alto)")

        self._progetti = elenca_progetti(Path(self.cfg.get("cartella_radice", "")))
        lb = tk.Listbox(self.cv, font=FONT, bg=c["campo"], fg=c["fg"], relief="flat", bd=0, highlightthickness=0,
                        selectbackground=c["acc2"], selectforeground="#ffffff", activestyle="none",
                        exportselection=False)
        sb = tk.Scrollbar(self.cv, command=lb.yview)
        lb.config(yscrollcommand=sb.set)
        self.cv.create_window(self._px(40), self._px(132), window=lb, anchor="nw", width=self._px(280), height=self._px(178))
        self.cv.create_window(self._px(322), self._px(132), window=sb, anchor="nw", width=self._px(16), height=self._px(178))
        self._widgets += [lb, sb]
        for d in self._progetti:
            lb.insert("end", d.name)
        if self._progetti:
            lb.selection_set(0)
            lb.bind("<Double-Button-1>", lambda _e: self._apri_selezionato())
        else:
            self._testo(190, 220, "Nessun progetto in questa cartella.\nUsa «Altra cartella…»", FONT_S,
                        c["mut"], anchor="center", justify="center")
        self.lb = lb
        self._bottone(34, 328, 140, 32, "Altra cartella…", self._altra_cartella, font=FONT)
        self._bottone(34, 372, 153, 44, "Apri", self._apri_selezionato, primario=True)
        self._bottone(193, 372, 153, 44, "Riprendi", self._riprendi_selezionato)
        self._testo(34, 428, "Apri: guarda la pagina; clicca sul titolo o su una descrizione per modificarla "
                             "(la copia si salva come «nome_modificato»).\n"
                             "Riprendi: continua a registrare nello stesso progetto, anche un altro giorno; "
                             "nella pagina compare una riga con la data.", FONT_S, c["mut"], width=312)
        self.t_msg = self._testo(34, 526, "", FONT_S, c["err"], width=312)
        self.root.bind("<Return>", lambda _e: self._apri_selezionato())
        self.root.bind("<Escape>", lambda _e: self._scena_avvio())

    def _altra_cartella(self):
        d = filedialog.askdirectory(title="Scegli la cartella del progetto", parent=self.root,
                                    mustexist=True, initialdir=self.cfg.get("cartella_radice") or str(Path.home()))
        if not d:
            return
        d = Path(d)
        if (d / "passi.json").is_file():
            self._apri_progetto(d)
        else:
            self._msg("In quella cartella non c'è un passi.json: scegli la cartella del progetto.", ok=False)

    def _apri_selezionato(self):
        if self.scena != "progetti":
            return
        sel = self.lb.curselection()
        if not sel:
            self._msg("Scegli un progetto dalla lista.", ok=False)
            return
        self._apri_progetto(self._progetti[int(sel[0])])

    def _apri_progetto(self, d: Path):
        togli_tmp(d)
        try:  # rigenero la pagina cosi' i progetti vecchi hanno l'aspetto e le funzioni nuove
            dati = json.loads((d / "passi.json").read_text(encoding="utf-8"))
            genera_html(d, dati.get("titolo") or d.name, dati.get("passi", []),
                        dati.get("modalita", ""), dati.get("creato", ""))
        except Exception:
            _log.exception("Errore nel preparare la pagina del progetto")
        try:
            os.startfile(d / "index.html")
        except Exception as e:
            self._msg(f"Non riesco ad aprire la pagina: {e}", ok=False)
            return
        self._scena_avvio(msg=f"Aperto «{d.name}». Clicca sul titolo o su una descrizione per modificarla.")

    def _riprendi_selezionato(self):
        if self.scena != "progetti":
            return
        sel = self.lb.curselection()
        if not sel:
            self._msg("Scegli un progetto dalla lista.", ok=False)
            return
        self._riprendi_progetto(self._progetti[int(sel[0])])

    def _riprendi_progetto(self, d: Path):
        """Continua a registrare dentro un progetto già esistente (passi nuovi in fondo)."""
        if self.rec is not None:
            return
        togli_tmp(d)
        try:
            dati = json.loads((d / "passi.json").read_text(encoding="utf-8"))
            if not isinstance(dati.get("passi"), list):
                raise ValueError("passi.json non è valido")
        except Exception as e:
            self._msg(f"Non riesco a leggere il progetto: {e}", ok=False)
            return
        nome_modo = dati.get("modalita")
        if nome_modo not in self.cfg["modalita"]:
            nome_modo = getattr(self, "var_modo", "Normale")
            if nome_modo not in self.cfg["modalita"]:
                nome_modo = "Normale"
        self.rec = Registratore(d, nome_modo, self.cfg["modalita"][nome_modo], self.cfg, esistente=dati)
        self.in_chiusura = False
        try:
            self.rec.avvia()
        except Exception as e:
            self.rec = None
            self._msg(f"Errore all'avvio: {e}", ok=False)
            return
        self._scena_registrazione(prima_volta=True)
        self._poll()

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

    # ---- barra di registrazione: piccola, senza cornice, sempre in primo piano ----
    def _cerchio(self, cx, cy, d, tag, cmd, hint):
        c = self.c
        D_ = self._px(d)
        n = self._foto(_capa_rr(D_, D_, D_ // 2, c["vetro"], c["vetro_bordo"]))
        o = self._foto(_capa_rr(D_, D_, D_ // 2, c["vetro_hover"], c["vetro_bordo"]))
        it = self.cv.create_image(self._px(cx), self._px(cy), image=n, anchor="center", tags=(tag,))

        def entra(_e):
            self._hover = True
            self.cv.itemconfig(it, image=o)
            self.cv.config(cursor="hand2")
            self.cv.itemconfig(self.t_passi, text=hint(), fill=self.c["fg"])

        def esce(_e):
            self._hover = False
            self.cv.itemconfig(it, image=n)
            self.cv.config(cursor="")
            self._aggiorna_rec()

        self._mani((tag,), entra=entra, esce=esce, click=lambda _e: cmd())
        return it

    def _icone_barra(self):
        """Disegna l'icona del pulsante pausa (cambia tra pausa e riprendi)."""
        c = self.c
        self.cv.delete("ico_btn_p")
        cx, cy = 130, 20
        tags = ("btn_p", "ico_btn_p")
        if self.rec and self.rec.paused:
            pts = [cx - 4, cy - 7, cx - 4, cy + 7, cx + 7, cy]
            self.cv.create_polygon(*[self._px(v) for v in pts], fill=c["pausa"], outline="", tags=tags)
        else:
            for dx in (-5, 2):
                self.cv.create_rectangle(self._px(cx + dx), self._px(cy - 6), self._px(cx + dx + 3), self._px(cy + 6),
                                         fill=c["fg"], width=0, tags=tags)

    def _scena_registrazione(self, prima_volta=False):
        c = self.c
        self._scena("rec", 190, 40, "alto_dx", senza_bordo=True)
        W, H = self._px(self.w), self._px(self.h)
        barra = (22, 31, 45) if self.tema == "scuro" else (241, 244, 250)
        self.base = Image.new("RGBA", (W, H), barra + (255,))
        self._rr(0, 0, self.w, self.h, 20, None, c["vetro_bordo"])
        self._sfondo_pronto("drag")
        # il pallino resta fisso: rosso = registro, arancione = in pausa
        self.dot = self.cv.create_oval(self._px(14), self._px(14), self._px(26), self._px(26), width=0, tags=("drag",))
        self.t_passi = self._testo(36, 20, "", FONT_SB, anchor="w")
        self.cv.addtag_withtag("drag", self.t_passi)
        self._cerchio(130, 20, 28, "btn_p", self._toggle_pausa,
                      lambda: ("Riprendi" if self.rec and self.rec.paused else "Pausa")
                      + f" · {str(self.cfg.get('tasto_pausa', 'f10')).upper()}")
        self._cerchio(164, 20, 28, "btn_f", self._ferma_e_chiedi,
                      lambda: f"Fine · {str(self.cfg.get('tasto_fine', 'f9')).upper()}")
        self.cv.create_rectangle(self._px(159), self._px(15), self._px(169), self._px(25),
                                 fill=c["rec"], width=0, tags=("btn_f",))
        self.cv.tag_bind("drag", "<Button-1>", self._drag_inizio)
        self.cv.tag_bind("drag", "<B1-Motion>", self._drag_muovi)
        self._ico_stato = None
        self.root.attributes("-topmost", True)
        self.root.update_idletasks()
        self._aggiorna_rec()
        self._aggiorna_own_rect()
        self.barra_nascosta = None
        self.rec.maschera_barra = False
        if self.cfg.get("nascondi_barra", True):
            hw = hwnd_finestra(self.root)
            nascondi_da_cattura(hw, True)
            self.barra_nascosta = nascosta_da_cattura(hw)
            # se Windows non la nasconde davvero, la copro io negli screenshot
            self.rec.maschera_barra = not self.barra_nascosta
            if prima_volta:
                _log.info("Barra nascosta dagli screenshot: %s", self.barra_nascosta)

    def _drag_inizio(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag_muovi(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")
        self._aggiorna_own_rect()

    def _aggiorna_own_rect(self):
        if self.rec is None:
            return
        try:
            l, t, r, b = rettangolo_finestra(hwnd_finestra(self.root))
            self.rec.own_rect = (l - 6, t - 6, r + 6, b + 6)
        except Exception:
            pass

    def _aggiorna_rec(self):
        if self.scena != "rec" or not self.rec:
            return
        c = self.c
        in_pausa = self.rec.paused
        self.cv.itemconfig(self.dot, fill=c["pausa"] if in_pausa else c["rec"])
        if not self._hover:
            n = self.rec.n_passi
            testo = "1 passo" if n == 1 else f"{n} passi"
            self.cv.itemconfig(self.t_passi, text=f"In pausa · {n}" if in_pausa else testo,
                               fill=c["pausa"] if in_pausa else c["fg"])
        if in_pausa != self._ico_stato:
            self._ico_stato = in_pausa
            self._icone_barra()
        self.root.title(f"{'Pausa' if in_pausa else 'REC'} · {self.rec.dir.name}")

    def _poll(self):
        if self.rec is None or self.in_chiusura:
            return
        self._tick += 1
        if self.scena == "rec":
            self._aggiorna_own_rect()
            if self.cfg.get("nascondi_barra", True) and self._tick % 8 == 0:
                nascondi_da_cattura(hwnd_finestra(self.root), True)  # lo ribadisco, per sicurezza
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
        self._hover = True
        self.cv.itemconfig(self.t_passi, text="Salvo…", fill=self.c["fg"])
        self.root.update_idletasks()
        self.rec.sospendi()
        self._scena_fine()

    def _scena_fine(self):
        c = self.c
        n = self.rec.n_passi
        self._scena("fine", 380, 372, "centro")
        self.root.title(f"Registrazione ferma · {APP}")
        self.root.attributes("-topmost", False)
        self._campo_bg(34, 114, 312, 42)
        self._sfondo_pronto()

        self._intestazione("Registrazione ferma", f"{n} passo salvato" if n == 1 else f"{n} passi registrati")
        self._etichetta(34, 94, "Nome del progetto (puoi cambiarlo)")
        self.var_fine.set(self.rec.dir.name)
        e = self._entry(34, 114, 312, 42, self.var_fine)
        e.focus_set()
        e.select_range(0, "end")
        self._bottone(34, 178, 312, 48, "Salva e apri", self._salva, primario=True)
        self._bottone(34, 236, 312, 40, "Riprendi registrazione", self._riprendi)
        self._bottone(34, 284, 312, 36, "Annulla registrazione", self._scena_annulla, font=FONT)
        self._testo(190, 332, "Invio salva  ·  Esc riprende", FONT_S, c["mut"], anchor="n")
        self.root.bind("<Return>", lambda _e: self._salva())
        self.root.bind("<Escape>", lambda _e: self._riprendi())

    def _riprendi(self):
        if not self.rec or self.in_chiusura:
            return
        self.rec.riprendi()
        self._scena_registrazione()

    # ---- annullare: chiede conferma e non cancella subito (va nel backup) ----
    def _scena_annulla(self):
        if not self.rec or self.in_chiusura:
            return
        c = self.c
        rec = self.rec
        n = rec.n_nuovi
        massimo = int(self.cfg.get("backup_massimo", BACKUP_MASSIMO))
        self._scena("annulla", 380, 372, "centro")
        self.root.title(f"Annulla registrazione · {APP}")
        self.root.attributes("-topmost", False)
        self._sfondo_pronto()
        quanti = "1 passo" if n == 1 else f"{n} passi"
        self._intestazione("Annullare la registrazione?", f"{quanti} in questa ripresa" if rec.ripresa else quanti)
        if rec.ripresa:
            testo = (f"Tolgo solo i passi di questa ripresa: il progetto «{rec.dir.name}» resta com'era prima. "
                     f"I passi tolti non vengono cancellati subito: vanno nella cartella «{CARTELLA_BACKUP}», "
                     f"accanto ai progetti (ne tengo al massimo {massimo}, poi elimino i più vecchi).")
        elif n == 0:
            testo = f"Non c'è niente da salvare: la cartella vuota «{rec.dir.name}» verrà eliminata."
        else:
            testo = (f"La cartella «{rec.dir.name}» non viene cancellata subito: va nella cartella "
                     f"«{CARTELLA_BACKUP}», accanto ai progetti, e la puoi recuperare. "
                     f"Ne tengo al massimo {massimo}: quando sono di più, elimino i più vecchi.")
        self._testo(34, 100, testo, FONT_S, c["mut"], width=312)
        self._bottone(34, 236, 312, 48, "Torna indietro", self._scena_fine, primario=True)
        self._bottone(34, 292, 312, 40, "Sì, annulla", self._annulla_conferma)
        self._testo(190, 344, "Invio ed Esc tornano indietro", FONT_S, c["mut"], anchor="n")
        self.root.bind("<Return>", lambda _e: self._scena_fine())
        self.root.bind("<Escape>", lambda _e: self._scena_fine())

    def _annulla_conferma(self):
        if not self.rec or self.in_chiusura or self.scena != "annulla":
            return
        self.in_chiusura = True
        rec = self.rec
        nome = rec.dir.name
        radice = rec.dir.parent
        massimo = int(self.cfg.get("backup_massimo", BACKUP_MASSIMO))
        ok = True
        try:
            rec.ferma()
            if rec.ripresa:
                rec.annulla_ripresa(radice, massimo)
                msg = f"Ripresa annullata: «{nome}» è com'era prima. I passi tolti sono in «{CARTELLA_BACKUP}»."
            elif rec.n_passi == 0:
                shutil.rmtree(rec.dir, ignore_errors=True)
                msg = "Registrazione annullata (era vuota)."
            else:
                sposta_in_backup(rec.dir, radice, "annullata", massimo)
                msg = f"Registrazione annullata. La cartella «{nome}» è in «{CARTELLA_BACKUP}»."
        except Exception as e:
            _log.exception("Errore nell'annullare la registrazione")
            ok = False
            extra = ""
            try:
                rec._salva_json()
                rec.scrivi_html()
                copia_in_backup(rec.dir, radice, "errore", massimo)
                extra = f" Ho fatto una copia di sicurezza in «{CARTELLA_BACKUP}»."
            except Exception:
                _log.exception("Anche la copia di sicurezza è fallita")
            msg = f"Non sono riuscito ad annullare ({e}). La cartella «{nome}» è rimasta com'era.{extra}"
        self.rec = None
        self.in_chiusura = False
        self.var_nome.set("")
        self._scena_avvio(msg=msg, ok=ok)

    def _salva(self, chiudi=False):
        if not self.rec or self.in_chiusura:
            return
        rec = self.rec
        try:
            self._salva_interno(chiudi)
        except Exception as e:  # qualunque errore: i passi non si perdono, faccio anche una copia nel backup
            _log.exception("Errore nel salvataggio")
            self.rec = None
            self.in_chiusura = False
            extra = ""
            try:
                copia_in_backup(rec.dir, rec.dir.parent, "errore", int(self.cfg.get("backup_massimo", BACKUP_MASSIMO)))
                extra = f" Ho fatto una copia di sicurezza in «{CARTELLA_BACKUP}»."
            except Exception:
                _log.exception("Anche la copia di sicurezza è fallita")
            if chiudi:
                try:
                    self.root.destroy()
                except Exception:
                    pass
                return
            self.var_nome.set("")
            self._scena_avvio(msg=f"Errore nel salvare ({e}).{extra} I passi sono nella cartella «{rec.dir.name}».",
                              ok=False)

    def _salva_interno(self, chiudi=False):
        self.in_chiusura = True
        rec = self.rec
        attuale = rec.dir.name
        nuovo = pulisci_nome(self.var_fine.get())
        rec.ferma()
        rec.togli_ripresa_vuota()
        nota = ""
        if nuovo and nuovo != attuale:
            destinazione = unique_path(rec.dir.parent / nuovo)
            try:
                os.rename(rec.dir, destinazione)  # non sovrascrive: ho scelto un nome libero
                rec.dir = destinazione
                rec.titolo = None  # il titolo segue il nuovo nome della cartella
            except OSError as e:
                nota = f" Non sono riuscito a rinominare, resta «{attuale}» ({e})."
        rec._salva_json()
        rec.scrivi_html()
        index = rec.dir / "index.html"
        n = rec.n_passi
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
                self._ferma_e_chiedi()  # la chiusura non butta via niente: porta alla schermata di fine
                return
            if self.scena == "fine":
                self._salva(chiudi=True)
                return
        self.root.destroy()

    def _controlla_richiamo(self):
        """Una seconda copia del programma ci chiede di mostrarci: riportiamo la finestra davanti."""
        try:
            if _EVENTO and ctypes.windll.kernel32.WaitForSingleObject(ctypes.c_void_p(_EVENTO), 0) == 0:
                self._porta_in_primo_piano()
        except Exception:
            pass
        try:
            self.root.after(300, self._controlla_richiamo)
        except Exception:
            pass

    def _porta_in_primo_piano(self):
        r = self.root
        try:
            r.deiconify()
            r.lift()
            r.attributes("-topmost", True)
            r.focus_force()
            r.after(400, lambda: r.attributes("-topmost", False))
        except Exception:
            pass

    def run(self):
        self.root.after(300, self._controlla_richiamo)
        self.root.mainloop()


if __name__ == "__main__":
    if _istanza_unica():
        togli_tmp(CONFIG_DIR)
        pulisci_temporanei()
        App().run()
