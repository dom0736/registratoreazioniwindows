# Registratore Azioni

Un sostituto di "Registratore azioni utente" (PSR) di Windows, che non esiste più nelle versioni recenti.
A ogni clic, trascinamento o azione da tastiera salva uno screenshot (con un riquadro colorato sul punto cliccato)
e alla fine crea una pagina HTML con tutti i passi, le descrizioni modificabili con un clic e un lettore per le immagini.

Nato per rivedere a casa le lezioni di Cisco Packet Tracer, funziona con qualsiasi programma.

## Cosa fa
- Uno screenshot per ogni clic, trascinamento o azione da tastiera (la rotellina del mouse è ignorata).
- Un colore per ogni tipo di azione: rosso clic sinistro, blu destro, verde centrale, viola doppio clic, arancione trascinamento, verde acqua tastiera.
- Tutto in una sola finestra, senza popup. Durante la registrazione resta solo una barretta piccola e trascinabile (pallino rosso fisso, pulsanti rotondi per pausa e fine), nascosta dagli screenshot. Con **Fine** la registrazione si ferma ma non si salva: puoi scegliere il nome e salvare, oppure **Riprendere** da dove eri.
- Il testo digitato viene raggruppato: una riga di comando = un passo, non uno per lettera.
- Quattro qualità (Minima, Leggera, Normale, Nitida) per tenere leggere le immagini.
- Una pagina `index.html` per ogni progetto: stesso aspetto dell'app, tema scuro/chiaro, lettore immagini.
- **Stampa e salva** (icona della stampante o Ctrl+P): anteprima impaginata, scelta dei passi da includere, formato A4/Letter, verticale/orizzontale, margini, passi per pagina, cosa mostrare (descrizioni, ora, screenshot, numero di pagina, sfondo bianco). Poi «Stampa / Salva PDF…» apre la finestra di stampa del browser: scegliendo «Salva come PDF» ottieni il PDF. Si può anche scaricare l'elenco dei passi in Markdown.
- **Modifica dalla pagina HTML**: clicchi sul titolo o su una descrizione, scrivi e premi **Salva copia**: nasce la cartella `nome_modificato` accanto all'originale, che non cambia. Il browser chiede una volta di scegliere la cartella che contiene il progetto (serve Edge o Chrome).
- Non sovrascrive mai nulla: se un nome esiste già aggiunge `_2`, `_3`…
- Non lascia file in giro: i `.tmp` di una chiusura anomala e le vecchie cartelle `_MEI` dell'exe in Temp vengono tolti all'avvio successivo, i backup hanno un limite, i report vecchi sul Desktop spariscono dopo 14 giorni, una «Salva copia» fallita non lascia cartelle a metà.
- La barra del registratore viene nascosta dagli screenshot (Windows 10 2004 o successivi).
- Funziona solo su Windows.

## Uso
Apri l'exe, scrivi il nome del progetto e premi **Avvia registrazione**.
**F10** mette in pausa, **F9** finisce e salva. Il pulsante **Guida** dentro il programma spiega tutto il resto.

## Scaricare l'exe
1. Vai su **Releases** (colonna a destra del repository) e scarica `RegistratoreAzioni.exe` dell'ultima versione.
2. Avvialo. Se Windows mostra SmartScreen: *Ulteriori informazioni → Esegui comunque* (l'exe non è firmato).

## Costruirlo da soli
Serve solo GitHub: il workflow `.github/workflows/build.yml` crea l'exe con PyInstaller a ogni push su `main`.
Per usare un'icona personalizzata basta aggiungere un `icon.png` quadrato (512×512 va benissimo, anche più grande) nella radice del repository.
Il file `marchio_registratore.txt` serve alla pulizia automatica delle cartelle temporanee: tienilo nel repository.
Gli angoli vengono arrotondati in automatico per l'exe, la finestra e la pagina HTML. I colori dell'interfaccia seguono l'icona (grigio-blu, blu→magenta, pallino rosso). Senza `icon.png` il programma usa un'icona di riserva.

In locale, con Python 3.12:

```
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --windowed --name RegistratoreAzioni --collect-submodules pynput --add-data "marchio_registratore.txt;." registratore.py
```

## Impostazioni
Cartella dei progetti, tema e nascondere la barra dagli screenshot si cambiano da **Impostazioni** dentro il programma.
Il file `%APPDATA%\RegistratoreAzioni\config.json` contiene anche le qualità, i tasti (`tasto_pausa`, `tasto_fine`),
la pausa che chiude un passo di testo (`pausa_digitazione_sec`), `registra_testo_digitato`, `nascondi_barra` e il tema.

## Privacy
Il programma registra tutto quello che fai e scrivi mentre è in funzione, password comprese: usa la pausa quando serve.
Tutti i dati restano sul tuo PC, nella cartella del progetto. Non viene inviato nulla.

## Open source e segnalazioni
Il progetto è open source (licenza MIT) e mi fa piacere che circoli: più persone lo provano, più errori si trovano.
Se qualcosa non va o hai un'idea, scrivimi a **domenico.castaldod29@gmail.com**.

Dentro il programma c'è il pulsante **Segnala un problema**: crea un file di report con i dati tecnici utili per
sistemare l'errore (versione, Windows, schermi, impostazioni ed errori registrati). Non contiene screenshot né il testo
digitato, e puoi leggerlo prima di inviarlo. Allegalo alla mail con due righe su cosa stavi facendo.

---

## English summary
Registratore Azioni is a free, open source (MIT) replacement for the Windows Problem Steps Recorder: it saves a screenshot for every click, drag or keyboard action and builds an editable HTML page with all the steps. Windows only. The interface is in Italian.

Download `RegistratoreAzioni.exe` from **Releases**. The exe is not code-signed, so Windows SmartScreen may warn you: *More info → Run anyway*. Everything stays on your PC and nothing is sent anywhere.
