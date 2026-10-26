"""Trasforma icon.png in icon.ico con gli angoli arrotondati (lo usa il workflow di build).

Uso:  python crea_icona.py icon.png icon.ico
Va bene qualsiasi PNG quadrato (512x512 o piu' grande): viene portato a 512x512 prima di arrotondarlo.
"""
import sys

from PIL import Image, ImageChops, ImageDraw, ImageOps


def arrotonda(im: Image.Image, lato: int = 512, raggio: float = 0.22) -> Image.Image:
    ss = 3
    im = ImageOps.fit(im.convert("RGBA"), (lato, lato), Image.Resampling.LANCZOS)
    if im.getpixel((2, 2))[3] < 200:  # gli angoli sono gia' trasparenti: l'icona e' gia' arrotondata
        return im
    maschera = Image.new("L", (lato * ss, lato * ss), 0)
    ImageDraw.Draw(maschera).rounded_rectangle([0, 0, lato * ss - 1, lato * ss - 1],
                                               radius=int(lato * raggio * ss), fill=255)
    maschera = maschera.resize((lato, lato), Image.Resampling.LANCZOS)
    im.putalpha(ImageChops.multiply(im.getchannel("A"), maschera))
    return im


if __name__ == "__main__":
    origine = sys.argv[1] if len(sys.argv) > 1 else "icon.png"
    destinazione = sys.argv[2] if len(sys.argv) > 2 else "icon.ico"
    arrotonda(Image.open(origine)).save(
        destinazione, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"Creato {destinazione}")
