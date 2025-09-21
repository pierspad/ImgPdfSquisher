DEFAULT_QUALITY = 85
DEFAULT_MAX_COLORS = 256
PDF_DPI = 200
SHARPENING_FACTOR = 1.1
GRAYSCALE_THRESHOLD = 0.95
GRAYSCALE_COLOR_TOLERANCE = 10
BW_THRESHOLD = 200
BW_QUALITY = 95
BW_DETECTION_THRESHOLD = 0.85
DEFAULT_RAM_LIMIT_PERCENT = 75
MIN_BATCH_SIZE = 3
MAX_BATCH_SIZE = 100
ESTIMATED_MB_PER_PAGE = 12
COMPRESSION_MODES = {
	'auto': 'Automatically picks BW, Grayscale or Color per image for best size/quality',
	'bw': 'Pure black/white (1-bit PNG) — best for line art and scanned B/W manga',
	'grayscale': '8-bit grayscale JPEG — for pages without significant colors',
	'color': 'Full-color JPEG — for color pages/covers'
}
DEVICE_PROFILES = {'phone': {'size': (1080, 1920), 'dpi': 400, 'quality_adjust': 0, 'sharpening': 1.2, 'description': 'Smartphone (portrait)'}, 'tablet_7': {'size': (1200, 1920), 'dpi': 323, 'quality_adjust': 5, 'sharpening': 1.1, 'description': 'Tablet 7 pollici'}, 'tablet_10': {'size': (1600, 2560), 'dpi': 300, 'quality_adjust': 10, 'sharpening': 1.0, 'description': 'Tablet 10 pollici (default)'}, 'tablet_12': {'size': (2048, 2732), 'dpi': 264, 'quality_adjust': 15, 'sharpening': 0.9, 'description': 'Tablet 12 pollici'}, 'ereader': {'size': (1404, 1872), 'dpi': 300, 'quality_adjust': -10, 'sharpening': 1.3, 'description': 'E-reader (carta digitale)'}, 'laptop': {'size': (1920, 1080), 'dpi': 96, 'quality_adjust': 20, 'sharpening': 0.8, 'description': 'Laptop/notebook'}, 'desktop': {'size': (2560, 1440), 'dpi': 109, 'quality_adjust': 25, 'sharpening': 0.7, 'description': 'Monitor desktop'}}