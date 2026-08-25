import os
import json
from flask import Flask, render_template, redirect, url_for, request, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_wtf.csrf import CSRFProtect, CSRFError
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from groq import Groq

app = Flask(__name__)

# ---- SECRET KEY: nunca hardcodeada en producción ----
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-only-insecure-key-change-me')

# ---- BASE DE DATOS: usa Postgres persistente si existe DATABASE_URL (Render),
# cae a SQLite local solo para desarrollo en tu computador ----
database_url = os.getenv('DATABASE_URL')
if database_url and database_url.startswith('postgres://'):
    # Render entrega el URI viejo, SQLAlchemy 2.x necesita el prefijo postgresql://
    database_url = database_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url or 'sqlite:///local_dev.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Protección CSRF en todos los formularios POST.
# Sin límite de tiempo: un estudiante puede dejar el panel abierto toda la tarde
# y el token seguiría siendo válido mientras dure la sesión.
app.config['WTF_CSRF_TIME_LIMIT'] = None
csrf = CSRFProtect(app)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# ---- Groq (límite gratuito diario mucho más alto que Gemini) ----
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

SUBJECTS = ["Math", "Language Arts", "Lenguaje", "Geometría", "Global Perspectives",
            "Sociales", "Biología", "Física", "Química", "Computer Science"]
TASK_TYPES = ["Tarea", "Examen", "Proyecto", "Extracurricular"]

# ---- Fechas en español (el servidor corre con locale C, así que no
# dependemos de strftime para los nombres de días y meses) ----
WEEKDAYS_ES = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
MONTHS_ES = ["ene", "feb", "mar", "abr", "may", "jun",
             "jul", "ago", "sep", "oct", "nov", "dic"]


def subjects_for(user):
    """Materias del estudiante; si no configuró ninguna, usa la lista del colegio."""
    if user and user.subjects:
        propias = [ln.strip() for ln in user.subjects.splitlines() if ln.strip()]
        if propias:
            return propias
    return SUBJECTS


def format_date_es(value):
    """25 ago 2026"""
    if not value:
        return ""
    return f"{value.day} {MONTHS_ES[value.month - 1]} {value.year}"


def format_day_month_es(value):
    """25 ago"""
    if not value:
        return ""
    return f"{value.day} {MONTHS_ES[value.month - 1]}"


app.jinja_env.filters['fecha'] = format_date_es


# ---- MODELOS ----
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    subjects = db.Column(db.Text, nullable=True)  # una materia por línea; vacío = lista CAS
    tasks = db.relationship('Task', backref='student', lazy=True, cascade="all, delete-orphan")


