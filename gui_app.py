import os
import sys
import json
import math
from pathlib import Path
from threading import Thread
from typing import Optional
from PySide6.QtCore import Qt, Signal, QObject, QEvent, QTimer, QSize, QPointF
from PySide6.QtGui import QIcon, QDragEnterEvent, QDropEvent, QPixmap, QPainter, QTransform, QMouseEvent, QWheelEvent, QCursor
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QWidget, QFileDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QListWidget, QListWidgetItem, QComboBox, QSpinBox, QSlider, QProgressBar, QLineEdit, QMessageBox, QCheckBox, QAbstractItemView, QDoubleSpinBox, QButtonGroup, QGraphicsView, QGraphicsScene, QGraphicsPixmapItem
from modules import DEVICE_PROFILES, COMPRESSION_MODES, DEFAULT_QUALITY, DEFAULT_MAX_COLORS, PDFExtractor, ImageProcessor
from PIL import Image
import tempfile
from manga_compressor import MangaCompressorModular, parse_output_filename, load_default_config, save_default_config

class PreviewSyncController(QObject):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.zoom_rel = 1.0
        self.cx = 0.5
        self.cy = 0.5
        self._views = []
        self._updating = False

    def register(self, view: 'ImageZoomView'):
        if view not in self._views:
            self._views.append(view)

    def set_zoom_rel(self, zoom_rel: float):
        self.zoom_rel = max(1.0, min(zoom_rel, 20.0))
        self._notify()

    def multiply_zoom(self, factor: float):
        self.set_zoom_rel(self.zoom_rel * factor)

    def set_center_ratio(self, cx: float, cy: float):
        self.cx = max(0.0, min(cx, 1.0))
        self.cy = max(0.0, min(cy, 1.0))
        self._notify()

    def _notify(self):
        if self._updating:
            return
        try:
            self._updating = True
            for v in self._views:
                v.apply_sync()
        finally:
            self._updating = False
        self.changed.emit()

class ImageZoomView(QGraphicsView):

    def __init__(self, controller: PreviewSyncController, parent=None):
        super().__init__(parent)
        self.setObjectName('ImageZoomView')
        self._controller = controller
        controller.register(self)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pix_item: QGraphicsPixmapItem | None = None
        self.setFixedSize(270, 430)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setDragMode(QGraphicsView.NoDrag)
        self._dragging = False
        self._last_pos = None
        self._drag_anchor_scene = None
        self._drag_start_center = None
        self._last_cx = 0.5
        self._last_cy = 0.5
        self._fit_scale = 1.0

    def set_pixmap(self, pm: QPixmap):
        self._scene.clear()
        self._pix_item = None
        if pm and (not pm.isNull()):
            self._pix_item = self._scene.addPixmap(pm)
            self._pix_item.setTransformationMode(Qt.SmoothTransformation)
            self._scene.setSceneRect(pm.rect())
            self._compute_fit_scale()
            self.apply_sync()
        else:
            self._scene.setSceneRect(0, 0, 1, 1)
            self.resetTransform()

    def _compute_fit_scale(self):
        if not self._pix_item:
            self._fit_scale = 1.0
            return
        rect = self._pix_item.boundingRect()
        if rect.width() <= 0 or rect.height() <= 0:
            self._fit_scale = 1.0
            return
        vw = self.viewport().width()
        vh = self.viewport().height()
        self._fit_scale = max(vw / rect.width(), vh / rect.height())
        self.apply_sync()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._compute_fit_scale()

    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()
        if delta == 0:
            return
        factor = 1.25 if delta > 0 else 0.8
        self._controller.multiply_zoom(factor)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self._last_pos = event.position().toPoint()
            try:
                self._drag_anchor_scene = self.mapToScene(self._last_pos)
                self._drag_start_center = self.mapToScene(self.viewport().rect().center())
                br = self._pix_item.boundingRect() if self._pix_item else None
                if br and br.width() > 0 and (br.height() > 0):
                    self._last_cx = (self._drag_start_center.x() - br.left()) / br.width()
                    self._last_cy = (self._drag_start_center.y() - br.top()) / br.height()
            except Exception:
                self._drag_anchor_scene = None
                self._drag_start_center = None
            self.setCursor(QCursor(Qt.ClosedHandCursor))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._dragging and self._pix_item:
            try:
                curr_scene_under_mouse = self.mapToScene(event.position().toPoint())
                if self._drag_anchor_scene is None or self._drag_start_center is None:
                    self._drag_anchor_scene = curr_scene_under_mouse
                    self._drag_start_center = self.mapToScene(self.viewport().rect().center())
                delta_scene = curr_scene_under_mouse - self._drag_anchor_scene
                min_threshold = 1.0
                if abs(delta_scene.x()) < min_threshold and abs(delta_scene.y()) < min_threshold:
                    return
                new_center = self._drag_start_center - delta_scene
                br = self._pix_item.boundingRect()
                if br.width() > 0 and br.height() > 0:
                    cx = (new_center.x() - br.left()) / br.width()
                    cy = (new_center.y() - br.top()) / br.height()
                    cx = max(0.0, min(1.0, cx))
                    cy = max(0.0, min(1.0, cy))
                    if hasattr(self, '_last_cx') and hasattr(self, '_last_cy'):
                        smoothing_factor = 0.3
                        cx = cx * (1 - smoothing_factor) + self._last_cx * smoothing_factor
                        cy = cy * (1 - smoothing_factor) + self._last_cy * smoothing_factor
                    self._last_cx = cx
                    self._last_cy = cy
                    self._controller.set_center_ratio(cx, cy)
                event.accept()
                return
            except Exception:
                pass
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            self.setCursor(QCursor(Qt.ArrowCursor))
            self._drag_anchor_scene = None
            self._drag_start_center = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def apply_sync(self):
        if not self._pix_item:
            return
        scale = self._fit_scale * self._controller.zoom_rel
        tr = QTransform()
        tr.scale(scale, scale)
        self.setTransform(tr)
        br = self._pix_item.boundingRect()
        cx = br.left() + self._controller.cx * br.width()
        cy = br.top() + self._controller.cy * br.height()
        target = QPointF(cx, cy)
        vis = self.mapToScene(self.viewport().rect()).boundingRect()
        half_w = vis.width() / 2.0
        half_h = vis.height() / 2.0
        min_x = br.left() + half_w
        max_x = br.right() - half_w
        min_y = br.top() + half_h
        max_y = br.bottom() - half_h
        if br.width() <= vis.width():
            target.setX(br.center().x())
        else:
            target.setX(max(min_x, min(max_x, target.x())))
        if br.height() <= vis.height():
            target.setY(br.center().y())
        else:
            target.setY(max(min_y, min(max_y, target.y())))
        self.centerOn(target)

class Signals(QObject):
    log = Signal(str)
    progress = Signal(int, int, float)
    file_done = Signal(str, str)
    error = Signal(str)
    all_done = Signal()

class CompressorWorker(Thread):

    def __init__(self, files, out_dir: Path, tmp_dir: Path, device: str, mode: str, quality: int, max_colors: int, workers: Optional[int], ram_limit: int, signals: Signals, preset_key: Optional[str]=None):
        super().__init__(daemon=True)
        self.files = files
        self.out_dir = out_dir
        self.tmp_dir = tmp_dir
        self.device = device
        self.mode = mode
        self.quality = quality
        self.max_colors = max_colors
        self.workers = workers
        self.ram_limit = ram_limit
        self.signals = signals
        self.preset_key = preset_key
        from threading import Event
        self._stop_event = Event()
        self.compressor = None

    def progress_cb(self, payload: dict):
        try:
            evt = payload.get('event')
            if evt == 'progress':
                self.signals.progress.emit(int(payload.get('pages_processed', 0)), int(payload.get('pages_total', 0)), float(payload.get('percent', 0.0)))
            elif evt == 'file_done':
                self.signals.file_done.emit(payload.get('file', ''), payload.get('output', ''))
            elif evt == 'error':
                self.signals.error.emit(payload.get('message', 'Unknown error'))
        except Exception:
            pass

    def run(self):
        try:
            self.compressor = MangaCompressorModular(target_device=self.device, quality=self.quality, max_colors=self.max_colors, compression_mode=self.mode, workers=self.workers, ram_limit_percent=self.ram_limit, tmp_dir=str(self.tmp_dir), progress_callback=self.progress_cb, stop_checker=lambda: self._stop_event.is_set())
            failures = 0
            for f in self.files:
                in_path = Path(f)
                if not in_path.exists() or in_path.suffix.lower() != '.pdf':
                    self.signals.log.emit(f'Skipping invalid file: {f}')
                    continue
                if self.preset_key:
                    suffix = f'_compressed_{self.preset_key}'
                else:
                    suffix = f'_compressed_{self.mode}'
                out_path = Path(parse_output_filename(str(in_path), suffix, out_dir=str(self.out_dir)))
                self.signals.log.emit(f'Processing: {in_path.name}')
                ok = self.compressor.compress_pdf(str(in_path), str(out_path))
                if not ok:
                    failures += 1
            if failures:
                self.signals.error.emit(f'Completed with {failures} failure(s)')
            self.signals.all_done.emit()
        except Exception as e:
            self.signals.error.emit(str(e))
            self.signals.all_done.emit()

    def request_stop(self):
        try:
            self._stop_event.set()
            if self.compressor:
                self.compressor.request_stop()
        except Exception:
            pass

