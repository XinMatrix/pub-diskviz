import os
import sqlite3
import json as _json
from flask import request, jsonify, Response, stream_with_context
from openai import OpenAI

import config

# ── 从 app.py 注入的共享变量 ──
_DB_PATH = None
_BASE_DIR = None
_BUNDLE_DIR = None
_check_first_run = None
_get_network_time = None
_init_status_table = None
_set_status = None
_child_processes = None


def _api_conf():
    """每次请求都重新读配置，这样用户改完设置立即生效，不用重启。"""
    c = config.load()
    key = (c.get("api_key") or "").strip()
    base = (c.get("base_url") or "").strip()
    model = (c.get("model_ask") or c.get("model_analyze") or "").strip()
    return key, base, model

# ── 记忆配置 ──
MEMORY_MAX_CHARS   = 1000
MEMORY_MAX_PER_ITEM = 60

ASK_SYSTEM_PROMPT = """你是一位对Windows磁盘结构非常熟悉的专家助手，正在帮用户理解和决策某个具体文件。

你了解常见软件的文件结构、系统目录规范、驱动和运行库的作用、AI工具链、开发环境等。

回答要求：
- 用自然、口语化的中文回答，像一个朋友在帮你看电脑
- 回答要准确、具体，不要模糊
- 如果用户说了对判断有帮助的信息（比如"我已经卸载了这个软件"、"这是我备份的文件"），要根据新信息更新你的建议
- 保持简洁，不要啰嗦，但关键信息要说清楚
- 不要重复用户已经知道的信息

如果用户提供了新的背景信息（比如使用习惯、软件情况），请认可并基于这个信息给出更准确的建议。"""

MEMORY_EXTRACT_PROMPT = """你负责判断"用户这一次输入"里，是否包含值得长期记住的信息。

规则：
1. 只能依据用户明确表达的内容判断。
2. 绝不能使用AI回复内容，绝不能记录AI总结、AI推测。
3. 如果值得记忆，只输出一行：
True: 具体记忆内容
4. 如果不值得记忆，只输出一行：
False:
5. 不要解释，不要补充，不要输出多行。

值得记忆的内容包括：
- 用户明确说明的使用习惯
- 用户的系统环境信息
- 软件安装或卸载状态
- 对某类文件的长期处理原则
- 用户明确说"记住"的规则

不值得记忆的内容包括：
- 针对某个单独文件的一次性提问
- 当前正在问哪个文件
- AI对用户的总结
- 没有长期价值的信息
"""


def _get_memory_file():
    return os.path.join(_BASE_DIR, 'ask_memory.txt')


def _load_memories() -> list:
    mf = _get_memory_file()
    if not os.path.exists(mf):
        return []
    try:
        with open(mf, 'r', encoding='utf-8') as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
        return lines
    except:
        return []


def _save_memories(memories: list):
    memories = [m for m in memories if m and m.strip()]
    seen = set()
    deduped = []
    for m in memories:
        if m not in seen:
            seen.add(m)
            deduped.append(m)
    memories = deduped
    memories = [m[:MEMORY_MAX_PER_ITEM] for m in memories]
    while memories and sum(len(m) for m in memories) > MEMORY_MAX_CHARS:
        memories.pop(0)
    try:
        mf = _get_memory_file()
        os.makedirs(os.path.dirname(mf) if os.path.dirname(mf) else '.', exist_ok=True)
        with open(mf, 'w', encoding='utf-8') as f:
            f.write('\n'.join(memories))
    except:
        pass


