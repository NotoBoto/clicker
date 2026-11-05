# -*- coding: utf-8 -*-
"""
Кликер с графическим интерфейсом. Без внешних зависимостей (tkinter + ctypes).

Горячие клавиши (работают из любого окна):
    F6  - старт
    F7  - стоп (прерывает макрос сразу, даже во время паузы)
    F9  - записать позицию курсора (и цвет) в выбранный шаг
"""
import copy
import ctypes
import json
import os
import queue
import socket
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk

# Чтобы координаты совпадали с реальными пикселями экрана при масштабе 125%/150%
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
gdi32.GetPixel.restype = wintypes.DWORD
gdi32.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]

VK_F6, VK_F7, VK_F9 = 0x75, 0x76, 0x78
MOUSE_FLAGS = {'left': (0x0002, 0x0004), 'right': (0x0008, 0x0010)}

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FILE = os.path.join(HERE, 'macro.json')
SETTINGS_FILE = os.path.join(HERE, 'clicker_settings.json')   # последний макрос, недавние окна
BAD_NAME_CHARS = '\\/:*?"<>|'


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def list_macros():
    """Файлы макросов (*.json) рядом с программой, кроме файла настроек."""
    try:
        names = [f for f in os.listdir(HERE) if f.lower().endswith('.json')
                 and os.path.join(HERE, f) != SETTINGS_FILE]
    except OSError:
        names = []
    return sorted(names, key=str.lower)


# ---------------- WinAPI ----------------
def cursor_pos():
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def pixel(x, y):
    """Цвет пикселя в формате UoPilot/COLORREF (0x00BBGGRR). > 0xFFFFFF - ошибка."""
    hdc = user32.GetDC(None)
    try:
        return gdi32.GetPixel(hdc, x, y)
    finally:
        user32.ReleaseDC(None, hdc)


def mouse(button, down):
    user32.mouse_event(MOUSE_FLAGS[button][0 if down else 1], 0, 0, 0, 0)


def color_match(c1, c2, tol):
    if c1 > 0xFFFFFF:
        return False
    return all(abs(((c1 >> s) & 0xFF) - ((c2 >> s) & 0xFF)) <= tol for s in (0, 8, 16))


def to_hex(c):
    """COLORREF -> '#RRGGBB' для отображения."""
    return '#%02X%02X%02X' % (c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF)


# ---------------- Работа с окном в фоне ----------------
user32.FindWindowW.restype = wintypes.HWND
user32.GetWindowDC.restype = wintypes.HDC
user32.GetWindowDC.argtypes = [wintypes.HWND]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
PW_CLIENT_FULL = 0x1 | 0x2      # PW_CLIENTONLY | PW_RENDERFULLCONTENT (работает и для DirectX)
WM_MOUSEMOVE = 0x0200
MSG_BUTTONS = {'left': (0x0201, 0x0202, 0x0001), 'right': (0x0204, 0x0205, 0x0002)}  # down, up, MK_*


def list_windows():
    """Заголовки видимых окон других программ."""
    own = os.getpid()
    titles = []

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            t = buf.value
            if (pid.value != own and t not in titles and not t.startswith('Кликер')
                    and t not in ('Program Manager', 'Microsoft Text Input Application')):
                titles.append(t)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return titles


def find_window(title):
    return user32.FindWindowW(None, title) if title else None


def client_origin(hwnd):
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y


def client_size(hwnd):
    r = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    return r.right, r.bottom


def window_pixel(hwnd, x, y):
    """Цвет точки клиентской области окна, даже если окно перекрыто другими."""
    w, h = client_size(hwnd)
    if not (0 <= x < w and 0 <= y < h):
        return 0xFFFFFFFF
    wdc = user32.GetDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(wdc)
    bmp = gdi32.CreateCompatibleBitmap(wdc, w, h)
    old = gdi32.SelectObject(mdc, bmp)
    try:
        if not user32.PrintWindow(hwnd, mdc, PW_CLIENT_FULL):
            return 0xFFFFFFFF
        return gdi32.GetPixel(mdc, x, y)
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, wdc)


def window_mouse(hwnd, x, y, button, down):
    """Нажатие/отпускание кнопки прямо в окно, без движения настоящего курсора."""
    lp = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
    msg_down, msg_up, mk = MSG_BUTTONS[button]
    user32.PostMessageW(hwnd, WM_MOUSEMOVE, mk if not down else 0, lp)
    user32.PostMessageW(hwnd, msg_down if down else msg_up, mk if down else 0, lp)


kernel32 = ctypes.windll.kernel32
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
WM_ACTIVATE, WM_SETFOCUS, WM_ACTIVATEAPP, WM_NCACTIVATE = 0x0006, 0x0007, 0x001C, 0x0086
VK_MENU, KEYEVENTF_KEYUP = 0x12, 0x0002


def fake_activate(hwnd):
    """Сообщаем окну, что оно активно (многие игры иначе игнорируют ввод в фоне)."""
    user32.PostMessageW(hwnd, WM_ACTIVATEAPP, 1, 0)
    user32.PostMessageW(hwnd, WM_NCACTIVATE, 1, 0)
    user32.PostMessageW(hwnd, WM_ACTIVATE, 1, 0)
    user32.PostMessageW(hwnd, WM_SETFOCUS, 0, 0)


def force_foreground(hwnd):
    """Делает окно активным, обходя запрет Windows на смену фокуса из фоновой программы."""
    fg = user32.GetForegroundWindow()
    if fg == hwnd:
        return True
    cur = kernel32.GetCurrentThreadId()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = bool(fg_thread and fg_thread != cur and user32.AttachThreadInput(cur, fg_thread, True))
    try:
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(cur, fg_thread, False)
    if user32.GetForegroundWindow() != hwnd:
        user32.keybd_event(VK_MENU, 0, 0, 0)        # запасной трюк с Alt
        user32.SetForegroundWindow(hwnd)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    return user32.GetForegroundWindow() == hwnd


CLICK_METHODS = {'background': 'Фоновый (мышь не трогается)',
                 'focus': 'Перехват на миг (надёжный)',
                 'mod': 'Через мод игры (мышь свободна)'}
CLICK_METHOD_KEYS = {v: k for k, v in CLICK_METHODS.items()}
BRIDGE_PORT = 47800
BUTTON_INDEX = {'left': 0, 'right': 1}


