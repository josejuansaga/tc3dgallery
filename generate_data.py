import os
import json
import re
import shutil
from urllib.parse import quote, unquote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Paths (env vars override hardcoded defaults) ──────────────────────────────
TRABAJOS_DIR  = os.environ.get('TRABAJOS_DIR',  r"Z:\OneDriveTC3D\Trabajos")
TRABAJOS_DIR2 = os.environ.get('TRABAJOS_DIR2', '')   # carpeta adicional opcional
RENDERS_DIR   = os.path.join(TRABAJOS_DIR, "Renders")
OUTPUT_JS     = os.environ.get('OUTPUT_JS',    os.path.join(BASE_DIR, 'data.js'))
IMAGE_EXTS    = {'.jpg', '.jpeg', '.png', '.webp'}
HTTP_BASE     = '/trabajos'    # relative — works on any host/port
HTTP_BASE2    = '/trabajos2'

# Year folders to scan in phase 2
YEAR_RE   = re.compile(r'^20\d{2}$')
# Non-project entries at the year-folder level
SKIP_NAMES = {
    '0 excels', 'docs antiguos', 'portfolio', 'wetransfer', 'web - datos',
    'proxy', 'texturas', '1 - documentos cliente', '2 - imagenes referencia',
    '3 - cad', '4 - pdf', '5 - fotos',
}

FINAL_PHOTO_DIRS = {'fotos', '5 - fotos', 'fotos finales', 'fotos final', 'finales'}
OPTION_PATTERN = re.compile(r'(?<![a-z0-9])(?:op(?:cion)?|option)\s*[-_ ]*0*(\d+)(?![a-z0-9])', re.IGNORECASE)


# ── URL helpers ───────────────────────────────────────────────────────────────
def path_to_url(path):
    norm = os.path.normpath(path)
    if TRABAJOS_DIR2 and norm.startswith(os.path.normpath(TRABAJOS_DIR2)):
        rel = os.path.relpath(path, TRABAJOS_DIR2).replace('\\', '/')
        return HTTP_BASE2 + '/' + quote(rel, safe='/')
    rel = os.path.relpath(path, TRABAJOS_DIR).replace('\\', '/')
    return HTTP_BASE + '/' + quote(rel, safe='/')


# ── HQ / skip detection ───────────────────────────────────────────────────────
VR_PATTERN   = re.compile(r'\bVR\b|Virtual|Tour', re.IGNORECASE)
SKIP_SUBDIR  = re.compile(r'^elements$|^proxy$', re.IGNORECASE)  # render passes etc.


def is_hq_dir(name):
    if not re.match(r'HQ', name, re.IGNORECASE):
        return False
    if VR_PATTERN.search(name):
        return False
    return True


def should_skip_subdir(name):
    return bool(VR_PATTERN.search(name) or SKIP_SUBDIR.match(name))


def is_final_photos_dir(name):
    return name.strip().lower() in FINAL_PHOTO_DIRS


def contains_p2vr(path):
    try:
        return any(f.lower().endswith('.p2vr') for f in os.listdir(path))
    except OSError:
        return False


# ── Sorting / classification ──────────────────────────────────────────────────
def hq_sort_key(name):
    m_num  = re.search(r'HQ\s*(\d+)', name, re.IGNORECASE)
    m_date = re.search(r'(\d{4})[.\-](\d{2})[.\-](\d{2})', name)
    num    = int(m_num.group(1)) if m_num else 0
    date   = m_date.group(0).replace('.', '-') if m_date else '0000-00-00'
    return (date, num)


def extract_year(hq_label, fallback_year=None):
    for m in re.finditer(r'(\d{4})', hq_label):
        y = int(m.group(1))
        if 2015 <= y <= 2030:
            return y
    return fallback_year


def extract_date(hq_label):
    """Return YYYY-MM-DD from HQ folder name, or None."""
    m = re.search(r'(\d{4})[.\-](\d{2})[.\-](\d{2})', hq_label)
    return m.group(0).replace('.', '-') if m else None


TYPE_RULES = [
    ('Exterior',          ['exterior', 'fachada', 'frontal', 'pano', 'panoram', 'porche', 'perg', 'azotea']),
    ('Salón / SCC',       ['salon', 'scc', 'ssc', 'comedor', 'living', 'sala ']),
    ('Cocina',            ['cocina', 'kitchen']),
    ('Baño / Aseo',       ['bano', 'aseo', 'jacuzzi', 'ducha', 'lavabo']),
    ('Dormitorio',        ['dorm', 'dormitorio', 'habitacion', 'tatami', 'vestidor', 'alcoba', 'suite']),
    ('Terraza / Piscina', ['terraza', 'piscina', 'patio ', 'jardin', 'sotano', 'spa']),
    ('Oficina',           ['oficina', 'despacho', 'reunion', 'trabajo', 'espera', 'cowork', 'meeting']),
    ('Restaurante / Bar', ['restaurante', 'food', 'foodcorner', 'cafet', ' bar ', 'cafeteria', 'baco']),
    ('Deporte',           ['padel', 'gym', 'gimnasio', 'fitness', 'ecogym', 'pistas', 'american padel', 'padel creation']),
    ('Parking',           ['parking', 'garaje']),
]


