from flask import Flask, render_template, request, jsonify, session, redirect, url_for, abort, Response
from flask_socketio import SocketIO, emit
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import sqlite3, time, json, os
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'builback-v2-5051')
socketio = SocketIO(app, cors_allowed_origins="*")

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, 'builback.db')

CATEGORIES = ['PC', 'Laptop']
MAX_ATTEMPTS = 2
ROLES = ['admin', 'super', 'volunteer']

# Physical stations: Level 1 (PC build) = L11..L16, Level 2 (Laptop build) = L21..L22
STATIONS = {
    'Level 1': ['L11', 'L12', 'L13', 'L14', 'L15', 'L16'],
    'Level 2': ['L21', 'L22'],
}
STATION_LEVEL = {code: lvl for lvl, codes in STATIONS.items() for code in codes}

# Admin-tweakable event values (Value Adjustments)
DEFAULT_CONFIG = {'level2_cutoff_secs': 180}  # finish Level 1 within this to move to Level 2

# ── Time budget for the auto mini-game popup (seconds) ──────────────────────
MINIGAME_POPUP_AT = 150  # 2:30

# ── Mini games ------------------------------------- tasks ─┬─ model ─┬─ bonus/sec ─┬─ teams only
MINIGAMES = {
    "What's This":     {'tasks': 6, 'model': 'tap',   'bonus': 3, 'teams_only': False, 'icon': '🔍'},
    'Screw Hunt':      {'tasks': 5, 'model': 'tap',   'bonus': 3, 'teams_only': False, 'icon': '🔩'},
    'Port Bingo':      {'tasks': 4, 'model': 'tap',   'bonus': 3, 'teams_only': False, 'icon': '🔌'},
    'Cable Sprint':    {'tasks': 5, 'model': 'tap',   'bonus': 3, 'teams_only': False, 'icon': '🪢'},
    'Fix It Fast':     {'tasks': 4, 'model': 'tap',   'bonus': 3, 'teams_only': False, 'icon': '🔧'},
    "Don't Say It":    {'tasks': 4, 'model': 'timed', 'bonus': 9, 'teams_only': True,  'icon': '🗣️'},
}

# Don't Say It: elapsed-seconds → deduction (first matching threshold wins)
DSI_BRACKETS = [
    (5,  9),
    (10, 7),
    (15, 5),
    (20, 3),
]

# ── Build penalties (seconds added) ─────────────────────────────────────────
DEFAULT_PENALTIES = [
    {'key': 'Leftover Screw',       'label': '🔩 Leftover Screw',       'secs': 30},
    {'key': 'CPU Lever Not Locked', 'label': '⚙️ CPU Lever Not Locked', 'secs': 15},
    {'key': 'RAM Not Seated',       'label': '💾 RAM Not Seated',       'secs': 15},
    {'key': 'Slot Not Secure',      'label': '🖱️ Slot Not Secure',      'secs': 15},
    {'key': 'Cooler Not Flush',     'label': '🌡️ Cooler Not Flush',     'secs': 15},
    {'key': 'Cable Misrouted',      'label': '🪢 Cable Misrouted',       'secs': 10},
    {'key': 'Panel Not Flush',      'label': '📐 Panel Not Flush',       'secs': 10},
]


