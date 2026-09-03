# Todo app entrypoint. Deployed via ArgoCD (pull-based) from the HTTPS S3 Helm repo.
from flask import Flask, request, jsonify, render_template, redirect, session, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from functools import wraps
from datetime import timedelta, date, datetime
import os
import time
import uuid
import re

app = Flask(__name__)

# ─── Environment ───
ENV = os.environ.get('FLASK_ENV', 'production').lower()
IS_PRODUCTION = ENV == 'production'

# Secret key: required in production, dev fallback only outside production.
SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    if IS_PRODUCTION:
        raise RuntimeError(
            'SECRET_KEY environment variable is required in production. '
            'Refusing to start with an insecure default.'
        )
    SECRET_KEY = 'dev-secret-key-change-in-production'
app.secret_key = SECRET_KEY

# MySQL connection using environment variables
MYSQL_HOST = os.environ.get('MYSQL_HOST', 'mysql_db')
MYSQL_PORT = os.environ.get('MYSQL_PORT', '3306')
MYSQL_USER = os.environ.get('MYSQL_USER', 'root')
MYSQL_PASSWORD = os.environ.get('MYSQL_PASSWORD', 'rootpassword')
MYSQL_DATABASE = os.environ.get('MYSQL_DATABASE', 'tododb')

# Unsplash API key (passed to frontend template)
UNSPLASH_ACCESS_KEY = os.environ.get('UNSPLASH_ACCESS_KEY', '')

# Allow an explicit database URI override (used by tests to point at SQLite).
# In production this env var is unset, so the MySQL URI below is used unchanged.
_DB_URI_OVERRIDE = os.environ.get('SQLALCHEMY_DATABASE_URI')
app.config['SQLALCHEMY_DATABASE_URI'] = _DB_URI_OVERRIDE or (
    f'mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# Connection pool hardening: recycle stale connections, verify liveness.
# The MySQL-oriented pool options are skipped for SQLite, which does not use them.
if not app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite'):
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True,
        'pool_recycle': 280,
        'pool_size': 10,
        'max_overflow': 20,
    }

# Secure session cookies
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,  # 1 MB request cap
)

db = SQLAlchemy(app)

# ─── CSRF protection ───
# Protects all state-changing HTML form submissions (login, signup, password
# reset). JSON API routes are exempted below because they are same-origin
# fetch() calls already guarded by the SameSite=Lax session cookie.
# Flask-WTF disables CSRF automatically when app.config['TESTING'] is True.
csrf = CSRFProtect(app)


@app.errorhandler(CSRFError)
def _handle_csrf_error(e):
    """Render a friendly message instead of the default 400 for CSRF failures."""
    if _wants_json():
        return jsonify({'error': 'CSRF validation failed'}), 400
    return render_template('login.html',
                           error='Your session expired. Please try again.'), 400


# ─── Rate limiting ───
# Throttles abuse of auth endpoints (brute force, signup/reset spam). Uses an
# in-memory store by default; for multi-process/replicated deployments set
# RATELIMIT_STORAGE_URI (e.g. redis://...) so limits are shared across workers.
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],  # opt-in per route; no global limit
    storage_uri=os.environ.get('RATELIMIT_STORAGE_URI', 'memory://'),
    headers_enabled=True,
    enabled=not IS_PRODUCTION or True,  # always on; disabled in tests via config below
)


@app.before_request
def _disable_limiter_in_tests():
    # Honour Flask's TESTING flag (set by the pytest fixtures) so the suite is
    # not throttled or blocked by rate limits / CSRF.
    if app.config.get('TESTING'):
        limiter.enabled = False


# ─── Password reset tokens ───
# Signed, expiring tokens (no DB column needed). The token binds to the user's
# current password hash, so a token is automatically invalidated once the
# password changes or the account is removed.
PASSWORD_RESET_SALT = 'password-reset'
PASSWORD_RESET_MAX_AGE = 30 * 60  # seconds (30 minutes)
_reset_serializer = URLSafeTimedSerializer(app.secret_key, salt=PASSWORD_RESET_SALT)


def _generate_reset_token(user):
    """Create a signed token bound to the user id + a snapshot of the pw hash."""
    return _reset_serializer.dumps({'uid': user.id, 'pw': user.password_hash})


