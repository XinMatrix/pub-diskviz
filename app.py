import sys, os

if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
    BUNDLE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    BUNDLE_DIR = BASE_DIR

from flask import Flask, jsonify, send_from_directory, request
from PIL import Image
from pystray import Icon, MenuItem, Menu

import ctypes
import datetime
import json
import multiprocessing
import shutil
import sqlite3
import subprocess
import threading
import time
import webbrowser

import config

# ── 本地状态（上次扫描时间等），存在 %APPDATA%\\DiskViz\\state.json ──
_STATE_PATH = os.path.join(config.config_dir(), "state.json")

_child_processes = []


def _load_state() -> dict:
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(patch: dict) -> None:
    data = _load_state()
    data.update(patch)
    try:
        with open(_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def get_network_time():
    """统一的时间来源。开源版不联网对时，直接用本机时间。"""
    return datetime.datetime.now()


def _check_first_run() -> bool:
    if "first_run" in _load_state():
        return False
    _save_state({"first_run": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    return True


def _get_last_scan_date() -> str:
    return _load_state().get("last_scan_date", "")


def _set_last_scan_date(date_str: str):
    _save_state({"last_scan_date": date_str})


app = Flask(__name__, static_folder=os.path.join(BUNDLE_DIR, 'static'))
DB_PATH = os.environ.get('DB_PATH', os.path.join(BASE_DIR, 'disk_scan.db'))

PROTECTED = [
    r'c:\windows', r'c:\program files', r'c:\program files (x86)',
    r'c:\recovery', r'c:\programdata\microsoft',
]


def get_db():
    if not os.path.exists(DB_PATH):
        raise RuntimeError('数据库不存在，请先扫描')
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_trash_table():
    if not os.path.exists(DB_PATH):
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trash (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                original_path TEXT NOT NULL,
                trash_path    TEXT NOT NULL,
                label         TEXT,
                reason        TEXT,
                size_bytes    INTEGER,
                deleted_at    TEXT NOT NULL
            )
        """)
        conn.commit()


def init_status_table():
    if not os.path.exists(DB_PATH):
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scan_status (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()


def set_status(key, value):
    if not os.path.exists(DB_PATH):
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scan_status (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("INSERT OR REPLACE INTO scan_status (key, value) VALUES (?, ?)", (key, value))
        conn.commit()


# ── 注册 app_routes 里的大型路由 ──
from app_routes import register_routes

register_routes(app, {
    'DB_PATH': DB_PATH,
    'BASE_DIR': BASE_DIR,
    'BUNDLE_DIR': BUNDLE_DIR,
    'check_first_run': _check_first_run,
    'get_network_time': get_network_time,
    'init_status_table': init_status_table,
    'set_status': set_status,
    'child_processes': _child_processes,
})


@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/api/files')
def get_files():
    try:
        conn = get_db()
        rows = conn.execute("""
            SELECT a.path, a.label, a.reason, a.size_bytes, a.analyzed_at,
                   COALESCE(e.days_since_access, 0) AS days_since_access
            FROM analysis a
            LEFT JOIN entries e ON e.path = a.path
            ORDER BY a.size_bytes DESC
        """).fetchall()

        conn.close()
        files = [dict(r) for r in rows]
        for f in files:
            f['reason'] = f['reason'] or ''
            f['size_bytes'] = f['size_bytes'] or 0

        def _sum(label):
            return sum(f['size_bytes'] for f in files if f['label'] == label)

        def _cnt(label):
            return sum(1 for f in files if f['label'] == label)

        return jsonify({
            'files': files,
            'stats': {
                'total': len(files),
                'deletable_count': _cnt('✅ 可删除'),
                'confirm_count': _cnt('⚠️ 需确认'),
                'keep_count': _cnt('❌ 不要删'),
                'deletable_bytes': _sum('✅ 可删除'),
                'confirm_bytes': _sum('⚠️ 需确认'),
                'keep_bytes': _sum('❌ 不要删'),
            }
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/app_suggestions')
def get_app_suggestions():
    try:
        conn = get_db()
        # 表不存在时直接返回空
        has_table = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='app_suggestions'"
        ).fetchone()[0]
        if not has_table:
            conn.close()
            return jsonify({'suggestions': []})
        rows = conn.execute("""
            SELECT app_root, display_name, total_size_bytes,
                   file_count, deletable_count, suggestion
            FROM app_suggestions
            ORDER BY total_size_bytes DESC
        """).fetchall()
        conn.close()
        return jsonify({'suggestions': [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/trash')
def get_trash():
    try:
        conn = get_db()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trash (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                original_path TEXT NOT NULL,
                trash_path    TEXT NOT NULL,
                label         TEXT,
                reason        TEXT,
                size_bytes    INTEGER,
                deleted_at    TEXT NOT NULL
            )
        """)
        conn.commit()
        rows = conn.execute("""
            SELECT id, original_path, trash_path, label, reason, size_bytes, deleted_at
            FROM trash ORDER BY deleted_at DESC
        """).fetchall()
        conn.close()
        items = []
        for r in rows:
            d = dict(r)
            d['exists'] = os.path.exists(d['trash_path'])
            items.append(d)
        total_bytes = sum(i['size_bytes'] or 0 for i in items)
        return jsonify({'items': items, 'total_bytes': total_bytes, 'count': len(items)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/delete', methods=['POST'])
def delete_file():
    try:
        data = request.get_json()
        path = data.get('path', '').strip()
        if not path:
            return jsonify({'error': '路径不能为空'}), 400
        norm = os.path.normpath(path).lower()
        for p in PROTECTED:
            if norm.startswith(p):
                return jsonify({'error': '系统保护路径，拒绝删除'}), 403
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT label, reason, size_bytes FROM analysis WHERE path=?", (path,)).fetchone()
        label = row['label'] if row else ''
        reason = row['reason'] if row else ''
        size_bytes = row['size_bytes'] if row else 0
        if not os.path.exists(path):
            return jsonify({'error': f'文件不存在，可能已被移动或删除：{path}'}), 404
        parent = os.path.dirname(path)
        filename = os.path.basename(path)
        name, ext = os.path.splitext(filename)
        prefix = "DiskVizDeleted_"
        max_name_bytes = 255 - len(prefix.encode()) - len(ext.encode())
        name_truncated = name.encode()[:max_name_bytes].decode(errors='ignore')
        new_name = f"{prefix}{name_truncated}{ext}"
        trash_path = os.path.join(parent, new_name)
        if os.path.exists(trash_path):
            ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            prefix_ts = f"DiskVizDeleted_{ts}_"
            max_name_bytes = 255 - len(prefix_ts.encode()) - len(ext.encode())
            name_truncated = name.encode()[:max_name_bytes].decode(errors='ignore')
            new_name = f"{prefix_ts}{name_truncated}{ext}"
            trash_path = os.path.join(parent, new_name)
        if os.path.isdir(path):
            shutil.move(path, trash_path)
        else:
            os.rename(path, trash_path)
        deleted_at = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trash (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_path TEXT NOT NULL,
                    trash_path    TEXT NOT NULL,
                    label         TEXT,
                    reason        TEXT,
                    size_bytes    INTEGER,
                    deleted_at    TEXT NOT NULL
                )
            """)
            conn.execute("""
                INSERT INTO trash (original_path, trash_path, label, reason, size_bytes, deleted_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (path, trash_path, label, reason, size_bytes, deleted_at))
            conn.execute("DELETE FROM analysis WHERE path=?", (path,))
            conn.execute("DELETE FROM entries  WHERE path=?", (path,))
            conn.commit()
        return jsonify({'ok': True, 'trash_path': trash_path})
    except PermissionError:
        return jsonify({'error': '权限不足（请以管理员身份运行）'}), 403
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/restore', methods=['POST'])
def restore_file():
    try:
        data = request.get_json()
        item_id = data.get('id')
        conn = get_db()
        row = conn.execute("SELECT * FROM trash WHERE id=?", (item_id,)).fetchone()
        conn.close()
        if not row:
            return jsonify({'error': '记录不存在'}), 404
        trash_path = row['trash_path']
        original_path = row['original_path']
        if not os.path.exists(trash_path):
            return jsonify({'error': '临时文件已丢失，无法还原'}), 404
        os.makedirs(os.path.dirname(original_path), exist_ok=True)
        dest = original_path
        if os.path.exists(dest):
            base, ext = os.path.splitext(dest)
            dest = f"{base}_restored{ext}"
        shutil.move(trash_path, dest)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO analysis (analyzed_at, path, label, reason, size_bytes)
                VALUES (?, ?, ?, ?, ?)
            """, (row['deleted_at'], dest, row['label'], row['reason'], row['size_bytes']))
            conn.execute("DELETE FROM trash WHERE id=?", (item_id,))
            conn.commit()
        return jsonify({'ok': True, 'restored_path': dest})
    except PermissionError:
        return jsonify({'error': '权限不足（请以管理员身份运行）'}), 403
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/purge', methods=['POST'])
def purge_file():
    try:
        data = request.get_json()
        item_id = data.get('id')
        conn = get_db()
        row = conn.execute("SELECT * FROM trash WHERE id=?", (item_id,)).fetchone()
        conn.close()
        if not row:
            return jsonify({'error': '记录不存在'}), 404
        if os.path.exists(row['trash_path']):
            os.remove(row['trash_path'])
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM trash WHERE id=?", (item_id,))
            conn.commit()
        return jsonify({'ok': True})
    except PermissionError:
        return jsonify({'error': '权限不足（请以管理员身份运行）'}), 403
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/purge_all', methods=['POST'])
def purge_all():
    try:
        conn = get_db()
        rows = conn.execute("SELECT trash_path FROM trash").fetchall()
        conn.close()
        for r in rows:
            if os.path.exists(r['trash_path']):
                os.remove(r['trash_path'])
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM trash")
            conn.commit()
        return jsonify({'ok': True, 'purged': len(rows)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/stats')
def get_stats():
    try:
        conn = get_db()
        scan = conn.execute("SELECT MAX(scanned_at) as last_scan FROM scans").fetchone()
        conn.close()
        return jsonify({'last_scan': scan['last_scan'] if scan else None})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/db_status')
def db_status():
    if not os.path.exists(DB_PATH):
        return jsonify({'exists': False, 'ai_incomplete': False})
    try:
        with sqlite3.connect(DB_PATH) as conn:
            has_analysis = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='analysis'"
            ).fetchone()[0] > 0
            has_entries = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='entries'"
            ).fetchone()[0] > 0
            if not has_analysis or not has_entries:
                return jsonify({'exists': False, 'ai_incomplete': False})
            entry_count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            analyzed_count = conn.execute("SELECT COUNT(*) FROM analysis").fetchone()[0]
            return jsonify({
                'exists': analyzed_count,
                'ai_incomplete': False,
                'entry_count': entry_count,
                'analyzed_count': analyzed_count,
            })
    except:
        return jsonify({'exists': False, 'ai_incomplete': False})


@app.route('/api/run_scan', methods=['POST'])
def run_scan():
    data = request.get_json()
    paths = data.get('paths', [])
    if not paths:
        return jsonify({'error': '请至少选择一个扫描路径'}), 400
    base = BUNDLE_DIR
    scan_script = os.path.join(base, '1_Scan.py')
    if not os.path.exists(scan_script):
        return jsonify({'error': '脚本不存在：1_Scan.py'}), 500
    try:
        result = subprocess.run(
            [sys.executable, scan_script] + paths,
            cwd=base, capture_output=True, text=True,
            creationflags=0x08000000, encoding='utf-8', errors='replace'
        )
        if result.returncode != 0:
            return jsonify({'error': '1_Scan.py 执行失败', 'detail': result.stderr or result.stdout}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    ai_script = os.path.join(base, '2_AI.py')
    if not os.path.exists(ai_script):
        return jsonify({'error': '脚本不存在：2_AI.py'}), 500
    try:
        result = subprocess.run(
            [sys.executable, ai_script],
            creationflags=0x08000000, cwd=base, capture_output=True,
            text=True, encoding='utf-8', errors='replace'
        )
        if result.returncode != 0:
            return jsonify({'error': '2_AI.py 执行失败', 'detail': result.stderr or result.stdout}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True})


@app.route('/api/drives')
def get_drives():
    import string
    drives = []
    for letter in string.ascii_uppercase:
        drv = letter + ':\\'
        if os.path.exists(drv):
            drives.append(drv)
    return jsonify({'drives': drives})


@app.route('/api/open_dir', methods=['POST'])
def open_dir():
    data = request.get_json()
    path = data.get('path', '').strip()
    if not path:
        return jsonify({'error': '路径为空'}), 400
    target_dir = os.path.dirname(path)
    if not os.path.exists(path) and not os.path.exists(target_dir):
        return jsonify({'error': f'文件及目录均不存在：{target_dir}'}), 404
    subprocess.Popen(['explorer', '/select,', path])
    return jsonify({'ok': True})


@app.route('/api/run_scan_step', methods=['POST'])
def run_scan_step():
    data = request.get_json()
    paths = data.get('paths', [])
    step = data.get('step', 'scan')
    if step == 'scan':
        _set_last_scan_date(datetime.datetime.now().strftime('%Y-%m-%d'))
    base = BUNDLE_DIR
    init_status_table()
    is_frozen = getattr(sys, 'frozen', False)
    if step == 'scan':
        set_status('stage', 'scan')
        set_status('stage_text', '准备扫描')
        set_status('progress', '0')
        if not paths:
            return jsonify({'error': '请选择一个扫描路径'}), 400
        set_status('stage_text', '正在扫描磁盘文件')
        set_status('progress', '5')
        script = os.path.join(base, '1_Scan.exe' if is_frozen else '1_Scan.py')
        args = ([script] + paths) if is_frozen else ([sys.executable, script] + paths)
    else:
        set_status('stage', 'ai')
        set_status('stage_text', '正在 AI 分析文件用途')
        set_status('progress', '50')
        script = os.path.join(base, '2_AI.exe' if is_frozen else '2_AI.py')
        args = [script] if is_frozen else [sys.executable, script]
    if not os.path.exists(script):
        return jsonify({'error': f'脚本不存在：{os.path.basename(script)}'}), 500
    try:
        env = os.environ.copy()
        env['DB_PATH'] = DB_PATH
        proc = subprocess.Popen(
            args, cwd=base, env=env, text=True,
            encoding='utf-8', errors='replace',
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=0x08000000,
        )
        _child_processes.append(proc)
        stdout, stderr = proc.communicate()
        result = type('R', (), {
            'returncode': proc.returncode,
            'stdout': stdout,
            'stderr': stderr,
        })()
        _child_processes.remove(proc)
        if result.returncode != 0:
            detail = f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
            set_status('stage_text', '❌ 出错')
            return jsonify({'error': f'{os.path.basename(script)} 执行失败', 'detail': detail}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    if step == 'scan':
        set_status('progress', '50')
        set_status('stage_text', '文件扫描完成')
    else:
        set_status('progress', '100')
        set_status('stage', 'done')
        set_status('stage_text', '分析完成')
    return jsonify({'ok': True})


@app.route('/api/scan_status')
def scan_status():
    if not os.path.exists(DB_PATH):
        return jsonify({})
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute("SELECT key, value FROM scan_status").fetchall()
        return jsonify({r[0]: r[1] for r in rows})
    except:
        return jsonify({})


@app.route('/api/settings', methods=['GET'])
def get_settings():
    return jsonify(config.masked())


@app.route('/api/settings', methods=['POST'])
def post_settings():
    data = request.get_json(silent=True) or {}
    patch = {}
    for k in ('base_url', 'api_key', 'model_analyze', 'model_ask', 'min_mb', 'preset'):
        if k in data:
            patch[k] = data[k]
    # 前端回传的是掩码（sk-ab…1234），不能拿它覆盖真 key
    if isinstance(patch.get('api_key'), str) and '…' in patch['api_key']:
        patch.pop('api_key')
    cfg = config.save(patch)
    return jsonify({'ok': True, 'config': config.masked(cfg)})


@app.route('/api/settings/test', methods=['POST'])
def test_settings():
    """用当前配置发一次最小请求，告诉用户 key 到底能不能用。"""
    from openai import OpenAI
    key, base = config.load().get('api_key', ''), config.load().get('base_url', '')
    model = config.load().get('model_ask') or config.load().get('model_analyze')
    if not (key and base and model):
        return jsonify({'ok': False, 'error': '还没填完：Base URL、API Key、模型名都要有'})
    try:
        client = OpenAI(api_key=key, base_url=base, timeout=25)
        resp = client.chat.completions.create(
            model=model,
            messages=[{'role': 'user', 'content': 'ping'}],
            max_tokens=8,
        )
        txt = (resp.choices[0].message.content or '').strip()
        return jsonify({'ok': True, 'model': model, 'reply': txt[:60]})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:300]})


PORT = 54329


def create_tray_image():
    if getattr(sys, 'frozen', False):
        img_path = os.path.join(sys._MEIPASS, 'DiskViz.png')
    else:
        img_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'DiskViz.png')
    return Image.open(img_path)


def open_browser():
    webbrowser.open(f"http://127.0.0.1:{PORT}")


def on_exit(icon, item):
    global running
    running = False
    for proc in list(_child_processes):
        try:
            proc.kill()
        except Exception:
            pass
    icon.stop()
    os.kill(os.getpid(), 9)


def setup_tray_icon():
    image = create_tray_image()
    menu = Menu(
        MenuItem('打开页面', lambda icon, item: open_browser()),
        MenuItem('退出程序', on_exit),
    )
    icon = Icon("DiskViz", image, "DiskViz 正在运行", menu)
    icon.run()


def start_flask():
    app.run(debug=False, port=PORT, use_reloader=False, threaded=True)

if __name__ == '__main__':
    if getattr(sys, 'frozen', False) and '_PYI_SPLASH_STATUS' not in os.environ:
        multiprocessing.freeze_support()

    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\DiskVizOSS_SingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:
        webbrowser.open(f"http://127.0.0.1:{PORT}")
        sys.exit(0)

    os.makedirs('static', exist_ok=True)

    if os.path.exists(DB_PATH):
        init_trash_table()
        init_status_table()

    if os.path.exists(DB_PATH):
        with sqlite3.connect(DB_PATH) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS analysis (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    analyzed_at TEXT NOT NULL,
                    path        TEXT NOT NULL UNIQUE,
                    label       TEXT NOT NULL,
                    reason      TEXT,
                    size_bytes  INTEGER
                );
                CREATE TABLE IF NOT EXISTS entries (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id           INTEGER,
                    path              TEXT NOT NULL,
                    filename          TEXT,
                    ext               TEXT,
                    parent_dir        TEXT,
                    path_kind         TEXT,
                    size_bytes        INTEGER,
                    last_modified     TEXT,
                    last_accessed     TEXT,
                    days_since_access INTEGER,
                    siblings          TEXT,
                    install_status    TEXT,
                    is_running        INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS scans (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    scanned_at TEXT,
                    scan_path  TEXT
                );
            """)
            conn.commit()

    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()

    time.sleep(2)
    open_browser()

    setup_tray_icon()