def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db():
    with get_db() as conn:
        # Migration guard: if a legacy participants table exists without the new
        # columns, drop the old tables so the new schema is (re)created cleanly.
        legacy = conn.execute(
            "SELECT 1 FROM pragma_table_info('participants') WHERE name='play_mode'").fetchone()
        if not legacy:
            for t in ('settings', 'minigame_results', 'penalties', 'runs', 'students', 'participants', 'users'):
                conn.execute(f'DROP TABLE IF EXISTS {t}')
        conn.executescript('''
            DROP TABLE IF EXISTS stations;

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                display_name TEXT,
                station TEXT,
                approved INTEGER DEFAULT 0,
                created_at REAL
            );

            CREATE TABLE IF NOT EXISTS participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                play_mode TEXT NOT NULL CHECK(play_mode IN ('individual','team')),
                approved INTEGER DEFAULT 0,
                created_at REAL
            );

            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                participant_id INTEGER NOT NULL,
                slot INTEGER NOT NULL,  -- 1 primary, 2 teammate
                name TEXT NOT NULL,
                department TEXT NOT NULL,
                semester TEXT NOT NULL,
                college TEXT NOT NULL,
                FOREIGN KEY(participant_id) REFERENCES participants(id)
            );

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                participant_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                station TEXT,
                volunteer_id INTEGER,
                start_time REAL,
                end_time REAL,
                raw_seconds REAL DEFAULT 0,
                penalty_seconds REAL DEFAULT 0,
                bonus_seconds REAL DEFAULT 0,
                final_seconds REAL DEFAULT 0,
                status TEXT DEFAULT 'waiting',
                disqualified INTEGER DEFAULT 0,
                paused_start REAL,
                paused_seconds REAL DEFAULT 0,
                created_at REAL,
                FOREIGN KEY(participant_id) REFERENCES participants(id),
                FOREIGN KEY(volunteer_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS penalties (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                violation TEXT NOT NULL,
                seconds REAL NOT NULL,
                timestamp REAL,
                FOREIGN KEY(run_id) REFERENCES runs(id)
            );

            CREATE TABLE IF NOT EXISTS minigame_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                game TEXT NOT NULL,
                task_index INTEGER DEFAULT 0,
                result TEXT,           -- correct/incorrect/answered/timeout
                seconds REAL DEFAULT 0, -- bonus applied (negative balance increase)
                detail TEXT,
                timestamp REAL,
                FOREIGN KEY(run_id) REFERENCES runs(id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        ''')

        # Seed default admin (first run only)
        if not conn.execute("SELECT id FROM users WHERE username='admin'").fetchone():
            conn.execute(
                "INSERT INTO users (username,password_hash,role,display_name,approved,created_at) VALUES (?,?,?,?,1,?)",
                ('admin', generate_password_hash('admin'), 'admin', 'Admin', time.time())
            )

        # Persist penalty defaults
        cur = conn.execute('SELECT value FROM settings WHERE key=?', ('penalties',))
        if not cur.fetchone():
            conn.execute('INSERT INTO settings (key,value) VALUES (?,?)',
                         ('penalties', json.dumps(DEFAULT_PENALTIES)))

        # Persist event value-adjustment config
        cur = conn.execute('SELECT value FROM settings WHERE key=?', ('config',))
        if not cur.fetchone():
            conn.execute('INSERT INTO settings (key,value) VALUES (?,?)',
                         ('config', json.dumps(DEFAULT_CONFIG)))

        # Migration: add pause columns to existing runs table if missing
        run_cols = {r['name'] for r in conn.execute('PRAGMA table_info(runs)').fetchall()}
        if 'paused_start' not in run_cols:
            conn.execute('ALTER TABLE runs ADD COLUMN paused_start REAL')
        if 'paused_seconds' not in run_cols:
            conn.execute('ALTER TABLE runs ADD COLUMN paused_seconds REAL DEFAULT 0')

        # Ensure each participant has MAX_ATTEMPTS runs per category
        parts = conn.execute('SELECT id FROM participants').fetchall()
        for p in parts:
            for cat in CATEGORIES:
                cnt = conn.execute('SELECT COUNT(*) FROM runs WHERE participant_id=? AND category=?',
                                   (p['id'], cat)).fetchone()[0]
                for _ in range(max(0, MAX_ATTEMPTS - cnt)):
                    conn.execute('INSERT INTO runs (participant_id,category,status,created_at) VALUES (?,?,?,?)',
                                 (p['id'], cat, 'waiting', time.time()))


init_db()


def get_settings():
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='penalties'").fetchone()
    return json.loads(row['value'])


def get_config():
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='config'").fetchone()
    cfg = json.loads(row['value']) if row else {}
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


