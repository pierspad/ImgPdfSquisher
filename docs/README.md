
# Imgpdfsquisher [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Imgpdfsquisher** is a Python application for efficiently reducing PDF file sizes by optimizing high-resolution images. It provides both a user-friendly GUI and a powerful command-line interface.

![Screenshot of the app](image.png)

## Features

- **Intelligent compression**: Automatic image type detection and optimization
- **Multiple device profiles**: Pre-configured settings for tablets, smartphones, e-readers
- **Batch processing**: Process multiple PDF files simultaneously
- **GUI and CLI modes**: Choose your preferred interface
- **Quality presets**: From minimal to ultra-high quality compression
- **Multi-language support**: Available in 11 languages

## Quick Start

### GUI Mode
Simply run the application to open the graphical interface:
```bash
python gui_app.py
```

### CLI Mode
Basic compression (default tablet 10" profile):
```bash
python manga_compressor.py input.pdf output.pdf
```

Compress for smartphone with maximum compression:
```bash
python manga_compressor.py input.pdf output.pdf --device phone --mode bw
```

High-quality compression for e-reader:
```bash
python manga_compressor.py input.pdf output.pdf --device ereader --quality 95
```

Batch processing multiple files:
```bash
python manga_compressor.py --files *.pdf --out-dir compressed
```

Process files from a list:
```bash
python manga_compressor.py --file-list files.txt --suffix _compressed
```

### CLI Options
- `--device`: Target device (phone, tablet_7, tablet_10, ereader, desktop)
- `--mode`: Compression mode (auto, bw, grayscale, color)
- `--quality`: JPEG quality 1-100 (default: 85)
- `--workers`: Number of parallel workers (default: auto)
- `--ram-limit`: RAM usage limit percentage (default: 75)
- `--verbose`: Enable detailed logging

## Installation

### From Source
1. Ensure you have Python 3.13+ installed
2. Clone the repository:
   ```bash
   git clone https://github.com/pierspad/imgpdfsquisher.git
   cd imgpdfsquisher
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

### For Arch Linux (AUR)
```bash
yay -S imgpdfsquisher
# or
paru -S imgpdfsquisher
```

## Contributing
Pull requests are welcome! We especially appreciate help with:
- Translations to additional languages
- Packaging for Windows/macOS and other Linux distributions
- Bug fixes and feature improvements

For major changes, please open an issue first to discuss your ideas.

## License
This project is licensed under the MIT License – see the [LICENSE](LICENSE) file for details.