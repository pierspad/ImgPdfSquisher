import os
import sys
import json
import math
import time
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
        self.setMinimumSize(1060, 820)
        self.resize(1200, 860)
        self.setWindowIcon(QIcon())
        self.presets = self._load_presets()
        self.signals = Signals()
        self.signals.log.connect(self.on_log)
        self.signals.progress.connect(self.on_progress)
        self.signals.file_done.connect(self.on_file_done)
        self.signals.error.connect(self.on_error)
        self.signals.all_done.connect(self.on_all_done)
        self.defaults = load_default_config() or {}
        # Prima esecuzione: non impostare output/tmp di default, costringere l'utente a sceglierle
        if not self.defaults:
            self.defaults = {
                'device': 'tablet_10',
                'mode': 'auto',
                'quality': DEFAULT_QUALITY,
                'max_colors': DEFAULT_MAX_COLORS,
                'workers': None,
                'ram_limit': 75,
                'suffix': None,
                'out_dir': '',
                'theme': 'dark',
                'language': 'en',
                'ui_mode': 'simple',
                'first_run_done': False,
            }
        self._stop_requested = False
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
        self.worker = None  # type: ignore
        # progress animation controller (per-segment)
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(50)
        self._progress_timer.timeout.connect(self._on_progress_tick)
        # Effetto gradiente pulsante sulla barra di progresso
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(120)
        self._pulse_timer.timeout.connect(self._update_progressbar_pulse)
        self._pulse_phase = 0.0
        self._pulse_active = False
        self._progress_file_sizes = []
        self._progress_total_bytes = 0
        self._anim_total_files = 0
        self._anim_index = 0
        self._anim_seg_start_time = 0.0
        self._anim_seg_target_dur = 0.0
        self._anim_seg_start_frac = 0.0
        self._anim_seg_end_frac = 0.0
        self._display_count = 0
        # Storico throughput per media pesata (ultimi K file)
        self._throughput_hist = []  # list of (bytes, seconds)
        self._throughput_window = 5

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
        # mostrare percorsi completi senza elisione
        try:
            self.files_list.setTextElideMode(Qt.ElideNone)
        except Exception:
            pass
        try:
            self.files_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            self.files_list.setWordWrap(False)
        except Exception:
            pass
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
        rem_btn.setEnabled(False)
        clear_btn = QPushButton()
        clear_btn.setObjectName('clearButton')
        clear_btn.setIcon(self._icon('trash'))
        add_btn.clicked.connect(self.on_add_files)
        rem_btn.clicked.connect(self.on_remove_selected)
        # clear deve anche aggiornare lo stato dei pulsanti e di "Avvia"
        clear_btn.clicked.connect(self._on_clear_files)
        # aggiorna stato pulsanti in base alla selezione
        self.files_list.itemSelectionChanged.connect(self._update_files_buttons_state)
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
        # avvicina e centra le 2 stime, con separatore verticale netto
        sug_layout.setSpacing(10)
        self.suggestion_label = QLabel('')
        self.suggestion_label.setObjectName('suggestionLabel')
        self.suggestion_label.setAlignment(Qt.AlignCenter)
        from PySide6.QtWidgets import QFrame
        self.suggestion_separator = QFrame()
        self.suggestion_separator.setFrameShape(QFrame.VLine)
        self.suggestion_separator.setFrameShadow(QFrame.Sunken)
        # Label destra: stima totale di tutti i file
        self.suggestion_total_label = QLabel('')
        self.suggestion_total_label.setObjectName('suggestionTotalLabel')
        self.suggestion_total_label.setAlignment(Qt.AlignCenter)
        # Disclaimer breve sulle stime
        self.suggestion_disclaimer = QLabel('')
        try:
            self.suggestion_disclaimer.setStyleSheet('color: #8b98a5; font-size: 11px;')
        except Exception:
            pass
        self.suggestion_apply_btn = QPushButton()
        self.suggestion_apply_btn.setObjectName('suggestionApply')
        self.suggestion_apply_btn.clicked.connect(self.on_apply_suggestion)
        self.suggestion_apply_btn.setVisible(False)
        # Stretch esterni per centrare
        sug_layout.addStretch(1)
        sug_layout.addWidget(self.suggestion_label)
        sug_layout.addWidget(self.suggestion_separator)
        sug_layout.addWidget(self.suggestion_total_label)
        sug_layout.addWidget(self.suggestion_disclaimer)
        sug_layout.addStretch(1)
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
        self.out_dir_edit.setPlaceholderText(str((Path.cwd() / 'compressed').resolve()))
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
        # avvicina label e select in tutta la griglia
        opts_grid.setHorizontalSpacing(4)
        opts_grid.setVerticalSpacing(6)
        opts_grid.setContentsMargins(4, 0, 4, 0)
        self.device_combo = QComboBox()
        for k, v in DEVICE_PROFILES.items():
            self.device_combo.addItem(f"{k} — {v['description']}", userData=k)
        # Advanced brand/model (replaces legacy single select)
        self.lbl_brand_adv = QLabel()
        self.advanced_brand_combo = QComboBox()
        self.lbl_model_adv = QLabel()
        self.advanced_model_combo = QComboBox()
        self.advanced_brand_combo.currentIndexChanged.connect(self._on_advanced_brand_changed)
        self.advanced_model_combo.currentIndexChanged.connect(self._on_advanced_model_changed)
        self.mode_combo = QComboBox()
        # Mostra l'etichetta completa senza troncamenti e posiziona vicino alla label
        try:
            from PySide6.QtWidgets import QComboBox as _QB
            self.mode_combo.setSizeAdjustPolicy(_QB.AdjustToContents)
            self.mode_combo.setMinimumWidth(420)
        except Exception:
            pass
        for k in COMPRESSION_MODES.keys():
            label = self._localized_mode_label(k)
            self.mode_combo.addItem(f"{k} — {label}", userData=k)
        # Quality slider/spin
        self.quality_slider = QSlider(Qt.Horizontal)
        self.quality_slider.setRange(1, 100)
        self.quality_slider.setValue(DEFAULT_QUALITY)
        # Nessuna righetta per il quality slider (range troppo ampio)
        self.quality_slider.setSingleStep(1)
        try:
            self.quality_slider.setTickPosition(QSlider.NoTicks)
        except Exception:
            pass
        # Aggiorna valore mentre si trascina e salta alla posizione cliccata
        try:
            self.quality_slider.setTracking(True)
        except Exception:
            pass
        self.quality_slider.mousePressEvent = self._slider_jump_to_click(self.quality_slider)
        self.quality_slider.mouseMoveEvent = self._slider_drag_to_move(self.quality_slider)
        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(1, 100)
        self.quality_spin.setValue(DEFAULT_QUALITY)
        self.quality_spin.setFixedWidth(70)
        self.quality_spin.setToolTip('JPEG quality. 1 = smaller file, 100 = best quality.')
        self.quality_slider.valueChanged.connect(self.quality_spin.setValue)
        self.quality_spin.valueChanged.connect(self.quality_slider.setValue)
        # Max colors slider/spin (P=1..24)
        self.colors_slider = QSlider(Qt.Horizontal)
        self.colors_slider.setRange(1, 24)
        self.colors_slider.setValue(8)
        # 24 righette, una per ogni valore possibile (2^1 a 2^24)
        self.colors_slider.setTickInterval(1)
        self.colors_slider.setSingleStep(1)
        try:
            self.colors_slider.setTickPosition(QSlider.TicksBelow)
        except Exception:
            pass
        try:
            self.colors_slider.setTracking(True)
        except Exception:
            pass
        self.colors_slider.mousePressEvent = self._slider_jump_to_click(self.colors_slider)
        self.colors_slider.mouseMoveEvent = self._slider_drag_to_move(self.colors_slider)
        self.colors_spin = QSpinBox()
        self.colors_spin.setRange(1, 24)
        self.colors_spin.setValue(8)
        self.colors_spin.setFixedWidth(70)
        self.colors_spin.setToolTip('Palette size as power of two: 1=>2, ... 24=>16,777,216 (24-bit RGB).')
        self.colors_slider.valueChanged.connect(self.colors_spin.setValue)
        self.colors_spin.valueChanged.connect(self.colors_slider.setValue)
        import multiprocessing as _mp
        cpu_count = max(1, _mp.cpu_count())
        default_workers = max(1, cpu_count - 1)
        self.workers_slider = QSlider(Qt.Horizontal)
        self.workers_slider.setRange(0, max(1, min(64, cpu_count)))
        # Righette per ogni worker possibile (massimo pratico)
        worker_range = max(1, min(64, cpu_count))
        if worker_range <= 16:
            # Se pochi core, mostra una righetta per ogni worker
            self.workers_slider.setTickInterval(1)
        else:
            # Se molti core, righette ogni 2 o 4 per non sovraffollare
            self.workers_slider.setTickInterval(2 if worker_range <= 32 else 4)
        self.workers_slider.setSingleStep(1)
        try:
            self.workers_slider.setTickPosition(QSlider.TicksBelow)
        except Exception:
            pass
        try:
            self.workers_slider.setTracking(True)
        except Exception:
            pass
        self.workers_slider.mousePressEvent = self._slider_jump_to_click(self.workers_slider)
        self.workers_slider.mouseMoveEvent = self._slider_drag_to_move(self.workers_slider)
        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(0, max(1, min(64, cpu_count)))
        self.workers_spin.setValue(default_workers)
        self.workers_spin.setFixedWidth(70)
        self.workers_spin.setToolTip(f'Numero di processi di compressione. 0 = automatico (max {cpu_count}). Predefinito: CPU-1 = {default_workers}.')
        self.workers_slider.valueChanged.connect(self.workers_spin.setValue)
        self.workers_spin.valueChanged.connect(self.workers_slider.setValue)
        # Assicura la sincronizzazione iniziale: porta lo slider al valore corrente dello spin
        try:
            self.workers_slider.setValue(self.workers_spin.value())
        except Exception:
            pass
        self.ram_slider = QSlider(Qt.Horizontal)
        self.ram_slider.setRange(10, 95)
        # Rimuovi paletti/righette per RAM per un look pulito
        self.ram_slider.setSingleStep(1)
        try:
            self.ram_slider.setTickPosition(QSlider.NoTicks)
        except Exception:
            pass
        try:
            self.ram_slider.setTracking(True)
        except Exception:
            pass
        self.ram_slider.mousePressEvent = self._slider_jump_to_click(self.ram_slider)
        self.ram_slider.mouseMoveEvent = self._slider_drag_to_move(self.ram_slider)
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
        # Evita etichetta orfana "Device:" che in alcune piattaforme appare come finestra/tooltip
        self.lbl_device = QLabel(self)
        self.lbl_device.setVisible(False)
        self.lbl_mode = QLabel()
        try:
            self.lbl_mode.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        except Exception:
            pass
        self.lbl_quality = QLabel()
        self.lbl_colors = QLabel()
        self.lbl_workers = QLabel()
        self.lbl_ram = QLabel()
        # Riga Brand/Model in un'unica riga, a tutta larghezza (niente etichetta "Device")
        adv_dev_row = QHBoxLayout()
        adv_dev_row.addWidget(self.lbl_brand_adv)
        adv_dev_row.addWidget(self.advanced_brand_combo)
        adv_dev_row.addWidget(self.lbl_model_adv)
        adv_dev_row.addWidget(self.advanced_model_combo, 1)
        opts_grid.addLayout(adv_dev_row, 0, 0, 1, 2)

        # Pulsante per mostrare/nascondere la creazione di un dispositivo personalizzato
        self.create_custom_btn = QPushButton(' Create a custom device')
        self.create_custom_btn.setIcon(self._icon('file-plus'))
        self.create_custom_btn.setToolTip('Crea un dispositivo personalizzato partendo dal modello selezionato')
        self.create_custom_btn.clicked.connect(self._on_create_custom_clicked)
        self.create_custom_btn.setCheckable(True)  # Permette di mantenere lo stato premuto
        opts_grid.addWidget(self.create_custom_btn, 1, 0, 1, 2)

        # Campi custom (nascosti di default), immediatamente sotto Brand/Model, su due righe
        # Labels per chiarire i campi
        self.custom_name_label = QLabel('Name:')
        self.custom_name_edit = QLineEdit()
        self.custom_name_edit.setPlaceholderText('Custom')
        self.custom_inches_label = QLabel('Diagonal (in):')
        self.custom_inches = QDoubleSpinBox()
        self.custom_inches.setRange(4.0, 30.0)
        self.custom_inches.setSingleStep(0.1)
        self.custom_inches.setDecimals(1)
        self.custom_inches.setValue(10.0)
        self.custom_w_label = QLabel('Width (px):')
        self.custom_w_spin = QSpinBox()
        self.custom_w_spin.setRange(600, 6000)
        self.custom_w_spin.setValue(1600)
        self.custom_h_label = QLabel('Height (px):')
        self.custom_h_spin = QSpinBox()
        self.custom_h_spin.setRange(600, 6000)
        self.custom_h_spin.setValue(2560)
        self.custom_dpi_label = QLabel('DPI:')
        self.custom_dpi_spin = QSpinBox()
        self.custom_dpi_spin.setRange(96, 600)
        self.custom_dpi_spin.setValue(300)
        # Tooltips
        self.custom_name_edit.setToolTip('Nome modello personalizzato')
        self.custom_inches.setToolTip('Diagonale in pollici (in)')
        self.custom_w_spin.setToolTip('Larghezza in pixel')
        self.custom_h_spin.setToolTip('Altezza in pixel')
        self.custom_dpi_spin.setToolTip('Densità (dots per inch)')

        # Pulsante salvataggio custom (icona migliorata)
        self.save_custom_btn = QPushButton(' Save device')
        self.save_custom_btn.setIcon(self._icon('sliders'))  # Icona più appropriata per device settings
        self.save_custom_btn.setToolTip('Salva questo dispositivo sotto il brand "Customs"')
        self.save_custom_btn.clicked.connect(self._on_save_custom_device)

        # Container riga 1 (nascosto di default)
        self.cust_container1 = QWidget()
        cust_row1 = QHBoxLayout(self.cust_container1)
        cust_row1.setSpacing(8)
        # Prima riga: Name, Diagonal (in), DPI(PPI) - centrati
        cust_row1.addStretch(1)
        cust_row1.addWidget(self.custom_name_label)
        cust_row1.addWidget(self.custom_name_edit)
        cust_row1.addWidget(self.custom_inches_label)
        cust_row1.addWidget(self.custom_inches)
        cust_row1.addWidget(self.custom_dpi_label)
        cust_row1.addWidget(self.custom_dpi_spin)
        cust_row1.addStretch(1)
        self.cust_container1.setVisible(False)

        # Container riga 2 (nascosto di default)
        self.cust_container2 = QWidget()
        cust_row2 = QHBoxLayout(self.cust_container2)
        cust_row2.setSpacing(8)
        # Seconda riga: Width (px), Height (px), Save device - centrati
        cust_row2.addStretch(1)
        cust_row2.addWidget(self.custom_w_label)
        cust_row2.addWidget(self.custom_w_spin)
        cust_row2.addWidget(self.custom_h_label)
        cust_row2.addWidget(self.custom_h_spin)
        cust_row2.addWidget(self.save_custom_btn)
        cust_row2.addStretch(1)
        self.cust_container2.setVisible(False)

        # Posiziona i container nascosti in griglia (righe 2 e 3)
        opts_grid.addWidget(self.cust_container1, 2, 0, 1, 2)
        opts_grid.addWidget(self.cust_container2, 3, 0, 1, 2)

        # Riga Modalità: usa un layout orizzontale per avvicinare label e select (spostata più in basso)
        h_mode = QHBoxLayout()
        h_mode.setSpacing(6)
        h_mode.addWidget(self.lbl_mode)
        h_mode.addWidget(self.mode_combo, 1)
        # Dalla riga 4 in poi, dopo il bottone e i container custom
        opts_grid.addLayout(h_mode, 4, 0, 1, 2)
        opts_grid.addWidget(self.lbl_quality, 5, 0)
        qrow = QHBoxLayout()
        qrow.addWidget(self.quality_slider, 1)
        qrow.addWidget(self.quality_spin)
        opts_grid.addLayout(qrow, 5, 1)
        opts_grid.addWidget(self.lbl_colors, 6, 0)
        crow = QHBoxLayout()
        crow.addWidget(self.colors_slider, 1)
        crow.addWidget(self.colors_spin)
        opts_grid.addLayout(crow, 6, 1)
        opts_grid.addWidget(self.lbl_workers, 7, 0)
        wrow = QHBoxLayout()
        wrow.addWidget(self.workers_slider, 1)
        wrow.addWidget(self.workers_spin)
        opts_grid.addLayout(wrow, 7, 1)
        opts_grid.addWidget(self.lbl_ram, 8, 0)
        rrow = QHBoxLayout()
        rrow.addWidget(self.ram_slider, 1)
        rrow.addWidget(self.ram_spin)
        opts_grid.addLayout(rrow, 8, 1)
        opts_grid.addWidget(self.save_defaults_chk, 9, 0, 1, 2)
        layout.addLayout(opts_grid)
        # (Rimossi) pannello ed etichetta "Optional target device" e checkbox "Use custom device"
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        try:
            self.progress.setTextVisible(True)
        except Exception:
            pass
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
        try:
            # evitiamo casi in cui il bottone risulti visivamente attivo ma non riceva click (overlay/stacking)
            self.start_btn.setAttribute(Qt.WA_TransparentForMouseEvents, False)
            from PySide6.QtCore import Qt as _Qt
            self.start_btn.setFocusPolicy(_Qt.StrongFocus)
        except Exception:
            pass
        self.stop_btn = QPushButton(' Stop')
        self.stop_btn.setObjectName('stopButton')
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

        # Language selector: dropdown with emoji + label in alphabetical order
        self.language_combo = QComboBox()
        self.language_combo.setObjectName('languageCombo')
        self._build_language_combo()
        self.language_combo.currentIndexChanged.connect(self._on_language_changed_from_combo)
        btn_row2.addWidget(self.language_combo)

        self.mode_btn = QPushButton()
        self.mode_btn.setObjectName('uiModeButton')
        self.mode_btn.setIcon(self._icon('advanced' if self.ui_mode == 'advanced' else 'simple'))
        self.mode_btn.clicked.connect(self.on_toggle_ui_mode)
        btn_row2.addWidget(self.mode_btn)
        layout.addLayout(btn_row2)
        self._build_preview_panel(root)
        self.setAcceptDrops(True)
        self.files_list.installEventFilter(self)
        try:
            # aggiorna stima quando cambia elemento selezionato
            self.files_list.currentItemChanged.connect(self._on_current_file_changed)
        except Exception:
            pass
        # Validazione cartelle e abilitazione Start
        try:
            self.out_dir_edit.textChanged.connect(self._validate_dirs_enable_start)
        except Exception:
            pass
        # stato iniziale dei bottoni
        self._update_files_buttons_state()
        self._validate_dirs_enable_start()

    def _init_progress_tracking(self, files: list[str]):
        try:
            # Pesa i file per dimensione per stima durate e crea segmenti
            sizes = []
            for f in files:
                try:
                    s = int(Path(f).stat().st_size)
                except Exception:
                    s = 1
                sizes.append(max(1, s))
            self._progress_file_sizes = sizes
            self._progress_total_bytes = sum(sizes)
            self._progress_n_files = len(files)
            # Setup animazione a segmenti
            self._anim_total_files = self._progress_n_files
            self._anim_index = 0
            self._display_count = 0
            self._anim_step = (100.0 / max(1, self._anim_total_files)) if self._anim_total_files > 0 else 0.0
            now = time.monotonic()
            self._anim_seg_start_time = now
            self._anim_seg_target_dur = 20.0 if self._anim_total_files > 0 else 0.0  # ~20s primo pezzo
            # segmenti: [i/N, (i+1)/N]
            start_frac = 0.0
            end_frac = self._anim_step
            self._anim_seg_start_frac = start_frac
            self._anim_seg_end_frac = end_frac
            # configura progress bar
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.progress.setFormat(f"{self._display_count}/{self._anim_total_files}")
            # avvia timer animazione
            self._progress_timer.start()
            self._start_pulse_animation()
            # reset storico throughput
            self._throughput_hist.clear()
        except Exception:
            pass

    def _on_progress_tick(self):
        try:
            # Animazione lineare del segmento corrente
            N = max(1, int(self._anim_total_files))
            if N <= 0:
                self.progress.setValue(0)
                self.progress.setFormat("")
                return
            now = time.monotonic()
            elapsed = max(0.0, now - float(self._anim_seg_start_time))
            dur = max(0.001, float(self._anim_seg_target_dur)) if self._anim_total_files > 0 else 15.0
            t = min(1.0, elapsed / dur)
            start_v = float(self._anim_seg_start_frac)
            end_v = float(self._anim_seg_end_frac)
            cur = start_v + (end_v - start_v) * t
            # Non oltrepassare il bordo del segmento finché non arriva file_done
            cur = min(cur, end_v)
            self.progress.setValue(int(round(cur)))
            # Etichetta: conteggio file completati
            self.progress.setFormat(f"{self._display_count}/{self._anim_total_files}")
        except Exception:
            pass

    def _start_pulse_animation(self):
        try:
            if not self._pulse_active:
                self._pulse_active = True
                self._pulse_phase = 0.0
                self._pulse_timer.start()
        except Exception:
            pass

    def _stop_pulse_animation(self):
        try:
            self._pulse_active = False
            self._pulse_timer.stop()
            # ripristina stile di default della progress bar
            self.progress.setStyleSheet('')
        except Exception:
            pass

    def _update_progressbar_pulse(self):
        """Aggiorna lo stile della progress bar con un gradiente scorrevole (effetto pulsante)."""
        try:
            self._pulse_phase = (self._pulse_phase + 0.06) % 1.0
            base = '#3b82f6' if self.theme == 'light' else '#00bcd4'
            hi = '#93c5fd' if self.theme == 'light' else '#4de1ee'
            lo = base
            p = self._pulse_phase
            w = 0.12
            p1 = max(0.0, p - w)
            p2 = p
            p3 = min(1.0, p + w)
            style = (
                "QProgressBar { border: 1px solid transparent; border-radius: 6px; text-align: center; }\n"
                f"QProgressBar::chunk {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
                f" stop:0 {lo}, stop:{p1:.3f} {lo}, stop:{p2:.3f} {hi}, stop:{p3:.3f} {lo}, stop:1 {lo}); }}"
            )
            self.progress.setStyleSheet(style)
        except Exception:
            pass

    def _build_preview_panel(self, root_layout):
        from PySide6.QtWidgets import QVBoxLayout, QHBoxLayout
        self.preview_panel = QWidget()
        pv = QVBoxLayout(self.preview_panel)
        pv.setContentsMargins(6, 0, 0, 0)
        pv.setSpacing(8)
        # Title + small usage hint (zoom/pan)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        self.preview_title = QLabel('Anteprima qualità')
        self.preview_hint = QLabel('')
        try:
            self.preview_hint.setStyleSheet('color: #999; font-size: 11px;')
        except Exception:
            pass
        title_row.addWidget(self.preview_title)
        title_row.addStretch(1)
        title_row.addWidget(self.preview_hint)
        pv.addLayout(title_row)
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
        # rimuovere etichetta di livello (Normal/High ecc.) perché ridondante
        self.preview_level_label = QLabel('')
        self.preview_level_label.setVisible(False)
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
            # Non mostrare più il testo del livello
            self.preview_level_label.setText('')
        except Exception:
            pass

    def _load_defaults_into_ui(self):
        d = self.defaults
        out_dir = d.get('out_dir')
        if out_dir:
            try:
                self.out_dir_edit.setText(str(Path(out_dir).resolve()))
            except Exception:
                self.out_dir_edit.setText(out_dir)
        else:
            self.out_dir_edit.setText('')
        device = d.get('device', 'tablet_10')
        mode = d.get('mode', 'auto')
        self._set_combo_by_data(self.device_combo, device)
        self._load_devices_model_map()
        self._select_brand_model_by_device_key(device)
        # Populate advanced brand/model and select according to device
        self._populate_advanced_brand_model_from_map()
        self._select_adv_brand_model_by_device_key(device)
        self._set_combo_by_data(self.mode_combo, mode)
        self.quality_spin.setValue(int(d.get('quality', DEFAULT_QUALITY)))
        try:
            c = int(d.get('max_colors', DEFAULT_MAX_COLORS))
        except Exception:
            c = DEFAULT_MAX_COLORS
        c = max(2, min(16777216, c))
        allowed = [2 ** i for i in range(1, 25)]
        P = allowed.index(min(allowed, key=lambda x: abs(x - c))) + 1
        self.colors_spin.setValue(P)
        try:
            import multiprocessing as _mp
            cpu_count = max(1, _mp.cpu_count())
            default_workers = max(1, cpu_count - 1)
        except Exception:
            default_workers = 1
        saved_workers = d.get('workers', None)
        if saved_workers is None or int(saved_workers) == 0:
            self.workers_spin.setValue(default_workers)
        else:
            self.workers_spin.setValue(int(saved_workers))
        self.ram_spin.setValue(int(d.get('ram_limit', 75)))
        self.ram_slider.setValue(int(d.get('ram_limit', 75)))
        self._apply_ui_mode(self.ui_mode)
        self._update_previews(self._current_preset_key() or 'normal')
        # all'avvio, valuta se abilitare Start
        self._validate_dirs_enable_start()

    def _populate_advanced_brand_model_from_map(self):
        try:
            self.advanced_brand_combo.blockSignals(True)
            self.advanced_brand_combo.clear()
            # Ensure 'Customs' appears first, then the rest sorted alphabetically (case-insensitive)
            brands = list(self._devices_map.keys())
            def brand_sort_key(b: str):
                return (0, '') if str(b).lower() == 'customs' else (1, str(b).lower())
            for brand in sorted(brands, key=brand_sort_key):
                self.advanced_brand_combo.addItem(brand, userData=brand)
            self.advanced_brand_combo.blockSignals(False)
            self._on_advanced_brand_changed()
        except Exception:
            pass

    def _select_adv_brand_model_by_device_key(self, key: Optional[str]):
        if not key:
            return
        try:
            for i in range(self.advanced_brand_combo.count()):
                brand = self.advanced_brand_combo.itemData(i)
                for m in self._devices_map.get(brand, []):
                    if m.get('key') == key:
                        self.advanced_brand_combo.setCurrentIndex(i)
                        self._on_advanced_brand_changed()
                        for j in range(self.advanced_model_combo.count()):
                            if self.advanced_model_combo.itemData(j) == key:
                                self.advanced_model_combo.setCurrentIndex(j)
                                return
        except Exception:
            pass

    def _on_advanced_brand_changed(self):
        try:
            brand = self.advanced_brand_combo.currentData()
            models = self._devices_map.get(brand, [])
            self.advanced_model_combo.blockSignals(True)
            self.advanced_model_combo.clear()
            for m in models:
                label = m.get('model', 'Model')
                key = m.get('key')
                details = []
                if 'size' in m:
                    details.append(f"{m['size']}\"")
                if 'resolution' in m:
                    details.append(str(m['resolution']))
                if 'ppi' in m:
                    details.append(f"{m['ppi']} ppi")
                tooltip = ' · '.join(details) if details else None
                self.advanced_model_combo.addItem(label, userData=key)
                if tooltip:
                    idx = self.advanced_model_combo.count() - 1
                    self.advanced_model_combo.setItemData(idx, tooltip, Qt.ToolTipRole)
            self.advanced_model_combo.blockSignals(False)
            # preselect first and update custom fields
            if self.advanced_model_combo.count() > 0:
                self.advanced_model_combo.setCurrentIndex(0)
            if models:
                self._prefill_custom_from_model(models[0])
        except Exception:
            pass

    def _on_advanced_model_changed(self):
        try:
            key = self.advanced_model_combo.currentData()
            brand = self.advanced_brand_combo.currentData()
            if key and brand:
                # sync hidden legacy combo
                self._set_combo_by_data(self.device_combo, key)
                # update custom fields from chosen model
                for m in self._devices_map.get(brand, []):
                    if m.get('key') == key:
                        self._prefill_custom_from_model(m)
                        break
                # If selecting a model under 'Customs', automatically enable custom usage
                if str(brand).lower() == 'customs' and hasattr(self, 'use_custom_chk'):
                    try:
                        self.use_custom_chk.setChecked(True)
                    except Exception:
                        pass
        except Exception:
            pass

    def _prefill_custom_from_model(self, m: dict):
        try:
            name = m.get('model', 'Custom')
            if isinstance(name, str) and name:
                self.custom_name_edit.setText(name)
            size_in = m.get('size')
            try:
                if size_in is not None:
                    self.custom_inches.setValue(float(size_in))
            except Exception:
                pass
            res = m.get('resolution')
            if isinstance(res, str) and 'x' in res.lower():
                try:
                    w_s, h_s = res.lower().split('x', 1)
                    self.custom_w_spin.setValue(int(w_s))
                    self.custom_h_spin.setValue(int(h_s))
                except Exception:
                    pass
            ppi = m.get('ppi')
            try:
                if ppi is not None:
                    self.custom_dpi_spin.setValue(int(ppi))
            except Exception:
                pass
        except Exception:
            pass

    def _on_save_custom_device(self):
        """Save current custom device under brand 'Customs' into user-writable config file."""
        try:
            base = self._user_devices_path()
            data = {}
            if base.exists():
                try:
                    with open(base, 'r', encoding='utf-8') as f:
                        data = json.load(f) or {}
                except Exception:
                    data = {}
            if not isinstance(data, dict):
                data = {}
            brand = 'Customs'
            models = data.get(brand)
            if not isinstance(models, list):
                models = []
            name = (self.custom_name_edit.text() or 'Custom').strip()
            w = int(self.custom_w_spin.value())
            h = int(self.custom_h_spin.value())
            dpi = int(self.custom_dpi_spin.value())
            size_in = float(self.custom_inches.value())
            entry = {'model': name, 'size': round(size_in, 1), 'resolution': f'{w}x{h}', 'ppi': dpi, 'key': 'customs'}
            updated = False
            for i, m in enumerate(models):
                if isinstance(m, dict) and m.get('model') == name:
                    models[i] = entry
                    updated = True
                    break
            if not updated:
                models.append(entry)
            data[brand] = models
            with open(base, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            # Reload UI lists
            self._load_devices_model_map()
            self._populate_advanced_brand_model_from_map()
            # Select saved
            try:
                for i in range(self.simple_brand_combo.count()):
                    if self.simple_brand_combo.itemData(i) == brand:
                        self.simple_brand_combo.setCurrentIndex(i)
                        self._on_simple_brand_changed()
                        for j in range(self.simple_model_combo.count()):
                            if self.simple_model_combo.itemText(j) == name:
                                self.simple_model_combo.setCurrentIndex(j)
                                break
                        break
                for i in range(self.advanced_brand_combo.count()):
                    if self.advanced_brand_combo.itemData(i) == brand:
                        self.advanced_brand_combo.setCurrentIndex(i)
                        self._on_advanced_brand_changed()
                        for j in range(self.advanced_model_combo.count()):
                            if self.advanced_model_combo.itemText(j) == name:
                                self.advanced_model_combo.setCurrentIndex(j)
                                break
                        break
            except Exception:
                pass
            self.on_log(f'Saved custom device: {brand} / {name}')
        except Exception as e:
            self.on_log(f'Error: cannot save custom device: {e}')

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
            sheet = '\n            QWidget { background: #f7f7f7; color: #1f2937; }\n            QLineEdit, QListWidget { background: #ffffff; border: 1px solid #d1d5db; padding: 6px; }\n            /* Imposta esplicitamente colori delle righe per evitare righe nere */\n            QListWidget { alternate-background-color: #f9fafb; }\n            QListWidget::item { background: #ffffff; color: #111827; }\n            QListWidget::item:alternate { background: #f9fafb; }\n            /* Evidenziazione lista file (light) */\n            QListWidget::item:hover { background: #f3f4f6; }\n            QListWidget::item:selected { background: #dbeafe; color: #111827; }\n            QListWidget::item:selected:!active { background: #e5effe; color: #1f2937; }\n            QComboBox, QSpinBox, QSlider { background: #ffffff; border: 1px solid #d1d5db; padding: 3px; }\n            QPushButton { background: #e5e7eb; border: 1px solid #cbd5e1; padding: 6px 10px; border-radius: 6px; }\n            QPushButton:hover { background: #dfe3ea; }\n            QPushButton:disabled { background: #e5e7eb; color: #9ca3af; }\n            QProgressBar { background: #ffffff; border: 1px solid #d1d5db; border-radius: 6px; text-align: center; }\n            QProgressBar::chunk { background: #3b82f6; }\n            QLabel { color: #111827; }\n            /* Checkbox visibile su tema chiaro */\n            QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #6b7280; background: #ffffff; border-radius: 3px; }\n            QCheckBox::indicator:checked { background: #2563eb; border: 1px solid #1d4ed8; }\n            /* Preset attivo */\n            QPushButton[preset="true"]:checked { background: #2563eb; color: white; border: 1px solid #1d4ed8; }\n            QPushButton[preset="true"]:hover { background: #3b82f6; color: white; }\n            QPushButton[preset="true"]:checked:hover { background: #1d4ed8; color: white; }\n\n            /* Bottoni colorati */\n            #startButton { background: #16a34a; color: white; border: 1px solid #15803d; }\n            #startButton:hover { background: #22c55e; }\n            #stopButton { background: #dc2626; color: white; border: 1px solid #b91c1c; }\n            #stopButton:hover { background: #ef4444; }\n            #stopButton:disabled { background: #e5e7eb; color: #9ca3af; border: 1px solid #cbd5e1; }\n            #openOutputButton { background: #2563eb; color: white; border: 1px solid #1d4ed8; }\n            #openOutputButton:hover { background: #3b82f6; }\n            #removeButton, #clearButton { background: #b45309; color: white; border: 1px solid #92400e; }\n            #removeButton:hover, #clearButton:hover { background: #d97706; }\n            #removeButton:disabled, #clearButton:disabled { background: #e5e7eb; color: #9ca3af; border: 1px solid #cbd5e1; }\n\n            /* pulsanti spinbox standard */\n            QSpinBox::up-button { subcontrol-origin: border; subcontrol-position: top right; }\n            QSpinBox::down-button { subcontrol-origin: border; subcontrol-position: bottom right; }\n            '
            self.theme_btn.setIcon(self._icon('sun'))
            try:
                self.preview_view1.setStyleSheet('QGraphicsView { border: 1px solid #d1d5db; background: #ffffff; }')
                self.preview_view2.setStyleSheet('QGraphicsView { border: 1px solid #d1d5db; background: #ffffff; }')
            except Exception:
                pass
        else:
            sheet = '\n            QWidget { background: #0f1419; color: #eef2f5; }\n            QLineEdit, QListWidget { background: #1b2229; border: 1px solid #2b3540; padding: 6px; }\n            /* Evidenziazione lista file (dark) */\n            QListWidget::item:hover { background: #25303a; }\n            QListWidget::item:selected { background: #1e3a8a; color: #ffffff; }\n            QListWidget::item:selected:!active { background: #1f2a44; color: #ffffff; }\n            QComboBox, QSpinBox, QSlider { background: #1b2229; border: 1px solid #2b3540; padding: 3px; }\n            QPushButton { background: #2b3540; border: 1px solid #3a4653; padding: 6px 10px; border-radius: 6px; }\n            QPushButton:hover { background: #354252; }\n            QPushButton:disabled { background: #20262d; color: #8b98a5; }\n            QProgressBar { background: #1b2229; border: 1px solid #2b3540; border-radius: 6px; text-align: center; }\n            QProgressBar::chunk { background: #00bcd4; }\n            QLabel { color: #c9d1d9; }\n            /* Preset attivo */\n            QPushButton[preset="true"]:checked { background: #2563eb; color: white; border: 1px solid #1d4ed8; }\n            QPushButton[preset="true"]:checked:hover { background: #3b82f6; color: white; }\n\n            /* Bottoni colorati */\n            #startButton { background: #16a34a; color: white; border: 1px solid #15803d; }\n            #startButton:hover { background: #22c55e; }\n            #stopButton { background: #b91c1c; color: white; border: 1px solid #991b1b; }\n            #stopButton:hover { background: #dc2626; }\n            #stopButton:disabled { background: #20262d; color: #8b98a5; border: 1px solid #3a4653; }\n            #openOutputButton { background: #2563eb; color: white; border: 1px solid #1d4ed8; }\n            #openOutputButton:hover { background: #3b82f6; }\n            #removeButton, #clearButton { background: #b45309; color: white; border: 1px solid #92400e; }\n            #removeButton:hover, #clearButton:hover { background: #d97706; }\n            #removeButton:disabled, #clearButton:disabled { background: #20262d; color: #8b98a5; border: 1px solid #3a4653; }\n\n            /* pulsanti spinbox standard */\n            QSpinBox::up-button { subcontrol-origin: border; subcontrol-position: top right; }\n            QSpinBox::down-button { subcontrol-origin: border; subcontrol-position: bottom right; }\n            '
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
                # Mostra l'azione: clic su "Dark" imposta tema scuro, e viceversa
                self.theme_btn.setText(t['dark'] if theme == 'light' else t['light'])
            if hasattr(self, 'mode_btn'):
                self.mode_btn.setIcon(self._icon('advanced' if self.ui_mode == 'advanced' else 'simple'))
        except Exception:
            pass
        for btn_name in ('addButton', 'removeButton', 'clearButton', 'outButton', 'startButton', 'openOutputButton', 'themeButton', 'uiModeButton', 'clearLogButton'):
            b = self.findChild(QPushButton, btn_name)
            if not b:
                continue
            icon_map = {'addButton': 'file-plus', 'removeButton': 'trash', 'clearButton': 'trash', 'outButton': 'folder-open', 'startButton': 'play', 'openOutputButton': 'folder-open', 'themeButton': 'sun' if theme == 'light' else 'moon', 'uiModeButton': 'advanced' if self.ui_mode == 'advanced' else 'simple', 'clearLogButton': 'trash'}
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
            ob = self.findChild(QPushButton, 'openOutputButton')
            if ob:
                ob.setIcon(self._icon('folder-open', color_override=Qt.white))
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
            if hasattr(self, 'preview_hint'):
                self.preview_hint.setText(t.get('preview_hint', 'Scroll to zoom, drag to pan'))
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
        self.out_btn.setText(' Output…')
        self.lbl_device.setText(t['device'])
        self.lbl_mode.setText(t['mode'])
        # Etichette anche per la Advanced mode (brand/model)
        if hasattr(self, 'lbl_brand_adv'):
            self.lbl_brand_adv.setText(t.get('brand', 'Brand:'))
        if hasattr(self, 'lbl_model_adv'):
            self.lbl_model_adv.setText(t.get('model', 'Model:'))
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
            # Mostra l'azione: se il tema attuale è light, proponi "Dark" e viceversa
            self.theme_btn.setText(t['dark'] if self.theme == 'light' else t['light'])
        self.theme_btn.setToolTip(t['toggle_theme'])
        # Language combo label/tooltip
        try:
            if hasattr(self, 'language_combo'):
                self._build_language_combo()  # rebuild to ensure order/labels
                # set to current language
                self._set_combo_by_data(self.language_combo, self.language)
                self.language_combo.setToolTip(t['toggle_language'])
        except Exception:
            pass
        self.open_output_btn.setText(t['open_output'])
        # Mostra l'azione: se sei in advanced, proponi "Simple" e viceversa
        self.mode_btn.setText(' ' + (t['simple_mode'] if self.ui_mode == 'advanced' else t['advanced_mode']))
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
        # Disclaimer stime
        if hasattr(self, 'suggestion_disclaimer'):
            self.suggestion_disclaimer.setText(t.get('estimate_disclaimer', '≈'))
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
        """Migliora il comportamento del click sul slider per saltare direttamente al valore cliccato."""
        class SliderClickHandler:
            def __init__(self, parent_slider):
                self.slider = parent_slider
                
            def __call__(self, event):
                try:
                    if event.button() == Qt.LeftButton:
                        # Ottieni la posizione del click relativa al groove del slider
                        x = event.position().x() if hasattr(event, 'position') else event.x()
                        
                        # Calcola i margini del groove (solitamente circa 10px per lato)
                        groove_margin = 10
                        groove_width = max(1, self.slider.width() - 2 * groove_margin)
                        groove_start = groove_margin
                        
                        # Normalizza la posizione nel range [0, 1]
                        relative_x = max(0, min(groove_width, x - groove_start))
                        ratio = relative_x / groove_width
                        
                        # Calcola il nuovo valore nel range del slider
                        value_range = self.slider.maximum() - self.slider.minimum()
                        new_val = self.slider.minimum() + int(round(ratio * value_range))
                        new_val = max(self.slider.minimum(), min(self.slider.maximum(), new_val))
                        
                        self.slider.setValue(new_val)
                        # Accetta l'evento per evitare ulteriore processing
                        event.accept()
                        return
                except Exception:
                    pass
                    
                # Se non è un click sinistro o c'è un errore, usa il comportamento di default
                QSlider.mousePressEvent(self.slider, event)
                
        return SliderClickHandler(slider)

    def _slider_drag_to_move(self, slider: QSlider):
        """Permette di aggiornare il valore mentre si trascina il mouse sul groove."""
        class SliderDragHandler:
            def __init__(self, parent_slider):
                self.slider = parent_slider

            def __call__(self, event):
                try:
                    # Considera solo il tasto sinistro per il drag
                    if event.buttons() & Qt.LeftButton:
                        x = event.position().x() if hasattr(event, 'position') else event.x()
                        groove_margin = 10
                        groove_width = max(1, self.slider.width() - 2 * groove_margin)
                        relative_x = max(0, min(groove_width, x - groove_margin))
                        ratio = relative_x / groove_width
                        value_range = self.slider.maximum() - self.slider.minimum()
                        new_val = self.slider.minimum() + int(round(ratio * value_range))
                        new_val = max(self.slider.minimum(), min(self.slider.maximum(), new_val))
                        if new_val != self.slider.value():
                            self.slider.setValue(new_val)
                        event.accept()
                        return
                except Exception:
                    pass
                QSlider.mouseMoveEvent(self.slider, event)

        return SliderDragHandler(slider)

    def _on_create_custom_clicked(self):
        """Mostra/nasconde i campi custom precompilati con il brand/model correnti."""
        try:
            # Toggle della visibilità dei container
            is_currently_visible = self.cust_container1.isVisible()
            new_visibility = not is_currently_visible
            
            if new_visibility:
                # Mostrando i campi: prefill dai combo avanzati
                brand = self.advanced_brand_combo.currentData()
                key = self.advanced_model_combo.currentData()
                if brand and key:
                    for m in self._devices_map.get(brand, []):
                        if m.get('key') == key:
                            self._prefill_custom_from_model(m)
                            break
                # Mostra i container e porta il focus al nome
                self.cust_container1.setVisible(True)
                self.cust_container2.setVisible(True)
                # Cambia il testo e rimuovi l'icona
                self.create_custom_btn.setText(' Hide custom device fields')
                self.create_custom_btn.setIcon(QIcon())  # Rimuove l'icona
                self.create_custom_btn.setToolTip('Nascondi i campi del dispositivo personalizzato')
                self.create_custom_btn.setChecked(True)
                self.custom_name_edit.setFocus()
            else:
                # Nascondendo i campi: ripristina stato originale
                self.cust_container1.setVisible(False)
                self.cust_container2.setVisible(False)
                # Ripristina il testo originale e l'icona
                self.create_custom_btn.setText(' Create a custom device')
                self.create_custom_btn.setIcon(self._icon('file-plus'))
                self.create_custom_btn.setToolTip('Crea un dispositivo personalizzato partendo dal modello selezionato')
                self.create_custom_btn.setChecked(False)
        except Exception:
            pass

    def _shorten(self, text: str, max_len: int = 32) -> str:
        try:
            s = str(text)
            return s if len(s) <= max_len else (s[: max_len - 1] + '…')
        except Exception:
            return text

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
            # Mostra path completo
            item = QListWidgetItem(f)
            item.setData(Qt.UserRole, f)
            item.setToolTip(f)
            self.files_list.addItem(item)
        # aggiorna numerazione
        self._renumber_files()
        self._update_files_buttons_state()
        if files:
            self._last_loaded_file = files[-1]
            self.btn_p_normal.setChecked(True)
            self._apply_preset('normal')
            self._update_suggestion(files[-1])

    def on_remove_selected(self):
        for item in self.files_list.selectedItems():
            self.files_list.takeItem(self.files_list.row(item))
        self._renumber_files()
        # Refresh suggestion bar according to remaining items
        try:
            if self.files_list.count() > 0:
                cur = self.files_list.currentItem() or self.files_list.item(self.files_list.count() - 1)
                if cur:
                    p = cur.data(Qt.UserRole) or cur.text()
                    self._update_suggestion(p)
            else:
                if hasattr(self, 'suggestion_bar'):
                    self.suggestion_bar.setVisible(False)
                if hasattr(self, 'suggestion_label'):
                    self.suggestion_label.setText('')
                if hasattr(self, 'suggestion_total_label'):
                    self.suggestion_total_label.setText('')
        except Exception:
            pass
        self._update_files_buttons_state()

    def _on_clear_files(self):
        try:
            self.files_list.clear()
        except Exception:
            pass
        # Hide suggestion bar and clear labels on full clear
        try:
            if hasattr(self, 'suggestion_bar'):
                self.suggestion_bar.setVisible(False)
            if hasattr(self, 'suggestion_label'):
                self.suggestion_label.setText('')
            if hasattr(self, 'suggestion_total_label'):
                self.suggestion_total_label.setText('')
        except Exception:
            pass
        self._update_files_buttons_state()
        self._validate_dirs_enable_start()

    def choose_dir(self, line_edit: QLineEdit):
        t = self.i18n[self.language]
        d = QFileDialog.getExistingDirectory(self, t['choose_dir'], str(Path.cwd()))
        if d:
            line_edit.setText(str(Path(d).resolve()))
            self._validate_dirs_enable_start()

    def on_start(self):
        # reset flag stop
        self._stop_requested = False
        files = []
        for i in range(self.files_list.count()):
            it = self.files_list.item(i)
            files.append(it.data(Qt.UserRole) or it.text())
        if not files:
            t = self.i18n[self.language]
            QMessageBox.warning(self, t['no_files_title'], t['add_at_least'])
            return
        # cartella output: richiedi selezione esplicita
        out_text = self.out_dir_edit.text().strip()
        if not out_text:
            QMessageBox.warning(self, 'Missing output', 'select an output dir first please')
            return
        out_dir = Path(out_text).resolve()
        tmp_dir = (out_dir / 'tmp').resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        self.current_out_dir = out_dir
        self.current_tmp_dir = tmp_dir
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
        if not 1 <= P <= 24:
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
            args = type('Args', (), {'device': device, 'mode': mode, 'quality': quality, 'max_colors': max_colors, 'workers': workers, 'ram_limit': ram_limit, 'suffix': None, 'out_dir': str(out_dir), 'ui_mode': self.ui_mode, 'language': self.language})
            try:
                save_default_config(args)
                self.on_log(self.i18n[self.language]['defaults_saved'])
            except Exception as e:
                self.on_log(self.i18n[self.language]['cannot_save_defaults'].format(err=e))
        # inizializza tracking progress globale
        self._init_progress_tracking(files)
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
        # aggiorna stima durata del segmento successivo in modo adattivo
        try:
            if self._anim_total_files <= 0:
                return
            # stimiamo tempo per byte dal segmento corrente e regoliamo durata target del prossimo in base alle dimensioni
            idx = int(self._anim_index)
            if 0 <= idx < len(self._progress_file_sizes):
                cur_size = max(1, int(self._progress_file_sizes[idx]))
            else:
                cur_size = 1
            # percent è avanzamento corrente del file, non serve qui per l'animazione lineare
            # Aggiorna proiezione per il prossimo segmento (se disponibile)
            next_idx = idx + 1
            if 0 <= next_idx < len(self._progress_file_sizes):
                next_size = max(1, int(self._progress_file_sizes[next_idx]))
                now = time.monotonic()
                elapsed = max(0.001, now - float(self._anim_seg_start_time))
                # stima tempo per byte attuale (limitato)
                t_per_byte = min(0.5, max(0.000001, elapsed / float(cur_size)))
                est_next = t_per_byte * float(next_size)
                # smoothing leggero sulla stima futura
                self._anim_seg_est_next = 0.7 * getattr(self, '_anim_seg_est_next', est_next) + 0.3 * est_next
                # clamp durata a [2s, 60s]
                self._anim_est_next_dur = float(min(60.0, max(2.0, self._anim_seg_est_next)))
        except Exception:
            pass

    def on_file_done(self, file: str, output: str):
        self.on_log(f'Done: {file} -> {output}')
        # avanza al segmento successivo e aggiorna k/N
        try:
            # porta la barra alla fine del segmento corrente
            if self._anim_total_files > 0:
                target_end = min(100.0, (self._display_count + 1) * float(getattr(self, '_anim_step', 100.0)))
                self.progress.setValue(int(round(target_end)))
            self._display_count = min(self._display_count + 1, self._anim_total_files)
            self.progress.setFormat(f"{self._display_count}/{self._anim_total_files}")
            # se ci sono altri segmenti, prepara il prossimo
            if self._display_count < self._anim_total_files:
                self._anim_index += 1
                self._anim_seg_start_frac = float(self.progress.value())
                # prossimo segmento copre un altro blocco di 100/N
                step = float(getattr(self, '_anim_step', 100.0))
                self._anim_seg_end_frac = min(100.0, self._anim_seg_start_frac + step)
                self._anim_seg_start_time = time.monotonic()
                # durata target adattiva se stimata, altrimenti fallback 15s
                self._anim_seg_target_dur = float(getattr(self, '_anim_est_next_dur', 15.0))
            else:
                # ultimo segmento: completa al 100%
                self.progress.setValue(100)
        except Exception:
            pass

    def on_error(self, message: str):
        self.on_log(f'Error: {message}')

    def on_all_done(self):
        self.on_log(self.i18n[self.language]['all_done'])
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        # forza completamento barra, mostra N/N
        try:
            total = int(self._anim_total_files or self._progress_n_files or 0)
            if total > 0:
                self.progress.setValue(100)
                self.progress.setFormat(f"{total}/{total}")
            # poi apri output se non è stato premuto Stop
            if not getattr(self, '_stop_requested', False):
                try:
                    self.on_open_output()
                except Exception:
                    pass
            self._stop_requested = False
            # infine resetta la barra e ferma i timer
            self.progress.setValue(0)
            self.progress.setFormat("")
            self._progress_timer.stop()
            self._stop_pulse_animation()
        except Exception:
            pass
        # rimuovi completamente la directory temporanea out/tmp
        try:
            tmp_dir = getattr(self, 'current_tmp_dir', None)
            if not tmp_dir and getattr(self, 'current_out_dir', None):
                tmp_dir = Path(self.current_out_dir) / 'tmp'
            if tmp_dir:
                tmp_dir = Path(tmp_dir)
                if tmp_dir.exists():
                    import shutil
                    shutil.rmtree(tmp_dir, ignore_errors=True)
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
                        # mostra path completo
                        item = QListWidgetItem(p)
                        item.setData(Qt.UserRole, p)
                        item.setToolTip(p)
                        self.files_list.addItem(item)
                event.acceptProposedAction()
                self._renumber_files()
                self._update_files_buttons_state()
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

    def _build_language_combo(self):
        """Populate the language dropdown with emoji + localized name, alphabetically by label.
        The combo itemData holds the language code (e.g., 'it', 'en').
        """
        try:
            if not hasattr(self, 'language_combo'):
                return
            self.language_combo.blockSignals(True)
            self.language_combo.clear()
            # Map of known flags; default to a globe if unknown
            flag = {
                'en': '🇬🇧', 'it': '🇮🇹', 'es': '🇪🇸', 'fr': '🇫🇷', 'de': '🇩🇪', 'pt': '🇵🇹',
                'ar': '🇸🇦', 'ru': '🇷🇺', 'ja': '🇯🇵', 'ko': '🇰🇷', 'zh': '🇨🇳'
            }
            # Default native names as fallback when json doesn't provide one
            native = {
                'en': 'English', 'it': 'Italiano', 'es': 'Español', 'fr': 'Français', 'de': 'Deutsch',
                'pt': 'Português', 'ar': 'العربية', 'ru': 'Русский', 'ja': '日本語', 'ko': '한국어', 'zh': '中文'
            }
            entries = []
            for code, data in self.i18n.items():
                name = (data.get('language_native') if isinstance(data, dict) else None) or native.get(code.lower(), code.upper())
                emoji = flag.get(code.lower(), '🌐')
                entries.append((name, emoji, code))
            # Build labels, compute required pixel width and set a sensible minimum so full names are visible
            labels = []
            sorted_entries = sorted(entries, key=lambda x: x[0].lower())
            for name, emoji, code in sorted_entries:
                labels.append((f"{emoji}  {name}", code))

            try:
                # Add items and measure the widest label in pixels
                fm = self.language_combo.fontMetrics()
                max_w = 0
                for label, code in labels:
                    w = fm.horizontalAdvance(label)
                    if w > max_w:
                        max_w = w
                    self.language_combo.addItem(label, userData=code)
                # Add padding for the arrow/frames and a minimum baseline width
                padding = 40
                min_base = 140
                self.language_combo.setMinimumWidth(max(min_base, max_w + padding))
                # Also prefer adjusting to contents where supported
                try:
                    from PySide6.QtWidgets import QComboBox as _QB
                    self.language_combo.setSizeAdjustPolicy(_QB.AdjustToContents)
                except Exception:
                    pass
            except Exception:
                # Fallback: add items without measurements
                for label, code in labels:
                    self.language_combo.addItem(label, userData=code)
            # Select current
            self._set_combo_by_data(self.language_combo, self.language)
        except Exception:
            pass
        finally:
            try:
                self.language_combo.blockSignals(False)
            except Exception:
                pass

    def _on_language_changed_from_combo(self):
        try:
            code = self.language_combo.currentData()
            if code and code != self.language:
                self.apply_language(code)
                self._schedule_save()
        except Exception:
            pass

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
            # Give a bit more room to avoid truncation in most locales
            if is_simple:
                self.setFixedWidth(1120)
            else:
                self.setFixedWidth(780)
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
        try:
            self.lbl_device.setVisible(False)
        except Exception:
            pass
        # hide legacy device combo; show new brand/model controls
        self.device_combo.setVisible(False)
        if hasattr(self, 'lbl_brand_adv'):
            self.lbl_brand_adv.setVisible(not is_simple)
        if hasattr(self, 'lbl_model_adv'):
            self.lbl_model_adv.setVisible(not is_simple)
        if hasattr(self, 'advanced_brand_combo'):
            self.advanced_brand_combo.setVisible(not is_simple)
        if hasattr(self, 'advanced_model_combo'):
            self.advanced_model_combo.setVisible(not is_simple)
        # Bottone per creare custom e contenitori relativi
        if hasattr(self, 'create_custom_btn'):
            self.create_custom_btn.setVisible(not is_simple)
        if hasattr(self, 'cust_container1'):
            self.cust_container1.setVisible(False if is_simple else self.cust_container1.isVisible())
        if hasattr(self, 'cust_container2'):
            self.cust_container2.setVisible(False if is_simple else self.cust_container2.isVisible())
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

    def _user_config_dir(self) -> Path:
        try:
            xdg = os.environ.get('XDG_CONFIG_HOME')
            if xdg:
                return Path(xdg) / 'imgpdfsquisher'
        except Exception:
            pass
        return Path.home() / '.config' / 'imgpdfsquisher'

    def _user_devices_path(self) -> Path:
        d = self._user_config_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return d / 'devices.json'

    def _load_devices(self):
        """Load devices merging packaged defaults and user overrides."""
        try:
            root = Path(__file__).parent
            base = {}
            for rel in [Path('devices.json'), Path('assets/devices.json')]:
                p = root / rel
                if p.exists():
                    with open(p, 'r', encoding='utf-8') as f:
                        base = json.load(f) or {}
                        break
            if not isinstance(base, dict):
                base = {}
            # Merge user devices from XDG path
            up = self._user_devices_path()
            if up.exists():
                with open(up, 'r', encoding='utf-8') as f:
                    try:
                        user = json.load(f) or {}
                    except Exception:
                        user = {}
                if isinstance(user, dict):
                    for brand, models in user.items():
                        if not isinstance(models, list):
                            continue
                        base_list = list(base.get(brand, []))
                        name_index = {m.get('model'): i for i, m in enumerate(base_list) if isinstance(m, dict)}
                        for m in models:
                            if not isinstance(m, dict):
                                continue
                            name = m.get('model')
                            if name in name_index:
                                base_list[name_index[name]] = m
                            else:
                                base_list.append(m)
                        base[brand] = base_list
            return base
        except Exception:
            return None

    def _update_suggestion(self, file_path: str):
        """Update the estimate bar with selected file estimate (left) and total (right)."""
        try:
            # Hide bar if no files are loaded
            if self.files_list.count() == 0:
                if hasattr(self, 'suggestion_bar'):
                    self.suggestion_bar.setVisible(False)
                if hasattr(self, 'suggestion_disclaimer'):
                    self.suggestion_disclaimer.setVisible(False)
                return

            t = self.i18n.get(self.language, {})
            # Labels (fallbacks if missing in i18n)
            sel_label_key = t.get('estimate_selected') or t.get('estimate_label') or 'Estimated:'
            tot_label_key = t.get('estimate_total') or 'All files:'

            # Current preset and mapping
            preset_key = self._current_preset_key() or 'normal'
            ratio_map = {
                'ultra': 0.82, 'very_high': 0.72, 'high': 0.65,
                'normal': 0.50, 'low': 0.38, 'very_low': 0.33, 'minimal': 0.28
            }
            overhead = 180 * 1024
            ratio = ratio_map.get(preset_key, 0.50)

            # Helper for one file estimate (prefer precise, fallback to heuristic)
            has_precise_estimates = False
            def estimate_for(path: str) -> tuple[int, int]:
                nonlocal has_precise_estimates
                try:
                    orig = max(1, int(Path(path).stat().st_size))
                except Exception:
                    return (1, 1)
                precise = self._estimate_output_size_precise(path)
                if precise is not None:
                    has_precise_estimates = True
                    est = min(max(1, precise), orig)
                else:
                    # Fallback to heuristic estimate
                    est = int(orig * ratio + overhead)
                    est = min(max(1, est), orig)
                return (orig, est)

            # Selected file estimate (left)
            sel_path = file_path
            # indice file (1-based)
            file_index = None
            for i in range(self.files_list.count()):
                it = self.files_list.item(i)
                p = it.data(Qt.UserRole) or it.text()
                if p == sel_path:
                    file_index = i + 1
                    break
            sel_orig, sel_est = estimate_for(sel_path) if sel_path else (1, 1)
            sel_red = max(0, int(round((1 - sel_est / max(1, sel_orig)) * 100)))
            sel_mb = sel_est / (1024 * 1024)
            # testo specifico per file N
            try:
                prefix_tmpl = (self.i18n.get(self.language, {}) or {}).get('estimate_selected')
            except Exception:
                prefix_tmpl = None
            if not prefix_tmpl:
                prefix_tmpl = sel_label_key + ' {n}:'
            prefix = prefix_tmpl.format(n=file_index if file_index is not None else '?')
            self.suggestion_label.setText(f"{prefix} ~{sel_mb:.1f} MB (−{sel_red}%)")

            # Total estimate (right)
            tot_orig = 0
            tot_est = 0
            for i in range(self.files_list.count()):
                it = self.files_list.item(i)
                p = it.data(Qt.UserRole) or it.text()
                o, e = estimate_for(p)
                tot_orig += o
                tot_est += e
            tot_red = max(0, int(round((1 - (tot_est / max(1, tot_orig))) * 100)))
            tot_mb = tot_est / (1024 * 1024)
            self.suggestion_total_label.setText(f"{tot_label_key} ~{tot_mb:.1f} MB (−{tot_red}%)")

            # Show the bar
            if hasattr(self, 'suggestion_bar'):
                self.suggestion_bar.setVisible(True)
            # Show disclaimer only if we have actual estimates (not just heuristics)
            if hasattr(self, 'suggestion_disclaimer'):
                self.suggestion_disclaimer.setVisible(has_precise_estimates)

            # Keep suggested preset reference (for potential future logic)
            self._suggested_preset = preset_key
        except Exception:
            try:
                if hasattr(self, 'suggestion_bar'):
                    self.suggestion_bar.setVisible(False)
                if hasattr(self, 'suggestion_disclaimer'):
                    self.suggestion_disclaimer.setVisible(False)
            except Exception:
                pass

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
            t = self.i18n.get(self.language, {})
            mapping = {
                'ultra': t.get('preset_ultra', 'Ultra'),
                'very_high': t.get('preset_very_high', 'Very High'),
                'high': t.get('preset_high', 'High'),
                'normal': t.get('preset_normal', 'Normal'),
                'low': t.get('preset_low', 'Low'),
                'very_low': t.get('preset_very_low', 'Very Low'),
                'minimal': t.get('preset_minimal', 'Minimal'),
            }
            return mapping.get(key, key.title())
        except Exception:
            return key.title()

    def _on_current_file_changed(self, current, previous):
        try:
            f = current.data(Qt.UserRole) if current else getattr(self, '_last_loaded_file', None)
            if f:
                self._update_suggestion(f)
        except Exception:
            pass

    def _load_devices_model_map(self):
        self._devices_map = {}
        data = self._load_devices()
        if isinstance(data, dict) and data:
            self._devices_map = {brand: list(models) for brand, models in data.items()}
        if not self._devices_map:
            self._devices_map = {'Generic': [{'model': 'Phone', 'key': 'phone'}, {'model': 'Tablet 7"', 'key': 'tablet_7'}, {'model': 'Tablet 10"', 'key': 'tablet_10'}, {'model': 'Tablet 12"', 'key': 'tablet_12'}, {'model': 'E-reader', 'key': 'ereader'}, {'model': 'Laptop', 'key': 'laptop'}, {'model': 'Desktop', 'key': 'desktop'}]}
        self.simple_brand_combo.blockSignals(True)
        self.simple_brand_combo.clear()
        # Populate simple_brand_combo: exclude 'Customs' and sort remaining brands case-insensitively
        simple_brands = [b for b in self._devices_map.keys() if str(b).lower() != 'customs']
        for brand in sorted(simple_brands, key=lambda x: str(x).lower()):
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
                    'max_colors_range': 'Max-colors power P must be between 1 and 24 (2^P = 2..16,777,216).',
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
                    'estimate_selected': 'Selected:',
                    'estimate_total': 'All files:',
                    'apply_suggestion': 'Apply suggestion',
                    'apply_suggestion_tip': 'Set presets to the suggested quality level',
                    'preview_title': 'Quality preview',
                    'preview_hint': 'Scroll to zoom, drag to pan',
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
                self._stop_requested = True
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
            P = max(1, min(24, P))
            N = 2 ** P
            self.lbl_colors.setText(t['max_colors'] + f' (2^P = {N}; P=1..24):')
        except Exception:
            self.lbl_colors.setText('Max colors (2^P, P=1..24):')

    def _renumber_files(self):
        """Aggiorna la lista con numerazione 1), 2), 3) accanto al percorso."""
        try:
            for i in range(self.files_list.count()):
                it = self.files_list.item(i)
                path = it.data(Qt.UserRole) or it.text()
                it.setText(f"{i+1}) {path}")
                it.setToolTip(path)
        except Exception:
            pass

    def _update_files_buttons_state(self):
        try:
            rb = self.findChild(QPushButton, 'removeButton')
            has_sel = bool(self.files_list.selectedItems())
            if rb:
                rb.setEnabled(has_sel)
            # Aggiorna anche lo stato di Start
            self._validate_dirs_enable_start()
        except Exception:
            pass

    def _validate_dirs_enable_start(self):
        try:
            out_t = self.out_dir_edit.text().strip()
            if not out_t:
                out_t = str((Path.cwd() / 'compressed').resolve())
            # con una sola dir basta che esista una destinazione valida
            ok_dirs = bool(out_t)
            has_files = self.files_list.count() > 0
            not_running = (self.worker is None) or (not self.stop_btn.isEnabled())
            self.start_btn.setEnabled(ok_dirs and has_files and not_running)
            # Stop deve essere attivo solo durante l'elaborazione
            if not not_running:
                self.stop_btn.setEnabled(True)
            else:
                self.stop_btn.setEnabled(False)
        except Exception:
            pass

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

    
    