class MangaCompressorGUI(QWidget):

    def __init__(self):
        super().__init__()
        self.setWindowTitle('Manga PDF Compressor')
        self.setFixedSize(1030, 810)
        self.setWindowIcon(QIcon())
        self.presets = self._load_presets()
        self.signals = Signals()
        self.signals.log.connect(self.on_log)
        self.signals.progress.connect(self.on_progress)
        self.signals.file_done.connect(self.on_file_done)
        self.signals.error.connect(self.on_error)
        self.signals.all_done.connect(self.on_all_done)
        self.defaults = load_default_config() or {}
        if not self.defaults:
            self.defaults = {'device': 'tablet_10', 'mode': 'auto', 'quality': DEFAULT_QUALITY, 'max_colors': DEFAULT_MAX_COLORS, 'workers': None, 'ram_limit': 75, 'suffix': None, 'out_dir': str((Path.cwd() / 'compressed').resolve()), 'tmp_dir': str((Path.cwd() / 'tmp').resolve()), 'theme': 'dark', 'language': 'en', 'ui_mode': 'simple'}
            try:
                args = type('Args', (), self.defaults)
                save_default_config(args)
            except Exception:
                pass
        self.theme = self.defaults.get('theme', 'dark')
        self.language = self.defaults.get('language', 'en')
        self.ui_mode = self.defaults.get('ui_mode', 'simple')
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._persist_defaults_now)
        # Load i18n dictionaries from external JSON files (i18n/*.json)
        self._load_i18n()
        self._build_ui()
        self._load_defaults_into_ui()
        self.apply_theme(self.theme)
        self.apply_language(self.language)
        self.worker: Optional[CompressorWorker] = None

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(10)
        left_container = QWidget()
        layout = QVBoxLayout(left_container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        root.addWidget(left_container, 1)
        files_row = QHBoxLayout()
        self.files_list = QListWidget()
        self.files_list.setAcceptDrops(True)
        self.files_list.setDragEnabled(False)
        self.files_list.viewport().setAcceptDrops(True)
        self.files_list.setDragDropMode(QAbstractItemView.DropOnly)
        self.files_list.setAlternatingRowColors(True)
        self.files_list.setUniformItemSizes(True)
        self.files_list.setToolTip('')
        try:
            row_h = self.files_list.fontMetrics().height() + 10
            self.files_list.setFixedHeight(row_h * 5 + 6)
        except Exception:
            self.files_list.setFixedHeight(5 * 28 + 6)
        add_btn = QPushButton()
        add_btn.setObjectName('addButton')
        add_btn.setIcon(self._icon('file-plus'))
        rem_btn = QPushButton()
        rem_btn.setObjectName('removeButton')
        rem_btn.setIcon(self._icon('trash'))
        clear_btn = QPushButton()
        clear_btn.setObjectName('clearButton')
        clear_btn.setIcon(self._icon('trash'))
        add_btn.clicked.connect(self.on_add_files)
        rem_btn.clicked.connect(self.on_remove_selected)
        clear_btn.clicked.connect(self.files_list.clear)
        files_col = QVBoxLayout()
        files_col.addWidget(self.files_list)
        btn_row = QHBoxLayout()
        btn_row.addWidget(add_btn)
        btn_row.addWidget(rem_btn)
        btn_row.addWidget(clear_btn)
        btn_row.addStretch(1)
        files_col.addLayout(btn_row)
        self.suggestion_bar = QWidget()
        self.suggestion_bar.setObjectName('suggestionBar')
        sug_layout = QHBoxLayout(self.suggestion_bar)
        sug_layout.setContentsMargins(8, 6, 8, 6)
        sug_layout.setSpacing(8)
        self.suggestion_label = QLabel('')
        self.suggestion_label.setObjectName('suggestionLabel')
        self.suggestion_apply_btn = QPushButton()
        self.suggestion_apply_btn.setObjectName('suggestionApply')
        self.suggestion_apply_btn.clicked.connect(self.on_apply_suggestion)
        self.suggestion_apply_btn.setVisible(False)
        sug_layout.addWidget(self.suggestion_label, 1)
        sug_layout.addWidget(self.suggestion_apply_btn)
        self.suggestion_bar.setMinimumHeight(40)
        try:
            self.suggestion_apply_btn.setMinimumWidth(90)
            self.suggestion_apply_btn.setMinimumHeight(28)
        except Exception:
            pass
        self.suggestion_bar.setVisible(True)
        files_col.addWidget(self.suggestion_bar)
        files_row.addLayout(files_col)
        layout.addLayout(files_row)
        self.out_dir_edit = QLineEdit()
        self.tmp_dir_edit = QLineEdit()
        self.out_dir_edit.setPlaceholderText(str((Path.cwd() / 'compressed').resolve()))
        self.tmp_dir_edit.setPlaceholderText(str((Path.cwd() / 'tmp').resolve()))
        out_row = QHBoxLayout()
        self.lbl_out = QLabel()
        self.lbl_out.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        out_row.addWidget(self.lbl_out)
        out_row.addWidget(self.out_dir_edit, 1)
        self.out_btn = QPushButton()
        self.out_btn.setObjectName('outButton')
        self.out_btn.clicked.connect(lambda: self.choose_dir(self.out_dir_edit))
        self.out_btn.setIcon(self._icon('folder-open'))
        out_row.addWidget(self.out_btn)
        layout.addLayout(out_row)
        tmp_row = QHBoxLayout()
        self.lbl_tmp = QLabel()
        self.lbl_tmp.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        tmp_row.addWidget(self.lbl_tmp)
        tmp_row.addWidget(self.tmp_dir_edit, 1)
        self.tmp_btn = QPushButton()
        self.tmp_btn.setObjectName('tmpButton')
        self.tmp_btn.clicked.connect(lambda: self.choose_dir(self.tmp_dir_edit))
        self.tmp_btn.setIcon(self._icon('folder-open'))
        tmp_row.addWidget(self.tmp_btn)
        layout.addLayout(tmp_row)
        self.simple_panel = QWidget()
        sp_v = QVBoxLayout(self.simple_panel)
        sp_v.setContentsMargins(0, 0, 0, 0)
        sp_presets = QHBoxLayout()
        self.lbl_presets = QLabel()
        sp_presets.addWidget(self.lbl_presets)
        self.btn_p_min = QPushButton()
        self.btn_p_very_low = QPushButton()
        self.btn_p_low = QPushButton()
        self.btn_p_normal = QPushButton()
        self.btn_p_high = QPushButton()
        self.btn_p_very_high = QPushButton()
        self.btn_p_ultra = QPushButton()
        self.preset_group = QButtonGroup(self)
        self.preset_group.setExclusive(True)
        for b in (self.btn_p_min, self.btn_p_very_low, self.btn_p_low, self.btn_p_normal, self.btn_p_high, self.btn_p_very_high, self.btn_p_ultra):
            b.clicked.connect(self.on_preset_click)
            b.setCheckable(True)
            b.setProperty('preset', 'true')
            self.preset_group.addButton(b)
            sp_presets.addWidget(b)
        sp_presets.addStretch(1)
        sp_v.addLayout(sp_presets)
        self.btn_p_normal.setChecked(True)
        sp_dev = QHBoxLayout()
        self.lbl_brand = QLabel()
        self.simple_brand_combo = QComboBox()
        self.lbl_model = QLabel()
        self.simple_model_combo = QComboBox()
        self.simple_brand_combo.currentIndexChanged.connect(self._on_simple_brand_changed)
        self.simple_model_combo.currentIndexChanged.connect(self._on_simple_model_changed)
        sp_dev.addWidget(self.lbl_brand)
        sp_dev.addWidget(self.simple_brand_combo)
        sp_dev.addWidget(self.lbl_model)
        sp_dev.addWidget(self.simple_model_combo, 1)
        sp_v.addLayout(sp_dev)
        layout.addWidget(self.simple_panel)
        opts_grid = QGridLayout()
        opts_grid.setHorizontalSpacing(8)
        opts_grid.setVerticalSpacing(6)
        opts_grid.setContentsMargins(4, 0, 4, 0)
        self.device_combo = QComboBox()
        for k, v in DEVICE_PROFILES.items():
            self.device_combo.addItem(f"{k} — {v['description']}", userData=k)
        self.mode_combo = QComboBox()
        # Pre-populate with localization-aware labels
        for k in COMPRESSION_MODES.keys():
            self.mode_combo.addItem(f"{k} — {self._localized_mode_label(k)}", userData=k)
        self.quality_slider = QSlider(Qt.Horizontal)
        self.quality_slider.setRange(1, 100)
        self.quality_slider.setValue(DEFAULT_QUALITY)
        self.quality_slider.setTickInterval(5)
        self.quality_slider.setSingleStep(1)
        self.quality_slider.mousePressEvent = self._slider_jump_to_click(self.quality_slider)
        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(1, 100)
        self.quality_spin.setValue(DEFAULT_QUALITY)
        self.quality_spin.setFixedWidth(70)
        self.quality_spin.setToolTip('JPEG quality. 1 = smaller file, 100 = best quality.')
        self.quality_slider.valueChanged.connect(self.quality_spin.setValue)
        self.quality_spin.valueChanged.connect(self.quality_slider.setValue)
        self.colors_slider = QSlider(Qt.Horizontal)
        self.colors_slider.setRange(1, 8)
        self.colors_slider.setValue(8)
        self.colors_slider.setTickInterval(1)
        self.colors_slider.setSingleStep(1)
        self.colors_slider.mousePressEvent = self._slider_jump_to_click(self.colors_slider)
        self.colors_spin = QSpinBox()
        self.colors_spin.setRange(1, 8)
        self.colors_spin.setValue(8)
        self.colors_spin.setFixedWidth(70)
        self.colors_spin.setToolTip('Palette size as power of two: 1=>2, ... 8=>256.')
        self.colors_slider.valueChanged.connect(self.colors_spin.setValue)
        self.colors_spin.valueChanged.connect(self.colors_slider.setValue)
        import multiprocessing as _mp
        cpu_count = max(1, _mp.cpu_count())
        self.workers_slider = QSlider(Qt.Horizontal)
        self.workers_slider.setRange(0, max(1, min(64, cpu_count)))
        self.workers_slider.setTickInterval(1)
        self.workers_slider.setSingleStep(1)
        self.workers_slider.mousePressEvent = self._slider_jump_to_click(self.workers_slider)
        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(0, max(1, min(64, cpu_count)))
        self.workers_spin.setValue(0)
        self.workers_spin.setFixedWidth(70)
        self.workers_spin.setToolTip(f'Numero di processi di compressione. 0 = automatico (max {cpu_count}).')
        self.workers_slider.valueChanged.connect(self.workers_spin.setValue)
        self.workers_spin.valueChanged.connect(self.workers_slider.setValue)
        self.ram_slider = QSlider(Qt.Horizontal)
        self.ram_slider.setRange(10, 95)
        self.ram_slider.setTickInterval(5)
        self.ram_slider.setSingleStep(1)
        self.ram_slider.mousePressEvent = self._slider_jump_to_click(self.ram_slider)
        self.ram_spin = QSpinBox()
        self.ram_spin.setRange(10, 95)
        self.ram_spin.setValue(75)
        self.ram_spin.setSuffix(' %')
        self.ram_spin.setFixedWidth(70)
        self.ram_spin.setToolTip('Limite massimo di RAM usabile. Valori più alti = più veloci ma più rischiosi.')
        self.ram_slider.valueChanged.connect(self.ram_spin.setValue)
        self.ram_spin.valueChanged.connect(self.ram_slider.setValue)
        self.ram_slider.setValue(75)
        self.save_defaults_chk = QCheckBox()
        self.lbl_device = QLabel()
        self.lbl_mode = QLabel()
        self.lbl_quality = QLabel()
        self.lbl_colors = QLabel()
        self.lbl_workers = QLabel()
        self.lbl_ram = QLabel()
        opts_grid.addWidget(self.lbl_device, 0, 0)
        opts_grid.addWidget(self.device_combo, 0, 1)
        opts_grid.addWidget(self.lbl_mode, 1, 0)
        opts_grid.addWidget(self.mode_combo, 1, 1)
        opts_grid.addWidget(self.lbl_quality, 2, 0)
        qrow = QHBoxLayout()
        qrow.addWidget(self.quality_slider, 1)
        qrow.addWidget(self.quality_spin)
        opts_grid.addLayout(qrow, 2, 1)
        opts_grid.addWidget(self.lbl_colors, 3, 0)
        crow = QHBoxLayout()
        crow.addWidget(self.colors_slider, 1)
        crow.addWidget(self.colors_spin)
        opts_grid.addLayout(crow, 3, 1)
        opts_grid.addWidget(self.lbl_workers, 4, 0)
        wrow = QHBoxLayout()
        wrow.addWidget(self.workers_slider, 1)
        wrow.addWidget(self.workers_spin)
        opts_grid.addLayout(wrow, 4, 1)
        opts_grid.addWidget(self.lbl_ram, 5, 0)
        rrow = QHBoxLayout()
        rrow.addWidget(self.ram_slider, 1)
        rrow.addWidget(self.ram_spin)
        opts_grid.addLayout(rrow, 5, 1)
        opts_grid.addWidget(self.save_defaults_chk, 6, 0, 1, 2)
        layout.addLayout(opts_grid)
        self.advanced_custom_panel = QWidget()
        ac_l = QHBoxLayout(self.advanced_custom_panel)
        ac_l.setContentsMargins(0, 0, 0, 0)
        ac_l.setSpacing(8)
        self.lbl_custom_section = QLabel()
        self.use_custom_chk = QCheckBox()
        ac_l.addWidget(self.lbl_custom_section)
        ac_l.addWidget(self.use_custom_chk)
        ac_l.addStretch(1)
        ac_fields = QHBoxLayout()
        self.custom_name_edit = QLineEdit()
        self.custom_name_edit.setPlaceholderText('Custom')
        self.custom_inches = QDoubleSpinBox()
        self.custom_inches.setRange(4.0, 30.0)
        self.custom_inches.setSingleStep(0.1)
        self.custom_inches.setDecimals(1)
        self.custom_inches.setValue(10.0)
        self.custom_w_spin = QSpinBox()
        self.custom_w_spin.setRange(600, 6000)
        self.custom_w_spin.setValue(1600)
        self.custom_h_spin = QSpinBox()
        self.custom_h_spin.setRange(600, 6000)
        self.custom_h_spin.setValue(2560)
        self.custom_dpi_spin = QSpinBox()
        self.custom_dpi_spin.setRange(96, 600)
        self.custom_dpi_spin.setValue(300)
        ac_fields.addWidget(self.custom_name_edit)
        ac_fields.addWidget(self.custom_inches)
        ac_fields.addWidget(self.custom_w_spin)
        ac_fields.addWidget(self.custom_h_spin)
        ac_fields.addWidget(self.custom_dpi_spin)
        ac_fields.addStretch(1)
        ac_wrap = QVBoxLayout()
        ac_wrap.setContentsMargins(4, 0, 4, 0)
        ac_wrap.setSpacing(4)
        ac_wrap.addWidget(self.advanced_custom_panel)
        ac_wrap.addLayout(ac_fields)
        layout.addLayout(ac_wrap)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)
        log_container = QVBoxLayout()
        log_header = QHBoxLayout()
        log_title = QLabel('Log:')
        log_title.setStyleSheet('font-weight: bold;')
        log_header.addWidget(log_title)
        self.clear_log_btn = QPushButton()
        self.clear_log_btn.setObjectName('clearLogButton')
        self.clear_log_btn.setIcon(self._icon('trash'))
        self.clear_log_btn.setToolTip('Pulisci log')
        self.clear_log_btn.clicked.connect(self.on_clear_log)
        self.clear_log_btn.setMaximumWidth(30)
        log_header.addWidget(self.clear_log_btn)
        log_header.addStretch(1)
        layout.addLayout(log_header)
        self.log_list = QListWidget()
        layout.addWidget(self.log_list, 1)
        btn_row2 = QHBoxLayout()
        self.start_btn = QPushButton()
        self.start_btn.setObjectName('startButton')
        self.start_btn.setIcon(self._icon('play'))
        self.start_btn.clicked.connect(self.on_start)
        self.stop_btn = QPushButton(' Stop')
        self.stop_btn.setIcon(self._icon('stop'))
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.on_stop)
        btn_row2.addWidget(self.start_btn)
        btn_row2.addWidget(self.stop_btn)
        self.open_output_btn = QPushButton()
        self.open_output_btn.setObjectName('openOutputButton')
        self.open_output_btn.setIcon(self._icon('folder-open'))
        self.open_output_btn.clicked.connect(self.on_open_output)
        btn_row2.addWidget(self.open_output_btn)
        btn_row2.addStretch(1)
        self.theme_btn = QPushButton()
        self.theme_btn.setObjectName('themeButton')
        self.theme_btn.setToolTip('')
        self.theme_btn.setIcon(self._icon('moon'))
        self.theme_btn.clicked.connect(self.on_toggle_theme)
        btn_row2.addWidget(self.theme_btn)
        self.language_btn = QPushButton()
        self.language_btn.setObjectName('languageButton')
        self.language_btn.clicked.connect(self.on_cycle_language)
        btn_row2.addWidget(self.language_btn)
        self.mode_btn = QPushButton()
        self.mode_btn.setObjectName('uiModeButton')
        self.mode_btn.setIcon(self._icon('advanced' if self.ui_mode == 'advanced' else 'simple'))
        self.mode_btn.clicked.connect(self.on_toggle_ui_mode)
        btn_row2.addWidget(self.mode_btn)
        layout.addLayout(btn_row2)
        self._build_preview_panel(root)
        self.setAcceptDrops(True)
        self.files_list.installEventFilter(self)

    def _build_preview_panel(self, root_layout):
        from PySide6.QtWidgets import QVBoxLayout, QHBoxLayout
        self.preview_panel = QWidget()
        pv = QVBoxLayout(self.preview_panel)
        pv.setContentsMargins(6, 0, 0, 0)
        pv.setSpacing(8)
        self.preview_title = QLabel('Anteprima qualità')
        pv.addWidget(self.preview_title)
        preview_container1 = QWidget()
        pc1_layout = QVBoxLayout(preview_container1)
        pc1_layout.setContentsMargins(0, 0, 0, 0)
        pc1_layout.setSpacing(4)
        self.preview_label1 = QLabel('Originale')
        self.preview_label1.setAlignment(Qt.AlignCenter)
        self.preview_label1.setStyleSheet('font-weight: bold; color: #8cc8ff;')
        pc1_layout.addWidget(self.preview_label1)
        self._preview_sync = PreviewSyncController()
        self.preview_view1 = ImageZoomView(self._preview_sync)
        self.preview_view1.setObjectName('previewImage1')
        pc1_layout.addWidget(self.preview_view1)
        pv.addWidget(preview_container1)
        preview_container2 = QWidget()
        pc2_layout = QVBoxLayout(preview_container2)
        pc2_layout.setContentsMargins(0, 0, 0, 0)
        pc2_layout.setSpacing(4)
        self.preview_label2 = QLabel('Compressa')
        self.preview_label2.setAlignment(Qt.AlignCenter)
        self.preview_label2.setStyleSheet('font-weight: bold; color: #ff8c69;')
        pc2_layout.addWidget(self.preview_label2)
        self.preview_view2 = ImageZoomView(self._preview_sync)
        self.preview_view2.setObjectName('previewImage2')
        pc2_layout.addWidget(self.preview_view2)
        pv.addWidget(preview_container2)
        self.preview_level_label = QLabel('')
        pv.addWidget(self.preview_level_label)
        pv.addStretch(1)
        root_layout.addWidget(self.preview_panel, 0)
        self._preview_compressed_map = {'minimal': '1_image_compressed_minimal.png', 'very_low': '2_image_compressed_very_low.png', 'low': '3_image_compressed_low.png', 'normal': '4_image_compressed_normal.png', 'high': '5_image_compressed_high.png', 'very_high': '6_image_compressed_very_high.png', 'ultra': '7_image_compressed_ultra.png'}

    def _update_previews(self, preset_key: str):
        try:
            base = Path(__file__).parent / 'assets' / 'previews'
            orig_path = base / 'image.png'
            pm_orig = QPixmap(str(orig_path)) if orig_path.exists() else QPixmap()
            try:
                self.preview_view1.set_pixmap(pm_orig if not pm_orig.isNull() else QPixmap())
            except Exception:
                pass
            comp_name = self._preview_compressed_map.get(preset_key)
            comp_pm = QPixmap()
            if comp_name:
                comp_path = base / comp_name
                if comp_path.exists():
                    comp_pm = QPixmap(str(comp_path))
            try:
                self.preview_view2.set_pixmap(comp_pm if not comp_pm.isNull() else QPixmap())
            except Exception:
                pass
            # Build localized level label: "N - <PresetName>"
            order_map = {
                'minimal': 1,
                'very_low': 2,
                'low': 3,
                'normal': 4,
                'high': 5,
                'very_high': 6,
                'ultra': 7,
            }
            n = order_map.get(preset_key)
            label = self._preset_label_localized(preset_key)
            self.preview_level_label.setText(f"{n} - {label}" if n else label)
        except Exception:
            pass

    def _load_defaults_into_ui(self):
        d = self.defaults
        out_dir = d.get('out_dir') or str(Path.cwd() / 'compressed')
        tmp_dir = d.get('tmp_dir') or str(Path.cwd() / 'tmp')
        self.out_dir_edit.setText(str(Path(out_dir).resolve()))
        self.tmp_dir_edit.setText(str(Path(tmp_dir).resolve()))
        device = d.get('device', 'tablet_10')
        mode = d.get('mode', 'auto')
        self._set_combo_by_data(self.device_combo, device)
        self._load_devices_model_map()
        self._select_brand_model_by_device_key(device)
        self._set_combo_by_data(self.mode_combo, mode)
        self.quality_spin.setValue(int(d.get('quality', DEFAULT_QUALITY)))
        try:
            c = int(d.get('max_colors', DEFAULT_MAX_COLORS))
        except Exception:
            c = DEFAULT_MAX_COLORS
        c = max(2, min(256, c))
        allowed = [2, 4, 8, 16, 32, 64, 128, 256]
        P = allowed.index(min(allowed, key=lambda x: abs(x - c))) + 1
        self.colors_spin.setValue(P)
        self.workers_spin.setValue(int(d.get('workers', 0)) if d.get('workers') is not None else 0)
        self.ram_spin.setValue(int(d.get('ram_limit', 75)))
        self.ram_slider.setValue(int(d.get('ram_limit', 75)))
        self._apply_ui_mode(self.ui_mode)
        self._update_previews(self._current_preset_key() or 'normal')

    def _set_combo_by_data(self, combo: QComboBox, data: str):
        for i in range(combo.count()):
            if combo.itemData(i) == data:
                combo.setCurrentIndex(i)
                return

    def _icon(self, name: str, size: int=20, color_override=None) -> QIcon:
        try:
            svg_path = Path(__file__).parent / f'assets/icons/{name}.svg'
            if not svg_path.exists():
                return QIcon()
            renderer = QSvgRenderer(str(svg_path))
            w = h = size
            pm = QPixmap(w, h)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            renderer.render(p)
            p.setCompositionMode(QPainter.CompositionMode_SourceIn)
            color = color_override if color_override is not None else Qt.white if self.theme == 'dark' else Qt.black
            p.fillRect(pm.rect(), color)
            p.end()
            icon = QIcon(pm)
            return icon
        except Exception:
            return QIcon()

    def _schedule_save(self):
        self._save_timer.start(2000)

    def _persist_defaults_now(self):
        self.defaults['theme'] = self.theme
        self.defaults['language'] = self.language
        self.defaults['ui_mode'] = self.ui_mode
        try:
            args = type('Args', (), self.defaults)
            save_default_config(args)
        except Exception:
            pass

    def apply_theme(self, theme: str):
        if theme == 'light':
            sheet = '\n            QWidget { background: #f7f7f7; color: #1f2937; }\n            QLineEdit, QListWidget { background: #ffffff; border: 1px solid #d1d5db; padding: 6px; }\n            QComboBox, QSpinBox, QSlider { background: #ffffff; border: 1px solid #d1d5db; padding: 3px; }\n            QPushButton { background: #e5e7eb; border: 1px solid #cbd5e1; padding: 6px 10px; border-radius: 6px; }\n            QPushButton:hover { background: #dfe3ea; }\n            QPushButton:disabled { background: #e5e7eb; color: #9ca3af; }\n            QProgressBar { background: #ffffff; border: 1px solid #d1d5db; border-radius: 6px; text-align: center; }\n            QProgressBar::chunk { background: #3b82f6; }\n            QLabel { color: #111827; }\n            /* Checkbox visibile su tema chiaro */\n            QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #6b7280; background: #ffffff; border-radius: 3px; }\n            QCheckBox::indicator:checked { background: #2563eb; border: 1px solid #1d4ed8; }\n            /* Preset attivo */\n            QPushButton[preset="true"]:checked { background: #2563eb; color: white; border: 1px solid #1d4ed8; }\n            QPushButton[preset="true"]:hover { background: #3b82f6; color: white; }\n            QPushButton[preset="true"]:checked:hover { background: #1d4ed8; color: white; }\n\n            /* Bottoni colorati */\n            #startButton { background: #16a34a; color: white; border: 1px solid #15803d; }\n            #startButton:hover { background: #22c55e; }\n            #removeButton, #clearButton { background: #ef4444; color: white; border: 1px solid #dc2626; }\n            #removeButton:hover, #clearButton:hover { background: #f87171; }\n\n            /* pulsanti spinbox standard */\n            QSpinBox::up-button { subcontrol-origin: border; subcontrol-position: top right; }\n            QSpinBox::down-button { subcontrol-origin: border; subcontrol-position: bottom right; }\n            '
            self.theme_btn.setIcon(self._icon('sun'))
            try:
                self.preview_view1.setStyleSheet('QGraphicsView { border: 1px solid #d1d5db; background: #ffffff; }')
                self.preview_view2.setStyleSheet('QGraphicsView { border: 1px solid #d1d5db; background: #ffffff; }')
            except Exception:
                pass
        else:
            sheet = '\n            QWidget { background: #0f1419; color: #eef2f5; }\n            QLineEdit, QListWidget { background: #1b2229; border: 1px solid #2b3540; padding: 6px; }\n            QComboBox, QSpinBox, QSlider { background: #1b2229; border: 1px solid #2b3540; padding: 3px; }\n            QPushButton { background: #2b3540; border: 1px solid #3a4653; padding: 6px 10px; border-radius: 6px; }\n            QPushButton:hover { background: #354252; }\n            QPushButton:disabled { background: #20262d; color: #8b98a5; }\n            QProgressBar { background: #1b2229; border: 1px solid #2b3540; border-radius: 6px; text-align: center; }\n            QProgressBar::chunk { background: #00bcd4; }\n            QLabel { color: #c9d1d9; }\n            /* Preset attivo */\n            QPushButton[preset="true"]:checked { background: #2563eb; color: white; border: 1px solid #1d4ed8; }\n            QPushButton[preset="true"]:checked:hover { background: #3b82f6; color: white; }\n\n            /* Bottoni colorati */\n            #startButton { background: #16a34a; color: white; border: 1px solid #15803d; }\n            #startButton:hover { background: #22c55e; }\n            #removeButton, #clearButton { background: #b91c1c; color: white; border: 1px solid #991b1b; }\n            #removeButton:hover, #clearButton:hover { background: #dc2626; }\n\n            /* pulsanti spinbox standard */\n            QSpinBox::up-button { subcontrol-origin: border; subcontrol-position: top right; }\n            QSpinBox::down-button { subcontrol-origin: border; subcontrol-position: bottom right; }\n            '
            self.theme_btn.setIcon(self._icon('moon'))
            try:
                self.preview_view1.setStyleSheet('QGraphicsView { border: 1px solid #2b3540; background: #0f1419; }')
                self.preview_view2.setStyleSheet('QGraphicsView { border: 1px solid #2b3540; background: #0f1419; }')
            except Exception:
                pass
        self.setStyleSheet(sheet)
        self.theme = theme
        try:
            t = self.i18n[self.language]
            if hasattr(self, 'theme_btn'):
                self.theme_btn.setText(t['light'] if theme == 'light' else t['dark'])
            if hasattr(self, 'mode_btn'):
                self.mode_btn.setIcon(self._icon('advanced' if self.ui_mode == 'advanced' else 'simple'))
        except Exception:
            pass
        for btn_name in ('addButton', 'removeButton', 'clearButton', 'outButton', 'tmpButton', 'startButton', 'openOutputButton', 'themeButton', 'uiModeButton', 'languageButton', 'clearLogButton'):
            b = self.findChild(QPushButton, btn_name)
            if not b:
                continue
            icon_map = {'addButton': 'file-plus', 'removeButton': 'trash', 'clearButton': 'trash', 'outButton': 'folder-open', 'tmpButton': 'folder-open', 'startButton': 'play', 'openOutputButton': 'folder-open', 'themeButton': 'sun' if theme == 'light' else 'moon', 'uiModeButton': 'advanced' if self.ui_mode == 'advanced' else 'simple', 'languageButton': 'translate', 'clearLogButton': 'trash'}
            b.setIcon(self._icon(icon_map.get(btn_name, 'folder-open')))
        try:
            rb = self.findChild(QPushButton, 'removeButton')
            if rb:
                rb.setIcon(self._icon('trash', color_override=Qt.white))
            cb = self.findChild(QPushButton, 'clearButton')
            if cb:
                cb.setIcon(self._icon('trash', color_override=Qt.white))
            sb = self.findChild(QPushButton, 'startButton')
            if sb:
                sb.setIcon(self._icon('play', color_override=Qt.white))
        except Exception:
            pass
        self._schedule_save()

    def apply_language(self, lang: str):
        # accept any available language loaded from i18n folder
        self.language = lang if lang in self.i18n else (self.language if self.language in self.i18n else (list(self.i18n.keys())[0] if self.i18n else 'en'))
        t = self.i18n[self.language]
        self.setWindowTitle(t['window_title'])
        for btn in self.findChildren(QPushButton):
            pass
        # Preview titles and labels
        if hasattr(self, 'preview_title'):
            self.preview_title.setText(t.get('preview_title', self.preview_title.text()))
        if hasattr(self, 'preview_label1'):
            self.preview_label1.setText(t.get('preview_original', self.preview_label1.text()))
        if hasattr(self, 'preview_label2'):
            self.preview_label2.setText(t.get('preview_compressed', self.preview_label2.text()))
        self.files_list.setToolTip(t['drag_hint'])
        add_btn = self.findChild(QPushButton, 'addButton')
        if add_btn:
            add_btn.setText(t['add'])
        rem_btn = self.findChild(QPushButton, 'removeButton')
        if rem_btn:
            rem_btn.setText(t['remove'])
        clear_btn = self.findChild(QPushButton, 'clearButton')
        if clear_btn:
            clear_btn.setText(t['clear'])
        self.lbl_out.setText(t['output_dir'])
        self.lbl_tmp.setText(t['temp_dir'])
        self.out_btn.setText(' Output…')
        self.tmp_btn.setText(' Temp…')
        self.lbl_device.setText(t['device'])
        self.lbl_mode.setText(t['mode'])
        # Refresh localized labels of compression modes while keeping the selected key
        self._refresh_modes_labels()
        self.lbl_quality.setText(t['quality'] + ' (1–100):')
        self._update_colors_label()
        self.lbl_workers.setText(t['workers'])
        self.lbl_ram.setText(t['ram'])
        self.save_defaults_chk.setText(t['save_defaults'])
        self.start_btn.setText(t['start'])
        self.stop_btn.setText(t['stop'])
        if hasattr(self, 'theme_btn'):
            self.theme_btn.setText(t['light'] if self.theme == 'light' else t['dark'])
        self.theme_btn.setToolTip(t['toggle_theme'])
        if hasattr(self, 'language_btn'):
            # If t contains language_btn use it, otherwise display code in upper-case
            btn_txt = t.get('language_btn')
            if not btn_txt:
                btn_txt = f" {self.language.upper()}"
            self.language_btn.setText(btn_txt)
            self.language_btn.setToolTip(t['toggle_language'])
        self.open_output_btn.setText(t['open_output'])
        self.mode_btn.setText(' ' + (t['advanced_mode'] if self.ui_mode == 'advanced' else t['simple_mode']))
        self.mode_btn.setToolTip(t['toggle_mode'])
        if hasattr(self, 'lbl_custom_section'):
            self.lbl_custom_section.setText(t['custom_device_section'])
        if hasattr(self, 'use_custom_chk'):
            self.use_custom_chk.setText(t['custom_device_use'])
        self.lbl_presets.setText(t['presets'])
        self.btn_p_min.setText('1 - ' + t['preset_minimal'])
        self.btn_p_very_low.setText('2 - ' + t.get('preset_very_low', 'Very Low'))
        self.btn_p_low.setText('3 - ' + t['preset_low'])
        self.btn_p_normal.setText('4 - ' + t['preset_normal'])
        self.btn_p_high.setText('5 - ' + t['preset_high'])
        self.btn_p_very_high.setText('6 - ' + t.get('preset_very_high', 'Very High'))
        self.btn_p_ultra.setText('7 - ' + t['preset_ultra'])
        if hasattr(self, 'lbl_brand'):
            self.lbl_brand.setText(t['brand'])
        if hasattr(self, 'lbl_model'):
            self.lbl_model.setText(t['model'])
        if hasattr(self, 'suggestion_apply_btn'):
            self.suggestion_apply_btn.setText(t['apply_suggestion'])
        self.defaults['language'] = self.language
        self._schedule_save()
        try:
            if getattr(self, '_last_loaded_file', None):
                self._update_suggestion(self._last_loaded_file)
        except Exception:
            pass
        # Refresh bottom preview level label according to the selected preset
        try:
            p = self._current_preset_key() or 'normal'
            self._update_previews(p)
        except Exception:
            pass

    def _refresh_modes_labels(self):
        try:
            cur_key = self.mode_combo.currentData()
            self.mode_combo.blockSignals(True)
            for i in range(self.mode_combo.count()):
                key = self.mode_combo.itemData(i)
                self.mode_combo.setItemText(i, f"{key} — {self._localized_mode_label(key)}")
            # restore selection
            if cur_key is not None:
                self._set_combo_by_data(self.mode_combo, cur_key)
        except Exception:
            pass
        finally:
            try:
                self.mode_combo.blockSignals(False)
            except Exception:
                pass

    def _localized_mode_label(self, key: str) -> str:
        try:
            t = self.i18n.get(self.language, {})
            modes = t.get('modes', {}) if isinstance(t, dict) else {}
            return modes.get(key, COMPRESSION_MODES.get(key, key))
        except Exception:
            return COMPRESSION_MODES.get(key, key)

    def _slider_jump_to_click(self, slider: QSlider):

        def handler(event):
            try:
                if event.button() == Qt.LeftButton:
                    x = event.position().x() if hasattr(event, 'position') else event.x()
                    w = max(1, slider.width())
                    ratio = min(1.0, max(0.0, x / w))
                    new_val = slider.minimum() + int(ratio * (slider.maximum() - slider.minimum()))
                    slider.setValue(new_val)
            except Exception:
                pass
            return QSlider.mousePressEvent(slider, event)
        return handler

    def _abbreviate_path(self, path: str) -> str:
        try:
            p = Path(path)
            parts = list(p.parts)
            if len(parts) <= 5:
                return path
            root = ''
            if parts[0] == os.sep:
                root = os.sep
                parts = parts[1:]
            if len(parts) < 5:
                return path
            head = parts[:2]
            tail = parts[-3:]
            return root + os.sep.join(head) + os.sep + '…' + os.sep + os.sep.join(tail)
        except Exception:
            return path

    def on_add_files(self):
        t = self.i18n[self.language]
        files, _ = QFileDialog.getOpenFileNames(self, t['select_pdfs_title'], str(Path.cwd()), t['pdf_filter'])
        for f in files:
            item = QListWidgetItem(self._abbreviate_path(f))
            item.setData(Qt.UserRole, f)
            item.setToolTip(f)
            self.files_list.addItem(item)
        if files:
            self._last_loaded_file = files[-1]
            self.btn_p_normal.setChecked(True)
            self._apply_preset('normal')
            self._update_suggestion(files[-1])

    def on_remove_selected(self):
        for item in self.files_list.selectedItems():
            self.files_list.takeItem(self.files_list.row(item))

    def choose_dir(self, line_edit: QLineEdit):
        t = self.i18n[self.language]
        d = QFileDialog.getExistingDirectory(self, t['choose_dir'], str(Path.cwd()))
        if d:
            line_edit.setText(str(Path(d).resolve()))

    def on_start(self):
        files = []
        for i in range(self.files_list.count()):
            it = self.files_list.item(i)
            files.append(it.data(Qt.UserRole) or it.text())
        if not files:
            t = self.i18n[self.language]
            QMessageBox.warning(self, t['no_files_title'], t['add_at_least'])
            return
        out_dir = Path(self.out_dir_edit.text() or Path.cwd() / 'compressed').resolve()
        tmp_dir = Path(self.tmp_dir_edit.text() or Path.cwd() / 'tmp').resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        self.current_out_dir = out_dir
        if self.ui_mode == 'simple':
            device = self.simple_model_combo.currentData()
            custom_key = None
        else:
            custom_key = self._ensure_custom_device()
            device = custom_key or self.device_combo.currentData()
        mode = self.mode_combo.currentData()
        quality = self.quality_spin.value()
        P = int(self.colors_spin.value())
        max_colors = 2 ** P
        workers = self.workers_spin.value() or None
        ram_limit = self.ram_spin.value()
        if not 1 <= quality <= 100:
            t = self.i18n[self.language]
            QMessageBox.warning(self, t['params_title'], t['quality_range'])
            return
        if not 1 <= P <= 8:
            t = self.i18n[self.language]
            QMessageBox.warning(self, t['params_title'], t['max_colors_range'])
            return
        import multiprocessing as _mp
        cpu_count = max(1, _mp.cpu_count())
        if workers is not None and workers > cpu_count:
            t = self.i18n[self.language]
            QMessageBox.warning(self, t['params_title'], t['workers_limit'].format(cpu=cpu_count))
            return
        if not 10 <= ram_limit <= 95:
            t = self.i18n[self.language]
            QMessageBox.warning(self, t['params_title'], t['ram_range'])
            return
        if self.save_defaults_chk.isChecked():
            args = type('Args', (), {'device': device, 'mode': mode, 'quality': quality, 'max_colors': max_colors, 'workers': workers, 'ram_limit': ram_limit, 'suffix': None, 'out_dir': str(out_dir), 'tmp_dir': str(tmp_dir), 'ui_mode': self.ui_mode, 'language': self.language})
            try:
                save_default_config(args)
                self.on_log(self.i18n[self.language]['defaults_saved'])
            except Exception as e:
                self.on_log(self.i18n[self.language]['cannot_save_defaults'].format(err=e))
        self.progress.setValue(0)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.log_list.clear()
        preset_key = self._current_preset_key()
        self.worker = CompressorWorker(files, out_dir, tmp_dir, device, mode, quality, max_colors, workers, ram_limit, self.signals, preset_key=preset_key)
        self.worker.start()

    def on_log(self, msg: str):
        if self.language == 'it':
            repl = [('Processing:', 'Elaborazione:'), ('Done:', 'Fatto:'), ('Error:', 'Errore:'), ('Skipping invalid file:', 'Salto file non valido:'), ('Completed with', 'Completato con')]
            for a, b in repl:
                if msg.startswith(a):
                    msg = b + msg[len(a):]
                    break
        self.log_list.addItem(msg)
        self.log_list.scrollToBottom()

    def on_clear_log(self):
        self.log_list.clear()

    def on_progress(self, done: int, total: int, percent: float):
        self.progress.setValue(int(percent))

    def on_file_done(self, file: str, output: str):
        self.on_log(f'Done: {file} -> {output}')

    def on_error(self, message: str):
        self.on_log(f'Error: {message}')

    def on_all_done(self):
        self.on_log(self.i18n[self.language]['all_done'])
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        try:
            self.on_open_output()
        except Exception:
            pass
        try:
            tmp_dir = Path(self.tmp_dir_edit.text() or Path.cwd() / 'tmp').resolve()
            if tmp_dir.exists():
                for p in tmp_dir.iterdir():
                    try:
                        if p.is_file() or p.is_symlink():
                            p.unlink(missing_ok=True)
                        elif p.is_dir():
                            import shutil
                            shutil.rmtree(p, ignore_errors=True)
                    except Exception:
                        pass
        except Exception:
            pass

    def eventFilter(self, obj, event):
        if obj is self.files_list and event.type() in (QEvent.DragEnter, QEvent.Drop, QEvent.DragMove):
            if event.type() == QEvent.DragEnter or event.type() == QEvent.DragMove:
                mime = event.mimeData()
                if mime.hasUrls() and any((u.toLocalFile().lower().endswith('.pdf') for u in mime.urls())):
                    event.acceptProposedAction()
                    return True
            elif event.type() == QEvent.Drop:
                urls = event.mimeData().urls()
                for u in urls:
                    p = u.toLocalFile()
                    if p.lower().endswith('.pdf') and Path(p).exists():
                        item = QListWidgetItem(self._abbreviate_path(p))
                        item.setData(Qt.UserRole, p)
                        item.setToolTip(p)
                        self.files_list.addItem(item)
                event.acceptProposedAction()
                if urls:
                    try:
                        self._last_loaded_file = urls[-1].toLocalFile()
                        self.btn_p_normal.setChecked(True)
                        self._apply_preset('normal')
                        self._update_suggestion(self._last_loaded_file)
                    except Exception:
                        pass
                return True
        return super().eventFilter(obj, event)

    def on_toggle_theme(self):
        new_theme = 'light' if self.theme == 'dark' else 'dark'
        self.apply_theme(new_theme)

    def on_cycle_language(self):
        langs = list(self.i18n.keys())
        try:
            idx = langs.index(self.language)
        except ValueError:
            idx = 0
        next_lang = langs[(idx + 1) % len(langs)]
        self.apply_language(next_lang)
        self._schedule_save()

    def on_toggle_ui_mode(self):
        self.ui_mode = 'simple' if self.ui_mode == 'advanced' else 'advanced'
        self._apply_ui_mode(self.ui_mode)
        self.apply_language(self.language)
        self._schedule_save()

    def _apply_ui_mode(self, mode: str):
        is_simple = mode == 'simple'
        self.simple_panel.setVisible(is_simple)
        if hasattr(self, 'preview_panel'):
            self.preview_panel.setVisible(is_simple)
        try:
            if is_simple:
                self.setFixedWidth(1030)
            else:
                self.setFixedWidth(730)
        except Exception:
            pass
        if hasattr(self, 'suggestion_bar'):
            self.suggestion_bar.setVisible(is_simple)
            if is_simple and getattr(self, '_last_loaded_file', None):
                try:
                    self._update_suggestion(self._last_loaded_file)
                except Exception:
                    pass
        for w in (self.lbl_quality, self.quality_slider, self.quality_spin, self.lbl_colors, self.colors_slider, self.colors_spin, self.lbl_workers, self.workers_slider, self.workers_spin, self.lbl_ram, self.ram_slider, self.ram_spin):
            w.setVisible(not is_simple)
        self.lbl_device.setVisible(not is_simple)
        self.device_combo.setVisible(not is_simple)
        if hasattr(self, 'advanced_custom_panel'):
            self.advanced_custom_panel.setVisible(not is_simple)
        for w in (getattr(self, 'custom_name_edit', None), getattr(self, 'custom_inches', None), getattr(self, 'custom_w_spin', None), getattr(self, 'custom_h_spin', None), getattr(self, 'custom_dpi_spin', None)):
            if w:
                w.setVisible(not is_simple)
        if hasattr(self, 'use_custom_chk'):
            self.use_custom_chk.setVisible(not is_simple)
        if hasattr(self, 'lbl_custom_section'):
            self.lbl_custom_section.setVisible(not is_simple)
        if hasattr(self, 'mode_btn'):
            self.mode_btn.setIcon(self._icon('advanced' if not is_simple else 'simple'))

    def on_preset_click(self):
        sender = self.sender()
        key = None
        if sender is self.btn_p_ultra:
            key = 'ultra'
        elif sender is self.btn_p_very_high:
            key = 'very_high'
        elif sender is self.btn_p_high:
            key = 'high'
        elif sender is self.btn_p_normal:
            key = 'normal'
        elif sender is self.btn_p_low:
            key = 'low'
        elif sender is self.btn_p_very_low:
            key = 'very_low'
        elif sender is self.btn_p_min:
            key = 'minimal'
        self._apply_preset(key)
        self.on_log(f'Preset applied: {key}')

    def on_apply_suggestion(self):
        key = getattr(self, '_suggested_preset', None)
        if not key:
            return
        mapping = {'ultra': self.btn_p_ultra, 'very_high': self.btn_p_very_high, 'high': self.btn_p_high, 'normal': self.btn_p_normal, 'low': self.btn_p_low, 'very_low': self.btn_p_very_low, 'minimal': self.btn_p_min}
        btn = mapping.get(key)
        if btn:
            btn.click()

    def on_open_output(self):
        try:
            target = Path(self.out_dir_edit.text() or Path.cwd() / 'compressed').resolve()
        except Exception:
            target = (Path.cwd() / 'compressed').resolve()
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
        if sys.platform.startswith('linux'):
            os.system(f"xdg-open '{str(target)}' >/dev/null 2>&1 &")
        elif sys.platform == 'darwin':
            os.system(f"open '{str(target)}' >/dev/null 2>&1 &")
        elif os.name == 'nt':
            os.startfile(str(target))

    def _load_presets(self):
        try:
            root = Path(__file__).parent
            for rel in [Path('presets.json'), Path('assets/presets.json')]:
                p = root / rel
                if p.exists():
                    with open(p, 'r') as f:
                        return json.load(f)
        except Exception:
            pass
        return None

    def _load_devices(self):
        try:
            root = Path(__file__).parent
            for rel in [Path('devices.json'), Path('assets/devices.json')]:
                p = root / rel
                if p.exists():
                    with open(p, 'r') as f:
                        return json.load(f)
        except Exception:
            pass
        return None

    def _update_suggestion(self, file_path: str):
        try:
            total_bytes = max(1, int(Path(file_path).stat().st_size))
        except Exception:
            total_bytes = 1
        preset_key = self._current_preset_key() or 'normal'
        ratio_map = {'ultra': 0.82, 'very_high': 0.72, 'high': 0.65, 'normal': 0.5, 'low': 0.38, 'very_low': 0.33, 'minimal': 0.28}
        ratio = ratio_map.get(preset_key, 0.5)
        overhead = 180 * 1024
        est = int(total_bytes * ratio + overhead)
        est = min(est, total_bytes)
        t = self.i18n[self.language]
        est_mb = est / (1024 * 1024)
        red = max(0, int(round((1 - est / total_bytes) * 100)))
        self._suggested_preset = preset_key
        preset_label = self._preset_label_localized(preset_key)
        self.suggestion_label.setText(f"{t['estimate_label']} ~{est_mb:.1f} MB (−{red}%) — {preset_label}")
        if hasattr(self, 'suggestion_bar'):
            self.suggestion_bar.setVisible(True)

    def _current_device_key(self) -> Optional[str]:
        if self.ui_mode == 'simple':
            return self.simple_model_combo.currentData()
        ck = self._ensure_custom_device()
        return ck or self.device_combo.currentData()

    def _estimate_output_size_precise(self, file_path: str) -> Optional[int]:
        try:
            total_bytes = max(1, int(Path(file_path).stat().st_size))
            extractor = PDFExtractor()
            pages = max(1, extractor.get_page_count(file_path))
            preset_key = self._current_preset_key() or 'normal'
            ratio_map = {'ultra': 0.82, 'very_high': 0.72, 'high': 0.65, 'normal': 0.5, 'low': 0.38, 'very_low': 0.33, 'minimal': 0.28}
            bpp = total_bytes / pages
            overhead = 180 * 1024
            ratio = ratio_map.get(preset_key, 0.5)
            return int(pages * bpp * ratio + overhead)
        except Exception:
            return None

    def _preset_label_localized(self, key: str) -> str:
        try:
            t = self.i18n[self.language]
            mapping = {'ultra': t.get('preset_ultra', 'Ultra'), 'very_high': t.get('preset_very_high', 'Very High'), 'high': t.get('preset_high', 'High'), 'normal': t.get('preset_normal', 'Normal'), 'low': t.get('preset_low', 'Low'), 'very_low': t.get('preset_very_low', 'Very Low'), 'minimal': t.get('preset_minimal', 'Minimal')}
            return mapping.get(key, key.title())
        except Exception:
            return key.title()

    def _load_devices_model_map(self):
        self._devices_map = {}
        data = self._load_devices()
        if isinstance(data, dict) and data:
            self._devices_map = {brand: list(models) for brand, models in data.items()}
        if not self._devices_map:
            self._devices_map = {'Generic': [{'model': 'Phone', 'key': 'phone'}, {'model': 'Tablet 7"', 'key': 'tablet_7'}, {'model': 'Tablet 10"', 'key': 'tablet_10'}, {'model': 'Tablet 12"', 'key': 'tablet_12'}, {'model': 'E-reader', 'key': 'ereader'}, {'model': 'Laptop', 'key': 'laptop'}, {'model': 'Desktop', 'key': 'desktop'}]}
        self.simple_brand_combo.blockSignals(True)
        self.simple_brand_combo.clear()
        for brand in self._devices_map.keys():
            self.simple_brand_combo.addItem(brand, userData=brand)
        self.simple_brand_combo.blockSignals(False)
        self._on_simple_brand_changed()

    def _select_brand_model_by_device_key(self, key: Optional[str]):
        if not key:
            return
        try:
            for i in range(self.simple_brand_combo.count()):
                brand = self.simple_brand_combo.itemData(i)
                for m in self._devices_map.get(brand, []):
                    if m.get('key') == key:
                        self.simple_brand_combo.setCurrentIndex(i)
                        self._on_simple_brand_changed()
                        for j in range(self.simple_model_combo.count()):
                            if self.simple_model_combo.itemData(j) == key:
                                self.simple_model_combo.setCurrentIndex(j)
                                return
        except Exception:
            pass

    def _on_simple_brand_changed(self):
        try:
            brand = self.simple_brand_combo.currentData()
            models = self._devices_map.get(brand, [])
            self.simple_model_combo.blockSignals(True)
            self.simple_model_combo.clear()
            for m in models:
                label = m.get('model', 'Model')
                key = m.get('key')
                details = []
                if 'size' in m:
                    details.append(f'''{m['size']}"''')
                if 'resolution' in m:
                    details.append(str(m['resolution']))
                if 'ppi' in m:
                    details.append(f"{m['ppi']} ppi")
                tooltip = ' · '.join(details) if details else None
                self.simple_model_combo.addItem(label, userData=key)
                if tooltip:
                    idx = self.simple_model_combo.count() - 1
                    self.simple_model_combo.setItemData(idx, tooltip, Qt.ToolTipRole)
            self.simple_model_combo.blockSignals(False)
            key = self.simple_model_combo.itemData(0) if self.simple_model_combo.count() else None
            if key:
                self._set_combo_by_data(self.device_combo, key)
                self._prefill_custom_from_device(key)
        except Exception:
            pass

    def _on_simple_model_changed(self):
        try:
            key = self.simple_model_combo.currentData()
            if key:
                self._set_combo_by_data(self.device_combo, key)
                self._prefill_custom_from_device(key)
        except Exception:
            pass

    def _load_i18n(self):
        """Load i18n dictionaries from i18n/*.json. Fallback to built-ins if missing."""
        self.i18n = {}
        try:
            base = Path(__file__).parent / 'i18n'
            if base.exists():
                for p in sorted(base.glob('*.json')):
                    try:
                        code = p.stem.lower()
                        with open(p, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                        if isinstance(data, dict):
                            self.i18n[code] = data
                    except Exception:
                        pass
        except Exception:
            pass
        # ensure at least English exists to avoid crashes
        if not self.i18n:
            self.i18n = {
                'en': {
                    'window_title': 'Manga PDF Compressor',
                    'add': ' Add…',
                    'remove': ' Remove selected',
                    'clear': ' Clear',
                    'output_dir': 'Output dir:',
                    'temp_dir': 'Temp dir:',
                    'device': 'Device:',
                    'brand': 'Brand:',
                    'model': 'Model:',
                    'mode': 'Mode:',
                    'quality': 'Quality:',
                    'max_colors': 'Max colors:',
                    'workers': 'Workers:',
                    'ram': 'RAM %:',
                    'save_defaults': 'Save as default',
                    'start': ' Start',
                    'stop': ' Stop',
                    'theme': 'Theme',
                    'light': ' Light',
                    'dark': ' Dark',
                    'language_label': 'Language:',
                    'language_btn': ' EN',
                    'open_output': ' Open Output Folder',
                    'select_pdfs_title': 'Select PDF files',
                    'pdf_filter': 'PDF Files (*.pdf)',
                    'choose_dir': 'Choose directory',
                    'no_files_title': 'No files',
                    'add_at_least': 'Please add at least one PDF file.',
                    'params_title': 'Parameters',
                    'quality_range': 'Quality must be between 1 and 100.',
                    'max_colors_range': 'Max-colors power P must be between 1 and 8 (2^P = 2..256).',
                    'workers_limit': 'Maximum allowed workers: {cpu}.',
                    'ram_range': 'RAM % must be between 10 and 95.',
                    'defaults_saved': 'Defaults saved.',
                    'cannot_save_defaults': 'Cannot save defaults: {err}',
                    'all_done': 'All done.',
                    'drag_hint': "Drag PDF files here or use 'Add'",
                    'toggle_theme': 'Toggle light/dark theme',
                    'toggle_language': 'Change language',
                    'toggle_mode': 'Simple/Advanced',
                    'simple_mode': 'Simple mode',
                    'advanced_mode': 'Advanced mode',
                    'presets': 'Presets:',
                    'preset_ultra': 'Ultra',
                    'preset_very_high': 'Very High',
                    'preset_high': 'High',
                    'preset_normal': 'Normal',
                    'preset_low': 'Low',
                    'preset_very_low': 'Very Low',
                    'preset_minimal': 'Minimal',
                    'custom_device_section': 'Optional target device',
                    'custom_device_use': ' Use custom device',
                    'custom_name': 'Name',
                    'custom_inches': 'Diagonal (in)',
                    'custom_resolution': 'Resolution (px)',
                    'custom_dpi': 'DPI',
                    'suggest_prefix': 'Suggestion:',
                    'estimate_label': 'Estimated size:',
                    'apply_suggestion': 'Apply suggestion',
                    'apply_suggestion_tip': 'Set presets to the suggested quality level',
                    'preview_title': 'Quality preview',
                    'preview_original': 'Original',
                    'preview_compressed': 'Compressed',
                    'modes': {
                        'auto': 'Auto — Detect image type',
                        'bw': 'Force black and white (1-bit PNG)',
                        'grayscale': 'Force grayscale (JPEG)',
                        'color': 'Force color (JPEG)'
                    }
                }
            }

    def on_stop(self):
        try:
            if self.worker:
                self.on_log('Stopping…')
                self.worker.request_stop()
                self.stop_btn.setEnabled(False)
        except Exception:
            pass

    def _ensure_custom_device(self) -> Optional[str]:
        try:
            if not getattr(self, 'use_custom_chk', None) or not self.use_custom_chk.isChecked():
                return None
            name = (self.custom_name_edit.text() or 'custom').strip().replace(' ', '_')
            w = int(self.custom_w_spin.value())
            h = int(self.custom_h_spin.value())
            dpi = int(self.custom_dpi_spin.value())
            inches = float(self.custom_inches.value())
            if dpi <= 0 and inches > 0:
                diag_px = (w ** 2 + h ** 2) ** 0.5
                dpi = max(96, int(diag_px / inches))
            key = f'custom_{name}'
            DEVICE_PROFILES[key] = {'size': (w, h), 'dpi': dpi, 'quality_adjust': 10, 'sharpening': 1.0, 'description': f'Custom {inches:.1f}" {w}x{h}@{dpi}dpi'}
            return key
        except Exception:
            return None

    def _current_preset_key(self) -> Optional[str]:
        if self.btn_p_ultra.isChecked():
            return 'ultra'
        if hasattr(self, 'btn_p_very_high') and self.btn_p_very_high.isChecked():
            return 'very_high'
        if self.btn_p_high.isChecked():
            return 'high'
        if self.btn_p_normal.isChecked():
            return 'normal'
        if self.btn_p_low.isChecked():
            return 'low'
        if hasattr(self, 'btn_p_very_low') and self.btn_p_very_low.isChecked():
            return 'very_low'
        if self.btn_p_min.isChecked():
            return 'minimal'
        return None

    def _apply_preset(self, key: str):
        preset = (self.presets or {}).get(key)
        if preset:
            self.mode_combo.setCurrentIndex(self.mode_combo.findData(preset.get('mode', 'auto')))
            self.quality_spin.setValue(int(preset.get('quality', DEFAULT_QUALITY)))
            try:
                c = int(preset.get('max_colors', DEFAULT_MAX_COLORS))
            except Exception:
                c = DEFAULT_MAX_COLORS
            c = max(2, min(256, c))
            allowed = [2, 4, 8, 16, 32, 64, 128, 256]
            P = allowed.index(min(allowed, key=lambda x: abs(x - c))) + 1
            self.colors_spin.setValue(P)
        else:
            defaults = {'ultra': ('auto', 92, 256), 'very_high': ('auto', 85, 256), 'high': ('auto', 80, 256), 'normal': ('auto', 70, 128), 'low': ('auto', 55, 64), 'very_low': ('auto', 48, 32), 'minimal': ('auto', 40, 16)}
            m, q, c = defaults.get(key, ('auto', DEFAULT_QUALITY, DEFAULT_MAX_COLORS))
            self.mode_combo.setCurrentIndex(self.mode_combo.findData(m))
            self.quality_spin.setValue(q)
            allowed = [2, 4, 8, 16, 32, 64, 128, 256]
            P = allowed.index(min(allowed, key=lambda x: abs(x - c))) + 1
            self.colors_spin.setValue(P)
        if getattr(self, '_last_loaded_file', None):
            self._update_suggestion(self._last_loaded_file)
        self._update_previews(key)

    def _update_colors_label(self):
        try:
            t = self.i18n[self.language]
            P = int(self.colors_spin.value())
            P = max(1, min(8, P))
            N = 2 ** P
            self.lbl_colors.setText(t['max_colors'] + f' (2^P = {N}; P=1..8):')
        except Exception:
            self.lbl_colors.setText('Max colors (2^P, P=1..8):')

    def _on_simple_device_changed(self):
        pass

    def _prefill_custom_from_device(self, key: Optional[str]):
        try:
            if not key or key not in DEVICE_PROFILES:
                return
            prof = DEVICE_PROFILES[key]
            w, h = prof.get('size', (1600, 2560))
            dpi = prof.get('dpi', 300)
            self.custom_w_spin.setValue(int(w))
            self.custom_h_spin.setValue(int(h))
            self.custom_dpi_spin.setValue(int(dpi))
        except Exception:
            pass

def main():
    app = QApplication(sys.argv)
    gui = MangaCompressorGUI()
    gui.show()
    sys.exit(app.exec())
if __name__ == '__main__':
    main()