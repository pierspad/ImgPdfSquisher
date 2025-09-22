import gc
import time
from typing import Tuple, Any
from PIL import Image
from .image_processor import ImageProcessor

def process_image_worker(args) -> Tuple[Tuple[int, int], bytes]:
    image_or_path, processor_config = args
    processor = ImageProcessor(device_profile=processor_config['device_profile'], quality=processor_config['quality'], max_colors=processor_config['max_colors'], compression_mode=processor_config['compression_mode'])
    if isinstance(image_or_path, str):
        image = Image.open(image_or_path)
    else:
        image = image_or_path
    optimized = processor.optimize_image(image)
    size, compressed = processor.compress_image(optimized)
    try:
        image.close()
    except Exception:
        pass
    del optimized, image
    gc.collect()
    return (size, compressed)

def process_image_worker_with_timing(args) -> Tuple[Tuple[int, int], bytes, float]:
    start_time = time.time()
    image_or_path, processor_config = args
    processor = ImageProcessor(device_profile=processor_config['device_profile'], quality=processor_config['quality'], max_colors=processor_config['max_colors'], compression_mode=processor_config['compression_mode'])
    if isinstance(image_or_path, str):
        image = Image.open(image_or_path)
    else:
        image = image_or_path
    optimized = processor.optimize_image(image)
    size, compressed = processor.compress_image(optimized)
    try:
        image.close()
    except Exception:
        pass
    del optimized, image
    gc.collect()
    processing_time = time.time() - start_time
    return (size, compressed, processing_time)