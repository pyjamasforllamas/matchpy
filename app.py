import os
from datetime import datetime

from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, send_from_directory, jsonify
)

from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user,
    login_required, logout_user, current_user
)

from flask_socketio import SocketIO
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import stripe

# =====================
# LOAD ENV
# =====================
load_dotenv()

app = Flask(__name__)

app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///dating.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = "static/uploads"

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Stripe (SAFE)
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")


# =====================
# MODELS
# =====================

class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)

    username = db.Column(db.String(150), unique=True, nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)

    age = db.Column(db.Integer)
    gender = db.Column(db.String(50))
    bio = db.Column(db.Text)
    photo = db.Column(db.String(255))
    interests = db.Column(db.String(500))

    is_subscribed = db.Column(db.Boolean, default=False)
    subscription_end = db.Column(db.DateTime)

    stripe_customer_id = db.Column(db.String(255))
    stripe_subscription_id = db.Column(db.String(255))

    swipes_today = db.Column(db.Integer, default=0)
    last_swipe_date = db.Column(db.Date)


class Like(db.Model):
    __tablename__ = "likes"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    liked_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


class Pass(db.Model):
    __tablename__ = "passes"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    passed_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


# =====================
# AUTH
# =====================

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# =====================
# ROUTES
# =====================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":

        photo_path = None
        file = request.files.get("photo")

        if file and file.filename:
            filename = secure_filename(file.filename)
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
            photo_path = filename

        user = User(
            username=request.form["username"],
            email=request.form["email"].lower(),
            password=generate_password_hash(request.form["password"]),
            age=int(request.form.get("age", 0)),
            gender=request.form.get("gender"),
            bio=request.form.get("bio"),
            interests=request.form.get("interests"),
            photo=photo_path
        )

        try:
            db.session.add(user)
            db.session.commit()
            flash("Registration successful!", "success")
            return redirect(url_for("login"))
        except:
            db.session.rollback()
            flash("Username or email already exists.", "danger")

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":

        user = User.query.filter_by(
            email=request.form["email"].lower()
        ).first()

        if user and check_password_hash(user.password, request.form["password"]):
            login_user(user)
            return redirect(url_for("swipe"))

        flash("Invalid credentials", "danger")

    return render_template("login.html")


@app.route("/swipe")
@login_required
def swipe():

    today = datetime.utcnow().date()

    if current_user.last_swipe_date != today:
        current_user.swipes_today = 0
        current_user.last_swipe_date = today
        db.session.commit()

    remaining = "Unlimited" if current_user.is_subscribed else (10 - current_user.swipes_today)

    liked_ids = [l.liked_user_id for l in Like.query.filter_by(user_id=current_user.id).all()]
    passed_ids = [p.passed_user_id for p in Pass.query.filter_by(user_id=current_user.id).all()]

    excluded = liked_ids + passed_ids + [current_user.id]

    user = User.query.filter(User.id.notin_(excluded)).first()

    return render_template("swipe.html", user=user, remaining=remaining)


@app.route("/like/<int:target_id>", methods=["POST"])
@login_required
def like(target_id):

    if not current_user.is_subscribed and current_user.swipes_today >= 10:
        return jsonify({"status": "limit_reached"})

    db.session.add(Like(
        user_id=current_user.id,
        liked_user_id=target_id
    ))

    current_user.swipes_today += 1
    current_user.last_swipe_date = datetime.utcnow().date()

    db.session.commit()

    mutual = Like.query.filter_by(
        user_id=target_id,
        liked_user_id=current_user.id
    ).first()

    if mutual:
        flash("It's a match!", "success")

    return jsonify({"status": "success"})


@app.route("/pass/<int:target_id>", methods=["POST"])
@login_required
def pass_user(target_id):

    existing = Pass.query.filter_by(
        user_id=current_user.id,
        passed_user_id=target_id
    ).first()

    if not existing:
        db.session.add(Pass(
            user_id=current_user.id,
            passed_user_id=target_id
        ))
        db.session.commit()

    return jsonify({"status": "success"})


@app.route("/dashboard")
@login_required
def dashboard():

    matches = User.query.join(
        Like, Like.liked_user_id == User.id
    ).filter(
        Like.user_id == current_user.id
    ).all()

    return render_template("dashboard.html", matches=matches)


@app.route("/uploads/<filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("index"))


# =====================
# STRIPE (SIMPLE + SAFE)
# =====================

@app.route("/create-checkout-session", methods=["POST"])
@login_required
def create_checkout_session():

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer_email=current_user.email,
        line_items=[{
            "price_data": {
                "currency": "eur",
                "product_data": {"name": "MatchPy Premium"},
                "unit_amount": 999,
                "recurring": {"interval": "month"}
            },
            "quantity": 1
        }],
        success_url=url_for("swipe", _external=True),
        cancel_url=url_for("swipe", _external=True),
    )

    return redirect(session.url, code=303)


@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():

    payload = request.data
    sig = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig, STRIPE_WEBHOOK_SECRET
        )
    except:
        return "Invalid", 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]

        email = session.get("customer_email")
        user = User.query.filter_by(email=email).first()

        if user:
            user.is_subscribed = True
            db.session.commit()

    return "OK", 200

@app.route("/google7c1580915c9a5453.html")
def google_verify():
    return app.send_static_file("google7c1580915c9a5453.html")

# =====================
# RUN (RENDER SAFE)
# =====================

if __name__ == "__main__":
    with app.app_context():
        db.create_all()

    port = int(os.environ.get("PORT", 10000))

    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        debug=False
    )