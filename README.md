# Registratore Azioni

Un sostituto di "Registratore azioni utente" (PSR) di Windows, che non esiste più nelle versioni recenti.
A ogni clic, trascinamento o azione da tastiera salva uno screenshot (con un riquadro rosso sul punto cliccato)
e alla fine crea una pagina HTML con tutti i passi, le descrizioni modificabili e un lettore per le immagini.

Nato per rivedere a casa le lezioni di Cisco Packet Tracer, funziona con qualsiasi programma.

## Cosa fa
- Uno screenshot per ogni clic, trascinamento o azione da tastiera (la rotellina del mouse è ignorata).
- Il testo digitato viene raggruppato: una riga di comando = un passo, non uno per lettera.
- Quattro qualità (Minima, Leggera, Normale, Nitida) per tenere leggere le immagini.
- Una pagina `index.html` per ogni progetto: tema scuro/chiaro, lettore immagini, esportazione in PDF.
- Titolo e descrizioni si modificano direttamente nella pagina e si salvano nel file (Chrome o Edge).
- Non sovrascrive mai nulla: se un nome esiste già aggiunge `_2`, `_3`…
- La barra del registratore viene nascosta dagli screenshot (Windows 10 2004 o successivi).
- Funziona solo su Windows.

## Uso
Apri l'exe, scrivi il nome del progetto e premi **Avvia registrazione**.
**F10** mette in pausa, **F9** finisce e salva. Il pulsante **Guida** dentro il programma spiega tutto il resto.

## Scaricare l'exe
1. Vai su **Actions**, apri l'ultima esecuzione di *Build exe* andata a buon fine.
2. In fondo alla pagina, nella sezione **Artifacts**, scarica `RegistratoreAzioni`.
3. Estrai lo zip e avvia `RegistratoreAzioni.exe`. Se Windows mostra SmartScreen: *Ulteriori informazioni → Esegui comunque* (l'exe non è firmato).

## Costruirlo da soli
Serve solo GitHub: il workflow `.github/workflows/build.yml` crea l'exe con PyInstaller a ogni push su `main`.
Per usare un'icona personalizzata basta aggiungere un `icon.png` quadrato (consigliato 1024×1024, sfondo trasparente) nella radice del repository.

In locale, con Python 3.12:

```
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --windowed --name RegistratoreAzioni --collect-submodules pynput registratore.py
```

## Impostazioni
Il file `%APPDATA%\RegistratoreAzioni\config.json` contiene le qualità, i tasti (`tasto_pausa`, `tasto_fine`),
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