class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    subject = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(50), nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    notes = db.Column(db.Text, nullable=True)
    completed = db.Column(db.Boolean, default=False)
    hours = db.Column(db.Float, nullable=True)  # horas estimadas de trabajo
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    flash('Tu sesión expiró o el formulario no era válido. Inténtalo de nuevo.', 'error')
    if current_user.is_authenticated:
        return redirect(url_for('dashboard')), 302
    return redirect(url_for('login')), 302


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ---- AUTENTICACIÓN ----
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password, password):
            login_user(user, remember=True)
            return redirect(url_for('dashboard'))

        flash('Credenciales incorrectas. Verifica tu correo y contraseña.', 'error')
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        if not username or not email or not password:
            flash('Todos los campos son obligatorios.', 'error')
            return render_template('register.html')

        user_exists = User.query.filter((User.email == email) | (User.username == username)).first()
        if user_exists:
            flash('El usuario o correo ya está registrado.', 'error')
            return render_template('register.html')

        hashed_password = generate_password_hash(password, method='scrypt')
        new_user = User(username=username, email=email, password=hashed_password)

        db.session.add(new_user)
        db.session.commit()

        flash('Registro exitoso. Ya puedes iniciar sesión.', 'success')
        return redirect(url_for('login'))

    return render_template('register.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# ---- DASHBOARD ----
def build_weekly_radar(all_pending_tasks):
    """Construye un radar de 7 días (hoy + 6) con el nivel de carga por día."""
    today = datetime.now().date()
    radar = []
    for i in range(7):
        day = today + timedelta(days=i)
        day_tasks = [t for t in all_pending_tasks if t.due_date == day]
        exams = sum(1 for t in day_tasks if t.type == 'Examen')
        others = len(day_tasks) - exams

        # Carga en horas. Si un deber no tiene estimación, asumimos 1h
        # (2h si es examen) para no subestimar el día.
        load = 0.0
        for t in day_tasks:
            if t.hours:
                load += t.hours
            else:
                load += 2.0 if t.type == 'Examen' else 1.0

        if exams >= 1 or load >= 4:
            level = 'critico'
        elif load >= 2:
            level = 'medio'
        elif load > 0:
            level = 'bajo'
        else:
            level = 'libre'

        radar.append({
            'date': day,
            'label': WEEKDAYS_ES[day.weekday()],
            'day_num': day.strftime('%d'),
            'exams': exams,
            'tasks': others,
            'load': round(load, 1),
            'level': level,
            'is_today': i == 0,
        })
    return radar


@app.route('/dashboard', methods=['GET', 'POST'])
@login_required
def dashboard():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        subject = request.form.get('subject')
        task_type = request.form.get('type')
        due_date_str = request.form.get('due_date')
        notes = request.form.get('notes', '').strip()

        hours = None
        hours_raw = (request.form.get('hours') or '').strip().replace(',', '.')
        if hours_raw:
            try:
                hours = max(0.0, min(float(hours_raw), 24.0))
            except ValueError:
                hours = None

        if title and subject and task_type and due_date_str:
            due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
            new_task = Task(title=title, subject=subject, type=task_type, hours=hours,
                             due_date=due_date, notes=notes, student=current_user)
            db.session.add(new_task)
            db.session.commit()
            flash('Deber añadido correctamente.', 'success')
        else:
            flash('Completa título, materia, tipo y fecha límite.', 'error')
        return redirect(url_for('dashboard'))

    tasks = Task.query.filter_by(user_id=current_user.id, completed=False)\
        .order_by(Task.due_date).all()

    radar = build_weekly_radar(tasks)

    critical_alerts = []
    for day in radar:
        if day['level'] == 'critico':
            formatted_date = format_day_month_es(day['date'])
            pieces = []
            if day['exams']:
                pieces.append(f"{day['exams']} examen" + ("es" if day['exams'] > 1 else ""))
            if day['tasks']:
                pieces.append(f"{day['tasks']} entrega" + ("s" if day['tasks'] > 1 else ""))
            if day['load']:
                pieces.append(f"~{day['load']:g} h")
            critical_alerts.append(f"{formatted_date}: {' y '.join(pieces)}")

    today = datetime.now().date()
    overdue_count = sum(1 for t in tasks if t.due_date < today)

    # Lo que cae más allá de la semana visible. Sin esto, un proyecto a tres
    # semanas es invisible hasta que ya es tarde.
    fin_semana = today + timedelta(days=6)
    lejanos = [t for t in tasks if t.due_date > fin_semana]
    horizon = None
    if lejanos:
        carga = sum(t.hours if t.hours else (2.0 if t.type == 'Examen' else 1.0) for t in lejanos)
        proximo = min(t.due_date for t in lejanos)
        horizon = {
            'count': len(lejanos),
            'exams': sum(1 for t in lejanos if t.type == 'Examen'),
            'load': round(carga, 1),
            'next_date': proximo,
            'next_in': (proximo - today).days,
        }

    done_tasks = Task.query.filter_by(user_id=current_user.id, completed=True)\
        .order_by(Task.due_date.desc()).limit(15).all()

    return render_template('dashboard.html', tasks=tasks, radar=radar, today=today,
                            overdue_count=overdue_count, done_tasks=done_tasks, horizon=horizon,
                            alerts=critical_alerts, subjects=subjects_for(current_user),
                            my_subjects=current_user.subjects or "", task_types=TASK_TYPES)


@app.route('/materias', methods=['POST'])
@login_required
def update_subjects():
    texto = request.form.get('subjects', '')
    limpias = [ln.strip() for ln in texto.splitlines() if ln.strip()][:30]
    current_user.subjects = "\n".join(limpias) if limpias else None
    db.session.commit()
    if limpias:
        flash(f'Guardaste {len(limpias)} materia{"s" if len(limpias) > 1 else ""}.', 'success')
    else:
        flash('Volviste a la lista de materias del colegio.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/complete/<int:task_id>', methods=['POST'])
@login_required
def complete_task(task_id):
    task = db.session.get(Task, task_id)
    if task and task.user_id == current_user.id:
        task.completed = True
        db.session.commit()
        flash(f'"{task.title}" completado. Puedes reabrirlo desde "Completados".', 'success')
    return redirect(url_for('dashboard'))


@app.route('/reabrir/<int:task_id>', methods=['POST'])
@login_required
def reopen_task(task_id):
    task = db.session.get(Task, task_id)
    if task and task.user_id == current_user.id:
        task.completed = False
        db.session.commit()
        flash(f'"{task.title}" volvió a tus pendientes.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/editar/<int:task_id>', methods=['POST'])
@login_required
def edit_task(task_id):
    task = db.session.get(Task, task_id)
    if not task or task.user_id != current_user.id:
        flash('No encontramos ese deber.', 'error')
        return redirect(url_for('dashboard'))

    title = request.form.get('title', '').strip()
    subject = request.form.get('subject')
    task_type = request.form.get('type')
    due_date_str = request.form.get('due_date')

    if not (title and subject and task_type and due_date_str):
        flash('Completa título, materia, tipo y fecha límite.', 'error')
        return redirect(url_for('dashboard'))

    try:
        due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
    except ValueError:
        flash('La fecha no es válida.', 'error')
        return redirect(url_for('dashboard'))

    hours = None
    hours_raw = (request.form.get('hours') or '').strip().replace(',', '.')
    if hours_raw:
        try:
            hours = max(0.0, min(float(hours_raw), 24.0))
        except ValueError:
            hours = None

    task.title = title
    task.subject = subject
    task.type = task_type
    task.due_date = due_date
    task.hours = hours
    task.notes = request.form.get('notes', '').strip()
    db.session.commit()
    flash(f'"{task.title}" actualizado.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/delete/<int:task_id>', methods=['POST'])
@login_required
def delete_task(task_id):
    task = db.session.get(Task, task_id)
    if task and task.user_id == current_user.id:
        db.session.delete(task)
        db.session.commit()
    return redirect(url_for('dashboard'))


# ---- IA: DESGLOSADOR ----
def generate_plan(raw_input):
    """Pide el plan a Groq. Devuelve (days_plan, mensaje_de_error)."""
    if not groq_client:
        return None, ("<p>No hay una API Key de Groq configurada en este entorno. Define la "
                      "variable de entorno GROQ_API_KEY para activar el Desglosador IA.</p>")

    system_prompt = (
        "Actúas como un tutor experto en productividad y organización académica para el "
        "Colegio Colombo Americano (CAS). El usuario te va a pasar un texto confuso o largo con "
        "una o múltiples tareas escolares. Analízalo y responde ÚNICAMENTE con un objeto JSON "
        "válido (sin texto antes o después, sin ```json, sin comentarios), con exactamente esta "
        "forma:\n"
        '{"subtitle": "COLEGIO COLOMBO AMERICANO", "title": "título corto y sofisticado del plan '
        '(máx 6 palabras)", "intro": "1-2 frases explicando el volumen de trabajo y el enfoque '
        'elegido", "effort_level": <entero 1-5>, "days": [{"day_number": <entero>, "day_title": '
        '"título corto del enfoque de ese día (máx 5 palabras)", "tasks": [{"subject": "materia", '
        '"task": "qué hacer exactamente, concreto y accionable"}]}], "technique_title": "nombre de '
        'la técnica de estudio recomendada", "technique_description": "1-3 frases explicando cómo '
        'aplicarla a este caso específico", "closing_quote": "frase corta, motivacional, '
        'sofisticada y con liderazgo"}\n'
        "Distribuye el trabajo en máximo 5 días. Sé específico y concreto en cada tarea, evita "
        "generalidades. El intro y closing_quote deben sonar elegantes y con autoridad."
    )
    try:
        completion = groq_client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Texto del estudiante:\n{raw_input}"},
            ],
            temperature=0.6,
            max_tokens=2000,
            response_format={"type": "json_object"},
        )
        return json.loads(completion.choices[0].message.content), None
    except Exception as e:
        return None, f"<p>Error al generar el plan con Groq: {str(e)}</p>"