def save_config(cfg):
    merged = {}
    for k, v in DEFAULT_CONFIG.items():
        merged[k] = v
    for k, v in cfg.items():
        merged[k] = v
    with get_db() as conn:
        conn.execute('INSERT INTO settings (key,value) VALUES (?,?) '
                     'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     ('config', json.dumps(merged)))
    return merged


def elapsed_seconds(run, now=None):
    """Elapsed wall time for a run, excluding any paused duration."""
    if now is None:
        now = time.time()
    base = (now - run['start_time']) if run['start_time'] else 0
    paused = run['paused_seconds'] or 0
    if run['paused_start']:
        paused += max(0.0, now - run['paused_start'])
    return max(0.0, base - paused)


def dsi_deduction(elapsed):
    for limit, ded in DSI_BRACKETS:
        if elapsed < limit:
            return ded
    return 0


def broadcast(channel, data):
    socketio.emit(channel, data)


def broadcast_all():
    broadcast('leaderboard_update', get_leaderboard())
    broadcast('projector_update', get_projector_data())
    broadcast('stations_update', get_stations())


# ── Auth helpers ────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get('uid'):
            if request.path.startswith('/api/'):
                return jsonify({'ok': False, 'error': 'Not logged in'}), 401
            return redirect(url_for('login'))
        return f(*a, **kw)
    return wrapper


def roles_required(*roles):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            u = current_user()
            if not u or u['role'] not in roles:
                if request.path.startswith('/api/'):
                    return jsonify({'ok': False, 'error': 'Forbidden'}), 403
                return redirect(url_for('home'))
            return f(*a, **kw)
        return wrapper
    return deco


def current_user():
    uid = session.get('uid')
    if not uid:
        return None
    with get_db() as conn:
        u = conn.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    return dict(u) if u else None


# ── Pages ───────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET'])
def login():
    return render_template('login.html')


@app.route('/', methods=['GET'])
@login_required
def home():
    u = current_user()
    if not u:
        session.clear()
        return redirect(url_for('login'))
    if u['role'] == 'volunteer':
        return redirect(url_for('volunteer_page'))
    return redirect(url_for('admin_page'))


@app.route('/volunteer', methods=['GET'])
@login_required
@roles_required('volunteer', 'super', 'admin')
def volunteer_page():
    return render_template('volunteer.html', minigames=json.dumps(MINIGAMES),
                           popup_at=MINIGAME_POPUP_AT, penalties=json.dumps(get_settings()),
                           stations=json.dumps(STATIONS), config=json.dumps(get_config()))


@app.route('/competitors', methods=['GET'])
@login_required
@roles_required('volunteer', 'super', 'admin')
def competitors_page():
    return render_template('competitors.html')


@app.route('/admin', methods=['GET'])
@login_required
@roles_required('super', 'admin')
def admin_page():
    u = current_user()
    if not u:
        session.clear()
        return redirect(url_for('login'))
    return render_template('admin.html', minigames=json.dumps(MINIGAMES),
                           popup_at=MINIGAME_POPUP_AT, penalties=json.dumps(get_settings()),
                           is_admin=(u['role'] == 'admin'))


@app.route('/projector', methods=['GET'])
def projector_page():
    return render_template('projector.html', minigames=json.dumps(MINIGAMES),
                           categories=CATEGORIES)


@app.route('/preview/<name>', methods=['GET'])
def preview_page(name):
    path = os.path.join(BASE, 'previews', f'{name}.html')
    if not os.path.isfile(path):
        abort(404)
    return Response(open(path, encoding='utf-8').read(), mimetype='text/html')


# ── Auth API ────────────────────────────────────────────────────────────────

@app.route('/api/auth/register', methods=['POST'])
def register():
    """Self-registration is open ONLY for volunteers. Admin/Super accounts can
    only be created by an existing admin (see /api/auth/create-staff)."""
    d = request.json
    username = (d.get('username') or '').strip()
    password = d.get('password') or ''
    role = 'volunteer'
    display = (d.get('display_name') or '').strip() or username
    station = (d.get('station') or '').strip()
    if len(username) < 3 or len(password) < 4:
        return jsonify({'ok': False, 'error': 'Username (3+) and password (4+) required'})
    with get_db() as conn:
        if conn.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone():
            return jsonify({'ok': False, 'error': 'Username taken'})
        conn.execute('''INSERT INTO users (username,password_hash,role,display_name,station,approved,created_at)
                        VALUES (?,?,?,?,?,0,?)''',
                     (username, generate_password_hash(password), role, display, station, time.time()))
    socketio.emit('staff_update')
    return jsonify({'ok': True, 'msg': 'Volunteer registered — awaiting approval'})


@app.route('/api/auth/create-staff', methods=['POST'])
@login_required
@roles_required('admin')
def create_staff():
    """Only an existing admin can promote/create admin or super accounts."""
    d = request.json
    username = (d.get('username') or '').strip()
    password = d.get('password') or ''
    role = d.get('role')
    display = (d.get('display_name') or '').strip() or username
    station = (d.get('station') or '').strip()
    if role not in ('admin', 'super'):
        return jsonify({'ok': False, 'error': 'Only admin/super may be created here (volunteers self-register)'})
    if len(username) < 3 or len(password) < 4:
        return jsonify({'ok': False, 'error': 'Username (3+) and password (4+) required'})
    with get_db() as conn:
        if conn.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone():
            return jsonify({'ok': False, 'error': 'Username taken'})
        conn.execute('''INSERT INTO users (username,password_hash,role,display_name,station,approved,created_at)
                        VALUES (?,?,?,?,?,1,?)''',
                     (username, generate_password_hash(password), role, display, station, time.time()))
    return jsonify({'ok': True, 'msg': f'{role} account created'})


@app.route('/api/auth/login', methods=['POST'])
def do_login():
    d = request.json
    username = (d.get('username') or '').strip()
    password = d.get('password') or ''
    with get_db() as conn:
        u = conn.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not u or not check_password_hash(u['password_hash'], password):
        return jsonify({'ok': False, 'error': 'Invalid credentials'})
    if not u['approved']:
        return jsonify({'ok': False, 'error': 'Your account is awaiting approval'})
    session['uid'] = u['id']
    return jsonify({'ok': True, 'role': u['role']})


@app.route('/api/auth/logout', methods=['POST'])
def logout():
    session.pop('uid', None)
    return jsonify({'ok': True})


@app.route('/api/auth/me', methods=['GET'])
@login_required
def me():
    u = current_user()
    return jsonify({'ok': True, 'user': u})


# ── Registration / participant API ──────────────────────────────────────────

@app.route('/api/registrations', methods=['GET'])
@login_required
@roles_required('admin', 'super')
def list_registrations():
    with get_db() as conn:
        rows = conn.execute('''SELECT p.id, p.play_mode, p.approved, p.created_at,
                               (SELECT COUNT(*) FROM students s WHERE s.participant_id=p.id) as n_students
                               FROM participants p ORDER BY p.created_at DESC''').fetchall()
        regs = []
        for r in rows:
            stud = conn.execute('''SELECT slot,name,department,semester,college FROM students
                                   WHERE participant_id=? ORDER BY slot''', (r['id'],)).fetchall()
            regs.append({**dict(r), 'students': [dict(x) for x in stud]})
    return jsonify(regs)


@app.route('/api/registrations/approve', methods=['POST'])
@login_required
@roles_required('admin', 'super')
def approve_registration():
    pid = request.json['participant_id']
    with get_db() as conn:
        conn.execute('UPDATE participants SET approved=1 WHERE id=?', (pid,))
        for cat in CATEGORIES:
            cnt = conn.execute('SELECT COUNT(*) FROM runs WHERE participant_id=? AND category=?',
                               (pid, cat)).fetchone()[0]
            for _ in range(max(0, MAX_ATTEMPTS - cnt)):
                conn.execute('INSERT INTO runs (participant_id,category,status,created_at) VALUES (?,?,?,?)',
                             (pid, cat, 'waiting', time.time()))
    broadcast_all()
    return jsonify({'ok': True})


@app.route('/api/registrations/reject', methods=['POST'])
@login_required
@roles_required('admin', 'super')
def reject_registration():
    pid = request.json['participant_id']
    with get_db() as conn:
        conn.execute('DELETE FROM participants WHERE id=? AND approved=0', (pid,))
    broadcast_all()
    return jsonify({'ok': True})


@app.route('/api/participants', methods=['POST'])
@login_required
@roles_required('volunteer', 'super', 'admin')
def add_participant():
    """Add a competitor (individual or team). Staff-only; competitor details
    are filled in by the logged-in staff member (was previously public)."""
    d = request.json
    mode = d.get('play_mode')
    if mode not in ('individual', 'team'):
        return jsonify({'ok': False, 'error': 'Invalid play mode'})
    students = d.get('students') or []
    station = (d.get('station') or '').strip() or None
    if station and station not in STATION_LEVEL:
        return jsonify({'ok': False, 'error': f"Unknown station '{station}'"})
    if len(students) < 1:
        return jsonify({'ok': False, 'error': 'Primary student details required'})

    def clean(x):
        return {k: (x.get(k) or '').strip() for k in ('name', 'department', 'semester', 'college')}

    s1 = clean(students[0])
    if not all([s1['name'], s1['department'], s1['semester'], s1['college']]):
        return jsonify({'ok': False, 'error': 'Fill all primary student fields'})

    with get_db() as conn:
        cur = conn.execute('INSERT INTO participants (play_mode, approved, created_at) VALUES (?,?,?)',
                           (mode, 1, time.time()))
        pid = cur.lastrowid
        conn.execute('INSERT INTO students (participant_id,slot,name,department,semester,college) VALUES (?,1,?,?,?,?)',
                     (pid, s1['name'], s1['department'], s1['semester'], s1['college']))
        if mode == 'team':
            s2 = clean(students[1]) if len(students) > 1 else {'name': '', 'department': '', 'semester': '', 'college': ''}
            if not all([s2['name'], s2['department'], s2['semester'], s2['college']]):
                return jsonify({'ok': False, 'error': 'Team play requires teammate details'})
            conn.execute('INSERT INTO students (participant_id,slot,name,department,semester,college) VALUES (?,2,?,?,?,?)',
                         (pid, s2['name'], s2['department'], s2['semester'], s2['college']))
        for cat in CATEGORIES:
            for _ in range(MAX_ATTEMPTS):
                conn.execute('INSERT INTO runs (participant_id,category,status,created_at) VALUES (?,?,?,?)',
                             (pid, cat, 'waiting', time.time()))
            if station and cat == 'PC':
                conn.execute('UPDATE runs SET station=? WHERE participant_id=? AND category=? '
                             'AND status="waiting" ORDER BY id LIMIT 1',
                             (station, pid, cat))
    broadcast_all()
    return jsonify({'ok': True, 'participant_id': pid, 'msg': 'Registered — ready to compete'})


@app.route('/api/participants/list', methods=['GET'])
@login_required
@roles_required('volunteer', 'super', 'admin')
def list_participants():
    with get_db() as conn:
        rows = conn.execute('''SELECT p.id, p.play_mode, p.approved,
                               (SELECT name FROM students s WHERE s.participant_id=p.id AND s.slot=1) as name,
                               (SELECT name FROM students s WHERE s.participant_id=p.id AND s.slot=2) as mate
                               FROM participants p WHERE p.approved=1 ORDER BY p.id''').fetchall()
        out = []
        for r in rows:
            runs = conn.execute('''SELECT id,category,station,status,disqualified,start_time,end_time,
                                   raw_seconds,penalty_seconds,bonus_seconds,final_seconds,
                                   paused_start,paused_seconds
                                   FROM runs WHERE participant_id=? ORDER BY id''', (r['id'],)).fetchall()
            out.append({**dict(r), 'runs': [dict(x) for x in runs]})
    return jsonify(out)


# ── Run control ─────────────────────────────────────────────────────────────

def recalc(run, conn):
    if run['status'] == 'finished' and run['start_time']:
        raw = elapsed_seconds(run, run['end_time'] or run['start_time'])
        final = max(0.0, raw + (run['penalty_seconds'] or 0) - (run['bonus_seconds'] or 0))
        conn.execute('UPDATE runs SET final_seconds=? WHERE id=?', (final, run['id']))


@app.route('/api/run/start', methods=['POST'])
@login_required
@roles_required('volunteer', 'super', 'admin')
def start_run():
    d = request.json
    run_id = d.get('run_id')
    station = (d.get('station') or '').strip() or None
    u = current_user()
    now = time.time()
    with get_db() as conn:
        run = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if not run:
            return jsonify({'ok': False, 'error': 'Run not found'})
        if run['status'] == 'running':
            return jsonify({'ok': False, 'error': 'Already running'})
        conn.execute('''UPDATE runs SET start_time=?, status='running', end_time=NULL,
                        raw_seconds=0, penalty_seconds=0, bonus_seconds=0, final_seconds=0, disqualified=0,
                        paused_start=NULL, paused_seconds=0,
                        volunteer_id=?, station=? WHERE id=?''',
                     (now, u['id'], station or '', run_id))
        if station:
            conn.execute('UPDATE users SET station=? WHERE id=?', (station, u['id']))
        conn.execute('DELETE FROM penalties WHERE run_id=?', (run_id,))
        conn.execute('DELETE FROM minigame_results WHERE run_id=?', (run_id,))
    broadcast_all()
    return jsonify({'ok': True, 'start_time': now})


@app.route('/api/run/stop', methods=['POST'])
@login_required
@roles_required('volunteer', 'super', 'admin')
def stop_run():
    run_id = request.json['run_id']
    now = time.time()
    with get_db() as conn:
        run = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if not run or run['status'] != 'running':
            return jsonify({'ok': False, 'error': 'Not running'})
        # finalize any in-flight pause before computing raw
        if run['paused_start']:
            paused_seconds = (run['paused_seconds'] or 0) + (now - run['paused_start'])
            conn.execute('UPDATE runs SET paused_start=NULL, paused_seconds=? WHERE id=?',
                         (paused_seconds, run_id))
            run = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        raw = elapsed_seconds(run, now)
        final = max(0.0, raw + (run['penalty_seconds'] or 0) - (run['bonus_seconds'] or 0))
        conn.execute('UPDATE runs SET end_time=?, raw_seconds=?, final_seconds=?, status="finished" WHERE id=?',
                     (now, raw, final, run_id))
    broadcast_all()
    return jsonify({'ok': True, 'raw': raw, 'penalty': run['penalty_seconds'],
                    'bonus': run['bonus_seconds'], 'final': final})


@app.route('/api/run/penalty', methods=['POST'])
@login_required
@roles_required('volunteer', 'super', 'admin')
def add_penalty():
    d = request.json
    run_id = d['run_id']
    violation = d['violation']
    secs = 0
    for p in get_settings():
        if p['key'] == violation:
            secs = p['secs']
            break
    with get_db() as conn:
        run = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if not run or run['status'] not in ('running', 'waiting', 'finished'):
            return jsonify({'ok': False, 'error': 'Invalid run'})
        conn.execute('INSERT INTO penalties (run_id,violation,seconds,timestamp) VALUES (?,?,?,?)',
                     (run_id, violation, secs, time.time()))
        conn.execute('UPDATE runs SET penalty_seconds = penalty_seconds + ? WHERE id=?', (secs, run_id))
        recalc(conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone(), conn)
    broadcast_all()
    return jsonify({'ok': True, 'added': secs})


@app.route('/api/run/penalty/remove', methods=['POST'])
@login_required
@roles_required('admin', 'super')
def remove_penalty():
    pid = request.json['penalty_id']
    with get_db() as conn:
        p = conn.execute('SELECT * FROM penalties WHERE id=?', (pid,)).fetchone()
        if not p:
            return jsonify({'ok': False, 'error': 'Not found'})
        conn.execute('DELETE FROM penalties WHERE id=?', (pid,))
        conn.execute('UPDATE runs SET penalty_seconds = MAX(0, penalty_seconds - ?) WHERE id=?',
                     (p['seconds'], p['run_id']))
        recalc(conn.execute('SELECT * FROM runs WHERE id=?', (p['run_id'],)).fetchone(), conn)
    broadcast_all()
    return jsonify({'ok': True})


@app.route('/api/run/disqualify', methods=['POST'])
@login_required
@roles_required('volunteer', 'super', 'admin')
def disqualify():
    run_id = request.json['run_id']
    with get_db() as conn:
        conn.execute('UPDATE runs SET disqualified=1, status="finished" WHERE id=?', (run_id,))
    broadcast_all()
    return jsonify({'ok': True})


@app.route('/api/run/reset', methods=['POST'])
@login_required
@roles_required('admin', 'super')
def reset_run():
    run_id = request.json['run_id']
    with get_db() as conn:
        conn.execute('''UPDATE runs SET status='waiting', start_time=NULL, end_time=NULL,
                        raw_seconds=0, penalty_seconds=0, bonus_seconds=0, final_seconds=0, disqualified=0,
                        paused_start=NULL, paused_seconds=0 WHERE id=?''',
                     (run_id,))
        conn.execute('DELETE FROM penalties WHERE run_id=?', (run_id,))
        conn.execute('DELETE FROM minigame_results WHERE run_id=?', (run_id,))
    broadcast_all()
    return jsonify({'ok': True})


@app.route('/api/run/delete', methods=['POST'])
@login_required
@roles_required('admin', 'super')
def delete_run():
    """Remove a single run (and its penalties/mini-game results) entirely."""
    run_id = request.json['run_id']
    with get_db() as conn:
        conn.execute('DELETE FROM penalties WHERE run_id=?', (run_id,))
        conn.execute('DELETE FROM minigame_results WHERE run_id=?', (run_id,))
        conn.execute('DELETE FROM runs WHERE id=?', (run_id,))
    broadcast_all()
    return jsonify({'ok': True})


@app.route('/api/run/pause', methods=['POST'])
@login_required
@roles_required('volunteer', 'super', 'admin')
def pause_run():
    """Pause a running run's timer (e.g. during mini-games)."""
    run_id = request.json['run_id']
    with get_db() as conn:
        run = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if not run or run['status'] != 'running':
            return jsonify({'ok': False, 'error': 'Not running'})
        if not run['paused_start']:
            conn.execute('UPDATE runs SET paused_start=? WHERE id=?', (time.time(), run_id))
    return jsonify({'ok': True})


@app.route('/api/run/resume', methods=['POST'])
@login_required
@roles_required('volunteer', 'super', 'admin')
def resume_run():
    run_id = request.json['run_id']
    with get_db() as conn:
        run = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if not run or run['status'] != 'running':
            return jsonify({'ok': False, 'error': 'Not running'})
        if run['paused_start']:
            now = time.time()
            tot = (run['paused_seconds'] or 0) + (now - run['paused_start'])
            conn.execute('UPDATE runs SET paused_start=NULL, paused_seconds=? WHERE id=?', (tot, run_id))
    broadcast_all()
    return jsonify({'ok': True})


# ── Station assignment ───────────────────────────────────────────────────────

@app.route('/api/stations/free')
@login_required
def free_stations():
    """Stations with no run waiting/running on them (assignable)."""
    with get_db() as conn:
        busy = conn.execute('''SELECT DISTINCT station FROM runs
                               WHERE station IS NOT NULL AND station != ''
                               AND status IN ('waiting','running')''').fetchall()
        busy = {r['station'] for r in busy}
    out = []
    for level, codes in STATIONS.items():
        free = [c for c in codes if c not in busy]
        out.append({'level': level, 'category': 'PC' if level == 'Level 1' else 'Laptop',
                    'stations': free})
    return jsonify(out)


@app.route('/api/run/assign-station', methods=['POST'])
@login_required
@roles_required('volunteer', 'super', 'admin')
def assign_station():
    """Assign a physical station to a participant's next waiting run (Level 1
    stations for PC runs, Level 2 stations for Laptop runs)."""
    d = request.json
    participant_id = d.get('participant_id')
    category = d.get('category')
    station = (d.get('station') or '').strip()
    if category not in CATEGORIES:
        return jsonify({'ok': False, 'error': 'Invalid category'})
    if station not in STATION_LEVEL:
        return jsonify({'ok': False, 'error': f"Unknown station '{station}'"})
    want_level = 'Level 1' if category == 'PC' else 'Level 2'
    if STATION_LEVEL[station] != want_level:
        return jsonify({'ok': False, 'error': f'{category} runs use {want_level} stations only'})
    with get_db() as conn:
        run = conn.execute('''SELECT id FROM runs WHERE participant_id=? AND category=?
                              AND status IN ('waiting','running') ORDER BY id LIMIT 1''',
                           (participant_id, category)).fetchone()
        if not run:
            return jsonify({'ok': False, 'error': 'No available run for this category'})
        conn.execute('UPDATE runs SET station=? WHERE id=?', (station, run['id']))
    broadcast_all()
    return jsonify({'ok': True, 'run_id': run['id']})


# ── Mini-game API ───────────────────────────────────────────────────────────

@app.route('/api/minigame/result', methods=['POST'])
@login_required
@roles_required('volunteer', 'super', 'admin')
def minigame_result():
    d = request.json
    run_id = d['run_id']
    game = d['game']
    task_index = int(d.get('task_index', 0))
    result = d.get('result')   # correct / incorrect / answered / timeout
    detail = d.get('detail', '')
    cfg = MINIGAMES.get(game)
    if not cfg:
        return jsonify({'ok': False, 'error': 'Unknown game'})

    bonus = 0.0
    if cfg['model'] == 'tap':
        bonus = cfg['bonus'] if result == 'correct' else 0.0
    else:  # timed - Don't Say It
        elapsed = float(d.get('elapsed', 20) or 20)
        if result == 'answered':
            bonus = dsi_deduction(elapsed)
            detail = f"guessed @ {elapsed:0.1f}s"
        else:
            detail = "timeout"

    with get_db() as conn:
        run = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if not run or run['status'] != 'running':
            return jsonify({'ok': False, 'error': 'Run not running'})
        conn.execute('''INSERT INTO minigame_results (run_id,game,task_index,result,seconds,detail,timestamp)
                        VALUES (?,?,?,?,?,?,?)''',
                     (run_id, game, task_index, result, bonus, detail, time.time()))
        conn.execute('UPDATE runs SET bonus_seconds = bonus_seconds + ? WHERE id=?', (bonus, run_id))
        run2 = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if run2['start_time']:
            raw = elapsed_seconds(run2, time.time())
            final = max(0.0, raw + (run2['penalty_seconds'] or 0) - (run2['bonus_seconds'] or 0))
            conn.execute('UPDATE runs SET final_seconds=? WHERE id=?', (final, run_id))
    broadcast_all()
    return jsonify({'ok': True, 'bonus': bonus})


@app.route('/api/minigame/results/<int:run_id>')
@login_required
def minigame_results(run_id):
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM minigame_results WHERE run_id=? ORDER BY id', (run_id,)).fetchall()
    return jsonify([dict(r) for r in rows])


# ── Users / approvals (staff) ───────────────────────────────────────────────

@app.route('/api/staff/pending')
@login_required
@roles_required('admin', 'super')
def staff_pending():
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM users WHERE approved=0 ORDER BY created_at').fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/staff/list')
@login_required
@roles_required('admin', 'super')
def staff_list():
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM users WHERE approved=1 ORDER BY role, id').fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/staff/approve', methods=['POST'])
@login_required
@roles_required('admin', 'super')
def staff_approve():
    uid = request.json['uid']
    with get_db() as conn:
        conn.execute('UPDATE users SET approved=1 WHERE id=?', (uid,))
    return jsonify({'ok': True})