def detect_types(project_name, company_name, all_filenames):
    haystack = ' ' + (project_name + ' ' + company_name).lower() + ' '
    stems = set()
    for fname in all_filenames:
        stem = re.sub(r'\.\d{4}$', '', os.path.splitext(os.path.basename(fname))[0])
        stems.add(stem.lower())
    haystack += ' '.join(stems)
    return [label for label, kws in TYPE_RULES if any(kw in haystack for kw in kws)]


def detect_option_label(image_url):
    filename = os.path.basename(unquote(image_url))
    stem = re.sub(r'\.\d{4}$', '', os.path.splitext(filename)[0])
    match = OPTION_PATTERN.search(stem)
    if not match:
        return None
    return f'OP{int(match.group(1))}'


def build_option_groups(images):
    grouped = []
    grouped_map = {}
    general = []
    for image_url in images:
        label = detect_option_label(image_url)
        if not label:
            general.append(image_url)
            continue
        group = grouped_map.get(label)
        if group is None:
            group = {'label': label, 'images': []}
            grouped_map[label] = group
            grouped.append(group)
        group['images'].append(image_url)
    if general and grouped:
        grouped.insert(0, {'label': 'General', 'images': general})
    return grouped if grouped else []


# ── Core: collect images from HQ dirs under a given project path ──────────────
def collect_versions(project_path, fallback_year=None):
    """Return list of version dicts sorted newest-first, or [] if none."""
    try:
        entries = os.listdir(project_path)
    except OSError:
        return []

    hq_dirs = sorted(
        [d for d in entries if is_hq_dir(d)],
        key=hq_sort_key
    )

    versions = []
    for hq_name in hq_dirs:
        hq_path = os.path.join(project_path, hq_name)
        if contains_p2vr(hq_path):
            continue
        images = []

        for root, dirs, files in os.walk(hq_path):
            dirs[:] = [
                d for d in dirs
                if not should_skip_subdir(d) and not contains_p2vr(os.path.join(root, d))
            ]
            if any(f.lower().endswith('.p2vr') for f in files):
                dirs[:] = []
                continue

            for f in sorted(files):
                if os.path.splitext(f)[1].lower() in IMAGE_EXTS:
                    images.append(path_to_url(os.path.join(root, f)))

        if images:
            version = {'label': hq_name, 'images': images}
            option_groups = build_option_groups(images)
            if option_groups:
                version['option_groups'] = option_groups
            versions.append(version)

    versions.reverse()  # newest first
    return versions


def collect_final_photos(project_path):
    """Return images found inside a project-level Fotos folder."""
    photos = []
    try:
        entries = os.listdir(project_path)
    except OSError:
        return photos

    photo_dirs = [
        os.path.join(project_path, entry)
        for entry in entries
        if is_final_photos_dir(entry) and os.path.isdir(os.path.join(project_path, entry))
    ]

    for photo_dir in photo_dirs:
        for root, dirs, files in os.walk(photo_dir):
            dirs[:] = [
                d for d in dirs
                if not should_skip_subdir(d) and not contains_p2vr(os.path.join(root, d))
            ]
            if any(f.lower().endswith('.p2vr') for f in files):
                dirs[:] = []
                continue
            for f in sorted(files):
                if os.path.splitext(f)[1].lower() in IMAGE_EXTS:
                    photos.append(path_to_url(os.path.join(root, f)))

    return photos


def make_project(name, company, versions, fallback_year=None, project_path=None):
    final_photos = collect_final_photos(project_path) if project_path else []
    all_images = [img for v in versions for img in v['images']] + final_photos
    return {
        'name':     name,
        'versions': versions,
        'final_photos': final_photos,
        'types':    detect_types(name, company, all_images),
        'year':     extract_year(versions[0]['label'], fallback_year),
        'date':     extract_date(versions[0]['label']),
    }


# ── Phase 1: Renders folder ───────────────────────────────────────────────────
data = []               # list of {company, projects[]}
seen = set()            # (company_lower, project_lower) already indexed
total_projects = 0
total_images   = 0

print("Phase 1: scanning Renders/ ...")
if os.path.isdir(RENDERS_DIR):
    for company in sorted(os.listdir(RENDERS_DIR)):
        company_path = os.path.join(RENDERS_DIR, company)
        if not os.path.isdir(company_path):
            continue

        projects = []
        for project in sorted(os.listdir(company_path)):
            project_path = os.path.join(company_path, project)
            if not os.path.isdir(project_path):
                continue

            versions = collect_versions(project_path)
            if not versions:
                continue

            proj = make_project(project, company, versions, project_path=project_path)
            projects.append(proj)
            seen.add((company.lower(), project.lower()))
            total_images   += sum(len(v['images']) for v in versions) + len(proj.get('final_photos', []))
            total_projects += 1

        if projects:
            data.append({'company': company, 'projects': projects})