@app.route('/ai-tutor', methods=['GET', 'POST'])
@login_required
def ai_tutor():
    ai_response = None
    days_plan = None
    raw_input = ""
    if request.method == 'POST':
        raw_input = request.form.get('raw_input', '')
        days_plan, ai_response = generate_plan(raw_input)

    return render_template('ai_tutor.html', ai_response=ai_response, days_plan=days_plan, raw_input=raw_input)


@app.route('/organizar-semana', methods=['POST'])
@login_required
def organize_week():
    """Toma los pendientes del panel y se los pasa al Desglosador."""
    pending = Task.query.filter_by(user_id=current_user.id, completed=False)\
        .order_by(Task.due_date).all()

    if not pending:
        flash('No tienes deberes pendientes para organizar.', 'error')
        return redirect(url_for('dashboard'))

    today = datetime.now().date()
    lineas = ["Estos son mis deberes pendientes. Organízame un plan de estudio."]
    for t in pending:
        dias = (t.due_date - today).days
        if dias < 0:
            cuando = f"VENCIDO hace {abs(dias)} día(s)"
        elif dias == 0:
            cuando = "vence HOY"
        elif dias == 1:
            cuando = "vence mañana"
        else:
            cuando = f"vence en {dias} días"
        linea = f"- [{t.type}] {t.subject}: {t.title} ({cuando}, {format_date_es(t.due_date)}"
        if t.hours:
            linea += f", ~{t.hours:g} h estimadas"
        linea += ")"
        if t.notes:
            linea += f" Notas: {t.notes}"
        lineas.append(linea)

    raw_input = "\n".join(lineas)
    days_plan, ai_response = generate_plan(raw_input)

    return render_template('ai_tutor.html', ai_response=ai_response,
                           days_plan=days_plan, raw_input=raw_input)


