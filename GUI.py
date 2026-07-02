import sys
import os
import time
import struct
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QComboBox,
                             QSplitter, QProgressBar, QLabel, QInputDialog, QMessageBox,
                             QMenu, QFileDialog, QHeaderView, QListWidget, QListWidgetItem, QAbstractItemView, QDialog, QLineEdit, QDialogButtonBox, QStyle, QListView, QTreeWidget, QTreeWidgetItem, QStackedWidget)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QDir, QSize
from PyQt6.QtGui import QIcon, QAction, QShortcut, QKeySequence
import serial
import serial.tools.list_ports
import socket
import zlib
from datetime import datetime

def format_size(size_in_bytes):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_in_bytes < 1024.0:
            if unit == 'B': return f"{size_in_bytes} B"
            return f"{size_in_bytes:.2f} {unit}"
        size_in_bytes /= 1024.0
    return f"{size_in_bytes:.2f} TB"

class RenameDialog(QDialog):
    def __init__(self, old_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Rename")
        self.resize(300, 100)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("New Name:"))
        self.line_edit = QLineEdit(old_name)
        layout.addWidget(self.line_edit)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)
        
        # Select only name, not extension
        if "." in old_name and not old_name.startswith("."):
            name_part = old_name.rsplit(".", 1)[0]
            self.line_edit.setSelection(0, len(name_part))
        else:
            self.line_edit.selectAll()
            
    def get_name(self):
        return self.line_edit.text()


class Connection:
    def read(self, size): pass
    def write(self, data): pass
    def read_all(self): pass
    def settimeout(self, timeout): pass
    def close(self): pass

class SerialConnection(Connection):
    def __init__(self, port, baudrate, timeout=0.5):
        self.conn = serial.Serial(port, baudrate, timeout=timeout)
        self.conn.setDTR(False)
        self.conn.setRTS(True)
        time.sleep(0.1)
        self.conn.setDTR(False)
        self.conn.setRTS(False)
        time.sleep(0.5)

    def read(self, size):
        return self.conn.read(size)

    def write(self, data):
        return self.conn.write(data)
        
    def read_all(self):
        return self.conn.read_all()

    def settimeout(self, timeout):
        self.conn.timeout = timeout

    def close(self):
        self.conn.close()

class SocketConnection(Connection):
    def __init__(self, ip, port=8080, timeout=0.5):
        self.conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.conn.settimeout(timeout)
        self.conn.connect((ip, port))
        
    def read(self, size):
        try:
            return self.conn.recv(size)
        except socket.timeout:
            return b""
            
    def write(self, data):
        self.conn.sendall(data)
        return len(data)
        
    def read_all(self):
        # Sockets don't have read_all in the same way, return empty for boot clear
        return b""
        
    def settimeout(self, timeout):
        self.conn.settimeout(timeout)
        
    def close(self):
        self.conn.close()

