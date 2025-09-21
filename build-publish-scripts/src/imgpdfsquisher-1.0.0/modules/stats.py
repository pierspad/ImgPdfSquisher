import time
from dataclasses import dataclass, field
from typing import Dict

@dataclass
class TimingStats:
    extraction_time: float = 0.0
    compression_time: float = 0.0
    writing_time: float = 0.0
    total_time: float = 0.0
    pdf_analysis_time: float = 0.0
    image_processing_time: float = 0.0
    memory_management_time: float = 0.0

    def get_breakdown(self) -> Dict[str, float]:
        return {'extraction': self.extraction_time, 'compression': self.compression_time, 'writing': self.writing_time, 'pdf_analysis': self.pdf_analysis_time, 'image_processing': self.image_processing_time, 'memory_management': self.memory_management_time, 'total': self.total_time}

@dataclass
class CompressionStats:
    pages_processed: int = 0
    pages_total: int = 0
    original_size_mb: float = 0.0
    compressed_size_mb: float = 0.0
    start_time: float = field(default_factory=time.time)
    timing: TimingStats = field(default_factory=TimingStats)

    def compression_ratio(self) -> float:
        if self.original_size_mb > 0:
            return self.compressed_size_mb / self.original_size_mb
        return 1.0

    def space_saved_mb(self) -> float:
        return max(0, self.original_size_mb - self.compressed_size_mb)

    def is_compression_effective(self) -> bool:
        return self.compression_ratio() < 0.95

    def elapsed_time(self) -> float:
        return time.time() - self.start_time

    def pages_per_second(self) -> float:
        elapsed = self.elapsed_time()
        return self.pages_processed / elapsed if elapsed > 0 else 0

    def eta_seconds(self) -> float:
        if self.pages_processed > 0 and self.pages_total > self.pages_processed:
            rate = self.pages_per_second()
            remaining_pages = self.pages_total - self.pages_processed
            return remaining_pages / rate if rate > 0 else 0
        return 0