class Bridge:
    """Связь с модом ClickerBridge внутри игры (127.0.0.1:47800)."""

    def __init__(self, port=BRIDGE_PORT):
        self.port = port
        self.sock = None
        self.reader = None

    def connect(self):
        self.close()
        self.sock = socket.create_connection(('127.0.0.1', self.port), timeout=2)
        self.reader = self.sock.makefile('r', encoding='utf-8')

    def send(self, line):
        """Отправляет команду и возвращает ответ мода. Один раз переподключается при обрыве."""
        for attempt in (1, 2):
            try:
                if not self.sock:
                    self.connect()
                self.sock.sendall((line + '\n').encode('utf-8'))
                answer = self.reader.readline().strip()
                if not answer:
                    raise OSError('соединение закрыто')
                return answer
            except OSError:
                self.close()
                if attempt == 2:
                    raise

    def close(self):
        for obj in (self.reader, self.sock):
            try:
                if obj:
                    obj.close()
            except OSError:
                pass
        self.sock = self.reader = None


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.UINT), ('dwTime', wintypes.DWORD)]


MOUSEEVENTF_MOVE = 0x0001


def jiggle(x, y):
    """Шевелим мышь на 1 px, чтобы игра заметила курсор над нужной точкой."""
    user32.mouse_event(MOUSEEVENTF_MOVE, 1, 0, 0, 0)
    time.sleep(0.02)
    user32.mouse_event(MOUSEEVENTF_MOVE, -1, 0, 0, 0)
    user32.SetCursorPos(x, y)
    time.sleep(0.04)


def idle_ms():
    """Сколько мс пользователь не трогал мышь и клавиатуру."""
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    user32.GetLastInputInfo(ctypes.byref(info))
    return (kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF


def user_buttons_held():
    return any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in (0x01, 0x02, 0x04))  # ЛКМ, ПКМ, СКМ


# ---------------- Модель шагов ----------------
TYPES = {
    'click': 'Клик',
    'down': 'Зажать кнопку',
    'up': 'Отпустить кнопку',
    'wait': 'Пауза',
    'wait_color': 'Ждать цвет',
}
TYPE_KEYS = {v: k for k, v in TYPES.items()}
FIELDS = {
    'click': ['x', 'y', 'button'],
    'down': ['x', 'y', 'button'],
    'up': ['x', 'y', 'button'],
    'wait': ['ms'],
    'wait_color': ['x', 'y', 'color', 'tol', 'timeout'],
}
LABELS = {'x': 'X', 'y': 'Y', 'button': 'Кнопка', 'ms': 'Пауза, мс', 'color': 'Цвет',
          'tol': 'Допуск ±', 'timeout': 'Таймаут, с'}
HINTS = {'ms': '1000 мс = 1 с', 'tol': 'отличие по R/G/B', 'timeout': '0 = ждать всегда'}
DEFAULTS = {'x': 960, 'y': 540, 'button': 'left', 'ms': 1000, 'color': 0, 'tol': 12, 'timeout': 0}
BUTTONS = {'left': 'ЛКМ', 'right': 'ПКМ'}
BUTTON_KEYS = {v: k for k, v in BUTTONS.items()}
NON_NEGATIVE = ('ms', 'tol', 'timeout', 'color')

DEFAULT_STEPS = [
    {'type': 'down', 'x': 800, 'y': 650, 'note': 'Нажатие в центре'},
    {'type': 'wait', 'ms': 100},
    {'type': 'up', 'x': 800, 'y': 650},
    {'type': 'wait', 'ms': 500},
    {'type': 'click', 'x': 1780, 'y': 960, 'note': 'Правый нижний угол'},
    {'type': 'wait_color', 'x': 1175, 'y': 197, 'color': 65793, 'tol': 12, 'note': 'Ждём экран колеса'},
    {'type': 'wait', 'ms': 5000},
    {'type': 'click', 'x': 964, 'y': 670},
    {'type': 'wait', 'ms': 500},
    {'type': 'click', 'x': 1000, 'y': 600},
    {'type': 'wait', 'ms': 4000},
    {'type': 'click', 'x': 1000, 'y': 1000},
    {'type': 'wait', 'ms': 6000},
]


def new_step(t, base=None):
    """Создаёт корректный шаг типа t, по возможности беря значения из base."""
    base = base or {}
    st = {'type': t}
    for f in FIELDS[t]:
        val = base.get(f, DEFAULTS[f])
        if f == 'button':
            st[f] = val if val in BUTTONS else 'left'
        else:
            st[f] = int(val)
    st['note'] = str(base.get('note', ''))
    return st


def describe(st):
    t = st['type']
    pos = f"({st.get('x')}, {st.get('y')})"
    btn = BUTTONS.get(st.get('button'), '')
    if t == 'click':
        return f'Клик {btn}', pos
    if t == 'down':
        return f'Зажать {btn}', pos
    if t == 'up':
        return f'Отпустить {btn}', pos
    if t == 'wait':
        ms = st['ms']
        return 'Пауза', f'{ms} мс' + (f'  ({ms / 1000:g} с)' if ms >= 1000 else '')
    s = f"{pos} = {st['color']}  ±{st['tol']}"
    if st['timeout']:
        s += f",  макс. {st['timeout']} с"
    return 'Ждать цвет', s


def parse_int(s):
    """Целое число: десятичное или шестнадцатеричное с 0x."""
    s = s.strip().lower()
    return int(s, 16) if s.startswith('0x') else int(s)


# ---------------- Выполнение макроса ----------------
class Stopped(Exception):
    pass