@app.route('/ai-tutor/importar', methods=['POST'])
@login_required
def import_plan():
    """Convierte los ítems seleccionados de un plan generado en deberes reales."""
    selected = request.form.getlist('seleccion')
    if not selected:
        flash('No seleccionaste ninguna actividad del plan.', 'error')
        return redirect(url_for('ai_tutor'))

    try:
        plan = json.loads(request.form.get('plan_json', ''))
        days = plan.get('days', [])
    except (ValueError, AttributeError):
        flash('No se pudo leer el plan. Genéralo de nuevo.', 'error')
        return redirect(url_for('ai_tutor'))

    today = datetime.now().date()
    created = 0

    for ref in selected:
        try:
            d_idx, t_idx = (int(n) for n in ref.split('-'))
            item = days[d_idx]['tasks'][t_idx]
        except (ValueError, IndexError, KeyError, TypeError):
            continue

        # "Día 1" es hoy, "Día 2" mañana, y así sucesivamente.
        try:
            offset = max(int(days[d_idx].get('day_number', d_idx + 1)) - 1, 0)
        except (ValueError, TypeError):
            offset = d_idx

        subject = (item.get('subject') or '').strip()
        # Respetamos la materia sugerida solo si existe en la lista del colegio.
        mis = subjects_for(current_user)
        match = next((s for s in mis if s.lower() == subject.lower()), None)

        title = (item.get('task') or '').strip()
        if not title:
            continue

        db.session.add(Task(
            title=title[:200],
            subject=match or subject[:100] or mis[0],
            type='Tarea',
            due_date=today + timedelta(days=offset),
            notes=f"Generado por el Desglosador IA · {plan.get('title', 'Plan de trabajo')}",
            user_id=current_user.id,
        ))
        created += 1

    if not created:
        flash('No se pudo añadir ninguna actividad del plan.', 'error')
        return redirect(url_for('ai_tutor'))

    db.session.commit()
    flash(f"{created} actividad{'es' if created > 1 else ''} añadida{'s' if created > 1 else ''} a tu panel.", 'success')
    return redirect(url_for('dashboard'))


def ensure_schema():
    """Añade columnas nuevas si faltan. Idempotente: se puede correr en cada arranque.

    SQLAlchemy solo crea tablas que no existen; nunca altera las existentes. Como
    la base de producción ya tiene datos, las columnas nuevas hay que añadirlas a
    mano. Ojo: en Postgres 'user' es palabra reservada y debe ir entre comillas.
    """
    from sqlalchemy import inspect, text

    pendientes = [
        ('user', 'subjects', 'TEXT'),
        ('task', 'hours', 'FLOAT'),
    ]

    inspector = inspect(db.engine)
    tablas = set(inspector.get_table_names())

    for tabla, columna, tipo in pendientes:
        if tabla not in tablas:
            continue  # create_all la creará ya completa
        existentes = {c['name'] for c in inspector.get_columns(tabla)}
        if columna in existentes:
            continue
        try:
            db.session.execute(text(f'ALTER TABLE "{tabla}" ADD COLUMN {columna} {tipo}'))
            db.session.commit()
            print(f"[schema] columna añadida: {tabla}.{columna}", flush=True)
        except Exception as e:
            db.session.rollback()
            print(f"[schema] no se pudo añadir {tabla}.{columna}: {e}", flush=True)


with app.app_context():
    db.create_all()
    ensure_schema()

if __name__ == '__main__':
    app.run(debug=True)
