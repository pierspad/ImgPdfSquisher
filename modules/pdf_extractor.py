import gc
import logging
import math
import multiprocessing as mp
import time
from typing import List, Iterator, Tuple, Optional
from PIL import Image
from pdf2image import convert_from_path, pdfinfo_from_path
try:
    from pdf2image.exceptions import PDFInfoNotInstalledError, PDFPageCountError, PopplerNotInstalledError
except Exception:
    PDFInfoNotInstalledError = Exception
    PDFPageCountError = Exception
    PopplerNotInstalledError = Exception
try:
    import PyPDF2
    HAS_PYPDF2 = True
except ImportError:
    HAS_PYPDF2 = False
from .config import PDF_DPI
from .system_optimizer import SystemOptimizer

class PDFExtractor:

    def __init__(self, workers: int=None, optimizer: SystemOptimizer=None):
        self.optimizer = optimizer or SystemOptimizer()
        self.workers = workers or self.optimizer.get_optimal_workers('extraction')
        self._page_cache = {}
        logging.info(f'PDFExtractor inizializzato con {self.workers} worker')

    def get_page_count(self, pdf_path: str) -> int:
        try:
            if HAS_PYPDF2:
                with open(pdf_path, 'rb') as file:
                    reader = PyPDF2.PdfReader(file)
                    count = len(reader.pages)
                    logging.debug(f'PDF ha {count} pagine (PyPDF2)')
                    return count
            info = pdfinfo_from_path(pdf_path, userpw=None, poppler_path=None)
            count = int(info.get('Pages', 0))
            logging.debug(f'PDF ha {count} pagine (pdfinfo)')
            return count
        except (PDFInfoNotInstalledError, PopplerNotInstalledError) as e:
            logging.error("Poppler non trovato (pdfinfo/pdftoppm). Installa le utilità di Poppler e riprova. Esempio su Debian/Ubuntu: 'sudo apt-get install -y poppler-utils' | Arch/Manjaro: 'sudo pacman -S poppler'")
            logging.debug(f'Dettagli: {e}')
            return 0
        except Exception as e:
            logging.error(f'Errore nel conteggio pagine: {e}')
            return 0

    def extract_page_range(self, pdf_path: str, start_page: int, end_page: int, *, output_folder: Optional[str]=None, fmt: str='png', paths_only: bool=True) -> List[str]:
        try:
            logging.debug(f'Estraendo pagine {start_page}-{end_page} -> folder={output_folder} fmt={fmt}')
            start_time = time.time()
            results = convert_from_path(pdf_path, dpi=PDF_DPI, first_page=start_page, last_page=end_page, thread_count=max(1, self.workers), output_folder=output_folder, fmt=fmt, paths_only=paths_only)
            extract_time = time.time() - start_time
            pages_count = len(results) if results else 0
            speed = pages_count / extract_time if extract_time > 0 else 0
            logging.debug(f'Estratte {pages_count} pagine in {extract_time:.2f}s ({speed:.1f} pagine/sec)')
            return results if results else []
        except (PDFInfoNotInstalledError, PopplerNotInstalledError) as e:
            logging.error("Errore: Poppler non trovato (pdfinfo/pdftoppm). Installa poppler-utils e riprova. Debian/Ubuntu: 'sudo apt-get install -y poppler-utils' | Arch/Manjaro: 'sudo pacman -S poppler'")
            logging.debug(f'Dettagli: {e}')
            return []
        except PDFPageCountError as e:
            logging.error(f'Impossibile leggere il numero di pagine del PDF. File corrotto o protetto? Dettagli: {e}')
            return []
        except Exception as e:
            logging.error(f"Errore nell'estrazione pagine {start_page}-{end_page}: {e}")
            return []

    def extract_batches(self, pdf_path: str, batch_size: int, *, output_folder: Optional[str]=None, fmt: str='png') -> Iterator[Tuple[int, List[str]]]:
        total_pages = self.get_page_count(pdf_path)
        if total_pages == 0:
            logging.error('Impossibile determinare il numero di pagine del PDF')
            return
        logging.info(f'PDF contiene {total_pages} pagine, elaborazione in batch di {batch_size}')
        for batch_num, start_page in enumerate(range(1, total_pages + 1, batch_size)):
            end_page = min(start_page + batch_size - 1, total_pages)
            logging.info(f'Elaborando batch {batch_num + 1}/{math.ceil(total_pages / batch_size)}: pagine {start_page}-{end_page}')
            batch_start_time = time.time()
            images = self.extract_page_range(pdf_path, start_page, end_page, output_folder=output_folder, fmt=fmt, paths_only=True)
            batch_time = time.time() - batch_start_time
            if images:
                logging.debug(f'Batch {batch_num + 1} estratto in {batch_time:.2f}s ({len(images) / batch_time:.1f} pagine/sec)')
                yield (batch_num, images)
            else:
                logging.warning(f"Batch {batch_num + 1} vuoto o errore nell'estrazione")

    def clear_cache(self):
        self._page_cache.clear()
        gc.collect()