class DeviceWorker(QThread):

    progress_signal = pyqtSignal(int, int) # current, total
    status_signal = pyqtSignal(str)
    dir_loaded_signal = pyqtSignal(list)
    disk_info_signal = pyqtSignal(int, int) # used, total
    error_signal = pyqtSignal(str)
    operation_finished_signal = pyqtSignal(str)
    disconnected_signal = pyqtSignal()

    def __init__(self, connection_string, baudrate=921600):
        super().__init__()
        self.connection_string = connection_string
        self.baudrate = baudrate
        self.conn = None
        self.command_queue = []
        self.running = True

    def run(self):
        try:
            if "." in self.connection_string:
                self.conn = SocketConnection(self.connection_string, port=8080)
            else:
                self.conn = SerialConnection(self.connection_string, self.baudrate)
            
            self.conn.read_all()
            
            self.status_signal.emit("Synchronizing with ESP32...")
            boot_success = False
            
            # Phase 1: Wait for ESP32 to boot by listening for READY
            self.conn.timeout = 0.5
            for _ in range(20): # Max 10 seconds
                line = self._read_line()
                if "READY" in line:
                    boot_success = True
                    break
            
            # Phase 2: If READY was missed, try active ECHO ping
            if not boot_success:
                for _ in range(5):
                    self.conn.write(b"ECHO\n")
                    line = self._read_line()
                    if "ECHO_OK" in line:
                        boot_success = True
                        break

            if boot_success:
                # Phase 3: Flush ESP32's RX buffer with empty lines
                # (empty lines are harmlessly ignored by ESP32 firmware)
                for _ in range(20):
                    self.conn.write(b"\n")
                time.sleep(0.3)
                
                # Phase 4: Drain all responses from Python's RX buffer
                self.conn.timeout = 0.2
                while self.conn.readline():
                    pass
                
                # Phase 5: Final sync - confirm clean channel
                self.conn.timeout = 2
                self.conn.write(b"ECHO\n")
                sync_resp = self._read_line()
                if "ECHO_OK" not in sync_resp:
                    # Drain once more in case of stale data
                    while self.conn.readline():
                        pass
                
                self.conn.timeout = 5
                self.status_signal.emit(f"Connected to {self.port} at {self.baudrate}")
                self.queue_command("DISK_INFO")
                self.queue_command("GET_DIR", "/")
            else:
                self.running = False
                self.error_signal.emit("Timeout: ESP32 not responding. Check baud rate or press RESET button on board.")
                self.status_signal.emit("Connection Failed.")
                self.disconnected_signal.emit()
                return
        except serial.SerialException:
            self.running = False
            self.error_signal.emit(f"Serial port error: Could not open port {self.port}. It might be disconnected or in use by another program.")
            self.disconnected_signal.emit()
            return
        except Exception:
            self.running = False
            self.error_signal.emit(f"Serial port error: An unexpected hardware error occurred on {self.port}.")
            self.disconnected_signal.emit()
            return

        while self.running:
            if self.command_queue:
                cmd_tuple = self.command_queue.pop(0)
                cmd = cmd_tuple[0]
                args = cmd_tuple[1:]
                try:
                    if cmd == "GET_DIR":
                        self._handle_get_dir(args[0])
                    elif cmd == "DISK_INFO":
                        self._handle_disk_info()
                    elif cmd == "MKDIR":
                        self._handle_mkdir(args[0])
                    elif cmd == "DELETE":
                        self._handle_delete(args[0])
                    elif cmd == "RENAME":
                        self._handle_rename(args[0], args[1])
                    elif cmd == "COPY":
                        self._handle_copy(args[0], args[1])
                    elif cmd == "DOWNLOAD":
                        self._handle_download(args[0], args[1])
                    elif cmd == "UPLOAD":
                        self._handle_upload(args[0], args[1])
                except Exception as e:
                    self.error_signal.emit(f"Error during {cmd}: {e}")
            else:
                time.sleep(0.05)

        if self.conn:
            self.conn.close()
        self.disconnected_signal.emit()

    def queue_command(self, *args):
        self.command_queue.append(args)

    def stop(self):
        self.running = False

    def _send_text_cmd(self, cmd_line):
        self.conn.write((cmd_line + "\n").encode())
        self.conn.flush()

    def _read_line(self):
        try:
            return self.conn.readline().decode('utf-8', errors='ignore').strip()
        except Exception:
            return ""

    def _handle_get_dir(self, path):
        self._send_text_cmd(f"GET_DIR {path}")
        
        # Use a longer timeout for directory listing (SD cards can be slow)
        old_timeout = self.conn.timeout
        self.conn.timeout = 10
        
        resp = self._read_line()
        if resp != "ACK":
            self.conn.timeout = old_timeout
            self.error_signal.emit(f"GET_DIR Failed: {resp}")
            return
        
        items = []
        empty_count = 0
        while True:
            line = self._read_line()
            if line == "END":
                break
            if not line:
                empty_count += 1
                if empty_count > 5:  # 5 × 10s = 50s hard safety limit
                    self.error_signal.emit("GET_DIR timed out: too many files or SD card error.")
                    break
                continue  # Keep waiting, don't break!
            empty_count = 0  # Reset on successful read
            parts = line.split(":")
            if len(parts) >= 3:
                itype = parts[0]
                name = parts[1]
                size = int(parts[2])
                timestamp = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 0
                items.append({"name": name, "is_dir": itype == "DIR", "size": size, "timestamp": timestamp})
        
        self.conn.timeout = old_timeout
        self.dir_loaded_signal.emit(items)

    def _handle_disk_info(self):
        self._send_text_cmd("DISK_INFO")
        resp = self._read_line()
        if resp != "ACK":
            self.error_signal.emit(f"DISK_INFO Failed! Received: {resp}")
            return
            
        total = 0
        used = 0
        while True:
            line = self._read_line()
            if not line:
                self.error_signal.emit("DISK_INFO Failed: Serial Timeout!")
                break
            if line == "END":
                break
            if line.startswith("TOTAL:"):
                total = int(line.split(":")[1])
            elif line.startswith("USED:"):
                used = int(line.split(":")[1])
                
        self.disk_info_signal.emit(used, total)

    def _handle_mkdir(self, path):
        self._send_text_cmd(f"MKDIR {path}")
        resp = self._read_line()
        if resp == "ACK":
            self.operation_finished_signal.emit("Directory created.")
        else:
            self.error_signal.emit(resp)

    def _handle_delete(self, path):
        self._send_text_cmd(f"DELETE {path}")
        resp = self._read_line()
        if resp == "ACK":
            self.operation_finished_signal.emit("Deleted successfully.")
        else:
            self.error_signal.emit(resp)

    def _handle_rename(self, old_path, new_path):
        self._send_text_cmd(f"RENAME {old_path}|{new_path}")
        resp = self._read_line()
        if resp == "ACK":
            old_dir = old_path.rsplit('/', 1)[0]
            new_dir = new_path.rsplit('/', 1)[0]
            if old_dir == new_dir:
                self.operation_finished_signal.emit("Renamed successfully.")
            else:
                self.operation_finished_signal.emit("Moved successfully.")
        else:
            self.error_signal.emit(resp)

    def _handle_copy(self, old_path, new_path):
        self.status_signal.emit(f"Copying to {new_path}...")
        self._send_text_cmd(f"COPY {old_path}|{new_path}")
        
        old_timeout = self.conn.timeout
        self.conn.timeout = 5.0 # Max wait between progress updates
        
        while True:
            resp = self._read_line()
            if resp.startswith("PROG:"):
                parts = resp.split(":")
                if len(parts) == 3:
                    try:
                        self.progress_signal.emit(int(parts[1]), int(parts[2]))
                    except ValueError: pass
            elif resp == "ACK":
                self.operation_finished_signal.emit("Copied successfully.")
                break
            elif resp.startswith("ERROR"):
                self.error_signal.emit(resp)
                break
            elif not resp:
                self.error_signal.emit("Copy failed (timeout)")
                break
                
        self.conn.timeout = old_timeout

    def _handle_download(self, remote_path, local_path):
        self.status_signal.emit(f"Downloading {remote_path}...")
        self._send_text_cmd(f"DOWNLOAD {remote_path}")
        resp = self._read_line()
        if not resp.startswith("ACK"):
            self.error_signal.emit(f"Download failed: {resp}")
            return
        
        parts = resp.split(" ")
        file_size = int(parts[1]) if len(parts) > 1 else 0
        received_size = 0

        with open(local_path, "wb") as f:
            while True:
                header = self.conn.read(2)
                if len(header) < 2:
                    break
                chunk_len = (header[0] << 8) | header[1]
                if chunk_len == 0:
                    break # EOF
                
                data = self.conn.read(chunk_len)
                crc_bytes = self.conn.read(4)
                if len(crc_bytes) < 4:
                    break
                received_crc = (crc_bytes[0] << 24) | (crc_bytes[1] << 16) | (crc_bytes[2] << 8) | crc_bytes[3]
                
                calc_crc = zlib.crc32(data) & 0xFFFFFFFF
                if calc_crc == received_crc:
                    f.write(data)
                    received_size += chunk_len
                    self.conn.write(b"ACK\n")
                    self.conn.flush()
                    self.progress_signal.emit(received_size, file_size)
                else:
                    self.conn.write(b"NACK\n")
                    self.conn.flush()

        self.status_signal.emit("Download complete.")
        self.operation_finished_signal.emit("Download complete.")

    def _handle_upload(self, local_path, remote_path):
        file_size = os.path.getsize(local_path)
        self.status_signal.emit(f"Uploading {local_path} ({file_size} bytes)...")
        
        self._send_text_cmd(f"UPLOAD {remote_path}|{file_size}")
        resp = self._read_line()
        if not resp.startswith("ACK"):
            self.error_signal.emit(f"Upload start failed: {resp}")
            return
        
        parts = resp.split(" ")
        buffer_size = int(parts[1]) if len(parts) > 1 else 4096
        
        sent_size = 0
        with open(local_path, "rb") as f:
            while True:
                chunk = f.read(buffer_size)
                if not chunk:
                    break
                
                chunk_len = len(chunk)
                header = bytes([(chunk_len >> 8) & 0xFF, chunk_len & 0xFF])
                crc = zlib.crc32(chunk) & 0xFFFFFFFF
                crc_bytes = bytes([(crc >> 24) & 0xFF, (crc >> 16) & 0xFF, (crc >> 8) & 0xFF, crc & 0xFF])
                
                success = False
                for _ in range(3): # retry 3 times
                    self.conn.write(header)
                    self.conn.write(chunk)
                    self.conn.write(crc_bytes)
                    
                    resp = self._read_line()
                    if resp == "ACK":
                        success = True
                        break
                    elif resp.startswith("NACK_FATAL"):
                        self.error_signal.emit(f"Fatal error during upload: {resp}")
                        return
                
                if not success:
                    self.error_signal.emit("Upload failed after retries.")
                    return
                
                sent_size += chunk_len
                self.progress_signal.emit(sent_size, file_size)

        # Send EOF
        self.conn.write(bytes([0, 0]))
        
        # Wait for ESP32 to finish flushing cache to SD card
        # This can take several seconds for large files with PSRAM cache
        self.status_signal.emit("Saving to SD card...")
        old_timeout = self.conn.timeout
        self.conn.timeout = 30
        done_resp = self._read_line()
        self.conn.timeout = old_timeout
        
        if done_resp == "UPLOAD_DONE":
            self.status_signal.emit("Upload complete.")
            self.operation_finished_signal.emit("Upload complete.")
        else:
            self.status_signal.emit("Upload finished (no confirmation).")
            self.operation_finished_signal.emit("Upload finished.")
        self.queue_command("DISK_INFO")

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ESP USB Storage")
        self.resize(860, 600)
        self.setAcceptDrops(True)
        self.current_path = "/"
        self.worker = None
        self.pending_op_path = None
        self.pending_op_name = None
        self.pending_op_type = None  # "MOVE" or "COPY"

        self._setup_ui()

    def _setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # Top Bar
        top_bar = QHBoxLayout()
        self.btn_up = QPushButton("⬆ Up")
        self.btn_up.clicked.connect(self.go_up)
        self.lbl_path = QLabel("/")
        
        # COM Port Selection
        self.combo_ports = QComboBox()
        self.btn_refresh_ports = QPushButton("🔄 Ports")
        self.btn_refresh_ports.clicked.connect(self.refresh_ports)
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.clicked.connect(self.toggle_connection)
        
        self.btn_refresh = QPushButton("🔄 Refresh Dir")
        self.btn_refresh.clicked.connect(self.refresh_dir)
        
        top_bar.addWidget(self.btn_up)
        top_bar.addWidget(self.lbl_path)
        top_bar.addStretch()
        top_bar.addWidget(self.combo_ports)
        top_bar.addWidget(self.btn_refresh_ports)
        top_bar.addWidget(self.btn_connect)
        top_bar.addWidget(self.btn_refresh)
        main_layout.addLayout(top_bar)
        
        # View & Sort Bar
        view_bar = QHBoxLayout()
        self.combo_view = QComboBox()
        self.combo_view.addItems(["List View", "Icon View"])
        self.combo_view.currentIndexChanged.connect(self.change_view)
        
        self.combo_sort = QComboBox()
        self.combo_sort.addItems(["Sort: Name", "Sort: Size (Asc)", "Sort: Size (Desc)"])
        self.combo_sort.currentIndexChanged.connect(self.change_sort)
        
        view_bar.addStretch()
        view_bar.addWidget(self.combo_view)
        view_bar.addWidget(self.combo_sort)
        main_layout.addLayout(view_bar)

        self.refresh_ports()

        # Stacked Widget for Views
        self.stacked_widget = QStackedWidget()
        
        # Details View (Tree)
        self.tree_list = QTreeWidget()
        self.tree_list.setHeaderLabels(["Name", "Date Modified", "Type", "Size"])
        self.tree_list.setIndentation(0)
        self.tree_list.setRootIsDecorated(False)
        self.tree_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree_list.itemDoubleClicked.connect(self.on_item_double_clicked)
        self.tree_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree_list.customContextMenuRequested.connect(self.show_context_menu)
        self.tree_list.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.tree_list.header().setStretchLastSection(False)
        self.tree_list.setColumnWidth(0, 300)
        self.tree_list.setColumnWidth(1, 150)
        self.tree_list.setColumnWidth(2, 120)
        self.tree_list.setColumnWidth(3, 100)
        
        # Icon View (List)
        self.file_list = QListWidget()
        self.file_list.setViewMode(QListView.ViewMode.IconMode)
        self.file_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.file_list.setGridSize(QSize(130, 100))
        self.file_list.setIconSize(QSize(48, 48))
        self.file_list.setWordWrap(True)
        self.file_list.setSpacing(10)
        self.file_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.file_list.itemDoubleClicked.connect(self.on_item_double_clicked)
        self.file_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.file_list.customContextMenuRequested.connect(self.show_context_menu)
        
        self.stacked_widget.addWidget(self.tree_list)
        self.stacked_widget.addWidget(self.file_list)
        
        main_layout.addWidget(self.stacked_widget)
        self.current_items = []
        
        # Shortcuts
        self.shortcut_copy = QShortcut(QKeySequence("Ctrl+C"), self)
        self.shortcut_copy.activated.connect(self.shortcut_action_copy)

        self.shortcut_cut = QShortcut(QKeySequence("Ctrl+X"), self)
        self.shortcut_cut.activated.connect(self.shortcut_action_cut)

        self.shortcut_paste = QShortcut(QKeySequence("Ctrl+V"), self)
        self.shortcut_paste.activated.connect(self.action_paste)

        self.shortcut_rename = QShortcut(QKeySequence("F2"), self)
        self.shortcut_rename.activated.connect(self.shortcut_action_rename)
        
        self.shortcut_delete = QShortcut(QKeySequence("Delete"), self)
        self.shortcut_delete.activated.connect(self.shortcut_action_delete)

        # Bottom Bar
        bottom_bar = QHBoxLayout()
        self.lbl_status = QLabel("Disconnected")
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.lbl_capacity = QLabel("Capacity: Unknown")
        
        bottom_bar.addWidget(self.lbl_status)
        bottom_bar.addWidget(self.progress_bar)
        bottom_bar.addWidget(self.lbl_capacity)
        main_layout.addLayout(bottom_bar)

        self.change_view(0)

    def refresh_ports(self):
        self.combo_ports.clear()
        ports = serial.tools.list_ports.comports()
        for p in ports:
            self.combo_ports.addItem(f"{p.device} - {p.description}", p.device)

    def toggle_connection(self):
        if self.worker and self.worker.running:
            # Disconnect
            self.worker.stop()
            self.worker.wait()
            self.worker = None
        else:
            # Connect
            port = self.combo_ports.currentData()
            if not port:
                QMessageBox.warning(self, "Error", "No port selected!")
                return
            
            self.combo_ports.setEnabled(False)
            self.btn_refresh_ports.setEnabled(False)
            self.btn_connect.setText("Disconnect")
            
            self.worker = SerialWorker(port)
            self.worker.status_signal.connect(self.update_status)
            self.worker.progress_signal.connect(self.update_progress)
            self.worker.error_signal.connect(self.show_error)
            self.worker.dir_loaded_signal.connect(self.populate_list)
            self.worker.disk_info_signal.connect(self.update_capacity)
            self.worker.operation_finished_signal.connect(self.on_operation_finished)
            self.worker.disconnected_signal.connect(self.on_disconnected)
            self.worker.start()

    def on_disconnected(self):
        self.combo_ports.setEnabled(True)
        self.btn_refresh_ports.setEnabled(True)
        self.btn_connect.setText("Connect")
        self.update_status("Disconnected")
        self.file_list.clear()
        self.lbl_capacity.setText("Capacity: Unknown")
        self.worker = None

    def update_status(self, msg):
        self.lbl_status.setText(msg)

    def update_progress(self, current, total):
        if total > 0:
            pct = int((current / total) * 100)
            self.progress_bar.setValue(pct)
        else:
            self.progress_bar.setValue(0)

    def show_error(self, msg):
        QMessageBox.warning(self, "Error", msg)
        self.update_status("Idle")
        self.progress_bar.setValue(0)

    def update_capacity(self, used_mb, total_mb):
        if total_mb > 0:
            self.lbl_capacity.setText(f"Capacity: {used_mb} MB / {total_mb} MB")
        else:
            self.lbl_capacity.setText("Capacity: Unknown")

    def on_operation_finished(self, msg):
        self.update_status(msg)
        self.progress_bar.setValue(0)
        self.refresh_dir()
    def get_current_list(self):
        return self.stacked_widget.currentWidget()
        
    def get_item_data(self, item):
        from PyQt6.QtWidgets import QTreeWidgetItem
        if isinstance(item, QTreeWidgetItem):
            return item.data(0, Qt.ItemDataRole.UserRole)
        return item.data(Qt.ItemDataRole.UserRole)

    def populate_list(self, items=None):
        if items is not None:
            self.current_items = items
            
        self.file_list.clear()
        self.tree_list.clear()
        
        sort_mode = self.combo_sort.currentIndex()
        if sort_mode == 0: # Name
            self.current_items.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
        elif sort_mode == 1: # Size Asc
            self.current_items.sort(key=lambda x: (not x['is_dir'], x['size']))
        elif sort_mode == 2: # Size Desc
            self.current_items.sort(key=lambda x: (not x['is_dir'], -x['size']))
            
        is_icon_mode = self.combo_view.currentIndex() == 1
        
        for item in self.current_items:
            ts = item.get('timestamp', 0)
            date_str = datetime.fromtimestamp(ts).strftime('%d/%m/%Y %H:%M') if ts > 0 else ""
            
            if item['is_dir']:
                icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
                type_str = "File Folder"
                size_str = ""
            else:
                icon = self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
                ext = os.path.splitext(item['name'])[1].upper()
                type_str = f"{ext} File" if ext else "File"
                size_str = format_size(item['size'])
                
            if is_icon_mode:
                text = f"{item['name']}\n({size_str})" if size_str else item['name']
                list_item = QListWidgetItem(icon, text)
                list_item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
                list_item.setData(Qt.ItemDataRole.UserRole, item)
                self.file_list.addItem(list_item)
            else:
                tree_item = QTreeWidgetItem([item['name'], date_str, type_str, size_str])
                tree_item.setIcon(0, icon)
                tree_item.setData(0, Qt.ItemDataRole.UserRole, item)
                self.tree_list.addTopLevelItem(tree_item)
            
    def change_view(self, index):
        self.stacked_widget.setCurrentIndex(index)
        self.populate_list()
            
    def change_sort(self, index):
        self.populate_list()

    def go_up(self):
        if self.current_path != "/":
            parts = self.current_path.rstrip("/").split("/")
            parts.pop()
            self.current_path = "/" + "/".join(parts).lstrip("/")
            if not self.current_path.startswith("/"):
                self.current_path = "/" + self.current_path
            if self.current_path == "//":
                self.current_path = "/"
            self.lbl_path.setText(self.current_path)
            self.refresh_dir()

    def refresh_dir(self):
        if self.worker and self.worker.running:
            self.worker.queue_command("GET_DIR", self.current_path)

    def on_item_double_clicked(self, item):
        data = self.get_item_data(item)
        if data['is_dir']:
            name = data['name']
            if self.current_path == "/":
                self.current_path = "/" + name
            else:
                self.current_path = self.current_path + "/" + name
            self.lbl_path.setText(self.current_path)
            self.refresh_dir()
        else:
            if not self.worker or not self.worker.running: return
            # Download file
            name = data['name']
            remote_path = self.current_path + ("" if self.current_path == "/" else "/") + name
            local_path, _ = QFileDialog.getSaveFileName(self, "Download File", name)
            if local_path:
                self.worker.queue_command("DOWNLOAD", remote_path, local_path)

    def show_context_menu(self, pos):
        if not self.worker or not self.worker.running: return
        item = self.get_current_list().itemAt(pos)
        menu = QMenu()
        
        if not item:
            action_new_folder = QAction("New Folder", self)
            action_new_folder.triggered.connect(self.action_mkdir)
            menu.addAction(action_new_folder)
            
            action_upload = QAction("Upload from PC", self)
            action_upload.triggered.connect(self.action_upload_dialog)
            menu.addAction(action_upload)
        else:
            data = self.get_item_data(item)
            action_rename = QAction("Rename", self)
            action_rename.triggered.connect(lambda: self.action_rename(data['name']))
            menu.addAction(action_rename)
            
            action_cut = QAction("Cut", self)
            action_cut.triggered.connect(lambda: self.action_move_or_copy(data['name'], "MOVE"))
            menu.addAction(action_cut)
            
            action_copy = QAction("Copy", self)
            action_copy.triggered.connect(lambda: self.action_move_or_copy(data['name'], "COPY"))
            menu.addAction(action_copy)
            
            action_delete = QAction("Delete", self)
            action_delete.triggered.connect(lambda: self.action_delete(data['name']))
            menu.addAction(action_delete)
            
            if not data['is_dir']:
                action_download = QAction("Download", self)
                action_download.triggered.connect(lambda: self.on_item_double_clicked(item))
                menu.addAction(action_download)
                
        if self.pending_op_path:
            menu.addSeparator()
            icon = "📋" if self.pending_op_type == "COPY" else "✂️"
            action_name = "Copy" if self.pending_op_type == "COPY" else "Cut"
            action_paste = QAction(f"{icon} Paste ({action_name}) '{self.pending_op_name}'", self)
            action_paste.triggered.connect(self.action_paste)
            menu.addAction(action_paste)
            
            action_cancel = QAction("❌ Cancel Cut/Copy", self)
            action_cancel.triggered.connect(self.cancel_paste)
            menu.addAction(action_cancel)

        menu.exec(self.file_list.mapToGlobal(pos))

    def action_mkdir(self):
        name, ok = QInputDialog.getText(self, "New Folder", "Folder Name:")
        if ok and name:
            path = self.current_path + ("" if self.current_path == "/" else "/") + name
            self.worker.queue_command("MKDIR", path)

    def action_delete(self, name):
        reply = QMessageBox.question(self, "Delete", f"Are you sure you want to delete {name}?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            path = self.current_path + ("" if self.current_path == "/" else "/") + name
            self.worker.queue_command("DELETE", path)

    def action_rename(self, old_name):
        dialog = RenameDialog(old_name, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_name = dialog.get_name()
            if new_name and new_name != old_name:
                old_path = self.current_path + ("" if self.current_path == "/" else "/") + old_name
                new_path = self.current_path + ("" if self.current_path == "/" else "/") + new_name
                self.worker.queue_command("RENAME", old_path, new_path)

    def action_move_or_copy(self, name, op_type):
        self.pending_op_path = self.current_path + ("" if self.current_path == "/" else "/") + name
        self.pending_op_name = name
        self.pending_op_type = op_type

    def action_paste(self):
        if not self.pending_op_path or not self.pending_op_name: return
        new_path = self.current_path + ("" if self.current_path == "/" else "/") + self.pending_op_name
        
        if new_path == self.pending_op_path:
            self.cancel_paste()
            return
            
        collision = any(item['name'] == self.pending_op_name for item in self.current_items)
        overwrite_selected = False
        if collision:
            msgBox = QMessageBox(self)
            msgBox.setWindowTitle("File Exists")
            msgBox.setText(f"'{self.pending_op_name}' already exists in the destination.\nWhat would you like to do?")
            btn_overwrite = msgBox.addButton("Overwrite", QMessageBox.ButtonRole.AcceptRole)
            btn_skip = msgBox.addButton("Skip", QMessageBox.ButtonRole.RejectRole)
            btn_rename = msgBox.addButton("Rename Pasted File", QMessageBox.ButtonRole.ActionRole)
            msgBox.exec()
            
            clicked = msgBox.clickedButton()
            if clicked == btn_skip:
                self.cancel_paste()
                return
            elif clicked == btn_rename:
                dialog = RenameDialog(self.pending_op_name, self)
                if dialog.exec() == QDialog.DialogCode.Accepted:
                    new_name = dialog.get_name()
                    if new_name:
                        new_path = self.current_path + ("" if self.current_path == "/" else "/") + new_name
                    else:
                        self.cancel_paste()
                        return
                else:
                    self.cancel_paste()
                    return
            elif clicked == btn_overwrite:
                overwrite_selected = True
                
        if overwrite_selected:
            self.worker.queue_command("DELETE", new_path)
        
        if self.pending_op_type == "MOVE":
            self.worker.queue_command("RENAME", self.pending_op_path, new_path)
        elif self.pending_op_type == "COPY":
            self.worker.queue_command("COPY", self.pending_op_path, new_path)
            
        self.cancel_paste()

    def shortcut_action_copy(self):
        item = self.get_current_list().currentItem()
        if item:
            data = self.get_item_data(item)
            self.action_move_or_copy(data['name'], "COPY")

    def shortcut_action_cut(self):
        item = self.get_current_list().currentItem()
        if item:
            data = self.get_item_data(item)
            self.action_move_or_copy(data['name'], "MOVE")

    def shortcut_action_rename(self):
        item = self.get_current_list().currentItem()
        if item:
            data = self.get_item_data(item)
            self.action_rename(data['name'])

    def shortcut_action_delete(self):
        item = self.get_current_list().currentItem()
        if item:
            data = self.get_item_data(item)
            self.action_delete(data['name'])

    def cancel_paste(self):
        self.pending_op_path = None
        self.pending_op_name = None
        self.pending_op_type = None

    def action_upload_dialog(self):
        local_path, _ = QFileDialog.getOpenFileName(self, "Select File")
        if local_path:
            self.upload_file(local_path)

    def upload_file(self, local_path, override_name=None):
        name = override_name if override_name else os.path.basename(local_path)
        remote_path = self.current_path + ("" if self.current_path == "/" else "/") + name
        
        collision = any(item['name'] == name for item in self.current_items)
        overwrite_selected = False
        if collision:
            msgBox = QMessageBox(self)
            msgBox.setWindowTitle("File Exists")
            msgBox.setText(f"'{name}' already exists in the destination.\nWhat would you like to do?")
            btn_overwrite = msgBox.addButton("Overwrite", QMessageBox.ButtonRole.AcceptRole)
            btn_skip = msgBox.addButton("Skip", QMessageBox.ButtonRole.RejectRole)
            btn_rename = msgBox.addButton("Rename Uploaded File", QMessageBox.ButtonRole.ActionRole)
            msgBox.exec()
            
            clicked = msgBox.clickedButton()
            if clicked == btn_skip:
                return
            elif clicked == btn_rename:
                dialog = RenameDialog(name, self)
                if dialog.exec() == QDialog.DialogCode.Accepted:
                    new_name = dialog.get_name()
                    if new_name:
                        self.upload_file(local_path, override_name=new_name)
                return
            elif clicked == btn_overwrite:
                overwrite_selected = True
                
        if overwrite_selected:
            self.worker.queue_command("DELETE", remote_path)
        
        self.worker.queue_command("UPLOAD", local_path, remote_path)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        if not self.worker or not self.worker.running: return
        for url in event.mimeData().urls():
            local_path = url.toLocalFile()
            if os.path.isfile(local_path):
                self.upload_file(local_path)

    def closeEvent(self, event):
        if self.worker:
            self.worker.stop()
            self.worker.wait()
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet("""
        QWidget { background-color: #2b2b2b; color: white; }
        QListWidget, QTreeWidget { background-color: #3c3f41; color: white; font-size: 14px; border: none; }
        QHeaderView::section { background-color: #4a4a4a; color: white; padding: 5px; border: 1px solid #2b2b2b; }
        QPushButton { background-color: #4a4a4a; color: white; padding: 5px 10px; border-radius: 3px; border: none; }
        QPushButton:hover { background-color: #5a5a5a; }
        QComboBox { background-color: #4a4a4a; color: white; padding: 3px; border-radius: 3px; border: none; }
        QLabel { color: #d3d3d3; background: transparent; }
        QProgressBar { text-align: center; color: white; background-color: #4a4a4a; border-radius: 3px; }
        QProgressBar::chunk { background-color: #0078D7; }
        QLineEdit { background-color: #3c3f41; color: white; padding: 5px; border: 1px solid #4a4a4a; border-radius: 3px; }
        QDialog, QMessageBox { background-color: #2b2b2b; }
    """)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