class Runner(threading.Thread):
    def __init__(self, steps, count, q, window='', method='background', grab_delay=250):
        super().__init__(daemon=True)
        self.grab_delay = grab_delay    # мс между активацией окна и нажатием
        self.steps = copy.deepcopy(steps)
        self.count = count          # 0 = бесконечно
        self.q = q
        self.window = window        # '' = весь экран, иначе заголовок окна
        self.method = method        # 'background' или 'focus'
        self.grabbed = None         # (прежнее активное окно, позиция курсора) во время перехвата
        self.bridge = Bridge() if method == 'mod' else None
        self.hwnd = None
        self.last_pos = (0, 0)
        self.stop_event = threading.Event()
        self.held = set()

    def target(self):
        """Окно, в которое кликаем. Ждёт, пока окно появится и будет развёрнуто."""
        warned = False
        while True:
            if not (self.hwnd and user32.IsWindow(self.hwnd)):
                self.hwnd = find_window(self.window)
            if self.hwnd and not user32.IsIconic(self.hwnd):
                if warned:
                    self.post('log', 'Окно снова доступно, продолжаю')
                return self.hwnd
            if not warned:
                self.post('log', f'Окно «{self.window}» ' + ('свёрнуто: в свёрнутом окне игра не рисуется. '
                         'Разверните его (можно перекрыть другими окнами)' if self.hwnd else 'не найдено, жду…'))
                warned = True
            self.sleep(500)

    def press(self, x, y, button, down):
        if not self.window:
            user32.SetCursorPos(x, y)
            mouse(button, down)
            return
        hwnd = self.target()
        if self.method == 'mod':
            self.bridge_send(f"{'down' if down else 'up'} {BUTTON_INDEX[button]} {x} {y}")
            return
        if self.method == 'background':
            fake_activate(hwnd)
            window_mouse(hwnd, x, y, button, down)
            return
        # Перехват: ненадолго активируем окно, кликаем настоящей мышью, возвращаем всё как было
        ox, oy = client_origin(hwnd)
        sx, sy = ox + x, oy + y
        if not self.grabbed:
            self.wait_user_pause()
            self.grabbed = (user32.GetForegroundWindow(), cursor_pos())
            user32.SetCursorPos(sx, sy)          # курсор на месте ещё до активации
            if not force_foreground(hwnd):
                self.post('log', 'Не удалось активировать окно, кликаю всё равно')
            time.sleep(self.grab_delay / 1000)  # игре нужно несколько кадров, чтобы «проснуться»
            jiggle(sx, sy)
        else:
            user32.SetCursorPos(sx, sy)
            time.sleep(0.03)
        mouse(button, down)
        if not down:
            time.sleep(0.12)                     # даём игре обработать отпускание кнопки
            self.release_grab()

    def wait_user_pause(self, need_ms=250, max_ms=3000):
        """Ждём короткую паузу в вашем вводе, чтобы не перехватить мышь посреди печати или перетаскивания."""
        start = time.time()
        while (idle_ms() < need_ms or user_buttons_held()) and (time.time() - start) * 1000 < max_ms:
            self.sleep(30)

    def bridge_send(self, line):
        """Команда моду; если мод не отвечает - ждём его (игра могла ещё не запуститься)."""
        warned = False
        while True:
            try:
                answer = self.bridge.send(line)
                if warned:
                    self.post('log', 'Мод снова на связи')
                if answer != 'ok':
                    self.post('log', f'Мод ответил: {answer}')
                return
            except OSError:
                if not warned:
                    self.post('log', 'Мод ClickerBridge не отвечает. Игра запущена с BepInEx и модом? Жду…')
                    warned = True
                self.sleep(2000)

    def release_grab(self):
        if not self.grabbed:
            return
        prev, (cx, cy) = self.grabbed
        self.grabbed = None
        user32.SetCursorPos(cx, cy)
        if prev and prev != self.hwnd and user32.IsWindow(prev):
            force_foreground(prev)

    def read_pixel(self, x, y):
        return window_pixel(self.target(), x, y) if self.window else pixel(x, y)

    def post(self, *msg):
        self.q.put(msg)

    def sleep(self, ms):
        if self.stop_event.wait(ms / 1000.0):
            raise Stopped

    def run(self):
        self.post('log', 'СТАРТ')
        cycle = 0
        try:
            while not self.count or cycle < self.count:
                cycle += 1
                self.post('cycle', cycle, self.count)
                for i, st in enumerate(self.steps):
                    if self.stop_event.is_set():
                        raise Stopped
                    self.post('step', i)
                    self.do(st)
            self.post('log', 'Готово')
        except Stopped:
            self.post('log', 'СТОП')
        finally:
            for b in list(self.held):   # не оставляем кнопку зажатой
                if self.window and self.method == 'mod':
                    try:
                        self.bridge.send('release')
                    except OSError:
                        pass
                elif self.window and self.method == 'background':
                    if self.hwnd and user32.IsWindow(self.hwnd):
                        window_mouse(self.hwnd, *self.last_pos, b, False)
                else:
                    mouse(b, False)
            self.release_grab()
            if self.bridge:
                self.bridge.close()
            self.post('done')

    def do(self, st):
        t = st['type']
        if t in ('click', 'down', 'up'):
            x, y, b = st['x'], st['y'], st['button']
            self.last_pos = (x, y)
            if t in ('click', 'down'):
                self.press(x, y, b, True)
                self.held.add(b)
            if t == 'click':
                time.sleep(0.08 if self.window and self.method in ('focus', 'mod') else 0.03)
            if t in ('click', 'up'):
                self.press(x, y, b, False)
                self.held.discard(b)
        elif t == 'wait':
            self.sleep(st['ms'])
        elif t == 'wait_color':
            start = time.time()
            self.post('log', f"Жду цвет {st['color']} в ({st['x']}, {st['y']})...")
            while True:
                if color_match(self.read_pixel(st['x'], st['y']), st['color'], st['tol']):
                    self.post('log', 'Цвет найден')
                    return
                if st['timeout'] and time.time() - start >= st['timeout']:
                    self.post('log', 'Цвет не появился (таймаут), иду дальше')
                    return
                self.sleep(50)