@app.route('/api/staff/role', methods=['POST'])
@login_required
@roles_required('admin')
def staff_role():
    """Admin manually assigns a role to an approved staff account."""
    uid = request.json['uid']
    role = request.json.get('role')
    if role not in ROLES:
        return jsonify({'ok': False, 'error': 'Invalid role'})
    me = current_user()
    if uid == me['id']:
        return jsonify({'ok': False, 'error': "You can't change your own role"})
    with get_db() as conn:
        conn.execute('UPDATE users SET role=? WHERE id=?', (role, uid))
    return jsonify({'ok': True, 'msg': f'Role set to {role}'})


@app.route('/api/staff/delete', methods=['POST'])
@login_required
@roles_required('admin')
def staff_delete():
    uid = request.json['uid']
    me = current_user()
    if uid == me['id']:
        return jsonify({'ok': False, 'error': "You can't delete your own account"})
    with get_db() as conn:
        # unlink any runs that volunteer started, then delete
        conn.execute('UPDATE runs SET volunteer_id=NULL WHERE volunteer_id=?', (uid,))
        conn.execute('DELETE FROM users WHERE id=?', (uid,))
    return jsonify({'ok': True})


# ── Settings / config (admin) ───────────────────────────────────────────────

@app.route('/api/settings/penalties', methods=['GET'])
@login_required
def get_penalty_settings():
    return jsonify(get_settings())