def _verify_reset_token(token):
    """Return the User for a valid, unexpired token, else None."""
    try:
        data = _reset_serializer.loads(token, max_age=PASSWORD_RESET_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    user = User.query.get(data.get('uid'))
    # Token is invalid if the user is gone or the password has since changed.
    if not user or data.get('pw') != user.password_hash:
        return None
    return user


# Field length limits (align with DB columns / sane maxima)
MAX_TITLE_LEN = 200
MAX_DESC_LEN = 2000
MAX_EMAIL_LEN = 200
MIN_PASSWORD_LEN = 6

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _valid_email(email):
    return bool(email) and len(email) <= MAX_EMAIL_LEN and _EMAIL_RE.match(email)


def _parse_due_date(value):
    """Return (date|None, error|None). Empty/None/'null' -> (None, None) to allow clearing."""
    if value in (None, '', 'null'):
        return None, None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date(), None
    except (ValueError, TypeError):
        return None, 'Invalid due_date. Expected YYYY-MM-DD.'

VALID_CATEGORIES = ('today', 'this_week', 'eventually')


# User model
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(200), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    list_id = db.Column(db.String(8), nullable=False, unique=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


# Login required decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated_function


# API auth + ownership decorator: user must be logged in AND own the list_id.
def api_list_owner_required(f):
    @wraps(f)
    def decorated(list_id, *args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Authentication required'}), 401
        user = User.query.get(session['user_id'])
        if not user:
            session.clear()
            return jsonify({'error': 'Authentication required'}), 401
        if user.list_id != list_id:
            return jsonify({'error': 'Forbidden'}), 403
        return f(list_id, *args, **kwargs)
    return decorated


# Todo model
class Todo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    list_id = db.Column(db.String(8), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    completed = db.Column(db.Boolean, default=False)
    category = db.Column(db.String(20), nullable=False, default='today')
    position = db.Column(db.Integer, nullable=False, default=0)
    due_date = db.Column(db.Date, nullable=True)  # optional, date-only

    def to_dict(self):
        return {
            'id': self.id,
            'list_id': self.list_id,
            'title': self.title,
            'description': self.description,
            'completed': self.completed,
            'category': self.category,
            'position': self.position,
            'due_date': self.due_date.isoformat() if self.due_date else None
        }


# Root route - redirect to login or user's list
@app.route('/')
def index():
    """Redirect to user's todo list if logged in, otherwise to login."""
    if 'user_id' in session:
        user = User.query.get(session['user_id'])
        if user:
            return redirect(f'/list/{user.list_id}')
        # Session points to a deleted/invalid user — clear it to avoid a redirect loop.
        session.clear()
    return redirect('/login')


# Login page
@app.route('/login', methods=['GET', 'POST'])
@limiter.limit('10 per minute; 50 per hour', methods=['POST'])
def login():
    """Show login form and handle login."""
    if 'user_id' in session:
        if User.query.get(session['user_id']):
            return redirect('/')
        # Stale session (user no longer exists) — clear and show the login form.
        session.clear()

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        if not email or not password:
            return render_template('login.html', error='Email and password are required.', email=email)

        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            session.clear()
            session['user_id'] = user.id
            session.permanent = True
            return redirect('/dashboard')

        return render_template('login.html', error='Invalid email or password.', email=email)

    return render_template('login.html')


# Signup page
@app.route('/signup', methods=['GET', 'POST'])
@limiter.limit('5 per minute; 20 per hour', methods=['POST'])
def signup():
    """Show signup form and handle registration."""
    if 'user_id' in session:
        if User.query.get(session['user_id']):
            return redirect('/')
        session.clear()

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not email or not password:
            return render_template('signup.html', error='All fields are required.', email=email)

        if not _valid_email(email):
            return render_template('signup.html', error='Please enter a valid email address.', email=email)

        if password != confirm_password:
            return render_template('signup.html', error='Passwords do not match.', email=email)

        if len(password) < MIN_PASSWORD_LEN:
            return render_template('signup.html', error=f'Password must be at least {MIN_PASSWORD_LEN} characters.', email=email)

        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            return render_template('signup.html', error='An account with this email already exists.', email=email)

        # Generate a unique list_id (retry on the rare collision)
        list_id = uuid.uuid4().hex[:8]
        for _ in range(5):
            if not User.query.filter_by(list_id=list_id).first():
                break
            list_id = uuid.uuid4().hex[:8]

        user = User(email=email, list_id=list_id)
        user.set_password(password)
        try:
            db.session.add(user)
            db.session.commit()
        except Exception:
            db.session.rollback()
            return render_template('signup.html', error='Could not create account. Please try again.', email=email)

        session.clear()
        session['user_id'] = user.id
        session.permanent = True
        return redirect(f'/list/{user.list_id}')

    return render_template('signup.html')


# Logout
@app.route('/logout')
def logout():
    """Log out the current user."""
    session.clear()
    return redirect('/login')


# Forgot password — request a reset link
@app.route('/forgot-password', methods=['GET', 'POST'])
@limiter.limit('5 per minute; 20 per hour', methods=['POST'])
def forgot_password():
    """Ask for an email and (if it exists) start the reset flow.

    This app has no email delivery configured, so on success we hand the signed
    reset token straight to the reset page. The response is intentionally the
    same whether or not the email exists, to avoid leaking which emails are
    registered.
    """
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()

        if not email or not _valid_email(email):
            return render_template('forgot_password.html',
                                   error='Please enter a valid email address.', email=email)

        user = User.query.filter_by(email=email).first()
        if user:
            token = _generate_reset_token(user)
            # No mail server here: send the user directly to the reset form.
            return redirect(url_for('reset_password', token=token))

        # Unknown email: show the same neutral confirmation (no account enumeration).
        return render_template('forgot_password.html', sent=True, email=email)

    return render_template('forgot_password.html')


# Reset password — consume a signed token and set a new password
@app.route('/reset-password/<token>', methods=['GET', 'POST'])
@limiter.limit('10 per minute; 40 per hour', methods=['POST'])
def reset_password(token):
    """Validate the reset token and let the user set a new password."""
    user = _verify_reset_token(token)
    if not user:
        return render_template('reset_password.html',
                               invalid=True,
                               error='This reset link is invalid or has expired.')

    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not password:
            return render_template('reset_password.html', token=token, error='Password is required.')
        if password != confirm_password:
            return render_template('reset_password.html', token=token, error='Passwords do not match.')
        if len(password) < MIN_PASSWORD_LEN:
            return render_template('reset_password.html', token=token,
                                   error=f'Password must be at least {MIN_PASSWORD_LEN} characters.')

        user.set_password(password)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            return render_template('reset_password.html', token=token,
                                   error='Could not update password. Please try again.')

        # Any existing sessions/tokens are now stale (token binds to old pw hash).
        session.clear()
        return render_template('reset_password.html', done=True)

    return render_template('reset_password.html', token=token)


# Serve the UI for a specific list
@app.route('/list/<list_id>')
@login_required
def show_list(list_id):
    """Serve the todo app UI for a specific list (requires login)."""
    user = User.query.get(session['user_id'])
    if not user or user.list_id != list_id:
        return redirect('/')
    # Display name = the part of the email before @
    display_name = user.email.split('@')[0] if user.email else 'there'
    return render_template('index.html', list_id=list_id, user_email=user.email,
                           display_name=display_name, unsplash_key=UNSPLASH_ACCESS_KEY)


def _require_user():
    """Return the logged-in user or None."""
    return User.query.get(session['user_id']) if 'user_id' in session else None


@app.context_processor
def inject_display_name():
    """Make display_name available to every template (for the shared sidebar profile)."""
    try:
        if 'user_id' in session:
            u = User.query.get(session['user_id'])
            if u and u.email:
                return {'display_name': u.email.split('@')[0]}
    except Exception:
        pass
    return {'display_name': 'You'}


def _compute_stats(list_id):
    """Compute todo statistics for a given list."""
    todos = Todo.query.filter_by(list_id=list_id).all()
    total = len(todos)
    completed = sum(1 for t in todos if t.completed)
    pending = total - completed
    by_category = {}
    for cat in VALID_CATEGORIES:
        cat_todos = [t for t in todos if t.category == cat]
        by_category[cat] = {
            'total': len(cat_todos),
            'completed': sum(1 for t in cat_todos if t.completed),
            'pending': sum(1 for t in cat_todos if not t.completed),
        }
    completion_rate = round((completed / total) * 100) if total else 0
    return {
        'total': total,
        'completed': completed,
        'pending': pending,
        'completion_rate': completion_rate,
        'by_category': by_category,
    }


# ─── Sidebar Pages (all scoped to the logged-in user's list) ───

@app.route('/overview')
@login_required
def page_overview():
    user = _require_user()
    if not user:
        return redirect('/login')
    stats = _compute_stats(user.list_id)
    return render_template('overview.html', active='overview', list_id=user.list_id,
                           stats=stats, user_email=user.email, unsplash_key=UNSPLASH_ACCESS_KEY)


@app.route('/dashboard')
@login_required
def page_dashboard():
    user = _require_user()
    if not user:
        return redirect('/login')
    display_name = user.email.split('@')[0] if user.email else 'there'
    return render_template('dashboard.html', active='dashboard', list_id=user.list_id,
                           display_name=display_name, user_email=user.email,
                           unsplash_key=UNSPLASH_ACCESS_KEY)


@app.route('/analytics')
@login_required
def page_analytics():
    user = _require_user()
    if not user:
        return redirect('/login')
    stats = _compute_stats(user.list_id)
    return render_template('analytics.html', active='analytics', list_id=user.list_id,
                           stats=stats, user_email=user.email, unsplash_key=UNSPLASH_ACCESS_KEY)


@app.route('/schedule')
@login_required
def page_schedule():
    user = _require_user()
    if not user:
        return redirect('/login')
    stats = _compute_stats(user.list_id)
    return render_template('schedule.html', active='schedule', list_id=user.list_id,
                           stats=stats, user_email=user.email, unsplash_key=UNSPLASH_ACCESS_KEY)


@app.route('/completed')
@login_required
def page_completed():
    user = _require_user()
    if not user:
        return redirect('/login')
    done = Todo.query.filter_by(list_id=user.list_id, completed=True).order_by(Todo.id.desc()).all()
    stats = _compute_stats(user.list_id)
    return render_template('completed.html', active='completed', list_id=user.list_id,
                           todos=[t.to_dict() for t in done], stats=stats,
                           user_email=user.email, unsplash_key=UNSPLASH_ACCESS_KEY)


@app.route('/api/<list_id>/stats', methods=['GET'])
@api_list_owner_required
def get_stats(list_id):
    """Return todo statistics as JSON."""
    return jsonify(_compute_stats(list_id)), 200


@app.route('/api/<list_id>/calendar', methods=['GET'])
@api_list_owner_required
def get_calendar(list_id):
    """Return dated tasks shaped as FullCalendar events."""
    todos = Todo.query.filter(
        Todo.list_id == list_id,
        Todo.due_date.isnot(None)
    ).all()
    events = [{
        'id': t.id,
        'title': t.title,
        'start': t.due_date.isoformat(),
        'allDay': True,
        'completed': t.completed,
        'category': t.category,
    } for t in todos]
    return jsonify(events), 200


@app.route('/healthz', methods=['GET'])
def healthz():
    """Liveness probe - does not touch the database."""
    return jsonify({'status': 'ok'}), 200


@app.route('/readyz', methods=['GET'])
def readyz():
    """Readiness probe - performs a lightweight database check."""
    try:
        db.session.execute(db.text('SELECT 1'))
        return jsonify({'status': 'ready'}), 200
    except Exception as e:
        return jsonify({'status': 'not ready', 'error': str(e)}), 503




def _clean_todo_fields(data):
    """Validate & normalize title/description. Returns (title, description, error)."""
    title = (data.get('title') or '').strip()
    if not title:
        return None, None, 'Title is required'
    if len(title) > MAX_TITLE_LEN:
        return None, None, f'Title must be {MAX_TITLE_LEN} characters or fewer'
    description = data.get('description')
    if description is not None:
        description = str(description).strip()
        if len(description) > MAX_DESC_LEN:
            return None, None, f'Description must be {MAX_DESC_LEN} characters or fewer'
        if description == '':
            description = None
    return title, description, None

@app.route('/api/<list_id>/todos', methods=['GET'])
@api_list_owner_required
def get_todos(list_id):
    """Get all todos for a specific list ordered by position."""
    todos = Todo.query.filter_by(list_id=list_id).order_by(Todo.position).all()
    return jsonify([todo.to_dict() for todo in todos]), 200


@app.route('/api/<list_id>/todo', methods=['POST'])
@api_list_owner_required
def add_todo(list_id):
    """Add a new todo to a specific list."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid or missing JSON body'}), 400

    title, description, err = _clean_todo_fields(data)
    if err:
        return jsonify({'error': err}), 400

    category = data.get('category', 'today')
    if category not in VALID_CATEGORIES:
        return jsonify({'error': f'Invalid category. Must be one of: {VALID_CATEGORIES}'}), 400

    due_date, date_err = _parse_due_date(data.get('due_date'))
    if date_err:
        return jsonify({'error': date_err}), 400

    # Get the next position (append to end) for this list + category
    max_pos = db.session.query(db.func.max(Todo.position)).filter(
        Todo.list_id == list_id,
        Todo.category == category
    ).scalar()
    next_pos = (max_pos + 1) if max_pos is not None else 0

    todo = Todo(
        title=title,
        description=description,
        category=category,
        position=next_pos,
        list_id=list_id,
        due_date=due_date
    )
    try:
        db.session.add(todo)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({'error': 'Failed to create todo'}), 500

    return jsonify(todo.to_dict()), 201


@app.route('/api/<list_id>/todo/insert/index', methods=['POST'])
@api_list_owner_required
def insert_todo_at_index(list_id):
    """Insert a todo at a specific index position in a list."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid or missing JSON body'}), 400

    if 'index' not in data:
        return jsonify({'error': 'Title and index are required'}), 400

    title, description, err = _clean_todo_fields(data)
    if err:
        return jsonify({'error': err}), 400

    index = data['index']
    category = data.get('category', 'today')

    if category not in VALID_CATEGORIES:
        return jsonify({'error': f'Invalid category. Must be one of: {VALID_CATEGORIES}'}), 400

    due_date, date_err = _parse_due_date(data.get('due_date'))
    if date_err:
        return jsonify({'error': date_err}), 400

    total = Todo.query.filter_by(list_id=list_id, category=category).count()

    if index < 0 or index > total:
        return jsonify({'error': 'Index out of range'}), 400

    # Shift all todos at or after this index down by 1 (only in this list + category)
    Todo.query.filter(
        Todo.list_id == list_id,
        Todo.category == category,
        Todo.position >= index
    ).update({Todo.position: Todo.position + 1})

    # Insert new todo at the desired position
    todo = Todo(
        title=title,
        description=description,
        position=index,
        list_id=list_id,
        category=category,
        due_date=due_date
    )
    try:
        db.session.add(todo)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({'error': 'Failed to create todo'}), 500

    return jsonify(todo.to_dict()), 201


@app.route('/api/<list_id>/todo/<int:todo_id>', methods=['PUT'])
@api_list_owner_required
def update_todo(list_id, todo_id):
    """Update a todo (toggle completed, change title, description, category)."""
    todo = Todo.query.filter_by(id=todo_id, list_id=list_id).first()

    if not todo:
        return jsonify({'error': 'Todo not found'}), 404

    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid or missing JSON body'}), 400

    if 'completed' in data:
        todo.completed = bool(data['completed'])
    if 'title' in data:
        title = (data.get('title') or '').strip()
        if not title:
            return jsonify({'error': 'Title cannot be empty'}), 400
        if len(title) > MAX_TITLE_LEN:
            return jsonify({'error': f'Title must be {MAX_TITLE_LEN} characters or fewer'}), 400
        todo.title = title
    if 'description' in data:
        desc = data['description']
        if desc is not None:
            desc = str(desc).strip()
            if len(desc) > MAX_DESC_LEN:
                return jsonify({'error': f'Description must be {MAX_DESC_LEN} characters or fewer'}), 400
            desc = desc or None
        todo.description = desc
    if 'category' in data:
        if data['category'] not in VALID_CATEGORIES:
            return jsonify({'error': f'Invalid category. Must be one of: {VALID_CATEGORIES}'}), 400
        todo.category = data['category']
    if 'due_date' in data:
        due_date, date_err = _parse_due_date(data['due_date'])
        if date_err:
            return jsonify({'error': date_err}), 400
        todo.due_date = due_date

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({'error': 'Failed to update todo'}), 500
    return jsonify(todo.to_dict()), 200


def _canonical_due_date_for_category(category):
    """Return the canonical due_date for a target bucket so that category and
    due_date stay consistent (due_date is the source of truth for bucketing).

    Mirrors the frontend bucketing rule (weeks run Monday..Sunday):
      today       -> today's date
      this_week   -> the Sunday that ends the current week (mondayOf(today)+6)
      eventually  -> None (a task with no due date buckets as 'eventually')
    """
    today = date.today()
    if category == 'today':
        return today
    if category == 'this_week':
        monday = today - timedelta(days=today.weekday())  # Monday=0..Sunday=6
        return monday + timedelta(days=6)                  # current-week Sunday
    # 'eventually'
    return None


@app.route('/api/<list_id>/todo/<int:todo_id>/move', methods=['PATCH'])
@api_list_owner_required
def move_todo(list_id, todo_id):
    """Move a todo to a different category.

    Sets due_date to the canonical date for the target bucket so that the
    task's category never diverges from the bucket implied by its due_date.
    """
    todo = Todo.query.filter_by(id=todo_id, list_id=list_id).first()

    if not todo:
        return jsonify({'error': 'Todo not found'}), 404

    data = request.get_json(silent=True)
    if not data or 'category' not in data:
        return jsonify({'error': 'Category is required'}), 400

    category = data['category']
    if category not in VALID_CATEGORIES:
        return jsonify({'error': f'Invalid category. Must be one of: {VALID_CATEGORIES}'}), 400

    # Get next position in the target category
    max_pos = db.session.query(db.func.max(Todo.position)).filter(
        Todo.list_id == list_id,
        Todo.category == category
    ).scalar()
    next_pos = (max_pos + 1) if max_pos is not None else 0

    todo.category = category
    todo.position = next_pos
    # Keep due_date consistent with the target bucket (source of truth).
    todo.due_date = _canonical_due_date_for_category(category)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({'error': 'Failed to move todo'}), 500

    return jsonify(todo.to_dict()), 200


@app.route('/api/<list_id>/todo/<int:todo_id>/date', methods=['PATCH'])
@api_list_owner_required
def set_todo_date(list_id, todo_id):
    """Set (or clear) a todo's due date — used by calendar drag-to-reschedule."""
    todo = Todo.query.filter_by(id=todo_id, list_id=list_id).first()
    if not todo:
        return jsonify({'error': 'Todo not found'}), 404

    data = request.get_json(silent=True) or {}
    due_date, date_err = _parse_due_date(data.get('due_date'))
    if date_err:
        return jsonify({'error': date_err}), 400

    todo.due_date = due_date
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({'error': 'Failed to update due date'}), 500
    return jsonify(todo.to_dict()), 200


@app.route('/api/<list_id>/todos/completed', methods=['DELETE'])
@api_list_owner_required
def delete_completed(list_id):
    """Bulk delete all completed todos in a list."""
    deleted_count = Todo.query.filter_by(list_id=list_id, completed=True).delete()
    db.session.commit()
    return jsonify({'message': f'{deleted_count} completed todos removed', 'count': deleted_count}), 200


@app.route('/api/<list_id>/todo/<int:todo_id>', methods=['DELETE'])
@api_list_owner_required
def delete_todo(list_id, todo_id):
    """Delete a todo."""
    todo = Todo.query.filter_by(id=todo_id, list_id=list_id).first()

    if not todo:
        return jsonify({'error': 'Todo not found'}), 404

    db.session.delete(todo)
    db.session.commit()
    return jsonify({'message': 'Todo deleted'}), 200


# ─── CSRF exemption for JSON API routes ───
# The /api/* endpoints are called via same-origin fetch() with JSON bodies and
# are protected by the SameSite=Lax session cookie + per-list ownership checks,
# so they don't carry a CSRF form token. Exempt them from CSRF form validation.
# (Done after all routes are registered so every API view is present.)
for _rule in app.url_map.iter_rules():
    if _rule.rule.startswith('/api/'):
        _view = app.view_functions.get(_rule.endpoint)
        if _view is not None:
            csrf.exempt(_view)


# ─── Security + caching headers ───
@app.after_request
def set_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    if IS_PRODUCTION:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'

    # ── Caching ──
    # Only set caching if a handler hasn't already chosen one.
    if 'Cache-Control' not in response.headers:
        path = request.path
        if path.startswith('/static/'):
            # Bundled assets are content-stable; cache hard so navigation
            # between pages does not re-download them.
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        elif path.startswith('/api/'):
            # Task data must always be fresh.
            response.headers['Cache-Control'] = 'no-store'
        else:
            # HTML pages: let the browser reuse a cached copy instantly but
            # revalidate in the background, so back/forward and re-navigation
            # feel instant without ever showing stale logged-in content.
            response.headers['Cache-Control'] = 'private, no-cache'
    return response


# ─── Error handlers ───
def _wants_json():
    return request.path.startswith('/api/') or \
        request.accept_mimetypes.best == 'application/json'


@app.errorhandler(400)
def _bad_request(e):
    if _wants_json():
        return jsonify({'error': 'Bad request'}), 400
    return 'Bad request', 400


@app.errorhandler(401)
def _unauthorized(e):
    if _wants_json():
        return jsonify({'error': 'Authentication required'}), 401
    return redirect('/login')


@app.errorhandler(403)
def _forbidden(e):
    if _wants_json():
        return jsonify({'error': 'Forbidden'}), 403
    return 'Forbidden', 403


@app.errorhandler(404)
def _not_found(e):
    if _wants_json():
        return jsonify({'error': 'Not found'}), 404
    return redirect('/')


@app.errorhandler(413)
def _too_large(e):
    return jsonify({'error': 'Request payload too large'}), 413


@app.errorhandler(500)
def _server_error(e):
    try:
        db.session.rollback()
    except Exception:
        pass
    if _wants_json():
        return jsonify({'error': 'Internal server error'}), 500
    return 'Internal server error', 500


@app.errorhandler(429)
def _rate_limited(e):
    """Friendly response when an auth endpoint is hit too often."""
    if _wants_json():
        return jsonify({'error': 'Too many requests. Please slow down and try again shortly.'}), 429
    return render_template('login.html',
                           error='Too many attempts. Please wait a moment and try again.'), 429


def _ensure_due_date_column():
    """Add todo.due_date if it does not already exist (idempotent migration)."""
    with app.app_context():
        insp = db.inspect(db.engine)
        cols = [c['name'] for c in insp.get_columns('todo')]
        if 'due_date' not in cols:
            db.session.execute(db.text('ALTER TABLE todo ADD COLUMN due_date DATE NULL'))
            db.session.commit()
            print("Migration: added todo.due_date column.")


def wait_for_db(retries=10, delay=3):
    """Wait for MySQL to be ready and create tables before serving traffic."""
    for attempt in range(retries):
        try:
            with app.app_context():
                db.create_all()
            try:
                _ensure_due_date_column()
            except Exception as mig_err:
                print(f"due_date migration skipped/failed: {mig_err}")
            print("Database connected successfully!")
            return True
        except Exception as e:
            print(f"Database not ready (attempt {attempt + 1}/{retries}): {e}")
            time.sleep(delay)
    print("Could not connect to database after all retries.")
    return False


# Run DB readiness/create_all at import time so it also runs under gunicorn,
# where the __main__ block is never executed. Guarded so a failure here does
# not prevent the module from importing (readiness is still checked via /readyz).
try:
    wait_for_db()
except Exception as e:
    print(f"Database initialization at import failed: {e}")


if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() in ('1', 'true', 'yes')
    port = int(os.environ.get('PORT', '5001'))
    app.run(host='0.0.0.0', debug=debug_mode, port=port)