# ---------------- Интерфейс ----------------
class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.runner = None
        self.settings = load_settings()
        last = self.settings.get('last_macro')
        self.path = last if isinstance(last, str) and os.path.isfile(last) else DEFAULT_FILE
        self.macro_paths = {}                          # имя в списке -> путь к файлу
        self.macro_name = tk.StringVar(value='')
        self.steps = []
        self.keys_held = set()
        self.run_row = None
        self.ticks = 0
        self.swatch = None
        self.pick_btn = None

        self.repeat_inf = tk.BooleanVar(value=True)
        self.repeat_count = tk.StringVar(value='1')
        self.topmost = tk.BooleanVar(value=False)
        self.mode = tk.StringVar(value='screen')       # 'screen' или 'window'
        self.window_title = tk.StringVar(value='')
        self.click_method = tk.StringVar(value=CLICK_METHODS['focus'])
        self.grab_delay = tk.StringVar(value='250')
        self.coords_window = False                     # координаты шагов заданы относительно окна

        root.geometry('1040x720')
        root.minsize(860, 540)
        style = ttk.Style()
        base = tkfont.nametofont('TkDefaultFont')
        base.configure(size=10)
        style.configure('Treeview', rowheight=int(base.metrics('linespace') * 1.7))
        style.configure('Treeview.Heading', font=('Segoe UI', 10, 'bold'))

        self.build_menu()
        self.build_macro_bar()
        self.build_toolbar()
        self.build_target_bar()
        self.build_bottom()
        self.build_body()
        self.repeat_count.trace_add('write', lambda *a: self.save())
        self.grab_delay.trace_add('write', lambda *a: self.save())

        self.load_file(self.path, silent=True)
        root.protocol('WM_DELETE_WINDOW', self.on_close)
        self.tick()

    # ----- построение окна -----
    def build_menu(self):
        menubar = tk.Menu(self.root)
        m = tk.Menu(menubar, tearoff=0)
        m.add_command(label='Новый макрос…', command=self.new_file)
        m.add_command(label='Открыть…', command=self.open_file)
        m.add_command(label='Сохранить как…', command=self.save_as)
        m.add_separator()
        m.add_command(label='Переименовать…', command=self.rename_macro)
        m.add_command(label='Копия макроса', command=self.copy_macro)
        m.add_command(label='Удалить макрос…', command=self.delete_macro)
        m.add_command(label='Открыть папку с макросами', command=lambda: os.startfile(HERE))
        m.add_separator()
        m.add_command(label='Загрузить пример (колесо)', command=self.load_example)
        m.add_separator()
        m.add_command(label='Выход', command=self.on_close)
        menubar.add_cascade(label='Файл', menu=m)
        self.root.config(menu=menubar)

    def build_macro_bar(self):
        bar = ttk.Frame(self.root, padding=(10, 10, 10, 0))
        bar.pack(side='top', fill='x')
        ttk.Label(bar, text='Макрос:', font=('Segoe UI', 10, 'bold')).pack(side='left')
        self.macro_combo = ttk.Combobox(bar, textvariable=self.macro_name, state='readonly', width=34,
                                        postcommand=self.refresh_macro_list)
        self.macro_combo.pack(side='left', padx=(8, 8))
        self.macro_combo.bind('<<ComboboxSelected>>', lambda e: self.on_macro_selected())
        ttk.Button(bar, text='＋ Новый', command=self.new_file).pack(side='left')
        ttk.Button(bar, text='⧉ Копия', command=self.copy_macro).pack(side='left', padx=(4, 0))
        ttk.Button(bar, text='✎ Переименовать', command=self.rename_macro).pack(side='left', padx=(4, 0))
        ttk.Button(bar, text='🗑 Удалить', command=self.delete_macro).pack(side='left', padx=(4, 0))

    def refresh_macro_list(self):
        self.macro_paths = {os.path.splitext(f)[0]: os.path.join(HERE, f) for f in list_macros()}
        cur = os.path.splitext(os.path.basename(self.path))[0]
        if os.path.normcase(os.path.dirname(os.path.abspath(self.path))) != os.path.normcase(HERE):
            cur = f'{cur} ({os.path.dirname(self.path)})'      # макрос открыт из другой папки
            self.macro_paths[cur] = self.path
        self.macro_combo.configure(values=list(self.macro_paths))
        self.macro_name.set(cur)

    def after_path_change(self):
        """Обновляет заголовок, список макросов и запоминает текущий макрос."""
        self.root.title(f'Кликер — {os.path.basename(self.path)}')
        self.refresh_macro_list()
        self.settings['last_macro'] = self.path
        save_settings(self.settings)

    def on_macro_selected(self):
        path = self.macro_paths.get(self.macro_name.get())
        if not path or os.path.normcase(path) == os.path.normcase(self.path):
            return
        if self.running():
            self.stop()
        self.save()
        self.load_file(path)

    def ask_macro_name(self, title, initial=''):
        while True:
            name = simpledialog.askstring(title, 'Название макроса:', initialvalue=initial, parent=self.root)
            if name is None:
                return None
            name = name.strip()
            if name.lower().endswith('.json'):
                name = name[:-5].strip()
            if not name or any(c in BAD_NAME_CHARS for c in name):
                messagebox.showerror('Название', f'Название не должно быть пустым и содержать символы {BAD_NAME_CHARS}')
                continue
            path = os.path.join(HERE, name + '.json')
            if path == SETTINGS_FILE:
                messagebox.showerror('Название', 'Это имя занято файлом настроек, выберите другое')
                continue
            if os.path.exists(path) and os.path.normcase(path) != os.path.normcase(self.path):
                messagebox.showerror('Название', f'Макрос «{name}» уже есть')
                continue
            return path

    def rename_macro(self):
        if self.running():
            self.stop()
        old = self.path
        path = self.ask_macro_name('Переименовать макрос', os.path.splitext(os.path.basename(old))[0])
        if not path or os.path.normcase(path) == os.path.normcase(old):
            return
        self.save()
        try:
            os.replace(old, path)
        except OSError as e:
            messagebox.showerror('Ошибка', f'Не удалось переименовать:\n{e}')
            return
        self.path = path
        self.after_path_change()
        self.log(f'Макрос переименован в «{os.path.basename(path)}»')

    def copy_macro(self):
        if self.running():
            self.stop()
        base = os.path.splitext(os.path.basename(self.path))[0]
        n, name = 1, f'{base} (копия)'
        while os.path.exists(os.path.join(HERE, name + '.json')):
            n += 1
            name = f'{base} (копия {n})'
        path = self.ask_macro_name('Копия макроса', name)
        if not path:
            return
        self.save()
        self.path = path
        self.save()
        self.after_path_change()
        self.log(f'Создана копия: {os.path.basename(path)}')

    def delete_macro(self):
        if self.running():
            self.stop()
        name = os.path.basename(self.path)
        if not messagebox.askyesno('Удалить макрос', f'Удалить макрос «{name}» безвозвратно?'):
            return
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
        except OSError as e:
            messagebox.showerror('Ошибка', f'Не удалось удалить:\n{e}')
            return
        self.log(f'Макрос «{name}» удалён')
        rest = list_macros()
        if rest:
            self.load_file(os.path.join(HERE, rest[0]))
        else:
            self.load_file(DEFAULT_FILE, silent=True)   # создаст macro.json с примером

    def build_toolbar(self):
        bar = ttk.Frame(self.root, padding=(10, 8, 10, 6))
        bar.pack(side='top', fill='x')
        common = dict(fg='white', activeforeground='white', font=('Segoe UI', 11, 'bold'),
                      relief='flat', padx=16, pady=5, cursor='hand2', bd=0)
        self.btn_start = tk.Button(bar, text='▶  Старт  (F6)', command=self.start,
                                   bg='#2e9e4f', activebackground='#257f40', **common)
        self.btn_start.pack(side='left')
        self.btn_stop = tk.Button(bar, text='■  Стоп  (F7)', command=self.stop,
                                  activebackground='#a33434', disabledforeground='#f7e3e3', **common)
        self.btn_stop.pack(side='left', padx=(8, 0))
        self.set_buttons(False)
        self.status = tk.Label(bar, text='● Остановлен', fg='#888', font=('Segoe UI', 11, 'bold'))
        self.status.pack(side='left', padx=18)

        right = ttk.Frame(bar)
        right.pack(side='right')
        ttk.Label(right, text='Повторов:').pack(side='left')
        self.spin = ttk.Spinbox(right, from_=1, to=1000000, width=7, textvariable=self.repeat_count)
        self.spin.pack(side='left', padx=(6, 8))
        ttk.Checkbutton(right, text='бесконечно', variable=self.repeat_inf,
                        command=self.on_repeat_toggle).pack(side='left')
        ttk.Separator(right, orient='vertical').pack(side='left', fill='y', padx=12)
        ttk.Checkbutton(right, text='Поверх всех окон', variable=self.topmost,
                        command=self.apply_topmost).pack(side='left')

    def build_target_bar(self):
        bar = ttk.Frame(self.root, padding=(10, 0, 10, 8))
        bar.pack(side='top', fill='x')
        ttk.Label(bar, text='Где кликать:', font=('Segoe UI', 10, 'bold')).pack(side='left')
        ttk.Radiobutton(bar, text='Весь экран (двигает мышь)', value='screen', variable=self.mode,
                        command=self.on_mode).pack(side='left', padx=(10, 0))
        ttk.Radiobutton(bar, text='Только в окне:', value='window', variable=self.mode,
                        command=self.on_mode).pack(side='left', padx=(14, 6))
        # Только выбор из списка: клик в любом месте поля открывает список открытых сейчас окон
        self.win_combo = ttk.Combobox(bar, textvariable=self.window_title, width=40, state='readonly',
                                      postcommand=self.refresh_window_list)
        self.win_combo.pack(side='left')
        self.win_combo.bind('<<ComboboxSelected>>', lambda e: self.on_window_selected())
        ttk.Button(bar, text='⟳', width=3, command=self.post_window_list).pack(side='left', padx=(4, 0))
        self.win_state = tk.Label(bar, text='', font=('Segoe UI', 10))
        self.win_state.pack(side='left', padx=10)

        bar2 = ttk.Frame(self.root, padding=(10, 0, 10, 8))
        bar2.pack(side='top', fill='x')
        ttk.Label(bar2, text='Способ клика в окне:').pack(side='left')
        cb = ttk.Combobox(bar2, textvariable=self.click_method, values=list(CLICK_METHODS.values()),
                          state='readonly', width=30)
        cb.pack(side='left', padx=(8, 8))
        cb.bind('<<ComboboxSelected>>', lambda e: self.save())
        ttk.Label(bar2, text='Задержка перехвата, мс:').pack(side='left', padx=(4, 4))
        ttk.Spinbox(bar2, from_=50, to=2000, increment=50, width=6,
                    textvariable=self.grab_delay).pack(side='left', padx=(0, 10))
        ttk.Button(bar2, text='🖱 Тест клика в точку выбранного шага', command=self.test_click).pack(side='left')

    def method(self):
        return CLICK_METHOD_KEYS.get(self.click_method.get(), 'background')

    def delay(self):
        try:
            return max(0, min(5000, int(self.grab_delay.get())))
        except ValueError:
            return 250

    def test_click(self):
        if self.running():
            return
        i = self.selected()
        if i is None or 'x' not in self.steps[i]:
            self.log('Выберите шаг с координатами (например, «Клик»), чтобы проверить клик в его точку')
            return
        st = self.steps[i]
        window = self.window_title.get() if self.mode.get() == 'window' else ''
        self.log(f"Тест: клик в ({st['x']}, {st['y']})"
                 + (f' в окне, способ «{self.click_method.get()}»' if window else ''))
        self.runner = Runner([new_step('click', st)], 1, self.q, window, self.method(), self.delay())
        self.runner.start()
        self.set_buttons(True)

    def target_hwnd(self):
        return find_window(self.window_title.get()) if self.mode.get() == 'window' else None

    def refresh_window_list(self):
        """Открытые сейчас окна; недавно выбранные — первыми."""
        open_now = list_windows()
        recent = [t for t in self.settings.get('recent_windows', []) if t in open_now]
        self.win_combo.configure(values=recent + [t for t in open_now if t not in recent])

    def post_window_list(self):
        self.win_combo.focus_set()
        self.win_combo.tk.call('ttk::combobox::Post', self.win_combo)

    def on_mode(self):
        if self.mode.get() == 'window' and not find_window(self.window_title.get()):
            self.root.after(50, self.post_window_list)     # сразу показать список окон
        self.sync_coords()
        self.load_editor()
        self.save()

    def on_window_selected(self):
        title = self.window_title.get()
        recent = [title] + [t for t in self.settings.get('recent_windows', []) if t != title]
        self.settings['recent_windows'] = recent[:15]
        save_settings(self.settings)
        self.win_combo.selection_clear()
        self.mode.set('window')
        self.sync_coords()
        self.load_editor()
        self.save()

    def sync_coords(self):
        """Пересчитывает координаты шагов между «весь экран» и «внутри окна»."""
        want = self.mode.get() == 'window'
        if want == self.coords_window:
            return
        hwnd = find_window(self.window_title.get())
        if not hwnd:
            if self.window_title.get():
                self.log('Окно не найдено. Откройте его, чтобы пересчитать координаты шагов')
            return
        ox, oy = client_origin(hwnd)
        if (ox, oy) != (0, 0) and any('x' in s for s in self.steps):
            text = (f'Пересчитать координаты шагов относительно окна «{self.window_title.get()}»?'
                    if want else 'Пересчитать координаты шагов обратно в координаты экрана?')
            if messagebox.askyesno('Координаты', text + f'\n\nСдвиг: {ox}, {oy} px.\n'
                                   '(Нажмите «Нет», если уже задали их для этого режима.)'):
                sign = -1 if want else 1
                for s in self.steps:
                    if 'x' in s:
                        s['x'] += sign * ox
                        s['y'] += sign * oy
                self.refresh_tree(select=self.selected())
                self.log('Координаты пересчитаны')
        self.coords_window = want

    def update_window_state(self):
        if self.mode.get() != 'window':
            self.win_state.configure(text='')
            return
        hwnd = self.target_hwnd()
        if not hwnd:
            self.win_state.configure(text='✖ окно не найдено', fg='#c94040')
        elif user32.IsIconic(hwnd):
            self.win_state.configure(text='⚠ окно свёрнуто — разверните (перекрывать можно)', fg='#c98a00')
        else:
            w, h = client_size(hwnd)
            self.win_state.configure(text=f'✔ найдено, {w}×{h}', fg='#2e9e4f')

    def build_body(self):
        pw = ttk.PanedWindow(self.root, orient='horizontal')
        pw.pack(side='top', fill='both', expand=True, padx=10)
        left = ttk.LabelFrame(pw, text=' Шаги макроса (выполняются сверху вниз) ', padding=6)
        self.editor = ttk.LabelFrame(pw, text=' Настройка шага ', padding=12)
        pw.add(left, weight=3)
        pw.add(self.editor, weight=2)

        btns = ttk.Frame(left)
        btns.pack(side='bottom', fill='x', pady=(6, 0))
        add = ttk.Menubutton(btns, text='➕ Добавить шаг')
        menu = tk.Menu(add, tearoff=0)
        for k, title in TYPES.items():
            menu.add_command(label=title, command=lambda k=k: self.add_step(k))
        add['menu'] = menu
        add.pack(side='left')
        for text, cmd in (('⧉ Копия', self.dup_step), ('✖ Удалить', self.delete_step),
                          ('▲ Выше', lambda: self.move_step(-1)), ('▼ Ниже', lambda: self.move_step(1))):
            ttk.Button(btns, text=text, command=cmd).pack(side='left', padx=(6, 0))

        cols = ('n', 'action', 'params', 'note')
        self.tree = ttk.Treeview(left, columns=cols, show='headings', selectmode='browse')
        for c, title, w, stretch in (('n', '№', 40, False), ('action', 'Действие', 140, False),
                                     ('params', 'Параметры', 230, True), ('note', 'Описание', 160, True)):
            self.tree.heading(c, text=title)
            self.tree.column(c, width=w, minwidth=w // 2, stretch=stretch,
                             anchor='center' if c == 'n' else 'w')
        sb = ttk.Scrollbar(left, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        sb.pack(side='left', fill='y')
        self.tree.tag_configure('run', background='#ffe58a')
        self.tree.bind('<<TreeviewSelect>>', lambda e: self.load_editor())
        self.tree.bind('<Delete>', lambda e: self.delete_step())

    def build_bottom(self):
        bottom = ttk.Frame(self.root, padding=(10, 6, 10, 10))
        bottom.pack(side='bottom', fill='x')
        info = ttk.Frame(bottom)
        info.pack(fill='x')
        self.cursor_lbl = ttk.Label(info, font=('Consolas', 10))
        self.cursor_lbl.pack(side='left')
        self.cursor_sw = tk.Label(info, width=3, relief='solid', bd=1)
        self.cursor_sw.pack(side='left', padx=8)
        ttk.Label(info, text='F9 — записать позицию курсора (и цвет) в выбранный шаг',
                  foreground='#666').pack(side='right')
        self.log_box = tk.Text(bottom, height=7, state='disabled', font=('Consolas', 9),
                               bg='#f6f6f6', relief='flat', padx=6, pady=4)
        self.log_box.pack(fill='x', pady=(6, 0))

    # ----- редактор шага -----
    def selected(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def load_editor(self):
        for w in self.editor.winfo_children():
            w.destroy()
        self.swatch = self.pick_btn = None
        i = self.selected()
        f = self.editor
        if i is None:
            ttk.Label(f, text='Выберите шаг в списке слева\nили добавьте новый кнопкой «➕ Добавить шаг».',
                      foreground='#666', justify='left').grid(row=0, column=0, sticky='w')
            return
        st = self.steps[i]
        f.columnconfigure(1, weight=1)
        row = 0
        ttk.Label(f, text=f'Шаг №{i + 1}', font=('Segoe UI', 12, 'bold')).grid(
            row=row, column=0, columnspan=3, sticky='w', pady=(0, 10))
        row += 1

        ttk.Label(f, text='Действие').grid(row=row, column=0, sticky='w', pady=4, padx=(0, 10))
        tv = tk.StringVar(value=TYPES[st['type']])
        cb = ttk.Combobox(f, textvariable=tv, values=list(TYPES.values()), state='readonly')
        cb.grid(row=row, column=1, columnspan=2, sticky='ew', pady=4)
        cb.bind('<<ComboboxSelected>>', lambda e: self.change_type(TYPE_KEYS[tv.get()]))
        row += 1

        for key in FIELDS[st['type']]:
            ttk.Label(f, text=LABELS[key]).grid(row=row, column=0, sticky='w', pady=4, padx=(0, 10))
            if key == 'button':
                v = tk.StringVar(value=BUTTONS[st[key]])
                w = ttk.Combobox(f, textvariable=v, values=list(BUTTONS.values()), state='readonly', width=8)
                w.bind('<<ComboboxSelected>>', lambda e, v=v: self.set_field('button', BUTTON_KEYS[v.get()]))
            else:
                v = tk.StringVar(value=str(st[key]))
                w = tk.Entry(f, textvariable=v, width=12, relief='solid', bd=1, font=('Consolas', 10))
                v.trace_add('write', lambda *a, k=key, v=v, w=w: self.on_entry(k, v, w))
            w.grid(row=row, column=1, sticky='w', pady=4)
            if key == 'color':
                self.swatch = tk.Label(f, width=4, relief='solid', bd=1, bg=to_hex(st['color']))
                self.swatch.grid(row=row, column=2, sticky='w')
            elif key in HINTS:
                ttk.Label(f, text=HINTS[key], foreground='#888').grid(row=row, column=2, sticky='w')
            row += 1

        ttk.Label(f, text='Описание').grid(row=row, column=0, sticky='w', pady=4, padx=(0, 10))
        nv = tk.StringVar(value=st.get('note', ''))
        ttk.Entry(f, textvariable=nv).grid(row=row, column=1, columnspan=2, sticky='ew', pady=4)
        nv.trace_add('write', lambda *a: self.set_field('note', nv.get()))
        row += 1

        if 'x' in st:
            ttk.Separator(f).grid(row=row, column=0, columnspan=3, sticky='ew', pady=12)
            row += 1
            self.pick_btn = ttk.Button(f, text='📍 Взять позицию курсора через 3 с',
                                       command=self.pick_countdown)
            self.pick_btn.grid(row=row, column=0, columnspan=3, sticky='ew')
            row += 1
            hint = '…или наведите курсор и нажмите F9'
            if st['type'] == 'wait_color':
                hint += ' (цвет тоже запишется)'
            ttk.Label(f, text=hint, foreground='#888', wraplength=260, justify='left').grid(
                row=row, column=0, columnspan=3, sticky='w', pady=(4, 0))
            row += 1
        if st['type'] == 'wait_color':
            ttk.Button(f, text='🎨 Взять текущий цвет из точки X, Y', command=self.grab_color).grid(
                row=row, column=0, columnspan=3, sticky='ew', pady=(10, 0))
            row += 1
            ttk.Label(f, text='Макрос будет ждать, пока в точке не появится этот цвет.',
                      foreground='#888', wraplength=260, justify='left').grid(
                row=row, column=0, columnspan=3, sticky='w', pady=(10, 0))

    def on_entry(self, key, var, widget):
        try:
            val = parse_int(var.get())
            if key in NON_NEGATIVE and val < 0 or key == 'color' and val > 0xFFFFFF:
                raise ValueError
        except ValueError:
            widget.configure(bg='#ffd6d6')
            return
        widget.configure(bg='white')
        self.set_field(key, val)
        if key == 'color' and self.swatch:
            self.swatch.configure(bg=to_hex(val))

    def set_field(self, key, val):
        i = self.selected()
        if i is None:
            return
        self.steps[i][key] = val
        self.refresh_row(i)
        self.save()

    def change_type(self, t):
        i = self.selected()
        if i is None or self.steps[i]['type'] == t:
            return
        self.steps[i] = new_step(t, self.steps[i])
        self.refresh_row(i)
        self.load_editor()
        self.save()

    def pick_countdown(self, n=3):
        btn = self.pick_btn
        if not btn or not btn.winfo_exists():
            return
        if n == 0:
            self.capture()
            return
        btn.configure(text=f'Наведите курсор на нужное место… {n}')
        self.root.after(1000, lambda: self.pick_countdown(n - 1))

    def grab_color(self):
        i = self.selected()
        if i is None:
            return
        st = self.steps[i]
        c = self.read_pixel(st['x'], st['y'])
        if c is None or c > 0xFFFFFF:
            self.log('Не удалось прочитать цвет (точка за пределами экрана/окна?)')
            return
        st['color'] = c
        self.refresh_row(i)
        self.load_editor()
        self.save()
        self.log(f"Шаг №{i + 1}: цвет в ({st['x']}, {st['y']}) = {c}")

    def capture(self):
        """F9: записать позицию курсора (и цвет) в выбранный шаг."""
        i = self.selected()
        pos = self.cursor_in_target()
        if pos is None:
            self.log(f'Курсор не над окном «{self.window_title.get()}» (или оно не найдено)')
            return
        x, y = pos
        if i is None or 'x' not in self.steps[i]:
            self.log(f'Курсор ({x}, {y}), цвет {self.read_pixel(x, y)}. Выберите шаг с координатами, чтобы записать.')
            return
        st = self.steps[i]
        st['x'], st['y'] = x, y
        msg = f'Шаг №{i + 1}: записана точка ({x}, {y})' + (' в окне' if self.mode.get() == 'window' else '')
        if st['type'] == 'wait_color':
            st['color'] = self.read_pixel(x, y)
            msg += f", цвет {st['color']}"
        self.refresh_row(i)
        self.load_editor()
        self.save()
        self.log(msg)

    def cursor_in_target(self):
        """Позиция курсора: на экране, а в режиме окна - внутри окна (None, если вне окна)."""
        x, y = cursor_pos()
        if self.mode.get() != 'window':
            return x, y
        hwnd = self.target_hwnd()
        if not hwnd:
            return None
        ox, oy = client_origin(hwnd)
        w, h = client_size(hwnd)
        x, y = x - ox, y - oy
        return (x, y) if 0 <= x < w and 0 <= y < h else None

    def read_pixel(self, x, y):
        """Цвет так же, как его увидит макрос."""
        if self.mode.get() != 'window':
            return pixel(x, y)
        hwnd = self.target_hwnd()
        return window_pixel(hwnd, x, y) if hwnd else None

    # ----- список шагов -----
    def row_values(self, i):
        action, params = describe(self.steps[i])
        return i + 1, action, params, self.steps[i].get('note', '')

    def refresh_row(self, i):
        self.tree.item(str(i), values=self.row_values(i))

    def refresh_tree(self, select=None):
        self.tree.delete(*self.tree.get_children())
        self.run_row = None
        for i in range(len(self.steps)):
            self.tree.insert('', 'end', iid=str(i), values=self.row_values(i))
        if select is not None and self.steps:
            select = max(0, min(select, len(self.steps) - 1))
            self.tree.selection_set(str(select))
            self.tree.see(str(select))
        else:
            self.load_editor()

    def add_step(self, t):
        i = self.selected()
        pos = len(self.steps) if i is None else i + 1
        self.steps.insert(pos, new_step(t))
        self.refresh_tree(select=pos)
        self.save()

    def dup_step(self):
        i = self.selected()
        if i is None:
            return
        self.steps.insert(i + 1, copy.deepcopy(self.steps[i]))
        self.refresh_tree(select=i + 1)
        self.save()

    def delete_step(self):
        i = self.selected()
        if i is None:
            return
        del self.steps[i]
        self.refresh_tree(select=min(i, len(self.steps) - 1) if self.steps else None)
        self.save()

    def move_step(self, d):
        i = self.selected()
        if i is None or not 0 <= i + d < len(self.steps):
            return
        self.steps[i], self.steps[i + d] = self.steps[i + d], self.steps[i]
        self.refresh_tree(select=i + d)
        self.save()

    # ----- запуск / стоп -----
    def running(self):
        return self.runner is not None and self.runner.is_alive()

    def start(self):
        if self.running():
            return
        if not self.steps:
            self.log('Нет шагов: добавьте хотя бы один')
            return
        count = 0
        if not self.repeat_inf.get():
            try:
                count = max(1, int(self.repeat_count.get()))
            except ValueError:
                count = 1
        window = ''
        if self.mode.get() == 'window':
            window = self.window_title.get()
            if not window:
                self.log('Выберите окно, в котором кликать (или режим «Весь экран»)')
                return
            self.sync_coords()
            self.log(f'Кликаю только в окне «{window}», способ: {self.click_method.get()}')
        self.runner = Runner(self.steps, count, self.q, window, self.method(), self.delay())
        self.runner.start()
        self.set_buttons(True)
        self.status.configure(text='● Работает', fg='#2e9e4f')

    def set_buttons(self, running):
        self.btn_start.configure(state='disabled' if running else 'normal',
                                 bg='#9fd3ae' if running else '#2e9e4f')
        self.btn_stop.configure(state='normal' if running else 'disabled',
                                bg='#c94040' if running else '#e6a8a8')

    def stop(self):
        if self.running():
            self.runner.stop_event.set()
            self.status.configure(text='● Остановка…', fg='#c98a00')

    def set_run_row(self, i):
        if self.run_row is not None and self.tree.exists(str(self.run_row)):
            self.tree.item(str(self.run_row), tags=())
        self.run_row = i
        if i is not None and self.tree.exists(str(i)):
            self.tree.item(str(i), tags=('run',))
            self.tree.see(str(i))

    def handle(self, msg):
        kind = msg[0]
        if kind == 'log':
            self.log(msg[1])
        elif kind == 'cycle':
            n, total = msg[1], msg[2]
            self.status.configure(text=f'● Работает — цикл {n}' + (f' из {total}' if total else ''), fg='#2e9e4f')
        elif kind == 'step':
            self.set_run_row(msg[1])
        elif kind == 'done':
            self.set_run_row(None)
            self.set_buttons(False)
            self.status.configure(text='● Остановлен', fg='#888')

    def tick(self):
        for vk, fn in ((VK_F6, self.start), (VK_F7, self.stop), (VK_F9, self.capture)):
            if user32.GetAsyncKeyState(vk) & 0x8000:
                if vk not in self.keys_held:
                    self.keys_held.add(vk)
                    fn()
            else:
                self.keys_held.discard(vk)

        self.ticks += 1
        if self.ticks % 3 == 0:
            sx, sy = cursor_pos()
            c = pixel(sx, sy)
            pos = self.cursor_in_target()
            if self.mode.get() != 'window':
                where = f'Курсор: ({sx}, {sy})'
            elif pos:
                where = f'Курсор в окне: ({pos[0]}, {pos[1]})'
            else:
                where = 'Курсор вне окна'
            if c <= 0xFFFFFF:
                self.cursor_lbl.configure(text=f'{where}    цвет: {c}  {to_hex(c)}')
                self.cursor_sw.configure(bg=to_hex(c))
        if self.ticks % 15 == 0:
            self.update_window_state()

        while True:
            try:
                self.handle(self.q.get_nowait())
            except queue.Empty:
                break
        self.root.after(30, self.tick)

    def log(self, msg):
        self.log_box.configure(state='normal')
        self.log_box.insert('end', time.strftime('[%H:%M:%S] ') + msg + '\n')
        if int(self.log_box.index('end-1c').split('.')[0]) > 500:
            self.log_box.delete('1.0', '100.0')
        self.log_box.see('end')
        self.log_box.configure(state='disabled')

    # ----- настройки окна -----
    def on_repeat_toggle(self, save=True):
        self.spin.state(['disabled'] if self.repeat_inf.get() else ['!disabled'])
        if save:
            self.save()

    def apply_topmost(self, save=True):
        self.root.attributes('-topmost', self.topmost.get())
        if save:
            self.save()

    # ----- файлы -----
    def save(self):
        data = {'steps': self.steps, 'repeat_infinite': self.repeat_inf.get(),
                'repeat_count': self.repeat_count.get(), 'topmost': self.topmost.get(),
                'mode': self.mode.get(), 'window': self.window_title.get(),
                'coords_window': self.coords_window, 'click_method': self.method(),
                'grab_delay': self.delay()}
        try:
            with open(self.path, 'w', encoding='utf-8') as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        except OSError as e:
            self.log(f'Не удалось сохранить: {e}')

    def load_file(self, path, silent=False):
        try:
            with open(path, encoding='utf-8') as fh:
                data = json.load(fh)
            steps = [new_step(s['type'], s) for s in data.get('steps', []) if s.get('type') in TYPES]
        except FileNotFoundError:
            if not silent:
                messagebox.showerror('Ошибка', f'Файл не найден:\n{path}')
                return
            data, steps = {}, [new_step(s['type'], s) for s in DEFAULT_STEPS]
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            messagebox.showerror('Ошибка', f'Не удалось открыть файл:\n{e}')
            if not silent:
                return
            data, steps = {}, [new_step(s['type'], s) for s in DEFAULT_STEPS]
        self.path = path
        self.steps = steps
        self.repeat_inf.set(bool(data.get('repeat_infinite', True)))
        self.repeat_count.set(str(data.get('repeat_count', '1')))
        self.topmost.set(bool(data.get('topmost', False)))
        self.mode.set('window' if data.get('mode') == 'window' else 'screen')
        self.window_title.set(str(data.get('window', '')))
        self.coords_window = bool(data.get('coords_window', False))
        self.click_method.set(CLICK_METHODS.get(data.get('click_method'), CLICK_METHODS['focus']))
        self.grab_delay.set(str(data.get('grab_delay', 250)))
        self.update_window_state()
        self.apply_topmost(save=False)
        self.on_repeat_toggle(save=False)
        self.refresh_tree(select=0 if steps else None)
        self.save()
        self.after_path_change()
        if not silent:
            self.log(f'Открыт {path}')

    def ask_path(self, title):
        return filedialog.asksaveasfilename(title=title, defaultextension='.json', initialdir=HERE,
                                            filetypes=[('Макрос', '*.json'), ('Все файлы', '*.*')])

    def new_file(self):
        """Пустой макрос в новом файле «Макрос N.json» рядом с программой, без диалогов."""
        if self.running():
            self.stop()
        n = 1
        while os.path.exists(os.path.join(HERE, f'Макрос {n}.json')):
            n += 1
        self.path = os.path.join(HERE, f'Макрос {n}.json')
        self.steps = []
        self.coords_window = self.mode.get() == 'window'
        self.refresh_tree()
        self.save()
        self.after_path_change()
        self.log(f'Создан новый макрос: {os.path.basename(self.path)}. Предыдущий сохранён — он есть в списке «Макрос»')

    def open_file(self):
        path = filedialog.askopenfilename(title='Открыть макрос', initialdir=HERE,
                                          filetypes=[('Макрос', '*.json'), ('Все файлы', '*.*')])
        if path:
            self.load_file(path)

    def save_as(self):
        path = self.ask_path('Сохранить макрос как')
        if path:
            self.path = path
            self.save()
            self.after_path_change()
            self.log(f'Сохранено в {path}')

    def load_example(self):
        if messagebox.askyesno('Пример', 'Заменить текущие шаги примером (макрос колеса)?'):
            self.steps = [new_step(s['type'], s) for s in DEFAULT_STEPS]
            self.coords_window = False      # в примере координаты экрана
            self.sync_coords()
            self.refresh_tree(select=0)
            self.save()

    def on_close(self):
        if self.running():
            self.runner.stop_event.set()
            self.runner.join(timeout=1)
        self.save()
        self.root.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    App(root)
    root.mainloop()
