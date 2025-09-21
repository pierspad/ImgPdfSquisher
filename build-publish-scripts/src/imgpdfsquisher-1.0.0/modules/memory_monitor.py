import gc
import logging
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False
from .config import DEFAULT_RAM_LIMIT_PERCENT, MIN_BATCH_SIZE, MAX_BATCH_SIZE, ESTIMATED_MB_PER_PAGE

class MemoryMonitor:

    def __init__(self, ram_limit_percent: int=DEFAULT_RAM_LIMIT_PERCENT):
        self.ram_limit_percent = ram_limit_percent
        if HAS_PSUTIL:
            total_ram = psutil.virtual_memory().total
            self.total_ram_gb = total_ram / 1024 ** 3
            self.max_ram_gb = self.total_ram_gb * (ram_limit_percent / 100)
            logging.info(f'Sistema: {self.total_ram_gb:.1f}GB RAM totale, limite impostato: {self.max_ram_gb:.1f}GB ({ram_limit_percent}%)')
        else:
            self.total_ram_gb = 8.0
            self.max_ram_gb = self.total_ram_gb * (ram_limit_percent / 100)
            logging.warning(f'psutil non disponibile, uso valori conservativi: {self.total_ram_gb:.1f}GB')

    def get_current_usage_gb(self) -> float:
        if HAS_PSUTIL:
            return psutil.virtual_memory().used / 1024 ** 3
        else:
            return self.total_ram_gb * 0.4

    def get_available_gb(self) -> float:
        return max(0, self.max_ram_gb - self.get_current_usage_gb())

    def can_process_batch(self, batch_size: int, mb_per_page: float=ESTIMATED_MB_PER_PAGE) -> bool:
        required_gb = batch_size * mb_per_page / 1024
        available_gb = self.get_available_gb()
        return available_gb >= required_gb

    def calculate_optimal_batch_size(self, total_pages: int, mb_per_page: float=ESTIMATED_MB_PER_PAGE) -> int:
        available_gb = self.get_available_gb()
        max_pages_by_ram = int(available_gb * 1024 / mb_per_page)
        optimal_size = max(MIN_BATCH_SIZE, min(MAX_BATCH_SIZE, max_pages_by_ram))
        optimal_size = min(optimal_size, total_pages)
        logging.info(f'Batch size ottimale calcolato: {optimal_size} pagine (RAM disponibile: {available_gb:.1f} GB)')
        return optimal_size

    def force_gc(self):
        gc.collect()
        if HAS_PSUTIL:
            current_gb = self.get_current_usage_gb()
            logging.debug(f'Memoria dopo GC: {current_gb:.1f}GB')