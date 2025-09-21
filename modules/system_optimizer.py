import logging
import multiprocessing as mp
import os
import tempfile
import time
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False
from .config import MIN_BATCH_SIZE, MAX_BATCH_SIZE, ESTIMATED_MB_PER_PAGE

class SystemOptimizer:

    def __init__(self):
        self.cpu_count = mp.cpu_count()
        self.has_ssd = self._detect_ssd()
        self.ram_gb = self._get_total_ram()
        self.io_performance = self._benchmark_io()
        logging.info(f"Sistema rilevato: {self.cpu_count} CPU cores, {self.ram_gb:.1f}GB RAM, SSD: {('Sì' if self.has_ssd else 'No')}, I/O: {self.io_performance}")

    def _get_total_ram(self) -> float:
        if HAS_PSUTIL:
            return psutil.virtual_memory().total / 1024 ** 3
        else:
            return 8.0

    def _detect_ssd(self) -> bool:
        if not HAS_PSUTIL:
            return False
        try:
            for partition in psutil.disk_partitions():
                if partition.mountpoint == '/':
                    return True
        except:
            return False

    def _benchmark_io(self) -> str:
        try:
            test_size = 1024 * 1024
            test_data = b'0' * test_size
            with tempfile.NamedTemporaryFile() as tmp:
                start_time = time.time()
                tmp.write(test_data)
                tmp.flush()
                os.fsync(tmp.fileno())
                write_time = time.time() - start_time
                start_time = time.time()
                tmp.seek(0)
                tmp.read()
                read_time = time.time() - start_time
                avg_time = (write_time + read_time) / 2
                if avg_time < 0.01:
                    return 'Veloce'
                elif avg_time < 0.05:
                    return 'Medio'
                else:
                    return 'Lento'
        except:
            return 'Sconosciuto'

    def get_optimal_workers(self, task_type: str) -> int:
        if task_type == 'extraction':
            if self.has_ssd:
                return min(self.cpu_count, 4)
            else:
                return min(self.cpu_count, 2)
        elif task_type == 'compression':
            return self.cpu_count
        else:
            return max(1, self.cpu_count // 2)

    def get_optimal_batch_size(self, total_pages: int, available_ram_gb: float) -> int:
        ram_pages = int(available_ram_gb * 1024 / ESTIMATED_MB_PER_PAGE)
        if self.has_ssd:
            storage_multiplier = 1.5
        else:
            storage_multiplier = 0.8
        optimal_size = int(ram_pages * storage_multiplier)
        optimal_size = max(MIN_BATCH_SIZE, min(MAX_BATCH_SIZE, optimal_size))
        optimal_size = min(optimal_size, total_pages)
        return optimal_size