@app.route('/api/settings/penalties', methods=['POST'])
@login_required
@roles_required('admin')
def save_penalty_settings():
    items = request.json.get('penalties') or []
    cleaned = []
    for it in items:
        try:
            cleaned.append({'key': it['key'], 'label': it.get('label', it['key']), 'secs': int(it['secs'])})
        except Exception:
            continue
    with get_db() as conn:
        conn.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(cleaned), 'penalties'))
    broadcast_all()
    return jsonify({'ok': True, 'penalties': get_settings()})


@app.route('/api/settings/config', methods=['GET'])
@login_required
def get_config_settings():
    return jsonify(get_config())


@app.route('/api/settings/config', methods=['POST'])
@login_required
@roles_required('admin')
def save_config_settings():
    cfg = save_config({k: v for k, v in (request.json or {}).items()})
    broadcast_all()
    return jsonify({'ok': True, 'config': cfg})


# ── Leaderboard / projector data ────────────────────────────────────────────

def get_leaderboard():
    with get_db() as conn:
        rows = conn.execute('''SELECT r.*, p.play_mode,
                               (SELECT name FROM students s WHERE s.participant_id=p.id AND s.slot=1) as name,
                               (SELECT name FROM students s WHERE s.participant_id=p.id AND s.slot=2) as mate
                               FROM runs r JOIN participants p ON r.participant_id=p.id
                               WHERE p.approved=1 ORDER BY r.id''').fetchall()
        runs = [dict(r) for r in rows]
    return runs


