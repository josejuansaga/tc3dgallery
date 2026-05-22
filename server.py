from http.server import HTTPServer, ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import unquote, urlparse, parse_qs
import os
import json
import re
import sys
import base64
import html
import hmac
import mimetypes
import subprocess
import threading
import hashlib, secrets, io, zipfile
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Config from environment (defaults = Windows local dev) ────────────────────
GALLERY_DIR   = os.environ.get('GALLERY_DIR',   BASE_DIR)
DATA_DIR      = os.environ.get('DATA_DIR',      GALLERY_DIR)
TRABAJOS_DIR  = os.environ.get('TRABAJOS_DIR',  r"Z:\OneDriveTC3D\Trabajos")
TRABAJOS_DIR2 = os.environ.get('TRABAJOS_DIR2', '')
PORT         = int(os.environ.get('PORT', 8765))
PASSWORD     = os.environ.get('GALLERY_PASSWORD', '')   # vacío = sin autenticación
OPEN_BROWSER = '--open' in sys.argv                     # abrir navegador solo con flag
MEDIA_HASH_SECRET = os.environ.get(
    'MEDIA_HASH_SECRET',
    f'{GALLERY_DIR}|{TRABAJOS_DIR}|{TRABAJOS_DIR2 or ""}'
)

USERS_FILE = os.path.join(DATA_DIR, 'users.json')
SHARED_LINKS_FILE = os.path.join(DATA_DIR, 'shared_links.json')
ROTATIONS_FILE = os.path.join(DATA_DIR, 'image_rotations.json')
PROJECT_BILLING_FILE = os.path.join(DATA_DIR, 'project_billing.json')
SOCIAL_VISIBILITY_FILE = os.path.join(DATA_DIR, 'social_visibility.json')
FAVORITES_FILE = os.path.join(DATA_DIR, 'favorites.json')
THUMB_DIR  = os.path.join(DATA_DIR, 'thumbs')
_sessions      = {}   # token → {username, role, expires}
_gallery_cache = {'data': None, 'mtime': 0}

# ── Export state (one export at a time) ────────────────────────────────────────
export_lock   = threading.Lock()
export_status = {'running': False, 'done': 0, 'total': 0, 'errors': [], 'output_dir': ''}

os.makedirs(DATA_DIR, exist_ok=True)


def url_to_filepath(url):
    """Convert a gallery image URL (relative or absolute) to a filesystem path."""
    if url.startswith('/trabajos2/') and TRABAJOS_DIR2:
        rel = unquote(url[len('/trabajos2/'):]).replace('/', os.sep)
        return os.path.join(TRABAJOS_DIR2, rel)
    # Relative: /trabajos/...  — used by Docker/NAS builds
    if url.startswith('/trabajos/'):
        rel = unquote(url[len('/trabajos/'):]).replace('/', os.sep)
    # Absolute: http://*/trabajos/...  — used by legacy local builds
    elif '/trabajos/' in url:
        idx = url.index('/trabajos/')
        rel = unquote(url[idx + len('/trabajos/'):]).replace('/', os.sep)
    else:
        return None
    return os.path.join(TRABAJOS_DIR, rel)


def clean_name(s):
    """Strip leading project number and sanitise for filenames."""
    s = re.sub(r'^\d+\s*[-–]\s*', '', s).strip()
    return re.sub(r'[\\/*?:"<>|]', '_', s)

INSTAGRAM_FORMATS = {
    'square':   (1080, 1080, 'Post cuadrado'),
    'portrait': (1080, 1350, 'Post vertical'),
    'story':    (1080, 1920, 'Story/Reel cover'),
}


# ── Thumbnail cache ───────────────────────────────────────────────────────────
_thumb_lock = threading.Lock()

def get_or_create_thumb(src_url, width):
    """Return path to a cached JPEG thumbnail (width px on longest side).
    Creates it on first request using Pillow. Returns None on any error."""
    fp = url_to_filepath(src_url)
    if not fp or not os.path.isfile(fp):
        return None

    os.makedirs(THUMB_DIR, exist_ok=True)

    key        = hashlib.md5(f'{fp}:{width}'.encode()).hexdigest()
    thumb_path = os.path.join(THUMB_DIR, f'{key}.jpg')

    # Return cached thumb if it's newer than the source
    with _thumb_lock:
        if os.path.isfile(thumb_path):
            try:
                if os.path.getmtime(thumb_path) >= os.path.getmtime(fp):
                    return thumb_path
            except OSError:
                pass

        try:
            from PIL import Image
            img = Image.open(fp)
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            w, h = img.size
            longest = max(w, h)
            if longest > width:
                scale = width / longest
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            img.save(thumb_path, 'JPEG', quality=75, optimize=True)
            return thumb_path
        except Exception as e:
            print(f'[thumb] Error: {src_url} -> {e}', flush=True)
            return None


# ── Users & Sessions ──────────────────────────────────────────────────────────
def file_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0

def password_is_hashed(value):
    return isinstance(value, str) and value.startswith('pbkdf2_sha256$')

def hash_password(password, salt=None, iterations=260000):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), iterations)
    encoded = base64.urlsafe_b64encode(digest).decode('ascii')
    return f'pbkdf2_sha256${iterations}${salt}${encoded}'

def verify_password(password, stored):
    if password_is_hashed(stored):
        try:
            _, iter_raw, salt, encoded = stored.split('$', 3)
            expected = base64.urlsafe_b64decode(encoded.encode('ascii'))
            digest = hashlib.pbkdf2_hmac(
                'sha256',
                password.encode('utf-8'),
                salt.encode('utf-8'),
                int(iter_raw),
            )
            return secrets.compare_digest(digest, expected)
        except Exception:
            return False
    return stored == password

def normalize_user_record(username, raw):
    raw = raw if isinstance(raw, dict) else {}
    password = str(raw.get('password') or '')
    changed = False
    if password and not password_is_hashed(password):
        password = hash_password(password)
        changed = True

    role = str(raw.get('role') or 'client').strip()
    if role not in ('admin', 'client', 'social'):
        role = 'client'
        changed = True

    user = {
        'password': password,
        'role': role,
        'name': str(raw.get('name') or username).strip() or username,
    }
    logo_url = str(raw.get('logo_url') or '').strip()
    if logo_url:
        user['logo_url'] = logo_url
    if role in ('client', 'social'):
        companies = []
        raw_companies = raw.get('companies', []) if isinstance(raw.get('companies'), list) else []
        for company in raw_companies:
            company = str(company).strip()
            if company and company not in companies:
                companies.append(company)
        user['companies'] = companies
    return user, changed or user != raw

def sanitize_users_for_admin(users):
    sanitized = {}
    for username, user in users.items():
        item = {k: v for k, v in user.items() if k != 'password'}
        item['has_password'] = bool(user.get('password'))
        sanitized[username] = item
    return sanitized

DEFAULT_USERS = {
    "admin": normalize_user_record("admin", {"password": "admin", "role": "admin", "name": "Administrador"})[0]
}

def load_users():
    try:
        with open(USERS_FILE, encoding='utf-8') as f:
            raw_users = json.load(f)
    except Exception:
        return DEFAULT_USERS
    users = {}
    changed = False
    for username, raw in raw_users.items():
        username = str(username).strip()
        if not username:
            changed = True
            continue
        user, item_changed = normalize_user_record(username, raw)
        users[username] = user
        changed = changed or item_changed
    if not users:
        return DEFAULT_USERS
    if changed:
        save_users(users)
    return users

def save_users(users):
    tmp = USERS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(users, f, ensure_ascii=False, indent=2)
    os.replace(tmp, USERS_FILE)

def load_shared_links():
    try:
        with open(SHARED_LINKS_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def save_shared_links(links):
    tmp = SHARED_LINKS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(links, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SHARED_LINKS_FILE)

def cleanup_shared_links(links=None):
    links = load_shared_links() if links is None else links
    return links

def load_image_rotations():
    try:
        with open(ROTATIONS_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def save_image_rotations(rotations):
    tmp = ROTATIONS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(rotations, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ROTATIONS_FILE)

def load_project_billing():
    try:
        with open(PROJECT_BILLING_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def save_project_billing(data):
    tmp = PROJECT_BILLING_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PROJECT_BILLING_FILE)

def load_social_visibility():
    try:
        with open(SOCIAL_VISIBILITY_FILE, encoding='utf-8') as f:
            raw = json.load(f)
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): bool(value) for key, value in raw.items() if value}

def save_social_visibility(data):
    tmp = SOCIAL_VISIBILITY_FILE + '.tmp'
    cleaned = {str(key): bool(value) for key, value in data.items() if value}
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SOCIAL_VISIBILITY_FILE)

def favorite_key(company, project_name):
    return f'{company}||{project_name}'

def load_favorites():
    try:
        with open(FAVORITES_FILE, encoding='utf-8') as f:
            raw = json.load(f)
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    cleaned = {}
    for username, entries in raw.items():
        if not isinstance(entries, list):
            continue
        unique = []
        seen = set()
        for entry in entries:
            entry = str(entry).strip()
            if not entry or entry in seen:
                continue
            seen.add(entry)
            unique.append(entry)
        cleaned[str(username).strip()] = unique
    return cleaned

def save_favorites(data):
    tmp = FAVORITES_FILE + '.tmp'
    cleaned = {}
    for username, entries in data.items():
        username = str(username).strip()
        if not username:
            continue
        unique = []
        seen = set()
        for entry in entries if isinstance(entries, list) else []:
            entry = str(entry).strip()
            if not entry or entry in seen:
                continue
            seen.add(entry)
            unique.append(entry)
        cleaned[username] = unique
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)
    os.replace(tmp, FAVORITES_FILE)

def favorites_for_user(username):
    return load_favorites().get(username, [])

def billing_key(company, project_name):
    return f'{company}||{project_name}'

def social_key(company, project_name):
    return f'{company}||{project_name}'

def project_image_count(project):
    return sum(len(version.get('images', [])) for version in project.get('versions', [])) + len(project.get('final_photos', []))

def iter_project_images(project):
    for version in project.get('versions', []):
        for img in version.get('images', []):
            yield img
    for img in project.get('final_photos', []):
        yield img

def normalize_billing_entry(value):
    if isinstance(value, dict):
        try:
            amount = float(value.get('amount') or 0)
        except (TypeError, ValueError):
            amount = 0.0
        paid_by = str(value.get('paid_by') or '').strip()
        if paid_by not in ('JJ', 'Noelia'):
            paid_by = ''
        return {
            'amount': round(max(0.0, amount), 2),
            'date': str(value.get('date') or '').strip(),
            'paid_by': paid_by,
            'paid': bool(value.get('paid', False)),
        }
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    return {'amount': round(max(0.0, amount), 2), 'date': '', 'paid_by': '', 'paid': False}

def rotation_key(src):
    fp = url_to_filepath(src)
    return os.path.normcase(os.path.abspath(fp)) if fp else src

def make_public_url(path):
    base = os.environ.get('PUBLIC_BASE_URL', '').rstrip('/')
    return f'{base}{path}' if base else path

