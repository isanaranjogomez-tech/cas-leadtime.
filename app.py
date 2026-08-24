import os
import json
from flask import Flask, render_template, redirect, url_for, request, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
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
    tasks = db.relationship('Task', backref='student', lazy=True, cascade="all, delete-orphan")


class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    subject = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(50), nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    notes = db.Column(db.Text, nullable=True)
    completed = db.Column(db.Boolean, default=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)


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

        if exams >= 1 or others >= 4:
            level = 'critico'
        elif others >= 2:
            level = 'medio'
        elif others >= 1:
            level = 'bajo'
        else:
            level = 'libre'

        radar.append({
            'date': day,
            'label': WEEKDAYS_ES[day.weekday()],
            'day_num': day.strftime('%d'),
            'exams': exams,
            'tasks': others,
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

        if title and subject and task_type and due_date_str:
            due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
            new_task = Task(title=title, subject=subject, type=task_type,
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
            critical_alerts.append(f"{formatted_date}: {' y '.join(pieces)}")

    return render_template('dashboard.html', tasks=tasks, radar=radar,
                            alerts=critical_alerts, subjects=SUBJECTS, task_types=TASK_TYPES)


@app.route('/complete/<int:task_id>', methods=['POST'])
@login_required
def complete_task(task_id):
    task = db.session.get(Task, task_id)
    if task and task.user_id == current_user.id:
        task.completed = True
        db.session.commit()
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
@app.route('/ai-tutor', methods=['GET', 'POST'])
@login_required
def ai_tutor():
    ai_response = None
    days_plan = None
    raw_input = ""
    if request.method == 'POST':
        raw_input = request.form.get('raw_input', '')

        if not groq_client:
            ai_response = ("<p>No hay una API Key de Groq configurada en este entorno. Define la "
                            "variable de entorno GROQ_API_KEY para activar el Desglosador IA.</p>")
        else:
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
                raw_json = completion.choices[0].message.content
                days_plan = json.loads(raw_json)
            except Exception as e:
                ai_response = f"<p>Error al generar el plan con Groq: {str(e)}</p>"

    return render_template('ai_tutor.html', ai_response=ai_response, days_plan=days_plan, raw_input=raw_input)


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
        match = next((s for s in SUBJECTS if s.lower() == subject.lower()), None)

        title = (item.get('task') or '').strip()
        if not title:
            continue

        db.session.add(Task(
            title=title[:200],
            subject=match or subject[:100] or SUBJECTS[0],
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


with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True)