def combined_total(participant_id):
    """Best finished, non-DQ time per category, summed. Complete only when a
    category is either fully disqualified or has a finished valid run."""
    with get_db() as conn:
        rows = conn.execute('''SELECT category, final_seconds, status, disqualified FROM runs
                               WHERE participant_id=?''', (participant_id,)).fetchall()
    best = {}
    has_dq = set()
    for r in rows:
        if r['status'] == 'finished':
            if r['disqualified']:
                has_dq.add(r['category'])
            elif r['category'] not in best or r['final_seconds'] < best[r['category']]:
                best[r['category']] = r['final_seconds']
    if min(len(best), len(CATEGORIES)) != len(CATEGORIES):
        return None, False
    return sum(best.get(c, 0) for c in CATEGORIES), True


def get_projector_data():
    led = get_leaderboard()

    # Per-category rankings (best finished non-DQ attempt per participant)
    ranked = {c: [] for c in CATEGORIES}
    dq_rows = {c: [] for c in CATEGORIES}
    by_participant_cat = {}
    for r in led:
        if r['status'] == 'finished':
            key = (r['participant_id'], r['category'])
            if r['disqualified']:
                by_participant_cat.setdefault(key, {'dq': r})
            else:
                cur = by_participant_cat.setdefault(key, {'dq': None})
                if cur.get('best') is None or r['final_seconds'] < cur['best']['final_seconds']:
                    cur['best'] = r
    for key, v in by_participant_cat.items():
        cat = key[1]
        if v.get('best'):
            ranked[cat].append(v['best'])
        elif v.get('dq'):
            dq_rows[cat].append(v['dq'])
    for c in CATEGORIES:
        ranked[c] = sorted(ranked[c], key=lambda x: x['final_seconds'])

    # Combined best (best PC + best Laptop) — only participants with both finished
    by_participant = {}
    for r in led:
        by_participant.setdefault(r['participant_id'], []).append(r)
    combined = []
    for pid, runs in by_participant.items():
        total, complete = combined_total(pid)
        if complete and total is not None:
            name = next((r['name'] for r in runs if r['name']), '')
            mate = next((r['mate'] for r in runs if r['mate']), '')
            play = next((r['play_mode'] for r in runs), '')
            combined.append({'participant_id': pid, 'name': name, 'mate': mate,
                             'play_mode': play, 'total': total})
    combined = sorted(combined, key=lambda x: x['total'])

    active = [r for r in led if r['status'] == 'running']
    waiting = [r for r in led if r['status'] == 'waiting']
    finished_count = sum(1 for r in led if r['status'] == 'finished' and not r['disqualified'])
    pc_finished = sum(1 for r in led if r['status'] == 'finished' and not r['disqualified'] and r['category'] == 'PC')
    laptop_finished = sum(1 for r in led if r['status'] == 'finished' and not r['disqualified'] and r['category'] == 'Laptop')

    return {
        'runs': led,
        'ranked': ranked,
        'dq_rows': dq_rows,
        'combined': combined,
        'active': active,
        'waiting': waiting,
        'finished_count': finished_count,
        'pc_finished': pc_finished,
        'laptop_finished': laptop_finished,
        'total_participants': len(by_participant),
        'stations': get_stations_v2(),
    }