def media_id_for_src(src):
    fp = url_to_filepath(src)
    basis = os.path.normcase(os.path.abspath(fp)) if fp else src
    return hmac.new(
        MEDIA_HASH_SECRET.encode('utf-8'),
        basis.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()[:32]

def clone_public_project(project, lookup):
    cloned = {k: v for k, v in project.items() if k not in ('versions', 'final_photos')}
    cloned_versions = []
    for version in project.get('versions', []):
        images = []
        for src in version.get('images', []):
            media_id = media_id_for_src(src)
            lookup[media_id] = src
            images.append(media_id)
        cloned_version = {**version, 'images': images}
        option_groups = []
        for group in version.get('option_groups', []) if isinstance(version.get('option_groups'), list) else []:
            group_images = []
            for src in group.get('images', []):
                media_id = media_id_for_src(src)
                lookup[media_id] = src
                group_images.append(media_id)
            if group_images:
                option_groups.append({
                    'label': str(group.get('label') or '').strip(),
                    'images': group_images,
                })
        if option_groups:
            cloned_version['option_groups'] = option_groups
        cloned_versions.append(cloned_version)
    final_photos = []
    for src in project.get('final_photos', []):
        media_id = media_id_for_src(src)
        lookup[media_id] = src
        final_photos.append(media_id)
    cloned['versions'] = cloned_versions
    cloned['final_photos'] = final_photos
    return cloned

def clone_public_project_summary(project, lookup):
    cloned = {k: v for k, v in project.items() if k not in ('versions', 'final_photos')}
    versions = project.get('versions', []) or []
    latest = versions[0] if versions else {}
    latest_images = latest.get('images', []) or []
    cover = ''
    if latest_images:
        src = latest_images[0]
        media_id = media_id_for_src(src)
        lookup[media_id] = src
        cover = media_id
    cloned.update({
        'cover': cover,
        'latest_version_label': latest.get('label', ''),
        'latest_image_count': len(latest_images),
        'version_count': len(versions),
        'render_count': sum(len(version.get('images', []) or []) for version in versions),
        'final_photo_count': len(project.get('final_photos', []) or []),
    })
    return cloned

def build_public_gallery_for_user(username):
    lookup = {}
    public_data = []
    for entry in filter_data_for_user(username):
        public_projects = [clone_public_project_summary(project, lookup) for project in entry.get('projects', [])]
        public_data.append({**entry, 'projects': public_projects})
    return public_data, lookup

def ensure_session_gallery_data(sess):
    gallery_stamp = file_mtime(os.path.join(GALLERY_DIR, 'data.js'))
    users_stamp = file_mtime(USERS_FILE)
    social_stamp = file_mtime(SOCIAL_VISIBILITY_FILE)
    if (
        sess.get('public_gallery_data') is None
        or sess.get('public_gallery_stamp') != gallery_stamp
        or sess.get('public_users_stamp') != users_stamp
        or sess.get('public_social_stamp') != social_stamp
    ):
        public_data, lookup = build_public_gallery_for_user(sess['username'])
        sess['public_gallery_data'] = public_data
        sess['media_lookup'] = lookup
        sess['public_gallery_stamp'] = gallery_stamp
        sess['public_users_stamp'] = users_stamp
        sess['public_social_stamp'] = social_stamp
    return sess['public_gallery_data'], sess['media_lookup']

def resolve_session_image_ref(sess, ref):
    if not ref:
        return None
    _, lookup = ensure_session_gallery_data(sess)
    if ref in lookup:
        return lookup[ref]
    if ref in lookup.values():
        return ref
    return None

def admin_payload():
    users = load_users()
    gallery = load_gallery_data()
    billing = load_project_billing()
    social_visibility = load_social_visibility()
    companies = []
    project_billing = []
    social_projects = []
    total_projects = 0
    total_images = 0
    total_billed = 0.0
    for entry in gallery:
        projects = entry.get('projects', [])
        project_count = len(projects)
        image_count = sum(project_image_count(project) for project in projects)
        companies.append({
            'name': entry.get('company', ''),
            'projects': project_count,
            'images': image_count,
        })
        for project in projects:
            item = normalize_billing_entry(billing.get(billing_key(entry.get('company', ''), project.get('name', '')), {}))
            total_billed += item['amount']
            project_billing.append({
                'company': entry.get('company', ''),
                'project': project.get('name', ''),
                'name': clean_name(project.get('name', '')),
                'date': project.get('date', ''),
                **item,
            })
            if social_visibility.get(social_key(entry.get('company', ''), project.get('name', ''))):
                social_projects.append({
                    'company': entry.get('company', ''),
                    'project': project.get('name', ''),
                    'name': clean_name(project.get('name', '')),
                })
        total_projects += project_count
        total_images += image_count
    companies.sort(key=lambda c: c['name'].lower())
    links = []
    now = datetime.now()
    for token, link in load_shared_links().items():
        expires_raw = link.get('expires_at', '')
        expired = True
        try:
            expired = datetime.fromisoformat(expires_raw) <= now
        except ValueError:
            pass
        links.append({
            'token': token,
            'url': make_public_url(f'/s/{token}'),
            'company': link.get('company', ''),
            'project': link.get('project', ''),
            'title': link.get('title', ''),
            'created_at': link.get('created_at', ''),
            'expires_at': expires_raw,
            'expired': expired,
            'watermark': bool(link.get('watermark')),
            'images': len(link.get('images') or []),
            'visits': int(link.get('visits', 0) or 0),
            'last_visit': link.get('last_visit', ''),
        })
    links.sort(key=lambda l: l.get('created_at') or '', reverse=True)
    return {
        'users': sanitize_users_for_admin(users),
        'companies': companies,
        'shared_links': links,
        'stats': {
            'companies': len(companies),
            'projects': total_projects,
            'images': total_images,
            'billed': total_billed,
        },
        'project_billing': sorted(project_billing, key=lambda p: (p.get('company','').lower(), p.get('name','').lower())),
        'social_projects': sorted(social_projects, key=lambda p: (p.get('company','').lower(), p.get('name','').lower())),
        'reindex': _reindex_log,
    }

def load_gallery_data():
    path = os.path.join(GALLERY_DIR, 'data.js')
    try:
        mtime = os.path.getmtime(path)
        if _gallery_cache['data'] is not None and mtime == _gallery_cache['mtime']:
            return _gallery_cache['data']
        with open(path, encoding='utf-8') as f:
            content = f.read().strip()
        content = re.sub(r'^const\s+GALLERY_DATA\s*=\s*', '', content).rstrip(';').strip()
        _gallery_cache['data']  = json.loads(content)
        _gallery_cache['mtime'] = mtime
        return _gallery_cache['data']
    except Exception as e:
        print(f'[data] Error: {e}', flush=True)
        return []

def filter_data_for_user(username):
    users   = load_users()
    user    = users.get(username, {})
    data    = load_gallery_data()
    if user.get('role') == 'admin':
        return data
    if user.get('role') == 'social':
        allowed = {c.lower() for c in user.get('companies', [])}
        social_visibility = load_social_visibility()
        filtered = []
        for entry in data:
            if entry['company'].lower() not in allowed:
                continue
            projects = [
                project for project in entry.get('projects', [])
                if social_visibility.get(social_key(entry.get('company', ''), project.get('name', '')))
            ]
            if projects:
                filtered.append({**entry, 'projects': projects})
        return filtered
    allowed = {c.lower() for c in user.get('companies', [])}
    return [e for e in data if e['company'].lower() in allowed]

def find_project_for_user(username, company, project_name):
    for entry in filter_data_for_user(username):
        if entry.get('company') != company:
            continue
        for project in entry.get('projects', []):
            if project.get('name') == project_name:
                return {**project, 'company': company}
    return None

def find_project(company, project_name):
    for entry in load_gallery_data():
        if entry.get('company') != company:
            continue
        for project in entry.get('projects', []):
            if project.get('name') == project_name:
                return {**project, 'company': company}
    return None

def create_session(username, role):
    token = secrets.token_hex(32)
    _sessions[token] = {
        'username': username,
        'role':     role,
        'expires':  datetime.now() + timedelta(hours=24),
    }
    return token

def get_session(headers):
    for part in headers.get('Cookie', '').split(';'):
        name, _, value = part.strip().partition('=')
        if name == 'gallery_session':
            sess = _sessions.get(value.strip())
            if sess and sess['expires'] > datetime.now():
                return sess, value.strip()
    return None, None


LOGIN_HTML = '''<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Acceso · Galería</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{min-height:100vh;background:#0d0d0d;display:flex;align-items:center;
     justify-content:center;font-family:"Segoe UI",system-ui,sans-serif;padding:16px}}
.box{{background:#161616;border:1px solid #2a2a2a;border-radius:12px;
     padding:40px 36px;width:100%;max-width:360px}}
.logo{{display:flex;align-items:center;gap:12px;margin-bottom:32px;justify-content:center}}
.logo svg{{width:38px;height:35px;color:#c8a96e}}
.logo-text{{display:flex;flex-direction:column;gap:2px}}
.brand{{font-size:16px;font-weight:300;color:#e8e8e8;line-height:1.1}}
.brand strong{{font-weight:800}}
.rule{{height:1px;background:#2a2a2a;margin:3px 0}}
.tagline{{font-size:9px;color:#555;letter-spacing:.06em;text-transform:uppercase}}
h2{{font-size:12px;font-weight:600;color:#666;text-align:center;margin-bottom:24px;
    text-transform:uppercase;letter-spacing:.08em}}
label{{display:block;font-size:11px;color:#666;margin-bottom:5px;text-transform:uppercase;
       letter-spacing:.05em}}
input{{width:100%;padding:10px 14px;background:#1e1e1e;border:1px solid #2a2a2a;
      border-radius:6px;color:#e8e8e8;font-size:14px;margin-bottom:16px;outline:none;
      transition:border-color .15s}}
input:focus{{border-color:#c8a96e}}
input::placeholder{{color:#444}}
button{{width:100%;padding:12px;background:#c8a96e;border:none;border-radius:6px;
       color:#111;font-size:14px;font-weight:700;cursor:pointer;margin-top:4px;
       transition:background .15s}}
button:hover{{background:#e8c98e}}
.error{{color:#e05555;font-size:12px;text-align:center;margin-top:16px;
        background:rgba(200,80,80,.1);padding:8px;border-radius:6px}}
</style>
</head>
<body>
<div class="box">
  <div class="logo">
    <svg viewBox="0 0 100 92" fill="none" xmlns="http://www.w3.org/2000/svg">
      <polygon points="50,5 95,46 95,87 5,87 5,46" stroke="currentColor" stroke-width="9" stroke-linejoin="miter" fill="none"/>
    </svg>
    <div class="logo-text">
      <div class="brand"><strong>tu casa</strong> en 3d</div>
      <div class="rule"></div>
      <div class="tagline">diseño + infografía</div>
    </div>
  </div>
  <h2>Acceso a la galería</h2>
  <form method="post" action="/login">
    <label>Usuario</label>
    <input name="username" autocomplete="username" autofocus placeholder="usuario">
    <label>Contraseña</label>
    <input name="password" type="password" autocomplete="current-password" placeholder="••••••••">
    <button type="submit">Entrar →</button>
    {error}
  </form>
</div>
</body>
</html>'''


def build_shared_html(token, link):
    title = html.escape(link.get('title') or link.get('project') or 'Proyecto')
    company = html.escape(link.get('company') or '')
    logo_url = html.escape(link.get('logo_url') or '')
    images = link.get('images') or []
    option_groups = link.get('option_groups') or []
    watermark = bool(link.get('watermark'))
    expires = link.get('expires_at', '')
    try:
        expires_label = datetime.fromisoformat(expires).strftime('%d/%m/%Y')
    except ValueError:
        expires_label = ''
    img_data = json.dumps([f'/shared-image/{token}?i={i}' for i in range(len(images))], ensure_ascii=False)
    rotations = {str(k): int(v) % 360 for k, v in (link.get('rotations') or {}).items()}
    rotation_data = json.dumps(rotations, ensure_ascii=False)
    if not isinstance(option_groups, list):
        option_groups = []
    image_index = {src: i for i, src in enumerate(images)}
    used_indexes = set()
    sections = []
    for group in option_groups:
        if not isinstance(group, dict):
            continue
        label = html.escape(str(group.get('label') or '').strip())
        group_cards = []
        for src in group.get('images', []) if isinstance(group.get('images'), list) else []:
            idx = image_index.get(src)
            if idx is None or idx in used_indexes:
                continue
            used_indexes.add(idx)
            group_cards.append(
                f'''<button class="tile{' wm' if watermark else ''}" onclick="openLb({idx})">
  <img src="/shared-thumb/{token}?i={idx}&w=900" loading="lazy" alt="" style="transform:rotate({rotations.get(str(idx), 0)}deg)">
</button>'''
            )
        if group_cards:
            sections.append((label, '\n'.join(group_cards)))
    remaining_cards = []
    for i in range(len(images)):
        if i in used_indexes:
            continue
        remaining_cards.append(
            f'''<button class="tile{' wm' if watermark else ''}" onclick="openLb({i})">
  <img src="/shared-thumb/{token}?i={i}&w=900" loading="lazy" alt="" style="transform:rotate({rotations.get(str(i), 0)}deg)">
</button>'''
        )
    if remaining_cards:
        sections.append(('', '\n'.join(remaining_cards)))
    cards = '\n'.join(
        (
            f'<section class="shared-group"><div class="shared-group-title">{label}</div><div class="grid">{section_cards}</div></section>'
            if label else
            f'<section class="shared-group"><div class="grid">{section_cards}</div></section>'
        )
        for label, section_cards in sections
    )
    logo_html = f'<img class="client-logo" src="{logo_url}" alt="{company}">' if logo_url else ''
    return f'''<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · Tu casa en 3D</title>
<style>
  :root {{
    --bg:#111; --surface:#181818; --surface2:#202020; --text:#f5f2ec;
    --muted:#96928a; --border:#2e2b26; --accent:#b99a58;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:Inter,Segoe UI,Arial,sans-serif; background:var(--bg); color:var(--text); }}
  header {{ position:relative; min-height:34vh; display:flex; flex-direction:column; justify-content:flex-end; padding:34px clamp(18px,5vw,72px) 34px; border-bottom:1px solid var(--border); background:linear-gradient(180deg,#181818,#101010); }}
  .topbar {{ position:absolute; top:24px; left:clamp(18px,5vw,72px); right:clamp(18px,5vw,72px); display:flex; align-items:flex-start; justify-content:space-between; gap:24px; }}
  .brand {{ display:flex; align-items:center; gap:18px; margin-left:auto; }}
  .brand-mark {{ width:48px; height:44px; color:var(--accent); flex:0 0 auto; }}
  .brand-text {{ font-size:13px; color:var(--muted); text-transform:uppercase; letter-spacing:.08em; }}
  .client-logo {{ max-width:220px; max-height:76px; object-fit:contain; background:white; border-radius:6px; padding:10px; }}
  h1 {{ font-size:clamp(32px,6vw,72px); line-height:.95; font-weight:800; letter-spacing:0; max-width:1100px; }}
  .meta {{ margin-top:18px; color:var(--muted); font-size:14px; display:flex; gap:14px; flex-wrap:wrap; }}
  main {{ padding:22px clamp(10px,3vw,38px) 44px; }}
  .shared-group {{ margin-bottom:26px; }}
  .shared-group-title {{ margin:0 0 12px; color:var(--accent); font-size:13px; font-weight:800; letter-spacing:.08em; text-transform:uppercase; }}
  .grid {{ columns:4 280px; column-gap:12px; }}
  .tile {{ display:block; width:100%; border:0; padding:0; margin:0 0 12px; background:var(--surface2); cursor:pointer; overflow:hidden; break-inside:avoid; border-radius:6px; }}
  .tile img {{ width:100%; display:block; transition:transform .25s, opacity .25s; }}
  .tile:hover img {{ opacity:.9; }}
  .wm {{ position:relative; }}
  .wm:after {{ content:'TU CASA EN 3D'; position:absolute; right:14px; bottom:12px; color:rgba(255,255,255,.55); font-size:11px; font-weight:800; letter-spacing:.08em; text-shadow:0 1px 6px rgba(0,0,0,.65); pointer-events:none; }}
  #lb {{ display:none; position:fixed; inset:0; z-index:10; background:rgba(0,0,0,.94); align-items:center; justify-content:center; }}
  #lb.open {{ display:flex; }}
  #lb img {{ max-width:94vw; max-height:82vh; object-fit:contain; }}
  #lb.wm:after {{ content:'TU CASA EN 3D'; position:fixed; right:34px; bottom:76px; color:rgba(255,255,255,.55); font-size:13px; font-weight:800; letter-spacing:.08em; text-shadow:0 1px 6px rgba(0,0,0,.65); }}
  .lb-btn {{ position:fixed; border:0; background:rgba(255,255,255,.08); color:white; border-radius:6px; cursor:pointer; }}
  #close {{ top:18px; right:22px; width:42px; height:42px; font-size:28px; }}
  #prev,#next {{ top:50%; width:48px; height:64px; transform:translateY(-50%); font-size:42px; }}
  #prev {{ left:18px; }} #next {{ right:18px; }}
  #tools {{ position:fixed; bottom:18px; left:50%; transform:translateX(-50%); display:flex; align-items:center; gap:10px; color:var(--muted); font-size:13px; }}
  .rotate-btn {{ border:1px solid var(--border); background:rgba(255,255,255,.08); color:var(--text); border-radius:6px; padding:8px 12px; cursor:pointer; }}
  .rotate-btn:hover {{ border-color:var(--accent); color:var(--accent); }}
  .download-all {{ position:fixed; top:18px; left:22px; z-index:12; border:1px solid var(--border); color:var(--text); background:rgba(255,255,255,.08); border-radius:6px; padding:10px 14px; text-decoration:none; font-size:13px; }}
  .download-all:hover {{ border-color:var(--accent); color:var(--accent); }}
  footer {{ padding:0 clamp(18px,5vw,72px) 34px; color:var(--muted); font-size:12px; }}
  @media (max-width:720px) {{
    header {{ min-height:28vh; padding:96px 18px 24px; }}
    .topbar {{ top:18px; left:18px; right:18px; }}
    .brand-text {{ display:none; }}
    .client-logo {{ max-width:150px; max-height:58px; }}
    main {{ padding:10px 8px 28px; }}
    .grid {{ columns:2 150px; column-gap:8px; }}
    .tile {{ margin-bottom:8px; border-radius:4px; }}
    #prev,#next {{ width:38px; height:56px; font-size:34px; }}
  }}
</style>
</head>
<body>
<header>
  <div class="topbar">
    {logo_html}
    <div class="brand">
      <svg class="brand-mark" viewBox="0 0 100 92" fill="none" xmlns="http://www.w3.org/2000/svg">
        <polygon points="50,5 95,46 95,87 5,87 5,46" stroke="currentColor" stroke-width="9" stroke-linejoin="miter" fill="none"/>
      </svg>
      <div class="brand-text">tu casa en 3d · diseño + infografía</div>
    </div>
  </div>
  <h1>{title}</h1>
  <div class="meta"><span>{company}</span><span>{len(images)} imágenes</span><span>Disponible hasta {html.escape(expires_label)}</span></div>
</header>
<main>{cards}</main>
<footer>Enlace privado temporal. No requiere usuario y caduca automáticamente.</footer>
<a class="download-all" href="/shared-download/{token}">↓ Descargar todo</a>
<div id="lb" class="{'wm' if watermark else ''}" onclick="if(event.target.id==='lb') closeLb()">
  <button class="lb-btn" id="close" onclick="closeLb()">×</button>
  <button class="lb-btn" id="prev" onclick="nav(-1)">‹</button>
  <img id="lb-img" src="" alt="">
  <button class="lb-btn" id="next" onclick="nav(1)">›</button>
  <div id="tools">
    <button class="rotate-btn" onclick="rotateCurrent(-90)">↺ Girar izq.</button>
    <span id="count"></span>
    <button class="rotate-btn" onclick="rotateCurrent(90)">↻ Girar dcha.</button>
  </div>
</div>
<script>
const IMAGES = {img_data};
const ROTATIONS = {rotation_data};
let idx = 0;
function openLb(i) {{ idx = i; document.getElementById('lb').classList.add('open'); render(); }}
function closeLb() {{ document.getElementById('lb').classList.remove('open'); document.getElementById('lb-img').src=''; }}
function nav(d) {{ idx = (idx + d + IMAGES.length) % IMAGES.length; render(); }}
function rotationFor(i) {{ return Number(ROTATIONS[String(i)] || 0); }}
function render() {{
  const img = document.getElementById('lb-img');
  img.src = IMAGES[idx];
  img.style.transform = `rotate(${{rotationFor(idx)}}deg)`;
  document.getElementById('count').textContent = `${{idx+1}} / ${{IMAGES.length}}`;
}}
async function rotateCurrent(delta) {{
  ROTATIONS[String(idx)] = (rotationFor(idx) + delta + 360) % 360;
  render();
  document.querySelectorAll('.tile img')[idx].style.transform = `rotate(${{rotationFor(idx)}}deg)`;
  try {{
    await fetch('/shared-rotate/{token}', {{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{i:idx, rotation:rotationFor(idx)}})
    }});
  }} catch(e) {{}}
}}
document.addEventListener('keydown', e => {{
  if (!document.getElementById('lb').classList.contains('open')) return;
  if (e.key === 'Escape') closeLb();
  if (e.key === 'ArrowRight') nav(1);
  if (e.key === 'ArrowLeft') nav(-1);
}});
</script>
</body>
</html>'''


def cover_resize(img, size):
    from PIL import Image
    target_w, target_h = size
    w, h = img.size
    scale = max(target_w / w, target_h / h)
    resized = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    left = max(0, (resized.width - target_w) // 2)
    top = max(0, (resized.height - target_h) // 2)
    return resized.crop((left, top, left + target_w, top + target_h))

def draw_instagram_brand(canvas, project_title, company, watermark):
    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(canvas)
    try:
        font_big = ImageFont.truetype('arial.ttf', 44)
        font_small = ImageFont.truetype('arial.ttf', 28)
        font_mark = ImageFont.truetype('arial.ttf', 24)
    except Exception:
        font_big = font_small = font_mark = ImageFont.load_default()

    overlay_h = 168
    draw.rectangle((0, canvas.height - overlay_h, canvas.width, canvas.height), fill=(0, 0, 0, 130))
    draw.text((42, canvas.height - 122), project_title, fill=(245, 242, 236), font=font_big)
    draw.text((42, canvas.height - 66), company, fill=(185, 154, 88), font=font_small)
    if watermark:
        text = 'TU CASA EN 3D'
        bbox = draw.textbbox((0, 0), text, font=font_mark)
        draw.text((canvas.width - (bbox[2] - bbox[0]) - 36, 34), text, fill=(255, 255, 255, 150), font=font_mark)

def build_instagram_zip(project, fmt='portrait', watermark=True, include_caption=True, selected_images=None):
    from PIL import Image
    if fmt not in INSTAGRAM_FORMATS:
        fmt = 'portrait'
    width, height, fmt_label = INSTAGRAM_FORMATS[fmt]
    title = clean_name(project.get('name', 'Proyecto'))
    company = project.get('company', '')

    project_images = list(iter_project_images(project))

    valid_images = set(project_images)
    if selected_images:
        images = [src for src in selected_images if src in valid_images]
    else:
        images = project_images[:10]
    if not images:
        raise ValueError('No hay imagenes validas para Instagram')

    stamp = datetime.now().strftime('%Y%m%d_%H%M')
    filename = f'{clean_name(company)}_{title}_instagram_{stamp}.zip'
    buf = io.BytesIO()
    exported = 0
    rotations = load_image_rotations()

    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for src in images:
            fp = url_to_filepath(src)
            if not fp or not os.path.isfile(fp):
                continue
            img = Image.open(fp)
            if img.mode != 'RGB':
                img = img.convert('RGB')
            rot = int(rotations.get(rotation_key(src), 0) or 0) % 360
            if rot:
                img = img.rotate(-rot, expand=True)
            canvas = cover_resize(img, (width, height)).convert('RGBA')
            draw_instagram_brand(canvas, title, company, watermark)

            exported += 1
            img_buf = io.BytesIO()
            canvas.convert('RGB').save(img_buf, 'JPEG', quality=90, optimize=True)
            zf.writestr(f'{exported:02d}_{fmt}.jpg', img_buf.getvalue())

        if include_caption:
            caption = (
                f'{title}\n\n'
                f'Proyecto desarrollado para {company}.\n'
                'Visualizacion arquitectonica e infografia 3D.\n\n'
                '#arquitectura #interiorismo #render #infografia3d #visualizacionarquitectonica '
                '#disenodeinteriores #arquitecturainterior #3dvisualization #tucasaen3d\n'
            )
            zf.writestr('caption.txt', caption)

    if exported == 0:
        raise ValueError('No se ha podido generar ninguna imagen')
    return buf.getvalue(), filename, exported, fmt_label

def generate_instagram_pack(project, fmt='portrait', watermark=True, include_caption=True):
    from PIL import Image
    if fmt not in INSTAGRAM_FORMATS:
        fmt = 'portrait'
    width, height, fmt_label = INSTAGRAM_FORMATS[fmt]
    title = clean_name(project.get('name', 'Proyecto'))
    company = project.get('company', '')
    images = list(iter_project_images(project))
    images = images[:10]
    if not images:
        raise ValueError('El proyecto no tiene imágenes')

    stamp = datetime.now().strftime('%Y%m%d_%H%M')
    out_dir = os.path.join(GALLERY_DIR, 'instagram_exports', f'{clean_name(company)}_{title}_{stamp}')
    os.makedirs(out_dir, exist_ok=True)

    exported = []
    rotations = load_image_rotations()
    for idx, src in enumerate(images, 1):
        fp = url_to_filepath(src)
        if not fp or not os.path.isfile(fp):
            continue
        img = Image.open(fp)
        if img.mode != 'RGB':
            img = img.convert('RGB')
        rot = int(rotations.get(rotation_key(src), 0) or 0) % 360
        if rot:
            img = img.rotate(-rot, expand=True)
        canvas = cover_resize(img, (width, height)).convert('RGBA')
        draw_instagram_brand(canvas, title, company, watermark)
        out = os.path.join(out_dir, f'{idx:02d}_{fmt}.jpg')
        canvas.convert('RGB').save(out, 'JPEG', quality=90, optimize=True)
        exported.append(out)

    if include_caption:
        caption = (
            f'{title}\n\n'
            f'Proyecto desarrollado para {company}.\n'
            'Visualización arquitectónica e infografía 3D.\n\n'
            '#arquitectura #interiorismo #render #infografia3d #visualizacionarquitectonica '
            '#diseñodeinteriores #arquitecturainterior #3dvisualization #tucasaen3d\n'
        )
        with open(os.path.join(out_dir, 'caption.txt'), 'w', encoding='utf-8') as f:
            f.write(caption)

    return out_dir, exported, fmt_label


def run_export(output_dir, projects, max_px, quality, scope_label):
    """Copy+resize images keeping Project/HQ structure, then generate index.html."""
    from PIL import Image

    images_dir = os.path.join(output_dir, 'images')
    total = sum(project_image_count(p) for p in projects)

    with export_lock:
        export_status.update(running=True, done=0, total=total,
                             errors=[], output_dir=output_dir)

    html_projects = []

    for proj in projects:
        proj_name = clean_name(proj['name'])
        html_versions = []

        for version in proj['versions']:
            v_label  = version['label']
            v_dir    = os.path.join(images_dir, proj_name, v_label)
            os.makedirs(v_dir, exist_ok=True)
            v_images = []
            used     = {}

            for url in version['images']:
                src_path = url_to_filepath(url)
                if not src_path or not os.path.isfile(src_path):
                    with export_lock:
                        export_status['errors'].append(f'No encontrado: {url}')
                        export_status['done'] += 1
                    continue

                orig  = os.path.basename(src_path)
                stem  = re.sub(r'\.\d{4}$', '', os.path.splitext(orig)[0]).strip()
                base  = stem
                key   = base.lower()
                idx   = used.get(key, 0)
                used[key] = idx + 1
                name  = f'{base}{" (" + str(idx) + ")" if idx else ""}.jpg'
                out   = os.path.join(v_dir, name)

                try:
                    img = Image.open(src_path)
                    if img.mode not in ('RGB', 'L'):
                        img = img.convert('RGB')
                    w, h = img.size
                    if max(w, h) > max_px:
                        scale = max_px / max(w, h)
                        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
                    img.save(out, 'JPEG', quality=quality, optimize=True)
                    # Relative path from index.html
                    v_images.append(f'images/{proj_name}/{v_label}/{name}')
                except Exception as e:
                    with export_lock:
                        export_status['errors'].append(f'{name}: {e}')

                with export_lock:
                    export_status['done'] += 1

            if v_images:
                html_versions.append({'label': v_label, 'images': v_images})

        final_photos = []
        if proj.get('final_photos'):
            v_dir = os.path.join(images_dir, proj_name, 'Fotos finales')
            os.makedirs(v_dir, exist_ok=True)
            used = {}
            for url in proj.get('final_photos', []):
                src_path = url_to_filepath(url)
                if not src_path or not os.path.isfile(src_path):
                    with export_lock:
                        export_status['errors'].append(f'No encontrado: {url}')
                        export_status['done'] += 1
                    continue
                orig = os.path.basename(src_path)
                stem, ext = os.path.splitext(orig)
                key = stem.lower()
                idx = used.get(key, 0)
                used[key] = idx + 1
                name = f'{stem}{" (" + str(idx) + ")" if idx else ""}.jpg'
                out = os.path.join(v_dir, name)
                try:
                    img = Image.open(src_path)
                    if img.mode != 'RGB':
                        img = img.convert('RGB')
                    w, h = img.size
                    if max(w, h) > max_px:
                        scale = max_px / max(w, h)
                        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
                    img.save(out, 'JPEG', quality=quality, optimize=True)
                    final_photos.append(f'images/{proj_name}/Fotos finales/{name}')
                except Exception as e:
                    with export_lock:
                        export_status['errors'].append(f'{name}: {e}')
                with export_lock:
                    export_status['done'] += 1

        if html_versions:
            html_projects.append({
                'name':     proj_name,
                'versions': html_versions,
                'final_photos': final_photos,
                'types':    proj.get('types', []),
                'year':     proj.get('year'),
            })

    html = build_client_html(html_projects, scope_label)
    with open(os.path.join(output_dir, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(html)

    with export_lock:
        export_status['running'] = False


def build_client_html(projects, title):
    """Return a fully self-contained HTML gallery for the client."""
    import json as _json, datetime

    date_str = datetime.date.today().strftime('%d/%m/%Y')
    proj_data = _json.dumps(projects, ensure_ascii=False)
    total_imgs = sum(project_image_count(p) for p in projects)

    return f'''<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#0d0d0d;--surface:#161616;--surface2:#1e1e1e;--border:#2a2a2a;
      --accent:#c8a96e;--text:#e8e8e8;--dim:#888;--r:8px}}
html,body{{min-height:100%;background:var(--bg);color:var(--text);
           font-family:"Segoe UI",system-ui,sans-serif;font-size:14px}}

/* ── Header ── */
#header{{background:var(--surface);border-bottom:1px solid var(--border);
         padding:28px 40px 22px;display:flex;flex-direction:column;align-items:flex-start;gap:16px}}
#header-logo{{display:flex;align-items:center;gap:14px}}
#header-logo svg{{width:44px;height:40px;color:var(--accent);flex-shrink:0}}
.hlogo-text{{display:flex;flex-direction:column;gap:2px}}
.hlogo-brand{{font-size:18px;font-weight:300;color:var(--text);letter-spacing:.01em;line-height:1.1}}
.hlogo-brand strong{{font-weight:800}}
.hlogo-rule{{height:1px;background:var(--border);margin:3px 0}}
.hlogo-tagline{{font-size:9.5px;font-weight:300;color:var(--dim);letter-spacing:.07em;text-transform:uppercase}}
#header-scope{{padding-left:2px;border-top:1px solid var(--border);padding-top:14px;width:100%}}
#header-scope .scope-label{{font-size:22px;font-weight:700;color:var(--text);letter-spacing:.01em}}
#header-scope .scope-meta{{font-size:12px;color:var(--dim);margin-top:5px}}

/* ── Filter bar ── */
#filter-bar{{padding:14px 40px;border-bottom:1px solid var(--border);
             background:var(--surface);display:flex;flex-direction:column;gap:8px}}
.filter-row{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
.filter-label{{font-size:10px;font-weight:700;color:var(--dim);text-transform:uppercase;
               letter-spacing:.07em;min-width:32px}}
.pill{{padding:3px 10px;border-radius:20px;border:1px solid var(--border);
       background:none;color:var(--dim);font-size:11px;cursor:pointer;
       transition:all .15s;white-space:nowrap}}
.pill:hover{{border-color:var(--accent);color:var(--accent)}}
.pill.active{{background:var(--accent);border-color:var(--accent);color:#111;font-weight:600}}
#result-count{{font-size:11px;color:var(--dim);padding:6px 40px 0}}

/* ── Grid ── */
#content{{padding:24px 40px 40px}}
.proj-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px}}
.proj-card{{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);
            overflow:hidden;cursor:pointer;transition:border-color .2s,transform .2s}}
.proj-card:hover{{border-color:var(--accent);transform:translateY(-2px)}}
.proj-card.hidden{{display:none}}
.card-thumb{{aspect-ratio:4/3;background:var(--surface2);overflow:hidden}}
.card-thumb img{{width:100%;height:100%;object-fit:cover;display:block;
                 transition:transform .3s,opacity .4s;opacity:0}}
.card-thumb img.loaded{{opacity:1}}
.proj-card:hover .card-thumb img{{transform:scale(1.04)}}
.card-body{{padding:11px 13px}}
.card-name{{font-size:13px;font-weight:600;color:var(--text);white-space:nowrap;
            overflow:hidden;text-overflow:ellipsis}}
.card-tags{{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}}
.card-tag{{font-size:10px;padding:1px 7px;border-radius:10px;
           background:var(--surface2);color:var(--dim);border:1px solid var(--border)}}
.card-year{{font-size:10px;color:var(--text-dimmer,#444);margin-top:4px}}

/* ── Modal ── */
#modal-overlay{{display:none;position:fixed;inset:0;z-index:50;
                background:rgba(0,0,0,.88);overflow-y:auto;padding:32px 20px}}
#modal-overlay.open{{display:block}}
#modal{{background:var(--surface);border:1px solid var(--border);border-radius:12px;
        max-width:1100px;margin:0 auto;padding:28px 32px;position:relative}}
#modal-close{{position:absolute;top:16px;right:20px;background:none;border:none;
              color:var(--dim);font-size:26px;cursor:pointer;line-height:1}}
#modal-close:hover{{color:var(--text)}}
#modal-title{{font-size:18px;font-weight:700;color:var(--accent);margin-bottom:20px;padding-right:32px}}
.version-label{{font-size:11px;font-weight:700;color:var(--dim);text-transform:uppercase;
                letter-spacing:.07em;margin-bottom:10px}}
.img-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;margin-bottom:16px}}
.img-item{{border-radius:6px;overflow:hidden;cursor:pointer;aspect-ratio:4/3;background:var(--surface2)}}
.img-item img{{width:100%;height:100%;object-fit:cover;display:block;
               transition:transform .25s,opacity .4s;opacity:0}}
.img-item img.loaded{{opacity:1}}
.img-item:hover img{{transform:scale(1.04)}}
.older-toggle{{display:flex;align-items:center;gap:8px;cursor:pointer;padding:10px 0;
               border-top:1px solid var(--border);color:var(--dim);font-size:12px;
               user-select:none;margin-top:8px}}
.older-toggle:hover{{color:var(--text)}}
.toggle-arrow{{transition:transform .2s;display:inline-block}}
.toggle-arrow.open{{transform:rotate(90deg)}}
.older-versions{{display:none;margin-top:12px}}
.older-versions.open{{display:block}}

/* ── Lightbox ── */
#lb{{display:none;position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.96);
      align-items:center;justify-content:center}}
#lb.open{{display:flex}}
#lb img{{max-width:calc(100vw - 120px);max-height:calc(100vh - 80px);
          object-fit:contain;border-radius:4px;box-shadow:0 20px 60px rgba(0,0,0,.8)}}
#lb-close{{position:fixed;top:16px;right:20px;background:none;border:none;
            color:var(--dim);font-size:30px;cursor:pointer;line-height:1}}
#lb-close:hover{{color:var(--text)}}
.lb-nav{{position:fixed;top:50%;transform:translateY(-50%);background:rgba(255,255,255,.08);
          border:none;color:var(--text);width:50px;height:80px;font-size:24px;
          cursor:pointer;border-radius:4px}}
.lb-nav:hover{{background:rgba(255,255,255,.15)}}
#lb-prev{{left:10px}}#lb-next{{right:10px}}
#lb-info{{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);
           background:rgba(0,0,0,.6);color:var(--dim);font-size:12px;
           padding:4px 14px;border-radius:10px;white-space:nowrap}}
</style>
</head>
<body>

<!-- Header -->
<div id="header">
  <div id="header-logo">
    <svg viewBox="0 0 100 92" fill="none" xmlns="http://www.w3.org/2000/svg">
      <polygon points="50,5 95,46 95,87 5,87 5,46" stroke="currentColor" stroke-width="9" stroke-linejoin="miter" fill="none"/>
    </svg>
    <div class="hlogo-text">
      <div class="hlogo-brand"><strong>tu casa</strong> en 3d</div>
      <div class="hlogo-rule"></div>
      <div class="hlogo-tagline">diseño + infografía</div>
    </div>
  </div>
  <div id="header-scope">
    <div class="scope-label">{title}</div>
    <div class="scope-meta">{len(projects)} proyecto{'s' if len(projects) != 1 else ''} · {total_imgs} imagen{'es' if total_imgs != 1 else ''} · {date_str}</div>
  </div>
</div>

<!-- Filter bar -->
<div id="filter-bar">
  <div class="filter-row">
    <span class="filter-label">Tipo</span>
    <div id="type-pills"></div>
  </div>
  <div class="filter-row">
    <span class="filter-label">Año</span>
    <div id="year-pills"></div>
  </div>
</div>
<div id="result-count"></div>

<!-- Grid -->
<div id="content">
  <div class="proj-grid" id="grid"></div>
</div>

<!-- Modal -->
<div id="modal-overlay">
  <div id="modal">
    <button id="modal-close" onclick="closeModal()">×</button>
    <div id="modal-title"></div>
    <div id="modal-body"></div>
  </div>
</div>

<!-- Lightbox -->
<div id="lb">
  <button id="lb-close" onclick="lbClose()">×</button>
  <button class="lb-nav" id="lb-prev" onclick="lbNav(-1)">‹</button>
  <img id="lb-img" src="" alt="">
  <button class="lb-nav" id="lb-next" onclick="lbNav(1)">›</button>
  <div id="lb-info"></div>
</div>

<script>
const PROJECTS = {proj_data};

let lbAll = [], lbIdx = 0;
let activeType = null, activeYear = null;

// ── Filters ────────────────────────────────────────────────────────────────────
const allTypes = [...new Set(PROJECTS.flatMap(p => p.types || []))].sort();
const allYears = [...new Set(PROJECTS.map(p => p.year).filter(Boolean))].sort((a,b) => b-a);

function buildPills(containerId, items, onClick) {{
  const wrap = document.getElementById(containerId);
  items.forEach(val => {{
    const btn = document.createElement('button');
    btn.className = 'pill';
    btn.textContent = val;
    btn.dataset.val = val;
    btn.onclick = () => onClick(btn, val);
    wrap.appendChild(btn);
  }});
}}

function togglePill(btn, containerId) {{
  document.querySelectorAll(`#${{containerId}} .pill`).forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
}}

function clearPill(containerId) {{
  document.querySelectorAll(`#${{containerId}} .pill`).forEach(p => p.classList.remove('active'));
}}

buildPills('type-pills', allTypes, (btn, val) => {{
  if (activeType === val) {{ activeType = null; clearPill('type-pills'); }}
  else {{ activeType = val; togglePill(btn, 'type-pills'); }}
  applyFilters();
}});

buildPills('year-pills', allYears, (btn, val) => {{
  if (activeYear === val) {{ activeYear = null; clearPill('year-pills'); }}
  else {{ activeYear = val; togglePill(btn, 'year-pills'); }}
  applyFilters();
}});

function applyFilters() {{
  let visible = 0;
  document.querySelectorAll('.proj-card').forEach(card => {{
    const pi = parseInt(card.dataset.pi);
    const p = PROJECTS[pi];
    const typeOk = !activeType || (p.types || []).includes(activeType);
    const yearOk = !activeYear || p.year === activeYear;
    const show = typeOk && yearOk;
    card.classList.toggle('hidden', !show);
    if (show) visible++;
  }});
  document.getElementById('result-count').textContent =
    (activeType || activeYear)
      ? `${{visible}} proyecto${{visible !== 1 ? 's' : ''}} con los filtros aplicados`
      : '';
}}

// ── Card grid ──────────────────────────────────────────────────────────────────
const grid = document.getElementById('grid');
const cardObs = new IntersectionObserver(entries => {{
  entries.forEach(e => {{
    if (!e.isIntersecting) return;
    const img = e.target.querySelector('img[data-src]');
    if (img) {{ img.src = img.dataset.src; img.removeAttribute('data-src'); }}
    cardObs.unobserve(e.target);
  }});
}}, {{ rootMargin: '400px' }});

PROJECTS.forEach((proj, pi) => {{
  const cover = proj.versions[0]?.images[0] ?? '';
  const vCount = proj.versions.length;
  const tags = (proj.types || []).map(t => `<span class="card-tag">${{t}}</span>`).join('');
  const card = document.createElement('div');
  card.className = 'proj-card';
  card.dataset.pi = pi;
  card.innerHTML = `
    <div class="card-thumb"><img data-src="${{cover}}" alt="${{proj.name}}"></div>
    <div class="card-body">
      <div class="card-name">${{proj.name}}</div>
      ${{tags ? `<div class="card-tags">${{tags}}</div>` : ''}}
      <div class="card-year">${{proj.year || ''}}${{vCount > 1 ? ` · ${{vCount}} versiones` : ''}}</div>
    </div>`;
  card.querySelector('img').onload = e => e.target.classList.add('loaded');
  card.onclick = () => openModal(pi);
  grid.appendChild(card);
  cardObs.observe(card);
}});

// ── Modal ──────────────────────────────────────────────────────────────────────
function openModal(pi) {{
  const proj = PROJECTS[pi];
  document.getElementById('modal-title').textContent = proj.name;
  const body = document.getElementById('modal-body');
  body.innerHTML = '';
  lbAll = [];

  const [latest, ...older] = proj.versions;
  body.appendChild(buildVersionGrid(latest, true));

  if (older.length) {{
    const toggle = document.createElement('div');
    toggle.className = 'older-toggle';
    toggle.innerHTML = `<span class="toggle-arrow">›</span> Versiones anteriores (${{older.length}})`;
    const olderDiv = document.createElement('div');
    olderDiv.className = 'older-versions';
    older.forEach(v => olderDiv.appendChild(buildVersionGrid(v, false)));
    toggle.onclick = () => {{
      const open = olderDiv.classList.toggle('open');
      toggle.querySelector('.toggle-arrow').classList.toggle('open', open);
    }};
    body.appendChild(toggle);
    body.appendChild(olderDiv);
  }}

  if ((proj.final_photos || []).length) {{
    body.appendChild(buildFinalPhotosGrid(proj.final_photos));
  }}

  document.getElementById('modal-overlay').classList.add('open');

  const imgObs = new IntersectionObserver(entries => {{
    entries.forEach(e => {{
      if (!e.isIntersecting) return;
      const img = e.target.querySelector('img[data-src]');
      if (img) {{ img.src = img.dataset.src; img.removeAttribute('data-src'); }}
      imgObs.unobserve(e.target);
    }});
  }}, {{ rootMargin: '200px' }});
  body.querySelectorAll('.img-item').forEach(el => imgObs.observe(el));
}}

function buildVersionGrid(version, isLatest) {{
  const wrap = document.createElement('div');
  const label = document.createElement('div');
  label.className = 'version-label';
  label.textContent = isLatest ? version.label + ' — última versión' : version.label;
  wrap.appendChild(label);

  const imgGrid = document.createElement('div');
  imgGrid.className = 'img-grid';

  version.images.forEach((src, i) => {{
    const globalIdx = lbAll.length;
    lbAll.push({{ src, proj: version.label, n: i + 1, total: version.images.length }});
    const item = document.createElement('div');
    item.className = 'img-item';
    item.onclick = () => lbOpen(globalIdx);
    const img = document.createElement('img');
    img.dataset.src = src;
    img.alt = version.label;
    img.onload = () => img.classList.add('loaded');
    item.appendChild(img);
    imgGrid.appendChild(item);
  }});

  wrap.appendChild(imgGrid);
  return wrap;
}}

function buildFinalPhotosGrid(images) {{
  return buildVersionGrid({{ label: 'Fotos finales de obra', images }}, false);
}}

function closeModal() {{
  document.getElementById('modal-overlay').classList.remove('open');
  lbAll = [];
}}

document.getElementById('modal-overlay').addEventListener('click', e => {{
  if (e.target === document.getElementById('modal-overlay')) closeModal();
}});

// ── Lightbox ───────────────────────────────────────────────────────────────────
function lbOpen(idx) {{
  lbIdx = idx;
  document.getElementById('lb').classList.add('open');
  lbRender();
}}
function lbClose() {{
  document.getElementById('lb').classList.remove('open');
  document.getElementById('lb-img').src = '';
}}
function lbNav(d) {{
  lbIdx = (lbIdx + d + lbAll.length) % lbAll.length;
  lbRender();
}}
function lbRender() {{
  const e = lbAll[lbIdx];
  document.getElementById('lb-img').src = e.src;
  document.getElementById('lb-info').textContent = `${{e.proj}} — ${{e.n}} / ${{e.total}}`;
}}
document.addEventListener('keydown', e => {{
  if (document.getElementById('lb').classList.contains('open')) {{
    if (e.key === 'Escape') lbClose();
    else if (e.key === 'ArrowRight') lbNav(1);
    else if (e.key === 'ArrowLeft')  lbNav(-1);
    return;
  }}
  if (e.key === 'Escape') closeModal();
}});
document.getElementById('lb').addEventListener('click', e => {{
  if (e.target === document.getElementById('lb')) lbClose();
}});
</script>
</body>
</html>'''


# ── Auto-reindex (watchdog) ───────────────────────────────────────────────────
_reindex_timer = None
_reindex_lock  = threading.Lock()
_reindex_log   = {'running': False, 'last': None, 'error': None, 'output': None}


def schedule_reindex(delay=120):
    """Debounced reindex: waits `delay` seconds after the last change."""
    global _reindex_timer
    with _reindex_lock:
        if _reindex_timer:
            _reindex_timer.cancel()
        _reindex_timer = threading.Timer(delay, _do_reindex)
        _reindex_timer.daemon = True
        _reindex_timer.start()


def _do_reindex():
    import datetime
    _reindex_log['running'] = True
    _reindex_log['error']   = None
    _reindex_log['output']  = None
    print('[reindex] Iniciando...', flush=True)
    script = os.path.join(GALLERY_DIR, 'generate_data.py')
    env    = {**os.environ, 'TRABAJOS_DIR': TRABAJOS_DIR,
              'OUTPUT_JS': os.path.join(GALLERY_DIR, 'data.js')}
    if TRABAJOS_DIR2:
        env['TRABAJOS_DIR2'] = TRABAJOS_DIR2
    r = subprocess.run([sys.executable, script], capture_output=True, text=True, env=env)
    _reindex_log['running'] = False
    _reindex_log['last']    = datetime.datetime.now().isoformat()
    if r.returncode != 0:
        _reindex_log['error'] = r.stderr[-500:]
        print(f'[reindex] Error:\n{r.stderr}', flush=True)
    else:
        _reindex_log['output'] = r.stdout[-2000:]
        print(f'[reindex] Completado.\n{r.stdout}', flush=True)


REINDEX_INTERVAL = int(os.environ.get('REINDEX_INTERVAL', 60))  # minutos


def start_watcher():
    """Reindex periodically — avoids inotify limits on NAS with many folders."""
    if REINDEX_INTERVAL <= 0:
        print('[watcher] Reindexado automático desactivado.', flush=True)
        return

    def loop():
        while True:
            threading.Event().wait(REINDEX_INTERVAL * 60)
            print(f'[watcher] Reindexado periodico ({REINDEX_INTERVAL} min)...', flush=True)
            _do_reindex()

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    print(f'[watcher] Reindexado automático cada {REINDEX_INTERVAL} min.', flush=True)


# ── HTTP Handler ───────────────────────────────────────────────────────────────
class Handler(SimpleHTTPRequestHandler):

    def translate_path(self, path):
        path = unquote(path).split('?', 1)[0].split('#', 1)[0]
        if path.startswith('/trabajos2/') and TRABAJOS_DIR2:
            rel = path[len('/trabajos2/'):].replace('/', os.sep)
            return os.path.join(TRABAJOS_DIR2, rel)
        if path.startswith('/trabajos/'):
            rel = path[len('/trabajos/'):].replace('/', os.sep)
            return os.path.join(TRABAJOS_DIR, rel)
        rel = path.lstrip('/').replace('/', os.sep)
        return os.path.join(GALLERY_DIR, rel)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header('Location', location)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _html(self, content, code=200):
        body = content.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _js(self, source, code=200):
        body = source.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/javascript; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    # ── Login page ─────────────────────────────────────────────────────────
    def _serve_login(self, error=''):
        err_html = f'<div class="error">{error}</div>' if error else ''
        self._html(LOGIN_HTML.format(error=err_html))

    def _handle_login(self):
        length = int(self.headers.get('Content-Length', 0))
        raw    = self.rfile.read(length).decode('utf-8')
        params = {}
        for part in raw.split('&'):
            k, _, v = part.partition('=')
            params[unquote(k.replace('+', ' '))] = unquote(v.replace('+', ' '))
        username = params.get('username', '').strip()
        password = params.get('password', '')
        users    = load_users()
        user     = users.get(username)
        if user and verify_password(password, user.get('password', '')):
            token = create_session(username, user.get('role', 'client'))
            self.send_response(302)
            self.send_header('Location', '/')
            self.send_header('Set-Cookie',
                f'gallery_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400')
            self.send_header('Content-Length', '0')
            self.end_headers()
        else:
            self._serve_login(error='Usuario o contraseña incorrectos')

    # ── Serve index with injected user info ────────────────────────────────
    def _serve_index(self, sess):
        path = os.path.join(GALLERY_DIR, 'index.html')
        try:
            with open(path, encoding='utf-8') as f:
                content = f.read()
        except OSError:
            self.send_error(500)
            return
        user_json = json.dumps({'username': sess['username'], 'role': sess['role']},
                               ensure_ascii=False)
        inject = f'<script>window.GALLERY_USER = {user_json};</script>\n'
        defaults = '<script>window.GALLERY_DATA = window.GALLERY_DATA || []; window.IMAGE_ROTATIONS = window.IMAGE_ROTATIONS || {}; window.GALLERY_BOOT_ERROR = "";</script>\n'
        content = content.replace('<script src="data.js"></script>',
                                  inject + defaults + '<script src="data.js"></script>', 1)
        self._html(content)

    # ── Serve filtered data.js ─────────────────────────────────────────────
    def _serve_data_js(self, sess):
        filtered, _ = ensure_session_gallery_data(sess)
        self._js(f'window.GALLERY_DATA = {json.dumps(filtered, ensure_ascii=False)};\n')

    def _serve_rotations_js(self, sess):
        rotations = load_image_rotations()
        filtered = {}
        _, lookup = ensure_session_gallery_data(sess)
        for media_id, src in lookup.items():
            rot = int(rotations.get(rotation_key(src), 0) or 0) % 360
            if rot:
                filtered[media_id] = rot
        self._js(f'window.IMAGE_ROTATIONS = {json.dumps(filtered, ensure_ascii=False)};\n')

    def _serve_project_detail(self, sess, company, project_name):
        project = find_project_for_user(sess.get('username', ''), company, project_name)
        if not project:
            self._json({'error': 'Proyecto no encontrado'}, 404)
            return
        lookup = {}
        public_project = clone_public_project(project, lookup)
        _, media_lookup = ensure_session_gallery_data(sess)
        media_lookup.update(lookup)
        rotations = load_image_rotations()
        filtered_rotations = {}
        for media_id, src in lookup.items():
            rot = int(rotations.get(rotation_key(src), 0) or 0) % 360
            if rot:
                filtered_rotations[media_id] = rot
        self._json({
            'ok': True,
            'project': public_project,
            'rotations': filtered_rotations,
        })

    def _serve_session_required_js(self):
        self._js(
            'window.GALLERY_DATA = window.GALLERY_DATA || [];\n'
            'window.IMAGE_ROTATIONS = window.IMAGE_ROTATIONS || {};\n'
            'window.GALLERY_BOOT_ERROR = "session_required";\n'
            'window.location.replace("/login");\n'
        )

    def _serve_favorites(self, sess):
        self._json({'favorites': favorites_for_user(sess.get('username', ''))})

    def _toggle_favorite(self, sess):
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length) or b'{}')
        company = str(body.get('company', '')).strip()
        project_name = str(body.get('project', '')).strip()
        if not company or not project_name:
            self._json({'error': 'Faltan datos del proyecto'}, 400)
            return
        project = find_project_for_user(sess.get('username', ''), company, project_name)
        if not project:
            self._json({'error': 'Proyecto no encontrado'}, 404)
            return
        favorites = load_favorites()
        username = sess.get('username', '')
        current = favorites.get(username, [])
        key = favorite_key(company, project_name)
        if key in current:
            current = [entry for entry in current if entry != key]
            favorited = False
        else:
            current = current + [key]
            favorited = True
        favorites[username] = current
        save_favorites(favorites)
        self._json({'ok': True, 'favorited': favorited, 'favorites': current})

    def _require_admin(self, sess):
        if sess.get('role') != 'admin':
            self._json({'error': 'No autorizado'}, 403)
            return False
        return True

    def _admin_save_user(self, sess):
        if not self._require_admin(sess):
            return
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length) or b'{}')
        username = str(body.get('username', '')).strip()
        password = str(body.get('password', ''))
        role = str(body.get('role', 'client')).strip()
        name = str(body.get('name', username)).strip() or username
        logo_url = str(body.get('logo_url', '')).strip()
        companies = body.get('companies', [])

        if not username:
            self._json({'error': 'Usuario vacío'}, 400)
            return
        if role not in ('admin', 'client', 'social'):
            self._json({'error': 'Rol no válido'}, 400)
            return
        users = load_users()
        existing = users.get(username, {})
        if not password:
            password = existing.get('password', '')
        if not password:
            self._json({'error': 'Contraseña vacía'}, 400)
            return
        if password != existing.get('password', ''):
            password = hash_password(password)

        clean_companies = []
        if isinstance(companies, list):
            for company in companies:
                company = str(company).strip()
                if company and company not in clean_companies:
                    clean_companies.append(company)

        user = {'password': password, 'role': role, 'name': name}
        if logo_url:
            user['logo_url'] = logo_url
        if role in ('client', 'social'):
            user['companies'] = clean_companies
        users[username] = user
        save_users(users)
        self._json({'ok': True, 'users': sanitize_users_for_admin(users)})

    def _admin_delete_user(self, sess):
        if not self._require_admin(sess):
            return
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length) or b'{}')
        username = str(body.get('username', '')).strip()
        if not username:
            self._json({'error': 'Usuario vacío'}, 400)
            return
        if username == sess.get('username'):
            self._json({'error': 'No puedes borrar tu propio usuario'}, 400)
            return
        users = load_users()
        if username not in users:
            self._json({'error': 'Usuario no encontrado'}, 404)
            return
        del users[username]
        save_users(users)
        self._json({'ok': True, 'users': users})

    def _admin_create_shared_link(self, sess):
        if sess.get('role') not in ('admin', 'client'):
            self._json({'error': 'No autorizado'}, 403)
            return
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length) or b'{}')
        company = str(body.get('company', '')).strip()
        project_name = str(body.get('project', '')).strip()
        logo_url = str(body.get('logo_url', '')).strip()
        title = str(body.get('title', '')).strip()
        watermark = bool(body.get('watermark'))
        days = int(body.get('days', 10) or 10)
        days = max(1, min(days, 30))

        project = find_project_for_user(sess.get('username', ''), company, project_name)
        if not project:
            self._json({'error': 'Proyecto no encontrado'}, 404)
            return
        versions = project.get('versions') or []
        if not versions:
            self._json({'error': 'Proyecto sin imágenes'}, 400)
            return

        latest = versions[0]
        images = latest.get('images') or []
        if not images:
            self._json({'error': 'Versión actual sin imágenes'}, 400)
            return

        if not logo_url:
            users = load_users()
            for user in users.values():
                if user.get('role') == 'client' and company in (user.get('companies') or []):
                    logo_url = str(user.get('logo_url', '')).strip()
                    if logo_url:
                        break

        links = cleanup_shared_links()
        token = secrets.token_urlsafe(18)
        expires_at = (datetime.now() + timedelta(days=days)).replace(microsecond=0).isoformat()
        links[token] = {
            'created_at': datetime.now().replace(microsecond=0).isoformat(),
            'expires_at': expires_at,
            'created_by': sess.get('username'),
            'company': company,
            'project': project_name,
            'title': title or project_name,
            'version': latest.get('label', ''),
            'logo_url': logo_url,
            'watermark': watermark,
            'images': images,
            'option_groups': latest.get('option_groups', []),
        }
        save_shared_links(links)
        path = f'/s/{token}'
        self._json({'ok': True, 'token': token, 'url': make_public_url(path), 'expires_at': expires_at})

    def _admin_update_shared_link(self, sess):
        if not self._require_admin(sess):
            return
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length) or b'{}')
        token = str(body.get('token', '')).strip()
        action = str(body.get('action', '')).strip()
        links = load_shared_links()
        link = links.get(token)
        if not link:
            self._json({'error': 'Enlace no encontrado'}, 404)
            return
        if action == 'extend':
            days = int(body.get('days', 5) or 5)
            days = max(1, min(days, 30))
            try:
                base = datetime.fromisoformat(link.get('expires_at', ''))
            except ValueError:
                base = datetime.now()
            if base < datetime.now():
                base = datetime.now()
            link['expires_at'] = (base + timedelta(days=days)).replace(microsecond=0).isoformat()
            save_shared_links(links)
            self._json({'ok': True, 'expires_at': link['expires_at']})
            return
        if action == 'delete':
            del links[token]
            save_shared_links(links)
            self._json({'ok': True})
            return
        self._json({'error': 'Acción no válida'}, 400)

    def _get_shared_link(self, token):
        links = cleanup_shared_links()
        link = links.get(token)
        if not link:
            return None
        try:
            if datetime.fromisoformat(link.get('expires_at', '1970-01-01')) <= datetime.now():
                return None
        except ValueError:
            return None
        return link

    def _serve_shared_page(self, token):
        link = self._get_shared_link(token)
        if not link:
            self._html('<!doctype html><meta charset="utf-8"><title>Enlace caducado</title><body style="font-family:Arial;background:#111;color:#eee;padding:40px">Este enlace no existe o ha caducado.</body>', 404)
            return
        links = load_shared_links()
        tracked = links.get(token)
        if tracked:
            tracked['visits'] = int(tracked.get('visits', 0) or 0) + 1
            tracked['last_visit'] = datetime.now().replace(microsecond=0).isoformat()
            save_shared_links(links)
            link = tracked
        self._html(build_shared_html(token, link))

    def _serve_shared_image(self, token, thumb=False):
        link = self._get_shared_link(token)
        if not link:
            self.send_error(404)
            return
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            idx = int(params.get('i', ['0'])[0])
        except ValueError:
            self.send_error(400)
            return
        images = link.get('images') or []
        if idx < 0 or idx >= len(images):
            self.send_error(404)
            return
        src = images[idx]
        if thumb:
            try:
                width = int(params.get('w', ['900'])[0])
                width = max(200, min(width, 2400))
            except ValueError:
                width = 900
            path = get_or_create_thumb(src, width) or url_to_filepath(src)
        else:
            path = url_to_filepath(src)
        if not path or not os.path.isfile(path):
            self.send_error(404)
            return
        try:
            with open(path, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Content-Length', len(data))
            self.send_header('Cache-Control', 'public, max-age=3600')
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            self.send_error(500)

    # ── Download version as ZIP ────────────────────────────────────────────
    def _handle_shared_download(self, token):
        link = self._get_shared_link(token)
        if not link:
            self.send_error(404)
            return
        label = clean_name(link.get('title') or link.get('project') or 'imagenes')
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            used = {}
            for url in link.get('images') or []:
                fp = url_to_filepath(url)
                if not fp or not os.path.isfile(fp):
                    continue
                stem, ext = os.path.splitext(os.path.basename(fp))
                key = stem.lower()
                idx = used.get(key, 0)
                used[key] = idx + 1
                name = f'{stem}{" (" + str(idx) + ")" if idx else ""}{ext}'
                zf.write(fp, name)
        buf.seek(0)
        data = buf.read()
        self.send_response(200)
        self.send_header('Content-Type', 'application/zip')
        self.send_header('Content-Disposition', f'attachment; filename="{label}.zip"')
        self.send_header('Content-Length', len(data))
        self.end_headers()
        self.wfile.write(data)

    def _handle_shared_rotate(self, token):
        links = cleanup_shared_links()
        link = links.get(token)
        if not link:
            self._json({'error': 'Enlace no encontrado'}, 404)
            return
        try:
            if datetime.fromisoformat(link.get('expires_at', '1970-01-01')) <= datetime.now():
                self._json({'error': 'Enlace caducado'}, 404)
                return
        except ValueError:
            self._json({'error': 'Enlace no válido'}, 404)
            return
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length) or b'{}')
        try:
            idx = int(body.get('i'))
            rotation = int(body.get('rotation')) % 360
        except (TypeError, ValueError):
            self._json({'error': 'Rotación no válida'}, 400)
            return
        images = link.get('images') or []
        if idx < 0 or idx >= len(images):
            self._json({'error': 'Imagen no encontrada'}, 404)
            return
        rotations = link.setdefault('rotations', {})
        if rotation:
            rotations[str(idx)] = rotation
        else:
            rotations.pop(str(idx), None)
        save_shared_links(links)
        self._json({'ok': True, 'rotation': rotation})

    def _handle_gallery_rotate(self, sess):
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length) or b'{}')
        src = resolve_session_image_ref(sess, str(body.get('src', '')).strip())
        try:
            rotation = int(body.get('rotation')) % 360
        except (TypeError, ValueError):
            self._json({'error': 'Rotación no válida'}, 400)
            return
        if not src:
            self._json({'error': 'Imagen no autorizada'}, 403)
            return
        key = rotation_key(src)
        rotations = load_image_rotations()
        if rotation:
            rotations[key] = rotation
        else:
            rotations.pop(key, None)
        save_image_rotations(rotations)
        self._json({'ok': True, 'rotation': rotation})

    def _admin_create_instagram_pack(self, sess):
        if not self._require_admin(sess):
            return
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length) or b'{}')
        company = str(body.get('company', '')).strip()
        project_name = str(body.get('project', '')).strip()
        fmt = str(body.get('format', 'portrait')).strip()
        watermark = bool(body.get('watermark', True))
        include_caption = bool(body.get('caption', True))
        selected_images = body.get('images', [])
        if not isinstance(selected_images, list):
            selected_images = []
        selected_images = [
            src for src in (resolve_session_image_ref(sess, str(ref).strip()) for ref in selected_images)
            if src
        ]
        project = find_project(company, project_name)
        if not project:
            self._json({'error': 'Proyecto no encontrado'}, 404)
            return
        try:
            zip_data, filename, exported, fmt_label = build_instagram_zip(
                project, fmt, watermark, include_caption, selected_images
            )
        except Exception as e:
            self._json({'error': str(e)}, 500)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/zip')
        self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.send_header('Content-Length', str(len(zip_data)))
        self.send_header('X-Instagram-Count', str(exported))
        self.send_header('X-Instagram-Format', fmt_label)
        self.end_headers()
        self.wfile.write(zip_data)

    def _admin_save_project_billing(self, sess):
        if not self._require_admin(sess):
            return
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length) or b'{}')
        company = str(body.get('company', '')).strip()
        project_name = str(body.get('project', '')).strip()
        try:
            amount = float(str(body.get('amount', 0)).replace(',', '.').strip() or 0)
        except ValueError:
            self._json({'error': 'Importe no valido'}, 400)
            return
        if amount < 0:
            self._json({'error': 'El importe no puede ser negativo'}, 400)
            return
        date = str(body.get('date', '')).strip()
        if date and not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
            self._json({'error': 'Fecha no valida'}, 400)
            return
        paid_by = str(body.get('paid_by', '')).strip()
        if paid_by not in ('', 'JJ', 'Noelia'):
            self._json({'error': 'Cobrador no valido'}, 400)
            return
        paid = bool(body.get('paid', False))
        if not find_project(company, project_name):
            self._json({'error': 'Proyecto no encontrado'}, 404)
            return
        billing = load_project_billing()
        key = billing_key(company, project_name)
        entry = {
            'amount': round(amount, 2),
            'date': date,
            'paid_by': paid_by,
            'paid': paid,
        }
        if amount or date or paid_by or paid:
            billing[key] = entry
        else:
            billing.pop(key, None)
        save_project_billing(billing)
        self._json({'ok': True, **entry})

    def _admin_save_social_project(self, sess):
        if not self._require_admin(sess):
            return
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length) or b'{}')
        company = str(body.get('company', '')).strip()
        project_name = str(body.get('project', '')).strip()
        enabled = bool(body.get('enabled', False))
        if not find_project(company, project_name):
            self._json({'error': 'Proyecto no encontrado'}, 404)
            return
        social_visibility = load_social_visibility()
        key = social_key(company, project_name)
        if enabled:
            social_visibility[key] = True
        else:
            social_visibility.pop(key, None)
        save_social_visibility(social_visibility)
        self._json({'ok': True, 'enabled': enabled})

    def _handle_download(self, sess):
        length = int(self.headers.get('Content-Length', 0))
        body   = json.loads(self.rfile.read(length))
        images = body.get('images', [])
        label  = clean_name(body.get('label', 'version'))
        if not isinstance(images, list):
            images = []
        resolved_images = []
        for ref in images:
            src = resolve_session_image_ref(sess, str(ref).strip())
            if src and src not in resolved_images:
                resolved_images.append(src)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            used = {}
            for url in resolved_images:
                fp = url_to_filepath(url)
                if not fp or not os.path.isfile(fp):
                    continue
                stem, ext = os.path.splitext(os.path.basename(fp))
                key = stem.lower()
                idx = used.get(key, 0)
                used[key] = idx + 1
                name = f'{stem}{" (" + str(idx) + ")" if idx else ""}{ext}'
                zf.write(fp, name)
        buf.seek(0)
        data = buf.read()

        self.send_response(200)
        self.send_header('Content-Type', 'application/zip')
        self.send_header('Content-Disposition', f'attachment; filename="{label}.zip"')
        self.send_header('Content-Length', len(data))
        self.end_headers()
        self.wfile.write(data)

    # ── Serve thumbnail ───────────────────────────────────────────────────
    def _serve_image_file(self, path, content_type='image/jpeg', cache_control='private, max-age=604800'):
        try:
            with open(path, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', len(data))
            self.send_header('Cache-Control', cache_control)
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            self.send_error(500)

    def _handle_thumb(self, src_url, width):
        thumb_path = get_or_create_thumb(src_url, width)
        if thumb_path:
            self._serve_image_file(thumb_path, 'image/jpeg')
            return
        thumb_path = url_to_filepath(src_url)
        if not thumb_path or not os.path.isfile(thumb_path):
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(thumb_path)[0] or 'image/jpeg'
        self._serve_image_file(thumb_path, content_type)

    def _handle_media(self, sess, ref, width=0):
        src_url = resolve_session_image_ref(sess, ref)
        if not src_url:
            self.send_error(404)
            return
        width = max(0, min(int(width or 0), 3200))
        if width:
            thumb_path = get_or_create_thumb(src_url, width)
            if thumb_path:
                self._serve_image_file(thumb_path, 'image/jpeg')
                return
        path = url_to_filepath(src_url)
        if not path or not os.path.isfile(path):
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(path)[0] or 'image/jpeg'
        self._serve_image_file(path, content_type)

    # ── Request handlers ───────────────────────────────────────────────────
    def do_GET(self):
        if self.path.startswith('/s/'):
            token = self.path.split('?', 1)[0].strip('/').split('/', 1)[1]
            self._serve_shared_page(token)
            return
        if self.path.startswith('/shared-thumb/'):
            token = self.path.split('?', 1)[0].strip('/').split('/', 1)[1]
            self._serve_shared_image(token, thumb=True)
            return
        if self.path.startswith('/shared-image/'):
            token = self.path.split('?', 1)[0].strip('/').split('/', 1)[1]
            self._serve_shared_image(token, thumb=False)
            return
        if self.path.startswith('/shared-download/'):
            token = self.path.split('?', 1)[0].strip('/').split('/', 1)[1]
            self._handle_shared_download(token)
            return

        if self.path in ('/login', '/login?error=1'):
            self._serve_login()
            return

        sess, _ = get_session(self.headers)
        if not sess:
            if self.path in ('/data.js', '/rotations.js'):
                self._serve_session_required_js()
                return
            self._redirect('/login')
            return

        if self.path in ('/', '/index.html'):
            self._serve_index(sess)
            return
        if self.path == '/data.js':
            self._serve_data_js(sess)
            return
        if self.path == '/rotations.js':
            self._serve_rotations_js(sess)
            return
        parsed = urlparse(self.path)
        if parsed.path.startswith('/media/'):
            ref = parsed.path[len('/media/'):]
            params = parse_qs(parsed.query)
            try:
                width = int(params.get('w', ['0'])[0] or 0)
            except ValueError:
                width = 0
            if not ref:
                self.send_error(400)
                return
            self._handle_media(sess, ref, width)
            return
        if parsed.path.startswith('/thumb/'):
            ref = parsed.path[len('/thumb/'):]
            params = parse_qs(parsed.query)
            try:
                width = int(params.get('w', ['400'])[0])
                width = max(100, min(width, 2400))
            except ValueError:
                width = 400
            src = resolve_session_image_ref(sess, ref)
            if not src:
                self.send_error(404)
                return
            self._handle_thumb(src, width)
            return
        if self.path == '/export/status':
            self._json(export_status if sess['role'] == 'admin' else {'error': 'No autorizado'})
            return
        if self.path == '/api/admin':
            if not self._require_admin(sess):
                return
            self._json(admin_payload())
            return
        if self.path == '/api/favorites':
            self._serve_favorites(sess)
            return
        if parsed.path == '/api/project-detail':
            params = parse_qs(parsed.query)
            company = str(params.get('company', [''])[0]).strip()
            project_name = str(params.get('project', [''])[0]).strip()
            if not company or not project_name:
                self._json({'error': 'Faltan datos del proyecto'}, 400)
                return
            self._serve_project_detail(sess, company, project_name)
            return
        if self.path == '/api/reindex/status':
            if not self._require_admin(sess):
                return
            self._json(_reindex_log)
            return
        if self.path == '/logout':
            self.send_response(302)
            self.send_header('Location', '/login')
            self.send_header('Set-Cookie', 'gallery_session=; Path=/; Max-Age=0')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return

        if parsed.path == '/thumb':
            params = parse_qs(parsed.query)
            src = params.get('src', [''])[0]
            try:
                w = int(params.get('w', ['400'])[0])
                w = max(100, min(w, 2400))  # clamp to sane range
            except ValueError:
                w = 400
            if not src:
                self.send_error(400)
                return
            src = resolve_session_image_ref(sess, src)
            if not src:
                self.send_error(404)
                return
            self._handle_thumb(src, w)
            return
        if parsed.path.startswith('/trabajos/') or parsed.path.startswith('/trabajos2/'):
            self.send_error(404)
            return

        super().do_GET()

    def do_POST(self):
        if self.path == '/login':
            self._handle_login()
            return
        if self.path.startswith('/shared-rotate/'):
            token = self.path.split('?', 1)[0].strip('/').split('/', 1)[1]
            self._handle_shared_rotate(token)
            return

        sess, _ = get_session(self.headers)
        if not sess:
            self._json({'error': 'No autorizado'}, 401)
            return

        if self.path == '/api/download-version':
            self._handle_download(sess)
            return
        if self.path == '/api/admin/save-user':
            self._admin_save_user(sess)
            return
        if self.path == '/api/admin/delete-user':
            self._admin_delete_user(sess)
            return
        if self.path == '/api/admin/create-shared-link':
            self._admin_create_shared_link(sess)
            return
        if self.path == '/api/admin/update-shared-link':
            self._admin_update_shared_link(sess)
            return
        if self.path == '/api/rotate-image':
            self._handle_gallery_rotate(sess)
            return
        if self.path == '/api/favorite-toggle':
            self._toggle_favorite(sess)
            return
        if self.path == '/api/admin/create-instagram-pack':
            self._admin_create_instagram_pack(sess)
            return
        if self.path == '/api/admin/save-project-billing':
            self._admin_save_project_billing(sess)
            return
        if self.path == '/api/admin/save-social-project':
            self._admin_save_social_project(sess)
            return
        if self.path == '/api/reindex':
            if sess['role'] != 'admin':
                self._json({'error': 'No autorizado'}, 403)
                return
            schedule_reindex(delay=0)
            self._json({'scheduled': True})
            return
        if self.path == '/export/start':
            if sess['role'] != 'admin':
                self._json({'error': 'No autorizado'}, 403)
                return
            length = int(self.headers.get('Content-Length', 0))
            body   = json.loads(self.rfile.read(length))
            if export_status['running']:
                self._json({'error': 'Ya hay una exportación en curso'}, 409)
                return
            output_dir = body.get('output_dir', '').strip()
            if not output_dir:
                self._json({'error': 'Carpeta destino vacía'}, 400)
                return
            projects    = body.get('projects', [])
            max_px      = int(body.get('max_px', 1920))
            quality     = int(body.get('quality', 82))
            scope_label = body.get('scope_label', 'Portfolio')
            t = threading.Thread(target=run_export,
                                 args=(output_dir, projects, max_px, quality, scope_label),
                                 daemon=True)
            t.start()
            total = sum(project_image_count(p) for p in projects)
            self._json({'started': True, 'total': total})
        elif self.path == '/export/cancel':
            with export_lock:
                export_status['running'] = False
            self._json({'cancelled': True})
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass


import socket as _socket
_local_ip = _socket.gethostbyname(_socket.gethostname())

print(f"Galería disponible en:")
print(f"  Local  -> http://localhost:{PORT}")
print(f"  Red    -> http://{_local_ip}:{PORT}")
print("Ctrl+C para detener.\n")

start_watcher()

# Reindex on startup if data.js is missing, has old absolute localhost URLs,
# or does not include the optional secondary trabajos mount.
_data_js = os.path.join(GALLERY_DIR, 'data.js')
_needs_reindex = True
try:
    with open(_data_js, 'r', encoding='utf-8') as _f:
        _data_js_content = _f.read()
        _needs_reindex = (
            'localhost' in _data_js_content[:1024]
            or (bool(TRABAJOS_DIR2) and '/trabajos2/' not in _data_js_content)
        )
except OSError:
    pass
if _needs_reindex:
    print('[startup] data.js necesita reindexado - reindexando...', flush=True)
    schedule_reindex(delay=3)

if OPEN_BROWSER:
    import webbrowser
    threading.Timer(1.0, lambda: webbrowser.open(f'http://localhost:{PORT}')).start()

ThreadingHTTPServer(('', PORT), Handler).serve_forever()
