import argparse
import gc
import io
import json
import logging
import multiprocessing as mp
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed, CancelledError, TimeoutError
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
try:
    import PyPDF2
    HAS_PYPDF2 = True
except ImportError:
    HAS_PYPDF2 = False
    logging.warning('PyPDF2 non disponibile - funzionalità di split limitate')
from modules import SystemOptimizer, MemoryMonitor, PDFExtractor, ImageProcessor, CompressionStats, TimingStats, DEVICE_PROFILES, COMPRESSION_MODES, DEFAULT_QUALITY, DEFAULT_MAX_COLORS
from modules.worker_functions import process_image_worker
from modules.config import ESTIMATED_MB_PER_PAGE

class MangaCompressorModular:

    def __init__(self, target_device: str='tablet_10', quality: int=DEFAULT_QUALITY, max_colors: int=DEFAULT_MAX_COLORS, compression_mode: str='auto', workers: int=None, ram_limit_percent: int=75, tmp_dir: Optional[str | Path]=None, progress_callback: Optional[Callable[[dict], None]]=None, stop_checker: Optional[Callable[[], bool]]=None):
        self.system_optimizer = SystemOptimizer()
        self.memory_monitor = MemoryMonitor(ram_limit_percent)
        self.pdf_extractor = PDFExtractor(workers=workers, optimizer=self.system_optimizer)
        self.device_profile = DEVICE_PROFILES[target_device]
        self.quality = quality
        self.max_colors = max_colors
        self.compression_mode = compression_mode
        self.tmp_dir = Path(tmp_dir) if tmp_dir else Path.cwd() / 'tmp'
        self.progress_cb = progress_callback
        self._stop_checker = stop_checker or (lambda: False)
        self._stop_requested = False
        self._executor: Optional[ProcessPoolExecutor] = None
        self.image_processor = ImageProcessor(self.device_profile, self.quality, self.max_colors, self.compression_mode)
        cpu_count = mp.cpu_count()
        if workers:
            self.compression_workers = max(1, workers)
        else:
            mem_per_worker_gb = ESTIMATED_MB_PER_PAGE * 1.5 / 1024.0
            avail_gb = max(0.5, self.memory_monitor.get_available_gb())
            by_mem = int(avail_gb / mem_per_worker_gb) if mem_per_worker_gb > 0 else cpu_count
            hard_cap = 16 if cpu_count >= 8 else 8
            self.compression_workers = max(1, min(cpu_count, by_mem, hard_cap))
        logging.info(f'Worker di compressione: {self.compression_workers} (CPU={cpu_count})')
        self.stats = CompressionStats()

    def request_stop(self):
        self._stop_requested = True
        try:
            self._cancel_active_workers()
        except Exception:
            pass
        return True

    def _cancel_active_workers(self):
        ex = self._executor
        if not ex:
            return
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        try:
            procs = []
            if hasattr(ex, '_processes'):
                procs = list(getattr(ex, '_processes').values())
            for p in procs:
                try:
                    p.terminate()
                except Exception:
                    pass
            for p in procs:
                try:
                    p.join(timeout=0.1)
                    if hasattr(p, 'is_alive') and p.is_alive():
                        p.kill()
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            self._executor = None

    def compress_pdf(self, input_path: str, output_path: str) -> bool:
        input_path = Path(input_path)
        output_path = Path(output_path)
        if not input_path.exists():
            logging.error(f'Input file not found: {input_path}')
            return False
        self.stats = CompressionStats()
        original_size = input_path.stat().st_size
        self.stats.original_size_mb = original_size / (1024 * 1024)
        base_tmp = self.tmp_dir
        base_tmp.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='manga_compress_', dir=str(base_tmp)) as temp_dir:
            try:
                if self._stop_checker():
                    return False
                success = self._compress_with_modules(input_path, output_path, temp_dir)
                if success and output_path.exists():
                    compressed_size = output_path.stat().st_size
                    self.stats.compressed_size_mb = compressed_size / (1024 * 1024)
                    self._print_final_stats(current_file=input_path.name, output_file=output_path.name)
                    if self.progress_cb:
                        try:
                            self.progress_cb({'event': 'file_done', 'file': input_path.name, 'output': str(output_path), 'stats': {'pages_processed': self.stats.pages_processed, 'pages_total': self.stats.pages_total, 'original_size_mb': self.stats.original_size_mb, 'compressed_size_mb': self.stats.compressed_size_mb, 'ratio': self.stats.compression_ratio(), 'saved_mb': self.stats.space_saved_mb(), 'elapsed_sec': self.stats.elapsed_time(), 'speed_pps': self.stats.pages_per_second()}})
                        except Exception:
                            pass
                return success
            except Exception as e:
                logging.error(f'Error during processing: {e}')
                if self.progress_cb:
                    try:
                        self.progress_cb({'event': 'error', 'message': str(e)})
                    except Exception:
                        pass
                return False
            finally:
                try:
                    if base_tmp.exists() and (not any(base_tmp.iterdir())):
                        base_tmp.rmdir()
                except Exception:
                    pass

    def _compress_with_modules(self, input_path: Path, output_path: Path, temp_dir: str) -> bool:
        try:
            logging.info(f'Analizzando PDF: {input_path.name}')
            total_pages = self.pdf_extractor.get_page_count(str(input_path))
            if total_pages == 0:
                logging.error('No pages found in PDF')
                return False
            logging.info(f'PDF contiene {total_pages} pagine')
            self.stats.pages_total = total_pages
            available_gb = self.memory_monitor.get_available_gb()
            optimal_batch = self.system_optimizer.get_optimal_batch_size(total_pages, available_gb)
            batch_size = max(8, min(optimal_batch, 64))
            logging.info(f'Batch size scelto: {batch_size} pagine (ottimizzato per ridurre overhead di estrazione)')
            tmp_extract_dir = Path(temp_dir) / 'tmp'
            if tmp_extract_dir.exists():
                try:
                    shutil.rmtree(tmp_extract_dir)
                except Exception:
                    pass
            tmp_extract_dir.mkdir(parents=True, exist_ok=True)
            temp_output = os.path.join(temp_dir, 'output.pdf')
            with open(temp_output, 'wb') as pdf_file:
                canvas_obj = canvas.Canvas(pdf_file)
                ranges = []
                for start_page in range(1, total_pages + 1, batch_size):
                    end_page = min(start_page + batch_size - 1, total_pages)
                    ranges.append((start_page, end_page))
                prefetch_batches = min(3, max(1, self.compression_workers // 2))
                logging.info(f'Prefetch di {prefetch_batches} batch di pagine per sovrapporre estrazione e compressione')
                all_temp_files = []
                with ThreadPoolExecutor(max_workers=prefetch_batches) as extractor_pool:
                    futures = {}

                    def submit_job(idx):
                        s, e = ranges[idx]
                        return extractor_pool.submit(self.pdf_extractor.extract_page_range, str(input_path), s, e, output_folder=str(tmp_extract_dir), fmt='png', paths_only=True)
                    next_submit = 0
                    next_process = 0
                    while next_submit < len(ranges) and len(futures) < prefetch_batches:
                        futures[next_submit] = submit_job(next_submit)
                        next_submit += 1
                    while next_process < len(ranges):
                        if self._stop_checker():
                            logging.info('Stop richiesto: interrompo pipeline')
                            return False
                        future = futures.get(next_process)
                        if future is None:
                            break
                        while True:
                            if self._stop_checker() or self._stop_requested:
                                logging.info('Stop richiesto: interrompo pipeline')
                                try:
                                    extractor_pool.shutdown(wait=False, cancel_futures=True)
                                except Exception:
                                    pass
                                self._cancel_active_workers()
                                return False
                            try:
                                image_paths = future.result(timeout=0.25)
                                break
                            except TimeoutError:
                                continue
                        del futures[next_process]
                        batch_start = time.time()
                        success = self._process_batch_modular(image_paths, canvas_obj, next_process + 1)
                        batch_time = time.time() - batch_start
                        if not success:
                            return False
                        self.stats.pages_processed += len(image_paths)
                        all_temp_files.extend(image_paths)
                        self._safe_delete_files(image_paths)
                        gc.collect()
                        self._update_progress(batch_time, len(image_paths))
                        if next_submit < len(ranges):
                            futures[next_submit] = submit_job(next_submit)
                            next_submit += 1
                        next_process += 1
                canvas_obj.save()
            if self._stop_checker():
                logging.info('Stop richiesto prima del salvataggio: annullo output')
                return False
            shutil.move(temp_output, output_path)
            return True
        except Exception as e:
            logging.error(f'Error during processing: {e}')
            return False

    def _compress_large_pdf_chunked(self, input_path: Path, output_path: Path, temp_dir: str, total_pages: int) -> bool:
        try:
            chunk_size = max(50, total_pages // self.compression_workers)
            logging.info(f'Chunk size: {chunk_size} pagine per worker')
            temp_output = os.path.join(temp_dir, 'output.pdf')
            with open(temp_output, 'wb') as pdf_file:
                canvas_obj = canvas.Canvas(pdf_file)
                for start_page in range(1, total_pages + 1, chunk_size):
                    end_page = min(start_page + chunk_size - 1, total_pages)
                    chunk_pages = end_page - start_page + 1
                    chunk_start_time = time.time()
                    logging.info(f'Processando pagine {start_page}-{end_page} ({chunk_pages} pagine)...')
                    logging.debug(f'Estraendo immagini pagine {start_page}-{end_page}')
                    chunk_images = self.pdf_extractor.extract_page_range(str(input_path), start_page, end_page)
                    if not chunk_images:
                        logging.warning(f'Nessuna immagine estratta per pagine {start_page}-{end_page}')
                        continue
                    logging.debug(f'Estratte {len(chunk_images)} immagini, iniziando compressione...')
                    batch_size = min(self.compression_workers * 2, 32)
                    for i in range(0, len(chunk_images), batch_size):
                        batch_images = chunk_images[i:i + batch_size]
                        batch_num = (start_page - 1) // chunk_size + 1
                        batch_start = time.time()
                        success = self._process_batch_modular(batch_images, canvas_obj, batch_num)
                        batch_time = time.time() - batch_start
                        if not success:
                            return False
                        self.stats.pages_processed += len(batch_images)
                        logging.debug(f'Batch di {len(batch_images)} immagini completato in {batch_time:.2f}s')
                        gc.collect()
                        self._update_progress(batch_time, len(batch_images))
                    chunk_time = time.time() - chunk_start_time
                    chunk_speed = chunk_pages / chunk_time
                    logging.info(f'Chunk {start_page}-{end_page} completato in {chunk_time:.1f}s ({chunk_speed:.1f} pag/sec)')
                    del chunk_images
                    gc.collect()
                canvas_obj.save()
            shutil.move(temp_output, output_path)
            return True
        except Exception as e:
            logging.error(f'Error during chunked processing: {e}')
            return False

    def _process_batch_modular(self, images, canvas_obj, batch_num: int) -> bool:
        if not images:
            return True
        logging.debug(f'Processando batch {batch_num} con {len(images)} immagini')
        processor_config = {'device_profile': self.device_profile, 'quality': self.quality, 'max_colors': self.max_colors, 'compression_mode': self.compression_mode}
        tasks = [(img, processor_config) for img in images]
        compression_start = time.time()
        logging.debug(f'Avviando compressione parallela con {self.compression_workers} workers...')
        results = []
        executor = ProcessPoolExecutor(max_workers=self.compression_workers)
        self._executor = executor
        try:
            future_to_idx = {executor.submit(process_image_worker, task): idx for idx, task in enumerate(tasks)}
            completed_count = 0
            for future in as_completed(future_to_idx):
                if self._stop_checker() or self._stop_requested:
                    logging.info('Stop richiesto durante compressione batch')
                    self._cancel_active_workers()
                    return False
                idx = future_to_idx[future]
                try:
                    size, compressed_data = future.result()
                    results.append((idx, size, compressed_data))
                    completed_count += 1
                    if completed_count % 10 == 0 or completed_count == len(tasks):
                        logging.debug(f'Compresse {completed_count}/{len(tasks)} immagini del batch')
                except CancelledError:
                    logging.info('Future cancellata')
                    return False
                except Exception as e:
                    logging.error(f'Error processing image {idx}: {e}')
                    return False
        finally:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            self._executor = None
        compression_time = time.time() - compression_start
        self.stats.timing.compression_time += compression_time
        compression_speed = len(images) / compression_time
        logging.debug(f'Compressione completata in {compression_time:.2f}s ({compression_speed:.1f} img/sec)')
        results.sort(key=lambda x: x[0])
        write_start = time.time()
        logging.debug(f'Scrivendo {len(results)} immagini nel PDF...')
        for idx, size, compressed_data in results:
            try:
                img_reader = ImageReader(io.BytesIO(compressed_data))
                img_width, img_height = size
                if img_width > 0 and img_height > 0:
                    page_width, page_height = self.device_profile['size']
                    scale_w = page_width / img_width
                    scale_h = page_height / img_height
                    scale = min(scale_w, scale_h)
                    final_width = img_width * scale
                    final_height = img_height * scale
                    x = (page_width - final_width) / 2
                    y = (page_height - final_height) / 2
                    canvas_obj.setPageSize((page_width, page_height))
                    canvas_obj.drawImage(img_reader, x, y, final_width, final_height)
                    canvas_obj.showPage()
                else:
                    logging.warning(f'Image {idx} has invalid dimensions: {size}')
            except Exception as e:
                logging.error(f'Error adding image {idx} to PDF: {e}')
                return False
        write_time = time.time() - write_start
        self.stats.timing.writing_time += write_time
        write_speed = len(results) / write_time if write_time > 0 else 0
        logging.debug(f'Scrittura completata in {write_time:.2f}s ({write_speed:.1f} img/sec)')
        return True

    def _safe_delete_files(self, paths):
        for p in paths:
            try:
                os.remove(p)
            except Exception:
                pass

    def _update_progress(self, batch_time: float, batch_pages: int):
        elapsed = self.stats.elapsed_time()
        pages_per_sec = self.stats.pages_per_second()
        eta_sec = self.stats.eta_seconds()
        progress_percent = self.stats.pages_processed / self.stats.pages_total * 100
        logging.info(f'Progress: {self.stats.pages_processed}/{self.stats.pages_total} ({progress_percent:.1f}%) - {pages_per_sec:.1f} pag/sec - ETA: {eta_sec / 60:.1f}min')
        logging.debug(f'Batch completato in {batch_time:.2f}s ({batch_pages / batch_time:.1f} pag/sec)')
        if self.progress_cb:
            try:
                self.progress_cb({'event': 'progress', 'pages_processed': self.stats.pages_processed, 'pages_total': self.stats.pages_total, 'percent': progress_percent, 'pages_per_sec': pages_per_sec, 'eta_sec': eta_sec, 'elapsed_sec': elapsed})
            except Exception:
                pass

    def _print_final_stats(self, *, current_file: str | None=None, output_file: str | None=None):
        ratio = self.stats.compression_ratio()
        saved_mb = self.stats.space_saved_mb()
        elapsed = self.stats.elapsed_time()
        print('\n' + '=' * 60)
        header = 'FINAL STATISTICS'
        if current_file:
            header += f' - {current_file}'
        print(header)
        print('=' * 60)
        print(f'Pages processed: {self.stats.pages_processed}')
        print(f'Original size: {self.stats.original_size_mb:.1f} MB')
        print(f'Compressed size: {self.stats.compressed_size_mb:.1f} MB')
        print(f'Compression ratio: {ratio:.1%}')
        print(f'Space saved: {saved_mb:.1f} MB')
        print(f'Total time: {elapsed / 60:.1f} minutes')
        print(f'Average speed: {self.stats.pages_per_second():.1f} pages/sec')
        if output_file:
            print(f'Output file: {output_file}')
        if not self.stats.is_compression_effective():
            print('WARNING: Low compression effectiveness (< 5% reduction)')
        print('=' * 60)

def setup_logging(verbose: bool=False):
    level = logging.DEBUG if verbose else logging.INFO
    format_str = '%(asctime)s - %(levelname)s - %(message)s'
    logging.basicConfig(level=level, format=format_str, handlers=[logging.StreamHandler(sys.stdout)])

def save_default_config(args):
    config_file = Path(__file__).parent / '.manga_compressor_defaults.json'
    existing = {}
    if config_file.exists():
        try:
            with open(config_file, 'r') as f:
                existing = json.load(f) or {}
        except Exception:
            existing = {}
    merged = {'device': getattr(args, 'device', existing.get('device', 'tablet_10')), 'mode': getattr(args, 'mode', existing.get('mode', 'auto')), 'quality': getattr(args, 'quality', existing.get('quality', DEFAULT_QUALITY)), 'max_colors': getattr(args, 'max_colors', existing.get('max_colors', DEFAULT_MAX_COLORS)), 'workers': getattr(args, 'workers', existing.get('workers', None)), 'ram_limit': getattr(args, 'ram_limit', existing.get('ram_limit', 75)), 'suffix': getattr(args, 'suffix', existing.get('suffix', None)), 'out_dir': getattr(args, 'out_dir', existing.get('out_dir', 'compressed')), 'tmp_dir': getattr(args, 'tmp_dir', existing.get('tmp_dir', 'tmp')), 'theme': getattr(args, 'theme', existing.get('theme', None)), 'language': getattr(args, 'language', existing.get('language', 'en')), 'ui_mode': getattr(args, 'ui_mode', existing.get('ui_mode', 'advanced'))}
    with open(config_file, 'w') as f:
        json.dump(merged, f, indent=2)
    print(f'Default configuration saved to {config_file}')

def load_default_config():
    config_file = Path(__file__).parent / '.manga_compressor_defaults.json'
    if config_file.exists():
        try:
            with open(config_file, 'r') as f:
                return json.load(f)
        except:
            pass
    return {}

def parse_output_filename(input_file, suffix=None, out_dir: str | None=None):
    input_path = Path(input_file)
    base_dir = Path(out_dir) if out_dir else input_path.parent
    base_dir.mkdir(parents=True, exist_ok=True)
    clean_suffix = None
    if suffix:
        clean_suffix = suffix[1:] if isinstance(suffix, str) and suffix.startswith('+') else suffix
    if not clean_suffix:
        clean_suffix = '_compressed'
    output_name = f'{input_path.stem}{clean_suffix}{input_path.suffix}'
    return str(base_dir / output_name)

def main():
    defaults = load_default_config()
    parser = argparse.ArgumentParser(description='Manga PDF Compressor - Reduce PDF manga file size by optimizing images', formatter_class=argparse.RawDescriptionHelpFormatter, epilog=f"""\nSUPPORTED DEVICES:\n{chr(10).join([f"  {name:12}: {profile['description']}" for name, profile in DEVICE_PROFILES.items()])}\n\nCOMPRESSION MODES:\n{chr(10).join([f'  {mode:12}: {desc}' for mode, desc in COMPRESSION_MODES.items()])}\n\nUSAGE EXAMPLES:\n  # Basic compression (tablet 10" default)\n  python {os.path.basename(__file__)} manga.pdf manga_compressed.pdf\n  \n  # For smartphone with maximum compression\n  python {os.path.basename(__file__)} manga.pdf manga_phone.pdf --device phone --mode bw\n  \n  # For e-reader with high quality\n  python {os.path.basename(__file__)} manga.pdf manga_ereader.pdf --device ereader --quality 95\n  \n  # With detailed logging for debug\n  python {os.path.basename(__file__)} manga.pdf output.pdf --verbose\n\n    # Auto-named into ./compressed using suffix\n    python {os.path.basename(__file__)} manga.pdf +_compressed\n\n    # Batch mode with explicit list\n    python {os.path.basename(__file__)} --files a.pdf b.pdf c.pdf --out-dir compressed\n\n    # Batch mode from a file (one path per line)\n    python {os.path.basename(__file__)} --file-list files.txt --suffix _phone\n\nNOTE: Default output directory is ./compressed (auto-created). In batch mode, OUTPUT is auto-named per input.\n        """)
    parser.add_argument('input_pdf', nargs='?', help='Input PDF file to compress')
    parser.add_argument('output_pdf', nargs='?', help='Output compressed PDF file or +suffix for auto naming')
    parser.add_argument('--files', nargs='+', metavar='PDF', help='Batch mode: list of input PDF files to compress')
    parser.add_argument('--file-list', metavar='PATH', help='Path to a text file containing one PDF path per line (batch mode)')
    parser.add_argument('--device', choices=list(DEVICE_PROFILES.keys()), default=defaults.get('device', 'tablet_10'), help='Target device - determines resolution and optimizations (default: tablet_10)')
    parser.add_argument('--mode', choices=list(COMPRESSION_MODES.keys()), default=defaults.get('mode', 'auto'), help="Compression mode - 'auto' automatically detects the best type (default: auto)")
    parser.add_argument('--quality', type=int, default=defaults.get('quality', DEFAULT_QUALITY), metavar='1-100', help=f"JPEG quality (1=minimum, 100=maximum) - affects final size (default: {defaults.get('quality', DEFAULT_QUALITY)})")
    parser.add_argument('--max-colors-power', type=int, default=None, metavar='P', help='Palette size as power of two (P). 1=>2, 2=>4, ..., 8=>256. Overrides --max-colors if given.')
    parser.add_argument('--max-colors', type=int, default=defaults.get('max_colors', DEFAULT_MAX_COLORS), metavar='N', help=f"Maximum colors (legacy). Will be clamped to a power of two in [2..256]. Default: {defaults.get('max_colors', DEFAULT_MAX_COLORS)}")
    parser.add_argument('--workers', type=int, default=defaults.get('workers', None), metavar='N', help='Parallel workers for processing - more workers = faster but more RAM (default: auto)')
    parser.add_argument('--ram-limit', type=int, default=defaults.get('ram_limit', 75), metavar='PERCENT', help='RAM limit percentage - controls how much memory to use (default: 75%%)')
    parser.add_argument('--out-dir', default=defaults.get('out_dir', 'compressed'), help='Directory where compressed PDFs will be written (default: ./compressed)')
    parser.add_argument('--tmp-dir', default=defaults.get('tmp_dir', 'tmp'), help='Directory for temporary image extraction (default: ./tmp)')
    parser.add_argument('--suffix', default=defaults.get('suffix', None), help="Suffix to append to output filenames (e.g., _compressed). Use '+_x' style also supported.")
    parser.add_argument('--default', action='store_true', help='Save current arguments as defaults for future runs')
    parser.add_argument('--verbose', '-v', action='store_true', help='Detailed logging for debugging and progress monitoring')
    args = parser.parse_args()
    if args.output_pdf and isinstance(args.output_pdf, str) and args.output_pdf.startswith('+'):
        args.suffix = args.output_pdf
        args.output_pdf = None
    if args.default:
        save_default_config(args)
    batch_inputs = []
    if args.files:
        batch_inputs.extend(args.files)
    if args.file_list:
        try:
            with open(args.file_list, 'r') as f:
                for line in f:
                    line = line.strip().strip('"').strip("'")
                    if line:
                        batch_inputs.append(line)
        except Exception as e:
            logging.error(f'Unable to read file list {args.file_list}: {e}')
            sys.exit(1)
    is_batch = len(batch_inputs) > 0
    if not is_batch and (not args.input_pdf):
        print('ERROR: Missing input file!')
        print('Usage: python manga_compressor.py INPUT.pdf [OUTPUT.pdf | +suffix]')
        print('Batch: python manga_compressor.py --files a.pdf b.pdf c.pdf')
        print('      or python manga_compressor.py --file-list files.txt')
        sys.exit(1)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not is_batch:
        if args.output_pdf and isinstance(args.output_pdf, str) and args.output_pdf.startswith('+'):
            args.suffix = args.output_pdf
            args.output_pdf = parse_output_filename(args.input_pdf, args.suffix, out_dir=str(out_dir))
        elif not args.output_pdf:
            suffix_to_use = args.suffix if getattr(args, 'suffix', None) else defaults.get('suffix')
            args.output_pdf = parse_output_filename(args.input_pdf, suffix_to_use, out_dir=str(out_dir))
    setup_logging(args.verbose)
    if not 1 <= args.quality <= 100:
        logging.error(f'Quality must be between 1 and 100, received: {args.quality}')
        sys.exit(1)

    def _nearest_power_of_two(n: int) -> int:
        n = max(2, min(256, n))
        allowed = [2, 4, 8, 16, 32, 64, 128, 256]
        return min(allowed, key=lambda x: abs(x - n))
    if getattr(args, 'max_colors_power', None) is not None:
        p = int(args.max_colors_power)
        if not 1 <= p <= 8:
            logging.error(f'--max-colors-power must be between 1 and 8 (2^P => [2..256]), got: {p}')
            sys.exit(1)
        args.max_colors = 2 ** p
    else:
        orig = int(args.max_colors)
        if orig < 2:
            logging.error(f'Max colors must be >= 2, received: {orig}')
            sys.exit(1)
        norm = _nearest_power_of_two(orig)
        if norm != orig:
            logging.info(f'Normalizing max colors to nearest power of two: {orig} -> {norm}')
        args.max_colors = norm
    if args.workers is not None and args.workers < 1:
        logging.error(f'Workers must be >= 1, received: {args.workers}')
        sys.exit(1)
    if not 10 <= args.ram_limit <= 95:
        logging.error(f'RAM limit must be between 10 and 95, received: {args.ram_limit}')
        sys.exit(1)
    print('Manga PDF Compressor - Modular Version')
    print('=' * 55)
    print(f"Target: {args.device} ({DEVICE_PROFILES[args.device]['description']})")
    print(f'Mode: {args.mode}')
    print(f'Quality: {args.quality}, RAM limit: {args.ram_limit}%')
    print()
    compressor = MangaCompressorModular(target_device=args.device, quality=args.quality, max_colors=args.max_colors, compression_mode=args.mode, workers=args.workers, ram_limit_percent=args.ram_limit, tmp_dir=args.tmp_dir)
    if is_batch:
        normalized = []
        for p in batch_inputs:
            p_stripped = p.strip().strip('"').strip("'")
            if not p_stripped:
                continue
            path = Path(p_stripped)
            if not path.is_absolute():
                path = Path.cwd() / path
            if not path.exists():
                logging.warning(f'Input not found, skipping: {path}')
                continue
            if path.suffix.lower() != '.pdf':
                logging.warning(f'Not a PDF, skipping: {path}')
                continue
            normalized.append(path)
        if not normalized:
            logging.error('No valid input PDFs found for batch mode')
            return 1
        failures = 0
        suffix_to_use = args.suffix if getattr(args, 'suffix', None) else defaults.get('suffix', None)
        print(f'Batch mode: {len(normalized)} file(s)')
        print(f'Output directory: {out_dir}')
        for in_path in normalized:
            out_path = Path(parse_output_filename(str(in_path), suffix_to_use, out_dir=str(out_dir)))
            print('-' * 60)
            print(f'Processing: {in_path.name}')
            ok = compressor.compress_pdf(str(in_path), str(out_path))
            if not ok:
                failures += 1
        if failures:
            logging.error(f'Completed with {failures} failure(s)')
            return 1
        return 0
    else:
        success = compressor.compress_pdf(args.input_pdf, args.output_pdf)
        return 0 if success else 1
if __name__ == '__main__':
    mp.set_start_method('spawn', force=True)
    sys.exit(main())