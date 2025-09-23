import argparse
import logging
from pathlib import Path
from typing import Tuple, List

import numpy as np
from PIL import Image
from pdf2image import convert_from_path

try:
    import cv2  # type: ignore
except Exception as e:  # pragma: no cover - opzionale in ambienti senza opencv
    cv2 = None
    _cv2_import_error = e
else:
    _cv2_import_error = None


def _ensure_outdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def pil_to_cv(img: Image.Image) -> np.ndarray:
    if img.mode == "RGBA":
        img = img.convert("RGB")
    return np.array(img)[:, :, ::-1]  # RGB->BGR


def cv_to_pil(img: np.ndarray) -> Image.Image:
    if img.ndim == 2:
        return Image.fromarray(img)
    return Image.fromarray(img[:, :, ::-1])


def build_bubble_mask(gray: np.ndarray) -> np.ndarray:
    """Stima una maschera delle balloon su un'immagine in toni di grigio.

    Strategia robusta ma classica:
    - smoothing leggero per ridurre il rumore
    - threshold adattivo per separare sfondo chiaro/segni neri
    - canny + dilatazione per chiudere i contorni
    - ricerca dei contorni e filtro per area/rotondità/solidity
    - verifica presenza di testo (pixel scuri) all'interno
    """
    h, w = gray.shape[:2]

    # Pre-filter per attenuare retinature
    blur = cv2.GaussianBlur(gray, (5, 5), 0)

    # Edge map dei bordi neri delle balloon
    edges = cv2.Canny(blur, 60, 160)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

    # Chiudi piccoli buchi nei contorni
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8), iterations=1)

    # Trova contorni
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # Maschera finale
    mask = np.zeros((h, w), dtype=np.uint8)

    # Prepara mappe per controlli
    # Pixel scuri ~ testo
    text_mask = (gray < 130).astype(np.uint8) * 255

    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < (w * h) * 0.002 or area > (w * h) * 0.35:
            continue
        peri = cv2.arcLength(cnt, True)
        if peri == 0:
            continue
        roundness = 4 * np.pi * (area / (peri * peri))
        # App rossimazione a elisse/non spigoloso
        if roundness < 0.12:  # troppo spigoloso -> prob. vignetta, non balloon
            continue
        hull = cv2.convexHull(cnt)
        hull_area = cv2.contourArea(hull)
        if hull_area == 0:
            continue
        solidity = float(area) / float(hull_area)
        if solidity < 0.6:
            continue

        # Crea una maschera temporanea della regione candidata
        temp = np.zeros_like(mask)
        cv2.drawContours(temp, [cnt], -1, 255, thickness=-1)

        # Verifica presenza di testo (abbastanza pixel scuri all'interno)
        inter = cv2.bitwise_and(text_mask, text_mask, mask=temp)
        text_ratio = float(cv2.countNonZero(inter)) / float(area + 1e-6)
        if text_ratio < 0.01:
            # Probabile balloon vuota, vignetta o fumetto decorativo
            continue

        mask = cv2.bitwise_or(mask, temp)

    # Finitura maschera: smussa e riempi piccoli buchi
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8), iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8), iterations=1)
    return mask


def extract_bubbles_from_pil(pil_img: Image.Image) -> Image.Image:
    if cv2 is None:
        raise RuntimeError(
            f"OpenCV non disponibile: {repr(_cv2_import_error)}. Installa 'opencv-python-headless' per usare questo script."
        )
    # Converti e costruisci maschera
    cv_img = pil_to_cv(pil_img)
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    mask = build_bubble_mask(gray)

    # Applica maschera per ottenere PNG trasparente
    rgba = pil_img.convert("RGBA")
    alpha = Image.fromarray(mask)
    # Mantieni completamente opaco dentro le balloon, trasparente altrove
    out = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
    out.paste(rgba, (0, 0), mask=alpha)
    return out


def process_pdf(input_pdf: Path, out_dir: Path, dpi: int = 300, debug: bool = False) -> List[Path]:
    _ensure_outdir(out_dir)
    pages = convert_from_path(str(input_pdf), dpi=dpi)
    outputs: List[Path] = []
    for i, pil_page in enumerate(pages, start=1):
        bubbles = extract_bubbles_from_pil(pil_page)
        out_path = out_dir / f"page-{i:03d}_bubbles.png"
        bubbles.save(out_path)
        outputs.append(out_path)
        if debug and cv2 is not None:
            # Esporta anche una preview con contorni
            cv_img = pil_to_cv(pil_page)
            gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
            mask = build_bubble_mask(gray)
            preview = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
            preview[mask > 0] = (0, 255, 0)
            cv2.imwrite(str(out_dir / f"page-{i:03d}_preview.jpg"), preview)
    return outputs


def main():
    parser = argparse.ArgumentParser(
        description="Estrae le balloon dei dialoghi da un PDF e crea PNG trasparenti con solo i dialoghi"
    )
    parser.add_argument(
        "--input",
        default=str(Path("prova-segmentation") / "prova.pdf"),
        help="PDF di input (default: prova-segmentation/prova.pdf)",
    )
    parser.add_argument(
        "--out-dir",
        default=str(Path("prova-segmentation")),
        help="Cartella output (default: prova-segmentation)",
    )
    parser.add_argument("--dpi", type=int, default=300, help="DPI di rasterizzazione per il PDF")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log dettagliati")
    parser.add_argument("--debug", action="store_true", help="Esporta immagini di debug")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s - %(message)s")

    in_pdf = Path(args.input)
    out_dir = Path(args.out_dir)
    if not in_pdf.exists():
        logging.error(f"Input non trovato: {in_pdf}")
        return 1

    if cv2 is None:
        logging.error(
            f"OpenCV non disponibile. Installa 'opencv-python-headless' (errore: {repr(_cv2_import_error)})"
        )
        return 1

    logging.info(f"Estrazione balloon da {in_pdf.name} -> {out_dir}")
    outputs = process_pdf(in_pdf, out_dir, dpi=args.dpi, debug=args.debug)
    logging.info(f"Generate {len(outputs)} immagini di balloon")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