else:
    print(f"  skipped: {RENDERS_DIR} not found")

# Index for fast company lookup
company_index = {entry['company']: entry for entry in data}

print(f"  {total_projects} projects, {total_images} images")


# ── Phase 2: Year folders ─────────────────────────────────────────────────────
print("Phase 2: scanning year folders ...")
new_projects = 0
new_images   = 0

for year_dir in sorted(os.listdir(TRABAJOS_DIR)):
    if not YEAR_RE.match(year_dir):
        continue
    year_int = int(year_dir)
    year_path = os.path.join(TRABAJOS_DIR, year_dir)

    for company in sorted(os.listdir(year_path)):
        if company.lower() in SKIP_NAMES:
            continue
        company_path = os.path.join(year_path, company)
        if not os.path.isdir(company_path):
            continue

        for project in sorted(os.listdir(company_path)):
            if project.lower() in SKIP_NAMES:
                continue
            project_path = os.path.join(company_path, project)
            if not os.path.isdir(project_path):
                continue

            # Skip if already indexed from Renders
            if (company.lower(), project.lower()) in seen:
                continue

            # HQ dirs can be directly in project, or inside a "Render HQ" subfolder
            render_hq = os.path.join(project_path, 'Render HQ')
            scan_path = render_hq if os.path.isdir(render_hq) else project_path

            versions = collect_versions(scan_path, fallback_year=year_int)
            if not versions:
                continue

            proj = make_project(project, company, versions, fallback_year=year_int, project_path=project_path)

            # Add to existing company entry or create new one
            if company in company_index:
                company_index[company]['projects'].append(proj)
            else:
                entry = {'company': company, 'projects': [proj]}
                data.append(entry)
                company_index[company] = entry

            seen.add((company.lower(), project.lower()))
            new_projects += 1
            new_images   += sum(len(v['images']) for v in versions) + len(proj.get('final_photos', []))

print(f"  {new_projects} new projects, {new_images} new images")

# ── Phase 3: TRABAJOS_DIR2 year folders ───────────────────────────────────────
if TRABAJOS_DIR2 and os.path.isdir(TRABAJOS_DIR2):
    print("Phase 3: scanning TRABAJOS_DIR2 year folders ...")
    p3_projects = 0
    p3_images   = 0

    for year_dir in sorted(os.listdir(TRABAJOS_DIR2)):
        if not YEAR_RE.match(year_dir):
            continue
        year_int  = int(year_dir)
        year_path = os.path.join(TRABAJOS_DIR2, year_dir)

        for company in sorted(os.listdir(year_path)):
            if company.lower() in SKIP_NAMES:
                continue
            company_path = os.path.join(year_path, company)
            if not os.path.isdir(company_path):
                continue

            for project in sorted(os.listdir(company_path)):
                if project.lower() in SKIP_NAMES:
                    continue
                project_path = os.path.join(company_path, project)
                if not os.path.isdir(project_path):
                    continue

                if (company.lower(), project.lower()) in seen:
                    continue

                render_hq = os.path.join(project_path, 'Render HQ')
                scan_path = render_hq if os.path.isdir(render_hq) else project_path

                versions = collect_versions(scan_path, fallback_year=year_int)
                if not versions:
                    continue

                proj = make_project(project, company, versions, fallback_year=year_int, project_path=project_path)

                if company in company_index:
                    company_index[company]['projects'].append(proj)
                else:
                    entry = {'company': company, 'projects': [proj]}
                    data.append(entry)
                    company_index[company] = entry

                seen.add((company.lower(), project.lower()))
                p3_projects += 1
                p3_images   += sum(len(v['images']) for v in versions) + len(proj.get('final_photos', []))

    print(f"  {p3_projects} new projects, {p3_images} new images")
    new_projects += p3_projects
    new_images   += p3_images

# Sort projects within each company alphabetically
for entry in data:
    entry['projects'].sort(key=lambda p: p['name'].lower())

total_projects += new_projects
total_images   += new_images

if total_projects == 0 and (os.path.isdir(TRABAJOS_DIR) or (TRABAJOS_DIR2 and os.path.isdir(TRABAJOS_DIR2))):
    raise SystemExit(
        "No se han encontrado proyectos. Reindexado cancelado para no sobrescribir data.js con una galería vacía."
    )

if os.path.isfile(OUTPUT_JS) and os.path.getsize(OUTPUT_JS) > 100000:
    shutil.copy2(OUTPUT_JS, OUTPUT_JS + '.bak')

with open(OUTPUT_JS, 'w', encoding='utf-8') as f:
    f.write('const GALLERY_DATA = ')
    json.dump(data, f, ensure_ascii=False)
    f.write(';\n')

print(f"\nDone: {len(data)} companies, {total_projects} projects, {total_images} images")
print(f"Written to {OUTPUT_JS}")
