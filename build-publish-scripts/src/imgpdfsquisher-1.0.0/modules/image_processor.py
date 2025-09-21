import io
import logging
from typing import Dict, Any, Tuple
from PIL import Image, ImageEnhance
from .config import SHARPENING_FACTOR, GRAYSCALE_THRESHOLD, GRAYSCALE_COLOR_TOLERANCE, BW_THRESHOLD, BW_QUALITY, BW_DETECTION_THRESHOLD

class ImageProcessor:

    def __init__(self, device_profile: Dict[str, Any], quality: int, max_colors: int, compression_mode: str):
        self.device_profile = device_profile
        self.quality = max(1, min(100, quality + device_profile.get('quality_adjust', 0)))
        self.max_colors = max_colors
        self.compression_mode = compression_mode
        self.target_size = device_profile['size']
        self.sharpening = device_profile.get('sharpening', 1.0)
        logging.debug(f'ImageProcessor configurato: qualità={self.quality}, target_size={self.target_size}, sharpening={self.sharpening}')

    def optimize_image(self, image: Image.Image) -> Image.Image:
        if image.mode not in ['RGB', 'L']:
            image = image.convert('RGB')
        target_width, target_height = self.target_size
        img_width, img_height = image.size
        scale_w = target_width / img_width
        scale_h = target_height / img_height
        scale = min(scale_w, scale_h)
        if scale < 1.0:
            new_width = int(img_width * scale)
            new_height = int(img_height * scale)
            image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
        if self.sharpening != 1.0:
            enhancer = ImageEnhance.Sharpness(image)
            image = enhancer.enhance(self.sharpening)
        return image

    def compress_image(self, image: Image.Image) -> Tuple[Tuple[int, int], bytes]:
        if self.compression_mode == 'bw' or (self.compression_mode == 'auto' and self._is_pure_bw(image)):
            if image.mode != 'L':
                image = image.convert('L')
            threshold_func = lambda x: 255 if x > BW_THRESHOLD else 0
            image = image.point(threshold_func, mode='1')
            buffer = io.BytesIO()
            try:
                image.save(buffer, format='PNG', optimize=True, bits=1)
            except Exception:
                image = image.convert('L')
                image.save(buffer, format='PNG', optimize=True)
        elif self.compression_mode == 'grayscale' or (self.compression_mode == 'auto' and self._is_mostly_grayscale(image)):
            if image.mode != 'L':
                image = image.convert('L')
            buffer = io.BytesIO()
            image.save(buffer, format='JPEG', quality=self.quality, optimize=True, progressive=True)
        else:
            if image.mode != 'RGB':
                image = image.convert('RGB')
            if self.max_colors < 256 * 256 * 256:
                image = image.quantize(colors=self.max_colors, method=Image.Quantize.MEDIANCUT)
                image = image.convert('RGB')
            buffer = io.BytesIO()
            image.save(buffer, format='JPEG', quality=self.quality, optimize=True, progressive=True)
        return (image.size, buffer.getvalue())

    def _is_pure_bw(self, image: Image.Image) -> bool:
        try:
            if image.mode != 'L':
                gray_image = image.convert('L')
            else:
                gray_image = image
            sample_size = min(image.size[0] * image.size[1], 10000)
            if sample_size < image.size[0] * image.size[1]:
                sample_ratio = (sample_size / (image.size[0] * image.size[1])) ** 0.5
                sample_width = max(1, int(image.size[0] * sample_ratio))
                sample_height = max(1, int(image.size[1] * sample_ratio))
                gray_image = gray_image.resize((sample_width, sample_height), Image.Resampling.NEAREST)
            histogram = gray_image.histogram()
            black_pixels = sum(histogram[:15])
            white_pixels = sum(histogram[240:])
            total_pixels = sum(histogram)
            bw_ratio = (black_pixels + white_pixels) / total_pixels if total_pixels > 0 else 0
            return bw_ratio > BW_DETECTION_THRESHOLD
        except Exception as e:
            logging.debug(f"Errore nell'analisi B/W: {e}")
            return False

    def _is_mostly_grayscale(self, image: Image.Image) -> bool:
        try:
            if image.mode == 'L':
                return True
            if image.mode != 'RGB':
                image = image.convert('RGB')
            sample_size = min(image.size[0] * image.size[1], 5000)
            if sample_size < image.size[0] * image.size[1]:
                sample_ratio = (sample_size / (image.size[0] * image.size[1])) ** 0.5
                sample_width = max(1, int(image.size[0] * sample_ratio))
                sample_height = max(1, int(image.size[1] * sample_ratio))
                image = image.resize((sample_width, sample_height), Image.Resampling.NEAREST)
            width, height = image.size
            total_pixels = 0
            grayscale_pixels = 0
            step = 5
            for y in range(0, height, step):
                for x in range(0, width, step):
                    try:
                        r, g, b = image.getpixel((x, y))
                        total_pixels += 1
                        if abs(r - g) <= GRAYSCALE_COLOR_TOLERANCE and abs(r - b) <= GRAYSCALE_COLOR_TOLERANCE and (abs(g - b) <= GRAYSCALE_COLOR_TOLERANCE):
                            grayscale_pixels += 1
                    except:
                        continue
            grayscale_ratio = grayscale_pixels / total_pixels if total_pixels > 0 else 0
            return grayscale_ratio > GRAYSCALE_THRESHOLD
        except Exception as e:
            logging.debug(f"Errore nell'analisi grayscale: {e}")
            return False