def register_routes(app, shared: dict):
    """由 app.py 调用，注入共享变量并注册路由"""
    global _DB_PATH, _BASE_DIR, _BUNDLE_DIR
    global _check_first_run, _get_network_time
    global _init_status_table, _set_status, _child_processes

    _DB_PATH           = shared['DB_PATH']
    _BASE_DIR          = shared['BASE_DIR']
    _BUNDLE_DIR        = shared['BUNDLE_DIR']
    _check_first_run   = shared['check_first_run']
    _get_network_time  = shared['get_network_time']
    _init_status_table = shared['init_status_table']
    _set_status        = shared['set_status']
    _child_processes   = shared['child_processes']

    # ── /api/ask_memory ──────────────────────────────
    @app.route('/api/ask_memory')
    def get_ask_memory():
        memories = _load_memories()
        return jsonify({'memories': memories})

    # ── /api/ask ─────────────────────────────────────
    @app.route('/api/ask', methods=['POST'])
    def ask_file():
        data      = request.get_json()
        file_info = data.get('file', {})
        history   = data.get('history', [])

        path       = file_info.get('path', '')
        label      = file_info.get('label', '')
        reason     = file_info.get('reason', '')
        size_bytes = file_info.get('size_bytes', 0)
        mb         = size_bytes / (1024 * 1024)
        size_str   = f"{mb/1024:.1f}GB" if mb >= 1024 else f"{mb:.0f}MB"

        file_context = (
            f"【当前正在讨论的文件】\n"
            f"路径：{path}\n"
            f"大小：{size_str}\n"
            f"AI初步判断：{label}\n"
            f"判断理由：{reason}"
        )

        memories = _load_memories()
        memory_context = ""
        if memories:
            memory_context = "\n\n【关于这位用户你已知道的信息】\n" + "\n".join(f"- {m}" for m in memories)

        system_with_context = ASK_SYSTEM_PROMPT + "\n\n" + file_context + memory_context

        messages = [{"role": "system", "content": system_with_context}]
        for msg in history:
            if msg.get('role') in ('user', 'assistant') and msg.get('content'):
                messages.append({"role": msg['role'], "content": msg['content']})

        def generate():
            key, base, model = _api_conf()
            if not (key and base and model):
                yield ("data: " + _json.dumps(
                    {'text': '还没有配置模型接口。点右上角「设置」填入 Base URL 和 API Key 就能用了。'},
                    ensure_ascii=False) + "\n\n")
                yield "data: [DONE]\n\n"
                return
            try:
                client = OpenAI(api_key=key, base_url=base)
                stream = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    stream=True,
                    timeout=120,
                    max_tokens=4096,
                )
                for chunk in stream:
                    if not getattr(chunk, 'choices', None):
                        continue
                    delta = chunk.choices[0].delta
                    txt   = getattr(delta, 'content', None) or ''
                    if txt:
                        yield f"data: {_json.dumps({'text': txt}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                yield f"data: {_json.dumps({'text': f'[错误：{str(e)}]'}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype='text/event-stream',
            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
        )

    # ── /api/ask_extract_memory ───────────────────────
    @app.route('/api/ask_extract_memory', methods=['POST'])
    def ask_extract_memory():
        data     = request.get_json()
        user_msg = data.get('user_msg', '')
        if not user_msg:
            return jsonify({'ok': True, 'extracted': []})

        try:
            key, base, model = _api_conf()
            if not (key and base and model):
                return jsonify({'ok': True, 'extracted': []})
            client = OpenAI(api_key=key, base_url=base)
            resp   = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": MEMORY_EXTRACT_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                stream=False,
                timeout=60,
                max_tokens=512,
            )
            raw          = (resp.choices[0].message.content or '').strip()
            new_memories = []
            for line in raw.splitlines():
                line = line.strip()
                if line.startswith("True:"):
                    content = line[len("True:"):].strip()
                    if content:
                        new_memories.append(content)
            if new_memories:
                existing = _load_memories()
                _save_memories(existing + new_memories)
            return jsonify({'ok': True, 'extracted': new_memories})
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)})

    # ── /api/ask_delete_memory ───────────────────────
    @app.route('/api/ask_delete_memory', methods=['POST'])
    def ask_delete_memory():
        data = request.get_json()
        index = data.get('index')
        if index is None:
            return jsonify({'error': 'index 缺失'}), 400
        try:
            memories = _load_memories()
            if 0 <= index < len(memories):
                memories.pop(index)
                _save_memories(memories)
            return jsonify({'ok': True})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    # ── /api/reset ────────────────────────────────────
    @app.route('/api/reset', methods=['POST'])
    def reset_db():
        try:
            last_paths = []
            with sqlite3.connect(_DB_PATH) as conn:
                try:
                    rows       = conn.execute("SELECT DISTINCT scan_path FROM scans").fetchall()
                    last_paths = [r[0] for r in rows]
                except:
                    pass
                for tbl in ('analysis', 'entries', 'scans', 'scan_status', 'trash'):
                    try:
                        conn.execute(f"DELETE FROM {tbl}")
                    except:
                        pass
                conn.commit()
            _init_status_table()
            with sqlite3.connect(_DB_PATH) as conn:
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
            return jsonify({'ok': True, 'last_paths': last_paths})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
