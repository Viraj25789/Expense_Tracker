import os
import io
import base64
import calendar
from datetime import date, datetime, timedelta

from flask import Flask, render_template, request, url_for, flash, redirect, Response
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

# Matplotlib setup for server (no GUI)
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from weasyprint import HTML

app = Flask(__name__)

# --- Configuration ---
basedir = os.path.abspath(os.path.dirname(__file__))
instance_path = os.path.join(basedir, 'instance')
if not os.path.exists(instance_path):
    os.makedirs(instance_path)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(instance_path, 'expenses.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = 'change-this-secret-key-for-production'

db = SQLAlchemy(app)

# --- Login Manager Setup ---
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# --- Database Models ---

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    expenses = db.relationship('Expense', backref='owner', lazy=True)

class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(120), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(50), nullable=False)
    date = db.Column(db.Date, nullable=False, default=date.today)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

class Budget(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(50), nullable=False)
    limit = db.Column(db.Float, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

# Create Database Tables
with app.app_context():
    db.create_all()

# --- Helpers & Constants ---
CATEGORIES = ['Food', 'Transport', 'Rent', 'Utilities', 'Health', 'Other']

def parse_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None

def get_filtered_query(query, start, end, category):
    # Only show data for the logged-in user
    query = query.filter(Expense.user_id == current_user.id)
    if start:
        query = query.filter(Expense.date >= start)
    if end:
        query = query.filter(Expense.date <= end)
    if category:
        query = query.filter(Expense.category == category)
    return query

def ai_categorize(description):
    description = description.lower()
    rules = {
        'Food': ['burger', 'pizza', 'coffee', 'groceries', 'dinner', 'lunch', 'breakfast', 'snack', 'restaurant'],
        'Transport': ['uber', 'bus', 'fuel', 'gas', 'petrol', 'train', 'ticket', 'taxi'],
        'Rent': ['rent', 'house', 'apartment', 'mortgage'],
        'Utilities': ['electric', 'water', 'bill', 'internet', 'wifi', 'phone', 'mobile'],
        'Health': ['doctor', 'pharmacy', 'medicine', 'gym', 'hospital', 'dental'],
        'Other': []
    }
    for category, keywords in rules.items():
        if any(word in description for word in keywords):
            return category
    return "Other"

def generate_chart_image(labels, values, title):
    if not values or sum(values) == 0:
        return None
    
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.pie(values, labels=labels, autopct='%1.1f%%', startangle=90)
    ax.set_title(title)

    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight')
    buf.seek(0)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode('utf-8')

# --- Routes ---

@app.route("/")
def index():
    # 1. Filters
    start_str = (request.args.get("start") or "").strip()
    end_str = (request.args.get("end") or "").strip()
    selected_category = (request.args.get("category") or "").strip()

    # FIX: Function name is parse_date, not parse_date_or_none
    start_date = parse_date(start_str)
    end_date = parse_date(end_str)

    if not current_user.is_authenticated:
        return redirect(url_for('login'))

    # 2. Filtered Data
    q = Expense.query
    q = get_filtered_query(q, start_date, end_date, selected_category)
    expenses = q.order_by(Expense.date.desc(), Expense.id.desc()).all()
    filter_total = round(sum(e.amount for e in expenses), 2)

    # 3. Lifetime Total
    lifetime_total = db.session.query(func.sum(Expense.amount)).filter(Expense.user_id == current_user.id).scalar() or 0

    # 4. Monthly Comparison
    today = date.today()
    this_month_start = date(today.year, today.month, 1)
    last_month_end = this_month_start - timedelta(days=1)
    last_month_start = date(last_month_end.year, last_month_end.month, 1)

    this_month_sum = db.session.query(func.sum(Expense.amount)).filter(
        Expense.user_id == current_user.id,
        Expense.date >= this_month_start,
        Expense.date <= today
    ).scalar() or 0

    last_month_sum = db.session.query(func.sum(Expense.amount)).filter(
        Expense.user_id == current_user.id,
        Expense.date >= last_month_start,
        Expense.date <= last_month_end
    ).scalar() or 0

    diff_percent = 0
    diff_message = "No data for last month"
    diff_color = "text-slate-400"

    if last_month_sum > 0:
        diff_percent = ((this_month_sum - last_month_sum) / last_month_sum) * 100
        if diff_percent > 0:
            diff_message = f"{abs(diff_percent):.0f}% MORE than last month"
            diff_color = "text-rose-400"
        elif diff_percent < 0:
            diff_message = f"{abs(diff_percent):.0f}% LESS than last month"
            diff_color = "text-emerald-400"
        else:
            diff_message = "Same as last month"

    # 5. Prediction
    import calendar
    _, days_in_month = calendar.monthrange(today.year, today.month)
    current_day = today.day
    daily_avg = this_month_sum / current_day if current_day > 0 else 0
    predicted_total = daily_avg * days_in_month
    prediction_msg = f"₹{predicted_total:,.2f}"

    # 6. Charts
    cat_q = db.session.query(Expense.category, func.sum(Expense.amount))
    cat_q = get_filtered_query(cat_q, start_date, end_date, selected_category)
    cat_row = cat_q.group_by(Expense.category).all()
    cat_labels = [c for c, _ in cat_row]
    cat_values = [round(float(s or 0), 2) for _, s in cat_row]

    day_q = db.session.query(Expense.date, func.sum(Expense.amount))
    day_q = get_filtered_query(day_q, start_date, end_date, selected_category)
    day_row = day_q.group_by(Expense.date).order_by(Expense.date).all()
    day_labels = [d.isoformat() for d, _ in day_row]
    day_values = [round(float(s or 0), 2) for _, s in day_row]

    return render_template(
        "index.html",
        categories=CATEGORIES,
        today=date.today().isoformat(),
        expenses=expenses,
        filter_total=filter_total,
        lifetime_total=lifetime_total,
        this_month_sum=this_month_sum,
        diff_message=diff_message,
        diff_color=diff_color,
        prediction_msg=prediction_msg,
        start_str=start_str,
        end_str=end_str,
        selected_category=selected_category,
        cat_labels=cat_labels,
        cat_values=cat_values,
        day_labels=day_labels,
        day_values=day_values
    )

@app.route("/add", methods=['POST'])
@login_required
def add():
    try:
        amount = float(request.form.get("amount", 0))
        if amount <= 0: raise ValueError
    except ValueError:
        flash("Invalid amount", "error")
        return redirect(url_for("index"))

    description = request.form.get("description", "").strip()
    category = request.form.get("category")
    # Handle Auto-Categorization
    if category == "Auto":
        category = ai_categorize(description)
        flash(f'AI chose category: {category}', 'success')

    date_obj = parse_date(request.form.get("date")) or date.today()

    e = Expense(
        description=description,
        amount=amount,
        category=category,
        date=date_obj,
        user_id=current_user.id
    )
    db.session.add(e)
    db.session.commit()
    flash("Expense added", "success")
    return redirect(url_for("index"))

@app.route('/delete/<int:id>', methods=['POST'])
@login_required
def delete(id):
    e = db.session.get(Expense, id)
    if e and e.user_id == current_user.id:
        db.session.delete(e)
        db.session.commit()
        flash('Deleted', 'success')
    else:
        flash('Unauthorized', 'error')
    return redirect(url_for("index"))

@app.route('/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit(id):
    e = db.session.get(Expense, id)
    if not e or e.user_id != current_user.id:
        return redirect(url_for('index'))

    if request.method == 'POST':
        try:
            amount = float(request.form.get("amount", 0))
            if amount <= 0: raise ValueError
        except ValueError:
            flash("Invalid amount", "error")
            return redirect(url_for('edit', id=id))

        e.description = request.form.get("description", "").strip()
        e.amount = amount
        e.category = request.form.get("category")
        e.date = parse_date(request.form.get("date")) or date.today()
        db.session.commit()
        flash("Updated", "success")
        return redirect(url_for("index"))

    return render_template("edit.html", expense=e, categories=CATEGORIES)

@app.route("/export.csv")
@login_required
def export_csv():
    start_str = request.args.get("start", "").strip()
    end_str = request.args.get("end", "").strip()
    cat = request.args.get("category", "").strip()
    
    q = Expense.query
    q = get_filtered_query(q, parse_date(start_str), parse_date(end_str), cat)
    expenses = q.order_by(Expense.date).all()

    csv_data = "Date,Description,Category,Amount\n" + "\n".join([f"{e.date},{e.description},{e.category},{e.amount:.2f}" for e in expenses])
    return Response(csv_data, headers={"Content-Type": "text/csv", "Content-Disposition": "attachment; filename=expenses.csv"})

@app.route("/export_pdf")
@login_required
def export_pdf():
    start_str = request.args.get("start", "").strip()
    end_str = request.args.get("end", "").strip()
    cat = request.args.get("category", "").strip()
    start, end = parse_date(start_str), parse_date(end_str)

    q = Expense.query
    q = get_filtered_query(q, start, end, cat)
    expenses = q.order_by(Expense.date.desc()).all()
    total = sum(e.amount for e in expenses)

    # Chart for PDF
    cat_q = db.session.query(Expense.category, func.sum(Expense.amount))
    cat_q = get_filtered_query(cat_q, start, end, cat)
    cat_data = cat_q.group_by(Expense.category).all()
    
    labels = [c for c, _ in cat_data]
    values = [amount for _, amount in cat_data]
    chart_image = generate_chart_image(labels, values, "Expenses by Category")

    html = render_template("pdf_report.html", 
                         expenses=expenses, 
                         total=total, 
                         chart_image=chart_image,
                         generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
                         user=current_user)

    pdf_file = HTML(string=html).write_pdf()
    return Response(pdf_file, headers={"Content-Type": "application/pdf", "Content-Disposition": "attachment; filename=expense_report.pdf"})

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if User.query.filter_by(username=username).first():
            flash('Username already exists.', 'error')
            return redirect(url_for('register'))

        hashed_pw = generate_password_hash(password, method='pbkdf2:sha256')
        new_user = User(username=username, password_hash=hashed_pw)
        db.session.add(new_user)
        db.session.commit()
        
        login_user(new_user)
        flash('Account created!', 'success')
        return redirect(url_for('index'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            flash('Logged in successfully.', 'success')
            return redirect(url_for('index'))
        else:
            flash('Incorrect username or password.', 'error')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'success')
    return redirect(url_for('login'))

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        new_username = request.form.get('username')
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')

        if new_username and new_username != current_user.username:
            if User.query.filter_by(username=new_username).first():
                flash('Username taken.', 'error')
            else:
                current_user.username = new_username
                db.session.commit()
                flash('Username updated!', 'success')

        if new_password:
            if not current_password or not check_password_hash(current_user.password_hash, current_password):
                flash('Incorrect current password.', 'error')
            else:
                current_user.password_hash = generate_password_hash(new_password, method='pbkdf2:sha256')
                db.session.commit()
                flash('Password changed.', 'success')
        return redirect(url_for('profile'))
    return render_template('profile.html')

@app.route("/budget", methods=['GET', 'POST'])
@login_required
def budget():
    if request.method == 'POST':
        category = request.form.get("category")
        limit = float(request.form.get("limit"))
        
        existing = Budget.query.filter_by(user_id=current_user.id, category=category).first()
        if existing:
            existing.limit = limit
            flash(f"Budget updated for {category}", "success")
        else:
            new_b = Budget(category=category, limit=limit, user_id=current_user.id)
            db.session.add(new_b)
            flash(f"Budget set for {category}", "success")
        db.session.commit()
        return redirect(url_for('budget'))

    budgets = Budget.query.filter_by(user_id=current_user.id).all()
    budget_data = []
    for b in budgets:
        spent = db.session.query(func.sum(Expense.amount)).filter(
            Expense.user_id == current_user.id,
            Expense.category == b.category,
            func.strftime('%Y-%m', Expense.date) == date.today().strftime('%Y-%m')
        ).scalar() or 0
        
        percent = round((spent / b.limit) * 100) if b.limit > 0 else 0
        budget_data.append({
            'id': b.id,
            'category': b.category,
            'limit': b.limit,
            'spent': spent,
            'percent': percent,
            'width': min(percent, 100),
            'is_over': percent > 100
        })
    return render_template("budget.html", budget_data=budget_data, categories=CATEGORIES)

@app.route('/delete_budget/<int:id>', methods=['POST'])
@login_required
def delete_budget(id):
    b = db.session.get(Budget, id)
    if b and b.user_id == current_user.id:
        db.session.delete(b)
        db.session.commit()
        flash("Budget deleted", "success")
    else:
        flash("Error deleting budget", "error")
    return redirect(url_for('budget'))

if __name__ == "__main__":
    app.run(debug=True)