def get_stations():
    """Physical stations (L11..L16 Level 1, L21..L22 Level 2) with the team
    currently assigned/running at each. A station is 'occupied' by the most
    recent waiting or running run assigned to it."""
    with get_db() as conn:
        rows = conn.execute('''SELECT r.id, r.category, r.station, r.status, r.start_time, r.end_time,
                               r.final_seconds, r.disqualified, r.paused_start, r.paused_seconds,
                               p.play_mode,
                               (SELECT name FROM students s WHERE s.participant_id=p.id AND s.slot=1) as name,
                               (SELECT name FROM students s WHERE s.participant_id=p.id AND s.slot=2) as mate
                               FROM runs r JOIN participants p ON r.participant_id=p.id
                               WHERE r.station IS NOT NULL AND r.station != '' AND p.approved=1
                               ORDER BY r.id''').fetchall()
        runs = [dict(r) for r in rows]

    by_station = {}
    for r in runs:
        by_station.setdefault(r['station'], []).append(r)

    out = []
    for level, codes in STATIONS.items():
        for code in codes:
            pool = by_station.get(code, [])
            # Most recent run that is not finished; else most recent finished (for flash)
            current = None
            for r in reversed(pool):
                if r['status'] != 'finished':
                    current = r
                    break
            if current is None and pool:
                current = pool[-1]
            out.append({
                'code': code,
                'level': level,
                'category': 'PC' if level == 'Level 1' else 'Laptop',
                'run': current,
            })
    return out


def get_stations_v2():
    return get_stations()


@app.route('/api/leaderboard')
@login_required
def api_leaderboard():
    return jsonify(get_leaderboard())


@app.route('/api/projector')
def api_projector():
    return jsonify(get_projector_data())


@app.route('/api/stations')
def api_stations():
    return jsonify(get_stations())


@app.route('/api/run/penalties/<int:run_id>')
@login_required
def run_penalties(run_id):
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM penalties WHERE run_id=? ORDER BY id', (run_id,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/meta')
def api_meta():
    return jsonify({'minigames': MINIGAMES, 'popup_at': MINIGAME_POPUP_AT,
                    'categories': CATEGORIES, 'dsi_brackets': DSI_BRACKETS,
                    'penalties': get_settings(), 'config': get_config(),
                    'stations': STATIONS, 'max_attempts': MAX_ATTEMPTS})


if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5001, debug=True, allow_unsafe_werkzeug=True)
